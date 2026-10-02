"""Execution budget guards for native Agent and Workflow runs."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from apps.shell.agent.runtime.errors import AgentRuntimeError


@dataclass(frozen=True)
class RunBudgetLimits:
    max_model_calls: int = 50
    max_tool_calls: int = 100
    max_terminal_calls: int = 50
    max_run_duration_seconds: int = 600
    max_workflow_steps: int = 200
    max_model_output_chars: int = 200_000
    max_tool_output_chars: int = 100_000
    max_context_chars: int = 200_000


    @classmethod
    def from_environment(cls) -> "RunBudgetLimits":
        """Apply an operator-controlled terminal budget without disabling other guards."""
        raw = os.environ.get("OHA_YACHIYO_AGENT_MAX_TERMINAL_CALLS", "").strip()
        try:
            configured = int(raw) if raw else cls().max_terminal_calls
        except ValueError:
            configured = cls().max_terminal_calls
        return cls(max_terminal_calls=max(0, min(configured, 1000)))


@dataclass
class RunBudget:
    limits: RunBudgetLimits
    started_at_epoch: float
    model_calls_used: int = 0
    tool_calls_used: int = 0
    terminal_calls_used: int = 0

    def check_duration(self) -> None:
        elapsed = max(0.0, time.time() - self.started_at_epoch)
        if elapsed > max(1, int(self.limits.max_run_duration_seconds)):
            raise AgentRuntimeError(
                "Run 已超过 "
                f"max_run_duration_seconds={self.limits.max_run_duration_seconds} 的执行预算"
            )

    def check_context(self, context_chars: int) -> None:
        self.check_duration()
        if context_chars > max(1, int(self.limits.max_context_chars)):
            raise AgentRuntimeError(
                f"Run 上下文超过 max_context_chars={self.limits.max_context_chars} 的执行预算"
            )

    def claim_model_call(self) -> None:
        self.check_duration()
        if self.model_calls_used >= max(1, int(self.limits.max_model_calls)):
            raise AgentRuntimeError(
                f"Run 已超过 max_model_calls={self.limits.max_model_calls} 的执行预算"
            )
        self.model_calls_used += 1

    def claim_tool_call(self, tool_name: str, *, terminal_execution: bool = False) -> None:
        self.check_duration()
        if self.tool_calls_used >= max(1, int(self.limits.max_tool_calls)):
            raise AgentRuntimeError(
                f"Run 已超过 max_tool_calls={self.limits.max_tool_calls} 的执行预算"
            )
        if terminal_execution and self.terminal_calls_used >= max(
            0,
            int(self.limits.max_terminal_calls),
        ):
            raise AgentRuntimeError(
                f"Run 已超过 max_terminal_calls={self.limits.max_terminal_calls} 的执行预算"
            )
        self.tool_calls_used += 1
        if terminal_execution:
            self.terminal_calls_used += 1


def json_chars(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def truncate_text(value: Any, max_chars: int) -> tuple[str, bool]:
    text = str(value or "")
    limit = max(1, int(max_chars or 1))
    if len(text) <= limit:
        return text, False
    marker = "\n\n[truncated]"
    if limit <= len(marker):
        return text[:limit], True
    return text[: limit - len(marker)] + marker, True


def limit_json_strings(value: Any, max_chars: int) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        limited: dict[str, Any] = {}
        for key, item in value.items():
            next_item, item_changed = limit_json_strings(item, max_chars)
            limited[str(key)] = next_item
            changed = changed or item_changed
        return limited, changed
    if isinstance(value, list):
        changed = False
        limited_items = []
        for item in value:
            next_item, item_changed = limit_json_strings(item, max_chars)
            limited_items.append(next_item)
            changed = changed or item_changed
        return limited_items, changed
    if isinstance(value, tuple):
        return limit_json_strings(list(value), max_chars)
    if isinstance(value, str):
        return truncate_text(value, max_chars)
    return value, False


def run_budget_from_timeline(
    limits: RunBudgetLimits,
    *,
    started_at_epoch: float,
    timeline: list[dict[str, Any]],
) -> RunBudget:
    model_calls = 0
    tool_calls = 0
    terminal_calls = 0
    for event in timeline:
        if not isinstance(event, dict):
            continue
        event_name = str(event.get("event") or "")
        if event_name in {"agent.model.response", "model.output.completed"}:
            model_calls += 1
        canonical_approval_receipt = bool(
            event.get("approval_resume_result_canonical")
        )
        if (
            event_name
            in {"agent.tool.call", "agent.tool.skipped", "agent.tool.denied"}
            and not canonical_approval_receipt
        ):
            tool_calls += 1
        if (
            event_name == "agent.tool.call"
            and not canonical_approval_receipt
            and str(event.get("detail") or "") == "terminal.run"
        ):
            result = event.get("result") if isinstance(event.get("result"), dict) else {}
            if not result.get("approval_required"):
                terminal_calls += 1
    return RunBudget(
        limits=limits,
        started_at_epoch=started_at_epoch,
        model_calls_used=model_calls,
        tool_calls_used=tool_calls,
        terminal_calls_used=terminal_calls,
    )


def run_budget_factory(
    *,
    limits: RunBudgetLimits | Callable[[], RunBudgetLimits],
    get_run: Callable[[str], dict[str, Any]],
    iso_epoch: Callable[[Any], float],
) -> Callable[[str, list[dict[str, Any]]], RunBudget]:
    def build_run_budget(run_id: str, timeline: list[dict[str, Any]]) -> RunBudget:
        try:
            run = get_run(run_id) if run_id else {}
        except KeyError:
            run = {}
        current_limits = limits() if callable(limits) else limits
        return run_budget_from_timeline(
            current_limits,
            started_at_epoch=iso_epoch(run.get("created_at")),
            timeline=timeline,
        )

    return build_run_budget


def check_context_budget(
    budget: RunBudget,
    messages: list[dict[str, Any]],
    *,
    redact_json_value: Callable[[Any], Any],
) -> None:
    budget.check_context(json_chars(redact_json_value(messages)))


def context_budget_checker(
    *,
    redact_json_value: Callable[[Any], Any],
) -> Callable[[RunBudget, list[dict[str, Any]]], None]:
    def check_runtime_context_budget(
        budget: RunBudget,
        messages: list[dict[str, Any]],
    ) -> None:
        check_context_budget(budget, messages, redact_json_value=redact_json_value)

    return check_runtime_context_budget


def limit_model_output(
    value: Any,
    *,
    limits: RunBudgetLimits,
    redact_text: Callable[[Any], str],
) -> tuple[str, bool]:
    return truncate_text(redact_text(value), limits.max_model_output_chars)


def model_output_limiter(
    *,
    limits: RunBudgetLimits | Callable[[], RunBudgetLimits],
    redact_text: Callable[[Any], str],
) -> Callable[[Any], tuple[str, bool]]:
    def limit_runtime_model_output(value: Any) -> tuple[str, bool]:
        current_limits = limits() if callable(limits) else limits
        return limit_model_output(value, limits=current_limits, redact_text=redact_text)

    return limit_runtime_model_output


def limit_tool_result(
    result: dict[str, Any],
    *,
    limits: RunBudgetLimits,
    redact_json_value: Callable[[Any], Any],
) -> dict[str, Any]:
    limited, truncated = limit_json_strings(
        redact_json_value(result),
        limits.max_tool_output_chars,
    )
    if isinstance(limited, dict) and truncated:
        return {**limited, "truncated": True}
    return limited if isinstance(limited, dict) else {"ok": False, "error": str(limited)}


def tool_result_limiter(
    *,
    limits: RunBudgetLimits | Callable[[], RunBudgetLimits],
    redact_json_value: Callable[[Any], Any],
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def limit_runtime_tool_result(result: dict[str, Any]) -> dict[str, Any]:
        current_limits = limits() if callable(limits) else limits
        return limit_tool_result(
            result,
            limits=current_limits,
            redact_json_value=redact_json_value,
        )

    return limit_runtime_tool_result


@dataclass
class WorkflowRunBudget:
    limits: RunBudgetLimits
    started_at_epoch: float
    steps_used: int = 0

    def check_duration(self) -> None:
        elapsed = max(0.0, time.time() - self.started_at_epoch)
        if elapsed > max(1, int(self.limits.max_run_duration_seconds)):
            raise AgentRuntimeError(
                "Workflow 已超过 "
                f"max_run_duration_seconds={self.limits.max_run_duration_seconds} 的执行预算"
            )

    def check_context(self, context_chars: int) -> None:
        self.check_duration()
        if context_chars > max(1, int(self.limits.max_context_chars)):
            raise AgentRuntimeError(
                "Workflow 上下文超过 "
                f"max_context_chars={self.limits.max_context_chars} 的执行预算"
            )

    def claim_step(self) -> None:
        self.check_duration()
        if self.steps_used >= max(1, int(self.limits.max_workflow_steps)):
            raise AgentRuntimeError(
                f"Workflow 已超过 max_workflow_steps={self.limits.max_workflow_steps} 的执行预算"
            )
        self.steps_used += 1
