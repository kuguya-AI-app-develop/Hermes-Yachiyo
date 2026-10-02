from __future__ import annotations

import copy
import hashlib
import json

import pytest

from apps.shell.agent.runtime.desktop_execution_providers import (
    LOCAL_DESKTOP_PROVIDER_ID,
    LOCAL_DESKTOP_PROVIDER_KIND,
)
from apps.shell.agent.runtime.dispatch_semantics import (
    exact_native_dispatch_receipt_matches,
)
from apps.shell.agent.runtime.events import (
    RUNTIME_EXECUTION_PROVENANCE_KEY,
    RUNTIME_EXECUTION_PROVENANCE_VERSION,
    RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
    redact_run_event_payload,
)
from apps.shell.agent.runtime.input_bindings import resolve_workspace_file_selection
from apps.shell.agent.runtime.goal_contract import (
    GoalContract,
    GoalCoordinator,
    GoalCriterion,
)
from apps.shell.agent.runtime.goal_runtime import (
    complete_response_only_goal,
    goal_assessment_event_payload,
    goal_contract_event_payload,
    pending_semantic_artifact_assessment_candidates,
    planned_goal_contract_payload,
    runtime_goal_assessment,
    runtime_goal_contract,
)
from apps.shell.agent.runtime.outcome_evaluator import evaluate_main_chat_outcome
from apps.shell.agent.runtime.recovery_lineage import (
    RUNTIME_PRIVATE_RECOVERY_AUTHORITY,
    RUNTIME_PRIVATE_RECOVERY_CONTEXT_VERSION,
    trusted_recovery_trace_fields,
)
from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner
from apps.shell.yachiyo_agent.runtime_execution import (
    runtime_execution_envelope_from_decision,
)


def _media_envelope() -> dict:
    decision = RuntimePlanner().decision(
        "Play Moonlight",
        allowed_tools=["media.apple_music_play"],
    )
    return {
        "task_core": decision.plan.task_core.model_dump(),  # type: ignore[union-attr]
    }


def _trusted_media_terminal_event(contract: GoalContract) -> dict:
    return {
        "event": "agent.tool.call",
        "run_id": contract.run_id,
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
        "plan_id": "plan-media-terminal",
        "step_id": "control-media-playback",
        "request_id": "request-media-terminal",
        "detail": "media.apple_music_play",
        "tool_call_id": "call-media-terminal",
        "capability_id": "media.playback",
        "action_target": {
            "kind": "media",
            "action": "play",
            "query": "Moonlight",
        },
        "result": {
            "ok": True,
            "postcondition_verified": True,
            RUNTIME_EXECUTION_PROVENANCE_KEY: {
                "source": RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
                "version": RUNTIME_EXECUTION_PROVENANCE_VERSION,
            },
            "data": {
                "query": "Moonlight",
                "track": "Moonlight",
                "track_identity_verified": True,
                "player_state": "playing",
                "playback_started": True,
                "postcondition_verified": True,
            },
        },
    }


def _runtime_owned_terminal_event(contract: GoalContract, event: dict) -> dict:
    """Shape a test receipt exactly like the Runtime's internal executor event."""

    shaped = dict(event)
    tool_name = str(shaped.get("tool") or shaped.get("detail") or "").strip()
    call_id = str(shaped.get("tool_call_id") or "").strip()
    shaped.setdefault("run_id", contract.run_id)
    shaped.setdefault("actor", "native_runtime")
    shaped.setdefault("execution_authority", "runtime_tool_executor")
    shaped.setdefault("plan_id", f"plan-{contract.contract_id}")
    shaped.setdefault("step_id", f"step-{call_id}")
    shaped.setdefault("request_id", f"request-{call_id}")
    result = dict(shaped.get("result") or {})
    is_pause = bool(
        result.get("approval_required") is True
        and str(result.get("status") or "").strip() == "approval_required"
    )
    provider = (
        dict(result.get("desktop_execution_provider") or {})
        if isinstance(result.get("desktop_execution_provider"), dict)
        else {}
    )
    if not provider:
        provider_kind = str(
            result.get("verification_provider_kind")
            or result.get("provider_kind")
            or ""
        ).strip()
        provider_id = str(
            result.get("verification_provider_id")
            or result.get("provider_id")
            or ""
        ).strip()
        if provider_kind and provider_id:
            provider = {
                "provider_kind": provider_kind,
                "provider_id": provider_id,
            }
    if not is_pause and provider.get("provider_kind") and provider.get("provider_id"):
        provider["adapter_registered"] = True
        result["tool"] = tool_name
        result["desktop_execution_provider_routed"] = True
        result["desktop_execution_provider"] = provider
        result["desktop_execution_route"] = {
            "selected_provider_kind": provider["provider_kind"],
            "selected_provider_id": provider["provider_id"],
        }
        result.setdefault(
            "desktop_execution_provider_evidence",
            {"tool": tool_name, "executor_receipt": True},
        )
    elif not is_pause:
        result[RUNTIME_EXECUTION_PROVENANCE_KEY] = {
            "source": RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
            "version": RUNTIME_EXECUTION_PROVENANCE_VERSION,
        }
    shaped["result"] = result
    return shaped


def _runtime_owned_terminal_events(
    contract: GoalContract,
    events: list[dict],
) -> list[dict]:
    return [
        _runtime_owned_terminal_event(contract, event)
        if str(event.get("event") or event.get("event_type") or "").strip()
        in {"agent.tool.call", "agent.tool.failed", "agent.tool.skipped"}
        else dict(event)
        for event in events
    ]


def test_planner_contract_is_bound_to_runtime_run() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-1",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )

    assert contract is not None
    assert contract.run_id == "run-media-1"
    assert contract.original_goal == "Play Moonlight"
    assert contract.criteria[0].effectful is True


def test_semantic_verification_predicate_order_does_not_create_contract_conflict() -> None:
    contract, source, verifier = _exact_workspace_file_goal_lineage(
        require_semantic_adequacy=True,
    )
    template = contract.to_payload()
    template["run_id"] = ""
    equivalent = GoalContract.from_payload(template).to_payload()
    equivalent["criteria"][0]["required_verification_predicates"] = [
        "semantic_artifact_adequacy",
        "exact_file_content_present",
    ]

    restored = runtime_goal_contract(
        run_id=contract.run_id,
        original_goal=contract.original_goal,
        goal_contract_template=template,
        runtime_execution_envelope=None,
        runtime_execution_metadata={"goal_contract": equivalent},
        messages=(),
        timeline=(),
    )

    assert restored is not None
    assert set(restored.criteria[0].required_verification_predicates) == {
        "exact_file_content_present",
        "semantic_artifact_adequacy",
    }
    reordered_contract = GoalContract.from_payload(equivalent).bind_run(contract.run_id)
    original_digest = pending_semantic_artifact_assessment_candidates(
        contract,
        [source, verifier],
    )[0]["semantic_rubric_sha256"]
    reordered_digest = pending_semantic_artifact_assessment_candidates(
        reordered_contract,
        [source, verifier],
    )[0]["semantic_rubric_sha256"]
    assert reordered_digest == original_digest


def test_semantic_verification_predicates_reject_malformed_contract_payload() -> None:
    contract, _, _ = _exact_workspace_file_goal_lineage(
        require_semantic_adequacy=True,
    )
    malformed = contract.to_payload()
    malformed["run_id"] = ""
    malformed["criteria"][0]["required_verification_predicates"] = (
        "semantic_artifact_adequacy"
    )

    with pytest.raises(ValueError, match="goal_contract_invalid"):
        runtime_goal_contract(
            run_id=contract.run_id,
            original_goal=contract.original_goal,
            goal_contract_template=malformed,
            runtime_execution_envelope=None,
            runtime_execution_metadata=None,
            messages=(),
            timeline=(),
        )


def test_unverified_playback_receipt_cannot_complete_goal() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-2",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    timeline = [
        {
            "event": "agent.tool.call",
            "detail": "media.apple_music_play",
            "tool_call_id": "play-call-2",
            "step_id": "control-media-playback",
            "capability_id": "media.playback",
            "action_target": {
                "kind": "media",
                "action": "play",
                "query": "Moonlight",
            },
            "result": {
                "ok": True,
                "data": {
                    "query": "Moonlight",
                    "playback_started": True,
                },
            },
        }
    ]

    assessment = runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, timeline),
    )

    assert assessment.completed is False
    assert assessment.unsatisfied_criterion_ids == (
        contract.criteria[0].criterion_id,
    )


def test_verified_correlated_playback_receipt_completes_goal() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-3",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    timeline = [
        {
            "event": "agent.tool.call",
            "detail": "media.apple_music_play",
            "tool_call_id": "play-call-3",
            "step_id": "control-media-playback",
            "capability_id": "media.playback",
            "action_target": {
                "kind": "media",
                "action": "play",
                "query": "Moonlight",
            },
            "result": {
                "ok": True,
                "postcondition_verified": True,
                "data": {
                    "query": "Moonlight",
                    "playback_started": True,
                    "postcondition_verified": True,
                },
            },
        }
    ]

    assessment = runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, timeline),
    )

    assert assessment.completed is True


def test_effectful_terminal_without_exact_run_identity_cannot_complete_goal() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-missing-terminal-run",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    event = _trusted_media_terminal_event(contract)
    event.pop("run_id")

    assessment = runtime_goal_assessment(contract, [event])

    assert assessment.completed is False
    assert assessment.evidence == ()


def test_effectful_terminal_without_runtime_actor_cannot_complete_goal() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-missing-terminal-actor",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    event = _trusted_media_terminal_event(contract)
    event.pop("actor")

    assessment = runtime_goal_assessment(contract, [event])

    assert assessment.completed is False
    assert assessment.evidence == ()


def test_effectful_terminal_without_executor_authority_cannot_complete_goal() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-missing-terminal-authority",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    event = _trusted_media_terminal_event(contract)
    event.pop("execution_authority")

    assessment = runtime_goal_assessment(contract, [event])

    assert assessment.completed is False
    assert assessment.evidence == ()


def test_effectful_terminal_without_plan_identity_cannot_complete_goal() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-missing-terminal-plan",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    event = _trusted_media_terminal_event(contract)
    event.pop("plan_id")

    assessment = runtime_goal_assessment(contract, [event])

    assert assessment.completed is False
    assert assessment.evidence == ()


def test_effectful_terminal_without_step_identity_cannot_complete_goal() -> None:
    contract = GoalContract(
        contract_id="goal-media-missing-terminal-step",
        run_id="run-media-missing-terminal-step",
        original_goal="Play Moonlight",
        criteria=(
            GoalCriterion(
                criterion_id="criterion-media-missing-terminal-step",
                description="Moonlight is playing",
                effectful=True,
                required_capabilities=("media.playback",),
            ),
        ),
    )
    event = _trusted_media_terminal_event(contract)
    event.pop("step_id")

    assessment = runtime_goal_assessment(contract, [event])

    assert assessment.completed is False
    assert assessment.evidence == ()


def test_observation_without_completion_authority_cannot_complete_effectful_goal() -> None:
    contract = GoalContract(
        contract_id="goal-observation-no-effectful-authority",
        run_id="run-observation-no-effectful-authority",
        original_goal="Open the requested app",
        criteria=(
            GoalCriterion(
                criterion_id="criterion-effectful-app-state",
                description="The requested app state changed",
                effectful=True,
                required_capabilities=("desktop.visual_verification",),
            ),
        ),
    )
    event = _runtime_owned_terminal_event(
        contract,
        {
            "event": "agent.tool.call",
            "detail": "desktop.verify",
            "tool_call_id": "call-read-only-observation",
            "step_id": "runtime-replan-observation-step",
            "request_id": "runtime-replan-observation-request",
            "capability_id": "desktop.visual_verification",
            "source": "runtime_model_replan_observation",
            "observation_only": True,
            "goal_completion_authority": False,
            "result": {
                "ok": True,
                "postcondition_verified": True,
                "data": {
                    "verified": True,
                    "postcondition_verified": True,
                },
            },
        },
    )

    assessment = runtime_goal_assessment(contract, [event])

    assert assessment.completed is False
    assert assessment.satisfied_criterion_ids == ()
    assert assessment.unsatisfied_criterion_ids == (
        "criterion-effectful-app-state",
    )


def test_effectful_terminal_without_request_identity_cannot_complete_goal() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-missing-terminal-request",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    event = _trusted_media_terminal_event(contract)
    event.pop("request_id")

    assessment = runtime_goal_assessment(contract, [event])

    assert assessment.completed is False
    assert assessment.evidence == ()


def test_effectful_terminal_without_provider_attestation_cannot_complete_goal() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-missing-terminal-provider",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    event = _trusted_media_terminal_event(contract)
    result = dict(event["result"])
    result.pop(RUNTIME_EXECUTION_PROVENANCE_KEY)
    event["result"] = result

    assessment = runtime_goal_assessment(contract, [event])

    assert assessment.completed is False
    assert assessment.evidence == ()


def test_provider_self_report_cannot_attest_effectful_terminal() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-self-reported-provider",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    event = _trusted_media_terminal_event(contract)
    result = dict(event["result"])
    result.pop(RUNTIME_EXECUTION_PROVENANCE_KEY)
    result["desktop_execution_provider_routed"] = True
    result["desktop_execution_provider"] = {
        "provider_kind": "background_desktop",
        "provider_id": "provider-self-report",
        "adapter_registered": True,
    }
    event["result"] = result

    assessment = runtime_goal_assessment(contract, [event])

    assert assessment.completed is False
    assert assessment.evidence == ()


def test_canonical_runtime_owned_effectful_terminal_completes_goal() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-canonical-terminal",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None

    assessment = runtime_goal_assessment(
        contract,
        [_trusted_media_terminal_event(contract)],
    )

    assert assessment.completed is True
    assert len(assessment.evidence) == 1
    assert assessment.evidence[0].status == "success"


