"""Exercise the public GroupRun tool-recovery facade, not only its service."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from apps.bridge.routes import yachiyo, yachiyo_studio_group_handlers
from apps.shell.agent_runtime import AgentRuntimeError
from apps.shell.yachiyo_agent import RunTimelineSnapshot


@pytest.mark.asyncio
async def test_group_tool_recovery_route_forwards_payload_and_returns_run(monkeypatch):
    body = yachiyo.RunToolRecoveryActionBody(
        tool_call_id="tool-call-1",
        action_id="recover-1",
        client_run_id="retry-request-1",
        input_override={"path": "report.txt"},
    )
    calls = []

    def start(group_run_id, request):
        calls.append((group_run_id, request))
        return RunTimelineSnapshot(run_id="recovery-run-1", title="Recovery", status="running")

    service = SimpleNamespace(start_group_tool_recovery_action=start)
    monkeypatch.setattr(yachiyo_studio_group_handlers, "studio_service", lambda request: service)

    result = await yachiyo.start_studio_group_run_tool_recovery_action("group-1", body, None)

    assert calls == [("group-1", body)]
    assert result["run_id"] == "recovery-run-1"
    assert result["status"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status",
    [(KeyError("missing"), 404), (AgentRuntimeError("invalid recovery action"), 400)],
)
async def test_group_tool_recovery_route_preserves_structured_errors(monkeypatch, error, status):
    def start(*args):
        raise error

    service = SimpleNamespace(start_group_tool_recovery_action=start)
    monkeypatch.setattr(yachiyo_studio_group_handlers, "studio_service", lambda request: service)
    body = yachiyo.RunToolRecoveryActionBody(tool_call_id="tool-call-1", action_id="recover-1")

    with pytest.raises(HTTPException) as caught:
        await yachiyo.start_studio_group_run_tool_recovery_action("group-1", body, None)

    assert caught.value.status_code == status
