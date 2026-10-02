"""Task execution strategies.

TaskRunner owns the product Task lifecycle. ExecutionStrategy implementations
only decide how a Task is executed; the default product path is Native Agent.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Optional

from apps.core.special_sessions import is_proactive_chat_session
from apps.core.title_generator import (
    build_session_title_prompt as build_direct_session_title_prompt,
)
from apps.core.title_generator import (
    generate_title_with_direct_api,
    looks_like_title_prompt_echo,
)
from apps.shell.agent.runtime.callbacks import supports_keyword
from apps.shell.agent.runtime.outcome_evaluator import (
    MainChatOutcomeEvaluation,
    evaluate_main_chat_outcome,
)
from packages.protocol.schemas import TaskInfo
from packages.security import redact_api_error_text

if TYPE_CHECKING:
    from apps.core.activity_store import ActivityStore
    from apps.core.chat_session import ChatSession
    from apps.core.runtime import AppRuntime

logger = logging.getLogger(__name__)

NATIVE_AGENT_NOT_READY_MESSAGE = "Native Agent 当前未就绪，请先配置并选择默认对话模型。"
_DAILY_DESKTOP_FALLBACK_TOOL_PREFIXES = (
    "app.",
    "desktop.",
    "media.",
    "system.",
    "clipboard.",
    "reminders.",
    "calendar.",
    "notes.",
)


class NativeAgentError(RuntimeError):
    """Native Agent execution failure with a stable machine-readable reason."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "native_agent_error",
        reason: str = "execution_failed",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason

    def to_error_string(self) -> str:
        return str(self)


class _AwaitingUserReply(str):
    """Marks a successful interaction turn that must not complete its Native Run."""


@dataclass(frozen=True)
class _ClarificationContinuation:
    """User-authored authority carried into a new Main Chat Run."""

    user_goal: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class DelegationDirective:
    """Structured internal contract for Native Agent delegation."""

    kind: str
    name: str = ""
    runnable_id: str = ""
    goal: str = ""

    @property
    def target_label(self) -> str:
        return self.name or self.runnable_id or "Yachiyo Agent"

    def as_request(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "name": self.name,
            "runnable_id": self.runnable_id,
            "goal": self.goal,
        }


_TITLE_GENERATION_ENABLED_ENV = "OHA_YACHIYO_TITLE_GENERATION"
_TITLE_CONTEXT_MESSAGES_ENV = "OHA_YACHIYO_TITLE_CONTEXT_MESSAGES"
_TITLE_INTERVAL_TURNS_ENV = "OHA_YACHIYO_TITLE_INTERVAL_TURNS"
_TITLE_INTERVAL_MESSAGES_ENV = "OHA_YACHIYO_TITLE_INTERVAL_MESSAGES"
_TITLE_TIMEOUT_ENV = "OHA_YACHIYO_TITLE_TIMEOUT_SECONDS"
_DEFAULT_TITLE_CONTEXT_MESSAGES = 8
_DEFAULT_TITLE_INTERVAL_TURNS = 4
_DEFAULT_TITLE_TIMEOUT_SECONDS = 8.0
_GENERATED_TITLE_MAX_CHARS = 28
_TITLE_REFRESH_TASKS: dict[str, asyncio.Task[None]] = {}
_MEMORY_CONTEXT_MAX_ITEMS = 8
_MEMORY_CONTEXT_MAX_SESSIONS = 200
_MEMORY_CONTEXT_ITEM_MAX_CHARS = 180
_MEMORY_CONTEXT_TOTAL_MAX_CHARS = 1400
_MEMORY_MARKERS = (
    "记住",
    "记忆",
    "长期",
    "以后",
    "今后",
    "偏好",
    "我喜欢",
    "我不喜欢",
    "不要",
    "别",
    "必须",
    "先问",
    "许可",
    "允许",
    "未经",
    "擅自",
    "always",
    "never",
    "remember",
    "preference",
    "permission",
)


_WEEKDAY_NAMES = (
    "星期一",
    "星期二",
    "星期三",
    "星期四",
    "星期五",
    "星期六",
    "星期日",
)

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MAIN_CHAT_HISTORY_MESSAGE_LIMIT = 20
_MAIN_CHAT_HISTORY_CHAR_LIMIT = 32_000
_MAIN_CHAT_IMAGE_ATTACHMENT_LIMIT = 4
_MAIN_CHAT_IMAGE_MAX_BYTES = 12 * 1024 * 1024
_OUTCOME_EVENT_PAGE_LIMIT = 1000
_OUTCOME_EVENT_MAX_PAGES = 10
_OUTCOME_EVENT_MAX_EVENTS = _OUTCOME_EVENT_PAGE_LIMIT * _OUTCOME_EVENT_MAX_PAGES


def _incomplete_outcome_event_history() -> MainChatOutcomeEvaluation:
    return MainChatOutcomeEvaluation(
        kind="failed",
        reason="outcome_event_history_incomplete",
        message="无法确认任务结果：执行事件记录不完整，请重试。",
    )


def format_persona_description(
    description: str,
    persona_prompt: str = "",
    user_address: str = "",
    environment_context: str = "",
    profile_context: str = "",
) -> str:
    """按共享助手资料包装用户请求，资料为空时保持原始描述。"""
    persona = (persona_prompt or "").strip()
    address = (user_address or "").strip()
    environment = (environment_context or "").strip()
    profile = (profile_context or "").strip()
    if not environment and not persona and not address and not profile:
        return description
    parts: list[str] = []
    if environment:
        parts.append(environment)
    if profile:
        parts.append(profile)
    if persona:
        parts.append(f"[人设设定]\n{persona}")
    if address:
        parts.append(f"[用户称呼]\n请称呼用户为：{address}")
    parts.append(f"[用户请求]\n{description}")
    return "\n\n".join(parts)


def _oha_delegation_catalog_context(service: Any | None = None) -> str:
    try:
        if service is None:
            from apps.shell.agent_runtime import get_agent_runtime_service

            service = get_agent_runtime_service()
        targets = service.list_delegation_targets()
    except Exception:
        logger.debug("读取 OHA 委派目标失败", exc_info=True)
        return ""
    entries: list[str] = []
    for agent in targets.get("agents") or []:
        entries.append(
            "- Agent: "
            f"{agent.get('name')} | category={agent.get('category') or 'custom'} | "
            f"output={agent.get('output_contract') or 'chat'} | "
            f"description={agent.get('description') or ''}"
        )
    for workflow in targets.get("workflows") or []:
        entries.append(
            "- Workflow: "
            f"{workflow.get('name')} | nodes={workflow.get('nodes') or 0} | "
            f"description={workflow.get('description') or ''}"
        )
    if not entries:
        return ""
    return (
        "[Oha-Yachiyo 持久 Agent 委派]\n"
        "你可以把明确的子任务委派给 Agent Studio 中已保存的持久 Agent 或 Workflow。"
        "这些不是一次性临时 delegate_task 子 Agent，而是有固定职责、Skills、模型和审计记录的岗位。\n"
        "当你需要委派时，只输出一个 JSON 对象，不要附加其他正文：\n"
        '{"action":"run_oha_agent","agent":"Agent 名称","goal":"自包含任务目标"}\n'
        "或：\n"
        '{"action":"run_oha_workflow","workflow":"Workflow 名称","goal":"自包含任务目标"}\n'
        "委派结果会返回给你，然后你再继续整合最终回复。每轮最多委派 3 次。\n"
        "可用目标：\n"
        + "\n".join(entries[:40])
    )


_GROUP_CONTEXT_MARKERS = ("[Oha-Yachiyo 群组上下文]", "[Yachiyo 群组上下文]")


def _is_oha_yachiyo_group_coordinator_task(description: str) -> bool:
    return any(marker in (description or "") for marker in _GROUP_CONTEXT_MARKERS)


def _oha_group_dispatch_context() -> str:
    return (
        "[Oha-Yachiyo 群组派活]\n"
        "当前会话是群组。你是默认协调者，不要默认让所有 Agent 参与。"
        "如果用户没有明确 @ 某个 Agent，请先判断应该由你直接回答，还是派给群组上下文里列出的一个或多个 Agent。"
        "只有当用户表达“大家/所有人/分别做”等意图时，才派给多个 Agent。\n"
        "派活不是终端命令，也不是工具调用；不要调用 shell/terminal 来 echo、模拟或包装派活指令。\n"
        "如果需要派活，请先用自然语言向用户说明你的安排，再附加一个机器可读 native 派活 JSON，格式如下：\n"
        '{"tool":"oha.group_dispatch","input":{"tasks":[{"kind":"agent","target":"Agent 名称或昵称","goal":"自包含任务目标"}]}}\n'
        "可以一次派给多个 Agent，但每个 goal 都要独立完整，不能用“同上”“继续”等省略说法。\n"
        "不要使用旧委派 action 或旧标签；不要把 JSON 包进 Markdown 代码块。"
        "群组派活会由聊天层接管、隐藏内部 JSON，并显示各 Agent 的执行状态。"
    )


def _append_oha_delegation_context(
    profile_context: str,
    *,
    group_coordinator: bool = False,
    runtime_service: Any | None = None,
) -> str:
    catalog = (
        _oha_group_dispatch_context()
        if group_coordinator
        else _oha_delegation_catalog_context(runtime_service)
    )
    if not catalog:
        return profile_context
    base = (profile_context or "").strip()
    return f"{base}\n\n{catalog}" if base else catalog


def _payload_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    normalized_keys = {
        re.sub(r"[\s_-]+", "", str(key or "")).lower()
        for key in keys
        if str(key or "").strip()
    }
    for raw_key, value in payload.items():
        if value in (None, ""):
            continue
        normalized = re.sub(r"[\s_-]+", "", str(raw_key or "")).lower()
        if normalized in normalized_keys:
            return value
    return None


def _json_candidate_texts(text: str) -> list[str]:
    candidates = [text]
    normalized = (
        text.replace("“", '"')
        .replace("”", '"')
        .replace("„", '"')
        .replace("＂", '"')
    )
    if normalized != text:
        candidates.append(normalized)
    return candidates