@pytest.mark.parametrize(
    ("mutation", "replacement"),
    (
        ("missing_run", None),
        ("run_id", "foreign-run"),
        ("actor", "public_client"),
        ("actor", "model"),
        ("execution_authority", "model_tool_call"),
    ),
)
def test_untrusted_failed_terminal_cannot_take_first_winner_from_canonical_success(
    mutation: str,
    replacement: object,
) -> None:
    contract = runtime_goal_contract(
        run_id=f"run-media-untrusted-first-{mutation}-{replacement}",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    success = _trusted_media_terminal_event(contract)
    failed = {
        **success,
        "result": {
            "ok": False,
            "error": "untrusted serialized failure",
            RUNTIME_EXECUTION_PROVENANCE_KEY: {
                "source": RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
                "version": RUNTIME_EXECUTION_PROVENANCE_VERSION,
            },
        },
    }
    if mutation == "missing_run":
        failed.pop("run_id")
    else:
        failed[mutation] = replacement

    assessment = runtime_goal_assessment(contract, [failed, success])

    assert assessment.completed is True
    assert len(assessment.evidence) == 1
    assert assessment.evidence[0].status == "success"


def test_terminal_tool_call_id_first_winner_blocks_partial_to_success_replay() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-terminal-winner",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    base = {
        "event": "agent.tool.call",
        "run_id": contract.run_id,
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
        "plan_id": "plan-media-terminal-winner",
        "step_id": "control-media-playback",
        "detail": "media.apple_music_play",
        "tool_call_id": "play-terminal-winner",
        "capability_id": "media.playback",
        "action_target": {
            "kind": "media",
            "action": "play",
            "query": "Moonlight",
        },
    }
    partial = {
        **base,
        "result": {
            "ok": True,
            "data": {
                "query": "Moonlight",
                "status": "not_found",
                "outcome": "partial",
                "playback_started": False,
            },
        },
    }
    replayed_success = {
        **base,
        "result": {
            "ok": True,
            "postcondition_verified": True,
            "data": {
                "query": "Moonlight",
                "status": "played",
                "track": "Moonlight",
                "track_identity_verified": True,
                "player_state": "playing",
                "playback_started": True,
                "postcondition_verified": True,
            },
        },
    }

    assessment = runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, [partial, replayed_success]),
    )

    assert assessment.completed is False
    assert len(assessment.evidence) == 1
    assert assessment.evidence[0].status == "partial"


@pytest.mark.parametrize("action", ("next", "previous"))
def test_media_navigation_requires_explicit_track_change_or_identity_evidence(
    action: str,
) -> None:
    prompt = "下一首" if action == "next" else "上一首"
    decision = RuntimePlanner().decision(
        prompt,
        allowed_tools=["media.system_control"],
    )
    contract = runtime_goal_contract(
        run_id=f"run-media-{action}",
        runtime_execution_envelope={
            "task_core": decision.plan.task_core.model_dump(),  # type: ignore[union-attr]
        },
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": prompt}],
        timeline=[],
    )
    assert contract is not None
    base_event = {
        "event": "agent.tool.call",
        "run_id": contract.run_id,
        "detail": "media.system_control",
        "tool_call_id": f"media-{action}",
        "step_id": "control-media-playback",
        "capability_id": "media.playback",
        "action_target": {"kind": "media", "action": action},
        "result": {
            "ok": True,
            "postcondition_verified": True,
            "data": {
                "requested_control": action,
                "player_state": "playing",
                "track": "Some Track",
                "postcondition_verified": True,
            },
        },
    }

    uncorrelated = runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, [base_event]),
    )
    changed = {
        **base_event,
        "result": {
            **base_event["result"],
            "data": {
                **base_event["result"]["data"],
                "track_changed": True,
            },
        },
    }
    correlated = runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, [changed]),
    )

    assert uncorrelated.completed is False
    assert correlated.completed is True


def test_media_pause_requires_the_declared_paused_state() -> None:
    prompt = "暂停当前音乐"
    decision = RuntimePlanner().decision(
        prompt,
        allowed_tools=["media.system_control"],
    )
    contract = runtime_goal_contract(
        run_id="run-media-pause",
        runtime_execution_envelope={
            "task_core": decision.plan.task_core.model_dump(),  # type: ignore[union-attr]
        },
        runtime_execution_metadata=None,
        messages=(),
        timeline=[],
    )
    assert contract is not None

    def event(state: str, call_id: str) -> dict:
        return {
            "event": "agent.tool.call",
            "run_id": contract.run_id,
            "detail": "media.system_control",
            "tool_call_id": call_id,
            "step_id": "control-media-playback",
            "capability_id": "media.playback",
            "action_target": {"kind": "media", "action": "pause"},
            "result": {
                "ok": True,
                "data": {
                    "requested_control": "pause",
                    "player_state": state,
                },
            },
        }

    assert runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, [event("paused", "pause-ok")]),
    ).completed is True
    assert runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(
            contract,
            [event("stopped", "pause-wrong-state")],
        ),
    ).completed is False


def test_media_status_is_read_only_and_accepts_a_structured_state_observation() -> None:
    prompt = "查看当前播放状态"
    decision = RuntimePlanner().decision(
        prompt,
        allowed_tools=["media.apple_music_status"],
    )
    contract = runtime_goal_contract(
        run_id="run-media-status",
        runtime_execution_envelope={
            "task_core": decision.plan.task_core.model_dump(),  # type: ignore[union-attr]
        },
        runtime_execution_metadata=None,
        messages=(),
        timeline=[],
    )
    assert contract is not None
    criterion = contract.criteria[0]

    assessment = runtime_goal_assessment(
        contract,
        [
            {
                "event": "agent.tool.call",
                "run_id": contract.run_id,
                "detail": "media.apple_music_status",
                "tool_call_id": "status-read",
                "step_id": "control-media-playback",
                "capability_id": "media.playback",
                "action_target": {"kind": "media", "action": "status"},
                "result": {
                    "ok": True,
                    "data": {"player_state": "paused", "track": "Some Track"},
                },
            }
        ],
    )

    assert criterion.effectful is False
    assert criterion.expected == {
        "target": {"kind": "media", "action": "status"}
    }
    assert assessment.completed is True


def _generic_music_playback_contract(*, run_id: str) -> GoalContract:
    decision = RuntimePlanner().decision(
        "Can you play some music?",
        allowed_tools=["media.music_app_open_and_play"],
    )
    contract = runtime_goal_contract(
        run_id=run_id,
        runtime_execution_envelope={
            "task_core": decision.plan.task_core.model_dump(),  # type: ignore[union-attr]
        },
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Can you play some music?"}],
        timeline=[],
    )
    assert contract is not None
    return contract


def _generic_music_playback_assessment(
    data: dict,
    *,
    run_id: str,
) -> object:
    contract = _generic_music_playback_contract(run_id=run_id)
    assessment = runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, [
            {
                "event": "agent.tool.call",
                "run_id": run_id,
                "detail": "media.music_app_open_and_play",
                "tool_call_id": f"{run_id}:play",
                "step_id": "control-media-playback",
                "capability_id": "media.playback",
                "action_target": {
                    "kind": "media",
                    "action": "play",
                    "app_name": "Music",
                },
                "result": {"ok": True, "data": data},
            }
        ]),
    )
    return assessment


def test_media_goal_canonicalizes_structured_player_state() -> None:
    assessment = _generic_music_playback_assessment(
        {
            "app_name": "Music",
            "control": "play",
            "playback_ok": True,
            "player_state": "playing",
        },
        run_id="run-generic-media-playing",
    )

    assert assessment.completed is True


def test_media_goal_canonicalizes_structured_playback_state() -> None:
    assessment = _generic_music_playback_assessment(
        {
            "app_name": "Music",
            "control": "play",
            "playback_ok": True,
            "playback_state": "playing",
        },
        run_id="run-generic-media-playback-state",
    )

    assert assessment.completed is True


def test_media_goal_canonicalizes_structured_status_state() -> None:
    assessment = _generic_music_playback_assessment(
        {
            "app_name": "Music",
            "control": "play",
            "playback_ok": True,
            "status": "playing",
        },
        run_id="run-generic-media-status",
    )

    assert assessment.completed is True


def test_media_goal_does_not_complete_from_success_boole_without_state() -> None:
    assessment = _generic_music_playback_assessment(
        {
            "app_name": "Music",
            "control": "play",
            "playback_ok": True,
        },
        run_id="run-generic-media-bool-only",
    )

    assert assessment.completed is False


def test_media_goal_rejects_nonplaying_and_partial_states() -> None:
    for index, data in enumerate(
        (
            {
                "app_name": "Music",
                "control": "play",
                "playback_ok": True,
                "player_state": "paused",
            },
            {
                "app_name": "Music",
                "control": "play",
                "playback_ok": True,
                "playback_state": "stopped",
            },
            {
                "app_name": "Music",
                "control": "play",
                "playback_ok": True,
                "status": "not_found",
            },
        )
    ):
        assessment = _generic_music_playback_assessment(
            data,
            run_id=f"run-generic-media-negative-{index}",
        )

        assert assessment.completed is False


def test_media_goal_rejects_wrong_track_identity_even_when_playing() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-wrong-track",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    assessment = runtime_goal_assessment(
        contract,
        [
            {
                "event": "agent.tool.call",
                "run_id": contract.run_id,
                "detail": "media.apple_music_play",
                "tool_call_id": "play-wrong-track",
                "step_id": "control-media-playback",
                "capability_id": "media.playback",
                "action_target": {
                    "kind": "media",
                    "action": "play",
                    "query": "Moonlight",
                },
                "result": {
                    "ok": True,
                    "data": {
                        "query": "Moonlight",
                        "track": "A Different Song",
                        "control": "play",
                        "playback_ok": True,
                        "player_state": "playing",
                        "catalog_match_verified": True,
                        "track_identity_verified": False,
                    },
                },
            }
        ],
    )

    assert assessment.completed is False


def test_media_goal_rejects_explicit_catalog_identity_conflict() -> None:
    assessment = _generic_music_playback_assessment(
        {
            "app_name": "Music",
            "control": "play",
            "playback_ok": True,
            "player_state": "playing",
            "catalog_match_verified": True,
            "track_identity_verified": True,
            "catalog_identity_conflict": True,
        },
        run_id="run-generic-media-catalog-conflict",
    )

    assert assessment.completed is False


def test_data_analysis_goal_requires_verified_exact_artifact_target() -> None:
    decision = RuntimePlanner().decision(
        "请分析 inputs/sales.csv 并输出报告",
        allowed_tools=["workspace.read", "data.analyze", "artifact.write"],
    )
    contract = runtime_goal_contract(
        run_id="run-data-analysis",
        runtime_execution_envelope={
            "task_core": decision.plan.task_core.model_dump(),  # type: ignore[union-attr]
        },
        runtime_execution_metadata=None,
        messages=(),
        timeline=[],
    )
    assert contract is not None
    base_event = {
        "event": "agent.tool.call",
        "run_id": contract.run_id,
        "detail": "data.analyze",
        "tool_call_id": "analyze-call",
        "step_id": "analyze-data-file",
        "plan_id": decision.plan.plan_id,
        "capability_id": "data.analysis",
    }
    verified = {
        **base_event,
        "action_target": {
            "kind": "data_analysis",
            "action": "analyze",
            "path": "inputs/sales.csv",
            "source_kind": "csv",
            "artifact_path": "analysis-report.md",
            "step_id": "analyze-data-file",
        },
        "result": {"ok": True, "postcondition_verified": True},
    }
    wrong_target = {
        **verified,
        "tool_call_id": "analyze-wrong-target",
        "action_target": {
            **verified["action_target"],
            "artifact_path": "other-report.md",
        },
    }
    unverified = {
        **verified,
        "tool_call_id": "analyze-unverified",
        "result": {"ok": True, "postcondition_verified": False},
    }

    assert runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, [verified]),
    ).completed is True
    assert runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, [wrong_target]),
    ).completed is False
    assert runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, [unverified]),
    ).completed is False


def test_dynamic_data_goal_requires_replayable_workspace_resolution() -> None:
    allowed_tools = ["workspace.list", "data.analyze", "artifact.write"]
    decision = RuntimePlanner().decision(
        "把 Downloads 里的 CSV 做成图表报告",
        allowed_tools=allowed_tools,
    )
    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=allowed_tools,
        full_plan=True,
    )
    assert envelope is not None
    planned = next(
        request.model_dump(mode="json")
        for request in envelope.requests
        if request.tool_name == "data.analyze"
    )
    contract = runtime_goal_contract(
        run_id="run-dynamic-data",
        runtime_execution_envelope={"task_core": decision.plan.task_core.model_dump()},
        runtime_execution_metadata=None,
        messages=(),
        timeline=[],
    )
    assert contract is not None
    source_event = {
        "event": "agent.tool.call",
        "detail": "workspace.list",
        "run_id": contract.run_id,
        "plan_id": decision.plan.plan_id,
        "step_id": "inspect-data-source",
        "tool_call_id": "call-inspect-data",
        "input_preview": {
            "path": "Downloads",
            "pattern": "*.csv",
            "file_type": "csv",
        },
        "result": {
            "ok": True,
            "path": "Downloads",
            "entries": [{"name": "sales.csv", "type": "file"}],
        },
    }
    execution_request = {
        **planned,
        "tool": "data.analyze",
        "run_id": contract.run_id,
        "tool_call_id": "call-analyze-data",
    }
    resolution = resolve_workspace_file_selection(
        execution_request,
        [source_event],
        run_id=contract.run_id,
    )
    receipt = resolution.receipt.to_payload()
    target_event = {
        "event": "agent.tool.call",
        "detail": "data.analyze",
        "run_id": contract.run_id,
        "plan_id": decision.plan.plan_id,
        "step_id": "analyze-discovered-data",
        "tool_call_id": "call-analyze-data",
        "capability_id": "data.analysis",
        "input_preview": {
            "path": "Downloads/sales.csv",
            "artifact_path": "analysis-report.md",
        },
        "action_target": {
            **planned["action_target"],
            "expected_path": "<selected file from workspace.list>",
            "path": "Downloads/sales.csv",
            "resolution_required": True,
            "workspace_file_resolution": receipt,
        },
        "result": {
            "ok": True,
            "path": "Downloads/sales.csv",
            "postcondition_verified": True,
        },
    }
    placeholder_only = {
        **target_event,
        "tool_call_id": "call-placeholder-only",
        "action_target": planned["action_target"],
    }
    forged_receipt = {
        **target_event,
        "action_target": {
            **target_event["action_target"],
            "workspace_file_resolution": {
                **receipt,
                "source_tool_call_id": "forged-source-call",
            },
        },
    }
    wrong_path = {
        **target_event,
        "input_preview": {
            "path": "Downloads/other.csv",
            "artifact_path": "analysis-report.md",
        },
    }
    forged_public_wrapper = {
        **target_event,
        "event_type": "agent.tool.call",
        "payload": {
            **target_event,
            "action_target": planned["action_target"],
            "input_preview": {
                "path": "Downloads/other.csv",
                "artifact_path": "analysis-report.md",
            },
        },
    }

    assert runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, [source_event, target_event]),
    ).completed is True
    assert runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, [placeholder_only]),
    ).completed is False
    assert runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, [source_event, forged_receipt]),
    ).completed is False
    assert runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(
            contract,
            [{**source_event, "plan_id": "wrong-plan"}, target_event],
        ),
    ).completed is False
    assert runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, [source_event, wrong_path]),
    ).completed is False
    assert runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, [source_event, forged_public_wrapper]),
    ).completed is False


