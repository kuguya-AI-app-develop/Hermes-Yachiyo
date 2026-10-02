"""Main chat runtime config helpers for the legacy engine entrypoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from apps.shell.yachiyo_agent.runtime_doctrine import YACHIYO_RUNTIME_OPERATING_MANUAL


MAIN_CHAT_DESKTOP_AGENT_INSTRUCTIONS = """
你是 Oha-Yachiyo 的日常桌面执行型 Agent，也是 Chat / Bubble / Live2D 的默认行动入口。
当用户提出可以由已授权工具完成的请求时，优先调用工具尝试执行，而不是只解释做法或要求用户自己操作。
{runtime_doctrine}
旧兼容映射不是封闭能力表；没有列出的应用、网站、数据源或任务，也必须先按 TaskIntent、Capability Registry、Runtime Planner 和发现工具处理。
能力类别包括：桌面发现与应用控制、窗口/UI 操作、浏览器、文件读取与本地路径、数据分析、artifact 输出、剪贴板、提醒/日程、媒体播放、系统控制、Workflow、GroupRun、记忆与 Skills。
少量代表工具示例：desktop.list_apps、desktop.open_app、desktop.focus_app、desktop.list_windows、desktop.read_ui、desktop.verify、desktop.inspect_app、desktop.running_apps、desktop.active_window、desktop.windows、desktop.ui_elements、screen.capture、desktop.permissions、app.open、app.focus、app.status、app.open_and_safe_type_text、app.focus_and_safe_type_text、app.open_and_safe_shortcut、app.focus_and_safe_shortcut、app.open_and_hotkey、app.open_and_click_ui_element、desktop.safe_shortcut、desktop.shortcut、desktop.safe_key、desktop.safe_type_text、desktop.search_submit、desktop.type、desktop.safe_click、desktop.safe_scroll、desktop.click_ui_element、desktop.type_into_ui_element、desktop.submit_foreground、browser.open_url、browser.open_url_and_extract_text、browser.open_url_and_screenshot、browser.extract_text、browser.click、desktop.reveal_path、desktop.open_path、desktop.open_path_with_app、data.analyze、workspace.write_patch、file.organize、artifact.write、terminal.run、python.run、clipboard.read、clipboard.write、notes.create、reminders.create、calendar.create_event、media.apple_music_play、media.apple_music_open_and_play、media.apple_music_control、media.music_app_open_and_play、system.settings_open、system.volume、system.brightness。
未知应用名、不确定窗口或需要 UI 上下文时，优先用 desktop.inspect_app 做一次通用应用检查；需要更细粒度时再用 desktop.list_apps、desktop.running_apps、desktop.list_windows、desktop.read_ui、desktop.verify、desktop.windows、desktop.ui_elements 或 screen.capture 做发现。只要用户说出了或可以发现目标应用，点击可见控件、向可见字段输入、安全快捷键、安全按键、滚动和坐标点击都优先使用 app.open_and_* / app.focus_and_* 这类 app-scoped 工具，让 Runtime 把动作绑定到目标应用；desktop.click_ui_element、desktop.type_into_ui_element、desktop.safe_* 只用于用户明确要求操作当前前台，或 app-scoped 工具不可用时的兼容 fallback。
媒体播放也按可发现桌面应用处理：先解析播放器应用，打开/聚焦后搜索、输入、提交并验证；media.apple_music_* 只是兼容 fallback，不是默认规划模型。
网页、搜索查询或 URL 优先用 browser.open_url；需要正文、截图或网页交互时用 browser.open_url_and_extract_text、browser.open_url_and_screenshot、browser.extract_text、browser.click 或 browser.type_text。
数据分析优先 data.analyze；需要自定义代码、复杂格式或可复现实验时再规划 workspace.read、python.run 或 terminal.run、artifact.write，其中代码/命令执行和代码写入必须保持审批。不要为了生成报告默认打开 Excel/Numbers，除非用户明确要求 UI 操作。
白名单快捷动作走 desktop.safe_shortcut，包括复制/粘贴/全选/撤销/重做/查找/聚焦地址栏/新建标签页/新建窗口/刷新/加入书签/打开历史记录/打开开发者工具/页面缩放/前进后退/重新打开关闭的标签页/隐藏其他应用等；安全导航键如 Escape、Tab、Shift+Tab、方向键、Home、End、Page Up、Page Down 走 desktop.safe_key。
命令面板、应用内搜索、点击可见控件后输入或确认时，拆成可观察步骤：先打开/聚焦/点击/输入，再用 desktop.safe_key 导航；提交搜索/查找 query 用 desktop.search_submit；发送消息、提交表单、确认破坏性或外部动作必须用 desktop.submit_foreground 生成审批，不要裸按 Return。
退出当前/前台应用用 desktop.quit_app，需要审批；不要改成让用户自己按 Command+Q。
低风险桌面动作默认直接执行，并把结果、失败、fallback 和 artifact 通过 Run Timeline 留痕；不要把低风险动作改成让用户手动操作。
当用户把多个明确低风险桌面动作串在一句话里时，可以按顺序执行这些工具并逐步留痕；如果序列中遇到需要审批的动作，交给 Runtime 生成审批卡后暂停。
前台点击、输入、快捷键、网页点击和网页输入属于中风险动作；要直接发起对应工具调用，让 Runtime 生成审批卡并在批准后继续执行，不要改成让用户手动操作。
如果系统权限缺失，明确说明缺少的权限和用户需要打开的系统设置入口；不要假装已经完成动作。
高风险动作仍必须遵守 approval/policy gate，尤其是删除或覆盖用户文件、发送消息、支付、系统设置、裸 shell、凭据相关操作。
""".format(runtime_doctrine=YACHIYO_RUNTIME_OPERATING_MANUAL).strip()


class MainChatRuntimeConfigBuilder:
    """Builds the runtime agent config used by the daily Chat entrypoint."""

    def __init__(
        self,
        *,
        main_chat_agent_id: str,
        agent_workspaces_dir: Path,
        workspace_status: Callable[[], dict[str, Any]],
        compile_tool_policy: Callable[[str, Any], dict[str, Any]],
        compile_workspace_policy: Callable[[Any], dict[str, Any]],
        trust_workspace_from_policy: Callable[..., None],
        memory_tool_names: Sequence[str],
        future_task_tool_names: Sequence[str],
        desktop_tool_names: Sequence[str] = (),
        default_workspace_name: str = "builtin-yachiyo-main",
    ) -> None:
        self._main_chat_agent_id = main_chat_agent_id
        self._agent_workspaces_dir = agent_workspaces_dir
        self._workspace_status = workspace_status
        self._compile_tool_policy = compile_tool_policy
        self._compile_workspace_policy = compile_workspace_policy
        self._trust_workspace_from_policy = trust_workspace_from_policy
        self._memory_tool_names = list(memory_tool_names)
        self._future_task_tool_names = list(future_task_tool_names)
        self._desktop_tool_names = list(desktop_tool_names)
        self._default_workspace_name = default_workspace_name

    def _default_workspace_dir(self) -> Path:
        return self._agent_workspaces_dir / self._default_workspace_name

    def workspace_policy(self, policy: dict[str, Any] | None = None) -> dict[str, Any]:
        if isinstance(policy, dict):
            compiled = self._compile_workspace_policy(policy)
        else:
            workspace = self._workspace_status()
            dirs = workspace.get("dirs") if isinstance(workspace.get("dirs"), dict) else {}
            if workspace.get("initialized") and dirs.get("projects"):
                workdir = Path(str(dirs["projects"]))
            else:
                workdir = self._default_workspace_dir()
            workdir.mkdir(parents=True, exist_ok=True)
            compiled = self._compile_workspace_policy(
                {
                    "default_workdir": str(workdir),
                    "readable_scopes": ["."],
                    "writable_scopes": ["."],
                }
            )
        if not str(compiled.get("default_workdir") or "").strip():
            workdir = self._default_workspace_dir()
            workdir.mkdir(parents=True, exist_ok=True)
            compiled = {**compiled, "default_workdir": str(workdir)}
        self._trust_workspace_from_policy(compiled, source="main_chat", commit=True)
        return compiled

    def virtual_workspace_policy(self) -> dict[str, Any]:
        return self._compile_workspace_policy(
            {
                "default_workdir": str(self._default_workspace_dir()),
                "readable_scopes": ["."],
                "writable_scopes": ["."],
            }
        )

    def tool_policy(self, policy: dict[str, Any] | None = None) -> dict[str, Any]:
        if isinstance(policy, Mapping) and policy.get("response_only") is True:
            # Main chat normally adds its baseline capabilities to a policy.
            # Internal evidence summaries explicitly restrict that surface.
            return self._compile_tool_policy("custom", {"allowed_tools": []})
        base_allowed = [
            "workspace.list",
            "workspace.read",
            "data.analyze",
            "workspace.write_patch",
            "file.organize",
            "terminal.run",
            "python.run",
            *self._desktop_tool_names,
            *self._memory_tool_names,
            *self._future_task_tool_names,
            "artifact.write",
        ]
        if isinstance(policy, Mapping):
            raw = dict(policy)
            raw_allowed = _raw_allowed_tools(policy)
            raw["allowed_tools"] = (
                _unique_tools([*raw_allowed, *base_allowed]) if raw_allowed else base_allowed
            )
        else:
            raw = {"allowed_tools": base_allowed}
        return self._compile_tool_policy("custom", raw)

    def agent_config(
        self,
        *,
        model_profile_id: str,
        tool_policy: dict[str, Any] | None = None,
        workspace_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "agent_id": self._main_chat_agent_id,
            "name": "Yachiyo",
            "nickname": "Yachiyo",
            "category": "orchestrator",
            "instructions": MAIN_CHAT_DESKTOP_AGENT_INSTRUCTIONS,
            "persona_prompt": "",
            "model_mode": "profile",
            "execution_backend": "native_profile",
            "model_profile_id": str(model_profile_id or "").strip(),
            "vision_model_profile_id": "",
            "model_config": {},
            "tool_policy": self.tool_policy(tool_policy),
            "workspace_policy": self.workspace_policy(workspace_policy),
            "skill_ids": [],
            "output_contract": "chat",
            "enabled": True,
        }

    def virtual_agent(self, *, default_profile_id: str = "") -> dict[str, Any]:
        clean_profile_id = str(default_profile_id or "").strip()
        return {
            "agent_id": self._main_chat_agent_id,
            "name": "Yachiyo",
            "nickname": "Yachiyo",
            "description": "Oha-Yachiyo main chat system agent.",
            "avatar_url": "",
            "category": "orchestrator",
            "instructions": MAIN_CHAT_DESKTOP_AGENT_INSTRUCTIONS,
            "persona_prompt": "",
            "model_mode": "profile",
            "execution_backend": "native_profile",
            "model_profile_id": clean_profile_id,
            "vision_model_profile_id": "",
            "model_config": {
                "provider": "model_profile",
                "base_url": "",
                "model": "",
                "api_key_configured": bool(clean_profile_id),
            },
            "tool_policy": self.tool_policy(),
            "workspace_policy": self.virtual_workspace_policy(),
            "skill_ids": [],
            "output_contract": "chat",
            "enabled": True,
            "virtual": True,
            "system": True,
            "builtin": True,
            "editable": False,
            "deletable": False,
            "created_at": "",
            "updated_at": "",
        }


class MainChatVirtualAgentProjector:
    """Projects the built-in daily chat Agent with the current default profile."""

    def __init__(
        self,
        *,
        main_chat_config: MainChatRuntimeConfigBuilder,
        default_profile_id: Callable[[], str],
    ) -> None:
        self._main_chat_config = main_chat_config
        self._default_profile_id = default_profile_id

    def virtual_agent(self) -> dict[str, Any]:
        try:
            default_profile_id = str(self._default_profile_id() or "").strip()
        except Exception:
            default_profile_id = ""
        return self._main_chat_config.virtual_agent(default_profile_id=default_profile_id)


def _raw_allowed_tools(policy: Mapping[str, Any]) -> list[str]:
    raw_allowed = policy.get("allowed_tools")
    if isinstance(raw_allowed, str):
        raw_allowed = [raw_allowed]
    if not isinstance(raw_allowed, Sequence):
        return []
    return [str(tool or "").strip() for tool in raw_allowed if str(tool or "").strip()]


def _unique_tools(tools: Sequence[str]) -> list[str]:
    result: list[str] = []
    for tool in tools:
        clean = str(tool or "").strip()
        if clean and clean not in result:
            result.append(clean)
    return result
