"""共享主动桌面观察服务。"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from apps.core.chat_session import ChatSession, MessageStatus
from apps.core.executor import execution_capabilities
from apps.core.special_sessions import PROACTIVE_CHAT_SESSION_ID, PROACTIVE_CHAT_SESSION_TITLE
from apps.locald.screenshot import capture_screenshot_to_file
from apps.shell.chat_api import (
    allocate_chat_attachment_path,
    chat_attachment_record,
)
from apps.shell.native_capabilities import get_native_image_input_capability
from packages.protocol.enums import RiskLevel, TaskStatus, TaskType

if TYPE_CHECKING:
    from apps.core.runtime import AppRuntime

logger = logging.getLogger(__name__)

_DESKTOP_WATCH_PROMPT = (
    "主动桌面观察：请查看用户当前桌面状态。若本轮附加了屏幕截图，请直接基于附件图片判断；"
    "这张截图会通过与对话窗口图片附件相同的图片识别链路传入，"
    "不要改用桌面截图/视觉工具替代附件；"
    "生成一段适合在详细对话框阅读的主动关怀消息，说明是否有需要提醒用户的事项；"
    "如果当前模型或工具无法读取截图，"
    "请明确说明缺少的多模态/vision 能力。"
)
_MIN_PROACTIVE_INTERVAL_SECONDS = 300
_DESKTOP_WATCH_VISIBLE_MESSAGE = "正在查看当前状态。"


@dataclass
class FutureTask:
    """A lightweight proactive self-wakeup scheduled by the Agent layer."""

    future_task_id: str
    title: str
    prompt: str
    scheduled_at_epoch: float
    cron: str = ""
    status: str = "scheduled"
    created_at_epoch: float = 0.0
    last_run_task_id: str = ""
    run_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "future_task_id": self.future_task_id,
            "title": self.title,
            "prompt": self.prompt,
            "scheduled_at_epoch": self.scheduled_at_epoch,
            "cron": self.cron,
            "status": self.status,
            "created_at_epoch": self.created_at_epoch,
            "last_run_task_id": self.last_run_task_id,
            "run_count": self.run_count,
        }


def get_proactive_chat_session(runtime: Any) -> ChatSession | Any | None:
    """Return the dedicated proactive chat session without changing current UI state."""
    current = getattr(runtime, "chat_session", None)
    if current is not None and getattr(current, "session_id", "") == PROACTIVE_CHAT_SESSION_ID:
        _ensure_proactive_session_title(current)
        return current

    store = getattr(current, "_store", None)
    if store is None:
        store = getattr(runtime, "store", None)
    if store is None:
        return current

    cached = getattr(runtime, "_proactive_chat_session", None)
    if (
        cached is not None
        and getattr(cached, "session_id", "") == PROACTIVE_CHAT_SESSION_ID
        and getattr(cached, "_store", None) is store
    ):
        _ensure_proactive_session_title(cached)
        return cached

    session = ChatSession(session_id=PROACTIVE_CHAT_SESSION_ID)
    session.attach_store(store, load_existing=True)
    _ensure_proactive_session_title(session)
    try:
        setattr(runtime, "_proactive_chat_session", session)
    except Exception:
        logger.debug("主动关怀会话缓存失败", exc_info=True)
    return session


def _ensure_proactive_session_title(chat_session: Any | None) -> None:
    if chat_session is None:
        return
    try:
        chat_session.set_session_title(PROACTIVE_CHAT_SESSION_TITLE)
    except Exception:
        logger.debug("主动关怀会话标题写入失败", exc_info=True)


def build_proactive_desktop_prompt(runtime: Any | None = None) -> str:
    """Build the desktop-watch prompt used for the detailed proactive message."""
    return (
        f"{_DESKTOP_WATCH_PROMPT}\n\n"
        "输出约束："
        "请输出 2-4 句自然中文，保持八千代人设，适合用户稍后打开详细对话框阅读；"
        "如果没有明确需要提醒的事项，就给一段轻量问候和状态确认，不要展开桌面隐私细节。"
        "TTS 会在播报前另行压缩成短语音，不要为了语音而牺牲详细消息的完整性。"
    )


class ProactiveDesktopService:
    """Bubble / Live2D 共享的主动桌面观察状态机。"""

    def __init__(self, runtime: "AppRuntime", mode_config: Any) -> None:
        self._runtime = runtime
        self._mode_config = mode_config
        self._last_check_at = time.monotonic()
        self._last_task_id: str | None = None
        self._attention_task_id: str | None = None
        self._acknowledged_task_id: str | None = None
        self._reported_failed_task_id: str | None = None
        self._future_tasks: dict[str, FutureTask] = {}

    @property
    def last_task_id(self) -> str | None:
        return self._last_task_id

    @property
    def session_id(self) -> str:
        return PROACTIVE_CHAT_SESSION_ID

    def acknowledge(self) -> None:
        """确认当前主动观察提示，清除 attention 状态。"""
        if self._attention_task_id:
            self._acknowledged_task_id = self._attention_task_id
        self._attention_task_id = None

    def schedule_future_task(
        self,
        title: str,
        prompt: str,
        *,
        delay_seconds: int | float | None = None,
        scheduled_at_epoch: int | float | None = None,
        cron: str = "",
    ) -> dict[str, Any]:
        """Schedule a low-risk FutureTask self-wakeup for the proactive session."""
        clean_title = str(title or "").strip() or "Future task"
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            raise ValueError("FutureTask prompt 不能为空")
        now = time.time()
        clean_cron = str(cron or "").strip()
        if clean_cron and scheduled_at_epoch is not None:
            self._next_cron_epoch(clean_cron, now)
        scheduled_at = self._coerce_future_task_epoch(
            now,
            delay_seconds=delay_seconds,
            scheduled_at_epoch=scheduled_at_epoch,
            cron=clean_cron,
        )
        future_task = FutureTask(
            future_task_id=f"future_{uuid4().hex[:12]}",
            title=clean_title,
            prompt=clean_prompt,
            scheduled_at_epoch=scheduled_at,
            cron=clean_cron,
            status="scheduled",
            created_at_epoch=now,
        )
        self._future_tasks[future_task.future_task_id] = future_task
        return {"ok": True, "future_task": future_task.to_dict()}

    def list_future_tasks(self) -> dict[str, Any]:
        """List scheduled and triggered FutureTask entries for UI/bridge surfaces."""
        tasks = sorted(
            self._future_tasks.values(),
            key=lambda item: (item.scheduled_at_epoch, item.created_at_epoch, item.future_task_id),
        )
        return {"ok": True, "future_tasks": [task.to_dict() for task in tasks]}

    def trigger_due_future_tasks(self, *, now_epoch: int | float | None = None) -> dict[str, Any]:
        """Create product Tasks for every due FutureTask and reschedule cron entries."""
        now = float(now_epoch if now_epoch is not None else time.time())
        triggered: list[dict[str, Any]] = []
        for future_task in sorted(self._future_tasks.values(), key=lambda item: item.scheduled_at_epoch):
            if future_task.status != "scheduled" or future_task.scheduled_at_epoch > now:
                continue
            product_task = self._create_future_task_run(future_task)
            future_task.last_run_task_id = product_task.task_id
            future_task.run_count += 1
            if future_task.cron:
                future_task.scheduled_at_epoch = self._next_cron_epoch(future_task.cron, now)
                future_task.status = "scheduled"
            else:
                future_task.status = "triggered"
            triggered.append(
                {
                    "future_task": future_task.to_dict(),
                    "task_id": product_task.task_id,
                    "scheduled": bool(future_task.cron),
                }
            )
        return {"ok": True, "triggered": triggered}

    def trigger_now(self) -> dict[str, Any]:
        """立即安排一次主动桌面观察，跳过间隔和触发概率。"""
        enabled = bool(getattr(self._mode_config, "proactive_enabled", False))
        desktop_watch_enabled = bool(
            getattr(self._mode_config, "proactive_desktop_watch_enabled", False)
        )
        if not enabled or not desktop_watch_enabled:
            return {
                "session_id": self.session_id,
                "enabled": enabled,
                "desktop_watch_enabled": desktop_watch_enabled,
                "status": "disabled" if not enabled else "idle",
                "has_attention": False,
                "ok": False,
                "error": "请先启用并保存主动桌面观察后再测试",
            }

        blocker = self._desktop_watch_blocker()
        if blocker:
            self._reset_wait_baseline()
            return {
                "session_id": self.session_id,
                "enabled": True,
                "desktop_watch_enabled": True,
                "status": "blocked",
                "has_attention": False,
                "ok": False,
                "error": blocker,
            }

        task = self._current_task()
        if task is not None and task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            return {
                "session_id": self.session_id,
                "enabled": True,
                "desktop_watch_enabled": True,
                "status": task.status.value,
                "has_attention": False,
                "ok": True,
                "task_id": task.task_id,
                "message": "已有主动桌面观察正在进行",
            }

        task_id = self._schedule_desktop_watch_task()
        state = self._state_after_schedule(task_id)
        return {
            **state,
            "ok": state.get("status") != "failed",
            "manual": True,
            "message": state.get("message") or "已立即安排主动桌面观察",
        }

    def get_state(self) -> dict[str, Any]:
        """返回当前主动观察状态，并在满足间隔时创建低风险截图任务。"""
        enabled = bool(getattr(self._mode_config, "proactive_enabled", False))
        desktop_watch_enabled = bool(
            getattr(self._mode_config, "proactive_desktop_watch_enabled", False)
        )
        future_task_result = (
            self.trigger_due_future_tasks()
            if enabled
            else {"ok": True, "triggered": []}
        )
        if not enabled:
            self._reset_wait_baseline()
            return {
                "session_id": self.session_id,
                "enabled": False,
                "desktop_watch_enabled": desktop_watch_enabled,
                "status": "disabled",
                "has_attention": False,
                "message": "主动关怀已关闭",
                "future_task": future_task_result,
            }

        if not desktop_watch_enabled:
            self._reset_wait_baseline()
            return {
                "session_id": self.session_id,
                "enabled": True,
                "desktop_watch_enabled": False,
                "status": "idle",
                "has_attention": False,
                "message": "主动关怀已开启，桌面观察未开启",
                "future_task": future_task_result,
            }

        blocker = self._desktop_watch_blocker()
        if blocker:
            self._reset_wait_baseline()
            return {
                "session_id": self.session_id,
                "enabled": True,
                "desktop_watch_enabled": True,
                "status": "blocked",
                "has_attention": False,
                "error": blocker,
            }

        now = time.monotonic()
        interval = self._interval_seconds()
        task = self._current_task()
        if task is not None:
            if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                return {
                    "session_id": self.session_id,
                    "enabled": True,
                    "desktop_watch_enabled": True,
                    "status": task.status.value,
                    "has_attention": False,
                    "task_id": task.task_id,
                    "message": "正在进行主动桌面观察",
                }
            if task.status == TaskStatus.COMPLETED:
                has_attention = self._acknowledged_task_id != task.task_id
                if has_attention:
                    self._attention_task_id = task.task_id
                elif now - self._last_check_at >= interval:
                    return self._maybe_schedule_after_interval()
                result = str(getattr(task, "result", "") or "").strip()
                self._upsert_proactive_message(
                    task.task_id,
                    result or "[主动观察已完成，无输出]",
                    MessageStatus.COMPLETED,
                )
                attention_text = _compact_attention_text(result)
                return {
                    "session_id": self.session_id,
                    "enabled": True,
                    "desktop_watch_enabled": True,
                    "status": "completed",
                    "has_attention": has_attention,
                    "task_id": task.task_id,
                    "message": attention_text if has_attention and attention_text else "主动观察结果已查看",
                    "result": result,
                    "attention_text": attention_text,
                    "attention_source": "proactive_desktop_watch",
                }
            if task.status == TaskStatus.FAILED:
                if (
                    self._reported_failed_task_id == task.task_id
                    and now - self._last_check_at >= interval
                ):
                    return self._maybe_schedule_after_interval()
                self._reported_failed_task_id = task.task_id
                self._upsert_proactive_message(
                    task.task_id,
                    task.error or "主动桌面观察失败",
                    MessageStatus.FAILED,
                    error=task.error or "主动桌面观察失败",
                )
                return {
                    "session_id": self.session_id,
                    "enabled": True,
                    "desktop_watch_enabled": True,
                    "status": "failed",
                    "has_attention": False,
                    "task_id": task.task_id,
                    "error": task.error or "主动桌面观察失败",
                }

        if now - self._last_check_at >= interval:
            return self._maybe_schedule_after_interval()

        return {
            "session_id": self.session_id,
            "enabled": True,
            "desktop_watch_enabled": True,
            "status": "waiting",
            "has_attention": False,
            "next_check_seconds": int(interval - (now - self._last_check_at)),
        }

    def _interval_seconds(self) -> int:
        try:
            value = int(getattr(self._mode_config, "proactive_interval_seconds", 300) or 300)
        except (TypeError, ValueError):
            value = 300
        return max(_MIN_PROACTIVE_INTERVAL_SECONDS, value)

    def _reset_wait_baseline(self) -> None:
        self._last_check_at = time.monotonic()

    def _trigger_probability(self) -> float:
        try:
            value = float(getattr(self._mode_config, "proactive_trigger_probability", 0.6))
        except (TypeError, ValueError):
            value = 0.6
        return max(0.0, min(1.0, value))

    def _coerce_future_task_epoch(
        self,
        now: float,
        *,
        delay_seconds: int | float | None,
        scheduled_at_epoch: int | float | None,
        cron: str,
    ) -> float:
        if scheduled_at_epoch is not None:
            try:
                return max(now, float(scheduled_at_epoch))
            except (TypeError, ValueError):
                raise ValueError("scheduled_at_epoch 必须是 Unix epoch 秒数") from None
        if delay_seconds is not None:
            try:
                return now + max(0.0, float(delay_seconds))
            except (TypeError, ValueError):
                raise ValueError("delay_seconds 必须是数字") from None
        if cron:
            return self._next_cron_epoch(cron, now)
        return now

    @staticmethod
    def _next_cron_epoch(cron: str, now: float) -> float:
        clean = str(cron or "").strip().lower()
        if clean == "@hourly":
            return now + 3600
        if clean == "@daily":
            return now + 86400
        if clean == "@weekly":
            return now + 7 * 86400
        match = re.fullmatch(r"every\s+(\d+)\s+(minute|minutes|hour|hours|day|days)", clean)
        if not match:
            raise ValueError("cron 目前支持 @hourly、@daily、@weekly 或 every N minutes/hours/days")
        amount = max(1, int(match.group(1)))
        unit = match.group(2)
        if unit.startswith("minute"):
            return now + amount * 60
        if unit.startswith("hour"):
            return now + amount * 3600
        return now + amount * 86400

    def _create_future_task_run(self, future_task: FutureTask):
        chat_session = get_proactive_chat_session(self._runtime)
        prompt = (
            f"FutureTask: {future_task.title}\n\n"
            f"{future_task.prompt}\n\n"
            "请以主动任务的身份处理这件事；如果需要用户继续确认，给出简短、可操作的下一步。"
        )
        task = self._runtime.state.create_task(
            prompt,
            task_type=TaskType.GENERAL,
            risk_level=RiskLevel.LOW,
            attachments=[],
            chat_session_id=getattr(chat_session, "session_id", None),
        )
        if chat_session is not None:
            try:
                chat_session.upsert_assistant_message(
                    task_id=task.task_id,
                    content=f"已到时间，正在处理：{future_task.title}",
                    status=MessageStatus.PROCESSING,
                    attachments=[],
                )
            except Exception:
                logger.debug("FutureTask 写入主动会话消息失败", exc_info=True)
        return task

    @staticmethod
    def _scheduled_state(task_id: str) -> dict[str, Any]:
        return {
            "enabled": True,
            "session_id": PROACTIVE_CHAT_SESSION_ID,
            "desktop_watch_enabled": True,
            "status": "scheduled",
            "has_attention": False,
            "task_id": task_id,
            "message": "已安排主动桌面观察",
        }

    def _state_after_schedule(self, task_id: str) -> dict[str, Any]:
        task = self._runtime.state.get_task(task_id)
        if task is not None and task.status == TaskStatus.FAILED:
            return {
                "session_id": self.session_id,
                "enabled": True,
                "desktop_watch_enabled": True,
                "status": "failed",
                "has_attention": False,
                "task_id": task_id,
                "error": task.error or "主动桌面观察失败",
            }
        return self._scheduled_state(task_id)

    def _maybe_schedule_after_interval(self) -> dict[str, Any]:
        probability = self._trigger_probability()
        if probability <= 0 or random.random() > probability:
            self._last_check_at = time.monotonic()
            self._last_task_id = None
            self._attention_task_id = None
            self._reported_failed_task_id = None
            return {
                "session_id": self.session_id,
                "enabled": True,
                "desktop_watch_enabled": True,
                "status": "skipped",
                "has_attention": False,
                "message": "本轮主动关怀按触发概率跳过",
                "trigger_probability": probability,
                "next_check_seconds": self._interval_seconds(),
            }
        task_id = self._schedule_desktop_watch_task()
        return self._state_after_schedule(task_id)

    def _current_task(self):
        if not self._last_task_id:
            return None
        return self._runtime.state.get_task(self._last_task_id)

    def _desktop_watch_blocker(self) -> str | None:
        try:
            readiness = getattr(self._runtime, "native_agent_readiness", None)
            ready = (
                bool(readiness().get("ready"))
                if callable(readiness)
                else bool(self._runtime.is_native_agent_ready())
            )
            if not ready:
                return "主动桌面观察需要 Native Agent 就绪"
        except Exception:
            return "主动桌面观察需要 Native Agent 就绪"

        runner = getattr(self._runtime, "task_runner", None)
        if runner is None:
            return "任务执行器尚未启动，暂时无法进行主动桌面观察"

        executor = getattr(runner, "executor", None)
        if not execution_capabilities(executor).get("image_input"):
            return "主动桌面观察需要支持图片输入的 Native Agent 执行器"

        try:
            image_input = get_native_image_input_capability()
        except Exception:
            logger.debug("主动桌面观察读取 Yachiyo 图片链路能力失败", exc_info=True)
            return None
        if image_input.get("can_attach_images") is False:
            reason = str(image_input.get("reason") or "当前 Yachiyo 图片链路不可用")
            return f"主动桌面观察需要可用的图片识别链路：{reason}"
        return None

    def _schedule_desktop_watch_task(self) -> str:
        chat_session = get_proactive_chat_session(self._runtime)
        attachments, screenshot_error = self._capture_desktop_attachments(chat_session)
        prompt = build_proactive_desktop_prompt(self._runtime)
        if screenshot_error:
            return self._record_screenshot_failure(prompt, screenshot_error, chat_session)
        task = self._runtime.state.create_task(
            "Describe the supplied context and recommend next steps.",
            task_type=TaskType.SCREENSHOT,
            risk_level=RiskLevel.LOW,
            attachments=attachments,
            chat_session_id=getattr(chat_session, "session_id", None),
            response_context=prompt,
        )

        if chat_session is not None:
            try:
                chat_session.upsert_assistant_message(
                    task_id=task.task_id,
                    content=_DESKTOP_WATCH_VISIBLE_MESSAGE,
                    status=MessageStatus.PROCESSING,
                    attachments=[],
                )
            except Exception:
                logger.debug("主动桌面观察写入聊天消息失败", exc_info=True)

        self._last_task_id = task.task_id
        self._attention_task_id = None
        self._reported_failed_task_id = None
        self._last_check_at = time.monotonic()
        return task.task_id

    def _record_screenshot_failure(self, prompt: str, screenshot_error: str, chat_session: Any | None) -> str:
        task = self._runtime.state.create_task(
            f"{prompt}\n\n本地截图捕获失败：{screenshot_error}",
            task_type=TaskType.SCREENSHOT,
            risk_level=RiskLevel.LOW,
            attachments=[],
            chat_session_id=getattr(chat_session, "session_id", None),
        )
        self._runtime.state.update_task_status(
            task.task_id,
            TaskStatus.FAILED,
            error=screenshot_error,
        )
        if chat_session is not None:
            try:
                chat_session.upsert_assistant_message(
                    task_id=task.task_id,
                    content=f"主动桌面观察暂时无法读取截图：{screenshot_error}",
                    status=MessageStatus.FAILED,
                    error=screenshot_error,
                )
            except Exception:
                logger.debug("主动桌面观察截图失败消息写入聊天失败", exc_info=True)
        self._last_task_id = task.task_id
        self._attention_task_id = None
        self._reported_failed_task_id = task.task_id
        self._last_check_at = time.monotonic()
        return task.task_id

    def _upsert_proactive_message(
        self,
        task_id: str,
        content: str,
        status: MessageStatus,
        error: str | None = None,
    ) -> None:
        chat_session = get_proactive_chat_session(self._runtime)
        if chat_session is None:
            return
        try:
            chat_session.upsert_assistant_message(
                task_id=task_id,
                content=content,
                status=status,
                error=error,
            )
        except Exception:
            logger.debug("主动关怀结果写入专用会话失败", exc_info=True)

    def _capture_desktop_attachments(self, chat_session: Any | None) -> tuple[list[dict], str]:
        session_id = str(getattr(chat_session, "session_id", "") or "proactive")
        attachment_id, target_path = allocate_chat_attachment_path(session_id, ".png")
        try:
            meta = capture_screenshot_to_file(target_path)
            attachment = chat_attachment_record(
                attachment_id,
                target_path,
                kind="image",
                name="主动关怀桌面截图.png",
                mime_type="image/png",
            )
            attachment["source"] = "proactive_desktop_watch"
            logger.info(
                "主动桌面观察截图已捕获: %s (%sx%s, %s bytes)",
                target_path,
                meta.get("width") if isinstance(meta, dict) else "?",
                meta.get("height") if isinstance(meta, dict) else "?",
                meta.get("size") if isinstance(meta, dict) else target_path.stat().st_size,
            )
            return [attachment], ""
        except Exception as exc:
            try:
                target_path.unlink(missing_ok=True)
            except OSError:
                logger.debug("清理主动桌面观察截图失败: %s", target_path, exc_info=True)
            logger.warning("主动桌面观察截图捕获失败: %s", exc)
            return [], f"Yachiyo 本地截图失败：{exc}"


def _compact_attention_text(text: str) -> str:
    value = " ".join(str(text or "").split())
    if not value:
        return "有新的主动观察结果"
    return value if len(value) <= 160 else value[:159].rstrip() + "…"