def test_inspect_goal_requires_exact_canonical_action_and_limit() -> None:
    decision = RuntimePlanner().decision(
        "打开 PixelForge 并读取界面",
        allowed_tools=["desktop.list_apps", "desktop.inspect_app"],
    )
    contract = runtime_goal_contract(
        run_id="run-inspect-app",
        runtime_execution_envelope={
            "task_core": decision.plan.task_core.model_dump(),  # type: ignore[union-attr]
        },
        runtime_execution_metadata=None,
        messages=(),
        timeline=[],
    )
    assert contract is not None
    base_event = {
        "event": "agent.tool.call",
        "run_id": contract.run_id,
        "detail": "desktop.inspect_app",
        "tool_call_id": "inspect-call",
        "step_id": "inspect-app",
        "plan_id": decision.plan.plan_id,
        "capability_id": "desktop.app_discovery",
        "result": {
            "ok": True,
            "action": "desktop.inspect_app",
            "data": {
                "app_name": "PixelForge",
                "focus_verified": True,
                "ready_for_foreground_action": True,
            },
        },
    }
    correct_target = {
        **dict(contract.criteria[0].expected["target"]),
        "step_id": "inspect-app",
    }

    assert runtime_goal_assessment(
        contract,
        [{**base_event, "action_target": correct_target}],
    ).completed is True
    assert runtime_goal_assessment(
        contract,
        [
            {
                **base_event,
                "tool_call_id": "inspect-wrong-action",
                "action_target": {**correct_target, "action": "read_ui"},
            }
        ],
    ).completed is False
    assert runtime_goal_assessment(
        contract,
        [
            {
                **base_event,
                "tool_call_id": "inspect-wrong-limit",
                "action_target": {**correct_target, "limit": 20},
            }
        ],
    ).completed is False


def test_open_path_goal_requires_exact_canonical_action_and_path() -> None:
    decision = RuntimePlanner().decision(
        "打开一个能编辑 PDF 的应用并打开 Downloads/report.pdf",
        allowed_tools=[
            "desktop.list_apps",
            "desktop.open_path_with_app",
            "desktop.ui_elements",
        ],
    )
    contract = runtime_goal_contract(
        run_id="run-open-path-with-app",
        runtime_execution_envelope={
            "task_core": decision.plan.task_core.model_dump(),  # type: ignore[union-attr]
        },
        runtime_execution_metadata=None,
        messages=(),
        timeline=[],
    )
    assert contract is not None
    correct_target = {
        **dict(contract.criteria[0].expected["target"]),
        "step_id": "open-selected-discovered-app",
    }

    def events(action_target: dict) -> list[dict]:
        return _runtime_owned_terminal_events(contract, [
            {
                "event": "agent.tool.call",
                "run_id": contract.run_id,
                "plan_id": decision.plan.plan_id,
                "step_id": "open-selected-discovered-app",
                "detail": "desktop.open_path_with_app",
                "tool_call_id": "call-open-path",
                "capability_id": "file.desktop_access",
                "action_target": action_target,
                "result": {"ok": True},
            },
            {
                "event": "agent.tool.call",
                "run_id": contract.run_id,
                "plan_id": decision.plan.plan_id,
                "step_id": "verify-desktop-result",
                "detail": "desktop.ui_elements",
                "tool_call_id": "call-verify-open-path",
                "source": "runtime_native_postcondition_receipt",
                "result": {
                    "ok": True,
                    "postcondition_verified": True,
                    "verification_satisfied_by_native_receipt": True,
                    "source_tool_call_id": "call-open-path",
                    "source_tool": "desktop.open_path_with_app",
                    "source_step_id": "open-selected-discovered-app",
                    "verified_observed_state": "fulfilled",
                },
            },
        ])

    assert runtime_goal_assessment(contract, events(correct_target)).completed is True
    assert runtime_goal_assessment(
        contract,
        events({**correct_target, "action": "desktop.open_path_with_app"}),
    ).completed is False
    assert runtime_goal_assessment(
        contract,
        events({**correct_target, "target_path": "Downloads/other.pdf"}),
    ).completed is False


def test_approval_canonical_duplicate_cannot_weaken_enriched_goal_evidence() -> None:
    decision = RuntimePlanner().decision(
        "退出当前应用",
        allowed_tools=["desktop.quit_app"],
    )
    contract = runtime_goal_contract(
        run_id="run-dispatch-duplicate",
        runtime_execution_envelope={
            "task_core": decision.plan.task_core.model_dump(),  # type: ignore[union-attr]
        },
        runtime_execution_metadata=None,
        messages=(),
        timeline=[],
    )
    assert contract is not None
    result = {
        "ok": True,
        "action": "desktop.quit_app",
        "summary": "Sent quit request",
        "data": {},
    }
    timeline = [
        {
            "event": "agent.tool.call",
            "run_id": contract.run_id,
            "detail": "desktop.quit_app",
            "tool_call_id": "call-quit",
            "step_id": "manage-foreground",
            "capability_id": "desktop.app_control",
            "action_target": {"action": "dispatch_management"},
            "result": result,
        },
        {
            "event": "agent.tool.call",
            "run_id": contract.run_id,
            "detail": "desktop.quit_app",
            "tool_call_id": "call-quit",
            "step_id": "manage-foreground",
            "approval_resume_result_canonical": True,
            "result": result,
        },
    ]

    assessment = runtime_goal_assessment(contract, timeline)

    assert assessment.completed is True
    assert len(assessment.evidence) == 1
    assert assessment.evidence[0].verified is True


def test_unplanned_native_chat_falls_back_to_response_only_contract() -> None:
    contract = runtime_goal_contract(
        run_id="run-answer-2",
        original_goal="Explain why the sky looks blue",
        runtime_execution_envelope=None,
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Explain why the sky looks blue"}],
        timeline=[],
    )
    assert contract is not None

    assessment = complete_response_only_goal(
        contract,
        runtime_goal_assessment(contract, []),
        "Rayleigh scattering makes shorter wavelengths scatter more strongly.",
    )

    assert assessment.completed is True


def test_effectful_general_intent_cannot_compile_response_only_contract() -> None:
    goal = "帮我在那个软件里弄一下"
    allowed_tools = ["app.open", "desktop.active_window", "desktop.safe_click"]
    decision = RuntimePlanner().decision(goal, allowed_tools=allowed_tools)

    assert decision.selected_intent.kind == "general"
    with pytest.raises(ValueError, match="goal_contract_compile_failed"):
        planned_goal_contract_payload(goal, allowed_tools=allowed_tools)


def test_effectful_general_intent_response_only_template_is_rejected() -> None:
    goal = "帮我在那个软件里弄一下"
    decision = RuntimePlanner().decision(
        goal,
        allowed_tools=["app.open", "desktop.active_window", "desktop.safe_click"],
    )
    assert decision.plan.task_core is not None

    with pytest.raises(
        ValueError,
        match="goal_contract_invalid: response_only_nonconversation",
    ):
        runtime_goal_contract(
            run_id="run-general-effectful",
            original_goal=goal,
            goal_contract_template=decision.plan.task_core.goal_contract.model_dump(),
            runtime_execution_envelope=None,
            runtime_execution_metadata=None,
            messages=(),
            timeline=[],
        )


def test_final_evaluator_rejects_persisted_incomplete_goal_contract_even_when_legacy_projection_claims_completion(
) -> None:
    contract = runtime_goal_contract(
        run_id="run-media-4",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    assessment = runtime_goal_assessment(contract, [])
    events = [
        {
            "event_type": "agent.goal.contract",
            "run_id": contract.run_id,
            "payload": goal_contract_event_payload(contract),
        },
        {
            "event_type": "agent.goal.assessed",
            "run_id": contract.run_id,
            "payload": goal_assessment_event_payload(assessment),
        },
        {
            "event_type": "agent.desktop.intent_completed",
            "run_id": contract.run_id,
            "payload": {
                "status": "completed",
                "summary": "Legacy projection claimed completion",
                "result": {
                    "ok": True,
                    "postcondition_verified": True,
                },
            },
        },
    ]

    outcome = evaluate_main_chat_outcome({"run_id": contract.run_id}, events)

    assert outcome.allows_completion is False
    assert outcome.reason == "goal_contract_incomplete"


def test_final_evaluator_preserves_authoritative_provider_blocker_message() -> None:
    contract = runtime_goal_contract(
        run_id="run-provider-blocked",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    events = [
        {
            "event_type": "agent.goal.contract",
            "run_id": contract.run_id,
            "payload": goal_contract_event_payload(contract),
        },
        {
            "event_type": "agent.desktop.intent_unavailable",
            "run_id": contract.run_id,
            "payload": {
                "tool": "desktop.safe_type_text",
                "status": "unavailable",
                "reason": "real_isolated_provider_required",
                "summary": "需要可用的真实隔离桌面 Provider 才能继续。",
                "result": {
                    "ok": False,
                    "status": "unavailable",
                    "reason": "real_isolated_provider_required",
                    "summary": "需要可用的真实隔离桌面 Provider 才能继续。",
                },
            },
        },
    ]

    outcome = evaluate_main_chat_outcome({"run_id": contract.run_id}, events)

    assert outcome.allows_completion is False
    assert outcome.reason == "real_isolated_provider_required"
    assert "真实隔离桌面 Provider" in outcome.message


@pytest.mark.parametrize(
    ("observed_action", "expected_completed"),
    [
        pytest.param("arrow_down", False, id="exact-key-without-observation"),
        pytest.param("escape", False, id="wrong-key"),
    ],
)
def test_safe_key_receipt_cannot_complete_without_independent_observation(
    observed_action: str,
    expected_completed: bool,
) -> None:
    contract = GoalContract(
        contract_id="goal-safe-key",
        run_id="run-safe-key",
        original_goal="Press Arrow Down",
        criteria=(
            GoalCriterion(
                criterion_id="dispatch-arrow-down",
                description="Dispatch the exact requested foreground key",
                effectful=False,
                required_capabilities=("desktop.ui_operation",),
                expected={
                    "state": "fulfilled",
                    "target": {
                        "action": "dispatch_shortcut",
                        "shortcut_action": "arrow_down",
                    },
                },
                source_step_ids=("operate-foreground-ui",),
            ),
        ),
    )
    events = [
        {
            "event": "agent.tool.call",
            "run_id": contract.run_id,
            "plan_id": "plan-safe-key",
            "step_id": "operate-foreground-ui",
            "detail": "desktop.safe_key",
            "tool_call_id": "call-safe-key",
            "capability_id": "desktop.ui_operation",
            "input_preview": {"action": "arrow_down", "repeat_count": 3},
            "action_target": {
                "kind": "desktop_foreground",
                "action": "dispatch_shortcut",
            },
            "result": {
                "ok": True,
                "action": "desktop.safe_key",
                "postcondition_verified": True,
                "data": {
                    "key_action": observed_action,
                    "repeat_count": 3,
                    "postcondition_verified": True,
                },
            },
        }
    ]

    assessment = runtime_goal_assessment(contract, events)

    assert assessment.completed is expected_completed


def test_final_evaluator_accepts_recomputed_completed_response_goal() -> None:
    contract = runtime_goal_contract(
        run_id="run-answer-3",
        original_goal="Explain why the sky looks blue",
        runtime_execution_envelope=None,
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Explain why the sky looks blue"}],
        timeline=[],
    )
    assert contract is not None
    assessment = complete_response_only_goal(
        contract,
        runtime_goal_assessment(contract, []),
        "Rayleigh scattering.",
    )
    events = [
        {
            "event_type": "agent.goal.contract",
            "run_id": contract.run_id,
            "payload": goal_contract_event_payload(contract),
        },
        {
            "event_type": "agent.goal.assessed",
            "run_id": contract.run_id,
            "payload": goal_assessment_event_payload(assessment),
        },
    ]

    outcome = evaluate_main_chat_outcome({"run_id": contract.run_id}, events)

    assert outcome.allows_completion is True


def test_persisted_goal_events_preserve_typed_completion_evidence() -> None:
    contract = runtime_goal_contract(
        run_id="run-answer-persisted",
        original_goal="Explain the result",
        runtime_execution_envelope=None,
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Explain the result"}],
        timeline=[],
    )
    assert contract is not None
    assessment = complete_response_only_goal(
        contract,
        runtime_goal_assessment(contract, []),
        "The result is explained.",
    )
    contract_payload = redact_run_event_payload(goal_contract_event_payload(contract))
    assessment_payload = redact_run_event_payload(
        goal_assessment_event_payload(assessment)
    )

    assert contract_payload["goal_contract"]["criteria"][0]["effectful"] is False
    assert assessment_payload["goal_assessment"]["evidence"][0]["verified"] is True
    outcome = evaluate_main_chat_outcome(
        {"run_id": contract.run_id},
        [
            {
                "event_type": "agent.goal.contract",
                "run_id": contract.run_id,
                "payload": contract_payload,
            },
            {
                "event_type": "agent.goal.assessed",
                "run_id": contract.run_id,
                "payload": assessment_payload,
            },
        ],
    )

    assert outcome.allows_completion is True


def _background_app_open_contract() -> GoalContract:
    return GoalContract(
        contract_id="goal-background-open",
        run_id="run-background-open",
        original_goal="Open Notes in the background",
        criteria=(
            GoalCriterion(
                criterion_id="open-notes",
                description="Notes has an agent-owned open window",
                effectful=True,
                required_capabilities=("demo.app.open",),
                expected={
                    "state": "open",
                    "target": {
                        "kind": "app",
                        "action": "open",
                        "app_name": "Notes",
                    },
                },
                source_step_ids=("open-notes",),
            ),
        ),
    )


def _background_app_open_events(*, trusted: bool, provider_id: str = "cua-1") -> list[dict]:
    contract = _background_app_open_contract()
    return _runtime_owned_terminal_events(contract, [
        {
            "event": "agent.tool.call",
            "run_id": "run-background-open",
            "plan_id": "plan-background-open",
            "step_id": "open-notes",
            "detail": "app.open",
            "tool_call_id": "call-open-notes",
            "capability_id": "demo.app.open",
            "action_target": {
                "kind": "app",
                "action": "open",
                "app_name": "Notes",
            },
            "result": {
                "ok": True,
                "desktop_execution_provider": {
                    "provider_kind": "background_desktop",
                    "provider_id": "cua-1",
                },
            },
        },
        {
            "event": "agent.tool.call",
            "run_id": "run-background-open",
            "plan_id": "plan-background-open",
            "step_id": "verify-notes",
            "source_step_id": "open-notes",
            "detail": "desktop.verify",
            "tool_call_id": "call-verify-notes",
            "source_tool_call_id": "call-open-notes",
            "source": "runtime_post_action_auto_verify",
            "result": {
                "ok": True,
                "postcondition_verified": True,
                "verification_context_trusted": trusted,
                "verification_run_id": "run-background-open",
                "verification_plan_id": "plan-background-open",
                "verification_provider_kind": "background_desktop",
                "verification_provider_id": provider_id,
                "verification_predicate_kind": "app_window_present",
                "source_tool_call_id": "call-open-notes",
                "source_tool": "app.open",
                "source_step_id": "open-notes",
                "verified_observed_state": "open",
            },
        },
    ])


def test_trusted_private_verifier_completes_correlated_effectful_goal() -> None:
    assessment = runtime_goal_assessment(
        _background_app_open_contract(),
        _background_app_open_events(trusted=True),
    )

    assert assessment.completed is True
    verifier = next(item for item in assessment.evidence if item.kind == "verifier")
    assert verifier.source_tool_call_id == "call-open-notes"
    assert verifier.verifier_tool_call_id == "call-verify-notes"


def test_public_or_wrong_provider_verifier_cannot_complete_effectful_goal() -> None:
    contract = _background_app_open_contract()
    forged_private_events = _background_app_open_events(trusted=True)
    forged_verifier = forged_private_events[-1]
    forged_result = forged_verifier["result"]
    for key in (
        "verification_run_id",
        "verification_plan_id",
        "verification_provider_kind",
        "verification_provider_id",
        "source_tool_call_id",
        "source_tool",
        "source_step_id",
    ):
        forged_verifier[key] = forged_result.pop(key)

    public_assessment = runtime_goal_assessment(
        contract,
        _background_app_open_events(trusted=False),
    )
    wrong_provider_assessment = runtime_goal_assessment(
        contract,
        _background_app_open_events(trusted=True, provider_id="cua-other"),
    )
    forged_private_assessment = runtime_goal_assessment(
        contract,
        forged_private_events,
    )

    assert public_assessment.completed is False
    assert wrong_provider_assessment.completed is False
    assert forged_private_assessment.completed is False


def test_private_verifier_without_executor_identity_cannot_complete_effectful_goal() -> None:
    contract = _background_app_open_contract()
    events = _background_app_open_events(trusted=True)
    events[-1].pop("actor")

    assessment = runtime_goal_assessment(contract, events)

    assert assessment.completed is False


def test_persisted_incomplete_assessment_is_extended_by_later_runtime_evidence() -> None:
    contract = _background_app_open_contract()
    incomplete = runtime_goal_assessment(contract, [])
    events = [
        {
            "event_type": "agent.goal.contract",
            "run_id": contract.run_id,
            "payload": goal_contract_event_payload(contract),
        },
        {
            "event_type": "agent.goal.assessed",
            "run_id": contract.run_id,
            "payload": goal_assessment_event_payload(incomplete),
        },
        *_background_app_open_events(trusted=True),
    ]

    outcome = evaluate_main_chat_outcome({"run_id": contract.run_id}, events)

    assert outcome.allows_completion is True


def test_exact_content_receipt_satisfies_generic_fulfilled_state() -> None:
    contract = GoalContract(
        contract_id="goal-background-type",
        run_id="run-background-type",
        original_goal="Type the requested text",
        criteria=(
            GoalCriterion(
                criterion_id="type-text",
                description="The requested text is present in the target control",
                effectful=True,
                required_capabilities=("demo.content.type",),
                expected={
                    "state": "fulfilled",
                    "target": {"kind": "ui", "action": "type", "target": "Body"},
                },
                source_step_ids=("type-body",),
            ),
        ),
    )
    provider = {
        "provider_kind": "background_desktop",
        "provider_id": "cua-1",
    }
    assessment = runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, [
            {
                "event": "agent.tool.call",
                "run_id": contract.run_id,
                "plan_id": "plan-type",
                "step_id": "type-body",
                "detail": "app.open_and_type_into_ui_element",
                "tool_call_id": "call-type",
                "capability_id": "demo.content.type",
                "action_target": {
                    "kind": "ui",
                    "action": "type",
                    "target": "Body",
                },
                "result": {"ok": True, "desktop_execution_provider": provider},
            },
            {
                "event": "agent.tool.call",
                "run_id": contract.run_id,
                "detail": "desktop.verify",
                "tool_call_id": "call-verify-type",
                "result": {
                    "ok": True,
                    "postcondition_verified": True,
                    "verification_context_trusted": True,
                    "verification_run_id": contract.run_id,
                    "verification_plan_id": "plan-type",
                    "provider_kind": provider["provider_kind"],
                    "provider_id": provider["provider_id"],
                    "verification_predicate_kind": "exact_typed_content_present",
                    "source_tool_call_id": "call-type",
                    "source_tool": "app.open_and_type_into_ui_element",
                    "source_step_id": "type-body",
                    "verified_observed_state": "typed",
                },
            },
        ]),
    )

    assert assessment.completed is True


