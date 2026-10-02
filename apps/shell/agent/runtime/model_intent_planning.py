"""Safe model-assisted intent planning at the Runtime authority seam.

The model may propose an intent kind, a normalized goal, and ordered semantic
subgoals.  It never proposes concrete tools, policy, approvals, identifiers,
action targets, or completion evidence.  Runtime validates user grounding,
compiles trusted adapters, and mints every authority-bearing snapshot.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, get_args

from apps.shell.agent.runtime.abstract_capability_planning import (
    ABSTRACT_CAPABILITY_MAX_INPUT_SLOTS,
    ABSTRACT_CAPABILITY_MAX_SUBGOALS,
    AbstractCapabilityInputSlotProposal,
    AbstractCapabilityPlanningError,
    AbstractCapabilityPlanProposal,
    AbstractCapabilitySubgoalProposal,
    compile_abstract_capability_plan,
)
from apps.shell.agent.runtime.goal_runtime import _explicit_pure_conversation_goal
from apps.shell.agent.runtime.tool_capabilities import capability_ids_for_tool
from apps.shell.yachiyo_agent.contracts import TaskIntentKind
from apps.shell.yachiyo_agent.entrypoint_tool_selection import DirectToolSelection
from apps.shell.yachiyo_agent.planner_execution import (
    planner_execution_tool_requests,
    planner_tool_requests_for_decision,
)
from apps.shell.yachiyo_agent.planner_projection import planner_selection_payload
from apps.shell.yachiyo_agent.runtime_planner import (
    _MODEL_INTENT_ACTION_EVIDENCE_RE,
    RuntimePlanner,
    _normalized_speech_act_text,
    _speech_act_action_occurrence_is_authorized,
)

MODEL_INTENT_PLANNING_TOOL_NAME = "runtime_propose_task_intent"
MODEL_INTENT_CONFIDENCE_MARGIN = 0.10
MODEL_INTENT_PLANNING_GOAL_MAX_CHARS = 2000
MODEL_INTENT_ACTION_EVIDENCE_MAX_CHARS = 200
MODEL_INTENT_CLARIFICATION_MAX_CHARS = 500
MODEL_INTENT_RATIONALE_MAX_CHARS = 1000

_KNOWN_INTENT_KINDS = frozenset(str(value) for value in get_args(TaskIntentKind))
_MODEL_PROPOSABLE_INTENT_KINDS = tuple(
    sorted(kind for kind in _KNOWN_INTENT_KINDS if kind != "general")
)
_PROPOSAL_FIELDS = frozenset(
    {
        "intent_kind",
        "planning_goal",
        "action_evidence",
        "subgoals",
        "clarification_question",
        "rationale",
    }
)
_ABSTRACT_SUBGOAL_FIELDS = frozenset(
    {
        "capability_id",
        "action_id",
        "planning_goal",
        "action_evidence",
        "input_slots",
    }
)
_ABSTRACT_INPUT_SLOT_FIELDS = frozenset({"slot", "value", "evidence_quote"})
_COMPOUND_ACTION_CONNECTOR_RE = re.compile(
    r"(?:[\uff0c,\uff1b;\u3002.!\uff01\uff1f?\n]+|"
    r"然后|接着|随后|而后|之后|再|顺便|以及|并且|并|同时|"
    r"(?:之后|以后|后)(?:再)?\s*$|"
    r"\b(?:and\s+then|then|and|next|afterwards|subsequently|"
    r"also|as\s+well\s+as)\b)",
    flags=re.IGNORECASE,
)


class ModelIntentPlanningError(ValueError):
    """The model proposal could not be promoted into Runtime authority."""


@dataclass(frozen=True)
class ModelIntentProposal:
    intent_kind: str
    planning_goal: str
    action_evidence: str = ""
    subgoals: tuple[AbstractCapabilitySubgoalProposal, ...] = ()
    clarification_question: str = ""
    rationale: str = ""


@dataclass(frozen=True)
class ModelIntentClarificationResolution:
    """A validated, non-executable exit from the initial planning turn."""

    original_goal: str
    question: str

    def __post_init__(self) -> None:
        original_goal = _required_text(self.original_goal, "original_goal")
        question = _required_text(self.question, "clarification_question")
        question = " ".join(question.split())
        if len(question) > MODEL_INTENT_CLARIFICATION_MAX_CHARS:
            raise ModelIntentPlanningError("model_intent_clarification_too_long")
        object.__setattr__(self, "original_goal", original_goal)
        object.__setattr__(self, "question", question)


@dataclass(frozen=True)
class ModelIntentPlanningResult:
    """Immutable summary of one validated Runtime planning promotion."""

    proposal: ModelIntentProposal
    selection: DirectToolSelection
    selected_tools: tuple[str, ...]
    clarification_required: bool = False


def model_intent_planning_tool_schema() -> dict[str, Any]:
    """Return the one pseudo-tool schema exposed during an intent planning turn."""

    return {
        "type": "function",
        "function": {
            "name": MODEL_INTENT_PLANNING_TOOL_NAME,
            "description": (
                "Propose one task intent and an explicit semantic paraphrase for "
                "Runtime validation. Preserve targets and identifiers explicitly "
                "provided by the user, but do not invent any or emit separate tool, "
                "approval, risk, action-target, or completion-evidence fields."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "intent_kind": {
                        "type": "string",
                        "enum": list(_MODEL_PROPOSABLE_INTENT_KINDS),
                        "description": "The semantic task family, not a concrete tool.",
                    },
                    "planning_goal": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MODEL_INTENT_PLANNING_GOAL_MAX_CHARS,
                        "description": (
                            "A faithful, explicit paraphrase of the user's task for "
                            "the deterministic intent router. Do not invent effects."
                        ),
                    },
                    "action_evidence": {
                        "type": "string",
                        "minLength": 0,
                        "maxLength": MODEL_INTENT_ACTION_EVIDENCE_MAX_CHARS,
                        "description": (
                            "An exact quote from the user's text that requests the "
                            "action. Never paraphrase or cite model-authored text; use "
                            "an empty string only when asking a clarification question."
                        ),
                    },
                    "subgoals": {
                        "type": "array",
                        "maxItems": ABSTRACT_CAPABILITY_MAX_SUBGOALS,
                        "description": (
                            "Optional ordered semantic actions for a compound or "
                            "otherwise unresolved goal. Runtime chooses tools, policy, "
                            "approval, identifiers, targets, and completion evidence."
                        ),
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "capability_id": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": (
                                        "An available abstract Runtime capability, never "
                                        "a tool or provider name."
                                    ),
                                },
                                "action_id": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": (
                                        "A semantic action declared by that capability; "
                                        "never a tool action or generated step id."
                                    ),
                                },
                                "planning_goal": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": MODEL_INTENT_PLANNING_GOAL_MAX_CHARS,
                                },
                                "action_evidence": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": MODEL_INTENT_ACTION_EVIDENCE_MAX_CHARS,
                                    "description": (
                                        "An exact user-authored quote requesting this action."
                                    ),
                                },
                                "input_slots": {
                                    "type": "array",
                                    "maxItems": ABSTRACT_CAPABILITY_MAX_INPUT_SLOTS,
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "slot": {
                                                "type": "string",
                                                "minLength": 1,
                                                "description": (
                                                    "A semantic slot accepted by the Runtime "
                                                    "compiler for this exact action."
                                                ),
                                            },
                                            "value": {
                                                "type": "string",
                                                "minLength": 1,
                                            },
                                            "evidence_quote": {
                                                "type": "string",
                                                "minLength": 1,
                                                "maxLength": MODEL_INTENT_CLARIFICATION_MAX_CHARS,
                                                "description": (
                                                    "An exact user-authored quote containing "
                                                    "the proposed semantic value."
                                                ),
                                            },
                                        },
                                        "required": [
                                            "slot",
                                            "value",
                                            "evidence_quote",
                                        ],
                                    },
                                },
                            },
                            "required": [
                                "capability_id",
                                "action_id",
                                "planning_goal",
                                "action_evidence",
                                "input_slots",
                            ],
                        },
                    },
                    "clarification_question": {
                        "type": "string",
                        "maxLength": MODEL_INTENT_CLARIFICATION_MAX_CHARS,
                        "description": (
                            "Optional single question when safe execution still needs "
                            "a user-provided input."
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "maxLength": MODEL_INTENT_RATIONALE_MAX_CHARS,
                        "description": "Optional non-authoritative audit rationale.",
                    },
                },
                "required": ["intent_kind", "planning_goal", "action_evidence"],
            },
        },
    }


def planner_selection_needs_model_assistance(
    selection: Any,
    original_goal: str,
    *,
    confidence_margin: float = MODEL_INTENT_CONFIDENCE_MARGIN,
) -> bool:
    """Return whether deterministic routing is too weak to authorize execution.

    A ten-point confidence margin treats genuinely competing semantic routes as
    ambiguous while preserving the fast path for one clear deterministic
    action.  Explicit response-only conversation is never promoted to an
    effectful planning turn.
    """

    clean_goal = str(original_goal or "").strip()
    if _explicit_pure_conversation_goal(clean_goal):
        return False

    decision = _selection_decision(selection)
    if decision is None:
        return True
    selected_intent = getattr(decision, "selected_intent", None)
    if selected_intent is None:
        return True
    if str(getattr(selected_intent, "kind", "") or "").strip() == "general":
        return True
    if _clean_string_list(getattr(selected_intent, "missing_inputs", [])):
        return True

    plan = getattr(decision, "plan", None)
    tool_plan = getattr(plan, "tool_plan", None)
    steps = list(getattr(tool_plan, "steps", []) or [])
    if plan is None or tool_plan is None or not steps:
        return True
    if _compound_action_clauses_underplanned(clean_goal, steps):
        return True
    selected_requests = list(getattr(selection, "requests", []) or [])
    selected_source = str(getattr(selection, "selected_source", "") or "").strip()
    if not selected_requests and selected_source in {"", "none"}:
        task_core = getattr(plan, "task_core", None)
        contract = getattr(task_core, "goal_contract", None)
        if any(
            bool(getattr(criterion, "required", False))
            and not bool(getattr(criterion, "response_satisfiable", False))
            for criterion in list(getattr(contract, "criteria", []) or [])
        ):
            # A non-response goal with no entrypoint request has not produced
            # an executable fast path.  Treat it as unresolved instead of
            # silently accepting an observation-only or underspecified plan.
            return True

    try:
        margin = max(0.0, min(1.0, float(confidence_margin)))
    except (TypeError, ValueError):
        margin = MODEL_INTENT_CONFIDENCE_MARGIN
    selected_kind = str(getattr(selected_intent, "kind", "") or "").strip()
    selected_confidence = float(getattr(selected_intent, "confidence", 0) or 0)
    for candidate in list(getattr(decision, "candidate_intents", []) or []):
        candidate_kind = str(getattr(candidate, "kind", "") or "").strip()
        candidate_goal = str(getattr(candidate, "user_goal", "") or "").strip()
        candidate_confidence = float(getattr(candidate, "confidence", 0) or 0)
        if (
            not candidate_kind
            or candidate_kind == "general"
            or candidate_kind == selected_kind
            or not candidate_goal
            or candidate_confidence <= 0
        ):
            continue
        if abs(selected_confidence - candidate_confidence) <= margin:
            return True
    return False


def _compound_action_clauses_underplanned(
    original_goal: str,
    steps: Iterable[Any],
) -> bool:
    normalized_goal = _normalized_speech_act_text(original_goal)
    matches = [
        match
        for match in _MODEL_INTENT_ACTION_EVIDENCE_RE.finditer(normalized_goal)
        if _speech_act_action_occurrence_is_authorized(
            normalized_goal,
            match.start(),
            match.end(),
        )
    ]
    connected_pairs = [
        (left, right)
        for left, right in zip(matches, matches[1:])
        if _COMPOUND_ACTION_CONNECTOR_RE.search(
            normalized_goal[left.end() : right.start()]
        )
    ]
    if not connected_pairs:
        return False
    requested = Counter(
        _semantic_action_family(match.group(0))
        for match in matches
    )
    planned = Counter(
        family
        for step in steps
        for family in _planned_step_action_families(step)
    )
    planned["search"] += _target_bound_discovery_search_coverage(
        normalized_goal,
        matches,
        steps,
    )
    return any(planned[family] < count for family, count in requested.items())


def _target_bound_discovery_search_coverage(
    normalized_goal: str,
    matches: list[Any],
    steps: Iterable[Any],
) -> int:
    """Count only discovery queries grounded in the matching search clause."""

    search_operands: list[str] = []
    for index, match in enumerate(matches):
        if _semantic_action_family(match.group(0)) != "search":
            continue
        operand_end = len(normalized_goal)
        if index + 1 < len(matches):
            next_match = matches[index + 1]
            between = normalized_goal[match.end() : next_match.start()]
            connector = _COMPOUND_ACTION_CONNECTOR_RE.search(between)
            operand_end = (
                match.end() + connector.start()
                if connector is not None
                else next_match.start()
            )
        operand = normalized_goal[match.end() : operand_end].strip()
        if operand:
            search_operands.append(operand)

    unmatched_operands = list(search_operands)
    coverage = 0
    for step in steps:
        action = str(getattr(step, "action", "") or "").strip().casefold()
        if action not in {"list_apps", "list_windows"}:
            continue
        raw_input = getattr(step, "input_preview", None)
        input_preview = raw_input if isinstance(raw_input, Mapping) else {}
        query = _normalized_speech_act_text(
            str(input_preview.get("query") or "")
        )
        if not query:
            continue
        matched_index = next(
            (
                index
                for index, operand in enumerate(unmatched_operands)
                if query in operand
            ),
            None,
        )
        if matched_index is None:
            continue
        coverage += 1
        unmatched_operands.pop(matched_index)
    return coverage


def _semantic_action_family(value: Any) -> str:
    text = str(value or "").strip().casefold()
    families = (
        ("search", ("搜索", "查找", "检索", "search", "find", "research")),
        ("extract", ("提取", "extract")),
        ("read", ("读取", "查看", "看看", "看一下", "看下", "read", "view", "inspect")),
        ("list", ("列出", "列举", "list", "enumerate")),
        ("focus", ("聚焦", "切换", "切到", "focus", "switch", "activate")),
        ("open", ("打开", "启动", "开启", "open", "launch", "start")),
        ("click", ("点击", "点开", "click")),
        ("type", ("输入", "填入", "填到", "填进", "type")),
        (
            "write",
            (
                "创建",
                "新建",
                "写",
                "写入",
                "记录",
                "记下",
                "保存",
                "生成",
                "输出",
                "起草",
                "草拟",
                "create",
                "write",
                "record",
                "save",
                "generate",
                "output",
                "draft",
                "produce",
            ),
        ),
        ("analyze", ("分析", "analyse", "analyze")),
        ("play", ("播放", "play")),
        ("send", ("发送", "发给", "发", "回复", "send", "reply")),
        ("delete", ("删除", "delete")),
        ("move", ("移动", "move")),
        ("copy", ("复制", "copy")),
        ("paste", ("粘贴", "paste")),
        ("schedule", ("安排", "提醒", "预约", "schedule", "remind", "arrange")),
        ("submit", ("提交", "确认", "submit", "confirm")),
        ("capture", ("截图", "截屏", "录屏", "capture")),
        ("scroll", ("滚动", "scroll")),
        ("download", ("下载", "download")),
        ("upload", ("上传", "upload")),
        ("uninstall", ("卸载", "uninstall")),
        ("install", ("安装", "install")),
        ("import", ("导入", "import")),
        ("export", ("导出", "export")),
    )
    for family, signals in families:
        if any(signal in text for signal in signals):
            return family
    return re.sub(r"\W+", "_", text).strip("_")


def _planned_step_action_families(step: Any) -> tuple[str, ...]:
    action = str(getattr(step, "action", "") or "").strip().casefold()
    tool_name = str(getattr(step, "tool_name", "") or "").strip().casefold()
    raw_input = getattr(step, "input_preview", None)
    input_preview = raw_input if isinstance(raw_input, Mapping) else {}

    if tool_name in {"browser.search", "browser.search_web"}:
        primary_family = "search"
    elif action in {
        "",
        "list_apps",
        "list_windows",
        "read_active_window",
        "verify",
    }:
        primary_family = ""
    elif action in {"search", "open_search", "submit_search"}:
        primary_family = "search"
    elif action in {"read", "read_file"}:
        primary_family = "read"
    elif action in {"list", "list_files"}:
        primary_family = "list"
    elif action in {"extract", "extract_text", "current_page"}:
        primary_family = "extract"
    elif action in {"focus", "focus_app"}:
        primary_family = "focus"
    elif action in {
        "open",
        "open_app",
        "open_url",
        "open_path",
        "open_path_with_app",
    }:
        primary_family = "open"
    else:
        primary_family = _semantic_action_family(action.replace("_", " "))

    families = [primary_family] if primary_family else []
    if tool_name.startswith("app.open_and_") and "open" not in families:
        # Atomic app+input tools satisfy the launch clause as well as their
        # keyboard/mouse action; otherwise a complete compound plan is
        # needlessly sent back to the model as underplanned.
        families.append("open")
    if tool_name.startswith("app.focus_and_") and "focus" not in families:
        families.append("focus")
    if "shortcut" in tool_name:
        shortcut_action = str(
            input_preview.get("shortcut_action")
            or input_preview.get("action")
            or ""
        ).strip()
        shortcut_family = _semantic_action_family(shortcut_action)
        if (
            shortcut_family
            and shortcut_family not in {"shortcut", "hotkey"}
            and shortcut_family not in families
        ):
            families.append(shortcut_family)
    if primary_family == "read" and input_preview.get("open_if_needed") is True:
        families.append("open")
    if primary_family == "analyze" and _planned_step_declares_output(input_preview):
        families.append("write")
    if (
        primary_family == "open"
        and tool_name == "desktop.open_path_with_app"
        and _planned_step_declares_app_and_path(input_preview)
    ):
        # Opening a path with a selected app satisfies both launch/open-app and
        # open-path clauses, even though Runtime deliberately compiles them to
        # one atomic provider call.
        families.append("open")
    return tuple(families)


def _planned_step_declares_output(input_preview: Mapping[str, Any]) -> bool:
    for field in ("artifact_path", "output_path"):
        value = input_preview.get(field)
        if isinstance(value, str) and value.strip():
            return True

    requested_outputs = input_preview.get("requested_outputs")
    if isinstance(requested_outputs, str):
        return bool(requested_outputs.strip())
    if not isinstance(requested_outputs, Iterable) or isinstance(
        requested_outputs,
        (bytes, bytearray, Mapping),
    ):
        return False
    return any(isinstance(item, str) and item.strip() for item in requested_outputs)


def _planned_step_declares_app_and_path(input_preview: Mapping[str, Any]) -> bool:
    app_name = input_preview.get("app_name")
    path = input_preview.get("path") or input_preview.get("target_path")
    return (
        isinstance(app_name, str)
        and bool(app_name.strip())
        and isinstance(path, str)
        and bool(path.strip())
    )


def model_intent_proposal_from_tool_requests(
    tool_requests: Any,
) -> ModelIntentProposal | None:
    """Parse exactly one pseudo-tool request into a non-authoritative proposal.

    No matching pseudo-tool means the planning turn produced no proposal.  A
    matching call mixed with other calls, repeated, malformed, or carrying any
    authority-like extra argument fails closed.
    """

    if not isinstance(tool_requests, Iterable) or isinstance(
        tool_requests,
        (str, bytes, bytearray, Mapping),
    ):
        return None
    requests = list(tool_requests)
    matching_indexes = [
        index
        for index, request in enumerate(requests)
        if _tool_request_name(request) == MODEL_INTENT_PLANNING_TOOL_NAME
    ]
    if not matching_indexes:
        return None
    if len(matching_indexes) != 1 or len(requests) != 1:
        raise ModelIntentPlanningError("model_intent_multiple_or_mixed_tool_requests")

    arguments = _tool_request_arguments(requests[matching_indexes[0]])
    extra_fields = sorted(set(arguments) - _PROPOSAL_FIELDS)
    if extra_fields:
        raise ModelIntentPlanningError(
            f"model_intent_authority_fields_forbidden:{','.join(extra_fields)}"
        )
    if "intent_kind" not in arguments or "planning_goal" not in arguments:
        raise ModelIntentPlanningError("model_intent_required_fields_missing")

    intent_kind = _required_text(arguments.get("intent_kind"), "intent_kind")
    if intent_kind not in _KNOWN_INTENT_KINDS or intent_kind == "general":
        raise ModelIntentPlanningError("model_intent_kind_unknown")
    planning_goal = _required_text(arguments.get("planning_goal"), "planning_goal")
    planning_goal = " ".join(planning_goal.split())
    if len(planning_goal) > MODEL_INTENT_PLANNING_GOAL_MAX_CHARS:
        raise ModelIntentPlanningError("model_intent_planning_goal_too_long")
    action_evidence = _optional_text(
        arguments.get("action_evidence"),
        "action_evidence",
    )
    if len(action_evidence) > MODEL_INTENT_ACTION_EVIDENCE_MAX_CHARS:
        raise ModelIntentPlanningError("model_intent_action_evidence_too_long")
    clarification = _optional_text(
        arguments.get("clarification_question"),
        "clarification_question",
    )
    rationale = _optional_text(arguments.get("rationale"), "rationale")
    if len(clarification) > MODEL_INTENT_CLARIFICATION_MAX_CHARS:
        raise ModelIntentPlanningError("model_intent_clarification_too_long")
    if len(rationale) > MODEL_INTENT_RATIONALE_MAX_CHARS:
        raise ModelIntentPlanningError("model_intent_rationale_too_long")
    subgoals = _abstract_capability_subgoals(arguments.get("subgoals"))
    return ModelIntentProposal(
        intent_kind=intent_kind,
        planning_goal=planning_goal,
        action_evidence=action_evidence,
        subgoals=subgoals,
        clarification_question=clarification,
        rationale=rationale,
    )


def _abstract_capability_subgoals(
    value: Any,
) -> tuple[AbstractCapabilitySubgoalProposal, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not value:
        raise ModelIntentPlanningError("abstract_capability_subgoals_invalid")
    if len(value) > ABSTRACT_CAPABILITY_MAX_SUBGOALS:
        raise ModelIntentPlanningError("abstract_capability_subgoals_too_many")
    subgoals: list[AbstractCapabilitySubgoalProposal] = []
    for raw_subgoal in value:
        if not isinstance(raw_subgoal, Mapping):
            raise ModelIntentPlanningError("abstract_capability_subgoal_invalid")
        fields = {str(key) for key in raw_subgoal}
        extra_fields = sorted(fields - _ABSTRACT_SUBGOAL_FIELDS)
        if extra_fields:
            raise ModelIntentPlanningError(
                "abstract_capability_authority_fields_forbidden:"
                + ",".join(extra_fields)
            )
        if fields != _ABSTRACT_SUBGOAL_FIELDS:
            raise ModelIntentPlanningError("abstract_capability_subgoal_fields_missing")
        capability_id = _required_text(
            raw_subgoal.get("capability_id"),
            "abstract_capability_id",
        )
        action_id = _required_text(
            raw_subgoal.get("action_id"),
            "abstract_action_id",
        )
        planning_goal = " ".join(
            _required_text(
                raw_subgoal.get("planning_goal"),
                "abstract_planning_goal",
            ).split()
        )
        if len(planning_goal) > MODEL_INTENT_PLANNING_GOAL_MAX_CHARS:
            raise ModelIntentPlanningError("abstract_capability_planning_goal_too_long")
        action_evidence = _required_text(
            raw_subgoal.get("action_evidence"),
            "abstract_action_evidence",
        )
        if len(action_evidence) > MODEL_INTENT_ACTION_EVIDENCE_MAX_CHARS:
            raise ModelIntentPlanningError("abstract_capability_action_evidence_too_long")
        input_slots = _abstract_capability_input_slots(
            raw_subgoal.get("input_slots")
        )
        subgoals.append(
            AbstractCapabilitySubgoalProposal(
                capability_id=capability_id,
                action_id=action_id,
                planning_goal=planning_goal,
                action_evidence=action_evidence,
                input_slots=input_slots,
            )
        )
    return tuple(subgoals)


def _abstract_capability_input_slots(
    value: Any,
) -> tuple[AbstractCapabilityInputSlotProposal, ...]:
    if not isinstance(value, (list, tuple)):
        raise ModelIntentPlanningError("abstract_capability_input_slots_invalid")
    if len(value) > ABSTRACT_CAPABILITY_MAX_INPUT_SLOTS:
        raise ModelIntentPlanningError("abstract_capability_input_slots_too_many")
    slots: list[AbstractCapabilityInputSlotProposal] = []
    for raw_slot in value:
        if not isinstance(raw_slot, Mapping):
            raise ModelIntentPlanningError("abstract_capability_input_slot_invalid")
        fields = {str(key) for key in raw_slot}
        extra_fields = sorted(fields - _ABSTRACT_INPUT_SLOT_FIELDS)
        if extra_fields:
            raise ModelIntentPlanningError(
                "abstract_capability_authority_fields_forbidden:"
                + ",".join(extra_fields)
            )
        if fields != _ABSTRACT_INPUT_SLOT_FIELDS:
            raise ModelIntentPlanningError("abstract_capability_input_slot_fields_missing")
        slot = _required_text(raw_slot.get("slot"), "abstract_input_slot")
        semantic_value = _required_preserved_text(
            raw_slot.get("value"),
            "abstract_input_value",
        )
        evidence_quote = _required_preserved_text(
            raw_slot.get("evidence_quote"),
            "abstract_input_evidence_quote",
        )
        if max(len(semantic_value), len(evidence_quote)) > MODEL_INTENT_CLARIFICATION_MAX_CHARS:
            raise ModelIntentPlanningError("abstract_capability_input_slot_too_long")
        slots.append(
            AbstractCapabilityInputSlotProposal(
                slot=slot,
                value=semantic_value,
                evidence_quote=evidence_quote,
            )
        )
    return tuple(slots)


def direct_tool_selection_from_model_intent_proposal(
    proposal: ModelIntentProposal,
    original_goal: str,
    allowed_tools: Iterable[str],
    *,
    metadata: Mapping[str, Any] | None = None,
    planner: RuntimePlanner | None = None,
) -> DirectToolSelection:
    """Validate a proposal and return a RuntimePlanner-owned tool selection."""

    return _model_intent_planning_result_from_proposal(
        proposal,
        original_goal,
        allowed_tools,
        metadata=metadata,
        planner=planner,
    ).selection


def direct_tool_selection_from_user_clarification(
    original_goal: str,
    allowed_tools: Iterable[str],
    *,
    metadata: Mapping[str, Any] | None = None,
    planner: RuntimePlanner | None = None,
) -> DirectToolSelection | None:
    """Promote a user clarification without requiring another model call.

    The RuntimePlanner accepts this fast path only when the concatenated
    user-authored text preserves the previous action and deterministically
    fills declared non-action slots.  Ambiguous text returns ``None`` so the
    caller may request bounded model assistance instead.
    """

    allowed = tuple(
        dict.fromkeys(
            tool
            for value in allowed_tools
            if (tool := str(value or "").strip())
        )
    )
    if not allowed:
        raise ModelIntentPlanningError("model_intent_allowed_tools_empty")
    runtime_planner = planner or RuntimePlanner()
    try:
        decision = runtime_planner.decision_from_user_clarification(
            original_goal,
            allowed,
            metadata,
        )
    except (TypeError, ValueError) as exc:
        reason = str(exc)
        if reason.startswith(
            (
                "clarification_authority_",
                "user_clarification_authority_",
            )
        ):
            raise ModelIntentPlanningError(
                "model_intent_clarification_authority_rejected"
            ) from exc
        return None

    if _clean_string_list(decision.selected_intent.missing_inputs):
        return None
    try:
        _validate_effectful_plan(decision)
        raw_requests = planner_tool_requests_for_decision(
            decision,
            allowed,
            direct=False,
            execution_normalized=False,
            metadata=metadata,
        )
        _validate_planner_requests(
            raw_requests,
            decision=decision,
            allowed_tools=allowed,
            require_plan_step=True,
        )
        selected_requests = planner_execution_tool_requests(raw_requests, allowed)
        if not selected_requests:
            return None
        _validate_planner_requests(
            selected_requests,
            decision=decision,
            allowed_tools=allowed,
            require_plan_step=False,
        )
    except ModelIntentPlanningError:
        return None

    event_payload = planner_selection_payload(
        decision=decision,
        planner_requests=raw_requests,
        legacy_requests=[],
        selected_requests=selected_requests,
        selected_source="runtime_planner",
        selected_reason="user_clarification_continuation",
        metadata=metadata,
    )
    event_payload["clarification_continuation"] = {
        "source": "runtime_validated_user_clarification",
    }
    selection = DirectToolSelection(
        decision=decision,
        requests=[dict(request) for request in selected_requests],
        event_payload=event_payload,
        selected_source="runtime_planner",
    )
    goal_contract_payload_from_model_selection(selection, original_goal)
    return selection


def model_intent_resolution_from_proposal(
    proposal: ModelIntentProposal,
    original_goal: str,
    allowed_tools: Iterable[str],
    *,
    metadata: Mapping[str, Any] | None = None,
    planner: RuntimePlanner | None = None,
) -> DirectToolSelection | ModelIntentClarificationResolution:
    """Promote a semantic proposal without turning clarification into a plan.

    A clarification is deliberately resolved before RuntimePlanner invocation,
    so it cannot mint a GoalContract or carry executable requests by accident.
    """

    if not isinstance(proposal, ModelIntentProposal):
        raise ModelIntentPlanningError("model_intent_proposal_invalid")
    if proposal.clarification_question != "":
        return ModelIntentClarificationResolution(
            original_goal=original_goal,
            question=proposal.clarification_question,
        )
    return direct_tool_selection_from_model_intent_proposal(
        proposal,
        original_goal,
        allowed_tools,
        metadata=metadata,
        planner=planner,
    )


def goal_contract_payload_from_model_selection(
    selection: DirectToolSelection,
    original_goal: str,
) -> dict[str, Any]:
    """Return the contract only when every authority layer names one root goal."""

    immutable_goal = str(original_goal or "").strip()
    decision = _selection_decision(selection)
    selected_intent = getattr(decision, "selected_intent", None)
    plan = getattr(decision, "plan", None)
    plan_intent = getattr(plan, "intent", None)
    task_core = getattr(plan, "task_core", None)
    goal_contract = getattr(task_core, "goal_contract", None)
    authority_goals = (
        str(getattr(decision, "prompt", "") or "").strip(),
        str(getattr(selected_intent, "user_goal", "") or "").strip(),
        str(getattr(plan_intent, "user_goal", "") or "").strip(),
        str(getattr(goal_contract, "original_goal", "") or "").strip(),
    )
    if (
        not immutable_goal
        or decision is None
        or selected_intent is None
        or plan is None
        or plan_intent is None
        or goal_contract is None
        or any(value != immutable_goal for value in authority_goals)
        or str(getattr(decision, "source", "") or "").strip() != "runtime_planner"
        or str(getattr(selection, "selected_source", "") or "").strip()
        != "runtime_planner"
        or str(getattr(selected_intent, "intent_id", "") or "").strip()
        != str(getattr(plan_intent, "intent_id", "") or "").strip()
        or str(getattr(goal_contract, "intent_kind", "") or "").strip()
        != str(getattr(selected_intent, "kind", "") or "").strip()
    ):
        raise ModelIntentPlanningError("model_intent_goal_authority_conflict")
    payload = goal_contract.model_dump(mode="json")
    if not isinstance(payload, dict):
        raise ModelIntentPlanningError("model_intent_goal_contract_invalid")
    return payload


def _model_intent_planning_result_from_proposal(
    proposal: ModelIntentProposal,
    original_goal: str,
    allowed_tools: Iterable[str],
    *,
    metadata: Mapping[str, Any] | None,
    planner: RuntimePlanner | None,
) -> ModelIntentPlanningResult:
    if not isinstance(proposal, ModelIntentProposal):
        raise ModelIntentPlanningError("model_intent_proposal_invalid")
    allowed = tuple(
        dict.fromkeys(
            tool
            for value in allowed_tools
            if (tool := str(value or "").strip())
        )
    )
    if not allowed:
        raise ModelIntentPlanningError("model_intent_allowed_tools_empty")
    runtime_planner = planner or RuntimePlanner()
    abstract_capability_plan = bool(proposal.subgoals)
    try:
        if abstract_capability_plan:
            decision = compile_abstract_capability_plan(
                AbstractCapabilityPlanProposal(
                    intent_kind=proposal.intent_kind,
                    planning_goal=proposal.planning_goal,
                    subgoals=proposal.subgoals,
                ),
                original_goal,
                allowed,
                metadata=metadata,
                planner=runtime_planner,
            )
        else:
            decision = runtime_planner.decision_from_model_intent_hint(
                original_goal,
                proposal.planning_goal,
                proposal.intent_kind,
                allowed,
                metadata,
                action_evidence=proposal.action_evidence,
            )
    except (AbstractCapabilityPlanningError, TypeError, ValueError) as exc:
        raise ModelIntentPlanningError("model_intent_hint_rejected") from exc

    if not abstract_capability_plan:
        plan = getattr(decision, "plan", None)
        tool_plan = getattr(plan, "tool_plan", None)
        steps = list(getattr(tool_plan, "steps", []) or [])
        if _compound_action_clauses_underplanned(original_goal, steps):
            # A model paraphrase may disambiguate one simple deterministic
            # route, but it may not erase another action from the immutable
            # user goal.  Compound repairs cross the semantic-subgoal seam so
            # Runtime compiles and verifies every requested action atomically.
            raise ModelIntentPlanningError(
                "model_intent_abstract_subgoals_required"
            )

    # A DirectToolSelection can be promoted into a full-plan execution
    # envelope by main chat.  A clarification therefore must remain a
    # non-executable proposal handled by the caller, never an empty selection
    # whose decision still carries effectful plan steps.
    if proposal.clarification_question:
        raise ModelIntentPlanningError("model_intent_clarification_requires_user_turn")

    selected_intent = decision.selected_intent
    if _clean_string_list(selected_intent.missing_inputs):
        raise ModelIntentPlanningError("model_intent_missing_inputs_require_clarification")
    _validate_effectful_plan(decision)
    raw_requests = (
        _abstract_capability_planner_requests_for_decision(decision, allowed)
        if abstract_capability_plan
        else planner_tool_requests_for_decision(
            decision,
            allowed,
            direct=False,
            execution_normalized=False,
            metadata=metadata,
        )
    )
    _validate_planner_requests(
        raw_requests,
        decision=decision,
        allowed_tools=allowed,
        require_plan_step=True,
    )
    selected_requests = planner_execution_tool_requests(raw_requests, allowed)
    if not selected_requests:
        raise ModelIntentPlanningError("model_intent_execution_normalization_empty")
    _validate_planner_requests(
        selected_requests,
        decision=decision,
        allowed_tools=allowed,
        require_plan_step=False,
    )
    selection = _selection_for_model_intent(
        proposal,
        decision,
        planner_requests=raw_requests,
        selected_requests=selected_requests,
        metadata=metadata,
        selected_reason=(
            "model_assisted_abstract_capability_plan"
            if abstract_capability_plan
            else "model_assisted_intent"
        ),
    )
    goal_contract_payload_from_model_selection(selection, original_goal)
    selected_tools = tuple(
        dict.fromkeys(
            str(request.get("tool") or "").strip()
            for request in selected_requests
            if str(request.get("tool") or "").strip()
        )
    )
    return ModelIntentPlanningResult(
        proposal=proposal,
        selection=selection,
        selected_tools=selected_tools,
    )


def _selection_for_model_intent(
    proposal: ModelIntentProposal,
    decision: Any,
    *,
    planner_requests: list[dict[str, Any]],
    selected_requests: list[dict[str, Any]],
    metadata: Mapping[str, Any] | None,
    selected_reason: str,
) -> DirectToolSelection:
    event_payload = planner_selection_payload(
        decision=decision,
        planner_requests=planner_requests,
        legacy_requests=[],
        selected_requests=selected_requests,
        selected_source="runtime_planner",
        selected_reason=selected_reason,
        metadata=metadata,
    )
    planning_audit: dict[str, Any] = {
        "planning_goal": proposal.planning_goal,
        "intent_kind": proposal.intent_kind,
        "action_evidence": proposal.action_evidence,
        "clarification_question": proposal.clarification_question,
        "rationale": proposal.rationale,
        "source": "runtime_validated_model_intent_hint",
    }
    if proposal.subgoals:
        planning_audit["subgoals"] = [
            {
                "capability_id": subgoal.capability_id,
                "action_id": subgoal.action_id,
                "planning_goal": subgoal.planning_goal,
                "action_evidence": subgoal.action_evidence,
                "input_slots": [
                    {
                        "slot": input_slot.slot,
                        "value": input_slot.value,
                        "evidence_quote": input_slot.evidence_quote,
                    }
                    for input_slot in subgoal.input_slots
                ],
            }
            for subgoal in proposal.subgoals
        ]
        planning_audit["source"] = "runtime_validated_abstract_capability_plan"
    event_payload["model_intent_planning"] = planning_audit
    return DirectToolSelection(
        decision=decision,
        requests=[dict(request) for request in selected_requests],
        event_payload=event_payload,
        selected_source="runtime_planner",
    )


def _abstract_capability_planner_requests_for_decision(
    decision: Any,
    allowed_tools: Iterable[str],
) -> list[dict[str, Any]]:
    """Project every Runtime-compiled step without re-routing by outer intent."""

    allowed = {
        str(tool or "").strip()
        for tool in allowed_tools
        if str(tool or "").strip()
    }
    plan = getattr(decision, "plan", None)
    tool_plan = getattr(plan, "tool_plan", None)
    requests: list[dict[str, Any]] = []
    for step in list(getattr(tool_plan, "steps", []) or []):
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        status = str(getattr(step, "status", "") or "planned").strip()
        if not tool_name or tool_name not in allowed or status != "planned":
            continue
        step_id = str(getattr(step, "step_id", "") or "").strip()
        capability_id = str(
            getattr(step, "capability_id", "") or ""
        ).strip()
        raw_input = getattr(step, "input_preview", None)
        request: dict[str, Any] = {
            "protocol": "json_fallback",
            "tool": tool_name,
            "input": dict(raw_input) if isinstance(raw_input, Mapping) else {},
            "source": "runtime_planner",
            "planning_reason": "model_assisted_abstract_capability_plan",
            "approval_required": bool(
                getattr(step, "approval_required", False)
            ),
            "status": status,
        }
        if step_id:
            request["step_id"] = step_id
            request["planner_step_id"] = step_id
        if capability_id:
            request["capability_id"] = capability_id
        depends_on = _clean_string_list(getattr(step, "depends_on", []))
        if depends_on:
            request["depends_on"] = depends_on
        requests.append(request)
    return requests


def _validate_effectful_plan(decision: Any) -> None:
    plan = getattr(decision, "plan", None)
    tool_plan = getattr(plan, "tool_plan", None)
    task_core = getattr(plan, "task_core", None)
    goal_contract = getattr(task_core, "goal_contract", None)
    steps = list(getattr(tool_plan, "steps", []) or [])
    criteria = list(getattr(goal_contract, "criteria", []) or [])
    effectful_criteria = [
        criterion for criterion in criteria if bool(getattr(criterion, "effectful", False))
    ]
    if not steps or not criteria:
        raise ModelIntentPlanningError("model_intent_plan_missing")
    if _clean_string_list(getattr(tool_plan, "missing_capabilities", [])):
        raise ModelIntentPlanningError("model_intent_plan_capability_unavailable")
    if any(str(getattr(step, "status", "") or "").strip() == "unavailable" for step in steps):
        raise ModelIntentPlanningError("model_intent_plan_step_unavailable")
    step_by_id = {
        str(getattr(step, "step_id", "") or "").strip(): step
        for step in steps
        if str(getattr(step, "step_id", "") or "").strip()
    }
    if effectful_criteria:
        effectful_source_ids = {
            source_step_id
            for criterion in effectful_criteria
            for source_step_id in _clean_string_list(
                getattr(criterion, "source_step_ids", [])
            )
        }
        if not effectful_source_ids:
            raise ModelIntentPlanningError("model_intent_effectful_source_steps_missing")
        for source_step_id in effectful_source_ids:
            step = step_by_id.get(source_step_id)
            if (
                step is None
                or str(getattr(step, "status", "") or "").strip() != "planned"
                or not str(getattr(step, "tool_name", "") or "").strip()
            ):
                raise ModelIntentPlanningError("model_intent_effectful_source_step_invalid")
    elif not any(
        str(getattr(step, "status", "") or "").strip() == "planned"
        and str(getattr(step, "tool_name", "") or "").strip()
        for step in steps
    ):
        raise ModelIntentPlanningError("model_intent_readonly_plan_step_missing")


def _validate_planner_requests(
    requests: Any,
    *,
    decision: Any,
    allowed_tools: Iterable[str],
    require_plan_step: bool,
) -> None:
    if not isinstance(requests, list) or not requests:
        raise ModelIntentPlanningError("model_intent_tool_requests_empty")
    allowed = {str(tool or "").strip() for tool in allowed_tools if str(tool or "").strip()}
    plan = getattr(decision, "plan", None)
    tool_plan = getattr(plan, "tool_plan", None)
    plan_step_ids = {
        str(getattr(step, "step_id", "") or "").strip()
        for step in list(getattr(tool_plan, "steps", []) or [])
        if str(getattr(step, "step_id", "") or "").strip()
    }
    plan_step_capabilities = {
        str(getattr(step, "step_id", "") or "").strip(): str(
            getattr(step, "capability_id", "") or ""
        ).strip()
        for step in list(getattr(tool_plan, "steps", []) or [])
        if str(getattr(step, "step_id", "") or "").strip()
    }
    for request in requests:
        if not isinstance(request, Mapping):
            raise ModelIntentPlanningError("model_intent_tool_request_malformed")
        tool_name = str(request.get("tool") or "").strip()
        if not tool_name or tool_name not in allowed:
            raise ModelIntentPlanningError("model_intent_tool_outside_allowlist")
        capability_id = str(request.get("capability_id") or "").strip()
        trusted_capabilities = capability_ids_for_tool(tool_name)
        if not trusted_capabilities:
            raise ModelIntentPlanningError("model_intent_tool_capability_mismatch")
        if capability_id and capability_id not in trusted_capabilities:
            raise ModelIntentPlanningError("model_intent_tool_capability_mismatch")
        if require_plan_step and not capability_id:
            raise ModelIntentPlanningError("model_intent_tool_capability_missing")
        step_id = str(
            request.get("step_id") or request.get("planner_step_id") or ""
        ).strip()
        if require_plan_step and (not step_id or step_id not in plan_step_ids):
            raise ModelIntentPlanningError("model_intent_tool_request_unplanned")
        if require_plan_step and capability_id != plan_step_capabilities.get(step_id, ""):
            raise ModelIntentPlanningError("model_intent_plan_capability_mismatch")


def _selection_decision(selection: Any) -> Any | None:
    decision = getattr(selection, "decision", None)
    if decision is not None:
        return decision
    if getattr(selection, "selected_intent", None) is not None:
        return selection
    return None


def _tool_request_name(request: Any) -> str:
    if not isinstance(request, Mapping):
        return ""
    direct_name = request.get("tool") or request.get("name")
    if direct_name:
        return str(direct_name).strip()
    function = request.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name") or "").strip()
    return ""


def _tool_request_arguments(request: Any) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ModelIntentPlanningError("model_intent_tool_request_malformed")
    if request.get("tool") or request.get("name"):
        raw_arguments = request.get("input", request.get("arguments"))
    else:
        function = request.get("function")
        if not isinstance(function, Mapping):
            raise ModelIntentPlanningError("model_intent_tool_request_malformed")
        raw_arguments = function.get("arguments")
    if isinstance(raw_arguments, str):
        try:
            raw_arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ModelIntentPlanningError("model_intent_arguments_invalid_json") from exc
    if not isinstance(raw_arguments, Mapping):
        raise ModelIntentPlanningError("model_intent_arguments_not_object")
    return {str(key): value for key, value in raw_arguments.items()}


def _required_preserved_text(value: Any, field_name: str) -> str:
    # Grounded payload bytes (patches, typed text, command arguments) are
    # executable inputs. Trimming them would change the authorized action.
    _required_text(value, field_name)
    return value


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ModelIntentPlanningError(f"model_intent_{field_name}_not_text")
    clean = value.strip()
    if not clean:
        raise ModelIntentPlanningError(f"model_intent_{field_name}_blank")
    return clean


def _optional_text(value: Any, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ModelIntentPlanningError(f"model_intent_{field_name}_not_text")
    return value.strip()


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, Iterable) or isinstance(
        value,
        (str, bytes, bytearray, Mapping),
    ):
        return []
    return [
        clean
        for item in value
        if (clean := str(item or "").strip())
    ]


__all__ = [
    "MODEL_INTENT_PLANNING_TOOL_NAME",
    "ModelIntentClarificationResolution",
    "ModelIntentPlanningError",
    "ModelIntentPlanningResult",
    "ModelIntentProposal",
    "direct_tool_selection_from_model_intent_proposal",
    "goal_contract_payload_from_model_selection",
    "model_intent_planning_tool_schema",
    "model_intent_proposal_from_tool_requests",
    "model_intent_resolution_from_proposal",
    "planner_selection_needs_model_assistance",
]
