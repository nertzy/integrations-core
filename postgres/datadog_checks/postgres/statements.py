# (C) Datadog, Inc. 2020-present
# All rights reserved
# Licensed under Simplified BSD License (see LICENSE)
from __future__ import unicode_literals

import copy
import time
from typing import Dict, Generator, List

import psycopg2
import psycopg2.extras
from cachetools import TTLCache

from datadog_checks.base import is_affirmative
from datadog_checks.base.utils.common import to_native_string
from datadog_checks.base.utils.db.sql import compute_sql_signature
from datadog_checks.base.utils.db.statement_metrics import StatementMetrics
from datadog_checks.base.utils.db.utils import DBMAsyncJob, DbRow, default_json_event_encoding
from datadog_checks.base.utils.serialization import json

from .version_utils import V9_4

try:
    import datadog_agent
except ImportError:
    from ..stubs import datadog_agent

STATEMENTS_QUERY = """
SELECT {cols}
  FROM {pg_stat_statements_view} as pg_stat_statements
  LEFT JOIN pg_roles
         ON pg_stat_statements.userid = pg_roles.oid
  LEFT JOIN pg_database
         ON pg_stat_statements.dbid = pg_database.oid
  WHERE query != '<insufficient privilege>'
  AND query NOT LIKE 'EXPLAIN %%'
  {filters}
  LIMIT {limit}
"""

# Use pg_stat_statements(false) when available as an optimization to avoid pulling SQL text from disk
PG_STAT_STATEMENTS_COUNT_QUERY = "SELECT COUNT(*) FROM pg_stat_statements(false)"
PG_STAT_STATEMENTS_COUNT_QUERY_LT_9_4 = "SELECT COUNT(*) FROM pg_stat_statements"

DEFAULT_STATEMENTS_LIMIT = 10000

# Required columns for the check to run
PG_STAT_STATEMENTS_REQUIRED_COLUMNS = frozenset({'calls', 'query', 'rows'})

PG_STAT_STATEMENTS_METRICS_COLUMNS = frozenset(
    {
        'calls',
        'rows',
        'total_time',
        'total_exec_time',
        'shared_blks_hit',
        'shared_blks_read',
        'shared_blks_dirtied',
        'shared_blks_written',
        'local_blks_hit',
        'local_blks_read',
        'local_blks_dirtied',
        'local_blks_written',
        'temp_blks_read',
        'temp_blks_written',
    }
)

PG_STAT_STATEMENTS_TAG_COLUMNS = frozenset(
    {
        'datname',
        'rolname',
        'query',
    }
)

PG_STAT_STATEMENTS_OPTIONAL_COLUMNS = frozenset({'queryid'})

PG_STAT_ALL_DESIRED_COLUMNS = (
    PG_STAT_STATEMENTS_METRICS_COLUMNS | PG_STAT_STATEMENTS_TAG_COLUMNS | PG_STAT_STATEMENTS_OPTIONAL_COLUMNS
)


def _row_key(row):
    """
    :param row: a normalized row from pg_stat_statements
    :return: a tuple uniquely identifying this row
    """
    return row['query_signature'], row['datname'], row['rolname']


DEFAULT_COLLECTION_INTERVAL = 10


