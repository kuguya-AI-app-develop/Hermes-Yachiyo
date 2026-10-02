"""Narrow completion receipts for an explicitly requested local process."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from apps.shell.agent.runtime.events import (
    RUNTIME_EXECUTION_PROVENANCE_KEY,
    RUNTIME_EXECUTION_PROVENANCE_VERSION,
    RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
)
from apps.shell.agent.runtime.goal_contract import GoalContract


def verified_terminal_process_observation(
    contract: GoalContract,
    event: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    runtime_owned: bool,
    eligible_criterion_ids: tuple[str, ...],
) -> dict[str, Any]:
    """Prove process completion, without proving arbitrary command effects.

    An exit code only fulfils the explicit ``run_command`` action. File writes,
    remote effects and other requested postconditions retain their separate
    verifiers. The caller authenticates executor/run/plan/call lineage first.
    """
    provenance = result.get(RUNTIME_EXECUTION_PROVENANCE_KEY)
    if not (
        runtime_owned
        and isinstance(provenance, Mapping)
        and provenance.get("version") == RUNTIME_EXECUTION_PROVENANCE_VERSION
        and provenance.get("source") == RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE
        and str(event.get("tool") or event.get("detail") or "") == "terminal.run"
        and str(event.get("run_id") or "") == contract.run_id
        and result.get("ok") is True
        and type(result.get("returncode")) is int
        and result["returncode"] == 0
        and result.get("timed_out") is False
    ):
        return {}
    request = event.get("input_preview")
    target = event.get("action_target")
    step_id = str(event.get("step_id") or event.get("planner_step_id") or "")
    if not isinstance(request, Mapping) or not isinstance(target, Mapping):
        return {}
    command = str(request.get("command") or "")
    if not command or target != {
        "kind": "local_compute", "action": "run_command", "command": command
    }:
        return {}
    criteria = [criterion for criterion in contract.criteria
                if criterion.criterion_id in eligible_criterion_ids]
    if not criteria or any(
        criterion.required_capabilities != ("terminal.execution",)
        or criterion.required_effects
        or criterion.required_verification_predicates
        or criterion.verifier_step_ids
        or step_id not in criterion.source_step_ids
        or dict(criterion.expected) != {"state": "fulfilled", "target": dict(target)}
        for criterion in criteria
    ):
        return {}
    return {"state": "fulfilled", "target": dict(target)}
