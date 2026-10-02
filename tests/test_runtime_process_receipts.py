from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from apps.shell.agent.runtime.events import RUNTIME_EXECUTION_PROVENANCE_KEY
from apps.shell.agent.runtime.goal_contract import GoalContract, GoalCriterion
from apps.shell.agent.runtime.process_receipts import verified_terminal_process_observation
from apps.shell.yachiyo_agent.terminal_plan_hints import terminal_command_hint
from apps.shell.agent.runtime.model_intent_planning import (
    model_intent_proposal_from_tool_requests,
    MODEL_INTENT_PLANNING_TOOL_NAME,
)


@pytest.fixture
def process_case():
    target = {"kind": "local_compute", "action": "run_command", "command": "printf smoke"}
    criterion = GoalCriterion(
        criterion_id="process-criterion", description="Execute one exact command",
        effectful=True, required_capabilities=("terminal.execution",),
        source_step_ids=("process-step",), expected={"state": "fulfilled", "target": target},
    )
    contract = GoalContract(contract_id="process-goal", run_id="process-run",
                            original_goal="Execute printf smoke", criteria=(criterion,))
    event = {"tool": "terminal.run", "run_id": "process-run", "step_id": "process-step",
             "action_target": target, "input_preview": {"command": "printf smoke"}}
    result = {"ok": True, "returncode": 0, "timed_out": False,
              RUNTIME_EXECUTION_PROVENANCE_KEY: {"source": "local_tool_broker", "version": 1}}
    return contract, event, result


def test_exact_local_process_receipt_proves_only_declared_command(process_case):
    contract, event, result = process_case
    observed = verified_terminal_process_observation(
        contract, event, result, runtime_owned=True, eligible_criterion_ids=("process-criterion",)
    )
    assert observed == {"state": "fulfilled", "target": event["action_target"]}
    assert "stdout" not in observed


@pytest.mark.parametrize("mutation", [
    "untrusted", "wrong_run", "wrong_tool", "wrong_step", "wrong_command", "failure",
    "boolean_exit", "timed_out", "missing_provenance", "wrong_provider", "missing_exit",
    "wrong_version", "ineligible", "output_effect",
])
def test_process_receipt_cannot_forge_authority_or_other_effects(process_case, mutation):
    contract, event, result = process_case
    event, result = deepcopy(event), deepcopy(result)
    runtime_owned, eligible = True, ("process-criterion",)
    if mutation == "untrusted": runtime_owned = False
    elif mutation == "wrong_run": event["run_id"] = "other-run"
    elif mutation == "wrong_tool": event["tool"] = "python.run"
    elif mutation == "wrong_step": event["step_id"] = "other-step"
    elif mutation == "wrong_command": event["input_preview"]["command"] = "rm target"
    elif mutation == "failure": result.update(ok=False, returncode=7)
    elif mutation == "boolean_exit": result["returncode"] = False
    elif mutation == "timed_out": result["timed_out"] = True
    elif mutation == "missing_provenance": result.pop(RUNTIME_EXECUTION_PROVENANCE_KEY)
    elif mutation == "wrong_provider": result[RUNTIME_EXECUTION_PROVENANCE_KEY]["source"] = "model"
    elif mutation == "missing_exit": result.pop("returncode")
    elif mutation == "wrong_version": result[RUNTIME_EXECUTION_PROVENANCE_KEY]["version"] = 2
    elif mutation == "ineligible": eligible = ()
    elif mutation == "output_effect":
        contract = replace(contract, criteria=(replace(contract.criteria[0],
                           required_effects=("file_written",)),))
    result["postcondition_verified"] = True  # A provider/model flag grants no authority.
    assert verified_terminal_process_observation(
        contract, event, result, runtime_owned=runtime_owned, eligible_criterion_ids=eligible
    ) == {}


def test_terminal_hint_preserves_nested_shell_quotes_and_rejects_prose():
    assert terminal_command_hint("Run python -c 'print(7)'") == {"command": "python -c 'print(7)'"}
    assert terminal_command_hint("Run `printf smoke`") == {"command": "printf smoke"}
    assert terminal_command_hint("Execute the exact command `printf smoke; exit 7` with shell=true") == {}
    assert terminal_command_hint('Execute command payload: {"command":"printf smoke; exit 7"}') == {}


def test_grounded_patch_bytes_survive_semantic_proposal_parsing():
    patch = "@@ -1 +1 @@\n-before\n+after\n"
    proposal = model_intent_proposal_from_tool_requests([{
        "tool": MODEL_INTENT_PLANNING_TOOL_NAME,
        "input": {
            "intent_kind": "file_operation", "planning_goal": "Write one exact patch",
            "action_evidence": "Write",
            "subgoals": [{
                "capability_id": "file.workspace_write", "action_id": "apply_patch",
                "planning_goal": "Write one exact patch", "action_evidence": "Write",
                "input_slots": [{"slot": "patch", "value": patch, "evidence_quote": patch}],
            }],
        },
    }])
    assert proposal is not None
    slot = proposal.subgoals[0].input_slots[0]
    assert slot.value == patch
    assert slot.evidence_quote == patch
