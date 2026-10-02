"""Tool request parsing for model messages."""

from __future__ import annotations

import json
import os
import re
from typing import Any
from uuid import uuid4

from apps.shell.agent.runtime.errors import AgentRuntimeError
from apps.shell.agent.tools.policy import TOOL_NAME_ALIASES


def agent_tool_iteration_limit() -> int:
    raw = str(
        os.environ.get("OHA_YACHIYO_AGENT_TOOL_ITERATION_LIMIT")
        or os.environ.get("HERMES_AGENT_TOOL_ITERATION_LIMIT")
        or ""
    ).strip()
    try:
        configured = int(raw) if raw else 200
    except ValueError:
        configured = 200
    return max(10, min(configured, 1000))


MAX_AGENT_TOOL_ITERATIONS = agent_tool_iteration_limit()
_BROWSER_TYPE_TEXT_INPUT_KEYS = ("selector", "text")


def normalize_tool_name(value: Any) -> str:
    name = str(value or "").strip()
    return TOOL_NAME_ALIASES.get(name, name)


def normalize_tool_request_input(tool_request: dict[str, Any]) -> dict[str, Any]:
    """Remove retired browser input fields before validation or persistence."""

    if normalize_tool_name(tool_request.get("tool")) != "browser.type_text":
        return tool_request
    raw_input = tool_request.get("input")
    if not isinstance(raw_input, dict):
        return tool_request
    tool_request["input"] = {
        key: raw_input[key]
        for key in _BROWSER_TYPE_TEXT_INPUT_KEYS
        if key in raw_input
    }
    return tool_request


def ensure_tool_call_id(tool_request: dict[str, Any]) -> str:
    """Keep an upstream call identity or assign one for this logical invocation."""

    for key in ("tool_call_id", "call_id", "id"):
        tool_call_id = str(tool_request.get(key) or "").strip()
        if tool_call_id:
            tool_request["tool_call_id"] = tool_call_id
            return tool_call_id
    tool_call_id = f"call_{uuid4().hex}"
    tool_request["tool_call_id"] = tool_call_id
    return tool_call_id


def normalize_tool_iteration(
    value: Any,
    *,
    max_iterations: int = MAX_AGENT_TOOL_ITERATIONS,
) -> int:
    try:
        iteration = int(value or 0)
    except (TypeError, ValueError):
        iteration = 0
    return max(0, min(iteration, max_iterations))


class ToolRequestParser:
    """Parses native tool_calls and JSON fallback tool requests."""

    def requests_from_message(self, message: dict[str, Any], content: str) -> list[dict[str, Any]]:
        native = self.parse_tool_calls(message.get("tool_calls"))
        if native:
            return native
        fallback = self.parse_json_fallback(content)
        return [fallback] if fallback else []

    def parse_tool_calls(self, tool_calls: Any) -> list[dict[str, Any]]:
        if not isinstance(tool_calls, list):
            return []
        requests = []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            function_name = str(function.get("name") or "").strip()
            if not function_name:
                continue
            raw_arguments = function.get("arguments") or "{}"
            if isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(raw_arguments or "{}")
                except json.JSONDecodeError as exc:
                    raise AgentRuntimeError(f"工具参数不是合法 JSON：{function_name}") from exc
            elif isinstance(raw_arguments, dict):
                arguments = raw_arguments
            else:
                raise AgentRuntimeError(f"工具参数格式无效：{function_name}")
            if not isinstance(arguments, dict):
                raise AgentRuntimeError(f"工具参数必须是对象：{function_name}")
            request = {
                "protocol": "tool_calls",
                "tool": normalize_tool_name(function_name),
                "input": arguments,
                "function_name": function_name,
            }
            for key in ("tool_call_id", "call_id", "id"):
                value = str(call.get(key) or "").strip()
                if value:
                    request["tool_call_id"] = value
                    break
            ensure_tool_call_id(request)
            requests.append(request)
        return requests

    def parse_json_fallback(self, content: str) -> dict[str, Any] | None:
        clean = content.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.DOTALL).strip()
        try:
            payload = json.loads(clean)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("action") == "tool" and payload.get("tool"):
            # JSON fallback is model-authored input.  Keep the same strict
            # boundary as native tool_calls: route, policy, provider, approval,
            # and runtime identity fields are assigned only by the runtime.
            request = {
                "protocol": "json_fallback",
                "tool": normalize_tool_name(payload.get("tool")),
                "input": (
                    dict(payload.get("input"))
                    if isinstance(payload.get("input"), dict)
                    else {}
                ),
            }
            ensure_tool_call_id(request)
            return request
        return None