@pytest.mark.parametrize(
    ("expected_state", "completed"),
    (("sent", True), ("submitted", True), ("delivered", False)),
)
def test_exact_submit_dispatch_receipt_never_proves_delivery(
    expected_state: str,
    completed: bool,
) -> None:
    contract = GoalContract(
        contract_id=f"goal-submit-{expected_state}",
        run_id="run-exact-submit-goal",
        original_goal="Send the prepared message",
        criteria=(
            GoalCriterion(
                criterion_id="submit-message",
                description="The prepared message is submitted",
                effectful=True,
                required_capabilities=("desktop.ui_operation",),
                expected={
                    "state": expected_state,
                    "target": {"kind": "desktop_ui", "action": "submit_ui"},
                },
                source_step_ids=("submit-message",),
                verifier_step_ids=("verify-submit-message",),
            ),
        ),
    )
    provider = {
        "provider_kind": "background_desktop",
        "provider_id": "submit-provider-1",
    }
    assessment = runtime_goal_assessment(
        contract,
        _runtime_owned_terminal_events(contract, [
            {
                "event": "agent.tool.call",
                "run_id": contract.run_id,
                "decision_id": "decision-exact-submit",
                "plan_id": "plan-exact-submit",
                "step_id": "submit-message",
                "detail": "desktop.submit_foreground",
                "tool_call_id": "call-exact-submit",
                "capability_id": "desktop.ui_operation",
                "input_preview": {"action": "send"},
                "action_target": {
                    "kind": "desktop_ui",
                    "action": "submit_ui",
                },
                "result": {
                    "ok": True,
                    "action": "desktop.submit_foreground",
                    "submitted_action": "send",
                    "data": {
                        "key": "return",
                        "modifiers": [],
                        "submit_action": "send",
                    },
                    "desktop_execution_provider": provider,
                },
            },
            {
                "event": "agent.tool.call",
                "run_id": contract.run_id,
                "decision_id": "decision-exact-submit",
                "plan_id": "plan-exact-submit",
                "step_id": "verify-submit-message",
                "detail": "desktop.ui_elements",
                "tool_call_id": "call-verify-exact-submit",
                "source": "runtime_native_postcondition_receipt",
                "result": {
                    "ok": True,
                    "postcondition_verified": True,
                    "verification_satisfied_by_native_receipt": True,
                    "verification_predicate_kind": (
                        "exact_submit_dispatch_receipt"
                    ),
                    "source_tool": "desktop.submit_foreground",
                    "source_step_id": "submit-message",
                    "source_tool_call_id": "call-exact-submit",
                    "provider_kind": provider["provider_kind"],
                    "provider_id": provider["provider_id"],
                    "submitted_action": "send",
                    "verified_observed_state": "submitted",
                },
            },
        ]),
    )

    assert assessment.completed is completed


def _manual_native_submit_projection_events() -> tuple[GoalContract, list[dict]]:
    """Build executor facts without the test helper minting verifier authority."""

    contract = GoalContract(
        contract_id="goal-native-submit-projection",
        run_id="run-native-submit-projection",
        original_goal="Send the prepared message",
        criteria=(
            GoalCriterion(
                criterion_id="submit-message",
                description="The prepared message is submitted",
                effectful=True,
                required_capabilities=("desktop.ui_operation",),
                expected={
                    "state": "submitted",
                    "target": {"kind": "desktop_ui", "action": "submit_ui"},
                },
                source_step_ids=("submit-message",),
                verifier_step_ids=("verify-submit-message",),
            ),
        ),
    )
    provider = {
        "provider_kind": "background_desktop",
        "provider_id": "submit-provider-native-projection",
    }
    source = {
        "event": "agent.tool.call",
        "run_id": contract.run_id,
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
        "decision_id": "decision-native-submit-projection",
        "plan_id": "plan-native-submit-projection",
        "step_id": "submit-message",
        "request_id": "request-native-submit-projection",
        "detail": "desktop.submit_foreground",
        "tool_call_id": "call-native-submit-projection",
        "capability_id": "desktop.ui_operation",
        "input_preview": {"action": "send"},
        "action_target": {
            "kind": "desktop_ui",
            "action": "submit_ui",
        },
        "result": {
            "ok": True,
            "tool": "desktop.submit_foreground",
            "submitted_action": "send",
            "data": {"action": "send", "key": "return", "modifiers": []},
            "desktop_execution_provider_routed": True,
            "desktop_execution_provider": {
                **provider,
                "adapter_registered": True,
            },
            "desktop_execution_route": {
                "selected_provider_kind": provider["provider_kind"],
                "selected_provider_id": provider["provider_id"],
            },
            "desktop_execution_provider_evidence": {
                "tool": "desktop.submit_foreground",
                "executor_receipt": True,
            },
        },
    }
    verifier = {
        "event": "agent.tool.call",
        "run_id": contract.run_id,
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
        "decision_id": "decision-native-submit-projection",
        "plan_id": "plan-native-submit-projection",
        "step_id": "verify-submit-message",
        "request_id": "request-verify-native-submit-projection",
        "detail": "desktop.ui_elements",
        "tool_call_id": "call-verify-native-submit-projection",
        "source": "runtime_native_postcondition_receipt",
        "result": {
            "ok": True,
            "postcondition_verified": True,
            "verification_satisfied_by_native_receipt": True,
            "verification_run_id": contract.run_id,
            "verification_plan_id": "plan-native-submit-projection",
            "verification_predicate_kind": "exact_submit_dispatch_receipt",
            "source_tool": "desktop.submit_foreground",
            "source_step_id": "submit-message",
            "source_tool_call_id": "call-native-submit-projection",
            "submitted_action": "send",
            "verified_observed_state": "submitted",
        },
    }
    return contract, [source, verifier]


def test_native_verifier_projection_inherits_exact_source_provider_authority() -> None:
    contract, events = _manual_native_submit_projection_events()
    verifier_result = events[-1]["result"]

    assert "desktop_execution_provider" not in verifier_result
    assert "desktop_execution_route" not in verifier_result
    assert RUNTIME_EXECUTION_PROVENANCE_KEY not in verifier_result

    assessment = runtime_goal_assessment(contract, events)

    assert assessment.completed is True


def test_correlated_native_app_state_receipt_uses_exact_source_authority() -> None:
    decision = RuntimePlanner().decision(
        "Slack 显示出来",
        allowed_tools=["app.show", "desktop.ui_elements"],
    )
    contract = runtime_goal_contract(
        run_id="run-native-app-state",
        runtime_execution_envelope={
            "task_core": decision.plan.task_core.model_dump(),  # type: ignore[union-attr]
        },
        runtime_execution_metadata=None,
        messages=(),
        timeline=[],
    )
    assert contract is not None
    provider = {
        "provider_kind": "background_desktop",
        "provider_id": "app-state-provider-1",
    }
    action_target = dict(contract.criteria[0].expected["target"])
    source = {
        "event": "agent.tool.call",
        "run_id": contract.run_id,
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
        "decision_id": decision.decision_id,
        "plan_id": decision.plan.plan_id,
        "step_id": "manage-app",
        "request_id": "request-show-slack",
        "detail": "app.show",
        "tool_call_id": "call-show-slack",
        "capability_id": "desktop.app_control",
        "input_preview": {"app_name": "Slack"},
        "action_target": action_target,
        "result": {
            "ok": True,
            "action": "app.show",
            "postcondition_verified": True,
            "data": {
                "app_name": "Slack",
                "show_status": "shown",
                "postcondition_verified": True,
            },
            "desktop_execution_provider_routed": True,
            "desktop_execution_provider": {
                **provider,
                "adapter_registered": True,
            },
            "desktop_execution_route": {
                "selected_provider_kind": provider["provider_kind"],
                "selected_provider_id": provider["provider_id"],
            },
            "desktop_execution_provider_evidence": {
                "tool": "app.show",
                "executor_receipt": True,
            },
        },
    }
    verifier = {
        "event": "agent.tool.call",
        "run_id": contract.run_id,
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
        "decision_id": decision.decision_id,
        "plan_id": decision.plan.plan_id,
        "step_id": "verify-desktop-result",
        "request_id": "request-verify-show-slack",
        "source_request_id": source["request_id"],
        "detail": "desktop.ui_elements",
        "tool_call_id": "call-verify-show-slack",
        "source": "runtime_native_postcondition_receipt",
        "result": {
            "ok": True,
            "postcondition_verified": True,
            "verification_satisfied_by_native_receipt": True,
            "source_tool": "app.show",
            "source_step_id": "manage-app",
            "source_request_id": source["request_id"],
            "source_tool_call_id": source["tool_call_id"],
            "provider_kind": provider["provider_kind"],
            "provider_id": provider["provider_id"],
            "verified_observed_state": "fulfilled",
        },
    }

    assessment = runtime_goal_assessment(contract, [source, verifier])

    assert assessment.completed is True

