"""主动桌面观察服务测试。"""

from __future__ import annotations

import pytest

from apps.core.chat_session import ChatSession, MessageRole, MessageStatus
from apps.core.chat_store import ChatStore
from apps.core.special_sessions import PROACTIVE_CHAT_SESSION_ID, PROACTIVE_CHAT_SESSION_TITLE
from apps.core.state import AppState
import apps.shell.proactive as proactive_mod
from apps.shell.config import AppConfig
from apps.shell.proactive import ProactiveDesktopService, build_proactive_desktop_prompt
from packages.protocol.enums import RiskLevel, TaskStatus, TaskType


class _ExecutorStub:
    name = "NativeAgentExecutor"
    capabilities = {"model": True, "image_input": True, "tools": False, "approval": False}


class _RunnerStub:
    executor = _ExecutorStub()


class _RuntimeStub:
    def __init__(self) -> None:
        self.config = AppConfig()
        self.state = AppState()
        self.chat_session = ChatSession(session_id="proactive-test")
        self.task_runner = _RunnerStub()
        self.ready = True
        self.limited_tools: list[str] = []

    def is_native_agent_ready(self) -> bool:
        return self.ready

    def get_status(self) -> dict:
        return {"native_agent": {"limited_tools": self.limited_tools}}


@pytest.fixture(autouse=True)
def _allow_image_input(monkeypatch):
    monkeypatch.setattr(
        proactive_mod,
        "get_native_image_input_capability",
        lambda: {"can_attach_images": True, "route": "vision_text"},
    )