class PostgresStatementMetrics(DBMAsyncJob):
    """Collects telemetry for SQL statements"""

    def __init__(self, check, config, shutdown_callback):
        collection_interval = float(
            config.statement_metrics_config.get('collection_interval', DEFAULT_COLLECTION_INTERVAL)
        )
        if collection_interval <= 0:
            collection_interval = DEFAULT_COLLECTION_INTERVAL
        super(PostgresStatementMetrics, self).__init__(
            check,
            run_sync=is_affirmative(config.statement_metrics_config.get('run_sync', False)),
            enabled=is_affirmative(config.statement_metrics_config.get('enabled', True)),
            expected_db_exceptions=(psycopg2.errors.DatabaseError,),
            min_collection_interval=config.min_collection_interval,
            config_host=config.host,
            dbms="postgres",
            rate_limit=1 / float(collection_interval),
            job_name="query-metrics",
            shutdown_callback=shutdown_callback,
        )
        self._metrics_collection_interval = collection_interval
        self._config = config
        self._state = StatementMetrics()
        self._stat_column_cache = []
        self._obfuscate_options = to_native_string(json.dumps(self._config.obfuscator_options))
        # full_statement_text_cache: limit the ingestion rate of full statement text events per query_signature
        self._full_statement_text_cache = TTLCache(
            maxsize=config.full_statement_text_cache_max_size,
            ttl=60 * 60 / config.full_statement_text_samples_per_hour_per_query,
        )

    def _execute_query(self, cursor, query, params=()):
        try:
            self._log.debug("Running query [%s] %s", query, params)
            cursor.execute(query, params)
            return cursor.fetchall()
        except (psycopg2.ProgrammingError, psycopg2.errors.QueryCanceled) as e:
            # A failed query could've derived from incorrect columns within the cache. It's a rare edge case,
            # but the next time the query is run, it will retrieve the correct columns.
            self._stat_column_cache = []
            raise e

    def _get_pg_stat_statements_columns(self):
        """
        Load the list of the columns available under the `pg_stat_statements` table. This must be queried because
        version is not a reliable way to determine the available columns on `pg_stat_statements`. The database can
        be upgraded without upgrading extensions, even when the extension is included by default.
        """
        if self._stat_column_cache:
            return self._stat_column_cache

        # Querying over '*' with limit 0 allows fetching only the column names from the cursor without data
        query = STATEMENTS_QUERY.format(
            cols='*', pg_stat_statements_view=self._config.pg_stat_statements_view, limit=0, filters=""
        )
        cursor = self._check._get_db(self._config.dbname).cursor()
        self._execute_query(cursor, query, params=(self._config.dbname,))
        col_names = [desc[0] for desc in cursor.description] if cursor.description else []
        self._stat_column_cache = col_names
        return col_names

    def run_job(self):
        self._tags_no_db = [t for t in self._tags if not t.startswith('db:')]
        self.collect_per_statement_metrics()

    def _payload_pg_version(self):
        version = self._check.version
        if not version:
            return ""
        return 'v{major}.{minor}.{patch}'.format(major=version.major, minor=version.minor, patch=version.patch)

    def collect_per_statement_metrics(self):
        # exclude the default "db" tag from statement metrics & FQT events because this data is collected from
        # all databases on the host. For metrics the "db" tag is added during ingestion based on which database
        # each query came from.
        try:
            rows = self._collect_metrics_rows()
            if not rows:
                return
            for event in self._rows_to_fqt_events(rows):
                self._check.database_monitoring_query_sample(json.dumps(event, default=default_json_event_encoding))
            for row in rows:
                # Truncate query text to the maximum length supported by metrics tags
                row.data['query'] = row.data['query'][0:200]
                # Inject metadata into the row. Prefix with `dd` to prevent name clashing.
                row.data['dd_tables'] = row.metadata.parse_tables_csv()
                row.data['dd_commands'] = row.metadata.commands
            payload = {
                'host': self._check.resolved_hostname,
                'timestamp': time.time() * 1000,
                'min_collection_interval': self._metrics_collection_interval,
                'tags': self._tags_no_db,
                'postgres_rows': [row.data for row in rows],
                'postgres_version': self._payload_pg_version(),
                'ddagentversion': datadog_agent.get_version(),
            }
            self._check.database_monitoring_query_metrics(json.dumps(payload, default=default_json_event_encoding))
        except Exception:
            self._log.exception('Unable to collect statement metrics due to an error')
            return []

    def _load_pg_stat_statements(self):
        try:
            available_columns = set(self._get_pg_stat_statements_columns())
            missing_columns = PG_STAT_STATEMENTS_REQUIRED_COLUMNS - available_columns
            if len(missing_columns) > 0:
                self._log.warning(
                    'Unable to collect statement metrics because required fields are unavailable: %s',
                    ', '.join(list(missing_columns)),
                )
                self._check.count(
                    "dd.postgres.statement_metrics.error",
                    1,
                    tags=self._tags
                    + [
                        "error:database-missing_pg_stat_statements_required_columns",
                    ]
                    + self._check._get_debug_tags(),
                    hostname=self._check.resolved_hostname,
                )
                return []

            query_columns = sorted(list(available_columns & PG_STAT_ALL_DESIRED_COLUMNS))
            params = ()
            filters = ""
            if self._config.dbstrict:
                filters = "AND pg_database.datname = %s"
                params = (self._config.dbname,)
            return self._execute_query(
                self._check._get_db(self._config.dbname).cursor(cursor_factory=psycopg2.extras.DictCursor),
                STATEMENTS_QUERY.format(
                    cols=', '.join(query_columns),
                    pg_stat_statements_view=self._config.pg_stat_statements_view,
                    filters=filters,
                    limit=DEFAULT_STATEMENTS_LIMIT,
                ),
                params=params,
            )
        except psycopg2.Error as e:
            error_tag = "error:database-{}".format(type(e).__name__)

            if (
                isinstance(e, psycopg2.errors.ObjectNotInPrerequisiteState)
            ) and 'pg_stat_statements must be loaded' in str(e.pgerror):
                error_tag = "error:database-{}-pg_stat_statements_not_loaded".format(type(e).__name__)
                self._log.warning(
                    "Unable to collect statement metrics because pg_stat_statements shared library is not loaded"
                )
            elif isinstance(e, psycopg2.errors.UndefinedTable) and 'pg_stat_statements' in str(e.pgerror):
                error_tag = "error:database-{}-pg_stat_statements_not_created".format(type(e).__name__)
                self._log.warning(
                    "Unable to collect statement metrics because pg_stat_statements is not created in this database"
                )
            else:
                self._log.warning("Unable to collect statement metrics because of an error running queries: %s", e)

            self._check.count(
                "dd.postgres.statement_metrics.error",
                1,
                tags=self._tags + [error_tag] + self._check._get_debug_tags(),
                hostname=self._check.resolved_hostname,
            )

            return []

    def _emit_pg_stat_statements_metrics(self):
        query = PG_STAT_STATEMENTS_COUNT_QUERY_LT_9_4 if self._check.version < V9_4 else PG_STAT_STATEMENTS_COUNT_QUERY
        try:
            rows = self._execute_query(
                self._check._get_db(self._config.dbname).cursor(cursor_factory=psycopg2.extras.DictCursor),
                query,
            )
            count = 0
            if rows:
                count = rows[0][0]
            self._check.count(
                "postgresql.pg_stat_statements.max",
                self._check.pg_settings.get("pg_stat_statements.max", 0),
                tags=self._tags,
                hostname=self._check.resolved_hostname,
            )
            self._check.count(
                "postgresql.pg_stat_statements.count",
                count,
                tags=self._tags,
                hostname=self._check.resolved_hostname,
            )
        except psycopg2.Error as e:
            self._log.warning("Failed to query for pg_stat_statements count: %s", e)

    def _collect_metrics_rows(self):
        # type: () -> List[DbRow]
        rows = self._load_pg_stat_statements()
        if rows:
            self._emit_pg_stat_statements_metrics()

        db_rows = self._normalize_queries(rows)
        if not db_rows:
            return []

        # When we compute the derivative, there will be less rows, so we need to remap the
        # remaining rows' metadata back after the computation.
        query_sig_to_metadata = {db_row.data['query_signature']: db_row.metadata for db_row in db_rows}
        available_columns = set(db_rows[0].data.keys())
        metric_columns = available_columns & PG_STAT_STATEMENTS_METRICS_COLUMNS
        rows = self._state.compute_derivative_rows([db_row.data for db_row in db_rows], metric_columns, key=_row_key)
        self._check.gauge(
            'dd.postgres.queries.query_rows_raw',
            len(rows),
            tags=self._tags + self._check._get_debug_tags(),
            hostname=self._check.resolved_hostname,
        )
        return [DbRow(row, query_sig_to_metadata.get(row['query_signature'])) for row in rows]

    def _normalize_queries(self, rows):
        # type: (Dict) -> List[DbRow]
        normalized_rows = []
        for row in rows:
            normalized_row = dict(copy.copy(row))
            try:
                statement = json.loads(datadog_agent.obfuscate_sql(row['query'], self._obfuscate_options))
            except Exception as e:
                # obfuscation errors are relatively common so only log them during debugging
                self._log.debug("Failed to obfuscate query '%s': %s", row['query'], e)
                continue

            obfuscated_query = statement['query']
            normalized_row['query'] = obfuscated_query
            normalized_row['query_signature'] = compute_sql_signature(obfuscated_query)
            normalized_rows.append(DbRow(normalized_row, statement['metadata']))

        return normalized_rows

    def _rows_to_fqt_events(self, rows):
        # type: (List[DbRow]) -> Generator
        for row in rows:
            query_cache_key = _row_key(row.data)
            if query_cache_key in self._full_statement_text_cache:
                continue
            self._full_statement_text_cache[query_cache_key] = True
            row_tags = self._tags_no_db + [
                "db:{}".format(row.data['datname']),
                "rolname:{}".format(row.data['rolname']),
            ]
            yield {
                "timestamp": time.time() * 1000,
                "host": self._check.resolved_hostname,
                "ddagentversion": datadog_agent.get_version(),
                "ddsource": "postgres",
                "ddtags": ",".join(row_tags),
                "dbm_type": "fqt",
                "db": {
                    "instance": row.data['datname'],
                    "query_signature": row.data['query_signature'],
                    "statement": row.data['query'],
                },
                "postgres": {
                    "datname": row.data["datname"],
                    "rolname": row.data["rolname"],
                },
            }