def _json_objects_from_text(text: str) -> list[dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return []
    text = re.sub(
        r"<\s*(?!oha[\s_-]*delegation\b)[a-z][\w\s_-]*delegation\b[^>]*>.*?</\s*[a-z][\w\s_-]*delegation\s*>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()
    match = re.search(r"<oha_delegation>\s*(.*?)\s*</oha_delegation>", text, re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()

    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in _json_candidate_texts(text):
        candidate = candidate.strip()
        decoder = json.JSONDecoder()
        index = 0
        while index < len(candidate):
            if candidate[index] != "{":
                index += 1
                continue
            try:
                payload, offset = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                index += 1
                continue
            if isinstance(payload, dict):
                key = json.dumps(payload, sort_keys=True, ensure_ascii=False)
                if key not in seen:
                    payloads.append(payload)
                    seen.add(key)
            index += max(offset, 1)
    return payloads


def _normalize_oha_delegation_action(value: str) -> str:
    compact = re.sub(r"[\s_\-./]+", "", str(value or "").strip().lower())
    if compact in {
        "agent",
        "agentrun",
        "runagent",
        "delegateagent",
        "delegatetoagent",
        "assignagent",
        "runohaagent",
        "ohaagent",
        "runnativeagent",
        "nativeagent",
    }:
        return "agent"
    if compact in {
        "workflow",
        "workflowrun",
        "runworkflow",
        "delegateworkflow",
        "delegatetoworkflow",
        "assignworkflow",
        "runohaworkflow",
        "ohaworkflow",
        "runnativeworkflow",
        "nativeworkflow",
    }:
        return "workflow"
    return ""


def _parse_oha_delegation_directive(content: str) -> DelegationDirective | None:
    for payload in _json_objects_from_text(content):
        directive = _oha_delegation_directive_from_payload(payload)
        if directive:
            return directive
    return None


def _parse_oha_delegation_request(content: str) -> dict[str, str] | None:
    directive = _parse_oha_delegation_directive(content)
    return directive.as_request() if directive else None


def _oha_delegation_directive_from_payload(payload: dict[str, Any]) -> DelegationDirective | None:
    action = _normalize_oha_delegation_action(str(
        _payload_value(payload, "action", "tool", "kind", "type", "target_kind", "runnable_kind")
        or ""
    ))
    if action not in {"agent", "workflow"}:
        return None
    goal = str(
        _payload_value(
            payload,
            "goal",
            "user_goal",
            "userGoal",
            "task",
            "task_goal",
            "taskGoal",
            "objective",
            "instruction",
            "instructions",
            "prompt",
        )
        or ""
    ).strip()
    if not goal:
        return None
    if action == "agent":
        name = str(
            _payload_value(
                payload,
                "agent",
                "agent_name",
                "agentName",
                "name",
                "target",
                "target_name",
                "targetName",
                "runnable",
                "runnable_name",
                "runnableName",
            )
            or ""
        ).strip()
        runnable_id = str(_payload_value(payload, "agent_id", "agentId", "runnable_id", "runnableId", "id") or "").strip()
        kind = "agent"
    else:
        name = str(
            _payload_value(
                payload,
                "workflow",
                "workflow_name",
                "workflowName",
                "name",
                "target",
                "target_name",
                "targetName",
                "runnable",
                "runnable_name",
                "runnableName",
            )
            or ""
        ).strip()
        runnable_id = str(_payload_value(payload, "workflow_id", "workflowId", "runnable_id", "runnableId", "id") or "").strip()
        kind = "workflow"
    if not name and not runnable_id:
        return None
    return DelegationDirective(kind=kind, name=name, runnable_id=runnable_id, goal=goal)


def _coerce_oha_delegation_directive(value: DelegationDirective | dict[str, str]) -> DelegationDirective:
    if isinstance(value, DelegationDirective):
        return value
    return DelegationDirective(
        kind=str(value.get("kind") or ""),
        name=str(value.get("name") or ""),
        runnable_id=str(value.get("runnable_id") or ""),
        goal=str(value.get("goal") or ""),
    )


def _run_oha_delegation(request: DelegationDirective | dict[str, str], service: Any | None = None) -> dict[str, Any]:
    directive = _coerce_oha_delegation_directive(request)
    if service is None:
        from apps.shell.agent_runtime import get_agent_runtime_service

        service = get_agent_runtime_service()
    return service.delegate_runnable(
        kind=directive.kind,
        name=directive.name,
        runnable_id=directive.runnable_id,
        user_goal=directive.goal,
    )


def _format_oha_delegation_result(result: dict[str, Any]) -> str:
    runnable = result.get("runnable") or {}
    lines = [
        f"Runnable: {runnable.get('kind') or ''} {runnable.get('name') or runnable.get('id') or ''}",
        f"Run: {result.get('run_id') or ''}",
        f"Status: {result.get('status') or ''}",
    ]
    pending = result.get("pending_approval") if isinstance(result.get("pending_approval"), dict) else {}
    if pending:
        lines.append("Pending approval:")
        tool = str(pending.get("tool") or "").strip()
        if tool:
            lines.append(f"- Tool: {tool}")
        preview = pending.get("input_preview")
        if preview:
            try:
                preview_text = json.dumps(preview, ensure_ascii=False, indent=2)
            except TypeError:
                preview_text = str(preview)
            lines.append(f"- Request:\n{preview_text[:2000]}")
    lines.append(f"Result:\n{result.get('result') or ''}")
    return "\n".join(lines).strip()


def _oha_delegation_activity_status(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "").strip()
    if status == "approval_required":
        return "approval_required"
    return "completed" if result.get("ok") else "failed"


def _oha_delegation_activity_title(target: str, result: dict[str, Any]) -> str:
    status = str(result.get("status") or "").strip()
    if status == "approval_required":
        return f"{target} 等待审批"
    return f"{target} 委派完成" if result.get("ok") else f"{target} 委派失败"


def _build_oha_delegation_followup(
    original_prompt: str,
    delegation_request_text: str,
    delegation_result: str,
) -> str:
    return (
        f"{original_prompt}\n\n"
        "[OHA 委派请求]\n"
        f"{delegation_request_text.strip()[:4000]}\n\n"
        "[OHA 委派结果]\n"
        f"{delegation_result[:12000]}\n\n"
        "请基于以上委派结果继续处理用户请求。若仍需委派，可再次输出同样 JSON；"
        "否则直接给出最终回复，不要重复委派 JSON。"
    )


def _task_image_paths(task: TaskInfo) -> list[str]:
    paths: list[str] = []
    for attachment in getattr(task, "attachments", []) or []:
        if not isinstance(attachment, dict):
            continue
        if str(attachment.get("kind") or "image") != "image":
            continue
        path = str(attachment.get("path") or "").strip()
        if path:
            paths.append(path)
    return paths


def _source_message_id_for_task(chat_session: Any | None, task_id: str) -> str:
    """Resolve the user-authored message bound to a Task, never assistant trace."""

    if chat_session is None or not str(task_id or "").strip():
        return ""
    try:
        messages = chat_session.get_all_messages()
    except Exception:
        return ""
    for message in reversed(messages):
        role = str(
            getattr(
                getattr(message, "role", ""),
                "value",
                getattr(message, "role", ""),
            )
            or ""
        )
        if role != "user" or str(getattr(message, "task_id", "") or "") != task_id:
            continue
        return str(getattr(message, "message_id", "") or "").strip()
    return ""


_CLARIFICATION_REPLY_MAX_CHARS = 200
_CLARIFICATION_GOAL_MAX_CHARS = 8000
_CLARIFICATION_ABANDON_RE = re.compile(
    r"^(?:算了|不用了?|不必了?|取消|停止|换个话题|聊点别的|"
    r"never\s*mind|forget\s+it|cancel|stop|new\s+topic)"
    r"[!,.?！，。？~～\s]*$",
    flags=re.IGNORECASE,
)
_STANDALONE_REQUEST_PREFIX_RE = re.compile(
    r"^(?:帮我|请|麻烦|能否|能不能|可以|我要|我想|给我|告诉我|"
    r"解释|说明|总结|写|创建|新建|打开|启动|播放|搜索|查找|运行|执行|"
    r"please\b|can\s+you\b|could\s+you\b|would\s+you\b|i\s+(?:want|need)\b|"
    r"tell\s+me\b|explain\b|summari[sz]e\b|write\b|create\b|open\b|launch\b|"
    r"play\b|search\b|find\b|run\b|execute\b)",
    flags=re.IGNORECASE,
)


def _runtime_clarification_event(run: dict[str, Any]) -> dict[str, Any] | None:
    """Return only a planner-authored clarification bound to this root goal."""

    user_goal = str(run.get("user_goal") or "").strip()
    result = str(run.get("result") or "").strip()
    if (
        str(run.get("kind") or "").strip() != "main_chat_run"
        or str(run.get("status") or "").strip() != "awaiting_user"
        or not user_goal
        or not result
    ):
        return None
    for raw_event in reversed(run.get("timeline") or []):
        if not isinstance(raw_event, dict):
            continue
        nested = raw_event.get("payload")
        payload = nested if isinstance(nested, dict) else {}
        event_type = str(
            raw_event.get("event")
            or raw_event.get("event_type")
            or payload.get("event")
            or payload.get("event_type")
            or ""
        ).strip()
        if event_type != "agent.plan.clarification_required":
            continue
        source = str(raw_event.get("source") or payload.get("source") or "").strip()
        event_goal = str(
            raw_event.get("original_goal") or payload.get("original_goal") or ""
        ).strip()
        question = str(raw_event.get("question") or payload.get("question") or "").strip()
        if (
            source == "runtime_model_intent_planner"
            and event_goal == user_goal
            and question == result
        ):
            return {**payload, **raw_event, "question": question}
    return None


def _clarification_reply_may_continue(reply: str) -> bool:
    """Distinguish a concise slot answer from an explicit independent request.

    Entity names can themselves look like executable intents to the Runtime
    planner (for example, ``Apple Music``).  Continuation safety therefore
    comes from the explicit lexical boundaries here plus the strict
    same-session/message adjacency checks in the caller, not from classifying
    the slot value as a standalone goal.
    """

    text = " ".join(str(reply or "").strip().split())
    if (
        not text
        or len(text) > _CLARIFICATION_REPLY_MAX_CHARS
        or _CLARIFICATION_ABANDON_RE.fullmatch(text)
        or "?" in text
        or "？" in text
        or _STANDALONE_REQUEST_PREFIX_RE.match(text)
    ):
        return False
    return True


def _clarification_continuation_for_task(
    service: Any,
    chat_session: Any | None,
    task: TaskInfo,
) -> _ClarificationContinuation | None:
    """Bind the immediately preceding clarification to one user reply.

    Only the previous Run's immutable user goal and the current user-authored
    message become authority.  The model's question, rationale, aliases, and
    proposed tool data are deliberately excluded.
    """

    session_id = str(getattr(task, "chat_session_id", "") or "").strip()
    lookup = getattr(service, "latest_awaiting_user_main_chat_run", None)
    if not session_id or chat_session is None or not callable(lookup):
        return None
    if getattr(task, "attachments", None) or _is_oha_yachiyo_group_coordinator_task(
        task.description
    ):
        return None
    try:
        pending = lookup(session_id)
    except Exception:
        logger.debug("Unable to inspect pending Main Chat clarification", exc_info=True)
        return None
    if not isinstance(pending, dict):
        return None
    event = _runtime_clarification_event(pending)
    if event is None or str(pending.get("session_id") or "").strip() != session_id:
        return None

    try:
        messages = list(chat_session.get_all_messages())
    except Exception:
        return None
    current_index = -1
    current_reply = ""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        role = str(
            getattr(
                getattr(message, "role", ""),
                "value",
                getattr(message, "role", ""),
            )
            or ""
        )
        if role != "user" or str(getattr(message, "task_id", "") or "") != task.task_id:
            continue
        current_index = index
        current_reply = str(getattr(message, "content", "") or "").strip()
        break
    if current_index < 0 or not _clarification_reply_may_continue(current_reply):
        return None

    previous_message = None
    for message in reversed(messages[:current_index]):
        role = str(
            getattr(
                getattr(message, "role", ""),
                "value",
                getattr(message, "role", ""),
            )
            or ""
        )
        content = str(getattr(message, "content", "") or "").strip()
        if role in {"user", "assistant"} and content:
            previous_message = message
            break
    if previous_message is None:
        return None
    previous_role = str(
        getattr(
            getattr(previous_message, "role", ""),
            "value",
            getattr(previous_message, "role", ""),
        )
        or ""
    )
    previous_task_id = str(getattr(previous_message, "task_id", "") or "").strip()
    previous_content = str(getattr(previous_message, "content", "") or "").strip()
    pending_task_id = str(pending.get("task_id") or "").strip()
    question = str(event.get("question") or "").strip()
    if (
        previous_role != "assistant"
        or not pending_task_id
        or previous_task_id != pending_task_id
        or not question
        or (question not in previous_content and previous_content not in question)
    ):
        return None

    prior_goal = str(pending.get("user_goal") or "").strip()
    continued_goal = f"{prior_goal}\n{current_reply}".strip()
    if not prior_goal or len(continued_goal) > _CLARIFICATION_GOAL_MAX_CHARS:
        return None
    return _ClarificationContinuation(
        user_goal=continued_goal,
        metadata={
            "runtime_clarification_continuation": True,
            "continued_from_run_id": str(pending.get("run_id") or "").strip(),
            "continued_from_task_id": pending_task_id,
            "clarification_authority": {
                "version": 1,
                "original_goal": prior_goal,
                "user_reply": current_reply,
            },
        },
    )


def _describe_day_period(hour: int) -> str:
    if 5 <= hour < 9:
        return "早上"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "深夜"


def format_environment_context(now: Optional[datetime] = None) -> str:
    """生成每轮对话的本地环境上下文。"""
    current = now.astimezone() if now is not None else datetime.now().astimezone()
    offset = current.strftime("%z")
    timezone_label = f"UTC{offset[:3]}:{offset[3:]}" if offset else "本地时区"
    weekday = _WEEKDAY_NAMES[current.weekday()]
    period = _describe_day_period(current.hour)
    return (
        "[当前环境]\n"
        f"当前本地时间：{current.strftime('%Y-%m-%d %H:%M:%S')}"
        f"（{timezone_label}，{weekday}，{period}）\n"
        "请结合当前时间、日期与时段理解问候、计划和相对时间表达。"
    )


def build_cross_session_memory_context(
    current_session_id: str = "",
    *,
    store: Any | None = None,
    memory_service: Any | None = None,
    max_items: int = _MEMORY_CONTEXT_MAX_ITEMS,
) -> str:
    """Recall managed, confirmed memory without scanning raw chat history.

    ``store`` remains as a compatibility argument, but is intentionally not
    read: ordinary chat text is not an authoritative durable-memory source.
    """
    max_items = max(0, min(int(max_items or 0), 20))
    if max_items <= 0 or memory_service is None:
        return ""
    try:
        context = str(
            memory_service.context_for(
                session_id=str(current_session_id or "").strip(),
                limit=max_items,
            )
            or ""
        ).strip()
    except Exception:
        logger.debug("读取托管长期记忆失败", exc_info=True)
        return ""
    if not context or context == "No durable memories yet.":
        return ""
    return f"[长期记忆]\n{context}"


def build_runtime_profile_context(runtime: "AppRuntime") -> str:
    """Combine configured profile fields with lightweight cross-session memory."""
    parts: list[str] = []
    try:
        configured = runtime.config.assistant.prompt_profile_context()
        if configured:
            parts.append(configured)
    except Exception:
        logger.debug("读取配置资料上下文失败", exc_info=True)
    try:
        current_session_id = str(getattr(runtime.chat_session, "session_id", "") or "")
        memory_service = getattr(
            getattr(runtime, "agent_runtime_service", None),
            "memory_services",
            None,
        )
        memory_context = build_cross_session_memory_context(
            current_session_id,
            memory_service=memory_service,
        )
        if memory_context:
            parts.append(memory_context)
    except Exception:
        logger.debug("构建长期记忆上下文失败", exc_info=True)
    return "\n\n".join(parts)


def _extract_memory_statement(text: str) -> str:
    value = " ".join(str(text or "").replace("\r", "\n").split())
    if len(value) < 4:
        return ""
    lowered = value.lower()
    if not any(marker in value or marker in lowered for marker in _MEMORY_MARKERS):
        return ""
    for prefix in (
        "请记住",
        "帮我记住",
        "记住：",
        "记住:",
        "记忆：",
        "记忆:",
        "以后请",
        "今后请",
    ):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    value = value.strip(" ：:，,。.;；")
    if not value:
        return ""
    if len(value) > _MEMORY_CONTEXT_ITEM_MAX_CHARS:
        value = value[: _MEMORY_CONTEXT_ITEM_MAX_CHARS - 1].rstrip() + "…"
    return value


def _format_memory_context(candidates: list[tuple[str, str, str]], current_session_id: str) -> str:
    if not candidates:
        return ""
    lines = [
        "[长期记忆]",
        "以下是用户在历史会话中明确表达的偏好、约束和长期说明；除非本轮明确更新，请持续遵守。",
    ]
    for created_at, session_id, memory in candidates:
        source = "当前会话" if session_id and session_id == current_session_id else "历史会话"
        date = _memory_context_date(created_at)
        label = f"{source} {date}".strip()
        lines.append(f"- {label}：{memory}" if label else f"- {memory}")
    context = "\n".join(lines)
    if len(context) > _MEMORY_CONTEXT_TOTAL_MAX_CHARS:
        context = context[: _MEMORY_CONTEXT_TOTAL_MAX_CHARS - 1].rstrip() + "…"
    return context


def _memory_context_date(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value[:10] if len(value) >= 10 else ""
    return parsed.date().isoformat()


def _read_title_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是有效整数，使用默认值 %d", name, raw, default)
        return default
    return max(minimum, min(value, maximum))


def _read_title_timeout() -> float:
    raw = os.getenv(_TITLE_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_TITLE_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是有效数字，使用默认值 %.0fs", _TITLE_TIMEOUT_ENV, raw, _DEFAULT_TITLE_TIMEOUT_SECONDS)
        return _DEFAULT_TITLE_TIMEOUT_SECONDS
    return max(3.0, min(value, 90.0))


def _session_title_generation_enabled() -> bool:
    raw = os.getenv(_TITLE_GENERATION_ENABLED_ENV, "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _read_title_interval_turns() -> int:
    raw = os.getenv(_TITLE_INTERVAL_TURNS_ENV, "").strip()
    if not raw:
        raw = os.getenv(_TITLE_INTERVAL_MESSAGES_ENV, "").strip()
    if not raw:
        return _DEFAULT_TITLE_INTERVAL_TURNS
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是有效整数，使用默认值 %d", _TITLE_INTERVAL_TURNS_ENV, raw, _DEFAULT_TITLE_INTERVAL_TURNS)
        return _DEFAULT_TITLE_INTERVAL_TURNS
    return max(1, min(value, 20))


def _completed_assistant_turn_count(chat_session: "ChatSession", *, include_pending_completion: bool = False) -> int:
    count = 1 if include_pending_completion else 0
    for message in chat_session.get_all_messages():
        role_value = getattr(getattr(message, "role", ""), "value", getattr(message, "role", ""))
        status_value = getattr(getattr(message, "status", ""), "value", getattr(message, "status", ""))
        if role_value == "assistant" and status_value == "completed":
            count += 1
    return count


def _should_refresh_generated_title(chat_session: "ChatSession", *, assistant_text: str = "") -> bool:
    if not _session_title_generation_enabled():
        return False
    turn_count = _completed_assistant_turn_count(
        chat_session,
        include_pending_completion=bool((assistant_text or "").strip()),
    )
    if turn_count >= 1 and looks_like_title_prompt_echo(_stored_session_title(chat_session)):
        return True
    interval = _read_title_interval_turns()
    return turn_count >= interval and turn_count % interval == 0


def _build_session_title_prompt(
    chat_session: "ChatSession",
    *,
    assistant_text: str = "",
) -> str:
    context_limit = _read_title_int_env(
        _TITLE_CONTEXT_MESSAGES_ENV,
        _DEFAULT_TITLE_CONTEXT_MESSAGES,
        minimum=2,
        maximum=24,
    )
    messages = chat_session.get_all_messages()
    assistant_text = assistant_text.strip()
    if assistant_text and not _message_list_already_ends_with_assistant_text(messages, assistant_text):
        messages = [
            *messages,
            SimpleNamespace(role="assistant", content=assistant_text),
        ]
    return build_direct_session_title_prompt(
        messages,
        current_title=_current_session_title(chat_session),
        context_limit=context_limit,
    )


def _message_list_already_ends_with_assistant_text(messages: list[Any], assistant_text: str) -> bool:
    for message in reversed(messages):
        role_value = getattr(getattr(message, "role", ""), "value", getattr(message, "role", ""))
        if role_value not in {"user", "assistant"}:
            continue
        return role_value == "assistant" and str(getattr(message, "content", "") or "").strip() == assistant_text
    return False


def _current_session_title(chat_session: "ChatSession") -> str:
    title = _stored_session_title(chat_session)
    if looks_like_title_prompt_echo(title):
        return ""
    return title


def _stored_session_title(chat_session: "ChatSession") -> str:
    store = getattr(chat_session, "_store", None)
    if store is None:
        return ""
    try:
        stored = store.get_session(chat_session.session_id)
    except Exception:
        logger.debug("读取当前会话标题失败", exc_info=True)
        return ""
    return str(getattr(stored, "title", "") or "") if stored is not None else ""


def _first_user_session_title(chat_session: "ChatSession") -> str:
    from apps.core.chat_store import make_session_title

    for message in chat_session.get_all_messages():
        role_value = getattr(getattr(message, "role", ""), "value", getattr(message, "role", ""))
        if role_value != "user":
            continue
        title = make_session_title(str(getattr(message, "content", "") or ""))
        if title:
            return title
    return ""


def _sanitize_generated_session_title(value: str | None) -> str:
    title = _ANSI_RE.sub("", value or "")
    title = re.sub(r"^(?:标题|会话标题)\s*[:：]\s*", "", title.strip(), flags=re.IGNORECASE)
    title = title.strip(" \t\r\n\"'“”‘’`*_#：:。.!！?？")
    title = re.sub(r"\s+", " ", title)
    title = title.splitlines()[0].strip() if title else ""
    if not title:
        return ""
    if looks_like_title_prompt_echo(title):
        return ""
    if len(title) > _GENERATED_TITLE_MAX_CHARS:
        title = title[:_GENERATED_TITLE_MAX_CHARS].rstrip()
    return title.strip(" \t\r\n\"'“”‘’`*_#：:。.!！?？")


async def _generate_session_title(prompt: str) -> str:
    """Generate a compact chat title with direct model API.

    Title generation is a product projection, so it must use the configured
    native model path and must not invoke an external execution kernel.
    """
    title = await generate_title_with_direct_api(prompt, timeout=_read_title_timeout())
    return _sanitize_generated_session_title(title)


async def _refresh_session_title_from_recent_messages(
    chat_session: "ChatSession",
    *,
    assistant_text: str = "",
) -> None:
    if is_proactive_chat_session(chat_session.session_id):
        return
    prompt = _build_session_title_prompt(chat_session, assistant_text=assistant_text)
    title = await _generate_session_title(prompt)
    if title:
        chat_session.set_session_title(title)
        return
    if looks_like_title_prompt_echo(_stored_session_title(chat_session)):
        fallback_title = _first_user_session_title(chat_session)
        if fallback_title:
            chat_session.set_session_title(fallback_title)


def _schedule_session_title_refresh(
    chat_session: "ChatSession | None",
    *,
    assistant_text: str = "",
) -> None:
    if chat_session is None or not _should_refresh_generated_title(chat_session, assistant_text=assistant_text):
        return
    existing = _TITLE_REFRESH_TASKS.get(chat_session.session_id)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(
        _refresh_session_title_from_recent_messages(chat_session, assistant_text=assistant_text),
        name=f"chat-title-{chat_session.session_id}",
    )
    _TITLE_REFRESH_TASKS[chat_session.session_id] = task

    def _log_title_task(done: asyncio.Task[None]) -> None:
        if _TITLE_REFRESH_TASKS.get(chat_session.session_id) is done:
            _TITLE_REFRESH_TASKS.pop(chat_session.session_id, None)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("会话标题生成任务失败", exc_info=True)

    task.add_done_callback(_log_title_task)


# ── 抽象接口 ─────────────────────────────────────────────────────────────────

class ExecutionStrategy(ABC):
    """任务执行策略抽象接口"""

    @abstractmethod
    async def run(self, task: TaskInfo) -> str:
        """执行任务，返回结果字符串。失败则抛出异常（TaskRunner 负责捕获）。"""
        ...

    @property
    def name(self) -> str:
        return type(self).__name__


def execution_capabilities(executor: Any) -> dict[str, bool]:
    """Normalize executor capabilities behind the TaskRunner boundary."""
    capabilities = getattr(executor, "capabilities", None)
    if isinstance(capabilities, dict):
        return {
            "model": bool(capabilities.get("model")),
            "image_input": bool(capabilities.get("image_input")),
            "tools": bool(capabilities.get("tools")),
            "approval": bool(capabilities.get("approval")),
        }
    return {
        "model": False,
        "image_input": False,
        "tools": False,
        "approval": False,
    }


# ── MVP 模拟执行器 ────────────────────────────────────────────────────────────

_SIM_RUN_DELAY: float = 2.0
_SIM_COMPLETE_DELAY: float = 5.0


class SimulatedExecutor(ExecutionStrategy):
    """MVP 模拟执行器（sleep + 占位结果，离线可用）"""

    async def run(self, task: TaskInfo) -> str:
        logger.debug("[Simulated] 开始执行: %s", task.task_id)
        await asyncio.sleep(_SIM_RUN_DELAY)
        await asyncio.sleep(_SIM_COMPLETE_DELAY)
        return f"[模拟结果] {task.description[:80]}"


class NativeAgentUnavailableExecutor(ExecutionStrategy):
    """Explicitly rejects user tasks until the native main model is ready."""

    def __init__(
        self,
        message: str | None = None,
        *,
        reason: str = "model_profile_required",
        daily_desktop_executor: "NativeAgentExecutor | None" = None,
    ) -> None:
        self.reason = str(message or NATIVE_AGENT_NOT_READY_MESSAGE)
        self.code = "native_agent_not_ready"
        self.reason_code = str(reason or "model_profile_required")
        self._daily_desktop_executor = daily_desktop_executor

    @property
    def capabilities(self) -> dict[str, bool]:
        return {
            "model": False,
            "image_input": False,
            "tools": self._daily_desktop_executor is not None,
            "approval": self._daily_desktop_executor is not None,
        }

    async def run(self, task: TaskInfo) -> str:
        if self._daily_desktop_executor is not None and self._is_daily_desktop_task(task):
            logger.info("[NativeAgentUnavailable] 使用日常桌面工具兜底执行任务: %s", task.task_id)
            return await self._daily_desktop_executor.run(task)
        logger.warning("[NativeAgentUnavailable] 拒绝执行任务: %s | %s", task.task_id, self.reason)
        raise NativeAgentError(
            self.reason,
            code=self.code,
            reason=self.reason_code,
        )

    @staticmethod
    def _is_daily_desktop_task(task: TaskInfo) -> bool:
        if getattr(task, "attachments", None):
            return False
        try:
            from apps.shell.yachiyo_agent.daily_desktop import daily_desktop_allowed_tools
            from apps.shell.yachiyo_agent.entrypoint_tool_selection import (
                planner_first_direct_tool_selection,
            )

            selection = planner_first_direct_tool_selection(
                str(task.description or ""),
                daily_desktop_allowed_tools(),
                metadata={"runtime_planner_execution_context": True},
            )
            return bool(
                selection.requests
                and selection.selected_source == "runtime_planner"
                and _daily_desktop_fallback_requests_are_direct(selection.requests)
            )
        except Exception:
            logger.debug("日常桌面 planner 识别失败", exc_info=True)
            return False


def _daily_desktop_fallback_requests_are_direct(requests: list[dict[str, Any]]) -> bool:
    tools = [
        str(request.get("tool") or request.get("tool_name") or "").strip()
        for request in requests
        if str(request.get("tool") or request.get("tool_name") or "").strip()
    ]
    return bool(tools) and all(
        tool.startswith(_DAILY_DESKTOP_FALLBACK_TOOL_PREFIXES) for tool in tools
    )


class NativeAgentExecutor(ExecutionStrategy):
    """TaskRunner adapter for the persistent native Agent runtime."""

    def __init__(
        self,
        chat_session: Optional["ChatSession"] = None,
        persona_prompt_getter: Optional[Callable[[], str]] = None,
        user_address_getter: Optional[Callable[[], str]] = None,
        profile_context_getter: Optional[Callable[[], str]] = None,
        runtime_service_getter: Optional[Callable[[], Any]] = None,
        tool_policy_getter: Optional[Callable[[], dict[str, Any]]] = None,
        workspace_policy_getter: Optional[Callable[[], dict[str, Any]]] = None,
        activity_store_getter: Optional[Callable[[], "ActivityStore"]] = None,
    ) -> None:
        self._chat_session = chat_session
        self._persona_prompt_getter = persona_prompt_getter
        self._user_address_getter = user_address_getter
        self._profile_context_getter = profile_context_getter
        self._runtime_service_getter = runtime_service_getter
        self._tool_policy_getter = tool_policy_getter
        self._workspace_policy_getter = workspace_policy_getter
        self._activity_store_getter = activity_store_getter
        self._main_chat_run_snapshots: dict[str, dict[str, Any]] = {}
        self._task_runner: Any = None

    @property
    def capabilities(self) -> dict[str, bool]:
        return {
            "model": True,
            "image_input": True,
            "tools": True,
            "approval": True,
        }

    def set_chat_session(self, chat_session: Optional["ChatSession"]) -> None:
        self._chat_session = chat_session

    def set_task_runner(self, runner: Any) -> None:
        self._task_runner = runner

    def is_available(self) -> bool:
        return True

    def _runtime_service(self) -> Any:
        if self._runtime_service_getter is not None:
            return self._runtime_service_getter()
        from apps.shell.agent_runtime import get_native_run_engine

        return get_native_run_engine()

    async def run(self, task: TaskInfo) -> str:
        service = self._runtime_service()
        chat_session = self._chat_session_for_task(task)
        run_id = ""
        delegated_child_ids: list[str] = []
        parent_policy = self._main_chat_runtime_policy_kwargs()
        try:
            continuation = _clarification_continuation_for_task(
                service,
                chat_session,
                task,
            )
            user_goal = continuation.user_goal if continuation else task.description
            group_context = self._native_group_context(chat_session, task)
            if group_context is not None and continuation is None:
                # Generated group instructions are model context, never a
                # replacement for the actual linked user message.
                from apps.shell.chat_api import ChatAPI

                user_goal = ChatAPI._main_model_goal_text(group_context["source_text"])
            run_metadata: dict[str, Any] = {
                "source_message_id": _source_message_id_for_task(
                    chat_session,
                    task.task_id,
                )
            }
            if task.response_context is not None:
                from apps.shell.agent.runtime.main_chat_delegation import _digest

                run_metadata["response_context_digest"] = _digest(task.response_context)
            runtime_execution_metadata: dict[str, Any] | None = None
            if continuation is not None:
                run_metadata.update(continuation.metadata)
                runtime_execution_metadata = dict(continuation.metadata)
            run = await asyncio.to_thread(
                service.start_main_chat_run,
                task_id=task.task_id,
                session_id=str(getattr(task, "chat_session_id", "") or ""),
                user_goal=user_goal,
                metadata=run_metadata,
            )
            run_id = str(run.get("run_id") or "")
            image_paths = _task_image_paths(task)
            messages = self._messages_for_task(
                task,
                chat_session,
                image_paths=image_paths,
                runtime_service=service,
            )
            if image_paths:
                bind_supplied_images = getattr(service, "bind_main_chat_supplied_images", None)
                if callable(bind_supplied_images):
                    await asyncio.to_thread(bind_supplied_images, run_id, messages)
                from apps.shell.native_capabilities import get_native_image_input_capability

                image_capability = get_native_image_input_capability()
                if not image_capability.get("can_attach_images"):
                    raise NativeAgentError(
                        str(image_capability.get("reason") or "Native Agent 图片输入不可用"),
                        reason="vision_model_profile_required",
                    )
                if image_capability.get("route") == "vision_text":
                    vision_result = await asyncio.to_thread(
                        service.call_main_chat_model,
                        run_id,
                        [
                            {
                                "role": "user",
                                "content": self._task_user_content(
                                    "请准确分析这些图片，并输出供另一个对话模型继续回答用户请求的详细文字描述。",
                                    image_paths,
                                ),
                            }
                        ],
                        profile_id=str(image_capability.get("profile_id") or ""),
                        capability="vision",
                    )
                    messages = self._messages_for_task(
                        task,
                        chat_session,
                        image_paths=[],
                        runtime_service=service,
                    )
                    messages[-1]["content"] = (
                        f"{self._task_prompt(task)}\n\n[图片识别结果]\n{vision_result}"
                    )
            group_coordinator = _is_oha_yachiyo_group_coordinator_task(task.description)
            prepare_delegation = getattr(service, "prepare_main_chat_delegation", None)
            delegation_plan = None
            if callable(prepare_delegation) and not image_paths and task.response_context is None:
                delegation_plan = await asyncio.to_thread(
                    prepare_delegation, run_id,
                    selected_ids=set(group_context["selected_ids"]) if group_context else set(),
                    group_scope=group_context is not None, **parent_policy,
                )
            if delegation_plan is not None:
                proposal_output = await asyncio.to_thread(service.call_main_chat_model, run_id, messages)
                if group_context is not None:
                    from apps.shell.chat_api import ChatAPI

                    proposals = [directive.as_request() for directive in ChatAPI._parse_group_dispatch_directives(proposal_output)]
                else:
                    directive = _parse_oha_delegation_directive(proposal_output)
                    proposals = [directive.as_request()] if directive else []
                for target in delegation_plan["binding"]["targets"]:
                    self._record_activity(task, f"正在委派给 {target['name']}", target["goal"], "running")
                upstream = task.description if group_context else ""
                if group_context:
                    upstream += "\n\n[Oha-Yachiyo 群组执行约定]\n请按当前岗位职责处理已绑定子目标。"
                children = await asyncio.to_thread(
                    service.start_main_chat_delegation, run_id, proposals,
                    task_id=task.task_id, group=group_context is not None, upstream=upstream,
                )
                delegated_child_ids = [str(child["run_id"]) for child in children]
                if group_context:
                    self._project_native_group_dispatch(chat_session, task, delegation_plan, children, proposal_output)
                settled = await self._wait_for_delegated_children(
                    service, run_id, children, chat_session, task, delegation_plan,
                    group=group_context is not None,
                )
                await asyncio.to_thread(service.verify_main_chat_delegation, run_id)
                if group_context:
                    from apps.shell.chat_api import ChatAPI

                    visible_output = ChatAPI._strip_group_dispatch_payloads(proposal_output).strip()
                    if delegation_plan["binding"].get("summary_required"):
                        await self._wait_for_group_summary(service, run_id, chat_session, task)
                    await self._complete_main_chat_run_with_outcome(service, run_id, visible_output)
                    return visible_output
                delegated_text = "\n\n".join(_format_oha_delegation_result({
                    **child, "runnable": target,
                }) for child, target in zip(settled, delegation_plan["binding"]["targets"]))
                messages.extend([
                    {"role": "assistant", "content": proposal_output},
                    {"role": "user", "content": _build_oha_delegation_followup(user_goal, proposal_output, delegated_text)},
                ])
            delegation_count = 0
            while True:
                output = await self._call_main_chat_model_loop(
                    service,
                    run_id,
                    messages,
                    chat_session,
                    task,
                    runtime_execution_metadata=runtime_execution_metadata,
                    final_response_only=delegation_plan is not None,
                )
                self._update_processing_message(chat_session, task.task_id, output)
                if isinstance(output, _AwaitingUserReply):
                    return str(output)
                delegation_directive = (
                    None
                    if group_coordinator or task.response_context is not None or delegation_plan is not None
                    else _parse_oha_delegation_directive(output)
                )
                if delegation_directive is None:
                    await self._complete_main_chat_run_with_outcome(
                        service,
                        run_id,
                        output,
                    )
                    _schedule_session_title_refresh(chat_session, assistant_text=output)
                    return output
                if delegation_count >= 3:
                    result = "OHA 自动委派已达到 3 次上限，已停止以避免循环调用。"
                    self._record_activity(
                        task,
                        "OHA 委派已停止",
                        "单轮自动委派超过 3 次上限",
                        "failed",
                    )
                    await self._complete_main_chat_run_with_outcome(
                        service,
                        run_id,
                        result,
                    )
                    return result
                delegation_count += 1
                target = delegation_directive.target_label
                self._record_activity(
                    task,
                    f"正在委派给 {target}",
                    delegation_directive.goal,
                    "running",
                )
                delegation_plan = await asyncio.to_thread(
                    service.prepare_main_chat_delegation_proposal, run_id,
                    delegation_directive.as_request(), **parent_policy,
                )
                children = await asyncio.to_thread(
                    service.start_main_chat_delegation, run_id, [delegation_directive.as_request()],
                    task_id=task.task_id, group=False, upstream="",
                )
                delegated_child_ids.extend(str(child["run_id"]) for child in children)
                settled = await self._wait_for_delegated_children(
                    service, run_id, children, chat_session, task, delegation_plan, group=False,
                )
                await asyncio.to_thread(service.verify_main_chat_delegation, run_id)
                delegated_text = _format_oha_delegation_result({**settled[0], "runnable": delegation_plan["binding"]["targets"][0]})
                messages.append({"role": "assistant", "content": output})
                messages.append(
                    {
                        "role": "user",
                        "content": _build_oha_delegation_followup(
                            task.description,
                            output,
                            delegated_text,
                        ),
                    }
                )
        except asyncio.CancelledError:
            for child_id in delegated_child_ids:
                try:
                    await asyncio.to_thread(service.cancel_run, child_id)
                except Exception:
                    logger.debug("取消已绑定的委派子 Run 失败: %s", child_id, exc_info=True)
            if run_id:
                try:
                    service.cancel_run(run_id)
                except Exception:
                    logger.debug("取消 Native Run 失败: %s", run_id, exc_info=True)
            raise
        except Exception as exc:
            for child_id in delegated_child_ids:
                try:
                    child = await asyncio.to_thread(service.get_run, child_id)
                    if child.get("status") not in {"completed", "failed", "cancelled"}:
                        await asyncio.to_thread(service.cancel_run, child_id)
                except Exception:
                    logger.debug("收尾已绑定的委派子 Run 失败: %s", child_id, exc_info=True)
            skip_run_failure = False
            if isinstance(exc, NativeAgentError) and run_id:
                try:
                    current_run = await asyncio.to_thread(service.get_run, run_id)
                    current_status = str(current_run.get("status") or "").lower()
                    if current_status in {
                        "awaiting_user",
                        "cancelled",
                        "failed",
                        "completed",
                    }:
                        skip_run_failure = True
                    elif exc.reason == "approval_required":
                        pending_approval = current_run.get("pending_approval")
                        skip_run_failure = current_status == "approval_required" or bool(
                            isinstance(pending_approval, dict) and pending_approval
                        )
                    elif exc.reason == "approval_timeout":
                        skip_run_failure = current_status in {
                            "cancelled",
                            "failed",
                            "completed",
                        }
                except Exception:
                    logger.debug("检查 Native Run 审批/超时状态失败: %s", run_id, exc_info=True)
            if run_id and not skip_run_failure:
                try:
                    await asyncio.to_thread(service.fail_main_chat_run, run_id, exc)
                except Exception:
                    logger.debug("记录 Native Run 失败状态失败: %s", run_id, exc_info=True)
            if isinstance(exc, NativeAgentError):
                raise
            raise NativeAgentError(redact_api_error_text(exc)) from exc
        finally:
            if run_id:
                self._main_chat_run_snapshots.pop(run_id, None)

    @staticmethod
    def _native_group_context(chat_session: Any, task: TaskInfo) -> dict[str, Any] | None:
        if chat_session is None or getattr(chat_session, "_store", None) is None:
            return None
        record = chat_session._store.get_session(chat_session.session_id)
        if record is None or getattr(record, "conversation_kind", "") != "group":
            return None
        source = next((message for message in chat_session.get_all_messages()
                       if str(getattr(message, "task_id", "")) == task.task_id
                       and str(getattr(message.role, "value", message.role)) == "user"), None)
        if source is None:
            return None
        participants = json.loads(getattr(record, "participants_json", "[]"))
        return {"source_text": source.content,
                "selected_ids": [str(item["id"]) for item in participants
                                 if isinstance(item, dict) and item.get("kind") == "agent" and item.get("id")]}

    @staticmethod
    def _project_native_group_dispatch(chat_session: Any, task: TaskInfo, plan: dict[str, Any], children: list[dict[str, Any]], output: str) -> None:
        if chat_session is None:
            return
        from apps.core.chat_session import MessageStatus
        from apps.shell.chat_api import ChatAPI

        started = []
        for index, (child, target) in enumerate(zip(children, plan["binding"]["targets"])):
            identity = str(child.get("client_request_id") or "")
            started.append({"dispatch_index": index, "runnable_id": target["runnable_id"],
                            "child_identity": identity, "run_id": child["run_id"], "run_group_id": child["run_group_id"]})
            chat_session.upsert_assistant_projection_message(identity, "", status=MessageStatus.PROCESSING,
                metadata={"sender": {"kind": "agent", "id": target["runnable_id"], "name": target["name"], "nickname": target["alias"]},
                          "runnable_kind": "agent", "runnable_id": target["runnable_id"],
                          "run_id": child["run_id"], "run_group_id": child["run_group_id"], "run_status": child["status"],
                          "conversation_kind": "group", "group_goal": target["goal"],
                          "delegated_by_task_id": task.task_id, "delegated_goal": target["goal"]})
        chat_session.upsert_assistant_message(task_id=task.task_id, content=ChatAPI._strip_group_dispatch_payloads(output).strip(), status=MessageStatus.PROCESSING,
            metadata={"group_dispatch_handled": True, "group_dispatch_state": "handled",
                      "group_dispatch_count": len(children), "group_dispatch_started": started,
                      "group_dispatch_run_group_id": children[0]["run_group_id"]})

    async def _wait_for_delegated_children(self, service: Any, parent_id: str, children: list[dict[str, Any]],
                                         chat_session: Any, task: TaskInfo, plan: dict[str, Any], *, group: bool) -> list[dict[str, Any]]:
        from apps.core.chat_session import MessageStatus

        deadlines = {child["run_id"]: time.monotonic() + self._approval_wait_timeout_seconds() for child in children}
        approval_ids: dict[str, str] = {}
        remaining = set(deadlines)
        settled: dict[str, dict[str, Any]] = {}
        while remaining:
            parent = await asyncio.to_thread(service.get_run, parent_id)
            if parent.get("status") != "running":
                for child_id in remaining:
                    await asyncio.to_thread(service.cancel_run, child_id)
                raise NativeAgentError("委派父任务已结束", reason="cancelled")
            for child, target in zip(children, plan["binding"]["targets"]):
                child_id = child["run_id"]
                if child_id not in remaining:
                    continue
                current = await asyncio.to_thread(service.get_run, child_id)
                status = str(current.get("status") or "")
                pending_id = self._pending_approval_id(current)
                if pending_id and approval_ids.get(child_id) != pending_id:
                    approval_ids[child_id] = pending_id
                    deadlines[child_id] = time.monotonic() + self._approval_wait_timeout_seconds()
                    self._record_activity(task, f"{target['name']} 等待审批", self._approval_required_content(current), "running",
                        metadata={"run_id": child_id, "run_group_id": current["run_group_id"], "run_status": status,
                                  "pending_approval": current.get("pending_approval") or {}})
                if group and chat_session is not None:
                    projected = next((message for message in chat_session.get_all_messages()
                                      if (message.metadata or {}).get("run_id") == child_id), None)
                    if projected is not None:
                        terminal = status in {"completed", "failed", "cancelled"}
                        result = str(current.get("result") or "")
                        content = f"{target['alias']}：{'已完成' if status == 'completed' else status}\n\n汇报：{result}" if terminal else ""
                        chat_session.update_assistant_message(projected.message_id, content,
                            status=MessageStatus.COMPLETED if status == "completed" else MessageStatus.FAILED if terminal else MessageStatus.PROCESSING,
                            metadata={"run_status": status, "pending_approval": current.get("pending_approval") or {},
                                      "agent_report": result if terminal else None, "agent_report_status": status if terminal else None})
                if status in {"failed", "cancelled", "awaiting_user"}:
                    for other_id in remaining - {child_id}:
                        await asyncio.to_thread(service.cancel_run, other_id)
                    raise NativeAgentError(str(current.get("result") or "委派子目标未完成"), reason="delegated_goal_unfulfilled")
                if status == "completed":
                    remaining.remove(child_id)
                    settled[child_id] = current
                    self._record_activity(task, f"{target['name']} 委派完成", str(current.get("result") or "")[:500], "completed",
                        metadata={"run_id": child_id, "run_group_id": current["run_group_id"], "run_status": status})
                elif time.monotonic() >= deadlines[child_id]:
                    if status == "approval_required":
                        await asyncio.to_thread(service.timeout_run_approval, child_id, reason="approval_wait_timeout",
                                                expected_approval_id=approval_ids.get(child_id) or "")
                    else:
                        await asyncio.to_thread(service.cancel_run, child_id)
                    raise NativeAgentError("委派子任务等待超时", reason="approval_timeout")
            if remaining:
                await asyncio.sleep(0.05)
        return [settled[child["run_id"]] for child in children]

    async def _wait_for_group_summary(self, service: Any, parent_id: str, session: Any, task: TaskInfo) -> None:
        from apps.shell.chat_api import ChatAPI
        from packages.protocol.enums import TaskStatus

        runner = self._task_runner
        if runner is None or session is None:
            raise NativeAgentError("群组总结执行器不可用", reason="delegation_summary_unavailable")
        api = ChatAPI(SimpleNamespace(state=runner._state, chat_session=session,
                    store=getattr(session, "_store", None), agent_runtime_service=service))
        api._maybe_create_group_agent_summary_task(task.task_id)
        parent_message = session.get_assistant_message_for_task(task.task_id)
        summary_id = str((parent_message.metadata or {}).get("group_agent_summary_task_id") or "") if parent_message else ""
        summary = runner._state.get_task(summary_id)
        if summary is None or summary.task_id == task.task_id or summary.response_context is None:
            raise NativeAgentError("群组总结任务绑定失败", reason="delegation_summary_binding_conflict")
        await asyncio.to_thread(service.bind_main_chat_delegation_summary, parent_id, summary)
        runner._dispatch_pending()
        deadline = time.monotonic() + self._approval_wait_timeout_seconds()
        try:
            while True:
                parent = await asyncio.to_thread(service.get_run, parent_id)
                current = runner._state.get_task(summary_id)
                if parent.get("status") != "running":
                    raise NativeAgentError("群组父任务已结束", reason="cancelled")
                if current is None or current.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
                    raise NativeAgentError(str(current.error or "群组总结失败") if current else "群组总结不可恢复", reason="delegation_summary_unfulfilled")
                if current.status == TaskStatus.COMPLETED:
                    await asyncio.to_thread(service.verify_main_chat_delegation_summary, parent_id)
                    return
                if time.monotonic() >= deadline:
                    raise NativeAgentError("群组总结等待超时", reason="approval_timeout")
                await asyncio.sleep(0.05)
        except BaseException:
            runner.cancel_task(summary_id)
            current = runner._state.get_task(summary_id)
            if current and current.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                runner._state.cancel_task(summary_id)
            raise

    def _messages_for_task(
        self,
        task: TaskInfo,
        chat_session: Optional["ChatSession"],
        *,
        image_paths: list[str] | None = None,
        runtime_service: Any | None = None,
    ) -> list[dict[str, Any]]:
        persona_prompt = self._safe_get(self._persona_prompt_getter)
        user_address = self._safe_get(self._user_address_getter)
        profile_context = self._safe_get(self._profile_context_getter)
        profile_context = _append_oha_delegation_context(
            profile_context,
            group_coordinator=_is_oha_yachiyo_group_coordinator_task(task.description),
            runtime_service=runtime_service,
        )
        system_prompt = format_persona_description(
            "请直接处理当前用户请求，并返回适合展示给用户的最终回复。",
            persona_prompt,
            user_address,
            format_environment_context(),
            profile_context,
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        history_chars = 0
        if chat_session is not None:
            history: list[dict[str, str]] = []
            for message in reversed(chat_session.get_all_messages()):
                if str(getattr(message, "task_id", "") or "") == task.task_id:
                    continue
                role = str(getattr(getattr(message, "role", ""), "value", getattr(message, "role", "")))
                content = str(getattr(message, "content", "") or "").strip()
                if role not in {"user", "assistant"} or not content:
                    continue
                if history_chars + len(content) > _MAIN_CHAT_HISTORY_CHAR_LIMIT:
                    break
                history.append({"role": role, "content": content})
                history_chars += len(content)
                if len(history) >= _MAIN_CHAT_HISTORY_MESSAGE_LIMIT:
                    break
            messages.extend(reversed(history))
        messages.append(
            {
                "role": "user",
                "content": self._task_user_content(
                    self._task_prompt(task),
                    _task_image_paths(task) if image_paths is None else image_paths,
                ),
            }
        )
        return messages

    @staticmethod
    def _task_prompt(task: TaskInfo) -> str:
        if task.response_context is None:
            return task.description
        return f"{task.description}\n\n[Supplied context]\n{task.response_context}"

    @staticmethod
    def _safe_get(getter: Optional[Callable[[], str]]) -> str:
        if getter is None:
            return ""
        try:
            return str(getter() or "")
        except Exception:
            logger.debug("读取 Native Agent 上下文失败", exc_info=True)
            return ""

    async def _call_main_chat_model_loop(
        self,
        service: Any,
        run_id: str,
        messages: list[dict[str, Any]],
        chat_session: Optional["ChatSession"],
        task: TaskInfo,
        *,
        runtime_execution_metadata: dict[str, Any] | None = None,
        final_response_only: bool = False,
    ) -> str:
        execute_loop = getattr(service, "execute_main_chat_model_loop", None)
        if not callable(execute_loop):
            return await asyncio.to_thread(service.call_main_chat_model, run_id, messages)
        kwargs = self._main_chat_runtime_policy_kwargs()
        if task.response_context is not None or final_response_only:
            # Internal summaries describe evidence that is already supplied.
            # Their context may contain the original effectful request, but
            # it cannot authorize a fresh tool call or delegation.
            kwargs["tool_policy"] = {"allowed_tools": [], "response_only": True}
        if runtime_execution_metadata:
            kwargs["runtime_execution_metadata"] = runtime_execution_metadata
        run = await asyncio.to_thread(execute_loop, run_id, messages, **kwargs)
        status = str(run.get("status") or "").strip().lower()
        result = str(run.get("result") or "").strip()
        if status == "awaiting_user":
            self._main_chat_run_snapshots[run_id] = dict(run)
            return _AwaitingUserReply(
                result or "请补充任务目标、对象或期望结果。"
            )
        if status == "approval_required":
            self._update_processing_message(
                chat_session,
                task.task_id,
                self._approval_required_content(run),
            )
            return await self._wait_for_main_chat_run_output(
                service,
                run_id,
                chat_session,
                task,
                previous_result=result,
            )
        if status in {"failed", "cancelled"}:
            raise NativeAgentError(
                result or f"Native Run {status}",
                reason=status,
            )
        if result:
            return result
        return await asyncio.to_thread(service.call_main_chat_model, run_id, messages)

    async def _complete_main_chat_run_with_outcome(
        self,
        service: Any,
        run_id: str,
        output: str,
    ) -> None:
        outcome = await self._evaluate_main_chat_outcome(service, run_id)
        if outcome.kind == "failed":
            raise NativeAgentError(
                outcome.message,
                reason=outcome.reason or "desktop_outcome_failed",
            )
        if outcome.kind == "approval_required":
            raise NativeAgentError(
                outcome.message or "Native Agent 仍在等待工具审批",
                reason="approval_required",
            )
        if outcome.kind == "awaiting_user":
            return
        completed = await asyncio.to_thread(
            service.complete_main_chat_run,
            run_id,
            output,
        )
        completed_payload = completed if isinstance(completed, dict) else {}
        completed_status = str(completed_payload.get("status") or "").strip().lower()
        pending_approval = completed_payload.get("pending_approval")
        if completed_status == "completed":
            return
        if completed_status == "approval_required" or bool(
            isinstance(pending_approval, dict) and pending_approval
        ):
            raise NativeAgentError(
                str(completed_payload.get("result") or "Native Agent 仍在等待工具审批"),
                reason="approval_required",
            )
        if completed_status in {"failed", "cancelled", "canceled"}:
            reason = "cancelled" if completed_status in {"cancelled", "canceled"} else "failed"
            raise NativeAgentError(
                str(completed_payload.get("result") or f"Native Run {reason}"),
                reason=reason,
            )
        raise NativeAgentError(
            "Native Agent 未确认任务已完成，请重试。",
            reason="run_completion_not_confirmed",
        )

    async def _evaluate_main_chat_outcome(
        self,
        service: Any,
        run_id: str,
    ) -> MainChatOutcomeEvaluation:
        run_payload = self._main_chat_run_snapshots.pop(run_id, {})
        get_run = getattr(service, "get_run", None)
        if callable(get_run):
            try:
                current_run = await asyncio.to_thread(get_run, run_id)
                if isinstance(current_run, dict):
                    run_payload = current_run
            except Exception:
                logger.debug(
                    "读取 Native Run outcome snapshot 失败: %s",
                    run_id,
                    exc_info=True,
                )
        list_events = getattr(service, "list_run_events", None)
        if not callable(list_events):
            # Older runtime ports may expose only the run snapshot/timeline.
            return evaluate_main_chat_outcome(run_payload)
        events: list[dict[str, Any]] = []
        after_sequence = 0
        for page_index in range(_OUTCOME_EVENT_MAX_PAGES):
            try:
                payload = await asyncio.to_thread(
                    list_events,
                    run_id,
                    after_sequence=after_sequence,
                    limit=_OUTCOME_EVENT_PAGE_LIMIT,
                    include_internal=True,
                )
            except TypeError:
                if page_index > 0:
                    return _incomplete_outcome_event_history()
                try:
                    payload = await asyncio.to_thread(list_events, run_id)
                except Exception:
                    logger.debug(
                        "读取 Native Run outcome events 失败: %s",
                        run_id,
                        exc_info=True,
                    )
                    # The runtime explicitly exposes a durable event stream, so
                    # an unavailable stream must never be treated as an empty
                    # history.  Falling back to the snapshot here can turn a
                    # failed desktop action into a false completion.
                    return _incomplete_outcome_event_history()
            except Exception:
                logger.debug(
                    "读取 Native Run outcome events 失败: %s",
                    run_id,
                    exc_info=True,
                )
                return _incomplete_outcome_event_history()

            if isinstance(payload, dict):
                if "events" not in payload:
                    return _incomplete_outcome_event_history()
                page_events = payload["events"]
            elif isinstance(payload, list):
                page_events = payload
            else:
                return _incomplete_outcome_event_history()
            if not isinstance(page_events, list) or any(
                not isinstance(event, dict) for event in page_events
            ):
                return _incomplete_outcome_event_history()
            events.extend(dict(event) for event in page_events)
            has_more = bool(payload.get("has_more")) if isinstance(payload, dict) else False
            if not has_more:
                return evaluate_main_chat_outcome(run_payload, events)
            if len(events) >= _OUTCOME_EVENT_MAX_EVENTS:
                return _incomplete_outcome_event_history()
            next_after_sequence = (
                int(payload.get("next_after_sequence") or 0)
                if isinstance(payload, dict)
                else 0
            )
            if next_after_sequence <= after_sequence:
                return _incomplete_outcome_event_history()
            after_sequence = next_after_sequence
        return _incomplete_outcome_event_history()

    def _main_chat_runtime_policy_kwargs(self) -> dict[str, dict[str, Any]]:
        kwargs: dict[str, dict[str, Any]] = {}
        tool_policy = self._safe_policy_get(self._tool_policy_getter)
        if tool_policy is not None:
            kwargs["tool_policy"] = tool_policy
        workspace_policy = self._safe_policy_get(self._workspace_policy_getter)
        if workspace_policy is not None:
            kwargs["workspace_policy"] = workspace_policy
        return kwargs

    @staticmethod
    def _safe_policy_get(getter: Optional[Callable[[], dict[str, Any]]]) -> dict[str, Any] | None:
        if getter is None:
            return None
        try:
            value = getter()
        except Exception:
            logger.debug("读取 Native Agent runtime policy 失败", exc_info=True)
            return None
        return dict(value) if isinstance(value, dict) else None

    async def _wait_for_main_chat_run_output(
        self,
        service: Any,
        run_id: str,
        chat_session: Optional["ChatSession"],
        task: TaskInfo,
        *,
        previous_result: str = "",
    ) -> str:
        timeout_seconds = self._approval_wait_timeout_seconds()
        deadline = time.monotonic() + timeout_seconds
        last_status = ""
        expected_approval_id = ""
        while True:
            while time.monotonic() < deadline:
                run = await asyncio.to_thread(service.get_run, run_id)
                if isinstance(run, dict):
                    self._main_chat_run_snapshots[run_id] = dict(run)
                status = str(run.get("status") or "").strip()
                result = str(run.get("result") or "").strip()
                if status != last_status:
                    last_status = status
                    if status == "approval_required":
                        self._update_processing_message(
                            chat_session,
                            task.task_id,
                            self._approval_required_content(run),
                        )
                if status == "approval_required":
                    current_approval_id = self._pending_approval_id(run)
                    if current_approval_id and current_approval_id != expected_approval_id:
                        expected_approval_id = current_approval_id
                        deadline = time.monotonic() + timeout_seconds
                    await asyncio.sleep(0.5)
                    continue
                if status == "awaiting_user":
                    return _AwaitingUserReply(
                        result or "请补充任务目标、对象或期望结果。"
                    )
                if status in {"failed", "cancelled"}:
                    raise NativeAgentError(result or f"Native Run {status}", reason=status)
                if (
                    status in {"running", "processing"}
                    and result
                    and result != previous_result
                    and not result.startswith("已批准，")
                ):
                    return result
                if status == "completed" and result:
                    return result
                await asyncio.sleep(0.5)
            try:
                timeout_result: dict[str, Any] = {}
                timeout_approval = getattr(service, "timeout_run_approval", None)
                if callable(timeout_approval):
                    timeout_kwargs: dict[str, Any] = {"reason": "approval_wait_timeout"}
                    if expected_approval_id and supports_keyword(
                        timeout_approval,
                        "expected_approval_id",
                    ):
                        timeout_kwargs["expected_approval_id"] = expected_approval_id
                    result = await asyncio.to_thread(
                        timeout_approval,
                        run_id,
                        **timeout_kwargs,
                    )
                    timeout_result = result if isinstance(result, dict) else {}
                else:
                    await asyncio.to_thread(service.cancel_run, run_id)
            except Exception:
                logger.debug(
                    "Native Run 审批等待超时后取消失败: %s",
                    run_id,
                    exc_info=True,
                )
                timeout_result = {}
            timeout_generation = self._pending_approval_id(timeout_result)
            if (
                str(timeout_result.get("status") or "").strip() == "approval_required"
                and timeout_generation
                and timeout_generation != expected_approval_id
            ):
                expected_approval_id = timeout_generation
                deadline = time.monotonic() + timeout_seconds
                continue
            timeout_message = str(timeout_result.get("result") or "").strip()
            raise NativeAgentError(
                timeout_message or "Native Agent 等待工具审批超时",
                reason="approval_timeout",
            )

    @staticmethod
    def _approval_wait_timeout_seconds() -> float:
        raw = os.getenv("OHA_YACHIYO_MAIN_CHAT_APPROVAL_TIMEOUT_SECONDS", "").strip()
        if not raw:
            return 600.0
        try:
            value = float(raw)
        except ValueError:
            return 600.0
        return max(1.0, value)

    @staticmethod
    def _pending_approval_id(run: Any) -> str:
        pending = run.get("pending_approval") if isinstance(run, dict) else None
        if not isinstance(pending, dict):
            return ""
        return str(pending.get("approval_id") or "").strip()

    @staticmethod
    def _approval_required_content(run: dict[str, Any]) -> str:
        pending = run.get("pending_approval") if isinstance(run.get("pending_approval"), dict) else {}
        tool = str(pending.get("tool") or "tool").strip()
        lines = [f"等待审批：{tool}"]
        preview = pending.get("input_preview")
        if preview:
            try:
                preview_text = json.dumps(preview, ensure_ascii=False, indent=2)
            except TypeError:
                preview_text = str(preview)
            lines.append(preview_text[:2000])
        return "\n".join(lines).strip()

    @staticmethod
    def _task_user_content(description: str, image_paths: list[str]) -> Any:
        if not image_paths:
            return description
        content: list[dict[str, Any]] = [{"type": "text", "text": description}]
        for raw_path in image_paths[:_MAIN_CHAT_IMAGE_ATTACHMENT_LIMIT]:
            path = Path(raw_path)
            try:
                data = path.read_bytes()
            except OSError:
                logger.debug("Native Agent 无法读取图片附件: %s", path, exc_info=True)
                continue
            if len(data) > _MAIN_CHAT_IMAGE_MAX_BYTES:
                logger.warning("Native Agent 跳过过大图片附件: %s", path)
                continue
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            encoded = base64.b64encode(data).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                }
            )
        return content

    @staticmethod
    def _update_processing_message(
        chat_session: Optional["ChatSession"],
        task_id: str,
        content: str,
    ) -> None:
        if chat_session is None:
            return
        try:
            from apps.core.chat_session import MessageStatus

            chat_session.upsert_assistant_message(
                task_id=task_id,
                content=content,
                status=MessageStatus.PROCESSING,
            )
        except Exception:
            logger.debug("Native Agent processing 消息更新失败", exc_info=True)

    def _record_activity(
        self,
        task: TaskInfo,
        title: str,
        detail: str,
        status: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._activity_store().record_event(
                session_id=str(getattr(task, "chat_session_id", "") or ""),
                task_id=task.task_id,
                tool_name="oha.delegation",
                phase="subagent",
                title=title,
                detail=detail,
                status=status,
                metadata=metadata or {},
            )
        except Exception:
            logger.debug("Native Agent 委派活动写入失败", exc_info=True)

    def _activity_store(self) -> "ActivityStore":
        if self._activity_store_getter is not None:
            return self._activity_store_getter()
        from apps.core.activity_store import get_activity_store

        return get_activity_store()

    def _chat_session_for_task(self, task: TaskInfo) -> Optional["ChatSession"]:
        session_id = str(getattr(task, "chat_session_id", "") or "")
        current = self._chat_session
        if not session_id:
            return current
        try:
            from apps.core.chat_session import load_existing_chat_session
            from apps.core.chat_store import get_chat_store

            return load_existing_chat_session(
                get_chat_store(),
                session_id,
                current=current,
                fail_active_messages=False,
            )
        except Exception:
            logger.debug("无法为 Native Agent 任务加载聊天会话: %s", session_id, exc_info=True)
            return None


def _is_key_activity_event(event: dict[str, Any]) -> bool:
    """Keep the durable activity log to meaningful task/tool milestones."""
    phase = str(event.get("phase") or "").strip()
    status = str(event.get("status") or "").strip()
    tool_name = str(event.get("tool_name") or "").strip()
    if phase in {"thinking", "reasoning", "tool_progress"}:
        return False
    if status in {"completed", "failed", "error", "cancelled"}:
        return True
    if phase in {"task_start", "task_complete", "task_failed", "task_cancelled"}:
        return True
    if phase == "subagent":
        return True
    if phase in {"tool_start", "tool_complete"}:
        return bool(tool_name)
    return False


# ── 执行器选择工厂 ────────────────────────────────────────────────────────────

def _native_agent_executor_for_runtime(runtime: "AppRuntime | None" = None) -> NativeAgentExecutor:
    return NativeAgentExecutor(
        chat_session=getattr(runtime, "chat_session", None),
        persona_prompt_getter=(
            (lambda: runtime.config.assistant.persona_prompt)
            if runtime is not None and hasattr(runtime, "config")
            else None
        ),
        user_address_getter=(
            (lambda: runtime.config.assistant.user_address)
            if runtime is not None and hasattr(runtime, "config")
            else None
        ),
        profile_context_getter=(
            (lambda: build_runtime_profile_context(runtime))
            if runtime is not None and hasattr(runtime, "config")
            else None
        ),
        runtime_service_getter=(
            (lambda: runtime.agent_runtime_service)
            if runtime is not None and hasattr(runtime, "agent_runtime_service")
            else None
        ),
        tool_policy_getter=(
            (lambda: runtime.main_chat_tool_policy())
            if runtime is not None and hasattr(runtime, "main_chat_tool_policy")
            else None
        ),
        workspace_policy_getter=(
            (lambda: runtime.main_chat_workspace_policy())
            if runtime is not None and hasattr(runtime, "main_chat_workspace_policy")
            else None
        ),
        activity_store_getter=(
            (lambda: runtime.activity_store)
            if runtime is not None and hasattr(runtime, "activity_store")
            else None
        ),
    )


def select_executor(runtime: "AppRuntime | None" = None) -> ExecutionStrategy:
    """Select the native TaskRunner adapter."""
    try:
        if runtime is not None and hasattr(runtime, "native_agent_readiness"):
            readiness = runtime.native_agent_readiness()
        else:
            from apps.shell.agent_runtime import get_native_agent_readiness

            readiness = get_native_agent_readiness()
    except Exception as exc:
        logger.warning("select_executor: Native Agent readiness 检查失败: %s", exc)
        readiness = {
            "ready": False,
            "reason": "model_profile_unavailable",
            "message": redact_api_error_text(exc),
        }

    if readiness.get("ready"):
        logger.info("select_executor: 选用 NativeAgentExecutor")
        return _native_agent_executor_for_runtime(runtime)

    daily_desktop_executor = None
    if str(readiness.get("reason") or "") in {"model_profile_required", "model_profile_unavailable"}:
        daily_desktop_executor = _native_agent_executor_for_runtime(runtime)

    return NativeAgentUnavailableExecutor(
        str(readiness.get("message") or NATIVE_AGENT_NOT_READY_MESSAGE),
        reason=str(readiness.get("reason") or "model_profile_required"),
        daily_desktop_executor=daily_desktop_executor,
    )


def describe_native_agent_unavailable(runtime: "AppRuntime | None" = None) -> str:
    """返回适合展示给用户的 Native Agent 未就绪原因。"""
    if runtime is not None and hasattr(runtime, "native_agent_readiness"):
        try:
            readiness = runtime.native_agent_readiness()
            message = str(readiness.get("message") or "").strip()
            if message:
                return message
        except Exception:
            logger.debug("读取 Native Agent readiness 失败", exc_info=True)
    return NATIVE_AGENT_NOT_READY_MESSAGE


def user_task_unavailable_reason(runtime: Any) -> str | None:
    """用户任务入口是否应拒绝创建任务；可用时返回 None。"""
    if not hasattr(runtime, "task_runner"):
        return None
    runner = getattr(runtime, "task_runner", None)
    executor = getattr(runner, "executor", None)
    if execution_capabilities(executor).get("model"):
        return None
    reason = str(getattr(executor, "reason", "") or "").strip()
    if reason:
        return reason
    return NATIVE_AGENT_NOT_READY_MESSAGE


def user_task_unavailable_payload(runtime: Any) -> dict[str, str] | None:
    reason = user_task_unavailable_reason(runtime)
    if reason is None:
        return None
    runner = getattr(runtime, "task_runner", None)
    executor = getattr(runner, "executor", None)
    return {
        "code": str(getattr(executor, "code", "") or "native_agent_not_ready"),
        "reason": str(getattr(executor, "reason_code", "") or "model_profile_required"),
        "error": reason,
    }
