"""Trusted desktop recovery metadata shared by Runtime entrypoints."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_DIRECT_RECOVERY_TOOLS = frozenset(
    {
        "app.focus",
        "app.focus_window",
        "app.focus_and_safe_shortcut",
        "app.focus_and_safe_key",
        "app.focus_and_hotkey",
        "app.focus_and_safe_scroll",
        "app.focus_and_safe_click",
        "app.focus_and_click_ui_element",
        "app.focus_and_type_into_ui_element",
        "app.focus_and_safe_type_text",
        "app.hide",
        "app.minimize",
        "app.open",
        "app.open_and_safe_shortcut",
        "app.open_and_safe_key",
        "app.open_and_hotkey",
        "app.open_and_safe_scroll",
        "app.open_and_safe_click",
        "app.open_and_click_ui_element",
        "app.open_and_type_into_ui_element",
        "app.open_and_safe_type_text",
        "app.quit",
        "app.show",
        "app.status",
        "browser.current_page",
        "browser.click",
        "browser.extract_text",
        "browser.open_url",
        "browser.open_url_and_extract_text",
        "browser.open_url_and_screenshot",
        "browser.screenshot",
        "browser.type_text",
        "calendar.create_event",
        "clipboard.write",
        "clipboard.read",
        "notes.create",
        "desktop.active_window",
        "desktop.click",
        "desktop.click_ui_element",
        "desktop.type_into_ui_element",
        "desktop.close_window",
        "desktop.quit_app",
        "desktop.hide_app",
        "desktop.show_all_apps",
        "desktop.hotkey",
        "desktop.minimize_window",
        "desktop.open_path",
        "desktop.permissions",
        "desktop.list_apps",
        "desktop.reveal_path",
        "desktop.running_apps",
        "desktop.safe_click",
        "desktop.safe_key",
        "desktop.safe_scroll",
        "desktop.safe_shortcut",
        "desktop.safe_type_text",
        "desktop.search_submit",
        "desktop.submit_foreground",
        "desktop.type_text",
        "desktop.ui_elements",
        "desktop.windows",
        "media.apple_music_control",
        "media.apple_music_status",
        "media.music_app_control",
        "media.music_app_open_and_play",
        "media.system_control",
        "media.apple_music_open_and_play",
        "media.apple_music_play",
        "reminders.create",
        "screen.capture",
        "system.settings_open",
        "system.brightness",
        "system.display_sleep",
        "system.screen_saver_start",
        "system.volume",
    }
)

_PROMPT_RECOVERY_TOOLS = frozenset(
    {
        "app.focus",
        "app.focus_and_safe_click",
        "app.focus_and_safe_key",
        "app.focus_and_safe_scroll",
        "app.focus_and_safe_shortcut",
        "app.focus_and_safe_type_text",
        "app.focus_and_click_ui_element",
        "app.focus_and_type_into_ui_element",
        "app.focus_window",
        "app.open",
        "app.open_and_safe_click",
        "app.open_and_safe_key",
        "app.open_and_safe_scroll",
        "app.open_and_safe_shortcut",
        "app.open_and_safe_type_text",
        "app.open_and_click_ui_element",
        "app.open_and_type_into_ui_element",
        "app.show",
        "app.status",
        "browser.current_page",
        "browser.extract_text",
        "browser.open_url",
        "browser.open_url_and_extract_text",
        "browser.open_url_and_screenshot",
        "browser.screenshot",
        "clipboard.read",
        "clipboard.write",
        "desktop.active_window",
        "desktop.open_path",
        "desktop.permissions",
        "desktop.list_apps",
        "desktop.running_apps",
        "desktop.click_ui_element",
        "desktop.safe_click",
        "desktop.safe_key",
        "desktop.safe_scroll",
        "desktop.safe_shortcut",
        "desktop.safe_type_text",
        "desktop.show_all_apps",
        "desktop.type_into_ui_element",
        "desktop.ui_elements",
        "desktop.windows",
        "media.apple_music_control",
        "media.apple_music_status",
        "media.apple_music_open_and_play",
        "media.apple_music_play",
        "media.music_app_control",
        "media.music_app_open_and_play",
        "media.system_control",
        "screen.capture",
        "system.brightness",
        "system.display_sleep",
        "system.screen_saver_start",
        "system.settings_open",
        "system.volume",
    }
)

_APP_FOREGROUND_RECOVERY_TOOLS = frozenset(
    {
        "app.open_and_safe_type_text",
        "app.focus_and_safe_type_text",
        "app.open_and_safe_shortcut",
        "app.focus_and_safe_shortcut",
        "app.open_and_safe_key",
        "app.focus_and_safe_key",
        "app.open_and_safe_scroll",
        "app.focus_and_safe_scroll",
        "app.open_and_safe_click",
        "app.focus_and_safe_click",
        "app.open_and_click_ui_element",
        "app.focus_and_click_ui_element",
        "app.open_and_type_into_ui_element",
        "app.focus_and_type_into_ui_element",
    }
)

_SAFE_SHORTCUT_PROMPTS = {
    "copy": "复制选中内容",
    "paste": "粘贴",
    "select_all": "全选",
    "undo": "撤销",
    "redo": "重做",
    "find": "打开查找",
    "focus_address_bar": "聚焦地址栏",
    "new_tab": "新建标签页",
    "new_private_window": "新建私密窗口",
    "close_tab": "关闭标签页",
    "next_tab": "切到下一个标签页",
    "previous_tab": "切到上一个标签页",
    "next_window": "切到下一个窗口",
    "previous_window": "切到上一个窗口",
    "switch_previous_app": "切到上一个应用",
    "switch_next_app": "切到下一个应用",
    "hide_other_apps": "隐藏其他应用",
    "toggle_full_screen": "切换当前窗口全屏",
    "mission_control": "打开任务控制中心",
    "application_windows": "显示当前应用窗口",
    "spotlight_search": "打开 Spotlight",
    "emoji_picker": "打开 Emoji 面板",
    "screenshot_selection": "截取选区截图",
    "screenshot_toolbar": "打开截图/录屏工具",
    "lock_screen": "锁屏",
    "force_quit_dialog": "打开强制退出窗口",
    "new_window": "新建窗口",
    "new_document": "新建文档",
    "new_message": "新建消息",
    "new_folder": "新建文件夹",
    "rename_selected": "重命名 Finder 选中项",
    "finder_get_info": "显示 Finder 选中项简介",
    "finder_airdrop": "打开隔空投送",
    "finder_network": "打开网络位置",
    "finder_recents": "打开最近使用",
    "parent_folder": "打开上一级文件夹",
    "new_note": "新建笔记",
    "new_reminder": "新建提醒事项",
    "new_event": "新建日程",
    "refresh": "刷新",
    "bookmark_page": "加入书签",
    "show_history": "打开历史记录",
    "open_devtools": "打开开发者工具",
    "command_palette": "打开命令面板",
    "obsidian_command_palette": "打开 Obsidian 命令面板",
    "preferences": "打开偏好设置",
    "zoom_in": "放大页面",
    "zoom_out": "缩小页面",
    "reset_zoom": "重置页面缩放",
    "browser_back": "返回上一页",
    "browser_forward": "前进一页",
    "reopen_closed_tab": "重新打开关闭的标签页",
    "finder_quick_look": "快速查看 Finder 选中项",
}


def daily_desktop_metadata_tool_request(
    metadata: Mapping[str, Any] | None,
    allowed_tools: list[str] | None = None,
) -> dict[str, Any] | None:
    """Return an exact request carried by trusted recovery metadata."""

    if not isinstance(metadata, Mapping) or metadata.get("desktop_permission_recovery") is not True:
        return None

    if metadata.get("desktop_permission_retry") is True or metadata.get("recovery_action_kind") == "retry_original":
        tool_name = str(metadata.get("recovery_tool") or metadata.get("recovery_retry_tool") or "").strip()
        raw_input = metadata.get("recovery_input")
        if not isinstance(raw_input, Mapping):
            raw_input = metadata.get("recovery_retry_input")
    else:
        if str(metadata.get("recovery_risk_level") or "").strip().lower() != "low":
            return None
        tool_name = str(metadata.get("recovery_tool") or "").strip()
        raw_input = metadata.get("recovery_input")

    if not tool_name or tool_name not in _DIRECT_RECOVERY_TOOLS:
        return None
    if allowed_tools is not None and tool_name not in {
        str(tool or "").strip() for tool in allowed_tools
    }:
        return None
    if not isinstance(raw_input, Mapping):
        return None
    return {
        "protocol": "json_fallback",
        "tool": tool_name,
        "input": dict(raw_input),
        "source": "daily_desktop_metadata",
        "planning_reason": "structured_recovery_metadata",
    }


def daily_desktop_recovery_prompt(metadata: Mapping[str, Any] | None) -> str:
    """Render legacy-compatible text for a low-risk recovery action."""

    if not isinstance(metadata, Mapping) or metadata.get("desktop_permission_recovery") is not True:
        return ""
    if str(metadata.get("recovery_risk_level") or "").strip().lower() != "low":
        return ""
    tool_name = str(metadata.get("recovery_tool") or "").strip()
    recovery_input = metadata.get("recovery_input")
    if tool_name not in _PROMPT_RECOVERY_TOOLS or not isinstance(recovery_input, Mapping):
        return ""
    prompt = _control_prompt(tool_name, recovery_input)
    if prompt:
        return prompt
    if tool_name == "system.settings_open":
        target = str(recovery_input.get("target") or "").strip()
        return f"打开{target}" if target else ""
    if tool_name == "browser.open_url":
        url = str(recovery_input.get("url") or "").strip()
        return f"打开 {url}" if url else ""
    if tool_name == "desktop.open_path":
        path = str(recovery_input.get("path") or "").strip()
        return f"打开 {path}" if path else ""
    app_name = str(recovery_input.get("app_name") or "").strip()
    return f"打开{app_name}" if app_name else ""


def _control_prompt(tool_name: str, recovery_input: Mapping[str, Any]) -> str:
    action = str(recovery_input.get("action") or "").strip().lower()
    if tool_name == "app.focus":
        app_name = str(recovery_input.get("app_name") or "").strip()
        return f"切到{app_name}" if app_name else ""
    if tool_name == "app.focus_window":
        app_name = str(recovery_input.get("app_name") or "").strip()
        title = str(recovery_input.get("window_title") or recovery_input.get("title_contains") or "").strip()
        if app_name and title:
            return f"切到{app_name} {title}窗口"
        return f"切到{app_name}窗口" if app_name else ""
    if tool_name == "app.show":
        app_name = str(recovery_input.get("app_name") or "").strip()
        return f"显示{app_name}" if app_name else ""
    if tool_name == "app.status":
        app_name = str(recovery_input.get("app_name") or "").strip()
        return f"检查{app_name}是否打开" if app_name else ""
    if tool_name == "desktop.show_all_apps":
        return "显示所有隐藏应用"
    foreground_prompt = _app_foreground_prompt(tool_name, recovery_input)
    if foreground_prompt:
        return foreground_prompt
    if tool_name == "media.apple_music_play":
        query = str(recovery_input.get("query") or "").strip()
        return f"播放{query}" if query else ""
    if tool_name == "media.apple_music_status":
        return "查看Apple Music播放状态"
    if tool_name == "media.apple_music_open_and_play":
        return "打开Apple Music并播放"
    if tool_name == "media.music_app_open_and_play":
        app_name = str(recovery_input.get("app_name") or "").strip()
        return f"打开{app_name}并播放" if app_name else ""
    if tool_name == "media.music_app_control":
        app_name = str(recovery_input.get("app_name") or "").strip()
        label = {
            "play": "播放",
            "pause": "暂停",
            "next": "下一首",
            "previous": "上一首",
            "toggle": "播放暂停",
        }.get(action, "")
        return f"{app_name}{label}" if app_name and label else ""
    if tool_name == "media.system_control":
        return {
            "play": "继续播放当前媒体",
            "pause": "暂停当前媒体",
            "next": "下一首",
            "previous": "上一首",
            "toggle": "播放暂停",
        }.get(action, "")
    if tool_name == "media.apple_music_control":
        return {
            "play": "播放音乐",
            "pause": "暂停音乐",
            "next": "下一首",
            "previous": "上一首",
            "toggle": "播放暂停",
        }.get(action, "")
    if tool_name == "system.volume":
        return _volume_prompt(action, recovery_input)
    if tool_name == "system.brightness":
        return {"up": "屏幕亮一点", "down": "屏幕暗一点"}.get(action, "")
    if tool_name == "system.display_sleep":
        return "让显示器睡眠"
    if tool_name == "system.screen_saver_start":
        return "启动屏幕保护程序"
    if tool_name == "clipboard.read":
        return "读取剪贴板"
    if tool_name == "clipboard.write":
        text = str(recovery_input.get("text") or "").strip()
        return f"复制{text}到剪贴板" if text else ""
    if tool_name == "screen.capture":
        return "截图当前屏幕"
    if tool_name == "desktop.permissions":
        return "检查桌面权限"
    if tool_name == "desktop.active_window":
        return "查看当前窗口"
    if tool_name == "desktop.list_apps":
        return "发现已安装应用"
    if tool_name == "desktop.running_apps":
        return "查看正在运行的应用"
    if tool_name == "desktop.windows":
        app_name = str(recovery_input.get("app_name") or "").strip()
        return f"查看{app_name}窗口" if app_name else "查看桌面窗口"
    if tool_name == "desktop.ui_elements":
        return "查看当前界面控件"
    if tool_name == "desktop.click_ui_element":
        return _click_ui_element_prompt(recovery_input)
    if tool_name == "desktop.type_into_ui_element":
        return _type_into_ui_element_prompt(recovery_input)
    if tool_name == "desktop.safe_shortcut":
        return _SAFE_SHORTCUT_PROMPTS.get(action, "")
    if tool_name == "desktop.safe_key":
        return _safe_key_prompt(recovery_input)
    if tool_name == "desktop.safe_scroll":
        return _safe_scroll_prompt(recovery_input)
    if tool_name == "desktop.safe_click":
        x, y = recovery_input.get("x"), recovery_input.get("y")
        return f"点击 {x}, {y}" if x is not None and y is not None else ""
    if tool_name == "desktop.safe_type_text":
        text = str(recovery_input.get("text") or "").strip()
        return f"输入{text}" if text else ""
    return {
        "browser.current_page": "查看当前网页",
        "browser.extract_text": "读取当前网页正文",
        "browser.screenshot": "截取当前网页",
    }.get(tool_name, _browser_url_prompt(tool_name, recovery_input))


def _app_foreground_prompt(tool_name: str, recovery_input: Mapping[str, Any]) -> str:
    if tool_name not in _APP_FOREGROUND_RECOVERY_TOOLS:
        return ""
    app_name = str(recovery_input.get("app_name") or "").strip()
    if not app_name:
        return ""
    prefix = "打开" if tool_name.startswith("app.open") else "切到"
    action = str(recovery_input.get("action") or "").strip().lower()
    if tool_name.endswith("safe_type_text"):
        text = str(recovery_input.get("text") or "").strip()
        detail = f"输入{text}" if text else "输入文字"
    elif tool_name.endswith("safe_shortcut"):
        detail = _SAFE_SHORTCUT_PROMPTS.get(action, "")
    elif tool_name.endswith("safe_key"):
        detail = _safe_key_prompt(recovery_input)
    elif tool_name.endswith("safe_scroll"):
        detail = _safe_scroll_prompt(recovery_input)
    elif tool_name.endswith("safe_click"):
        x, y = recovery_input.get("x"), recovery_input.get("y")
        detail = f"点击 {x}, {y}" if x is not None and y is not None else ""
    elif tool_name.endswith("click_ui_element"):
        detail = _click_ui_element_prompt(recovery_input)
    elif tool_name.endswith("type_into_ui_element"):
        detail = _type_into_ui_element_prompt(recovery_input)
    else:
        detail = ""
    return f"{prefix}{app_name}并{detail}" if detail else ""


def _volume_prompt(action: str, recovery_input: Mapping[str, Any]) -> str:
    prompt = {
        "status": "当前音量是多少",
        "mute": "静音",
        "unmute": "取消静音",
        "up": "调大音量",
        "down": "调小音量",
    }.get(action, "")
    if prompt or action != "set":
        return prompt
    level_value = recovery_input.get("level")
    level = str(level_value if level_value is not None else "").strip()
    return f"把音量调到 {level}%" if level else ""


def _click_ui_element_prompt(recovery_input: Mapping[str, Any]) -> str:
    target = str(recovery_input.get("target") or "").strip()
    return f"点击前台控件{target}" if target else ""


def _type_into_ui_element_prompt(recovery_input: Mapping[str, Any]) -> str:
    target = str(recovery_input.get("target") or "").strip()
    text = str(recovery_input.get("text") or "").strip()
    if target and text:
        return f"在前台控件{target}输入{text}"
    return f"在前台控件{target}输入文字" if target else ""


def _safe_key_prompt(recovery_input: Mapping[str, Any]) -> str:
    action = str(recovery_input.get("action") or "").strip().lower()
    label = {
        "escape": "Escape",
        "tab": "Tab",
        "shift_tab": "Shift+Tab",
        "arrow_up": "上箭头",
        "arrow_down": "下箭头",
        "arrow_left": "左箭头",
        "arrow_right": "右箭头",
        "home": "Home",
        "end": "End",
        "page_up": "Page Up",
        "page_down": "Page Down",
        "show_desktop": "显示桌面",
    }.get(action, "")
    if not label:
        return ""
    if action == "show_desktop":
        return "显示桌面"
    repeat_count = _positive_int(recovery_input.get("repeat_count"), default=1)
    suffix = "" if repeat_count == 1 else f"{repeat_count}次"
    return f"按{label}{suffix}"


def _safe_scroll_prompt(recovery_input: Mapping[str, Any]) -> str:
    direction = str(recovery_input.get("direction") or "").strip().lower()
    label = {"down": "向下", "up": "向上"}.get(direction, "")
    if not label:
        return ""
    pages = _positive_int(recovery_input.get("pages"), default=1)
    suffix = "" if pages == 1 else f"{pages}页"
    return f"{label}滚动{suffix}"


def _positive_int(value: Any, *, default: int) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _browser_url_prompt(tool_name: str, recovery_input: Mapping[str, Any]) -> str:
    url = str(recovery_input.get("url") or "").strip()
    if not url:
        return ""
    label = {
        "browser.open_url_and_extract_text": "打开并读取",
        "browser.open_url_and_screenshot": "打开",
    }.get(tool_name, "")
    if tool_name == "browser.open_url_and_screenshot":
        return f"{label} {url} 并截图"
    return f"{label} {url}" if label else ""


__all__ = [
    "daily_desktop_metadata_tool_request",
    "daily_desktop_recovery_prompt",
]
