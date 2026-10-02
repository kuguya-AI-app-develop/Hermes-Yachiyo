"""Private local AX snapshots for hash-bound prepared-submit approval resumes."""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any, Callable

_SCOPE_KEYS = (
    "tool",
    "plan_id",
    "decision_id",
    "tool_plan_id",
    "step_id",
    "request_id",
    "tool_call_id",
)


class _LocalPreparedSubmitObservation:
    def __init__(self, request: Mapping[str, Any], run_id: str, result: Mapping[str, Any]):
        self.scope = {key: str(request.get(key) or "") for key in _SCOPE_KEYS}
        self.run_id = run_id
        self.result = deepcopy(dict(result))
        self.used = False


def observe_actual_prepared_submit_target(
    broker: Any,
    *,
    request: Mapping[str, Any],
    run_id: str,
    allowed_tools: Sequence[str],
    budget: Any,
    assert_active: Callable[[], None],
) -> Any:
    """Read through the existing local broker only while the approval claim owns the run."""
    from apps.shell.agent.tools.broker import ToolBroker

    from .tool_execution import (
        LOCAL_DESKTOP_PROVIDER_ID,
        LOCAL_DESKTOP_PROVIDER_KIND,
        RUNTIME_PERSISTED_PREPARED_SUBMIT_RECEIPT_KEY,
    )

    receipt = request.get(RUNTIME_PERSISTED_PREPARED_SUBMIT_RECEIPT_KEY)
    if (
        not isinstance(broker, ToolBroker)
        or "desktop.ui_elements" not in allowed_tools
        or not isinstance(receipt, Mapping)
        or receipt.get("run_id") != run_id
        or receipt.get("provider_kind") != LOCAL_DESKTOP_PROVIDER_KIND
        or receipt.get("provider_id") != LOCAL_DESKTOP_PROVIDER_ID
        or not all(str(request.get(key) or "") for key in _SCOPE_KEYS)
        or request.get("run_id") != run_id
    ):
        return None
    assert_active()
    budget.claim_tool_call("desktop.ui_elements")
    result = broker.call(
        "desktop.ui_elements",
        {
            "app_name": str(receipt.get("target_app_name") or ""),
            "limit": 80,
        },
    )
    assert_active()
    if not isinstance(result, Mapping):
        return None
    return _LocalPreparedSubmitObservation(request, run_id, result)


def consume_actual_prepared_submit_observation(
    token: Any,
    *,
    request: Mapping[str, Any],
    run_id: str,
) -> dict[str, Any]:
    if type(token) is not _LocalPreparedSubmitObservation or token.used:
        return {}
    token.used = True
    result = token.result
    token.result = {}
    scope = {key: str(request.get(key) or "") for key in _SCOPE_KEYS}
    if token.run_id != run_id or token.scope != scope or not all(scope.values()):
        return {}
    if request.get("run_id") != run_id:
        return {}
    data = result.get("data")
    if (
        result.get("ok") is not True
        or result.get("approval_required")
        or result.get("permission_error")
        or result.get("verification_failed")
        or not isinstance(data, Mapping)
        or data.get("truncated") is True
    ):
        return {}
    return result