def _advance_to_first_check(monkeypatch, start: float = 1000.0):
    now = [start]
    monkeypatch.setattr(proactive_mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(proactive_mod.random, "random", lambda: 0.0)

    def fake_capture(target_path):
        target_path.write_bytes(b"fake-png")
        return {
            "path": str(target_path),
            "mime_type": "image/png",
            "format": "png",
            "width": 120,
            "height": 80,
            "size": 8,
        }

    monkeypatch.setattr(proactive_mod, "capture_screenshot_to_file", fake_capture)
    return now


def test_proactive_service_disabled():
    runtime = _RuntimeStub()
    config = AppConfig()
    service = ProactiveDesktopService(runtime, config.bubble_mode)

    state = service.get_state()

    assert state["status"] == "disabled"
    assert state["enabled"] is False
    assert runtime.state.list_tasks() == []


def test_proactive_service_blocks_when_native_agent_not_ready():
    runtime = _RuntimeStub()
    runtime.ready = False
    config = AppConfig()
    config.bubble_mode.proactive_enabled = True
    config.bubble_mode.proactive_desktop_watch_enabled = True
    service = ProactiveDesktopService(runtime, config.bubble_mode)

    state = service.get_state()

    assert state["status"] == "blocked"
    assert "Native Agent" in state["error"]
    assert runtime.state.list_tasks() == []


def test_proactive_service_ignores_native_vision_tool_limit():
    runtime = _RuntimeStub()
    runtime.limited_tools = ["vision"]
    config = AppConfig()
    config.bubble_mode.proactive_enabled = True
    config.bubble_mode.proactive_desktop_watch_enabled = True
    service = ProactiveDesktopService(runtime, config.bubble_mode)

    state = service.get_state()

    assert state["status"] == "waiting"
    assert runtime.state.list_tasks() == []


def test_proactive_service_blocks_when_image_chain_unavailable(monkeypatch):
    monkeypatch.setattr(
        proactive_mod,
        "get_native_image_input_capability",
        lambda: {"can_attach_images": False, "reason": "当前主模型未声明图片输入能力"},
    )
    runtime = _RuntimeStub()
    config = AppConfig()
    config.bubble_mode.proactive_enabled = True
    config.bubble_mode.proactive_desktop_watch_enabled = True
    service = ProactiveDesktopService(runtime, config.bubble_mode)

    state = service.get_state()

    assert state["status"] == "blocked"
    assert "图片识别链路" in state["error"]
    assert runtime.state.list_tasks() == []


def test_proactive_service_waits_minimum_interval_before_first_check(monkeypatch):
    now = _advance_to_first_check(monkeypatch)
    runtime = _RuntimeStub()
    runtime.config.live2d_mode.proactive_enabled = True
    runtime.config.live2d_mode.proactive_desktop_watch_enabled = True
    runtime.config.live2d_mode.proactive_interval_seconds = 60
    service = ProactiveDesktopService(runtime, runtime.config.live2d_mode)

    state = service.get_state()

    assert state["status"] == "waiting"
    assert state["next_check_seconds"] == 300
    assert runtime.state.list_tasks() == []

    now[0] = 1299.0
    assert service.get_state()["status"] == "waiting"
    assert runtime.state.list_tasks() == []

    now[0] = 1300.0
    state = service.get_state()

    assert state["status"] == "scheduled"
    assert runtime.state.list_tasks()[0].task_id == state["task_id"]


def test_proactive_service_can_skip_whole_chain_by_probability(monkeypatch):
    now = _advance_to_first_check(monkeypatch)
    monkeypatch.setattr(proactive_mod.random, "random", lambda: 0.9)
    runtime = _RuntimeStub()
    runtime.config.live2d_mode.proactive_enabled = True
    runtime.config.live2d_mode.proactive_desktop_watch_enabled = True
    runtime.config.live2d_mode.proactive_interval_seconds = 300
    runtime.config.live2d_mode.proactive_trigger_probability = 0.25
    service = ProactiveDesktopService(runtime, runtime.config.live2d_mode)

    now[0] = 1300.0
    state = service.get_state()

    assert state["status"] == "skipped"
    assert state["trigger_probability"] == 0.25
    assert "跳过" in state["message"]
    assert service.last_task_id is None
    assert runtime.state.list_tasks() == []
    assert runtime.chat_session.get_messages() == []


def test_proactive_service_trigger_now_bypasses_wait_and_probability(monkeypatch):
    now = _advance_to_first_check(monkeypatch)
    monkeypatch.setattr(proactive_mod.random, "random", lambda: 0.99)
    runtime = _RuntimeStub()
    runtime.config.live2d_mode.proactive_enabled = True
    runtime.config.live2d_mode.proactive_desktop_watch_enabled = True
    runtime.config.live2d_mode.proactive_interval_seconds = 300
    runtime.config.live2d_mode.proactive_trigger_probability = 0.0
    service = ProactiveDesktopService(runtime, runtime.config.live2d_mode)

    state = service.trigger_now()
    tasks = runtime.state.list_tasks()

    assert now[0] == 1000.0
    assert state["ok"] is True
    assert state["manual"] is True
    assert state["status"] == "scheduled"
    assert state["task_id"] == tasks[0].task_id
    assert tasks[0].task_type == TaskType.SCREENSHOT
    assert len(tasks[0].attachments) == 1


def test_proactive_service_trigger_now_requires_enabled_watch():
    runtime = _RuntimeStub()
    service = ProactiveDesktopService(runtime, runtime.config.bubble_mode)

    state = service.trigger_now()

    assert state["ok"] is False
    assert state["status"] == "disabled"
    assert "启用" in state["error"]
    assert runtime.state.list_tasks() == []


def test_proactive_service_schedules_and_triggers_future_task(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(proactive_mod.time, "time", lambda: now[0])
    runtime = _RuntimeStub()
    runtime.config.bubble_mode.proactive_enabled = True
    runtime.config.bubble_mode.proactive_desktop_watch_enabled = False
    service = ProactiveDesktopService(runtime, runtime.config.bubble_mode)

    created = service.schedule_future_task(
        "Stretch break",
        "提醒用户保存进度并休息一下。",
        delay_seconds=60,
    )
    early = service.trigger_due_future_tasks(now_epoch=1059.0)
    due = service.trigger_due_future_tasks(now_epoch=1060.0)
    tasks = runtime.state.list_tasks()
    messages = runtime.chat_session.get_messages()
    listed = service.list_future_tasks()["future_tasks"]

    assert created["ok"] is True
    assert created["future_task"]["status"] == "scheduled"
    assert created["future_task"]["cron"] == ""
    assert early["triggered"] == []
    assert len(due["triggered"]) == 1
    assert due["triggered"][0]["scheduled"] is False
    assert listed[0]["status"] == "triggered"
    assert tasks[0].task_type == TaskType.GENERAL
    assert tasks[0].risk_level == RiskLevel.LOW
    assert "FutureTask: Stretch break" in tasks[0].description
    assert "提醒用户保存进度" in tasks[0].description
    assert messages[0].content == "已到时间，正在处理：Stretch break"
    assert messages[0].status == MessageStatus.PROCESSING


def test_proactive_service_reschedules_cron_future_task(monkeypatch):
    now = [2000.0]
    monkeypatch.setattr(proactive_mod.time, "time", lambda: now[0])
    runtime = _RuntimeStub()
    runtime.config.live2d_mode.proactive_enabled = True
    runtime.config.live2d_mode.proactive_desktop_watch_enabled = False
    service = ProactiveDesktopService(runtime, runtime.config.live2d_mode)

    created = service.schedule_future_task(
        "Hourly summary",
        "总结最近一小时的工作线索。",
        scheduled_at_epoch=2005.0,
        cron="@hourly",
    )
    due = service.trigger_due_future_tasks(now_epoch=2005.0)
    future_task = service.list_future_tasks()["future_tasks"][0]

    assert created["future_task"]["status"] == "scheduled"
    assert created["future_task"]["cron"] == "@hourly"
    assert len(due["triggered"]) == 1
    assert due["triggered"][0]["scheduled"] is True
    assert future_task["status"] == "scheduled"
    assert future_task["run_count"] == 1
    assert future_task["last_run_task_id"] == runtime.state.list_tasks()[0].task_id
    assert future_task["scheduled_at_epoch"] == 5605.0


def test_proactive_service_creates_low_risk_screenshot_task(monkeypatch):
    now = _advance_to_first_check(monkeypatch)
    runtime = _RuntimeStub()
    runtime.config.live2d_mode.proactive_enabled = True
    runtime.config.live2d_mode.proactive_desktop_watch_enabled = True
    runtime.config.live2d_mode.proactive_interval_seconds = 300
    service = ProactiveDesktopService(runtime, runtime.config.live2d_mode)

    now[0] = 1300.0
    state = service.get_state()
    tasks = runtime.state.list_tasks()

    assert state["status"] == "scheduled"
    assert state["task_id"] == tasks[0].task_id
    assert tasks[0].task_type == TaskType.SCREENSHOT
    assert tasks[0].risk_level == RiskLevel.LOW
    assert len(tasks[0].attachments) == 1
    assert tasks[0].attachments[0]["kind"] == "image"
    assert tasks[0].attachments[0]["mime_type"] == "image/png"
    assert "详细对话框阅读" in tasks[0].response_context
    assert "TTS 会在播报前另行压缩" in tasks[0].response_context
    assert service.last_task_id == tasks[0].task_id

    messages = runtime.chat_session.get_messages()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.ASSISTANT
    assert messages[0].status == MessageStatus.PROCESSING
    assert messages[0].task_id == tasks[0].task_id
    assert messages[0].content == "正在查看当前状态。"
    assert messages[0].attachments == []
    assert "主动桌面观察" not in messages[0].content
    assert "输出约束" not in messages[0].content


def test_proactive_service_uses_dedicated_session_when_store_is_available(monkeypatch, tmp_path):
    now = _advance_to_first_check(monkeypatch)
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _RuntimeStub()
    runtime.store = store
    runtime.chat_session.attach_store(store, load_existing=False)
    runtime.config.live2d_mode.proactive_enabled = True
    runtime.config.live2d_mode.proactive_desktop_watch_enabled = True
    runtime.config.live2d_mode.proactive_interval_seconds = 300
    service = ProactiveDesktopService(runtime, runtime.config.live2d_mode)

    try:
        now[0] = 1300.0
        state = service.get_state()

        assert state["status"] == "scheduled"
        assert state["session_id"] == PROACTIVE_CHAT_SESSION_ID
        assert runtime.chat_session.get_messages() == []

        proactive_session = ChatSession(session_id=PROACTIVE_CHAT_SESSION_ID)
        proactive_session.attach_store(store, load_existing=True)
        messages = proactive_session.get_messages()
        stored = store.get_session(PROACTIVE_CHAT_SESSION_ID)

        assert stored is not None
        assert stored.title == PROACTIVE_CHAT_SESSION_TITLE
        assert len(messages) == 1
        assert messages[0].role == MessageRole.ASSISTANT
        assert messages[0].task_id == state["task_id"]
        assert runtime.state.get_task(state["task_id"]).chat_session_id == PROACTIVE_CHAT_SESSION_ID
    finally:
        store.close()


def test_proactive_service_records_local_screenshot_failure_without_running_native_agent(monkeypatch):
    now = _advance_to_first_check(monkeypatch)

    def fake_capture(_target_path):
        raise RuntimeError("当前后端进程没有 macOS 屏幕录制权限")

    monkeypatch.setattr(proactive_mod, "capture_screenshot_to_file", fake_capture)
    runtime = _RuntimeStub()
    runtime.config.live2d_mode.proactive_enabled = True
    runtime.config.live2d_mode.proactive_desktop_watch_enabled = True
    runtime.config.live2d_mode.proactive_interval_seconds = 300
    service = ProactiveDesktopService(runtime, runtime.config.live2d_mode)

    now[0] = 1300.0
    state = service.get_state()
    tasks = runtime.state.list_tasks()

    assert state["status"] == "failed"
    assert state["task_id"] == tasks[0].task_id
    assert "屏幕录制权限" in state["error"]
    assert tasks[0].status == TaskStatus.FAILED
    assert tasks[0].attachments == []
    assert "本地截图捕获失败" in tasks[0].description

    messages = runtime.chat_session.get_messages()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.ASSISTANT
    assert messages[0].status == MessageStatus.FAILED
    assert messages[0].task_id == tasks[0].task_id
    assert messages[0].attachments == []
    assert "主动桌面观察暂时无法读取截图" in messages[0].content
    assert "输出约束" not in messages[0].content


def test_build_proactive_desktop_prompt_keeps_detailed_message_constraints():
    runtime = _RuntimeStub()

    prompt = build_proactive_desktop_prompt(runtime)

    assert "详细对话框阅读" in prompt
    assert "TTS 会在播报前另行压缩" in prompt


def test_proactive_service_completed_state_exposes_observation_text(monkeypatch):
    now = _advance_to_first_check(monkeypatch)
    runtime = _RuntimeStub()
    runtime.config.bubble_mode.proactive_enabled = True
    runtime.config.bubble_mode.proactive_desktop_watch_enabled = True
    service = ProactiveDesktopService(runtime, runtime.config.bubble_mode)
    now[0] = 1300.0
    scheduled = service.get_state()

    runtime.state.update_task_status(
        scheduled["task_id"],
        TaskStatus.COMPLETED,
        result="你已经连续处理工具配置很久了，建议先保存进度再休息一下。",
    )
    state = service.get_state()

    assert state["status"] == "completed"
    assert state["has_attention"] is True
    assert state["attention_source"] == "proactive_desktop_watch"
    assert state["attention_text"] == "你已经连续处理工具配置很久了，建议先保存进度再休息一下。"
    assert state["message"] == state["attention_text"]


def test_proactive_service_reports_failed_before_retry_interval(monkeypatch):
    now = _advance_to_first_check(monkeypatch)
    runtime = _RuntimeStub()
    config = AppConfig()
    config.bubble_mode.proactive_enabled = True
    config.bubble_mode.proactive_desktop_watch_enabled = True
    config.bubble_mode.proactive_interval_seconds = 300
    service = ProactiveDesktopService(runtime, config.bubble_mode)

    now[0] = 1300.0
    scheduled = service.get_state()
    runtime.state.update_task_status(scheduled["task_id"], TaskStatus.FAILED, error="vision failed")
    now[0] = 1400.0

    state = service.get_state()

    assert state["status"] == "failed"
    assert state["task_id"] == scheduled["task_id"]
    assert state["error"] == "vision failed"
    assert len(runtime.state.list_tasks()) == 1


def test_proactive_service_retries_failed_task_after_interval(monkeypatch):
    now = _advance_to_first_check(monkeypatch)
    runtime = _RuntimeStub()
    config = AppConfig()
    config.live2d_mode.proactive_enabled = True
    config.live2d_mode.proactive_desktop_watch_enabled = True
    config.live2d_mode.proactive_interval_seconds = 300
    service = ProactiveDesktopService(runtime, config.live2d_mode)

    now[0] = 1300.0
    scheduled = service.get_state()
    runtime.state.update_task_status(scheduled["task_id"], TaskStatus.FAILED, error="vision failed")
    now[0] = 1601.0

    failed = service.get_state()
    assert failed["status"] == "failed"
    assert failed["task_id"] == scheduled["task_id"]

    now[0] = 1602.0
    state = service.get_state()
    tasks = runtime.state.list_tasks()

    assert state["status"] == "scheduled"
    assert state["task_id"] != scheduled["task_id"]
    assert service.last_task_id == state["task_id"]
    assert len(tasks) == 2
    assert tasks[-1].task_type == TaskType.SCREENSHOT
    assert tasks[-1].risk_level == RiskLevel.LOW
