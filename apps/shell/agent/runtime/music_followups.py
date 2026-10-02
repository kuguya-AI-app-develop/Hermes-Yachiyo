"""Bind one selected-Agent Music follow-up to verified server-side history."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import Any

from apps.shell.yachiyo_agent.daily_desktop import DailyDesktopEntrypointRuntimePlan
from apps.shell.yachiyo_agent.runtime_planner import _MODEL_INTENT_ACTION_EVIDENCE_RE

from .errors import AgentRuntimeError
from .goal_runtime import runtime_goal_assessment, runtime_goal_contract


def bind_selected_agent_music_followup(
    plan: DailyDesktopEntrypointRuntimePlan,
    *,
    original_goal: str,
    planning_goal: str,
    query: str,
    conversation_id: str,
    runnable: Mapping[str, Any],
    messages: Sequence[Any],
    get_run: Callable[[str], Mapping[str, Any]],
    list_run_events: Callable[[str], Sequence[Mapping[str, Any]]],
) -> tuple[DailyDesktopEntrypointRuntimePlan, dict[str, str]] | None:
    """Freeze a new goal's interpretation without changing historical goals.

    Caller metadata and assistant suggestions are not context authority. The
    latest persisted user turn must own a completed Music Goal for this same
    selected Agent, with independently verifiable native playback evidence.
    """

    agent_id = str(runnable.get("id") or "").strip()
    expected_query = re.sub(
        r"\s*(?:可以吗|好吗|好么|行吗|吗|嘛|呢|吧|please)[。！!]*$", "",
        " ".join(str(original_goal or "").split()).strip(), flags=re.IGNORECASE,
    ).strip()
    if (
        not agent_id or not conversation_id or not query
        or query != expected_query
        or runnable.get("kind") != "agent"
        or planning_goal != f"用Apple Music播放{query}"
        or len(original_goal) > 80
        or _MODEL_INTENT_ACTION_EVIDENCE_RE.search(original_goal)
        or re.search(r"[\n\r，,；;：:。!?？！\"'`“”‘’/\\]|https?://", original_goal)
        or re.match(
            r"\s*(?:不要|不用|别|取消|如果|假如|若|解释|说明|"
            r"(?:if|unless|don't|do not|never|explain|cancel)\b)",
            original_goal, re.IGNORECASE,
        )
    ):
        return None
    anchor = _verified_music_anchor(
        messages, conversation_id=conversation_id, runnable=runnable,
        get_run=get_run, list_run_events=list_run_events,
    )
    if anchor is None:
        return None
    decision = plan.decision
    core = getattr(getattr(decision, "plan", None), "task_core", None)
    contract = getattr(core, "goal_contract", None)
    raw_requests = plan.runtime_execution_envelope.get("requests")
    criteria = list(getattr(contract, "criteria", None) or [])
    if (
        contract is None or contract.original_goal != planning_goal
        or getattr(getattr(decision, "selected_intent", None), "kind", "") != "media_playback"
        or len(criteria) != 1
        or not criteria[0].effectful or criteria[0].response_satisfiable
        or set(criteria[0].required_capabilities) != {"media.playback"}
        or not isinstance(raw_requests, list) or len(raw_requests) != 1
        or not isinstance(raw_requests[0], Mapping)
        or not isinstance(raw_requests[0].get("action_target"), Mapping)
        or not isinstance(criteria[0].expected.get("target"), Mapping)
        or str(raw_requests[0].get("tool_name") or raw_requests[0].get("tool") or "")
        != "media.apple_music_play"
        or raw_requests[0].get("input") != {"query": query}
        or raw_requests[0].get("capability_id") != "media.playback"
        or set(criteria[0].source_step_ids) != {raw_requests[0].get("step_id")}
        or raw_requests[0].get("action_target", {}).get("query") != query
        or criteria[0].expected.get("target", {}).get("query") != query
        or criteria[0].expected.get("target", {}).get("action") != "play"
    ):
        return None
    binding = {
        "conversation_id": conversation_id,
        "runnable_id": agent_id,
        "source_user_message_id": anchor["user_message_id"],
        "source_assistant_message_id": anchor["assistant_message_id"],
        "source_run_id": anchor["run_id"],
        "source_goal_contract_id": anchor["contract_id"],
        "original_goal": original_goal,
        "query": query,
    }
    binding_id = hashlib.sha256(
        json.dumps(binding, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:20]
    binding["binding_id"] = binding_id
    bound_contract = contract.model_copy(update={
        "original_goal": original_goal,
        "contract_id": f"goal-contract-music-followup-{binding_id}",
    })
    bound_core = core.model_copy(update={"goal_contract": bound_contract})
    bound_intent = decision.selected_intent.model_copy(update={"user_goal": original_goal})
    bound_decision = decision.model_copy(update={
        "prompt": original_goal,
        "selected_intent": bound_intent,
        "candidate_intents": [
            bound_intent if item.intent_id == bound_intent.intent_id else item
            for item in decision.candidate_intents
        ],
        "plan": decision.plan.model_copy(update={"task_core": bound_core}),
    })
    envelope = deepcopy(plan.runtime_execution_envelope)
    envelope["task_core"]["goal_contract"] = bound_contract.model_dump(mode="json")
    # Keep the already admitted tools, routes, steps, inputs and approval flags.
    # No new planner selection or policy overlay is created by this binding.
    return replace(plan, decision=bound_decision, runtime_execution_envelope=envelope), binding


def _metadata(message: Any) -> Mapping[str, Any]:
    try:
        value = json.loads(str(getattr(message, "metadata_json", "{}") or "{}"))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _verified_music_anchor(
    messages: Sequence[Any], *, conversation_id: str,
    runnable: Mapping[str, Any], get_run: Callable[[str], Mapping[str, Any]],
    list_run_events: Callable[[str], Sequence[Mapping[str, Any]]],
) -> dict[str, str] | None:
    agent_id = str(runnable.get("id") or "")
    # Stop at the latest user turn; an older musical request cannot survive an
    # intervening unrelated turn, another Agent, failed run or pending approval.
    reverse = list(reversed(messages))
    latest_user_index = next((i for i, m in enumerate(reverse) if m.role == "user"), None)
    if latest_user_index is None:
        return None
    user = reverse[latest_user_index]
    meta = _metadata(user)
    if (
        user.session_id != conversation_id or user.status != "completed"
        or not user.message_id or meta.get("runnable_id") != agent_id
        or meta.get("runnable_kind") != "agent"
    ):
        return None
    assistants = [m for m in reverse[:latest_user_index] if m.role == "assistant"]
    if len(assistants) != 1:
        return None
    assistant = assistants[0]
    assistant_meta = _metadata(assistant)
    run_id = str(assistant_meta.get("run_id") or "")
    if (
        assistant.session_id != conversation_id or assistant.status != "completed"
        or not assistant.message_id or not run_id
        or assistant_meta.get("runnable_id") != agent_id
        or assistant_meta.get("runnable_kind") != "agent"
    ):
        return None
    try:
        run = get_run(run_id)
        if not isinstance(run, Mapping):
            return None
        if (
            run.get("run_id") != run_id or run.get("kind") != "agent_run"
            or run.get("runnable_id") != agent_id or run.get("status") != "completed"
        ):
            return None
        previous_goal = str(run.get("user_goal") or "")
        allowed_user_texts = {previous_goal}
        for key in ("name", "nickname"):
            label = str(runnable.get(key) or "").strip()
            if label:
                allowed_user_texts.add(f"@{label} {previous_goal}")
        if not previous_goal or str(user.content).strip() not in allowed_user_texts:
            return None
        bindings = [
            event.get("payload")
            for event in list_run_events(run_id)
            if isinstance(event, Mapping)
            and event.get("event_type") == "agent.chat.user_turn.bound"
            and event.get("visibility") == "internal"
            and event.get("actor") == "native_runtime"
        ]
        if len(bindings) != 1 or bindings[0] != {
            "conversation_id": conversation_id,
            "user_message_id": user.message_id,
            "assistant_message_id": assistant.message_id,
            "runnable_id": agent_id,
            "original_goal": previous_goal,
        }:
            return None
        contract = runtime_goal_contract(
            run_id=run_id, original_goal=previous_goal,
            runtime_execution_envelope=None, runtime_execution_metadata=None,
            messages=[], timeline=run.get("timeline") or [],
        )
        if contract is None or contract.intent_kind != "media_playback":
            return None
        criteria = list(contract.criteria)
        if len(criteria) != 1 or set(criteria[0].required_capabilities) != {"media.playback"}:
            return None
        target = criteria[0].expected.get("target", {})
        if (
            not isinstance(target, Mapping)
            or target.get("kind") != "media" or target.get("action") != "play"
            or target.get("app_name") != "Music"
        ):
            return None
        if not runtime_goal_assessment(contract, run.get("timeline") or []).completed:
            return None
    except (AgentRuntimeError, KeyError, TypeError, ValueError):
        return None
    return {
        "user_message_id": user.message_id,
        "assistant_message_id": assistant.message_id,
        "run_id": run_id,
        "contract_id": contract.contract_id,
    }