@pytest.mark.parametrize(
    "invalid_binding",
    (
        "wrong_claimed_provider",
        "foreign_plan",
        "foreign_run",
        "missing_source_provider",
        "wrong_source_call",
        "wrong_source_step",
    ),
)
def test_native_verifier_projection_rejects_untrusted_or_foreign_source_binding(
    invalid_binding: str,
) -> None:
    contract, events = _manual_native_submit_projection_events()
    source, verifier = events
    verifier_result = verifier["result"]

    if invalid_binding == "wrong_claimed_provider":
        verifier_result["provider_kind"] = "background_desktop"
        verifier_result["provider_id"] = "other-provider"
    elif invalid_binding == "foreign_plan":
        verifier["plan_id"] = "plan-foreign"
        verifier_result["verification_plan_id"] = "plan-foreign"
    elif invalid_binding == "foreign_run":
        verifier["run_id"] = "run-foreign"
        verifier_result["verification_run_id"] = "run-foreign"
    elif invalid_binding == "missing_source_provider":
        for key in (
            "desktop_execution_provider_routed",
            "desktop_execution_provider",
            "desktop_execution_route",
            "desktop_execution_provider_evidence",
        ):
            source["result"].pop(key)
    elif invalid_binding == "wrong_source_call":
        verifier_result["source_tool_call_id"] = "call-other"
    elif invalid_binding == "wrong_source_step":
        verifier_result["source_step_id"] = "step-other"
    else:  # pragma: no cover - parametrization is intentionally exhaustive.
        raise AssertionError(f"unexpected invalid binding: {invalid_binding}")

    assessment = runtime_goal_assessment(contract, events)

    assert assessment.completed is False


def _exact_submit_approval_lineage() -> tuple[
    GoalContract,
    dict,
    dict,
    dict,
]:
    contract = GoalContract(
        contract_id="goal-approved-submit",
        run_id="run-approved-submit",
        original_goal="Send the prepared message",
        criteria=(
            GoalCriterion(
                criterion_id="criterion-approved-submit",
                description="The prepared message is submitted",
                effectful=True,
                required_capabilities=("desktop.ui_operation",),
                expected={
                    "state": "submitted",
                    "target": {"kind": "desktop_ui", "action": "submit_ui"},
                },
                source_step_ids=("submit-message",),
                verifier_step_ids=("verify-submit-message",),
            ),
        ),
    )
    provider = {
        "provider_kind": "background_desktop",
        "provider_id": "submit-provider-approved",
    }
    common = {
        "event": "agent.tool.call",
        "run_id": contract.run_id,
        "decision_id": "decision-approved-submit",
        "plan_id": "plan-approved-submit",
        "tool_plan_id": "tool-plan-approved-submit",
        "step_id": "submit-message",
        "request_id": "request-approved-submit",
        "detail": "desktop.submit_foreground",
        "tool_call_id": "call-approved-submit",
        "materialization_binding_id": "binding-approved-submit",
        "materialized_content_sha256": "a" * 64,
        "capability_id": "desktop.ui_operation",
        "input_preview": {"action": "send"},
        "action_target": {
            "kind": "desktop_ui",
            "action": "submit_ui",
        },
    }
    pending = {
        **common,
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
        "approved": False,
        "source": "runtime_policy_gate",
        "result": {
            "ok": False,
            "approval_required": True,
            "status": "approval_required",
            "policy_reason": "User approval is required before dispatch.",
        },
    }
    approved = {
        **common,
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
        "approved": True,
        "result": {
            "ok": True,
            "action": "desktop.submit_foreground",
            "submitted_action": "send",
            "data": {
                "key": "return",
                "modifiers": [],
                "submit_action": "send",
            },
            "desktop_execution_provider": provider,
        },
    }
    verifier = {
        "event": "agent.tool.call",
        "run_id": contract.run_id,
        "plan_id": "plan-approved-submit",
        "step_id": "verify-submit-message",
        "detail": "desktop.ui_elements",
        "tool_call_id": "call-verify-approved-submit",
        "source": "runtime_native_postcondition_receipt",
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
        "result": {
            "ok": True,
            "postcondition_verified": True,
            "verification_satisfied_by_native_receipt": True,
            "verification_predicate_kind": "exact_submit_dispatch_receipt",
            "source_tool": "desktop.submit_foreground",
            "source_step_id": "submit-message",
            "source_tool_call_id": "call-approved-submit",
            "provider_kind": provider["provider_kind"],
            "provider_id": provider["provider_id"],
            "submitted_action": "send",
            "verified_observed_state": "submitted",
        },
    }
    pending, approved, verifier = _runtime_owned_terminal_events(
        contract,
        [pending, approved, verifier],
    )
    return contract, pending, approved, verifier


def test_approval_pause_does_not_beat_approved_terminal_success() -> None:
    contract, pending, approved, verifier = _exact_submit_approval_lineage()

    approved_only = runtime_goal_assessment(contract, [pending, approved])

    assessment = runtime_goal_assessment(
        contract,
        [pending, approved, verifier],
    )

    assert approved_only.completed is False
    assert assessment.completed is True
    assert any(
        evidence.source_tool_call_id == "call-approved-submit"
        and evidence.status == "success"
        for evidence in assessment.evidence
    )


def test_approval_pause_allows_trusted_failed_terminal_to_win() -> None:
    contract, pending, approved, _verifier = _exact_submit_approval_lineage()
    failed = _runtime_owned_terminal_event(contract, {
        **approved,
        "result": {
            "ok": False,
            "error": "approved dispatch failed",
            "desktop_execution_provider": approved["result"][
                "desktop_execution_provider"
            ],
        },
    })

    assessment = runtime_goal_assessment(
        contract,
        [pending, failed, approved, _verifier],
    )

    assert assessment.completed is False
    assert any(
        evidence.source_tool_call_id == "call-approved-submit"
        and evidence.status == "failed"
        for evidence in assessment.evidence
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("approved", False),
        ("actor", "model"),
        ("execution_authority", "model_tool_call"),
        ("run_id", "run-forged"),
        ("plan_id", "plan-forged"),
        ("step_id", "step-forged"),
        ("request_id", "request-forged"),
        ("decision_id", "decision-forged"),
        ("materialization_binding_id", "binding-forged"),
        ("materialized_content_sha256", "b" * 64),
        ("provider", None),
    ),
)
def test_public_success_after_approval_pause_cannot_complete_goal(
    field: str,
    replacement: object,
) -> None:
    contract, pending, approved, verifier = _exact_submit_approval_lineage()
    untrusted_success = dict(approved)
    if field == "provider":
        untrusted_result = dict(untrusted_success["result"])
        untrusted_result.pop("desktop_execution_provider", None)
        untrusted_success["result"] = untrusted_result
    else:
        untrusted_success[field] = replacement

    assessment = runtime_goal_assessment(
        contract,
        [pending, untrusted_success, verifier],
    )

    assert assessment.completed is False
    assert not any(
        evidence.source_tool_call_id == "call-approved-submit"
        for evidence in assessment.evidence
    )


def test_failed_terminal_still_beats_later_success_and_verifier() -> None:
    contract, _pending, approved, verifier = _exact_submit_approval_lineage()
    failed = _runtime_owned_terminal_event(contract, {
        **approved,
        "result": {
            "ok": False,
            "error": "dispatch failed first",
            "desktop_execution_provider": approved["result"][
                "desktop_execution_provider"
            ],
        },
    })

    assessment = runtime_goal_assessment(
        contract,
        [failed, approved, verifier],
    )

    assert assessment.completed is False
    evidence = next(
        item
        for item in assessment.evidence
        if item.source_tool_call_id == "call-approved-submit"
    )
    assert evidence.status == "failed"


def test_runtime_requires_the_exact_declared_verifier_step_in_the_same_plan() -> None:
    contract = GoalContract(
        contract_id="goal-bound-verifier",
        run_id="run-bound-verifier",
        original_goal="Type the report into the target app",
        criteria=(
            GoalCriterion(
                criterion_id="type-report",
                description="The exact report is present",
                effectful=True,
                required_capabilities=("desktop.ui_operation",),
                expected={"state": "fulfilled"},
                source_step_ids=("insert-report",),
                verifier_step_ids=("verify-report",),
            ),
        ),
    )
    provider = {
        "provider_kind": "background_desktop",
        "provider_id": "cua-1",
    }
    source = _runtime_owned_terminal_event(contract, {
        "event": "agent.tool.call",
        "run_id": contract.run_id,
        "plan_id": "plan-report",
        "step_id": "insert-report",
        "detail": "app.open_and_type_into_ui_element",
        "tool_call_id": "call-insert-report",
        "capability_id": "desktop.ui_operation",
        "result": {
            "ok": True,
            "postcondition_verified": True,
            "desktop_execution_provider": provider,
        },
    })

    def verifier(*, step_id: str, plan_id: str) -> dict:
        return _runtime_owned_terminal_event(contract, {
            "event": "agent.tool.call",
            "run_id": contract.run_id,
            "plan_id": plan_id,
            "step_id": step_id,
            "detail": "desktop.verify",
            "tool_call_id": f"call-{step_id}-{plan_id}",
            "result": {
                "ok": True,
                "postcondition_verified": True,
                "verification_context_trusted": True,
                "verification_run_id": contract.run_id,
                "verification_plan_id": "plan-report",
                "provider_kind": provider["provider_kind"],
                "provider_id": provider["provider_id"],
                "verification_predicate_kind": "exact_typed_content_present",
                "source_tool_call_id": "call-insert-report",
                "source_tool": "app.open_and_type_into_ui_element",
                "source_step_id": "insert-report",
                "verified_observed_state": "typed",
            },
        })

    source_only = runtime_goal_assessment(contract, [source])
    wrong_step = runtime_goal_assessment(
        contract,
        [source, verifier(step_id="inspect-report", plan_id="plan-report")],
    )
    wrong_plan = runtime_goal_assessment(
        contract,
        [source, verifier(step_id="verify-report", plan_id="plan-other")],
    )
    completed = runtime_goal_assessment(
        contract,
        [source, verifier(step_id="verify-report", plan_id="plan-report")],
    )

    assert source_only.completed is False
    assert wrong_step.completed is False
    assert wrong_plan.completed is False
    assert completed.completed is True


def _exact_workspace_file_goal_lineage(
    verifier_tool: str = "workspace.read",
    *,
    require_semantic_adequacy: bool = False,
) -> tuple[GoalContract, dict, dict]:
    content = "# Analysis\n\nExact output 🌙\n"
    contract = GoalContract(
        contract_id="goal-exact-workspace-file",
        run_id="run-exact-workspace-file",
        original_goal="Analyze the data and write reports/analysis.md",
        criteria=(
            GoalCriterion(
                criterion_id="analysis-output",
                description="The exact analysis output file is present",
                effectful=True,
                required_capabilities=("data.analysis",),
                required_verification_predicates=(
                    (
                        "exact_file_content_present",
                        "semantic_artifact_adequacy",
                    )
                    if require_semantic_adequacy
                    else ()
                ),
                expected={
                    "state": "fulfilled",
                    "target": {
                        "kind": "workspace_file",
                        "action": "analyze",
                        "artifact_path": "reports/analysis.md",
                    },
                },
                source_step_ids=("run-analysis",),
                verifier_step_ids=("verify-analysis",),
            ),
        ),
    )
    source = _runtime_owned_terminal_event(
        contract,
        {
            "event": "agent.tool.call",
            "detail": "python.run",
            "plan_id": "plan-analysis",
            "tool_plan_id": "tool-plan-analysis",
            "decision_id": "decision-analysis",
            "step_id": "run-analysis",
            "request_id": "request-run-analysis",
            "tool_call_id": "call-run-analysis",
            "capability_id": "data.analysis",
            "action_target": {
                "kind": "workspace_file",
                "action": "analyze",
                "artifact_path": "reports/analysis.md",
            },
            "result": {"ok": True, "returncode": 0},
        },
    )
    verifier = _runtime_owned_terminal_event(
        contract,
        {
            "event": "agent.tool.call",
            "detail": verifier_tool,
            "source": "runtime_native_postcondition_receipt",
            "plan_id": "plan-analysis",
            "tool_plan_id": "tool-plan-analysis",
            "decision_id": "decision-analysis",
            "step_id": "verify-analysis",
            "request_id": "request-verify-analysis",
            "tool_call_id": "call-verify-analysis:exact-file-readback-receipt",
            "result": {
                "ok": True,
                "path": "reports/analysis.md",
                "content": content,
                "truncated": False,
                "size_bytes": len(content.encode("utf-8")),
                "content_bytes": len(content.encode("utf-8")),
                "decoding_lossy": False,
                "postcondition_verified": True,
                "verification_satisfied_by_native_receipt": True,
                "run_id": contract.run_id,
                "plan_id": "plan-analysis",
                "tool_plan_id": "tool-plan-analysis",
                "decision_id": "decision-analysis",
                "provider_kind": LOCAL_DESKTOP_PROVIDER_KIND,
                "provider_id": LOCAL_DESKTOP_PROVIDER_ID,
                "source_tool": "python.run",
                "source_step_id": "run-analysis",
                "source_request_id": "request-run-analysis",
                "source_tool_call_id": "call-run-analysis",
                "verification_predicate_kind": "exact_file_content_present",
                "verified_observed_state": "fulfilled",
                "observed_path": "reports/analysis.md",
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "content_length": len(content.encode("utf-8")),
            },
        },
    )
    return contract, source, verifier


def _semantic_artifact_assessment_event(
    contract: GoalContract,
    source: dict,
    verifier: dict,
    *,
    verdict: str = "fulfilled",
) -> dict:
    receipt = verifier["result"]
    candidate = pending_semantic_artifact_assessment_candidates(
        contract,
        [source, verifier],
    )[0]
    return {
        "event": "agent.goal.semantic_artifact.assessed",
        "run_id": contract.run_id,
        "actor": "native_runtime",
        "execution_authority": "runtime_semantic_artifact_verifier",
        "source": "runtime_semantic_artifact_verifier",
        "visibility": "internal",
        "contract_id": contract.contract_id,
        "criterion_id": contract.criteria[0].criterion_id,
        "plan_id": source["plan_id"],
        "source_tool_call_id": source["tool_call_id"],
        "source_step_id": source["step_id"],
        "structural_verifier_tool_call_id": verifier["tool_call_id"],
        "structural_verifier_step_id": verifier["step_id"],
        "observed_path": receipt["observed_path"],
        "content_sha256": receipt["content_sha256"],
        "content_length": receipt["content_length"],
        "semantic_rubric_sha256": candidate["semantic_rubric_sha256"],
        "verdict": verdict,
    }


