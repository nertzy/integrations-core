# (C) Datadog, Inc. 2022-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)

from typing import Any, Callable, Dict

from datadog_checks.base import AgentCheck
from datadog_checks.base.stubs.aggregator import AggregatorStub

# from datadog_checks.dev.utils import get_metadata_metrics
# from datadog_checks.teradata import TeradataCheck


def test_check(dd_run_check, aggregator, instance):
    # type: (Callable[[AgentCheck, bool], None], AggregatorStub, Dict[str, Any]) -> None
    pass
    # check = TeradataCheck('teradata', {}, [instance])
    # dd_run_check(check)
    #
    # aggregator.assert_all_metrics_covered()
    # aggregator.assert_metrics_using_metadata(get_metadata_metrics())


def test_emits_critical_service_check_when_service_is_down(dd_run_check, aggregator, instance):
    pass


#     # type: (Callable[[AgentCheck, bool], None], AggregatorStub, Dict[str, Any]) -> None
# check = TeradataCheck('teradata', {}, [instance])
# dd_run_check(check)
# aggregator.assert_service_check('teradata.can_connect', TeradataCheck.CRITICAL)