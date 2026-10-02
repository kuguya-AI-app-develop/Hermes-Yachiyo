"""Grounded semantic planning fixtures shared by public entrypoint tests."""

import json


def semantic_fixture_goal(actions):
    """Declare the exact executable objective that an approval fixture owns."""
    parts = []
    for tool, payload in actions:
        if tool == "terminal_run":
            part = "Execute the exact command `" + payload["command"] + "`"
            for key, value in payload.items():
                if key != "command":
                    part += " with " + key + "=" + str(value).lower()
        elif tool == "workspace_write_patch":
            part = ("Write " + payload["path"] + " using this exact patch:\n"
                    + payload["patch"] + "\nEnd of exact patch.")
        else:
            raise AssertionError("Declare an explicit semantic fixture action")
        parts.append(part)
    return "\nThen ".join(parts)

def semantic_fixture_model_plan(messages, tools, actions, original_goal):
    """Supply grounded semantic proposals to the real planning boundary."""
    from apps.shell.agent.runtime.model_intent_planning import MODEL_INTENT_PLANNING_TOOL_NAME

    names = {(tool.get("function") or {}).get("name") for tool in tools or []}
    if MODEL_INTENT_PLANNING_TOOL_NAME not in names:
        return None
    assert names == {MODEL_INTENT_PLANNING_TOOL_NAME}
    assert any(original_goal in str(message.get("content") or "")
               or original_goal in str(json.loads(message["content"]).get("original_goal", ""))
               for message in messages if message.get("role") == "user")
    subgoals = []
    for tool, payload in actions:
        capability, action, verb, kind = {
            "terminal_run": ("terminal.execution", "run_command", "Execute", "code_task"),
            "workspace_write_patch": ("file.workspace_write", "apply_patch", "Write", "file_operation"),
        }[tool]
        slots = []
        for slot, value in payload.items():
            text = str(value).lower() if isinstance(value, bool) else str(value)
            assert text in original_goal
            slots.append({"slot": slot, "value": text, "evidence_quote": text})
        subgoals.append({
            "capability_id": capability, "action_id": action,
            "planning_goal": semantic_fixture_goal([(tool, payload)]),
            "action_evidence": verb, "input_slots": slots,
        })
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "fixture-semantic-plan", "type": "function",
        "function": {"name": MODEL_INTENT_PLANNING_TOOL_NAME,
                     "arguments": json.dumps({
                         "intent_kind": kind, "planning_goal": original_goal,
                         "action_evidence": verb, "subgoals": subgoals,
                     })},
    }]}

def semantic_fixture_execution_envelope(fake_chat, actions, original_goal):
    """Compile a planning turn through Runtime's actual API input contract.

    Agent Studio accepts a Runtime envelope from its caller. Model suggestions
    still pass the parser, original-goal grounding and capability compiler;
    fixtures supply no execution receipt or completion authority.
    """
    from apps.shell.agent.runtime.model_intent_planning import (
        model_intent_planning_tool_schema,
        model_intent_proposal_from_tool_requests,
        direct_tool_selection_from_model_intent_proposal,
    )
    from apps.shell.yachiyo_agent.runtime_execution import runtime_execution_envelope_payload

    allowed = list(dict.fromkeys({
        "terminal_run": "terminal.run", "workspace_write_patch": "workspace.write_patch",
    }[tool] for tool, _ in actions))
    response = fake_chat(
        "https://api.example.test/v1", "demo-model", "sk-secret",
        [{"role": "user", "content": json.dumps({"original_goal": original_goal})}],
        tools=[model_intent_planning_tool_schema()],
    )
    proposal = model_intent_proposal_from_tool_requests(response["tool_calls"])
    assert proposal is not None
    selection = direct_tool_selection_from_model_intent_proposal(proposal, original_goal, allowed)
    envelope = runtime_execution_envelope_payload(selection.decision, allowed_tools=allowed, full_plan=True)
    assert envelope
    return envelope