def test_semantic_artifact_goal_requires_readback_and_fulfilled_assessment() -> None:
    contract, source, verifier = _exact_workspace_file_goal_lineage(
        require_semantic_adequacy=True,
    )
    semantic = _semantic_artifact_assessment_event(
        contract,
        source,
        verifier,
    )

    exact_only = runtime_goal_assessment(contract, [source, verifier])
    completed = runtime_goal_assessment(contract, [source, verifier, semantic])

    assert exact_only.completed is False
    assert completed.completed is True
    assert {
        evidence.verification_predicate
        for evidence in completed.evidence
        if evidence.verified
    } >= {
        "exact_file_content_present",
        "semantic_artifact_adequacy",
    }


def test_goal_assessment_projection_never_persists_exact_artifact_content() -> None:
    contract, source, verifier = _exact_workspace_file_goal_lineage(
        require_semantic_adequacy=True,
    )
    semantic = _semantic_artifact_assessment_event(
        contract,
        source,
        verifier,
    )
    assessment = runtime_goal_assessment(
        contract,
        [source, verifier, semantic],
    )
    artifact_content = verifier["result"]["content"]
    assert any(
        evidence.observed.get("content") == artifact_content
        for evidence in assessment.evidence
        if evidence.verification_predicate
        in {"exact_file_content_present", "semantic_artifact_adequacy"}
    )

    event_payload = goal_assessment_event_payload(assessment)
    parsed_json = json.loads(event_payload["goal_assessment_json"])

    def leaf_values(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from leaf_values(item)
        elif isinstance(value, list):
            for item in value:
                yield from leaf_values(item)
        else:
            yield value

    for projection in (event_payload["goal_assessment"], parsed_json):
        artifact_evidence = [
            item
            for item in projection["evidence"]
            if item["verification_predicate"]
            in {"exact_file_content_present", "semantic_artifact_adequacy"}
        ]
        assert len(artifact_evidence) == 2
        assert artifact_content not in tuple(leaf_values(projection))
        for item in artifact_evidence:
            observed = item["observed"]
            assert "content" not in observed
            assert observed["content_redacted"] is True
            assert observed["observed_path"] == verifier["result"]["observed_path"]
            assert observed["content_sha256"] == verifier["result"]["content_sha256"]
            assert observed["content_length"] == verifier["result"]["content_length"]


def test_pending_semantic_artifact_candidate_preserves_exact_replay_bindings() -> None:
    contract, source, verifier = _exact_workspace_file_goal_lineage(
        require_semantic_adequacy=True,
    )

    candidates = pending_semantic_artifact_assessment_candidates(
        contract,
        [source, verifier],
    )

    semantic_rubric_sha256 = _semantic_artifact_assessment_event(
        contract,
        source,
        verifier,
    )["semantic_rubric_sha256"]
    assert candidates == (
        {
            "contract_id": contract.contract_id,
            "criterion_id": contract.criteria[0].criterion_id,
            "run_id": contract.run_id,
            "plan_id": source["plan_id"],
            "source_tool_call_id": source["tool_call_id"],
            "source_step_id": source["step_id"],
            "structural_verifier_tool_call_id": verifier["tool_call_id"],
            "structural_verifier_step_id": verifier["step_id"],
            "observed_path": verifier["result"]["observed_path"],
            "content_sha256": verifier["result"]["content_sha256"],
            "content_length": verifier["result"]["content_length"],
            "content": verifier["result"]["content"],
            "original_goal": contract.original_goal,
            "criterion_description": contract.criteria[0].description,
            "criterion_expected": {
                "state": "fulfilled",
                "target": {
                    "kind": "workspace_file",
                    "action": "analyze",
                    "artifact_path": "reports/analysis.md",
                },
            },
            "semantic_rubric_sha256": semantic_rubric_sha256,
            "verification_predicate": "semantic_artifact_adequacy",
        },
    )
    semantic = _semantic_artifact_assessment_event(
        contract,
        source,
        verifier,
    )
    assert pending_semantic_artifact_assessment_candidates(
        contract,
        [source, verifier, semantic],
    ) == ()


def test_semantic_artifact_candidate_deduplicates_compatibility_projections() -> None:
    contract, source, exact_receipt = _exact_workspace_file_goal_lineage(
        require_semantic_adequacy=True,
    )
    compatibility_projection = copy.deepcopy(exact_receipt)
    compatibility_projection["request_id"] = "request-verify-analysis-projection"
    compatibility_projection["tool_call_id"] = "call-verify-analysis"

    candidates = pending_semantic_artifact_assessment_candidates(
        contract,
        [source, compatibility_projection, exact_receipt],
    )

    assert len(candidates) == 1
    assert candidates[0]["structural_verifier_tool_call_id"] == exact_receipt[
        "tool_call_id"
    ]

    insufficient = _semantic_artifact_assessment_event(
        contract,
        source,
        exact_receipt,
        verdict="insufficient",
    )
    assessed_timeline = [
        source,
        compatibility_projection,
        exact_receipt,
        insufficient,
    ]
    assert pending_semantic_artifact_assessment_candidates(
        contract,
        assessed_timeline,
    ) == ()

    next_source = copy.deepcopy(source)
    next_source["request_id"] = "request-run-analysis-next"
    next_source["tool_call_id"] = "call-run-analysis-next"
    next_content = "# Analysis\n\nRewritten and adequate output.\n"
    next_receipt = copy.deepcopy(exact_receipt)
    next_receipt["request_id"] = "request-verify-analysis-next"
    next_receipt["tool_call_id"] = (
        "call-verify-analysis-next:exact-file-readback-receipt"
    )
    next_receipt["result"].update(
        {
            "content": next_content,
            "size_bytes": len(next_content.encode("utf-8")),
            "content_bytes": len(next_content.encode("utf-8")),
            "source_request_id": next_source["request_id"],
            "source_tool_call_id": next_source["tool_call_id"],
            "content_sha256": hashlib.sha256(
                next_content.encode("utf-8")
            ).hexdigest(),
            "content_length": len(next_content.encode("utf-8")),
        }
    )

    rewritten_candidates = pending_semantic_artifact_assessment_candidates(
        contract,
        [*assessed_timeline, next_source, next_receipt],
    )

    assert len(rewritten_candidates) == 1
    assert rewritten_candidates[0]["source_tool_call_id"] == next_source[
        "tool_call_id"
    ]
    assert rewritten_candidates[0]["content_sha256"] == next_receipt["result"][
        "content_sha256"
    ]


@pytest.mark.parametrize("change", ["new-source-call", "new-content-digest"])
def test_semantic_artifact_assessment_identity_allows_genuinely_new_work(
    change: str,
) -> None:
    contract, source, exact_receipt = _exact_workspace_file_goal_lineage(
        require_semantic_adequacy=True,
    )
    insufficient = _semantic_artifact_assessment_event(
        contract,
        source,
        exact_receipt,
        verdict="insufficient",
    )
    timeline = [source, exact_receipt, insufficient]
    next_receipt = copy.deepcopy(exact_receipt)
    next_receipt["request_id"] = f"request-verify-{change}"
    next_receipt["tool_call_id"] = (
        f"call-verify-{change}:exact-file-readback-receipt"
    )
    if change == "new-source-call":
        next_source = copy.deepcopy(source)
        next_source["request_id"] = "request-run-analysis-new-source"
        next_source["tool_call_id"] = "call-run-analysis-new-source"
        next_receipt["result"]["source_request_id"] = next_source["request_id"]
        next_receipt["result"]["source_tool_call_id"] = next_source[
            "tool_call_id"
        ]
        timeline.extend((next_source, next_receipt))
    else:
        next_content = "# Analysis\n\nA genuinely rewritten report.\n"
        next_bytes = next_content.encode("utf-8")
        next_receipt["result"].update(
            {
                "content": next_content,
                "size_bytes": len(next_bytes),
                "content_bytes": len(next_bytes),
                "content_sha256": hashlib.sha256(next_bytes).hexdigest(),
                "content_length": len(next_bytes),
            }
        )
        timeline.append(next_receipt)

    candidates = pending_semantic_artifact_assessment_candidates(
        contract,
        timeline,
    )

    assert len(candidates) == 1
    assert candidates[0]["structural_verifier_tool_call_id"] == next_receipt[
        "tool_call_id"
    ]


@pytest.mark.parametrize("verdict", ["insufficient", "uncertain"])
def test_semantic_artifact_nonfulfilled_verdict_is_terminal_unverified_evidence(
    verdict: str,
) -> None:
    contract, source, verifier = _exact_workspace_file_goal_lineage(
        require_semantic_adequacy=True,
    )
    semantic = _semantic_artifact_assessment_event(
        contract,
        source,
        verifier,
        verdict=verdict,
    )

    assessment = runtime_goal_assessment(
        contract,
        [source, verifier, semantic],
    )

    assert assessment.completed is False
    semantic_evidence = next(
        evidence
        for evidence in assessment.evidence
        if evidence.verification_predicate == "semantic_artifact_adequacy"
    )
    assert semantic_evidence.verified is False
    assert semantic_evidence.status == verdict
    assert pending_semantic_artifact_assessment_candidates(
        contract,
        [source, verifier, semantic],
    ) == ()


@pytest.mark.parametrize(
    "mismatch",
    [
        "malformed-verdict",
        "malformed-length",
        "forged-authority",
        "forged-source",
        "public",
        "model",
        "wrong-run",
        "wrong-contract",
        "wrong-criterion",
        "wrong-plan",
        "wrong-source-call",
        "wrong-source-step",
        "wrong-verifier-call",
        "wrong-verifier-step",
        "wrong-path",
        "wrong-digest",
        "wrong-length",
        "wrong-rubric",
        "wrong-order",
    ],
)
def test_semantic_artifact_assessment_rejects_untrusted_or_wrong_lineage(
    mismatch: str,
) -> None:
    contract, source, verifier = _exact_workspace_file_goal_lineage(
        require_semantic_adequacy=True,
    )
    semantic = _semantic_artifact_assessment_event(
        contract,
        source,
        verifier,
    )
    if mismatch == "malformed-verdict":
        semantic["verdict"] = "yes"
    elif mismatch == "malformed-length":
        semantic["content_length"] = str(semantic["content_length"])
    elif mismatch == "forged-authority":
        semantic["execution_authority"] = "runtime_tool_executor"
    elif mismatch == "forged-source":
        semantic["source"] = "model"
    elif mismatch == "public":
        semantic["visibility"] = "public"
    elif mismatch == "model":
        semantic["actor"] = "model"
    elif mismatch == "wrong-run":
        semantic["run_id"] = "run-other"
    elif mismatch == "wrong-contract":
        semantic["contract_id"] = "goal-other"
    elif mismatch == "wrong-criterion":
        semantic["criterion_id"] = "criterion-other"
    elif mismatch == "wrong-plan":
        semantic["plan_id"] = "plan-other"
    elif mismatch == "wrong-source-call":
        semantic["source_tool_call_id"] = "call-source-other"
    elif mismatch == "wrong-source-step":
        semantic["source_step_id"] = "write-other"
    elif mismatch == "wrong-verifier-call":
        semantic["structural_verifier_tool_call_id"] = "call-read-other"
    elif mismatch == "wrong-verifier-step":
        semantic["structural_verifier_step_id"] = "read-other"
    elif mismatch == "wrong-path":
        semantic["observed_path"] = "reports/other.md"
    elif mismatch == "wrong-digest":
        semantic["content_sha256"] = "b" * 64
    elif mismatch == "wrong-length":
        semantic["content_length"] += 1
    elif mismatch == "wrong-rubric":
        semantic["semantic_rubric_sha256"] = "c" * 64

    timeline = (
        [semantic, source, verifier]
        if mismatch == "wrong-order"
        else [source, verifier, semantic]
    )
    assessment = runtime_goal_assessment(contract, timeline)

    assert assessment.completed is False
    assert not any(
        evidence.verification_predicate == "semantic_artifact_adequacy"
        for evidence in assessment.evidence
    )
    assert len(
        pending_semantic_artifact_assessment_candidates(contract, timeline)
    ) == 1


def test_semantic_artifact_assessment_cannot_exist_without_prior_exact_readback() -> None:
    contract, source, verifier = _exact_workspace_file_goal_lineage(
        require_semantic_adequacy=True,
    )
    semantic = _semantic_artifact_assessment_event(
        contract,
        source,
        verifier,
    )

    assessment = runtime_goal_assessment(contract, [source, semantic])

    assert assessment.completed is False
    assert not any(
        evidence.verification_predicate == "semantic_artifact_adequacy"
        for evidence in assessment.evidence
    )
    assert pending_semantic_artifact_assessment_candidates(
        contract,
        [source, semantic],
    ) == ()


def test_semantic_artifact_first_trusted_verdict_wins_for_the_same_digest() -> None:
    contract, source, verifier = _exact_workspace_file_goal_lineage(
        require_semantic_adequacy=True,
    )
    insufficient = _semantic_artifact_assessment_event(
        contract,
        source,
        verifier,
        verdict="insufficient",
    )
    conflicting_fulfilled = _semantic_artifact_assessment_event(
        contract,
        source,
        verifier,
        verdict="fulfilled",
    )

    assessment = runtime_goal_assessment(
        contract,
        [source, verifier, insufficient, conflicting_fulfilled],
    )

    assert assessment.completed is False
    semantic_evidence = [
        evidence
        for evidence in assessment.evidence
        if evidence.verification_predicate == "semantic_artifact_adequacy"
    ]
    assert len(semantic_evidence) == 1
    assert semantic_evidence[0].status == "insufficient"


def test_semantic_artifact_internal_event_replays_from_persisted_payload() -> None:
    contract, source, verifier = _exact_workspace_file_goal_lineage(
        require_semantic_adequacy=True,
    )
    semantic = _semantic_artifact_assessment_event(
        contract,
        source,
        verifier,
    )
    persisted = {
        "event": semantic["event"],
        "payload": {
            key: value
            for key, value in semantic.items()
            if key != "event"
        },
    }

    assessment = runtime_goal_assessment(
        contract,
        [source, verifier, persisted],
    )

    assert assessment.completed is True


def test_exact_workspace_file_receipt_completes_only_after_exact_readback() -> None:
    contract, source, verifier = _exact_workspace_file_goal_lineage()
    raw_read = _runtime_owned_terminal_event(
        contract,
        {
            "event": "agent.tool.call",
            "detail": "workspace.read",
            "plan_id": "plan-analysis",
            "tool_plan_id": "tool-plan-analysis",
            "decision_id": "decision-analysis",
            "step_id": "verify-analysis",
            "request_id": "request-verify-analysis",
            "tool_call_id": "call-verify-analysis",
            "result": {
                "ok": True,
                "path": "reports/analysis.md",
                "content": verifier["result"]["content"],
            },
        },
    )

    source_only = runtime_goal_assessment(contract, [source])
    completed = runtime_goal_assessment(contract, [source, verifier])
    persisted_replay = runtime_goal_assessment(
        contract,
        [source, raw_read, verifier],
    )

    assert source_only.completed is False
    assert completed.completed is True
    assert persisted_replay.completed is True


@pytest.mark.parametrize(
    "verifier_tool",
    ["workspace.read", "fs.read_file", "file.read"],
)
def test_exact_workspace_file_receipt_accepts_trusted_read_aliases(
    verifier_tool: str,
) -> None:
    contract, source, verifier = _exact_workspace_file_goal_lineage(verifier_tool)

    assert runtime_goal_assessment(contract, [source, verifier]).completed is True


def test_exact_workspace_file_receipt_rejects_untrusted_read_alias() -> None:
    contract, source, verifier = _exact_workspace_file_goal_lineage()
    verifier["detail"] = "filesystem.read"

    assert runtime_goal_assessment(contract, [source, verifier]).completed is False


@pytest.mark.parametrize(
    "mismatch",
    [
        "wrong-verifier-step",
        "wrong-result-run",
        "wrong-path",
        "wrong-source-call",
        "wrong-plan",
        "wrong-provider",
        "wrong-predicate",
        "wrong-content-digest",
        "truncated-readback",
        "lossy-readback",
        "wrong-size",
        "failed-source",
    ],
)
def test_exact_workspace_file_receipt_rejects_wrong_step_path_or_lineage(
    mismatch: str,
) -> None:
    contract, source, verifier = _exact_workspace_file_goal_lineage()
    result = verifier["result"]
    if mismatch == "wrong-verifier-step":
        verifier["step_id"] = "inspect-analysis"
    elif mismatch == "wrong-result-run":
        result["run_id"] = "run-other"
    elif mismatch == "wrong-path":
        result["observed_path"] = "reports/other.md"
    elif mismatch == "wrong-source-call":
        result["source_tool_call_id"] = "call-other"
    elif mismatch == "wrong-plan":
        verifier["plan_id"] = "plan-other"
    elif mismatch == "wrong-provider":
        result["provider_id"] = "local-other"
    elif mismatch == "wrong-predicate":
        result["verification_predicate_kind"] = "file_exists"
    elif mismatch == "wrong-content-digest":
        result["content_sha256"] = hashlib.sha256(b"other").hexdigest()
    elif mismatch == "truncated-readback":
        result["truncated"] = True
    elif mismatch == "lossy-readback":
        result["decoding_lossy"] = True
    elif mismatch == "wrong-size":
        result["size_bytes"] += 1
    elif mismatch == "failed-source":
        source["result"]["ok"] = False
        source["result"]["returncode"] = 1

    assessment = runtime_goal_assessment(contract, [source, verifier])

    assert assessment.completed is False


def test_exact_workspace_file_receipt_requires_declared_verifier_step() -> None:
    contract, source, verifier = _exact_workspace_file_goal_lineage()
    criterion = contract.criteria[0]
    contract = GoalContract(
        contract_id=contract.contract_id,
        run_id=contract.run_id,
        original_goal=contract.original_goal,
        criteria=(
            GoalCriterion(
                criterion_id=criterion.criterion_id,
                description=criterion.description,
                effectful=criterion.effectful,
                required_capabilities=criterion.required_capabilities,
                expected=criterion.expected,
                source_step_ids=criterion.source_step_ids,
                verifier_step_ids=(),
            ),
        ),
    )

    assessment = runtime_goal_assessment(contract, [source, verifier])

    assert assessment.completed is False


def _code_repair_lineage_events(
    contract: GoalContract,
    *,
    repair_overrides: dict | None = None,
) -> list[dict]:
    criterion = contract.criteria[0]
    coordinator = GoalCoordinator()
    _, subgoal = coordinator.open_subgoal(
        contract,
        coordinator.initial(contract),
        criterion_id=criterion.criterion_id,
        action="repair_after_failed_verification",
        description="Repair after the declared verifier failed.",
        source_tool_call_id="call-code-verifier-failed",
    )
    assert subgoal is not None
    plan_id = "plan-code-repair-lineage"
    provenance = {
        "source": RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
        "version": RUNTIME_EXECUTION_PROVENANCE_VERSION,
    }
    provider = {
        "provider_kind": LOCAL_DESKTOP_PROVIDER_KIND,
        "provider_id": LOCAL_DESKTOP_PROVIDER_ID,
    }
    scope_id = "recovery-scope-" + hashlib.sha256(
        (
            f"{contract.contract_id}|{subgoal.subgoal_id}|"
            "call-code-root|call-code-verifier-failed"
        ).encode("utf-8")
    ).hexdigest()[:24]
    common_terminal = {
        "event": "agent.tool.call",
        "run_id": contract.run_id,
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
        "plan_id": plan_id,
    }
    repair = {
        **common_terminal,
        "step_id": "repair-after-code-verification",
        "source_step_id": "apply-code-changes",
        "detail": "workspace.write_patch",
        "tool_call_id": "call-code-repair",
        "capability_id": "file.workspace_write",
        "source": "runtime_internal_recovery",
        "source_tool_call_id": "call-code-verifier-failed",
        "recovery_link_kind": "coordinator_action",
        "recovery_action": subgoal.action,
        "recovery_source_tool": "terminal.run",
        "recovery_suggested_tool": "workspace.write_patch",
        "recovery_scope_id": scope_id,
        "replan_recovery_identity": scope_id,
        "recovery_origin_tool_call_id": "call-code-verifier-failed",
        "goal_contract_id": contract.contract_id,
        "goal_criterion_id": criterion.criterion_id,
        "goal_subgoal_id": subgoal.subgoal_id,
        "root_goal_unchanged": True,
        "root_source_tool_call_id": "call-code-root",
        "root_source_step_id": "apply-code-changes",
        "root_verifier_step_id": "verify-code-changes",
        "root_plan_id": plan_id,
        "result": {
            "ok": True,
            "state": "persisted",
            RUNTIME_EXECUTION_PROVENANCE_KEY: provenance,
        },
    }
    repair.update(
        trusted_recovery_trace_fields(
            "workspace.write_patch",
            repair,
            {
                "version": RUNTIME_PRIVATE_RECOVERY_CONTEXT_VERSION,
                "_authority": RUNTIME_PRIVATE_RECOVERY_AUTHORITY,
                "run_id": contract.run_id,
                "return_to_root": True,
                **{
                    key: repair[key]
                    for key in (
                        "tool_call_id",
                        "source_tool_call_id",
                        "recovery_source_tool",
                        "recovery_action",
                        "recovery_scope_id",
                        "goal_contract_id",
                        "goal_criterion_id",
                        "goal_subgoal_id",
                        "root_goal_unchanged",
                        "plan_id",
                        "source_step_id",
                        "recovery_suggested_tool",
                        "root_source_tool_call_id",
                        "root_source_step_id",
                        "root_verifier_step_id",
                        "root_plan_id",
                        "recovery_origin_tool_call_id",
                    )
                },
            },
            run_id=contract.run_id,
        )
    )
    repair.update(repair_overrides or {})
    return _runtime_owned_terminal_events(contract, [
        {
            **common_terminal,
            "step_id": "apply-code-changes",
            "detail": "workspace.write_patch",
            "tool_call_id": "call-code-root",
            "capability_id": "file.workspace_write",
            "result": {
                "ok": True,
                "state": "persisted",
                RUNTIME_EXECUTION_PROVENANCE_KEY: provenance,
            },
        },
        {
            **common_terminal,
            "step_id": "verify-code-changes",
            "detail": "terminal.run",
            "tool_call_id": "call-code-verifier-failed",
            "capability_id": "terminal.execution",
            "result": {
                "ok": False,
                "exit_code": 1,
                "error": "tests failed",
                RUNTIME_EXECUTION_PROVENANCE_KEY: provenance,
            },
        },
        {
            "event": "agent.goal.subgoal.opened",
            "run_id": contract.run_id,
            "status": "opened",
            "source": "runtime_goal_coordinator",
            "actor": "native_runtime",
            "execution_authority": "runtime_goal_coordinator",
            "visibility": "internal",
            "contract_id": contract.contract_id,
            "criterion_id": criterion.criterion_id,
            "source_tool_call_id": subgoal.source_tool_call_id,
            "recovery_origin_tool_call_id": "call-code-verifier-failed",
            "root_source_tool_call_id": "call-code-root",
            "root_source_step_id": "apply-code-changes",
            "root_verifier_step_id": "verify-code-changes",
            "root_plan_id": plan_id,
            "root_provider_kind": provider["provider_kind"],
            "root_provider_id": provider["provider_id"],
            "subgoal": subgoal.to_payload(),
        },
        repair,
    ])


def _code_repair_contract(run_id: str) -> GoalContract:
    return GoalContract(
        contract_id=f"goal-code-repair-{run_id}",
        run_id=run_id,
        original_goal="Repair the file and verify it",
        criteria=(
            GoalCriterion(
                criterion_id=f"criterion-code-repair-{run_id}",
                description="The exact repair passes verification",
                effectful=True,
                required_capabilities=("file.workspace_write",),
                source_step_ids=("apply-code-changes",),
                verifier_step_ids=("verify-code-changes",),
            ),
        ),
    )


def test_process_private_code_repair_is_bound_to_root_source_evidence() -> None:
    contract = _code_repair_contract("private-authority")

    assessment = runtime_goal_assessment(
        contract,
        _code_repair_lineage_events(contract),
    )

    assert assessment.completed is False
    repair_evidence = next(
        item
        for item in assessment.evidence
        if item.source_tool_call_id == "call-code-repair"
    )
    assert repair_evidence.source_step_id == "apply-code-changes"
    assert repair_evidence.plan_id == "plan-code-repair-lineage"


@pytest.mark.parametrize(
    "repair_overrides",
    (
        {"source": "model_followup"},
        {"actor": "model"},
        {"execution_authority": "model_tool_call"},
        {"recovery_context_trusted": False},
    ),
)
def test_public_or_model_forged_code_repair_cannot_add_root_evidence(
    repair_overrides: dict,
) -> None:
    contract = _code_repair_contract("forged-authority")

    assessment = runtime_goal_assessment(
        contract,
        _code_repair_lineage_events(
            contract,
            repair_overrides=repair_overrides,
        ),
    )

    assert not any(
        item.source_tool_call_id == "call-code-repair"
        for item in assessment.evidence
    )


def _media_recovery_lineage_events(
    contract: GoalContract,
    *,
    retry_overrides: dict | None = None,
) -> list[dict]:
    coordinator = GoalCoordinator()
    _, subgoal = coordinator.open_subgoal(
        contract,
        coordinator.initial(contract),
        criterion_id=contract.criteria[0].criterion_id,
        action="resolve_entity_alias",
        description="Resolve one evidenced alias, then return to playback",
        source_tool_call_id="play-original",
    )
    assert subgoal is not None
    plan_id = "plan-media-recovery"
    source_step_id = "control-media-playback"
    provider = {
        "provider_kind": "background_desktop",
        "provider_id": "media-provider-1",
    }
    scope_id = "tool-attempt:" + hashlib.sha256(
        "media.apple_music_play\0play-original".encode("utf-8")
    ).hexdigest()[:24]
    retry = {
        "event": "agent.tool.call",
        "run_id": contract.run_id,
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
        "step_id": "generated-recovery-retry",
        "detail": "media.apple_music_play",
        "tool_call_id": "play-retry",
        "plan_id": plan_id,
        "source_step_id": source_step_id,
        "capability_id": "media.playback",
        "source": "runtime_internal_recovery",
        "source_tool_call_id": "play-original",
        "recovery_link_kind": "coordinator_action",
        "recovery_action": subgoal.action,
        "recovery_source_tool": "media.apple_music_play",
        "recovery_suggested_tool": "media.apple_music_play",
        "recovery_scope_id": scope_id,
        "replan_recovery_identity": scope_id,
        "goal_contract_id": contract.contract_id,
        "goal_criterion_id": subgoal.criterion_id,
        "goal_subgoal_id": subgoal.subgoal_id,
        "root_goal_unchanged": True,
        "result": {
            "ok": True,
            "action": "media.apple_music_play",
            "postcondition_verified": True,
            "desktop_execution_provider": provider,
            "data": {
                "query": "Moonlight canonical alias",
                "status": "played",
                "track": "Moonlight canonical alias",
                "track_identity_verified": True,
                "player_state": "playing",
                "playback_started": True,
                "postcondition_verified": True,
            },
        },
    }
    retry.update(
        trusted_recovery_trace_fields(
            "media.apple_music_play",
            retry,
            {
                "version": RUNTIME_PRIVATE_RECOVERY_CONTEXT_VERSION,
                "_authority": RUNTIME_PRIVATE_RECOVERY_AUTHORITY,
                "run_id": contract.run_id,
                "return_to_root": True,
                **{
                    key: retry[key]
                    for key in (
                        "tool_call_id",
                        "source_tool_call_id",
                        "recovery_source_tool",
                        "recovery_action",
                        "recovery_scope_id",
                        "goal_contract_id",
                        "goal_criterion_id",
                        "goal_subgoal_id",
                        "root_goal_unchanged",
                        "plan_id",
                        "source_step_id",
                        "recovery_suggested_tool",
                    )
                },
            },
            run_id=contract.run_id,
        )
    )
    retry.update(retry_overrides or {})
    return _runtime_owned_terminal_events(contract, [
        {
            "event": "agent.tool.call",
            "run_id": contract.run_id,
            "actor": "native_runtime",
            "execution_authority": "runtime_tool_executor",
            "plan_id": plan_id,
            "step_id": source_step_id,
            "detail": "media.apple_music_play",
            "tool_call_id": "play-original",
            "capability_id": "media.playback",
            "action_target": {
                "kind": "media",
                "action": "play",
                "query": "Moonlight",
            },
            "result": {
                "ok": True,
                "action": "media.apple_music_play",
                "desktop_execution_provider": provider,
                "data": {
                    "query": "Moonlight",
                    "status": "not_found",
                    "outcome": "partial",
                    "playback_started": False,
                },
            },
        },
        {
            "event": "agent.goal.subgoal.opened",
            "run_id": contract.run_id,
            "status": "opened",
            "source": "runtime_goal_coordinator",
            "actor": "native_runtime",
            "execution_authority": "runtime_goal_coordinator",
            "visibility": "internal",
            "contract_id": contract.contract_id,
            "criterion_id": subgoal.criterion_id,
            "source_tool_call_id": subgoal.source_tool_call_id,
            "subgoal": subgoal.to_payload(),
        },
        retry,
    ])


def test_verified_recovery_retry_returns_to_original_goal_criterion() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-recovery",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None

    assessment = runtime_goal_assessment(
        contract,
        _media_recovery_lineage_events(contract),
    )

    assert assessment.completed is True
    retry_evidence = next(
        item
        for item in assessment.evidence
        if item.source_tool_call_id == "play-retry"
    )
    assert retry_evidence.observed["target"]["query"] == "Moonlight"


def test_generated_recovery_step_without_goal_lineage_cannot_complete_root() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-recovery-forged",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    invalid_variants = (
        {"goal_contract_id": "another-contract"},
        {"goal_criterion_id": "another-criterion"},
        {"goal_subgoal_id": "another-subgoal"},
        {"recovery_context_trusted": False},
        {"root_goal_unchanged": False},
        {"recovery_scope_id": "different-scope"},
        {"plan_id": "another-plan"},
        {"source_step_id": "another-step"},
        {"recovery_suggested_tool": "another-tool"},
        {"source": "model_followup"},
        {"source_tool_call_id": "another-source"},
        {"recovery_action": "another-action"},
        {"run_id": "another-run"},
        {"run_id": ""},
        {"plan_id": ""},
        {"actor": ""},
        {"actor": "model"},
        {"execution_authority": ""},
        {"execution_authority": "model_tool_call"},
        {"detail": "browser.search"},
    )

    for overrides in invalid_variants:
        assessment = runtime_goal_assessment(
            contract,
            _media_recovery_lineage_events(
                contract,
                retry_overrides=overrides,
            ),
        )
        assert assessment.completed is False, overrides


