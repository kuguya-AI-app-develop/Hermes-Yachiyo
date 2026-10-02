"""请求/响应 Schema 定义"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .enums import RiskLevel, TaskStatus, TaskType


# ── 通用 ──────────────────────────────────────────────


class StatusResponse(BaseModel):
    """GET /status 响应"""

    service: str = "oha-yachiyo"
    version: str = "0.4.0"
    uptime_seconds: float
    task_counts: dict[TaskStatus, int] = Field(default_factory=dict)
    native_agent_ready: bool = False
    build_metadata: dict[str, Any] = Field(default_factory=dict)


# ── 任务 ──────────────────────────────────────────────


class TaskCreateRequest(BaseModel):
    """POST /tasks 请求"""

    description: str = Field(..., min_length=1, max_length=500)
    task_type: TaskType = TaskType.GENERAL
    risk_level: RiskLevel = RiskLevel.LOW


class TaskInfo(BaseModel):
    """单个任务的信息"""

    task_id: str
    description: str
    task_type: TaskType
    status: TaskStatus
    risk_level: RiskLevel
    created_at: datetime
    updated_at: datetime
    result: str | None = None
    error: str | None = None
    attachments: list[dict] = Field(default_factory=list)
    chat_session_id: str | None = None
    progress_label: str | None = None
    progress_updated_at: datetime | None = None
    # Runtime-supplied evidence for a separate, response-only task. The
    # immutable objective remains description; this context grants no tools.
    response_context: str | None = None


class TaskGetResponse(BaseModel):
    """GET /tasks/{task_id} 响应"""

    task: TaskInfo


class TaskCreateResponse(BaseModel):
    """POST /tasks 响应"""

    task: TaskInfo


class TaskListResponse(BaseModel):
    """GET /tasks 响应"""

    tasks: list[TaskInfo]
    total: int


class TaskCancelResponse(BaseModel):
    """POST /tasks/{task_id}/cancel 响应"""

    task: TaskInfo


# ── 本地能力 ──────────────────────────────────────────


class ScreenshotResponse(BaseModel):
    """GET /screen/current 响应"""

    image_base64: str
    format: str = "png"
    width: int
    height: int
    captured_at: datetime


class ActiveWindowResponse(BaseModel):
    """GET /system/active-window 响应"""

    title: str
    app_name: str
    pid: int | None = None
    queried_at: datetime


# ── Assistant intent ───────────────────────────────────


class AssistantIntentRequest(BaseModel):
    """POST /assistant/intent 请求。"""

    text: str = Field(..., min_length=1, max_length=1000)
    source: str = "astrbot"
    sender_id: str = ""
    dry_run: bool = False


class AssistantIntentResponse(BaseModel):
    """POST /assistant/intent 响应。"""

    ok: bool
    action: str
    task_id: str | None = None
    message: str


class AssistantProfilePatchRequest(BaseModel):
    """PATCH /assistant/profile 请求。"""

    agent_name: str | None = Field(default=None, max_length=80)
    agent_nickname: str | None = Field(default=None, max_length=40)
    agent_avatar_path: str | None = Field(default=None, max_length=1000)
    persona_prompt: str | None = Field(default=None, max_length=12000)
    user_address: str | None = Field(default=None, max_length=80)
    user_name: str | None = Field(default=None, max_length=80)
    user_avatar_path: str | None = Field(default=None, max_length=1000)
    user_profile: str | None = Field(default=None, max_length=2000)
    user_preferences: str | None = Field(default=None, max_length=2000)


class AssistantProfileResponse(BaseModel):
    """GET/PATCH /assistant/profile 响应。"""

    ok: bool = True
    agent_name: str = ""
    agent_nickname: str = ""
    agent_avatar_path: str = ""
    agent_avatar_url: str = ""
    persona_prompt: str = ""
    user_address: str = ""
    user_name: str = ""
    user_avatar_path: str = ""
    user_avatar_url: str = ""
    user_profile: str = ""
    user_preferences: str = ""
    memory_enabled: bool = True
    memory_scope: str = "local_chat_history"
    prompt_order: list[str] = Field(
        default_factory=lambda: [
            "agent_profile",
            "persona",
            "user_address",
            "user_profile",
            "relevant_memory",
            "current_session",
            "request",
        ]
    )
    message: str = ""
