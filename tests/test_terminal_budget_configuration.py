"""Coding Run limits stay finite while permitting more than ten terminal calls."""

import time

import pytest

from apps.shell.agent.runtime.budget import RunBudget, RunBudgetLimits, run_budget_from_timeline
from apps.shell.agent.runtime.engine_state import build_runtime_engine_state
from apps.shell.agent.runtime.errors import AgentRuntimeError
from apps.shell.agent.runtime.tool_requests import agent_tool_iteration_limit
from apps.shell.credential_store import MemoryCredentialStore


def test_default_budget_allows_eleven_terminal_executions_but_enforces_cap():
    budget = RunBudget(RunBudgetLimits(), started_at_epoch=time.time())
    for _ in range(50):
        budget.claim_tool_call("terminal.run", terminal_execution=True)
    assert budget.terminal_calls_used == 50
    with pytest.raises(AgentRuntimeError, match="max_terminal_calls=50"):
        budget.claim_tool_call("terminal.run", terminal_execution=True)


@pytest.mark.parametrize("raw,expected", [("", 50), ("invalid", 50), ("0", 0), ("11", 11), ("-1", 0), ("1001", 1000)])
def test_terminal_budget_setting_is_bounded(monkeypatch, raw, expected):
    monkeypatch.setenv("OHA_YACHIYO_AGENT_MAX_TERMINAL_CALLS", raw)
    limits = RunBudgetLimits.from_environment()
    assert limits.max_terminal_calls == expected
    assert limits.max_tool_calls == 100
    assert limits.max_model_calls == 50
    assert limits.max_run_duration_seconds == 600


def test_runtime_uses_configured_budget_and_restores_counts_from_timeline(tmp_path, monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_AGENT_MAX_TERMINAL_CALLS", "1")
    credentials = MemoryCredentialStore()
    state = build_runtime_engine_state(
        db_path=tmp_path / "runtime.db", workspace_dir=tmp_path / "runtime", credential_store=credentials,
    )
    try:
        budget = run_budget_from_timeline(
            state.runtime_limits, started_at_epoch=time.time(), timeline=[
                {"event": "agent.tool.call", "detail": "terminal.run", "result": {"ok": True}},
                {"event": "agent.tool.call", "detail": "terminal.run", "result": {"approval_required": True}},
            ],
        )
        assert budget.terminal_calls_used == 1
        with pytest.raises(AgentRuntimeError, match="max_terminal_calls=1"):
            budget.claim_tool_call("terminal.run", terminal_execution=True)
    finally:
        state.conn.close()
        credentials.close()


@pytest.mark.parametrize("raw,expected", [("", 200), ("invalid", 200), ("1", 10), ("275", 275), ("1001", 1000)])
def test_iteration_limit_accepts_legacy_setting(monkeypatch, raw, expected):
    monkeypatch.delenv("OHA_YACHIYO_AGENT_TOOL_ITERATION_LIMIT", raising=False)
    monkeypatch.setenv("HERMES_AGENT_TOOL_ITERATION_LIMIT", raw)
    assert agent_tool_iteration_limit() == expected


def test_current_iteration_limit_setting_takes_precedence(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_TOOL_ITERATION_LIMIT", "150")
    monkeypatch.setenv("OHA_YACHIYO_AGENT_TOOL_ITERATION_LIMIT", "250")
    assert agent_tool_iteration_limit() == 250