def test_direct_recovery_without_provider_receipt_cannot_complete_root() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-recovery-missing-provider",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    events = _media_recovery_lineage_events(contract)
    retry = events[-1]
    retry_result = dict(retry["result"])
    retry_result.pop("desktop_execution_provider", None)
    retry["result"] = retry_result

    assessment = runtime_goal_assessment(contract, events)

    assert assessment.completed is False


def test_recovery_retry_from_a_different_provider_cannot_complete_root() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-recovery-provider",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    events = _media_recovery_lineage_events(contract)
    retry = events[-1]
    # Even a forged retry that copies the root planner step cannot bypass the
    # exact provider binding.
    retry["step_id"] = "control-media-playback"
    retry_result = dict(retry["result"])
    retry_result["desktop_execution_provider"] = {
        "provider_kind": "background_desktop",
        "provider_id": "forged-provider",
    }
    retry["result"] = retry_result

    assessment = runtime_goal_assessment(contract, events)

    assert assessment.completed is False


def test_replayed_recovery_call_cannot_replace_its_first_terminal_fact() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-recovery-replay",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    source, opened, retry = _media_recovery_lineage_events(contract)
    provider = dict(retry["result"]["desktop_execution_provider"])
    first_terminal = _runtime_owned_terminal_event(contract, {
        **retry,
        "result": {
            "ok": True,
            "action": "media.apple_music_play",
            "desktop_execution_provider": provider,
            "data": {
                "query": "Moonlight canonical alias",
                "status": "not_found",
                "outcome": "partial",
                "playback_started": False,
            },
        },
    })

    assessment = runtime_goal_assessment(
        contract,
        [source, opened, first_terminal, retry],
    )

    assert assessment.completed is False
    assert len(
        [
            item
            for item in assessment.evidence
            if item.source_tool_call_id == "play-retry"
        ]
    ) == 1


def test_recovery_retry_before_subgoal_open_cannot_complete_root() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-recovery-order",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    source, opened, retry = _media_recovery_lineage_events(contract)

    assessment = runtime_goal_assessment(contract, [source, retry, opened])

    assert assessment.completed is False


def test_same_capability_from_wrong_planner_step_cannot_complete_root() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-wrong-step",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    event = _media_recovery_lineage_events(contract)[-1]
    event = {
        **event,
        "source": "model_followup",
        "recovery_link_kind": "",
        "goal_subgoal_id": "",
    }

    assessment = runtime_goal_assessment(contract, [event])

    assert assessment.completed is False


def test_exact_open_path_native_receipt_completes_bound_desktop_goal() -> None:
    contract = GoalContract(
        contract_id="goal-open-path",
        run_id="run-open-path",
        original_goal="Open Downloads/report.pdf with a discovered PDF editor",
        criteria=(
            GoalCriterion(
                criterion_id="criterion-open-path",
                description="Open the exact path with the selected app",
                effectful=True,
                required_capabilities=("file.desktop_access",),
                expected={
                    "state": "fulfilled",
                    "target": {
                        "kind": "desktop_app",
                        "action": "open_path_with_selected_app",
                        "app_name": "<selected app from desktop.list_apps>",
                        "selection_source": "desktop.list_apps",
                        "query": "pdf",
                        "target_path": "Downloads/report.pdf",
                    },
                },
                source_step_ids=("open-selected-discovered-app",),
                verifier_step_ids=("verify-desktop-result",),
            ),
        ),
    )
    plan_id = "plan-open-path"
    source_call_id = "call-open-path"
    source_request_id = "request-open-path"
    source = _runtime_owned_terminal_event(
        contract,
        {
            "event": "agent.tool.call",
            "detail": "desktop.open_path_with_app",
            "plan_id": plan_id,
            "step_id": "open-selected-discovered-app",
            "request_id": source_request_id,
            "tool_call_id": source_call_id,
            "capability_id": "file.desktop_access",
            "input_preview": {
                "path": "Downloads/report.pdf",
                "app_name": "PixelForge",
                "requested_app_name": "pdf",
                "resolved_app_name": "PixelForge",
            },
            "action_target": {
                "kind": "desktop_app",
                "action": "open_path_with_selected_app",
                "app_name": "<selected app from desktop.list_apps>",
                "selection_source": "desktop.list_apps",
                "query": "pdf",
                "target_path": "Downloads/report.pdf",
            },
            "result": {
                "ok": True,
                "action": "desktop.open_path_with_app",
                "data": {
                    "path": "Downloads/report.pdf",
                    "app_name": "PixelForge",
                    "open_target": "app_open",
                    "exists": True,
                },
            },
        },
    )
    verifier = {
        "event": "agent.tool.call",
        "detail": "desktop.ui_elements",
        "source": "runtime_native_postcondition_receipt",
        "run_id": contract.run_id,
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
        "plan_id": plan_id,
        "step_id": "verify-desktop-result",
        "request_id": "request-verify-open-path",
        "tool_call_id": "call-verify-open-path",
        "result": {
            "ok": True,
            "action": "desktop.ui_elements",
            "postcondition_verified": True,
            "verification_satisfied_by_native_receipt": True,
            "source_tool": "desktop.open_path_with_app",
            "source_step_id": "open-selected-discovered-app",
            "source_request_id": source_request_id,
            "source_tool_call_id": source_call_id,
            "verified_observed_state": "fulfilled",
        },
    }

    assessment = runtime_goal_assessment(contract, [source, verifier])

    assert assessment.completed is True

    mismatched_source = {
        **source,
        "result": {
            **source["result"],
            "data": {
                **source["result"]["data"],
                "path": "Downloads/other.pdf",
            },
        },
    }

    assert runtime_goal_assessment(
        contract,
        [mismatched_source, verifier],
    ).completed is False


@pytest.mark.parametrize(
    ("tool_name", "request_input", "result", "expected_match"),
    (
        pytest.param(
            "desktop.open_path_with_app",
            {"app_name": "PixelForge", "path": "Downloads/report.pdf"},
            {
                "ok": True,
                "action": "desktop.open_path_with_app",
                "data": {
                    "app_name": "Preview",
                    "path": "Downloads/report.pdf",
                    "open_target": "app_open",
                    "exists": True,
                },
            },
            False,
            id="wrong-app",
        ),
        pytest.param(
            "desktop.open_path_with_app",
            {"app_name": "PixelForge", "path": "Downloads/report.pdf"},
            {
                "ok": True,
                "action": "desktop.open_path_with_app",
                "data": {
                    "app_name": "PixelForge",
                    "path": "Downloads/other.pdf",
                    "open_target": "app_open",
                    "exists": True,
                },
            },
            False,
            id="wrong-path",
        ),
        pytest.param(
            "desktop.safe_key",
            {"action": "arrow_down", "repeat_count": 3},
            {
                "ok": True,
                "action": "desktop.safe_key",
                "data": {"key_action": "escape", "repeat_count": 3},
            },
            False,
            id="wrong-key",
        ),
        pytest.param(
            "desktop.safe_key",
            {"action": "arrow_down", "repeat_count": 3},
            {
                "ok": True,
                "action": "desktop.safe_key",
                "data": {"key_action": "arrow_down", "repeat_count": 1},
            },
            False,
            id="wrong-repeat",
        ),
        pytest.param(
            "desktop.safe_scroll",
            {"direction": "down", "pages": 2},
            {
                "ok": True,
                "action": "desktop.safe_scroll",
                "data": {"direction": "down", "pages": 1},
            },
            False,
            id="wrong-pages",
        ),
        pytest.param(
            "desktop.safe_click",
            {"x": 480, "y": 320, "click_count": 2},
            {
                "ok": True,
                "action": "desktop.safe_click",
                "data": {"x": 481, "y": 320, "click_count": 2},
            },
            False,
            id="wrong-coords",
        ),
        pytest.param(
            "desktop.safe_key",
            {"action": "arrow_down", "repeat_count": True},
            {
                "ok": True,
                "action": "desktop.safe_key",
                "data": {"key_action": "arrow_down", "repeat_count": 1},
            },
            False,
            id="bool-repeat-request-rejected",
        ),
        pytest.param(
            "desktop.safe_scroll",
            {"direction": "down", "pages": 2},
            {
                "ok": True,
                "action": "desktop.safe_scroll",
                "data": {"direction": "down", "pages": True},
            },
            False,
            id="bool-pages-result-rejected",
        ),
        pytest.param(
            "desktop.safe_click",
            {"x": 480, "y": 320, "click_count": 1},
            {
                "ok": True,
                "action": "desktop.safe_click",
                "data": {"x": 480, "y": 320, "click_count": True},
            },
            False,
            id="bool-click-count-result-rejected",
        ),
        pytest.param(
            "desktop.safe_click",
            {"x": True, "y": 320, "click_count": 1},
            {
                "ok": True,
                "action": "desktop.safe_click",
                "data": {"x": 1, "y": 320, "click_count": 1},
            },
            False,
            id="bool-coordinate-request-rejected",
        ),
    ),
)
def test_exact_native_dispatch_receipts_fail_closed_on_schema_mismatch(
    tool_name: str,
    request_input: dict[str, object],
    result: dict[str, object],
    expected_match: bool,
) -> None:
    assert (
        exact_native_dispatch_receipt_matches(
            tool_name,
            request_input,
            result,
        )
        is expected_match
    )


def test_fallback_intermediate_successes_without_final_verification_do_not_complete_root() -> None:
    contract = runtime_goal_contract(
        run_id="run-media-fallback-incomplete",
        runtime_execution_envelope=_media_envelope(),
        runtime_execution_metadata=None,
        messages=[{"role": "user", "content": "Play Moonlight"}],
        timeline=[],
    )
    assert contract is not None
    plan_id = "plan-media-fallback"
    events = _runtime_owned_terminal_events(
        contract,
        [
            {
                "event": "agent.tool.call",
                "detail": "browser.search",
                "tool_call_id": "search-alias",
                "plan_id": plan_id,
                "step_id": "research-media-alias",
                "capability_id": "browser.research",
                "result": {
                    "ok": True,
                    "postcondition_verified": True,
                    "data": {
                        "query": "Moonlight alias",
                        "text": "Moonlight canonical alias",
                    },
                },
            },
            {
                "event": "agent.tool.call",
                "detail": "app.open",
                "tool_call_id": "open-player",
                "plan_id": plan_id,
                "step_id": "open-media-app",
                "capability_id": "desktop.app_control",
                "action_target": {
                    "kind": "desktop_app",
                    "action": "open",
                    "app_name": "Apple Music",
                },
                "result": {
                    "ok": True,
                    "postcondition_verified": True,
                    "data": {"app_name": "Apple Music", "app_running": True},
                },
            },
            {
                "event": "agent.tool.call",
                "detail": "desktop.safe_type_text",
                "tool_call_id": "type-alias",
                "plan_id": plan_id,
                "step_id": "type-media-query",
                "capability_id": "desktop.ui_operation",
                "action_target": {
                    "kind": "desktop_ui",
                    "action": "type_text",
                    "target": "search field",
                },
                "result": {
                    "ok": True,
                    "postcondition_verified": True,
                    "data": {"text": "Moonlight canonical alias"},
                },
            },
            {
                "event": "agent.tool.call",
                "detail": "desktop.search_submit",
                "tool_call_id": "submit-query",
                "plan_id": plan_id,
                "step_id": "submit-media-query",
                "capability_id": "desktop.ui_operation",
                "result": {
                    "ok": True,
                    "postcondition_verified": True,
                    "data": {"submitted": True},
                },
            },
        ],
    )

    assessment = runtime_goal_assessment(contract, events)

    assert assessment.completed is False
    assert assessment.unsatisfied_criterion_ids == (
        contract.criteria[0].criterion_id,
    )
