"""Structured desktop execution helpers for Agent tools."""

from __future__ import annotations

import hashlib
import json
import locale
import math
import os
import platform
import plistlib
import re
import shutil
import subprocess
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse
from urllib.request import Request, urlopen

from apps.core.tls import urlopen_with_bundled_ca
from apps.shell.agent.runtime.app_aliases import APP_ALIASES, compact_app_alias
from apps.shell.agent.runtime.callbacks import supports_keyword

_ELECTRON_NATIVE_URL_ENV = "OHA_YACHIYO_ELECTRON_NATIVE_URL"
_ELECTRON_NATIVE_TOKEN_ENV = "OHA_YACHIYO_ELECTRON_NATIVE_TOKEN"
_APPLE_MUSIC_PLAY_DEADLINE_SECONDS = 30.0
_APPLE_MUSIC_CATALOG_LIMIT = 20
_APPLE_MUSIC_CATALOG_POLL_ATTEMPTS = 4

_APP_ALIAS_EXPANSION_PRIMARY_SCORE = 95


def _remaining_timeout(deadline: float, cap_seconds: float) -> float:
    return max(0.0, min(float(cap_seconds), deadline - time.monotonic()))


def _call_with_bounded_timeout(
    callback: Any,
    *args: Any,
    timeout_seconds: float,
) -> Any:
    if supports_keyword(callback, "timeout_seconds"):
        return callback(*args, timeout_seconds=timeout_seconds)
    return callback(*args)

_APP_CAPABILITY_QUERY_PROFILES = (
    {
        "id": "web_browser",
        "aliases": (
            "browser",
            "browser app",
            "web browser",
            "web browser app",
            "default browser",
            "浏览器",
            "网页浏览器",
        ),
        "schemes": ("http", "https"),
        "documents": ("public.html", "public.xhtml", "html", "htm", "xhtml"),
        "score": 92,
        "require_scheme_and_document": True,
    },
    {
        "id": "file_manager",
        "aliases": (
            "file manager",
            "file manager app",
            "file browser",
            "file browser app",
            "文件管理器",
            "文件浏览器",
            "访达",
        ),
        "schemes": ("file", "afp", "smb", "cifs"),
        "documents": ("folder", "public.folder"),
        "score": 94,
        "require_scheme_and_document": True,
    },
    {
        "id": "communication",
        "aliases": (
            "communication",
            "communication app",
            "communicator",
            "chat",
            "chat app",
            "messaging",
            "messaging app",
            "messenger",
            "通讯",
            "通信",
            "聊天",
            "消息",
        ),
        "schemes": (
            "mailto",
            "message",
            "sms",
            "im",
            "imessage",
            "ichat",
            "slack",
            "wechat",
            "weixin",
            "discord",
            "telegram",
            "msteams",
            "zoommtg",
        ),
        "categories": ("social-networking",),
        "documents": ("email message", "plain text"),
        "score": 88,
    },
    {
        "id": "markdown",
        "aliases": (
            "markdown",
            "markdown app",
            "markdown editor",
            "md",
            "mdown",
            "mdtext",
            "mdtxt",
            "mkd",
            "mkdn",
        ),
        "documents": ("markdown", "md", "mdown", "mdtext", "mdtxt", "mkd", "mkdn"),
        "score": 88,
    },
    {
        "id": "pdf",
        "aliases": ("pdf", "PDF", "pdf app", "pdf editor", "pdf viewer"),
        "documents": ("pdf", "com.adobe.pdf", "pdf document"),
        "score": 86,
    },
    {
        "id": "code",
        "aliases": ("code", "code editor", "coding", "programming", "代码", "编程"),
        "documents": (
            "python",
            "javascript",
            "typescript",
            "source code",
            "public.source-code",
            "py",
            "js",
            "ts",
            "tsx",
            "jsx",
            "java",
            "go",
            "rs",
        ),
        "categories": ("developer-tools",),
        "score": 86,
    },
    {
        "id": "image",
        "aliases": ("image", "image editor", "photo", "photo editor", "picture", "图片", "图像", "照片"),
        "documents": (
            "image",
            "public.image",
            "public.jpeg",
            "public.png",
            "public.tiff",
            "gif",
            "jpeg",
            "jpg",
            "png",
            "tiff",
            "webp",
        ),
        "score": 84,
    },
    {
        "id": "spreadsheet",
        "aliases": (
            "spreadsheet",
            "spreadsheet app",
            "spreadsheet editor",
            "sheet",
            "excel",
            "xlsx",
            "csv",
            "表格",
            "电子表格",
        ),
        "documents": ("xlsx", "xls", "csv", "tsv", "spreadsheet", "comma-separated"),
        "score": 86,
    },
    {
        "id": "presentation",
        "aliases": (
            "presentation",
            "presentation app",
            "slides",
            "slides app",
            "ppt",
            "pptx",
            "keynote",
            "演示文稿",
            "幻灯片",
        ),
        "documents": ("ppt", "pptx", "presentation", "keynote", "slide"),
        "score": 86,
    },
    {
        "id": "music",
        "aliases": ("music", "music app", "music player", "audio player", "音乐", "播放器"),
        "schemes": ("music", "itmss", "spotify"),
        "documents": ("audio", "public.audio", "mp3", "m4a", "wav", "flac"),
        "score": 86,
    },
)

_COMMON_FOLDER_TARGETS = {
    "desktop": "Desktop",
    "desktopfolder": "Desktop",
    "桌面": "Desktop",
    "桌面文件夹": "Desktop",
    "downloads": "Downloads",
    "downloadsfolder": "Downloads",
    "下载": "Downloads",
    "下载文件夹": "Downloads",
    "documents": "Documents",
    "documentsfolder": "Documents",
    "文档": "Documents",
    "文档文件夹": "Documents",
    "文稿": "Documents",
    "文稿文件夹": "Documents",
    "applications": "/Applications",
    "applicationsfolder": "/Applications",
    "applicationsdirectory": "/Applications",
    "应用程序": "/Applications",
    "应用程序文件夹": "/Applications",
    "应用程序目录": "/Applications",
    "utilities": "/Applications/Utilities",
    "utilitiesfolder": "/Applications/Utilities",
    "utilitiesdirectory": "/Applications/Utilities",
    "utilityfolder": "/Applications/Utilities",
    "utilitydirectory": "/Applications/Utilities",
    "实用工具": "/Applications/Utilities",
    "实用工具文件夹": "/Applications/Utilities",
    "实用工具目录": "/Applications/Utilities",
    "library": "Library",
    "libraryfolder": "Library",
    "librarydirectory": "Library",
    "资源库": "Library",
    "资源库文件夹": "Library",
    "资源库目录": "Library",
    "home": "",
    "homefolder": "",
    "homedirectory": "",
    "userfolder": "",
    "userdirectory": "",
    "主目录": "",
    "个人主目录": "",
    "我的主目录": "",
    "用户文件夹": "",
    "用户目录": "",
    "个人文件夹": "",
    "个人目录": "",
}

_SAFE_OPEN_PATH_SUFFIXES = {
    ".txt",
    ".text",
    ".md",
    ".markdown",
    ".log",
    ".rtf",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".htm",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".pages",
    ".numbers",
    ".key",
    ".mp3",
    ".m4a",
    ".wav",
    ".aiff",
    ".flac",
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".webm",
}

_UNSAFE_OPEN_PATH_SUFFIXES = {
    ".app",
    ".command",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".py",
    ".rb",
    ".pl",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jar",
    ".pkg",
    ".dmg",
    ".exe",
    ".bin",
    ".run",
    ".workflow",
    ".scpt",
    ".applescript",
}

UI_CONTROL_LIKE_ROLES = {
    "AXButton",
    "AXCell",
    "AXCheckBox",
    "AXColorWell",
    "AXComboBox",
    "AXDateField",
    "AXDisclosureTriangle",
    "AXIncrementor",
    "AXLink",
    "AXPopUpButton",
    "AXRadioButton",
    "AXRow",
    "AXSlider",
    "AXTextArea",
    "AXTextField",
}

_SAFE_SHORTCUTS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "copy": ("c", ("command",), "copy"),
    "copy_current_page_link": ("l", ("command",), "copy current page link"),
    "paste": ("v", ("command",), "paste"),
    "select_all": ("a", ("command",), "select all"),
    "undo": ("z", ("command",), "undo"),
    "redo": ("z", ("command", "shift"), "redo"),
    "find": ("f", ("command",), "find"),
    "focus_address_bar": ("l", ("command",), "focus address bar"),
    "new_tab": ("t", ("command",), "new tab"),
    "new_private_window": ("n", ("command", "shift"), "new private window"),
    "close_tab": ("w", ("command",), "close tab"),
    "next_tab": ("]", ("command", "shift"), "next tab"),
    "previous_tab": ("[", ("command", "shift"), "previous tab"),
    "next_window": ("`", ("command",), "next window"),
    "previous_window": ("`", ("command", "shift"), "previous window"),
    "switch_previous_app": ("tab", ("command",), "switch to previous app"),
    "switch_next_app": ("tab", ("command", "shift"), "switch to next app"),
    "hide_other_apps": ("h", ("command", "option"), "hide other apps"),
    "toggle_full_screen": ("f", ("control", "command"), "toggle full screen"),
    "mission_control": ("up", ("control",), "mission control"),
    "application_windows": ("down", ("control",), "application windows"),
    "spotlight_search": ("space", ("command",), "spotlight search"),
    "emoji_picker": ("space", ("control", "command"), "emoji picker"),
    "screenshot_selection": ("4", ("command", "shift"), "screenshot selection"),
    "screenshot_toolbar": ("5", ("command", "shift"), "screenshot toolbar"),
    "lock_screen": ("q", ("control", "command"), "lock screen"),
    "force_quit_dialog": ("escape", ("command", "option"), "force quit dialog"),
    "new_window": ("n", ("command",), "new window"),
    "new_document": ("n", ("command",), "new document"),
    "new_message": ("n", ("command",), "new message"),
    "new_folder": ("n", ("command", "shift"), "new folder"),
    "rename_selected": ("return", (), "rename selected Finder item"),
    "parent_folder": ("up", ("command",), "open parent folder"),
    "finder_get_info": ("i", ("command",), "Finder Get Info"),
    "finder_airdrop": ("r", ("command", "shift"), "Finder AirDrop"),
    "finder_network": ("k", ("command", "shift"), "Finder Network"),
    "finder_recents": ("f", ("command", "shift"), "Finder Recents"),
    "new_note": ("n", ("command",), "new note"),
    "new_task": ("n", ("command",), "new task"),
    "new_reminder": ("n", ("command",), "new reminder"),
    "new_event": ("n", ("command",), "new calendar event"),
    "refresh": ("r", ("command",), "refresh"),
    "bookmark_page": ("d", ("command",), "bookmark current page"),
    "show_history": ("y", ("command",), "show history"),
    "open_devtools": ("i", ("command", "option"), "open developer tools"),
    "command_palette": ("p", ("command", "shift"), "command palette"),
    "obsidian_command_palette": ("p", ("command",), "Obsidian command palette"),
    "preferences": (",", ("command",), "preferences"),
    "zoom_in": ("+", ("command",), "zoom in"),
    "zoom_out": ("-", ("command",), "zoom out"),
    "reset_zoom": ("0", ("command",), "reset zoom"),
    "browser_back": ("[", ("command",), "browser back"),
    "browser_forward": ("]", ("command",), "browser forward"),
    "reopen_closed_tab": ("t", ("command", "shift"), "reopen closed tab"),
    "finder_quick_look": ("space", (), "Finder Quick Look"),
}

_SAFE_KEYS: dict[str, tuple[int, str]] = {
    "escape": (53, "Escape"),
    "tab": (48, "Tab"),
    "shift_tab": (48, "Shift+Tab"),
    "arrow_up": (126, "Up Arrow"),
    "arrow_down": (125, "Down Arrow"),
    "arrow_left": (123, "Left Arrow"),
    "arrow_right": (124, "Right Arrow"),
    "home": (115, "Home"),
    "end": (119, "End"),
    "page_up": (116, "Page Up"),
    "page_down": (121, "Page Down"),
    "show_desktop": (103, "Show Desktop"),
}

_HOTKEY_KEY_CODES: dict[str, int] = {
    "return": 36,
    "enter": 36,
    "tab": 48,
    "space": 49,
    "escape": 53,
    "esc": 53,
    "delete": 51,
    "backspace": 51,
    "home": 115,
    "end": 119,
    "page_up": 116,
    "pageup": 116,
    "page_down": 121,
    "pagedown": 121,
    "`": 50,
    "grave": 50,
    "backtick": 50,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
}

_APPLE_MUSIC_MEDIA_KEY_FALLBACKS: dict[str, tuple[int, str, str]] = {
    "toggle": (100, "Play/Pause", "toggle"),
    "play": (100, "Play/Pause", "toggle"),
    "next": (101, "Next", "next"),
    "previous": (98, "Previous", "previous"),
}

_PRIVACY_SECURITY_URLS = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy",
    "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension",
)
_BLUETOOTH_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.BluetoothSettings",
    "x-apple.systempreferences:com.apple.preferences.Bluetooth",
)
_NETWORK_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Network-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.network",
)
_WIFI_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.wifi-settings-extension",
    "x-apple.systempreferences:com.apple.preference.network?Wi-Fi",
    *_NETWORK_SETTINGS_URLS,
)
_DISPLAY_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Displays-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.displays",
)
_SOUND_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Sound-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.sound",
)
_KEYBOARD_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Keyboard-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.keyboard",
)
_NOTIFICATIONS_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Notifications-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.notifications",
)
_BATTERY_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Battery-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.battery",
)
_MOUSE_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Mouse-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.mouse",
)
_TRACKPAD_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Trackpad-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.trackpad",
)
_PRINTERS_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Print-Scan-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.printfax",
)
_FOCUS_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Focus-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.notifications?Focus",
)
_WALLPAPER_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Wallpaper-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.desktopscreeneffect",
)
_DESKTOP_DOCK_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Desktop-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.dock",
)
_SCREEN_SAVER_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.ScreenSaver-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.desktopscreeneffect?ScreenSaver",
)
_SIRI_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Siri-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.speech?Siri",
)
_LANGUAGE_REGION_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Localization-Settings.extension",
    "x-apple.systempreferences:com.apple.Localization",
)
_DATE_TIME_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Date-Time-Settings.extension",
    "x-apple.systempreferences:com.apple.preference.datetime",
)
_SOFTWARE_UPDATE_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Software-Update-Settings.extension",
    "x-apple.systempreferences:com.apple.preferences.softwareupdate",
)
_STORAGE_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Storage-Settings.extension",
    "x-apple.systempreferences:com.apple.settings.Storage",
)
_LOGIN_ITEMS_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.LoginItems-Settings.extension",
    "x-apple.systempreferences:com.apple.LoginItems-Settings.extension",
)
_USERS_GROUPS_SETTINGS_URLS = (
    "x-apple.systempreferences:com.apple.Users-Groups-Settings.extension",
    "x-apple.systempreferences:com.apple.preferences.users",
)

_SYSTEM_SETTINGS_TARGETS = {
    "privacysecurity": ("Privacy & Security", _PRIVACY_SECURITY_URLS),
    "privacyandsecurity": ("Privacy & Security", _PRIVACY_SECURITY_URLS),
    "securityprivacy": ("Privacy & Security", _PRIVACY_SECURITY_URLS),
    "securityandprivacy": ("Privacy & Security", _PRIVACY_SECURITY_URLS),
    "隐私与安全性": ("Privacy & Security", _PRIVACY_SECURITY_URLS),
    "隐私和安全性": ("Privacy & Security", _PRIVACY_SECURITY_URLS),
    "隐私安全": ("Privacy & Security", _PRIVACY_SECURITY_URLS),
    "安全性与隐私": ("Privacy & Security", _PRIVACY_SECURITY_URLS),
    "安全与隐私": ("Privacy & Security", _PRIVACY_SECURITY_URLS),
    "桌面权限": ("Privacy & Security", _PRIVACY_SECURITY_URLS),
    "桌面执行权限": ("Privacy & Security", _PRIVACY_SECURITY_URLS),
    "本地工具权限": ("Privacy & Security", _PRIVACY_SECURITY_URLS),
    "权限诊断": ("Privacy & Security", _PRIVACY_SECURITY_URLS),
    "bluetooth": ("Bluetooth", _BLUETOOTH_SETTINGS_URLS),
    "蓝牙": ("Bluetooth", _BLUETOOTH_SETTINGS_URLS),
    "network": ("Network", _NETWORK_SETTINGS_URLS),
    "网络": ("Network", _NETWORK_SETTINGS_URLS),
    "wifi": ("Wi-Fi", _WIFI_SETTINGS_URLS),
    "无线网络": ("Wi-Fi", _WIFI_SETTINGS_URLS),
    "无线局域网": ("Wi-Fi", _WIFI_SETTINGS_URLS),
    "display": ("Displays", _DISPLAY_SETTINGS_URLS),
    "displays": ("Displays", _DISPLAY_SETTINGS_URLS),
    "显示器": ("Displays", _DISPLAY_SETTINGS_URLS),
    "显示": ("Displays", _DISPLAY_SETTINGS_URLS),
    "sound": ("Sound", _SOUND_SETTINGS_URLS),
    "sounds": ("Sound", _SOUND_SETTINGS_URLS),
    "声音": ("Sound", _SOUND_SETTINGS_URLS),
    "声音设置": ("Sound", _SOUND_SETTINGS_URLS),
    "keyboard": ("Keyboard", _KEYBOARD_SETTINGS_URLS),
    "键盘": ("Keyboard", _KEYBOARD_SETTINGS_URLS),
    "键盘设置": ("Keyboard", _KEYBOARD_SETTINGS_URLS),
    "notifications": ("Notifications", _NOTIFICATIONS_SETTINGS_URLS),
    "notification": ("Notifications", _NOTIFICATIONS_SETTINGS_URLS),
    "通知": ("Notifications", _NOTIFICATIONS_SETTINGS_URLS),
    "通知设置": ("Notifications", _NOTIFICATIONS_SETTINGS_URLS),
    "battery": ("Battery", _BATTERY_SETTINGS_URLS),
    "电池": ("Battery", _BATTERY_SETTINGS_URLS),
    "电池设置": ("Battery", _BATTERY_SETTINGS_URLS),
    "mouse": ("Mouse", _MOUSE_SETTINGS_URLS),
    "鼠标": ("Mouse", _MOUSE_SETTINGS_URLS),
    "鼠标设置": ("Mouse", _MOUSE_SETTINGS_URLS),
    "trackpad": ("Trackpad", _TRACKPAD_SETTINGS_URLS),
    "触控板": ("Trackpad", _TRACKPAD_SETTINGS_URLS),
    "触控板设置": ("Trackpad", _TRACKPAD_SETTINGS_URLS),
    "printers": ("Printers & Scanners", _PRINTERS_SETTINGS_URLS),
    "printersscanners": ("Printers & Scanners", _PRINTERS_SETTINGS_URLS),
    "printersandscanners": ("Printers & Scanners", _PRINTERS_SETTINGS_URLS),
    "打印机": ("Printers & Scanners", _PRINTERS_SETTINGS_URLS),
    "打印机与扫描仪": ("Printers & Scanners", _PRINTERS_SETTINGS_URLS),
    "打印机和扫描仪": ("Printers & Scanners", _PRINTERS_SETTINGS_URLS),
    "专注模式": ("Focus", _FOCUS_SETTINGS_URLS),
    "focus": ("Focus", _FOCUS_SETTINGS_URLS),
    "wallpaper": ("Wallpaper", _WALLPAPER_SETTINGS_URLS),
    "墙纸": ("Wallpaper", _WALLPAPER_SETTINGS_URLS),
    "壁纸": ("Wallpaper", _WALLPAPER_SETTINGS_URLS),
    "desktopdock": ("Desktop & Dock", _DESKTOP_DOCK_SETTINGS_URLS),
    "desktopanddock": ("Desktop & Dock", _DESKTOP_DOCK_SETTINGS_URLS),
    "桌面与程序坞": ("Desktop & Dock", _DESKTOP_DOCK_SETTINGS_URLS),
    "桌面和程序坞": ("Desktop & Dock", _DESKTOP_DOCK_SETTINGS_URLS),
    "程序坞": ("Desktop & Dock", _DESKTOP_DOCK_SETTINGS_URLS),
    "dock": ("Desktop & Dock", _DESKTOP_DOCK_SETTINGS_URLS),
    "screensaver": ("Screen Saver", _SCREEN_SAVER_SETTINGS_URLS),
    "screen saver": ("Screen Saver", _SCREEN_SAVER_SETTINGS_URLS),
    "屏幕保护程序": ("Screen Saver", _SCREEN_SAVER_SETTINGS_URLS),
    "屏幕保护": ("Screen Saver", _SCREEN_SAVER_SETTINGS_URLS),
    "siri": ("Siri", _SIRI_SETTINGS_URLS),
    "siri与聚焦": ("Siri", _SIRI_SETTINGS_URLS),
    "siri和聚焦": ("Siri", _SIRI_SETTINGS_URLS),
    "languageregion": ("Language & Region", _LANGUAGE_REGION_SETTINGS_URLS),
    "languageandregion": ("Language & Region", _LANGUAGE_REGION_SETTINGS_URLS),
    "语言与地区": ("Language & Region", _LANGUAGE_REGION_SETTINGS_URLS),
    "语言和地区": ("Language & Region", _LANGUAGE_REGION_SETTINGS_URLS),
    "datetime": ("Date & Time", _DATE_TIME_SETTINGS_URLS),
    "dateandtime": ("Date & Time", _DATE_TIME_SETTINGS_URLS),
    "日期与时间": ("Date & Time", _DATE_TIME_SETTINGS_URLS),
    "日期和时间": ("Date & Time", _DATE_TIME_SETTINGS_URLS),
    "softwareupdate": ("Software Update", _SOFTWARE_UPDATE_SETTINGS_URLS),
    "软件更新": ("Software Update", _SOFTWARE_UPDATE_SETTINGS_URLS),
    "storage": ("Storage", _STORAGE_SETTINGS_URLS),
    "储存空间": ("Storage", _STORAGE_SETTINGS_URLS),
    "存储空间": ("Storage", _STORAGE_SETTINGS_URLS),
    "loginitems": ("Login Items", _LOGIN_ITEMS_SETTINGS_URLS),
    "登录项": ("Login Items", _LOGIN_ITEMS_SETTINGS_URLS),
    "usersgroups": ("Users & Groups", _USERS_GROUPS_SETTINGS_URLS),
    "usersandgroups": ("Users & Groups", _USERS_GROUPS_SETTINGS_URLS),
    "用户与群组": ("Users & Groups", _USERS_GROUPS_SETTINGS_URLS),
    "用户和群组": ("Users & Groups", _USERS_GROUPS_SETTINGS_URLS),
    "accessibility": (
        "Accessibility Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",),
    ),
    "assistive": (
        "Accessibility Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",),
    ),
    "辅助功能": (
        "Accessibility Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",),
    ),
    "无障碍": (
        "Accessibility Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",),
    ),
    "screenrecording": (
        "Screen Recording Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",),
    ),
    "screencapture": (
        "Screen Recording Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",),
    ),
    "屏幕录制": (
        "Screen Recording Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",),
    ),
    "屏幕录像": (
        "Screen Recording Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",),
    ),
    "automation": (
        "Automation Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",),
    ),
    "appleevents": (
        "Automation Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",),
    ),
    "自动化": (
        "Automation Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",),
    ),
    "fulldiskaccess": (
        "Full Disk Access",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",),
    ),
    "完全磁盘访问": (
        "Full Disk Access",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",),
    ),
    "filesandfolders": (
        "Files and Folders Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_ApplicationData",),
    ),
    "文件和文件夹": (
        "Files and Folders Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_ApplicationData",),
    ),
    "inputmonitoring": (
        "Input Monitoring Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",),
    ),
    "输入监控": (
        "Input Monitoring Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",),
    ),
    "microphone": (
        "Microphone Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",),
    ),
    "麦克风": (
        "Microphone Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",),
    ),
    "camera": (
        "Camera Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_Camera",),
    ),
    "摄像头": (
        "Camera Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_Camera",),
    ),
    "相机": (
        "Camera Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_Camera",),
    ),
    "locationservices": (
        "Location Services Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_LocationServices",),
    ),
    "定位服务": (
        "Location Services Permission",
        ("x-apple.systempreferences:com.apple.preference.security?Privacy_LocationServices",),
    ),
}

_PERMISSION_CAPABILITY_TOOLS = {
    "screen_capture": ("screen.capture",),
    "active_window": (
        "desktop.active_window",
        "desktop.running_apps",
        "desktop.windows",
        "desktop.list_windows",
        "desktop.ui_elements",
        "desktop.read_ui",
        "desktop.inspect_app",
        "desktop.verify",
        "desktop.click_ui_element",
        "desktop.type_into_ui_element",
    ),
    "app_control": (
        "app.status",
        "app.open",
        "desktop.open_app",
        "desktop.inspect_app",
        "system.settings_open",
        "app.focus",
        "desktop.focus_app",
        "app.focus_window",
        "app.show",
        "app.hide",
        "app.minimize",
        "app.quit",
        "desktop.reveal_path",
        "desktop.open_path",
        "desktop.open_path_with_app",
        "system.display_sleep",
        "system.screen_saver_start",
        "notes.create",
        "reminders.create",
        "calendar.create_event",
    ),
    "media_control": (
        "media.system_control",
        "media.apple_music_play",
        "media.apple_music_status",
        "media.apple_music_open_and_play",
        "media.apple_music_control",
        "media.music_app_control",
    ),
    "foreground_activation": (
        "app.focus",
        "desktop.inspect_app",
        "desktop.focus_app",
        "app.focus_window",
        "app.show",
        "app.open_and_safe_type_text",
        "app.focus_and_safe_type_text",
        "app.open_and_safe_shortcut",
        "app.focus_and_safe_shortcut",
        "app.open_and_safe_key",
        "app.focus_and_safe_key",
        "app.open_and_hotkey",
        "app.focus_and_hotkey",
        "app.open_and_safe_scroll",
        "app.focus_and_safe_scroll",
        "app.open_and_safe_click",
        "app.focus_and_safe_click",
        "app.open_and_click_ui_element",
        "app.focus_and_click_ui_element",
        "app.open_and_type_into_ui_element",
        "app.focus_and_type_into_ui_element",
        "media.music_app_open_and_play",
        "media.music_app_control",
    ),
    "foreground_input": (
        "media.music_app_open_and_play",
        "media.music_app_control",
        "app.open_and_safe_type_text",
        "app.focus_and_safe_type_text",
        "app.open_and_safe_shortcut",
        "app.focus_and_safe_shortcut",
        "app.open_and_safe_key",
        "app.focus_and_safe_key",
        "app.open_and_hotkey",
        "app.focus_and_hotkey",
        "app.open_and_safe_scroll",
        "app.focus_and_safe_scroll",
        "app.open_and_safe_click",
        "app.focus_and_safe_click",
        "app.open_and_click_ui_element",
        "app.focus_and_click_ui_element",
        "app.open_and_type_into_ui_element",
        "app.focus_and_type_into_ui_element",
        "desktop.hide_app",
        "desktop.show_all_apps",
        "desktop.minimize_window",
        "desktop.close_window",
        "desktop.safe_shortcut",
        "desktop.shortcut",
        "desktop.safe_key",
        "desktop.search_submit",
        "desktop.submit_foreground",
        "desktop.safe_type_text",
        "desktop.type",
        "desktop.safe_click",
        "desktop.safe_scroll",
        "desktop.click_ui_element",
        "desktop.type_into_ui_element",
        "desktop.hotkey",
        "desktop.type_text",
        "desktop.click",
    ),
    "browser_control": (
        "browser.open_url",
        "browser.open_url_and_extract_text",
        "browser.open_url_and_screenshot",
        "browser.current_page",
        "browser.click",
        "browser.type_text",
        "browser.extract_text",
        "browser.screenshot",
    ),
}

_PERMISSION_RECOVERY_ACTIONS = {
    "screen_recording": (
        {
            "label": "打开屏幕录制权限",
            "tool": "system.settings_open",
            "input": {"target": "屏幕录制权限"},
            "permission_target": "screen_recording",
            "risk_level": "low",
        },
    ),
    "screen_capture_probe_failed": (
        {
            "label": "打开屏幕录制权限",
            "tool": "system.settings_open",
            "input": {"target": "屏幕录制权限"},
            "permission_target": "screen_recording",
            "risk_level": "low",
        },
    ),
    "automation": (
        {
            "label": "打开自动化权限",
            "tool": "system.settings_open",
            "input": {"target": "自动化权限"},
            "permission_target": "automation",
            "risk_level": "low",
        },
    ),
    "automation_or_accessibility": (
        {
            "label": "打开自动化权限",
            "tool": "system.settings_open",
            "input": {"target": "自动化权限"},
            "permission_target": "automation",
            "risk_level": "low",
        },
        {
            "label": "打开辅助功能权限",
            "tool": "system.settings_open",
            "input": {"target": "辅助功能权限"},
            "permission_target": "accessibility",
            "risk_level": "low",
        },
    ),
    "accessibility": (
        {
            "label": "打开辅助功能权限",
            "tool": "system.settings_open",
            "input": {"target": "辅助功能权限"},
            "permission_target": "accessibility",
            "risk_level": "low",
        },
    ),
    "foreground_focus": (
        {
            "label": "打开自动化权限",
            "tool": "system.settings_open",
            "input": {"target": "自动化权限"},
            "permission_target": "automation",
            "risk_level": "low",
        },
        {
            "label": "打开辅助功能权限",
            "tool": "system.settings_open",
            "input": {"target": "辅助功能权限"},
            "permission_target": "accessibility",
            "risk_level": "low",
        },
    ),
    "input_monitoring": (
        {
            "label": "打开输入监控权限",
            "tool": "system.settings_open",
            "input": {"target": "输入监控"},
            "permission_target": "input_monitoring",
            "risk_level": "low",
        },
    ),
    "full_disk_access": (
        {
            "label": "打开完全磁盘访问权限",
            "tool": "system.settings_open",
            "input": {"target": "完全磁盘访问"},
            "permission_target": "full_disk_access",
            "risk_level": "low",
        },
    ),
    "files_and_folders": (
        {
            "label": "打开文件和文件夹权限",
            "tool": "system.settings_open",
            "input": {"target": "文件和文件夹"},
            "permission_target": "files_and_folders",
            "risk_level": "low",
        },
    ),
    "microphone": (
        {
            "label": "打开麦克风权限",
            "tool": "system.settings_open",
            "input": {"target": "麦克风"},
            "permission_target": "microphone",
            "risk_level": "low",
        },
    ),
    "camera": (
        {
            "label": "打开摄像头权限",
            "tool": "system.settings_open",
            "input": {"target": "摄像头"},
            "permission_target": "camera",
            "risk_level": "low",
        },
    ),
    "music_app": (
        {
            "label": "打开 Apple Music",
            "tool": "app.open",
            "input": {"app_name": "Music"},
            "permission_target": "music_app",
            "risk_level": "low",
        },
    ),
    "chrome_cdp": (
        {
            "label": "打开 Google Chrome",
            "tool": "app.open",
            "input": {"app_name": "Google Chrome"},
            "permission_target": "chrome_cdp",
            "risk_level": "low",
        },
    ),
}


def screen_capture(target_path: Path) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("screen.capture")
    from apps.locald.screenshot import capture_screenshot_to_file

    try:
        metadata = capture_screenshot_to_file(target_path)
    except Exception as exc:
        return _error("screen.capture", exc)
    payload = {
        "ok": True,
        "action": "screen.capture",
        "summary": (
            f"Captured screen {metadata.get('width', 0)}x{metadata.get('height', 0)} "
            f"{str(metadata.get('format') or 'png').upper()}"
        ),
        "data": metadata,
        "permission_error": False,
        "fallback_used": False,
    }
    if metadata.get("blank_frame") is True:
        data = payload["data"] if isinstance(payload["data"], dict) else {}
        data["visibility_limited"] = True
        data["blocking_condition"] = "screen_capture_blank"
        payload.update(
            {
                "summary": (
                    f"{payload['summary']} but the frame is blank/black; "
                    "foreground desktop interaction is not observable"
                ),
                "blocking_condition": "screen_capture_blank",
                "recommended_tools": ["desktop.active_window", "desktop.permissions"],
                "recovery_hints": [
                    "Unlock or wake the active macOS desktop session, then retry screen.capture.",
                    "If the screen is unlocked, verify the display is not blacked out by remote session state.",
                ],
            }
        )
    return payload


def active_window(*, timeout_seconds: float = 10.0) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.active_window")
    script = """
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        set appName to name of frontApp
        set appPID to unix id of frontApp
        try
            set frontWindow to front window of frontApp
            set winTitle to name of frontWindow
        on error
            set winTitle to ""
        end try
        set winID to ""
        try
            set winID to value of attribute "AXWindowNumber" of frontWindow as text
        on error
            try
                set winID to id of frontWindow as text
            end try
        end try
        return appName & "|" & appPID & "|" & winTitle & "|" & winID
    end tell
    """
    result = _call_with_bounded_timeout(
        _run_osascript,
        script,
        timeout_seconds=timeout_seconds,
    )
    if not result["ok"]:
        appkit_frontmost = _appkit_frontmost_app_name()
        frontmost_app = str(appkit_frontmost.get("app_name") or "")
        if _desktop_session_is_locked(frontmost_app):
            return _desktop_session_locked_result(
                "desktop.active_window",
                data={"frontmost_app": frontmost_app},
                fallback_result={
                    "system_events": result,
                    "appkit": appkit_frontmost,
                },
            )
        return _with_permission_metadata(
            "desktop.active_window",
            {**result, "action": "desktop.active_window", "summary": "desktop.active_window failed"},
        )
    parts = str(result.get("stdout") or "").strip().split("|", 3)
    app_name = parts[0] if len(parts) > 0 else ""
    pid_text = parts[1] if len(parts) > 1 else ""
    title = parts[2] if len(parts) > 2 else ""
    window_id_text = parts[3] if len(parts) > 3 else ""
    return {
        "ok": True,
        "action": "desktop.active_window",
        "summary": f"Active window: {app_name}{f' - {title}' if title else ''}",
        "data": {
            "app_name": app_name,
            "pid": int(pid_text) if pid_text.isdigit() else None,
            "title": title,
            **(
                {"window_id": int(window_id_text)}
                if window_id_text.isdigit()
                else {}
            ),
        },
        "permission_error": False,
        "fallback_used": False,
    }


def ui_elements(
    role_filter: str = "",
    limit: Any = 80,
    app_name: str = "",
) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.ui_elements")
    clean_filter = str(role_filter or "").strip()
    clean_app = str(app_name or "").strip()
    resolved_app = _resolve_installed_app_name(clean_app) if clean_app else ""
    app_filter = resolved_app or clean_app
    try:
        clean_limit = max(1, min(200, int(limit or 80)))
    except (TypeError, ValueError):
        clean_limit = 80
    script = """
    on replaceText(findText, replaceTextValue, sourceText)
        set oldDelimiters to AppleScript's text item delimiters
        set AppleScript's text item delimiters to findText
        set textItems to text items of sourceText
        set AppleScript's text item delimiters to replaceTextValue
        set joinedText to textItems as text
        set AppleScript's text item delimiters to oldDelimiters
        return joinedText
    end replaceText

    on cleanText(valueToClean)
        if valueToClean is missing value then return ""
        try
            set cleaned to valueToClean as text
        on error
            set cleaned to ""
        end try
        set cleaned to my replaceText(tab, " ", cleaned)
        set cleaned to my replaceText(linefeed, " ", cleaned)
        set cleaned to my replaceText(return, " ", cleaned)
        return cleaned
    end cleanText

    on joinRows(rowList)
        set oldDelimiters to AppleScript's text item delimiters
        set AppleScript's text item delimiters to linefeed
        set joinedRows to rowList as text
        set AppleScript's text item delimiters to oldDelimiters
        return joinedRows
    end joinRows

    on elementRow(targetElement, depthValue)
        set roleText to ""
        set subroleText to ""
        set nameText to ""
        set descriptionText to ""
        set valueText to ""
        set enabledText to ""
        set xText to ""
        set yText to ""
        set widthText to ""
        set heightText to ""
        tell application "System Events"
            try
                set elementProperties to properties of targetElement
                set roleText to my cleanText(role of elementProperties)
                set subroleText to my cleanText(subrole of elementProperties)
                set nameText to my cleanText(name of elementProperties)
                if nameText is "" then set nameText to my cleanText(help of elementProperties)
                set descriptionText to my cleanText(description of elementProperties)
                set valueText to my cleanText(value of elementProperties)
                set enabledText to my cleanText(enabled of elementProperties)
                set positionValue to position of elementProperties
                set xText to item 1 of positionValue as text
                set yText to item 2 of positionValue as text
                set sizeValue to size of elementProperties
                set widthText to item 1 of sizeValue as text
                set heightText to item 2 of sizeValue as text
            end try
        end tell
        return (depthValue as text) & tab & roleText & tab & subroleText & tab & nameText & tab & descriptionText & tab & valueText & tab & enabledText & tab & xText & tab & yText & tab & widthText & tab & heightText
    end elementRow

    on collectElements(containerElement, depthValue, maxDepth, maxItems)
        set elementRows to {}
        if depthValue > maxDepth then return elementRows
        try
            tell application "System Events"
                set childElements to every UI element of containerElement
            end tell
        on error
            return elementRows
        end try
        repeat with childElement in childElements
            if (count of elementRows) >= maxItems then exit repeat
            set end of elementRows to my elementRow(childElement, depthValue)
            if depthValue < maxDepth then
                set childRows to my collectElements(childElement, depthValue + 1, maxDepth, maxItems - (count of elementRows))
                repeat with childRow in childRows
                    if (count of elementRows) >= maxItems then exit repeat
                    set end of elementRows to childRow as text
                end repeat
            end if
        end repeat
        return elementRows
    end collectElements

    on run argv
        set maxItems to item 1 of argv as integer
        set maxDepth to item 2 of argv as integer
        set appFilter to item 3 of argv
        tell application "System Events"
            if appFilter is "" then
                set targetApp to first application process whose frontmost is true
            else
                set matchingApps to application processes whose name is appFilter
                if (count of matchingApps) is 0 then
                    return "META" & tab & appFilter & tab & "" & tab & "" & tab & ""
                end if
                set targetApp to item 1 of matchingApps
            end if
            set appName to my cleanText(name of targetApp)
            set appPID to unix id of targetApp
            try
                set targetWindow to front window of targetApp
                set windowTitle to my cleanText(name of targetWindow)
                set windowID to ""
                try
                    set windowID to value of attribute "AXWindowNumber" of targetWindow as text
                on error
                    try
                        set windowID to id of targetWindow as text
                    end try
                end try
                set elementRows to my collectElements(targetWindow, 0, maxDepth, maxItems)
            on error
                set windowTitle to ""
                set windowID to ""
                set elementRows to my collectElements(targetApp, 0, maxDepth, maxItems)
            end try
            set header to "META" & tab & appName & tab & (appPID as text) & tab & windowTitle & tab & windowID
            if (count of elementRows) is 0 then return header
            return header & linefeed & my joinRows(elementRows)
        end tell
    end run
    """
    result = _run_osascript(script, [str(clean_limit), "6", app_filter])
    if not result["ok"]:
        return _with_permission_metadata(
            "desktop.ui_elements",
            {
                **result,
                "action": "desktop.ui_elements",
                "summary": "desktop.ui_elements failed",
                "data": {
                    "app_name": app_filter,
                    **_app_resolution_metadata(clean_app, app_filter),
                },
            },
        )
    parsed = _parse_ui_elements_output(result.get("stdout"), clean_filter, clean_limit)
    inspection_metadata = _ui_inspection_metadata(
        parsed.get("elements", []),
        app_name=parsed.get("app_name", "") or app_filter,
        title=parsed.get("title", ""),
    )
    return {
        "ok": True,
        "action": "desktop.ui_elements",
        "summary": _ui_elements_summary(parsed.get("elements", []), parsed.get("app_name", "")),
        "data": {
            **parsed,
            "role_filter": clean_filter,
            "limit": clean_limit,
            **inspection_metadata,
            **_app_resolution_metadata(clean_app, app_filter),
        },
        "permission_error": False,
        "fallback_used": bool(clean_app and app_filter != clean_app),
    }


def click_ui_element(
    target: str,
    *,
    role_filter: str = "",
    limit: Any = 80,
    click_count: Any = 1,
    expected_app_name: str = "",
) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.click_ui_element")
    clean_target = _clean_required(target, "target")
    clean_filter = str(role_filter or "").strip()
    clean_count = _clean_click_count(click_count)
    clean_expected_app = str(expected_app_name or "").strip()
    observed = ui_elements(role_filter=clean_filter, limit=limit)
    observed_data = observed.get("data") if isinstance(observed.get("data"), dict) else {}
    if not observed.get("ok"):
        payload = {
            **observed,
            "action": "desktop.click_ui_element",
            "summary": "Could not observe foreground UI elements before clicking",
            "data": {
                **dict(observed_data),
                "target": clean_target,
                "role_filter": clean_filter,
                "click_count": clean_count,
            },
            "fallback_result": {"observe": observed},
        }
        return _with_permission_metadata("desktop.click_ui_element", payload)

    elements = observed_data.get("elements") if isinstance(observed_data.get("elements"), list) else []
    observed_app_name = str(observed_data.get("app_name") or "").strip()
    if clean_expected_app and not _app_name_matches_expected(clean_expected_app, observed_app_name):
        return {
            "ok": False,
            "action": "desktop.click_ui_element",
            "summary": "Foreground app changed before clicking UI element",
            "error": "foreground_app_mismatch",
            "data": {
                "target": clean_target,
                "role_filter": clean_filter,
                "click_count": clean_count,
                "expected_app_name": clean_expected_app,
                "observed_app_name": observed_app_name,
                "app_name": observed_app_name,
                "title": str(observed_data.get("title") or ""),
                "observed_count": len(elements),
                "candidates": _candidate_ui_element_previews(elements),
                "recommended_tools": ["app.focus", "desktop.active_window", "screen.capture"],
            },
            "permission_error": False,
            "fallback_used": False,
            "fallback_result": {"observe": observed},
        }
    matches = _matching_ui_elements(elements, clean_target, clean_filter)
    if not matches:
        recovery_actions = _ui_element_not_found_recovery_actions(
            clean_target,
            clean_filter,
            clean_count,
        )
        return {
            "ok": False,
            "action": "desktop.click_ui_element",
            "summary": f"No foreground UI element matched: {clean_target}",
            "error": "ui_element_not_found",
            "fallback": "screen.capture",
            "data": {
                "target": clean_target,
                "role_filter": clean_filter,
                "click_count": clean_count,
                "app_name": str(observed_data.get("app_name") or ""),
                "title": str(observed_data.get("title") or ""),
                "observed_count": len(elements),
                "inspection_level": str(observed_data.get("inspection_level") or ""),
                "visibility_status": str(observed_data.get("visibility_status") or ""),
                "visibility_limited": observed_data.get("visibility_limited") is True,
                "candidates": _candidate_ui_element_previews(elements),
                "recommended_tools": ["screen.capture", "desktop.click"],
                "recovery_actions": recovery_actions,
            },
            "permission_error": False,
            "fallback_used": False,
            "recovery_actions": recovery_actions,
            "fallback_result": {"observe": observed},
        }

    match = matches[0]
    center = match.get("center") if isinstance(match.get("center"), dict) else {}
    x = center.get("x")
    y = center.get("y")
    click_result = _send_desktop_click(
        "desktop.click_ui_element",
        x,
        y,
        click_count=clean_count,
    )
    click_data = click_result.get("data") if isinstance(click_result.get("data"), dict) else {}
    label = _ui_element_display_label(match) or clean_target
    data = {
        **dict(click_data),
        "target": clean_target,
        "matched_label": label,
        "role_filter": clean_filter,
        "app_name": str(observed_data.get("app_name") or ""),
        "title": str(observed_data.get("title") or ""),
        "observed_count": len(elements),
        "match_count": len(matches),
        "element": match,
    }
    if click_result.get("ok"):
        return {
            **click_result,
            "summary": f"Clicked foreground UI element: {label}",
            "data": data,
            "fallback_result": {"observe": observed},
        }
    payload = {
        **click_result,
        "action": "desktop.click_ui_element",
        "summary": f"Matched foreground UI element but click failed: {label}",
        "data": data,
        "fallback_result": {"observe": observed},
    }
    return _with_permission_metadata("desktop.click_ui_element", payload)


def type_into_ui_element(
    target: str,
    text: str,
    *,
    role_filter: str = "",
    limit: Any = 80,
    expected_app_name: str = "",
) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.type_into_ui_element")
    clean_target = _clean_required(target, "target")
    clean_text = _clean_required(text, "text")
    clean_filter = str(role_filter or "").strip() or "text"
    clean_expected_app = str(expected_app_name or "").strip()
    observed = ui_elements(role_filter=clean_filter, limit=limit)
    observed_data = observed.get("data") if isinstance(observed.get("data"), dict) else {}
    if not observed.get("ok"):
        payload = {
            **observed,
            "action": "desktop.type_into_ui_element",
            "summary": "Could not observe foreground UI elements before typing",
            "data": {
                **dict(observed_data),
                "target": clean_target,
                "role_filter": clean_filter,
                "character_count": len(clean_text),
            },
            "fallback_result": {"observe": observed},
        }
        return _with_permission_metadata("desktop.type_into_ui_element", payload)

    elements = observed_data.get("elements") if isinstance(observed_data.get("elements"), list) else []
    observed_app_name = str(observed_data.get("app_name") or "").strip()
    if clean_expected_app and not _app_name_matches_expected(clean_expected_app, observed_app_name):
        return {
            "ok": False,
            "action": "desktop.type_into_ui_element",
            "summary": "Foreground app changed before typing into UI element",
            "error": "foreground_app_mismatch",
            "data": {
                "target": clean_target,
                "role_filter": clean_filter,
                "character_count": len(clean_text),
                "expected_app_name": clean_expected_app,
                "observed_app_name": observed_app_name,
                "app_name": observed_app_name,
                "title": str(observed_data.get("title") or ""),
                "observed_count": len(elements),
                "candidates": _candidate_ui_element_previews(elements),
                "recommended_tools": ["app.focus", "desktop.active_window", "screen.capture"],
            },
            "permission_error": False,
            "fallback_used": False,
            "fallback_result": {"observe": observed},
        }
    matches = _matching_ui_elements(elements, clean_target, clean_filter)
    if not matches:
        recovery_actions = _ui_element_type_not_found_recovery_actions(
            clean_target,
            clean_filter,
            len(clean_text),
        )
        return {
            "ok": False,
            "action": "desktop.type_into_ui_element",
            "summary": f"No foreground UI element matched for typing: {clean_target}",
            "error": "ui_element_not_found",
            "fallback": "screen.capture",
            "data": {
                "target": clean_target,
                "role_filter": clean_filter,
                "character_count": len(clean_text),
                "app_name": str(observed_data.get("app_name") or ""),
                "title": str(observed_data.get("title") or ""),
                "observed_count": len(elements),
                "inspection_level": str(observed_data.get("inspection_level") or ""),
                "visibility_status": str(observed_data.get("visibility_status") or ""),
                "visibility_limited": observed_data.get("visibility_limited") is True,
                "candidates": _candidate_ui_element_previews(elements),
                "recommended_tools": ["screen.capture", "desktop.click", "desktop.type_text"],
                "recovery_actions": recovery_actions,
            },
            "permission_error": False,
            "fallback_used": False,
            "recovery_actions": recovery_actions,
            "fallback_result": {"observe": observed},
        }

    match = matches[0]
    center = match.get("center") if isinstance(match.get("center"), dict) else {}
    label = _ui_element_display_label(match) or clean_target
    click_result = _send_desktop_click(
        "desktop.type_into_ui_element",
        center.get("x"),
        center.get("y"),
        click_count=1,
    )
    click_data = click_result.get("data") if isinstance(click_result.get("data"), dict) else {}
    base_data = {
        **dict(click_data),
        "target": clean_target,
        "matched_label": label,
        "role_filter": clean_filter,
        "character_count": len(clean_text),
        "app_name": str(observed_data.get("app_name") or ""),
        "title": str(observed_data.get("title") or ""),
        "observed_count": len(elements),
        "match_count": len(matches),
        "element": match,
    }
    if not click_result.get("ok"):
        payload = {
            **click_result,
            "action": "desktop.type_into_ui_element",
            "summary": f"Matched foreground UI element but focus click failed: {label}",
            "data": base_data,
            "fallback_result": {"observe": observed, "focus": click_result},
        }
        return _with_permission_metadata("desktop.type_into_ui_element", payload)

    type_result = _send_desktop_text(
        "desktop.type_into_ui_element",
        clean_text,
        summary=f"Typed into foreground UI element: {label}",
    )
    type_data = type_result.get("data") if isinstance(type_result.get("data"), dict) else {}
    data = {**base_data, **dict(type_data)}
    if type_result.get("ok"):
        return {
            **type_result,
            "summary": f"Typed into foreground UI element: {label}",
            "data": data,
            "fallback_result": {"observe": observed, "focus": click_result, "type_text": type_result},
        }
    payload = {
        **type_result,
        "action": "desktop.type_into_ui_element",
        "summary": f"Focused foreground UI element but typing failed: {label}",
        "data": data,
        "fallback_result": {"observe": observed, "focus": click_result, "type_text": type_result},
    }
    return _with_permission_metadata("desktop.type_into_ui_element", payload)


def permissions(*, active_verification: Any = False) -> dict[str, Any]:
    """Return passive cached readiness unless interactive verification is explicit."""

    if not _clean_bool(active_verification, default=False):
        preflight = permission_preflight()
        return {
            **preflight,
            "action": "desktop.permissions",
            "active_verification": False,
        }

    from apps.shell.yachiyo_agent.desktop_permissions import (
        desktop_permission_missing_by_capability,
    )

    try:
        missing_by_capability = desktop_permission_missing_by_capability(use_cache=True)
    except Exception as exc:
        return _error("desktop.permissions", exc)

    clean_missing = _clean_missing_permissions_by_capability(missing_by_capability)
    runtime_blockers = _desktop_runtime_blocking_conditions(use_cache=True)
    missing_targets = _ordered_unique(
        target for targets in clean_missing.values() for target in targets
    )
    blocking_conditions = _ordered_unique(
        condition for conditions in runtime_blockers.values() for condition in conditions
    )
    affected_tools = _ordered_unique(
        [
            *_affected_tools_for_missing_permissions(clean_missing),
            *_affected_tools_for_missing_permissions(runtime_blockers),
        ]
    )
    ready = not missing_targets and not blocking_conditions
    summary = _desktop_permissions_summary(
        missing_targets,
        affected_tools,
        blocking_conditions=blocking_conditions,
    )
    recovery_hints = [
        *_permission_recovery_hints_for_targets(missing_targets),
        *_runtime_blocking_recovery_hints_for_conditions(blocking_conditions),
    ]
    recovery_actions = [
        *_permission_recovery_actions_for_targets(missing_targets),
        *_runtime_blocking_recovery_actions_for_conditions(blocking_conditions),
    ]
    return {
        "ok": True,
        "action": "desktop.permissions",
        "summary": summary,
        "data": {
            "checked": True,
            "diagnostic_status": "verified",
            "active_verification": True,
            "ready": ready,
            "missing_permissions": clean_missing,
            "permission_targets": missing_targets,
            "runtime_blocking_conditions": runtime_blockers,
            "blocking_conditions": blocking_conditions,
            "affected_tools": affected_tools,
            "recovery_actions": recovery_actions,
            "diagnostic_route": "/yachiyo/readiness",
        },
        "missing_permissions": missing_targets,
        "permission_targets": missing_targets,
        "runtime_blocking_conditions": runtime_blockers,
        "blocking_conditions": blocking_conditions,
        "affected_tools": affected_tools,
        "recovery_hints": recovery_hints,
        "recovery_actions": recovery_actions,
        "diagnostic_route": "/yachiyo/readiness",
        "checked": True,
        "diagnostic_status": "verified",
        "active_verification": True,
        "runtime_blocked": bool(blocking_conditions),
        "permission_error": bool(missing_targets),
        "fallback_used": False,
    }


def permission_preflight() -> dict[str, Any]:
    """Return cached desktop permission readiness without running fresh probes."""

    from apps.shell.yachiyo_agent.desktop_permissions import (
        cached_desktop_permission_diagnostics,
    )

    cache_status = cached_desktop_permission_diagnostics()
    clean_missing = _clean_missing_permissions_by_capability(
        cache_status.get("missing_permissions") or {}
    )
    runtime_blockers = _clean_missing_permissions_by_capability(
        cache_status.get("blocking_conditions") or {}
    )
    missing_targets = _ordered_unique(
        target for targets in clean_missing.values() for target in targets
    )
    blocking_conditions = _ordered_unique(
        condition for conditions in runtime_blockers.values() for condition in conditions
    )
    affected_tools = _ordered_unique(
        [
            *_affected_tools_for_missing_permissions(clean_missing),
            *_affected_tools_for_missing_permissions(runtime_blockers),
        ]
    )
    checked = cache_status.get("checked") is True
    ready = checked and not missing_targets and not blocking_conditions
    summary = (
        _desktop_permissions_summary(
            missing_targets,
            affected_tools,
            blocking_conditions=blocking_conditions,
        )
        if checked
        else (
            "Desktop permission status has not been interactively verified; "
            "no app was activated and no Apple Event was sent."
        )
    )
    recovery_hints = [
        *_permission_recovery_hints_for_targets(missing_targets),
        *_runtime_blocking_recovery_hints_for_conditions(blocking_conditions),
    ]
    recovery_actions = [
        *_permission_recovery_actions_for_targets(missing_targets),
        *_runtime_blocking_recovery_actions_for_conditions(blocking_conditions),
    ]
    return {
        "ok": True,
        "action": "desktop.permission_preflight",
        "summary": summary,
        "data": {
            "checked": checked,
            "diagnostic_status": str(cache_status.get("status") or "not_checked"),
            "active_verification": False,
            "ready": ready,
            "missing_permissions": clean_missing,
            "permission_targets": missing_targets,
            "runtime_blocking_conditions": runtime_blockers,
            "blocking_conditions": blocking_conditions,
            "affected_tools": affected_tools,
            "recovery_actions": recovery_actions,
            "diagnostic_route": "/yachiyo/readiness",
        },
        "missing_permissions": missing_targets,
        "permission_targets": missing_targets,
        "runtime_blocking_conditions": runtime_blockers,
        "blocking_conditions": blocking_conditions,
        "affected_tools": affected_tools,
        "recovery_hints": recovery_hints,
        "recovery_actions": recovery_actions,
        "diagnostic_route": "/yachiyo/readiness",
        "checked": checked,
        "diagnostic_status": str(cache_status.get("status") or "not_checked"),
        "active_verification": False,
        "runtime_blocked": bool(blocking_conditions),
        "permission_error": bool(missing_targets),
        "fallback_used": False,
    }


def running_apps() -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.running_apps")
    script = """
    tell application "System Events"
        set appNames to name of (application processes whose background only is false)
        set frontName to ""
        try
            set frontName to name of first application process whose frontmost is true
        end try
        set appRows to {}
        repeat with appNameRef in appNames
            set appName to appNameRef as text
            set appFront to appName is frontName
            set end of appRows to appName & "||" & appFront
        end repeat
        set AppleScript's text item delimiters to linefeed
        set output to appRows as text
        set AppleScript's text item delimiters to ""
        return output
    end tell
    """
    result = _run_osascript(script)
    if not result["ok"]:
        return _with_permission_metadata(
            "desktop.running_apps",
            {**result, "action": "desktop.running_apps", "summary": "desktop.running_apps failed"},
        )
    apps = _parse_running_apps(result.get("stdout"))
    names = [str(app.get("name") or "") for app in apps if str(app.get("name") or "")]
    return {
        "ok": True,
        "action": "desktop.running_apps",
        "summary": _running_apps_summary(names),
        "data": {
            "apps": apps,
            "count": len(apps),
            "frontmost": next((app.get("name") for app in apps if app.get("frontmost")), ""),
        },
        "permission_error": False,
        "fallback_used": False,
    }


def list_apps(query: str = "", limit: Any = 200) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.list_apps")
    clean_query = str(query or "").strip()
    try:
        clean_limit = max(1, min(500, int(limit or 200)))
    except (TypeError, ValueError):
        clean_limit = 200
    apps = _installed_app_match_candidates(clean_query) if clean_query else [
        {
            "name": bundle.stem,
            "path": str(bundle),
            "match_score": 0,
        }
        for bundle in _iter_installed_app_bundles()
    ]
    if clean_query:
        apps.sort(
            key=lambda item: (
                -int(item["match_score"]),
                len(str(item["name"])),
                str(item["name"]),
            )
        )
    else:
        apps.sort(key=lambda item: str(item["name"]).casefold())
    total_count = len(apps)
    limited_apps = apps[:clean_limit]
    names = [str(app.get("name") or "") for app in limited_apps[:5]]
    if clean_query:
        summary = (
            f"Installed apps matching {clean_query}: {', '.join(names)}"
            if names
            else f"No installed apps matching {clean_query}"
        )
    else:
        summary = f"Installed apps: {', '.join(names)}" if names else "No installed apps found"
    best_match = limited_apps[0] if clean_query and limited_apps else None
    resolution = _app_discovery_resolution(clean_query, best_match)
    data = {
        "query": clean_query,
        "apps": limited_apps,
        "count": len(limited_apps),
        "total_count": total_count,
        "truncated": total_count > len(limited_apps),
    }
    if clean_query:
        data["normalized_query"] = _compact_app_match_name(clean_query)
    if best_match is not None:
        data["best_match"] = best_match
    if resolution:
        data["resolution"] = resolution
    return {
        "ok": True,
        "action": "desktop.list_apps",
        "summary": summary,
        "data": data,
        "permission_error": False,
        "fallback_used": False,
    }


def windows(app_name: str = "") -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.windows")
    clean_app = str(app_name or "").strip()
    resolved_app = _resolve_installed_app_name(clean_app) if clean_app else ""
    app_filter = resolved_app or clean_app
    if app_filter:
        script = """
        on run argv
            set appFilter to item 1 of argv
            tell application "System Events"
                set windowRows to {}
                if not (exists application process appFilter) then
                    return ""
                end if
                set proc to application process appFilter
                set appName to name of proc
                set appPID to unix id of proc
                set appFront to frontmost of proc
                try
                    set winCount to count of windows of proc
                on error
                    set winCount to 0
                end try
                repeat with winIndex from 1 to winCount
                    try
                        set winTitle to name of window winIndex of proc
                    on error
                        set winTitle to ""
                    end try
                    set end of windowRows to appName & tab & appPID & tab & winIndex & tab & appFront & tab & winTitle
                end repeat
                set AppleScript's text item delimiters to linefeed
                set output to windowRows as text
                set AppleScript's text item delimiters to ""
                return output
            end tell
        end run
        """
    else:
        script = """
        on run argv
            set appFilter to item 1 of argv
            tell application "System Events"
                set windowRows to {}
                repeat with proc in (application processes whose background only is false)
                    set appName to name of proc
                    if appFilter is "" or appName is appFilter then
                        set appPID to unix id of proc
                        set appFront to frontmost of proc
                        try
                            set winCount to count of windows of proc
                        on error
                            set winCount to 0
                        end try
                        repeat with winIndex from 1 to winCount
                            try
                                set winTitle to name of window winIndex of proc
                            on error
                                set winTitle to ""
                            end try
                            set end of windowRows to appName & tab & appPID & tab & winIndex & tab & appFront & tab & winTitle
                        end repeat
                    end if
                end repeat
                set AppleScript's text item delimiters to linefeed
                set output to windowRows as text
                set AppleScript's text item delimiters to ""
                return output
            end tell
        end run
        """
    result = _run_osascript(script, [app_filter])
    if not result["ok"]:
        return _with_permission_metadata(
            "desktop.windows",
            {
                **result,
                "action": "desktop.windows",
                "summary": "desktop.windows failed",
                "data": {
                    "app_name": app_filter,
                    **_app_resolution_metadata(clean_app, app_filter),
                },
            },
        )
    windows_payload = _parse_window_rows(result.get("stdout"))
    visibility_metadata = (
        _window_visibility_metadata(app_filter, windows_payload)
        if not windows_payload
        else {}
    )
    return {
        "ok": True,
        "action": "desktop.windows",
        "summary": _windows_summary(windows_payload, app_filter),
        "data": {
            "app_name": app_filter,
            "windows": windows_payload,
            "count": len(windows_payload),
            **visibility_metadata,
            **_app_resolution_metadata(clean_app, app_filter),
        },
        "permission_error": False,
        "fallback_used": bool(clean_app and app_filter != clean_app),
    }


def app_status(app_name: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("app.status")
    clean_name = _clean_required(app_name, "app_name")
    resolved_app = _resolve_installed_app(clean_name)
    resolved_metadata = resolved_app.get("metadata")
    if not isinstance(resolved_metadata, Mapping):
        resolved_metadata = {}
    bundle_id = str(resolved_metadata.get("bundle_id") or "").strip()
    candidate_names = _ordered_unique(
        [
            resolved_app.get("matched_name"),
            resolved_app.get("name"),
            clean_name,
        ]
    )
    criteria = json.dumps(
        {"bundle_id": bundle_id, "names": candidate_names},
        ensure_ascii=False,
    )
    result = _run_jxa(
        """
        function run(argv) {
            ObjC.import("AppKit");
            const criteria = JSON.parse(String(argv[0] || "{}"));
            const requestedBundleId = String(criteria.bundle_id || "")
                .trim()
                .toLocaleLowerCase();
            const requestedNames = Array.isArray(criteria.names)
                ? criteria.names
                    .map(function (name) {
                        return String(name || "").trim().toLocaleLowerCase();
                    })
                    .filter(function (name) { return Boolean(name); })
                : [];
            const apps = $.NSWorkspace.sharedWorkspace.runningApplications;

            if (requestedBundleId) {
                for (let index = 0; index < apps.count; index += 1) {
                    const app = apps.objectAtIndex(index);
                    const bundleId = app.bundleIdentifier
                        ? String(ObjC.unwrap(app.bundleIdentifier)).toLocaleLowerCase()
                        : "";
                    if (bundleId === requestedBundleId) {
                        return JSON.stringify({running: true});
                    }
                }
            }

            for (let index = 0; index < apps.count; index += 1) {
                const app = apps.objectAtIndex(index);
                const localizedName = app.localizedName
                    ? String(ObjC.unwrap(app.localizedName)).trim().toLocaleLowerCase()
                    : "";
                if (localizedName && requestedNames.indexOf(localizedName) !== -1) {
                    return JSON.stringify({running: true});
                }
            }
            return JSON.stringify({running: false});
        }
        """,
        [criteria],
    )
    if not result.get("ok"):
        return _with_permission_metadata(
            "app.status",
            {
                **result,
                "action": "app.status",
                "summary": "app.status failed",
                "data": {"app_name": clean_name},
            },
        )
    try:
        status_payload = json.loads(str(result.get("stdout") or ""))
    except (TypeError, ValueError):
        payload = _error("app.status", ValueError("invalid app status response"))
        payload["data"] = {"app_name": clean_name}
        return payload
    if not isinstance(status_payload, Mapping) or not isinstance(
        status_payload.get("running"), bool
    ):
        payload = _error("app.status", ValueError("invalid app status response"))
        payload["data"] = {"app_name": clean_name}
        return payload
    running = status_payload["running"]
    status = "running" if running else "not_running"
    return {
        "ok": True,
        "action": "app.status",
        "summary": f"{clean_name} is {'running' if running else 'not running'}",
        "data": {
            "app_name": clean_name,
            "running": running,
            "status": status or "unknown",
        },
        "permission_error": False,
        "fallback_used": False,
    }


def app_open(app_name: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("app.open")
    clean_name = _clean_required(app_name, "app_name")
    settings_target = _system_settings_target(clean_name)
    if settings_target is not None:
        return _open_system_settings_target(clean_name, settings_target)
    folder_path = _common_folder_path(clean_name)
    if folder_path is not None:
        return _open_common_folder(clean_name, folder_path)
    resolved_name = clean_name
    app_resolution = "requested_app_name"
    resolution_metadata: dict[str, Any] = {}
    try:
        result = _run_open_app(resolved_name)
    except Exception as exc:
        return _error("app.open", exc)
    if result.returncode != 0:
        resolved_app = _resolve_installed_app(clean_name)
        resolved_name = str(resolved_app.get("name") or "").strip()
        if not resolved_name or resolved_name == clean_name:
            return _app_open_failed(clean_name, result)
        try:
            resolved_result = _run_open_app(resolved_name)
        except Exception as exc:
            return _error("app.open", exc)
        if resolved_result.returncode != 0:
            return _app_open_failed(clean_name, result)
        result = resolved_result
        app_resolution = "installed_app_bundle"
        resolution_metadata = _app_resolution_metadata_from_match(clean_name, resolved_app)
    verification = _app_running_verification(resolved_name)
    data = {"app_name": resolved_name, **verification}
    if resolved_name != clean_name:
        data.update(resolution_metadata or _app_resolution_metadata(clean_name, resolved_name))
        data["app_resolution"] = app_resolution
    return {
        "ok": True,
        "action": "app.open",
        "summary": f"Opened {resolved_name}",
        "data": data,
        "permission_error": False,
        "fallback_used": resolved_name != clean_name,
    }


def inspect_app(
    app_name: str,
    *,
    open_if_needed: Any = False,
    focus: Any = False,
    role_filter: str = "",
    limit: Any = 80,
) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.inspect_app")
    clean_name = _clean_required(app_name, "app_name")
    clean_open = _clean_bool(open_if_needed, default=False)
    clean_focus = _clean_bool(focus, default=False)
    clean_filter = str(role_filter or "").strip()
    try:
        clean_limit = max(1, min(200, int(limit or 80)))
    except (TypeError, ValueError):
        clean_limit = 80

    discovery = list_apps(query=clean_name, limit=10)
    discovered_names = _discovered_app_names(discovery)
    discovered_name = _best_discovered_app_name(clean_name, discovered_names)
    app_found = bool(discovered_names)
    target_app = discovered_name or clean_name
    before_status = app_status(target_app)
    before_running = _tool_running(before_status)

    open_result: dict[str, Any] | None = None
    if clean_open and before_running is not True:
        open_result = app_open(target_app)
        target_app = _resolved_tool_app_name(open_result, target_app)

    after_status = app_status(target_app)
    after_running = _tool_running(after_status)
    running = after_running if after_running is not None else before_running

    focus_result: dict[str, Any] | None = None
    if clean_focus and (running is not False or (open_result or {}).get("ok") is True):
        focus_result = app_focus(target_app)
        target_app = _resolved_tool_app_name(focus_result, target_app)

    active_window_result = active_window() if focus_result is not None else None
    windows_result = windows(target_app)
    ui_result = ui_elements(app_name=target_app, role_filter=clean_filter, limit=clean_limit)

    focus_data = _tool_data(focus_result)
    windows_data = _tool_data(windows_result)
    ui_data = _tool_data(ui_result)
    focus_verified = focus_data.get("focus_verified") is True
    window_count = int(windows_data.get("count") or 0)
    ui_count = int(ui_data.get("count") or 0)
    control_like_count = int(ui_data.get("control_like_count") or 0)
    visibility_limited = (
        windows_data.get("visibility_limited") is True
        or ui_data.get("visibility_limited") is True
    )
    inspection_level = str(ui_data.get("inspection_level") or "").strip() or (
        "control" if control_like_count > 0 else "empty" if ui_count == 0 else "structural"
    )
    ready_for_foreground_action = bool(focus_verified and control_like_count > 0)
    recommended_tools = _inspect_app_recommended_tools(
        app_found=app_found,
        running=running is True,
        focus_requested=clean_focus,
        focus_verified=focus_verified,
        window_count=window_count,
        ui_count=ui_count,
        control_like_count=control_like_count,
        visibility_limited=visibility_limited,
    )
    recovery_actions = _inspect_app_recovery_actions(
        target_app,
        focus_result=focus_result,
        ui_result=ui_result,
        app_found=app_found,
        running=running is True,
        focus_verified=focus_verified,
        visibility_limited=visibility_limited,
    )
    checks = {
        "discovered_app": discovery.get("ok") is True and app_found,
        "open_ok": open_result is None or open_result.get("ok") is True,
        "status_running": running is True,
        "focus_verified": focus_verified,
        "windows_query_ok": windows_result.get("ok") is True,
        "ui_query_ok": ui_result.get("ok") is True,
        "named_ui_elements_nonempty": ui_count > 0,
        "control_like_ui_visible": control_like_count > 0,
        "ready_for_foreground_action": ready_for_foreground_action,
    }
    data = {
        "app_name": target_app,
        "requested_app_name": clean_name,
        "discovered_app_name": discovered_name,
        "app_found": app_found,
        "open_if_needed": clean_open,
        "focus_requested": clean_focus,
        "running": running is True,
        "focus_verified": focus_verified,
        "window_count": window_count,
        "ui_element_count": ui_count,
        "inspection_level": inspection_level,
        "visibility_limited": visibility_limited,
        "visibility_status": str(
            ui_data.get("visibility_status")
            or windows_data.get("window_visibility_status")
            or ""
        ),
        "control_like_count": control_like_count,
        "ready_for_foreground_action": ready_for_foreground_action,
        "recommended_tools": recommended_tools,
        "recovery_actions": recovery_actions,
        "checks": checks,
        "discovery": discovery,
        "before_status": before_status,
        "open_result": open_result,
        "after_status": after_status,
        "focus_result": focus_result,
        "active_window": active_window_result,
        "windows": windows_result,
        "ui_elements": ui_result,
    }
    if not app_found:
        return {
            "ok": False,
            "action": "desktop.inspect_app",
            "summary": f"No installed app matched {clean_name}",
            "error": "app_not_found",
            "data": data,
            "permission_error": False,
            "fallback_used": False,
            "recommended_tools": recommended_tools,
            "recovery_actions": recovery_actions,
        }
    summary = _inspect_app_summary(
        target_app,
        ready_for_foreground_action=ready_for_foreground_action,
        focus_verified=focus_verified,
        inspection_level=inspection_level,
        window_count=window_count,
        ui_count=ui_count,
        visibility_limited=visibility_limited,
    )
    return {
        "ok": True,
        "action": "desktop.inspect_app",
        "summary": summary,
        "data": data,
        "permission_error": False,
        "fallback_used": bool(open_result and open_result.get("fallback_used")),
        "recommended_tools": recommended_tools,
        "recovery_actions": recovery_actions,
    }


def system_settings_open(target: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("system.settings_open")
    clean_target = _clean_required(target, "target")
    settings_target = _system_settings_target(clean_target)
    if settings_target is not None:
        return _open_system_settings_target(
            clean_target,
            settings_target,
            action="system.settings_open",
        )
    if _looks_like_system_settings_home(clean_target):
        return _open_system_settings_home(clean_target)
    return {
        "ok": False,
        "action": "system.settings_open",
        "summary": "system.settings_open failed",
        "error": f"Unknown System Settings target: {clean_target}",
        "error_code": "unknown_system_settings_target",
        "data": {"target": clean_target, "open_target": "system_settings"},
        "permission_error": False,
        "fallback_used": False,
    }


def reveal_path(path: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.reveal_path")
    clean_path = _clean_required(path, "path")
    special = _special_desktop_object_path(clean_path, "desktop.reveal_path", "finder_reveal")
    if special and "error_payload" in special:
        return special["error_payload"]
    target = special["target"] if special else _expanded_local_path(clean_path)
    data_base = special["data"] if special else {"path": clean_path, "expanded_path": str(target)}
    if not target.exists():
        return {
            "ok": False,
            "action": "desktop.reveal_path",
            "summary": "desktop.reveal_path failed",
            "error": f"Path not found: {clean_path}",
            "error_code": "path_not_found",
            "data": {
                **data_base,
                "open_target": "finder_reveal",
                "exists": False,
            },
            "permission_error": False,
            "fallback_used": False,
        }
    try:
        result = subprocess.run(
            ["open", "-R", str(target)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return _error("desktop.reveal_path", exc)
    if result.returncode != 0:
        payload = _failed("desktop.reveal_path", result)
        payload["data"] = {
            **data_base,
            "open_target": "finder_reveal",
            "exists": True,
            "is_dir": target.is_dir(),
        }
        return payload
    return {
        "ok": True,
        "action": "desktop.reveal_path",
        "summary": f"Revealed {target.name or str(target)} in Finder",
        "data": {
            **data_base,
            "open_target": "finder_reveal",
            "exists": True,
            "is_dir": target.is_dir(),
        },
        "permission_error": False,
        "fallback_used": False,
    }


def open_path(path: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.open_path")
    clean_path = _clean_required(path, "path")
    special = _special_desktop_object_path(clean_path, "desktop.open_path", "system_open")
    if special and "error_payload" in special:
        return special["error_payload"]
    target = special["target"] if special else _expanded_local_path(clean_path)
    data_base = special["data"] if special else {"path": clean_path, "expanded_path": str(target)}
    if not target.exists():
        return {
            "ok": False,
            "action": "desktop.open_path",
            "summary": "desktop.open_path failed",
            "error": f"Path not found: {clean_path}",
            "error_code": "path_not_found",
            "data": {
                **data_base,
                "open_target": "system_open",
                "exists": False,
            },
            "permission_error": False,
            "fallback_used": False,
        }
    safety_error = _unsafe_open_path_reason(target)
    if safety_error:
        return {
            "ok": False,
            "action": "desktop.open_path",
            "summary": "desktop.open_path blocked",
            "error": safety_error,
            "error_code": "unsafe_path_type",
            "data": {
                **data_base,
                "open_target": "system_open",
                "exists": True,
                "is_dir": target.is_dir(),
                "suffix": target.suffix.lower(),
            },
            "permission_error": False,
            "fallback_used": False,
        }
    try:
        result = subprocess.run(
            ["open", str(target)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return _error("desktop.open_path", exc)
    if result.returncode != 0:
        payload = _failed("desktop.open_path", result)
        payload["data"] = {
            **data_base,
            "open_target": "system_open",
            "exists": True,
            "is_dir": target.is_dir(),
            "suffix": target.suffix.lower(),
        }
        return payload
    return {
        "ok": True,
        "action": "desktop.open_path",
        "summary": f"Opened {target.name or str(target)}",
        "data": {
            **data_base,
            "open_target": "system_open",
            "exists": True,
            "is_dir": target.is_dir(),
            "suffix": target.suffix.lower(),
        },
        "permission_error": False,
        "fallback_used": False,
    }


def open_path_with_app(path: str, app_name: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.open_path_with_app")
    clean_path = _clean_required(path, "path")
    clean_app_name = _clean_required(app_name, "app_name")
    special = _special_desktop_object_path(
        clean_path,
        "desktop.open_path_with_app",
        "app_open",
    )
    if special and "error_payload" in special:
        return special["error_payload"]
    target = special["target"] if special else _expanded_local_path(clean_path)
    data_base = special["data"] if special else {"path": clean_path, "expanded_path": str(target)}
    if not target.exists():
        return {
            "ok": False,
            "action": "desktop.open_path_with_app",
            "summary": "desktop.open_path_with_app failed",
            "error": f"Path not found: {clean_path}",
            "error_code": "path_not_found",
            "data": {
                **data_base,
                "app_name": clean_app_name,
                "open_target": "app_open",
                "exists": False,
            },
            "permission_error": False,
            "fallback_used": False,
        }
    safety_error = _unsafe_open_path_reason(target)
    if safety_error:
        return {
            "ok": False,
            "action": "desktop.open_path_with_app",
            "summary": "desktop.open_path_with_app blocked",
            "error": safety_error,
            "error_code": "unsafe_path_type",
            "data": {
                **data_base,
                "app_name": clean_app_name,
                "open_target": "app_open",
                "exists": True,
                "is_dir": target.is_dir(),
                "suffix": target.suffix.lower(),
            },
            "permission_error": False,
            "fallback_used": False,
        }
    try:
        result = subprocess.run(
            ["open", "-a", clean_app_name, str(target)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return _error("desktop.open_path_with_app", exc)
    if result.returncode != 0:
        payload = _failed("desktop.open_path_with_app", result)
        payload["data"] = {
            **data_base,
            "app_name": clean_app_name,
            "open_target": "app_open",
            "exists": True,
            "is_dir": target.is_dir(),
            "suffix": target.suffix.lower(),
        }
        return payload
    return {
        "ok": True,
        "action": "desktop.open_path_with_app",
        "summary": f"Opened {target.name or str(target)} with {clean_app_name}",
        # A zero LaunchServices exit status is the native receipt that macOS
        # accepted this exact app/path dispatch.  Failed, missing, and unsafe
        # paths return above and never receive this marker.
        "postcondition_verified": True,
        "data": {
            **data_base,
            "app_name": clean_app_name,
            "open_target": "app_open",
            "exists": True,
            "postcondition_verified": True,
            "is_dir": target.is_dir(),
            "suffix": target.suffix.lower(),
        },
        "permission_error": False,
        "fallback_used": False,
    }


def _unsafe_open_path_reason(target: Path) -> str:
    suffix = target.suffix.lower()
    if target.is_dir():
        if suffix in _UNSAFE_OPEN_PATH_SUFFIXES:
            return f"Refusing to open unsafe path type: {suffix}"
        return ""
    if suffix in _UNSAFE_OPEN_PATH_SUFFIXES:
        return f"Refusing to open unsafe path type: {suffix}"
    if suffix not in _SAFE_OPEN_PATH_SUFFIXES:
        return f"Refusing to open unknown file type: {suffix or 'no extension'}"
    return ""


def _open_common_folder(label: str, folder_path: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["open", str(folder_path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return _error("app.open", exc)
    if result.returncode != 0:
        payload = _failed("app.open", result)
        payload["data"] = {
            "app_name": label,
            "path": str(folder_path),
            "open_target": "folder",
        }
        return payload
    return {
        "ok": True,
        "action": "app.open",
        "summary": f"Opened {folder_path.name or 'Home'}",
        "data": {
            "app_name": label,
            "path": str(folder_path),
            "open_target": "folder",
        },
        "permission_error": False,
        "fallback_used": False,
    }


def _open_system_settings_target(
    label: str,
    target: tuple[str, tuple[str, ...]],
    *,
    action: str = "app.open",
) -> dict[str, Any]:
    settings_label, urls = target
    errors: list[str] = []
    for index, url in enumerate(urls):
        try:
            result = subprocess.run(
                ["open", url],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception as exc:
            return _error(action, exc)
        if result.returncode == 0:
            return {
                "ok": True,
                "action": action,
                "summary": f"Opened System Settings: {settings_label}",
                "data": _system_settings_open_data(
                    action,
                    label,
                    settings_label=settings_label,
                    settings_url=url,
                    fallback_used=index > 0,
                ),
                "permission_error": False,
                "fallback_used": index > 0,
            }
        error = "\n".join(
            part.strip()
            for part in (result.stderr, result.stdout)
            if isinstance(part, str) and part.strip()
        )
        errors.append(error or f"{url}: exit code {result.returncode}")
    payload = _failed(action, result)
    payload["data"] = _system_settings_open_data(
        action,
        label,
        settings_label=settings_label,
        attempted_urls=list(urls),
        settings_errors=errors,
    )
    return payload


def _open_system_settings_home(label: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["open", "-a", "System Settings"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return _error("system.settings_open", exc)
    if result.returncode != 0:
        payload = _failed("system.settings_open", result)
        payload["data"] = {
            "target": label,
            "open_target": "system_settings",
            "settings_label": "System Settings",
        }
        return payload
    return {
        "ok": True,
        "action": "system.settings_open",
        "summary": "Opened System Settings",
        "data": {
            "target": label,
            "open_target": "system_settings",
            "settings_label": "System Settings",
        },
        "permission_error": False,
        "fallback_used": False,
    }


def _system_settings_open_data(
    action: str,
    label: str,
    *,
    settings_label: str,
    settings_url: str | None = None,
    attempted_urls: list[str] | None = None,
    settings_errors: list[str] | None = None,
    fallback_used: bool | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "open_target": "system_settings",
        "settings_label": settings_label,
    }
    if action == "app.open":
        data["app_name"] = label
    else:
        data["target"] = label
    if settings_url is not None:
        data["settings_url"] = settings_url
    if attempted_urls is not None:
        data["attempted_urls"] = attempted_urls
    if settings_errors is not None:
        data["settings_errors"] = settings_errors
    if fallback_used is not None:
        data["fallback_used"] = fallback_used
    return data


def _system_settings_target(value: str) -> tuple[str, tuple[str, ...]] | None:
    variants = _system_settings_alias_variants(value)
    for variant in variants:
        target = _SYSTEM_SETTINGS_TARGETS.get(variant)
        if target is not None:
            return target
    return None


def _looks_like_system_settings_home(value: str) -> bool:
    variants = _system_settings_alias_variants(value)
    return any(
        variant
        in {
            "systemsettings",
            "systempreferences",
            "settings",
            "preferences",
            "系统设置",
            "系统偏好",
            "系统偏好设置",
            "设置",
            "偏好设置",
        }
        for variant in variants
    )


def _system_settings_alias_variants(value: str) -> list[str]:
    normalized = str(value or "").strip().lower()
    normalized = normalized.replace("&", "and")
    normalized = normalized.replace("-", " ").replace("_", " ")
    normalized = normalized.replace("设置的", " ")
    variants = [_compact_alias(normalized)]
    simplified = _strip_system_settings_noise(normalized)
    simplified_compact = _compact_alias(simplified)
    if simplified_compact and simplified_compact not in variants:
        variants.append(simplified_compact)
    return [variant for variant in variants if variant]


def _strip_system_settings_noise(value: str) -> str:
    return (
        str(value or "")
        .replace("系统设置", " ")
        .replace("设置", " ")
        .replace("里的", " ")
        .replace("中的", " ")
        .replace("里面的", " ")
        .replace("内的", " ")
        .replace("权限", " ")
        .replace("页面", " ")
        .replace("面板", " ")
        .replace("settings", " ")
        .replace("setting", " ")
        .replace("preferences", " ")
        .replace("preference", " ")
        .replace("permissions", " ")
        .replace("permission", " ")
        .replace("pane", " ")
        .replace("page", " ")
    )


def _compact_alias(value: str) -> str:
    return "".join(str(value or "").strip().split())


def _expanded_local_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def _special_desktop_object_path(
    value: str,
    action: str,
    open_target: str,
) -> dict[str, Any]:
    compact = re.sub(r"[\s._-]+", "", str(value or "").strip().lower())
    if compact in {
        "finderselection",
        "finderselecteditem",
        "selectedfinderitem",
        "selectedfile",
        "selecteditem",
    }:
        return _finder_selection_desktop_object_path(value, action, open_target)
    if compact in {"latestscreenshot", "recentscreenshot", "lastscreenshot"}:
        return _latest_screenshot_desktop_object_path(value, action, open_target)
    if compact in {
        "latestdesktopitem",
        "recentdesktopitem",
        "latestdesktopfile",
        "recentdesktopfile",
    }:
        return _latest_desktop_item_object_path(value, action, open_target)
    if compact not in {"latestdownload", "recentdownload"}:
        return {}
    downloads = Path.home() / "Downloads"
    base_data = {
        "path": str(value or "").strip(),
        "open_target": open_target,
        "desktop_object": "latest_download",
        "source_folder": str(downloads),
    }
    if not downloads.exists() or not downloads.is_dir():
        return {
            "error_payload": {
                "ok": False,
                "action": action,
                "summary": f"{action} failed",
                "error": "Downloads folder not found",
                "error_code": "downloads_folder_not_found",
                "data": {
                    **base_data,
                    "expanded_path": str(downloads),
                    "exists": False,
                    "source_exists": False,
                },
                "permission_error": False,
                "fallback_used": False,
            }
        }
    target = _latest_download_item(downloads)
    if target is None:
        return {
            "error_payload": {
                "ok": False,
                "action": action,
                "summary": f"{action} failed",
                "error": "No completed downloads found",
                "error_code": "latest_download_not_found",
                "data": {
                    **base_data,
                    "expanded_path": str(downloads),
                    "exists": False,
                    "source_exists": True,
                },
                "permission_error": False,
                "fallback_used": False,
            }
        }
    return {
        "target": target,
        "data": {
            **base_data,
            "expanded_path": str(target),
            "resolved_path": str(target),
            "display_path": str(target),
            "source_exists": True,
        },
    }


def _latest_download_item(downloads: Path) -> Path | None:
    candidates: list[tuple[float, Path]] = []
    try:
        items = list(downloads.iterdir())
    except OSError:
        return None
    for item in items:
        name = item.name
        if name.startswith(".") or name.lower().endswith((".download", ".crdownload", ".part")):
            continue
        try:
            candidates.append((item.stat().st_mtime, item))
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _latest_screenshot_desktop_object_path(
    value: str,
    action: str,
    open_target: str,
) -> dict[str, Any]:
    home = Path.home()
    return _latest_desktop_object_from_folders(
        value,
        action,
        open_target,
        desktop_object="latest_screenshot",
        folders=[home / "Desktop", home / "Downloads", home / "Pictures"],
        matcher=_looks_like_screenshot_item,
        missing_error="Screenshot folders not found",
        missing_error_code="screenshot_folders_not_found",
        not_found_error="No recent screenshots found",
        not_found_error_code="latest_screenshot_not_found",
    )


def _latest_desktop_item_object_path(
    value: str,
    action: str,
    open_target: str,
) -> dict[str, Any]:
    desktop = Path.home() / "Desktop"
    return _latest_desktop_object_from_folders(
        value,
        action,
        open_target,
        desktop_object="latest_desktop_item",
        folders=[desktop],
        matcher=lambda _item: True,
        missing_error="Desktop folder not found",
        missing_error_code="desktop_folder_not_found",
        not_found_error="No desktop items found",
        not_found_error_code="latest_desktop_item_not_found",
    )


def _latest_desktop_object_from_folders(
    value: str,
    action: str,
    open_target: str,
    *,
    desktop_object: str,
    folders: list[Path],
    matcher: Any,
    missing_error: str,
    missing_error_code: str,
    not_found_error: str,
    not_found_error_code: str,
) -> dict[str, Any]:
    source_folders = [str(folder) for folder in folders]
    base_data = {
        "path": str(value or "").strip(),
        "open_target": open_target,
        "desktop_object": desktop_object,
        "source_folders": source_folders,
    }
    if len(folders) == 1:
        base_data["source_folder"] = source_folders[0]
    existing_folders = [folder for folder in folders if folder.exists() and folder.is_dir()]
    if not existing_folders:
        return {
            "error_payload": {
                "ok": False,
                "action": action,
                "summary": f"{action} failed",
                "error": missing_error,
                "error_code": missing_error_code,
                "data": {
                    **base_data,
                    "exists": False,
                    "source_exists": False,
                },
                "permission_error": False,
                "fallback_used": False,
            }
        }
    target = _latest_item_in_folders(existing_folders, matcher)
    if target is None:
        return {
            "error_payload": {
                "ok": False,
                "action": action,
                "summary": f"{action} failed",
                "error": not_found_error,
                "error_code": not_found_error_code,
                "data": {
                    **base_data,
                    "exists": False,
                    "source_exists": True,
                },
                "permission_error": False,
                "fallback_used": False,
            }
        }
    return {
        "target": target,
        "data": {
            **base_data,
            "source_folder": str(target.parent),
            "expanded_path": str(target),
            "resolved_path": str(target),
            "display_path": str(target),
            "source_exists": True,
        },
    }


def _latest_item_in_folders(folders: list[Path], matcher: Any) -> Path | None:
    candidates: list[tuple[float, Path]] = []
    for folder in folders:
        try:
            items = list(folder.iterdir())
        except OSError:
            continue
        for item in items:
            name = item.name
            if name.startswith(".") or _is_incomplete_desktop_item(name):
                continue
            if not matcher(item):
                continue
            try:
                candidates.append((item.stat().st_mtime, item))
            except OSError:
                continue
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _is_incomplete_desktop_item(name: str) -> bool:
    return str(name or "").lower().endswith((".download", ".crdownload", ".part"))


def _looks_like_screenshot_item(item: Path) -> bool:
    name = item.name
    lowered = name.lower()
    if item.suffix.lower() not in {".png", ".jpg", ".jpeg", ".heic", ".webp"}:
        return False
    compact = re.sub(r"[\s._-]+", "", lowered)
    return any(
        marker in compact
        for marker in ("screenshot", "截屏", "截圖", "屏幕截图", "螢幕截圖")
    )


def _finder_selection_desktop_object_path(
    value: str,
    action: str,
    open_target: str,
) -> dict[str, Any]:
    base_data = {
        "path": str(value or "").strip(),
        "open_target": open_target,
        "desktop_object": "finder_selection",
        "source_app": "Finder",
    }
    selected = _selected_finder_item_path()
    if not selected.get("ok"):
        payload = {
            **selected,
            "action": action,
            "summary": f"{action} failed",
            "data": {
                **base_data,
                "exists": False,
                "source_exists": False,
            },
        }
        return {"error_payload": _with_permission_metadata("osascript", payload)}
    selected_path = str(selected.get("path") or "").strip()
    if not selected_path:
        return {
            "error_payload": {
                "ok": False,
                "action": action,
                "summary": f"{action} failed",
                "error": "No Finder selection found",
                "error_code": "finder_selection_not_found",
                "data": {
                    **base_data,
                    "exists": False,
                    "source_exists": False,
                },
                "permission_error": False,
                "fallback_used": False,
            }
        }
    target = Path(selected_path).expanduser().resolve(strict=False)
    return {
        "target": target,
        "data": {
            **base_data,
            "expanded_path": str(target),
            "resolved_path": str(target),
            "display_path": str(target),
            "source_exists": True,
        },
    }


def _selected_finder_item_path() -> dict[str, Any]:
    result = _run_osascript(
        """
        tell application "Finder"
            if (count of selection) is 0 then
                return ""
            end if
            set selectedItem to item 1 of selection
            return POSIX path of (selectedItem as alias)
        end tell
        """
    )
    if not result.get("ok"):
        return result
    return {"ok": True, "path": str(result.get("stdout") or "").strip()}


def _common_folder_path(value: str) -> Path | None:
    compact = "".join(str(value or "").strip().lower().replace("-", " ").replace("_", " ").split())
    if compact not in _COMMON_FOLDER_TARGETS:
        return None
    folder_name = _COMMON_FOLDER_TARGETS[compact]
    return Path.home() / folder_name if folder_name else Path.home()


def _run_open_app(app_name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["open", "-a", app_name],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _resolve_installed_app_name(app_name: str) -> str:
    match = _resolve_installed_app(app_name)
    return str(match.get("name") or "").strip()


def _resolve_installed_app(app_name: str) -> dict[str, Any]:
    candidates = _installed_app_match_candidates(app_name)
    return candidates[0] if candidates else {}


def _installed_app_match_candidates(query_name: str) -> list[dict[str, Any]]:
    query = str(query_name or "").strip()
    query_key = _compact_app_match_name(query)
    if not query_key:
        return []
    matches = _installed_app_match_candidates_for_query(query)
    best_primary_score = max(
        (int(item.get("match_score") or 0) for item in matches),
        default=0,
    )
    alias_query = str(APP_ALIASES.get(compact_app_alias(query)) or "").strip()
    if (
        not alias_query
        or _compact_app_match_name(alias_query) == query_key
        or best_primary_score >= _APP_ALIAS_EXPANSION_PRIMARY_SCORE
    ):
        return matches

    alias_matches = _installed_app_match_candidates_for_query(alias_query)
    for match in alias_matches:
        match["matched_query"] = alias_query
        match["matched_query_source"] = "app_alias"
    return _merge_installed_app_match_candidates(matches, alias_matches)


def _installed_app_match_candidates_for_query(query: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for bundle in _iter_installed_app_bundles():
        candidate = bundle.stem
        metadata = _app_bundle_metadata(bundle)
        capability_match = _installed_app_capability_match(query, metadata)
        name_match = _installed_app_name_match(query, candidate, metadata)
        name_score = int(name_match.get("score") or 0)
        capability_score = int(capability_match.get("score") or 0)
        score = max(name_score, capability_score)
        if score <= 0:
            continue
        match_reason = _installed_app_match_reason(score)
        matched_capability = ""
        if capability_match and capability_score >= score:
            match_reason = str(capability_match.get("reason") or match_reason)
            matched_capability = str(capability_match.get("capability") or "")
        candidate_payload = {
            "name": candidate,
            "path": str(bundle),
            "match_score": score,
            "match_confidence": _installed_app_match_confidence(score),
            "match_reason": match_reason,
            "normalized_name": _compact_app_match_name(candidate),
        }
        matched_name = str(name_match.get("matched_name") or "").strip()
        matched_name_source = str(name_match.get("matched_name_source") or "").strip()
        if matched_name:
            candidate_payload["matched_name"] = matched_name
        if matched_name_source:
            candidate_payload["matched_name_source"] = matched_name_source
        if matched_capability:
            candidate_payload["matched_capability"] = matched_capability
        metadata_preview = _app_bundle_metadata_preview(metadata)
        if metadata_preview:
            candidate_payload["metadata"] = metadata_preview
        matches.append(candidate_payload)
    if not matches:
        return []
    matches.sort(
        key=lambda item: (
            -int(item["match_score"]),
            len(str(item["name"])),
            str(item["name"]),
        )
    )
    return matches


def _merge_installed_app_match_candidates(
    primary: list[dict[str, Any]],
    aliases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for match in (*primary, *aliases):
        key = str(match.get("path") or match.get("name") or "").strip()
        current = merged.get(key)
        if current is None or int(match.get("match_score") or 0) > int(
            current.get("match_score") or 0
        ):
            merged[key] = match
    matches = list(merged.values())
    matches.sort(
        key=lambda item: (
            -int(item["match_score"]),
            len(str(item["name"])),
            str(item["name"]),
        )
    )
    return matches


def _app_discovery_resolution(
    requested_app_name: str,
    best_match: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not best_match:
        return {}
    resolved = str(best_match.get("name") or "").strip()
    if not resolved:
        return {}
    return _app_resolution_metadata_from_match(requested_app_name, best_match)


def _app_resolution_metadata_from_match(
    requested_app_name: str,
    match: Mapping[str, Any],
) -> dict[str, Any]:
    requested = str(requested_app_name or "").strip()
    resolved = str(match.get("name") or "").strip()
    if not requested or not resolved:
        return {}
    metadata: dict[str, Any] = {
        "requested_app_name": requested,
        "resolved_app_name": resolved,
        "app_resolution": "installed_app_bundle",
        "app_resolution_source": "desktop.list_apps",
    }
    score = match.get("match_score")
    if score is not None:
        try:
            metadata["app_resolution_score"] = int(score)
        except (TypeError, ValueError):
            pass
    confidence = str(match.get("match_confidence") or "").strip()
    if confidence:
        metadata["app_resolution_confidence"] = confidence
    reason = str(match.get("match_reason") or "").strip()
    if reason:
        metadata["app_resolution_reason"] = reason
    matched_name = str(match.get("matched_name") or "").strip()
    if matched_name:
        metadata["app_resolution_matched_name"] = matched_name
    matched_name_source = str(match.get("matched_name_source") or "").strip()
    if matched_name_source:
        metadata["app_resolution_matched_name_source"] = matched_name_source
    matched_capability = str(match.get("matched_capability") or "").strip()
    if matched_capability:
        metadata["app_resolution_matched_capability"] = matched_capability
    path = str(match.get("path") or "").strip()
    if path:
        metadata["resolved_app_path"] = path
    return metadata


def _app_resolution_metadata(requested_app_name: str, resolved_app_name: str) -> dict[str, str]:
    requested = str(requested_app_name or "").strip()
    resolved = str(resolved_app_name or "").strip()
    if not requested or not resolved or requested == resolved:
        return {}
    return {
        "requested_app_name": requested,
        "resolved_app_name": resolved,
        "app_resolution": "installed_app_bundle",
    }


def _installed_app_match_confidence(score: int) -> str:
    if score >= 90:
        return "high"
    if score >= 80:
        return "medium"
    return "low"


def _installed_app_match_reason(score: int) -> str:
    if score >= 100:
        return "exact_name"
    if score >= 90:
        return "query_tokens_in_app_name"
    if score >= 85:
        return "app_name_tokens_in_query"
    if score >= 82:
        return "query_token_prefix"
    if score >= 80:
        return "app_name_suffix"
    if score >= 75:
        return "query_suffix"
    if score >= 70:
        return "non_ascii_query_substring"
    if score >= 65:
        return "non_ascii_app_name_substring"
    return "unknown"


def _application_search_dirs() -> list[Path]:
    return [
        Path("/Applications"),
        Path("/Applications/Utilities"),
        Path("/System/Applications"),
        Path("/System/Applications/Utilities"),
        Path("/System/Library/CoreServices"),
        Path.home() / "Applications",
    ]


def _iter_installed_app_bundles() -> list[Path]:
    bundles: list[Path] = []
    seen: set[str] = set()
    for folder in _application_search_dirs():
        try:
            if folder == Path("/System/Library/CoreServices"):
                candidates = [folder / "Finder.app"]
            else:
                candidates = list(folder.glob("*.app"))
            if folder.name == "Applications":
                candidates.extend(folder.glob("*/*.app"))
        except OSError:
            continue
        for candidate in candidates:
            key = str(candidate)
            if key in seen or not candidate.is_dir():
                continue
            seen.add(key)
            bundles.append(candidate)
    return bundles


def _app_bundle_metadata(bundle: Path) -> dict[str, Any]:
    info_path = bundle / "Contents" / "Info.plist"
    raw: Mapping[str, Any] = {}
    if info_path.exists():
        try:
            with info_path.open("rb") as handle:
                loaded = plistlib.load(handle)
            if isinstance(loaded, Mapping):
                raw = loaded
        except (OSError, ValueError, plistlib.InvalidFileException):
            raw = {}
    raw_names = [
        bundle.stem,
        raw.get("CFBundleName"),
        raw.get("CFBundleDisplayName"),
        raw.get("CFBundleExecutable"),
    ]
    names = _app_metadata_text_values(raw_names)
    display_names = _app_metadata_display_values(raw_names)
    schemes: set[str] = set()
    for url_type in raw.get("CFBundleURLTypes") or []:
        if isinstance(url_type, Mapping):
            schemes.update(_app_metadata_text_values(url_type.get("CFBundleURLSchemes") or []))
    documents: set[str] = set()
    for document_type in raw.get("CFBundleDocumentTypes") or []:
        if not isinstance(document_type, Mapping):
            continue
        documents.update(_app_metadata_text_values(document_type.get("CFBundleTypeExtensions") or []))
        documents.update(_app_metadata_text_values(document_type.get("LSItemContentTypes") or []))
        documents.update(_app_metadata_text_values([document_type.get("CFBundleTypeName")]))
    return {
        "bundle_id": str(raw.get("CFBundleIdentifier") or "").strip(),
        "category": str(raw.get("LSApplicationCategoryType") or "").strip(),
        "names": names,
        "display_names": display_names,
        "schemes": schemes,
        "documents": documents,
    }


def _app_metadata_text_values(values: Any) -> set[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return set()
    result: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text:
            result.add(text.casefold())
    return result


def _app_metadata_display_values(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _app_bundle_metadata_preview(metadata: Mapping[str, Any]) -> dict[str, Any]:
    preview: dict[str, Any] = {}
    bundle_id = str(metadata.get("bundle_id") or "").strip()
    category = str(metadata.get("category") or "").strip()
    if bundle_id:
        preview["bundle_id"] = bundle_id
    if category:
        preview["category"] = category
    schemes = sorted(str(item) for item in metadata.get("schemes") or [] if str(item))
    if schemes:
        preview["url_schemes"] = schemes[:8]
    documents = sorted(str(item) for item in metadata.get("documents") or [] if str(item))
    if documents:
        preview["document_types"] = documents[:12]
    return preview


def _installed_app_match_score(
    query_name: str,
    candidate_name: str,
    metadata: Mapping[str, Any] | None = None,
) -> int:
    name_match = _installed_app_name_match(query_name, candidate_name, metadata or {})
    name_score = int(name_match.get("score") or 0)
    capability_match = _installed_app_capability_match(query_name, metadata or {})
    capability_score = int(capability_match.get("score") or 0)
    return max(name_score, capability_score)


def _installed_app_name_match(
    query_name: str,
    candidate_name: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    best: dict[str, Any] = {}
    for candidate in _installed_app_match_name_candidates(candidate_name, metadata):
        score = _installed_app_name_match_score(query_name, candidate["name"])
        if score <= 0 or score <= int(best.get("score") or 0):
            continue
        best = {
            "score": score,
            "matched_name": candidate["name"],
            "matched_name_source": candidate["source"],
        }
    return best


def _installed_app_match_name_candidates(
    candidate_name: str,
    metadata: Mapping[str, Any],
) -> list[dict[str, str]]:
    names = [
        {"name": str(candidate_name or "").strip(), "source": "bundle_name"},
    ]
    for value in metadata.get("display_names") or []:
        text = str(value or "").strip()
        if text and all(item["name"] != text for item in names):
            names.append({"name": text, "source": "bundle_metadata"})
    for value in metadata.get("names") or []:
        text = str(value or "").strip()
        if text and all(item["name"] != text for item in names):
            names.append({"name": text, "source": "bundle_metadata_normalized"})
    return names


def _installed_app_name_match_score(query_name: str, candidate_name: str) -> int:
    query_compact = _compact_app_match_name(query_name)
    candidate_compact = _compact_app_match_name(candidate_name)
    if not query_compact or not candidate_compact:
        return 0
    if candidate_compact == query_compact:
        return 100
    query_tokens = _app_match_tokens(query_name)
    candidate_tokens = _app_match_tokens(candidate_name)
    if query_tokens and all(token in candidate_tokens for token in query_tokens):
        return 90
    if candidate_tokens and all(token in query_tokens for token in candidate_tokens):
        return 85
    if query_tokens and all(
        any(candidate_token.startswith(token) for candidate_token in candidate_tokens)
        for token in query_tokens
    ):
        return 82
    if candidate_compact.endswith(query_compact):
        return 80
    if query_compact.endswith(candidate_compact):
        return 75
    if _contains_non_ascii(query_name) and query_compact in candidate_compact:
        return 70
    if _contains_non_ascii(candidate_name) and candidate_compact in query_compact:
        return 65
    return 0


def _installed_app_capability_match(
    query_name: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    profiles = _app_capability_profiles_for_query(query_name)
    if not profiles:
        return {}
    schemes = {str(item or "").casefold() for item in metadata.get("schemes") or []}
    documents = {str(item or "").casefold() for item in metadata.get("documents") or []}
    category = str(metadata.get("category") or "").casefold()
    names = {str(item or "").casefold() for item in metadata.get("names") or []}
    bundle_id = str(metadata.get("bundle_id") or "").casefold()
    best: dict[str, Any] = {}
    for profile in profiles:
        profile_id = str(profile.get("id") or "").strip()
        scheme_matches = _metadata_term_matches(schemes, profile.get("schemes") or ())
        document_matches = _metadata_term_matches(documents, profile.get("documents") or ())
        category_matches = _metadata_term_matches({category}, profile.get("categories") or ())
        name_matches = _metadata_term_matches(names | {bundle_id}, profile.get("aliases") or ())
        if profile.get("require_scheme_and_document") and not (scheme_matches and document_matches):
            continue
        if not any((scheme_matches, document_matches, category_matches, name_matches)):
            continue
        score = int(profile.get("score") or 80)
        if scheme_matches and document_matches:
            score += 3
        elif category_matches:
            score += 1
        if not best or score > int(best.get("score") or 0):
            best = {
                "score": min(score, 99),
                "capability": profile_id,
                "reason": f"capability_{profile_id}",
            }
    return best


def _app_capability_profiles_for_query(query_name: str) -> list[Mapping[str, Any]]:
    query = str(query_name or "").strip()
    query_compact = _compact_app_match_name(query)
    if not query_compact:
        return []
    matches: list[Mapping[str, Any]] = []
    for profile in _APP_CAPABILITY_QUERY_PROFILES:
        aliases = tuple(str(alias or "").strip() for alias in profile.get("aliases") or ())
        if any(_capability_alias_matches_query(query, query_compact, alias) for alias in aliases):
            matches.append(profile)
    return matches


def _capability_alias_matches_query(query: str, query_compact: str, alias: str) -> bool:
    alias_compact = _compact_app_match_name(alias)
    if not alias_compact:
        return False
    if query_compact == alias_compact:
        return True
    if _contains_non_ascii(alias) or _contains_non_ascii(query):
        return alias_compact in query_compact
    return False


def _metadata_term_matches(values: set[str], terms: Any) -> bool:
    if isinstance(terms, str):
        terms = (terms,)
    if not isinstance(terms, (tuple, list, set)):
        return False
    clean_terms = [str(term or "").casefold().strip() for term in terms if str(term or "").strip()]
    if not clean_terms:
        return False
    for value in values:
        if not value:
            continue
        if any(term == value or term in value for term in clean_terms):
            return True
    return False


def _compact_app_match_name(value: str) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").strip().casefold())


def _app_name_without_bundle_suffix(value: str) -> str:
    clean_value = str(value or "").strip()
    return clean_value[:-4] if clean_value.casefold().endswith(".app") else clean_value


def _app_name_matches_expected(expected: str, actual: str) -> bool:
    clean_expected = _app_name_without_bundle_suffix(expected)
    clean_actual = _app_name_without_bundle_suffix(actual)
    if not clean_expected or not clean_actual:
        return False
    return _installed_app_match_score(clean_expected, clean_actual) >= 80


def _app_match_tokens(value: str) -> list[str]:
    return [token for token in re.split(r"[\W_]+", str(value or "").casefold()) if token]


def _contains_non_ascii(value: str) -> bool:
    return any(ord(char) > 127 for char in str(value or ""))


def _system_events_focus_app(app_name: str) -> dict[str, Any]:
    return _run_osascript(
        """
        on run argv
            set appName to item 1 of argv
            tell application appName to activate
            try
                tell application appName to reopen
            end try
            delay 0.2
            set targetFrontmost to false
            set targetVisible to ""
            set targetWindowCount to ""
            set frontName to ""
            tell application "System Events"
                try
                    set targetProc to first application process whose name is appName
                    try
                        set visible of targetProc to true
                    end try
                    try
                        set frontmost of targetProc to true
                        delay 0.1
                    end try
                    try
                        set targetFrontmost to frontmost of targetProc
                    end try
                    try
                        set targetVisible to visible of targetProc
                    end try
                    try
                        set targetWindowCount to count of windows of targetProc
                    end try
                end try
                try
                    set frontName to name of first application process whose frontmost is true
                end try
            end tell
            return "focused|" & appName & "|" & (targetFrontmost as text) & "|" & frontName & "|" & (targetVisible as text) & "|" & (targetWindowCount as text)
        end run
        """,
        [app_name],
    )


def _launchservices_focus_app(app_name: str) -> dict[str, Any]:
    try:
        open_result = _run_open_app(app_name)
    except Exception as exc:
        return _error("open -a", exc)
    if open_result.returncode != 0:
        return _failed("open -a", open_result)
    verify_result = _system_events_focus_app(app_name)
    verify_result["launchservices_returncode"] = open_result.returncode
    return verify_result


def _dock_focus_app(app_name: str) -> dict[str, Any]:
    candidates = _app_display_name_candidates(app_name)
    if not candidates:
        candidates = [app_name]
    return _run_osascript(
        """
        on run argv
            set appName to item 1 of argv
            set candidateText to item 2 of argv
            set dockStatus to "missing"
            set dockName to ""
            set targetFrontmost to false
            set targetVisible to ""
            set targetWindowCount to ""
            set frontName to ""
            tell application "System Events"
                try
                    tell process "Dock"
                        repeat with candidateName in paragraphs of candidateText
                            set cleanCandidateName to candidateName as text
                            if cleanCandidateName is not "" then
                                try
                                    click UI element cleanCandidateName of list 1
                                    set dockName to cleanCandidateName
                                    set dockStatus to "clicked"
                                    exit repeat
                                end try
                            end if
                        end repeat
                    end tell
                    delay 0.2
                end try
                try
                    set targetProc to first application process whose name is appName
                    try
                        set targetFrontmost to frontmost of targetProc
                    end try
                    try
                        set targetVisible to visible of targetProc
                    end try
                    try
                        set targetWindowCount to count of windows of targetProc
                    end try
                end try
                try
                    set frontName to name of first application process whose frontmost is true
                end try
            end tell
            return "dock|" & appName & "|" & dockStatus & "|" & (targetFrontmost as text) & "|" & frontName & "|" & (targetVisible as text) & "|" & (targetWindowCount as text) & "|" & dockName
        end run
        """,
        [app_name, "\n".join(candidates)],
    )


def _app_display_name_candidates(app_name: str) -> list[str]:
    candidates: list[str] = []
    for candidate in (app_name, _resolve_installed_app_name(app_name)):
        clean_candidate = str(candidate or "").strip()
        if clean_candidate and clean_candidate not in candidates:
            candidates.append(clean_candidate)
    bundle_path = _installed_app_bundle_path(app_name)
    localized_name = _localized_app_display_name(bundle_path) if bundle_path is not None else ""
    if localized_name and localized_name not in candidates:
        candidates.append(localized_name)
    return candidates


def _installed_app_bundle_path(app_name: str) -> Path | None:
    matches: list[tuple[int, int, Path]] = []
    for bundle in _iter_installed_app_bundles():
        score = _installed_app_match_score(app_name, bundle.stem)
        if score <= 0:
            continue
        matches.append((score, -len(bundle.stem), bundle))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][2]


def _localized_app_display_name(bundle_path: Path) -> str:
    try:
        result = subprocess.run(
            ["mdls", "-name", "kMDItemDisplayName", "-raw", str(bundle_path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    value = str(result.stdout or "").strip()
    return "" if value in {"", "(null)", "null"} else value


def _electron_native_bridge_config() -> tuple[str, str] | None:
    raw_url = os.getenv(_ELECTRON_NATIVE_URL_ENV, "").strip().rstrip("/")
    token = os.getenv(_ELECTRON_NATIVE_TOKEN_ENV, "").strip()
    if not raw_url or not token:
        return None
    parsed = urlparse(raw_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    if not parsed.port:
        return None
    return raw_url, token


def _electron_native_focus_app(
    app_name: str,
    *,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    config = _electron_native_bridge_config()
    if config is None:
        return {
            "ok": False,
            "action": "electron.native.desktop.focus",
            "summary": "Electron native bridge is unavailable",
            "error": "electron_native_bridge_unavailable",
            "data": {"native_bridge_available": False},
            "permission_error": False,
            "fallback_used": False,
        }
    bridge_url, token = config
    request = Request(
        f"{bridge_url}/native/desktop/focus",
        data=json.dumps({"app_name": app_name}).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-oha-yachiyo-bridge-token": token,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw_body = response.read().decode("utf-8")
            payload = json.loads(raw_body) if raw_body.strip() else {}
            return _normalize_electron_native_focus_payload(payload, response.status)
    except HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_body) if raw_body.strip() else {}
        except json.JSONDecodeError:
            payload = {
                "ok": False,
                "action": "electron.native.desktop.focus",
                "summary": "Electron native bridge returned invalid JSON",
                "error": raw_body or str(exc),
                "data": {},
            }
        return _normalize_electron_native_focus_payload(payload, exc.code)
    except Exception as exc:
        return {
            "ok": False,
            "action": "electron.native.desktop.focus",
            "summary": "Electron native bridge request failed",
            "error": str(exc),
            "data": {"native_bridge_available": True},
            "permission_error": False,
            "fallback_used": False,
        }


def _normalize_electron_native_focus_payload(payload: Any, status: int | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {
            "ok": False,
            "action": "electron.native.desktop.focus",
            "summary": "Electron native bridge returned an invalid payload",
            "error": "invalid_native_bridge_payload",
            "data": {},
        }
    result = dict(payload)
    result.setdefault("ok", False)
    result.setdefault("action", "electron.native.desktop.focus")
    result.setdefault("summary", "Electron native bridge focus result")
    result.setdefault("data", {})
    result.setdefault("permission_error", False)
    result.setdefault("fallback_used", False)
    result["native_bridge_available"] = True
    if status is not None:
        result["http_status"] = int(status)
    data = result.get("data")
    if isinstance(data, dict):
        data.setdefault("native_bridge_available", True)
    return result


def app_focus(app_name: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("app.focus")
    clean_name = _clean_required(app_name, "app_name")
    resolved_app = _resolve_installed_app(clean_name)
    resolved_name = str(resolved_app.get("name") or "").strip() or clean_name
    resolution_metadata = (
        _app_resolution_metadata_from_match(clean_name, resolved_app)
        if resolved_name != clean_name
        else {}
    )
    focus_attempts: list[dict[str, Any]] = []
    result = _system_events_focus_app(resolved_name)
    if not result["ok"]:
        focus_attempts.append(_app_focus_attempt("applescript_system_events", result))
        fallback = app_open(clean_name)
        appkit_data: dict[str, Any] = {}
        if fallback.get("ok"):
            fallback_data = fallback.get("data") if isinstance(fallback.get("data"), dict) else {}
            appkit_result = _appkit_activate_app(resolved_name, bundle_id=_app_bundle_id(resolved_name))
            appkit_data = _parse_appkit_focus_output(appkit_result.get("stdout"), resolved_name)
            focus_attempts.append(_app_focus_attempt("appkit_nsrunningapplication", appkit_result, appkit_data))
            if appkit_data.get("focus_verified") is True:
                return {
                    "ok": True,
                    "action": "app.focus",
                    "summary": f"Focused {resolved_name} via AppKit",
                    "data": {
                        "app_name": clean_name,
                        **fallback_data,
                        **appkit_data,
                        **resolution_metadata,
                        "focus_fallback": "app.open_then_appkit",
                        "focus_attempts": focus_attempts,
                    },
                    "permission_error": False,
                    "fallback_used": True,
                    "fallback_result": {"open": fallback, "appkit": appkit_result},
                }
            electron_native_result: dict[str, Any] | None = None
            electron_native_data: dict[str, Any] = {}
            if _electron_native_bridge_config() is not None:
                electron_native_result = _electron_native_focus_app(resolved_name)
                native_data = electron_native_result.get("data")
                electron_native_data = native_data if isinstance(native_data, dict) else {}
                focus_attempts.append(
                    _app_focus_attempt(
                        "electron_native_bridge",
                        electron_native_result,
                        electron_native_data,
                    )
                )
                if (
                    electron_native_result.get("ok") is True
                    and electron_native_data.get("focus_verified") is True
                ):
                    return {
                        "ok": True,
                        "action": "app.focus",
                        "summary": f"Focused {resolved_name} via Electron native bridge",
                        "data": {
                            "app_name": clean_name,
                            **fallback_data,
                            **electron_native_data,
                            **resolution_metadata,
                            "focus_fallback": "electron_native_bridge",
                            "focus_attempts": focus_attempts,
                        },
                        "permission_error": False,
                        "fallback_used": True,
                        "fallback_result": {
                            "open": fallback,
                            "appkit": appkit_result,
                            "electron_native": electron_native_result,
                        },
                    }
            latest_observation = _latest_focus_observation(appkit_data, electron_native_data)
            fallback_result: dict[str, Any] = {"open": fallback, "appkit": appkit_result}
            if electron_native_result is not None:
                fallback_result["electron_native"] = electron_native_result
            failed_data = {
                "app_name": clean_name,
                **fallback_data,
                **(
                    resolution_metadata
                    or _app_resolution_metadata(
                        clean_name,
                        str(fallback_data.get("app_name") or resolved_name),
                    )
                ),
                **latest_observation,
                "focus_fallback": "app.open",
                "focus_verified": False,
                "focus_status": "not_frontmost",
                "frontmost_app": latest_observation.get("frontmost_app")
                or appkit_data.get("frontmost_app")
                or "",
                "blocking_condition": "foreground_focus_unavailable",
                "retryable": True,
                "focus_attempts": focus_attempts,
                "recommended_tools": [
                    "desktop.running_apps",
                    "desktop.active_window",
                    "screen.capture",
                ],
            }
            locked_by_observation = _focus_failure_indicates_locked_session(
                failed_data["frontmost_app"],
                focus_attempts,
            )
            locked_by_runtime_probe = _desktop_session_locked_by_runtime_probe()
            runtime_probe_has_unlocked_observation = (
                locked_by_runtime_probe
                and _focus_attempts_observed_unlocked_frontmost(focus_attempts)
            )
            if runtime_probe_has_unlocked_observation:
                failed_data["desktop_session_locked_runtime_probe_ignored"] = True
            if locked_by_observation or (
                locked_by_runtime_probe and not runtime_probe_has_unlocked_observation
            ):
                if locked_by_runtime_probe:
                    failed_data["desktop_session_locked_by_runtime_probe"] = True
                return _desktop_session_locked_result(
                    "app.focus",
                    app_name=resolved_name,
                    data=failed_data,
                    fallback_result=fallback_result,
                )
            return {
                "ok": False,
                "action": "app.focus",
                "summary": f"Could not verify {resolved_name} is foreground after app.open fallback",
                "error": "app_focus_not_verified",
                "blocking_condition": "foreground_focus_unavailable",
                "retryable": True,
                "data": failed_data,
                "recommended_tools": ["app.open", "desktop.active_window", "screen.capture"],
                "missing_permissions": ["foreground_focus"],
                "permission_targets": ["foreground_focus"],
                "recovery_hints": _permission_recovery_hints_for_targets(["foreground_focus"]),
                "recovery_actions": _app_focus_recovery_actions(resolved_name),
                "permission_error": False,
                "fallback_used": True,
                "fallback_result": fallback,
                "focus_fallback_result": fallback_result,
            }
        payload = _with_permission_metadata(
            "app.focus",
            {**result, "action": "app.focus", "summary": "app.focus failed"},
        )
        payload["fallback_used"] = bool(fallback.get("ok"))
        payload["fallback_result"] = fallback
        return payload
    focus_data = _parse_app_focus_output(result.get("stdout"), resolved_name)
    focus_attempts.append(_app_focus_attempt("applescript_system_events", result, focus_data))
    focus_verified = focus_data.get("focus_verified") is True
    data = {
        **focus_data,
        **resolution_metadata,
    }
    if focus_verified:
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {resolved_name}",
            "data": data,
            "permission_error": False,
            "fallback_used": resolved_name != clean_name,
        }
    appkit_result = _appkit_activate_app(resolved_name, bundle_id=_app_bundle_id(resolved_name))
    appkit_data = _parse_appkit_focus_output(appkit_result.get("stdout"), resolved_name)
    focus_attempts.append(_app_focus_attempt("appkit_nsrunningapplication", appkit_result, appkit_data))
    if appkit_data.get("focus_verified") is True:
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {resolved_name} via AppKit",
            "data": {
                **data,
                **appkit_data,
                "focus_attempts": focus_attempts,
            },
            "permission_error": False,
            "fallback_used": True,
        }
    launchservices_result: dict[str, Any] | None = None
    launchservices_data: dict[str, Any] = {}
    dock_result: dict[str, Any] | None = None
    dock_data: dict[str, Any] = {}
    if _focus_surface_retry_needed(data, appkit_data):
        launchservices_result = _launchservices_focus_app(resolved_name)
        if launchservices_result.get("ok"):
            launchservices_data = _parse_app_focus_output(
                launchservices_result.get("stdout"),
                resolved_name,
            )
        focus_attempts.append(
            _app_focus_attempt(
                "launchservices_open_a",
                launchservices_result,
                launchservices_data,
            )
        )
        if launchservices_data.get("focus_verified") is True:
            return {
                "ok": True,
                "action": "app.focus",
                "summary": f"Focused {resolved_name} via LaunchServices",
                "data": {
                    **data,
                    **launchservices_data,
                    "focus_fallback": "launchservices_open_a",
                    "focus_attempts": focus_attempts,
                },
                "permission_error": False,
                "fallback_used": True,
            }
        dock_result = _dock_focus_app(resolved_name)
        if dock_result.get("ok"):
            dock_data = _parse_dock_focus_output(dock_result.get("stdout"), resolved_name)
        focus_attempts.append(_app_focus_attempt("dock", dock_result, dock_data))
        if dock_data.get("focus_verified") is True:
            return {
                "ok": True,
                "action": "app.focus",
                "summary": f"Focused {resolved_name} via Dock",
                "data": {
                    **data,
                    **dock_data,
                    "focus_fallback": "dock",
                    "focus_attempts": focus_attempts,
                },
                "permission_error": False,
                "fallback_used": True,
            }
    electron_native_result: dict[str, Any] | None = None
    electron_native_data: dict[str, Any] = {}
    if _electron_native_bridge_config() is not None:
        electron_native_result = _electron_native_focus_app(resolved_name)
        native_data = electron_native_result.get("data")
        electron_native_data = native_data if isinstance(native_data, dict) else {}
        focus_attempts.append(
            _app_focus_attempt(
                "electron_native_bridge",
                electron_native_result,
                electron_native_data,
            )
        )
        if (
            electron_native_result.get("ok") is True
            and electron_native_data.get("focus_verified") is True
        ):
            return {
                "ok": True,
                "action": "app.focus",
                "summary": f"Focused {resolved_name} via Electron native bridge",
                "data": {
                    **data,
                    **electron_native_data,
                    "focus_fallback": "electron_native_bridge",
                    "focus_attempts": focus_attempts,
                },
                "permission_error": False,
                "fallback_used": True,
            }
    latest_observation = _latest_focus_observation(
        data,
        appkit_data,
        launchservices_data,
        dock_data,
        electron_native_data,
    )
    fallback_result: dict[str, Any] = {"appkit": appkit_result}
    if launchservices_result is not None:
        fallback_result["launchservices"] = launchservices_result
    if dock_result is not None:
        fallback_result["dock"] = dock_result
    if electron_native_result is not None:
        fallback_result["electron_native"] = electron_native_result
    failed_data = {
        **data,
        **latest_observation,
        "focus_verified": False,
        "focus_status": "not_frontmost",
        "frontmost_app": latest_observation.get("frontmost_app")
        or appkit_data.get("frontmost_app")
        or data.get("frontmost_app")
        or "",
        "blocking_condition": "foreground_focus_unavailable",
        "retryable": True,
        "focus_attempts": focus_attempts,
        "recommended_tools": [
            "desktop.running_apps",
            "desktop.active_window",
            "screen.capture",
        ],
    }
    locked_by_observation = _focus_failure_indicates_locked_session(
        failed_data["frontmost_app"],
        focus_attempts,
    )
    locked_by_runtime_probe = _desktop_session_locked_by_runtime_probe()
    runtime_probe_has_unlocked_observation = (
        locked_by_runtime_probe
        and _focus_attempts_observed_unlocked_frontmost(focus_attempts)
    )
    if runtime_probe_has_unlocked_observation:
        failed_data["desktop_session_locked_runtime_probe_ignored"] = True
    if locked_by_observation or (
        locked_by_runtime_probe and not runtime_probe_has_unlocked_observation
    ):
        if locked_by_runtime_probe:
            failed_data["desktop_session_locked_by_runtime_probe"] = True
        return _desktop_session_locked_result(
            "app.focus",
            app_name=resolved_name,
            data=failed_data,
            fallback_result=fallback_result,
        )
    return {
        "ok": False,
        "action": "app.focus",
        "summary": f"Could not verify {resolved_name} is foreground",
        "error": "app_focus_not_verified",
        "blocking_condition": "foreground_focus_unavailable",
        "retryable": True,
        "data": failed_data,
        "recommended_tools": ["app.open", "desktop.active_window", "screen.capture"],
        "missing_permissions": ["foreground_focus"],
        "permission_targets": ["foreground_focus"],
        "recovery_hints": _permission_recovery_hints_for_targets(["foreground_focus"]),
        "recovery_actions": _app_focus_recovery_actions(resolved_name),
        "permission_error": False,
        "fallback_used": bool(
            launchservices_result
            or dock_result
            or electron_native_result
            or resolved_name != clean_name
        ),
        "fallback_result": fallback_result,
    }


def app_focus_window(app_name: str, title_contains: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("app.focus_window")
    clean_name = _clean_required(app_name, "app_name")
    clean_title = _clean_required(title_contains, "title_contains")
    resolved_name = _resolve_installed_app_name(clean_name) or clean_name
    result = _run_osascript(
        """
        on lowercaseText(theText)
            set upperChars to "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            set lowerChars to "abcdefghijklmnopqrstuvwxyz"
            set outputText to ""
            repeat with charIndex from 1 to length of theText
                set oneChar to character charIndex of theText
                set charOffset to offset of oneChar in upperChars
                if charOffset > 0 then
                    set outputText to outputText & character charOffset of lowerChars
                else
                    set outputText to outputText & oneChar
                end if
            end repeat
            return outputText
        end lowercaseText

        on run argv
            set appName to item 1 of argv
            set titleQuery to item 2 of argv
            set loweredQuery to my lowercaseText(titleQuery)
            tell application "System Events"
                if not (exists application process appName) then
                    return "not_running|" & appName & "|" & titleQuery
                end if
                set visible of application process appName to true
                tell application process appName
                    set windowIndex to 0
                    set matchedIndex to 0
                    set matchedTitle to ""
                    repeat with windowRef in windows
                        set windowIndex to windowIndex + 1
                        try
                            set windowTitle to name of windowRef
                        on error
                            set windowTitle to ""
                        end try
                        if (my lowercaseText(windowTitle)) contains loweredQuery then
                            set matchedIndex to windowIndex
                            set matchedTitle to windowTitle
                            try
                                if value of attribute "AXMinimized" of windowRef is true then
                                    set value of attribute "AXMinimized" of windowRef to false
                                end if
                            end try
                            try
                                perform action "AXRaise" of windowRef
                            end try
                            try
                                set value of attribute "AXMain" of windowRef to true
                            end try
                            exit repeat
                        end if
                    end repeat
                end tell
            end tell
            if matchedTitle is "" then
                return "not_found|" & appName & "|" & titleQuery
            end if
            tell application appName to activate
            return "focused|" & appName & "|" & matchedIndex & "|" & matchedTitle
        end run
        """,
        [resolved_name, clean_title],
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "app.focus_window",
            {
                **result,
                "action": "app.focus_window",
                "summary": "app.focus_window failed",
                "data": {
                    "app_name": resolved_name,
                    "title_contains": clean_title,
                    **_app_resolution_metadata(clean_name, resolved_name),
                },
            },
        )
    stdout = str(result.get("stdout") or "").strip()
    parts = stdout.split("|", 3) if stdout else []
    status = parts[0] if parts else "unknown"
    if status == "not_running":
        return {
            "ok": False,
            "action": "app.focus_window",
            "summary": f"{resolved_name} is not running",
            "error": "app_not_running",
            "error_code": "app_not_running",
            "data": {
                "app_name": resolved_name,
                "title_contains": clean_title,
                "focus_status": status,
                **_app_resolution_metadata(clean_name, resolved_name),
            },
            "permission_error": False,
            "fallback_used": resolved_name != clean_name,
        }
    if status == "not_found":
        return {
            "ok": False,
            "action": "app.focus_window",
            "summary": f"No {resolved_name} window matched {clean_title}",
            "error": "window_not_found",
            "error_code": "window_not_found",
            "data": {
                "app_name": resolved_name,
                "title_contains": clean_title,
                "focus_status": status,
                **_app_resolution_metadata(clean_name, resolved_name),
            },
            "permission_error": False,
            "fallback_used": resolved_name != clean_name,
        }
    window_index = _int_value(parts[2] if len(parts) > 2 else 0)
    window_title = parts[3] if len(parts) > 3 else ""
    return {
        "ok": True,
        "action": "app.focus_window",
        "summary": f"Focused {resolved_name} window: {window_title or clean_title}",
        "data": {
            "app_name": resolved_name,
            "title_contains": clean_title,
            "focus_status": status,
            "window_index": window_index,
            "window_title": window_title,
            **_app_resolution_metadata(clean_name, resolved_name),
        },
        "permission_error": False,
        "fallback_used": resolved_name != clean_name,
    }


def app_show(app_name: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("app.show")
    clean_name = _clean_required(app_name, "app_name")
    result = _run_osascript(
        """
        on run argv
            set appName to item 1 of argv
            set statusText to "launched"
            set restoredCount to 0
            tell application "System Events"
                if exists application process appName then
                    set statusText to "shown"
                    set visible of application process appName to true
                    tell application process appName
                        repeat with windowRef in windows
                            try
                                if value of attribute "AXMinimized" of windowRef is true then
                                    set value of attribute "AXMinimized" of windowRef to false
                                    set restoredCount to restoredCount + 1
                                end if
                            end try
                        end repeat
                    end tell
                end if
            end tell
            tell application appName to activate
            return statusText & "|" & appName & "|" & restoredCount
        end run
        """,
        [clean_name],
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "app.show",
            {
                **result,
                "action": "app.show",
                "summary": "app.show failed",
                "data": {"app_name": clean_name},
            },
        )
    stdout = str(result.get("stdout") or "").strip()
    parts = stdout.split("|") if stdout else []
    status = parts[0] if parts else "unknown"
    restored_count = _int_value(parts[2] if len(parts) > 2 else 0)
    summary = (
        f"Launched and showed {clean_name}"
        if status == "launched"
        else f"Showed {clean_name}"
    )
    return {
        "ok": True,
        "action": "app.show",
        "summary": summary,
        "data": {
            "app_name": clean_name,
            "show_status": status,
            "restored_window_count": restored_count,
        },
        "permission_error": False,
        "fallback_used": False,
    }


def app_hide(app_name: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("app.hide")
    clean_name = _clean_required(app_name, "app_name")
    result = _run_osascript(
        """
        on run argv
            set appName to item 1 of argv
            tell application "System Events"
                if exists application process appName then
                    set visible of application process appName to false
                    return "hidden|" & appName
                end if
            end tell
            return "not_running|" & appName
        end run
        """,
        [clean_name],
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "app.hide",
            {
                **result,
                "action": "app.hide",
                "summary": "app.hide failed",
                "data": {"app_name": clean_name},
            },
        )
    stdout = str(result.get("stdout") or "").strip()
    status = stdout.split("|", 1)[0] if stdout else "unknown"
    if status == "not_running":
        return {
            "ok": False,
            "action": "app.hide",
            "summary": f"{clean_name} is not running",
            "error": "app_not_running",
            "error_code": "app_not_running",
            "data": {"app_name": clean_name, "hide_status": status},
            "permission_error": False,
            "fallback_used": False,
        }
    return {
        "ok": True,
        "action": "app.hide",
        "summary": f"Hid {clean_name}",
        "data": {"app_name": clean_name, "hide_status": status},
        "permission_error": False,
        "fallback_used": False,
    }


def app_minimize(app_name: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("app.minimize")
    clean_name = _clean_required(app_name, "app_name")
    result = _run_osascript(
        """
        on run argv
            set appName to item 1 of argv
            tell application "System Events"
                if not (exists application process appName) then
                    return "not_running|" & appName & "|0"
                end if
                tell application process appName
                    set windowCount to count of windows
                    if windowCount is 0 then
                        return "no_windows|" & appName & "|0"
                    end if
                    repeat with windowRef in windows
                        try
                            set value of attribute "AXMinimized" of windowRef to true
                        on error
                            try
                                set miniaturized of windowRef to true
                            end try
                        end try
                    end repeat
                    return "minimized|" & appName & "|" & windowCount
                end tell
            end tell
        end run
        """,
        [clean_name],
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "app.minimize",
            {
                **result,
                "action": "app.minimize",
                "summary": "app.minimize failed",
                "data": {"app_name": clean_name},
            },
        )
    stdout = str(result.get("stdout") or "").strip()
    parts = stdout.split("|") if stdout else []
    status = parts[0] if parts else "unknown"
    window_count = _int_value(parts[2] if len(parts) > 2 else 0)
    if status == "not_running":
        return {
            "ok": False,
            "action": "app.minimize",
            "summary": f"{clean_name} is not running",
            "error": "app_not_running",
            "error_code": "app_not_running",
            "data": {
                "app_name": clean_name,
                "minimize_status": status,
                "window_count": window_count,
            },
            "permission_error": False,
            "fallback_used": False,
        }
    if status == "no_windows":
        return {
            "ok": False,
            "action": "app.minimize",
            "summary": f"{clean_name} has no windows to minimize",
            "error": "app_no_windows",
            "error_code": "app_no_windows",
            "data": {
                "app_name": clean_name,
                "minimize_status": status,
                "window_count": window_count,
            },
            "permission_error": False,
            "fallback_used": False,
        }
    return {
        "ok": True,
        "action": "app.minimize",
        "summary": f"Minimized {clean_name}",
        "data": {
            "app_name": clean_name,
            "minimize_status": status,
            "window_count": window_count,
        },
        "permission_error": False,
        "fallback_used": False,
    }


def app_quit(app_name: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("app.quit")
    clean_name = _clean_required(app_name, "app_name")
    result = _run_osascript(
        """
        on run argv
            set appName to item 1 of argv
            if application appName is running then
                tell application appName to quit
                delay 0.2
                if application appName is running then
                    return "quit_requested_running|" & appName
                end if
                return "quit|" & appName
            end if
            return "not_running|" & appName
        end run
        """,
        [clean_name],
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "app.quit",
            {
                **result,
                "action": "app.quit",
                "summary": "app.quit failed",
                "data": {"app_name": clean_name},
            },
        )
    stdout = str(result.get("stdout") or "").strip()
    status = stdout.split("|", 1)[0] if stdout else "unknown"
    verification = _app_running_verification(clean_name)
    running = verification.get("launch_verified")
    still_running = running is True
    if status == "not_running":
        summary = f"{clean_name} was not running"
    elif still_running:
        summary = f"Sent quit request to {clean_name}"
    else:
        summary = f"Quit {clean_name}"
    return {
        "ok": True,
        "action": "app.quit",
        "summary": summary,
        "data": {
            "app_name": clean_name,
            "quit_status": status,
            "quit_verified": running is False,
            "running": running,
            **verification,
        },
        "permission_error": False,
        "fallback_used": False,
    }


def _apple_music_background_partial_result(
    query: str,
    *,
    status: str = "not_found",
    summary: str = "",
    extra_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "action": "media.apple_music_play",
        "summary": summary
        or (
            f"Apple Music local library did not contain {query}; "
            "no foreground search was opened."
        ),
        "data": {
            "query": query,
            "status": status,
            "background_safe": True,
            "library_search_completed": True,
            "foreground_action_taken": False,
            "target_app": "Music",
            "search_opened": False,
            "dispatch_verified": False,
            "foreground_verified": False,
            "search_query_verified": False,
            "search_query_identity_verified": False,
            "search_result_changed_from_nonmatching_baseline": False,
            "playback_started": False,
            "outcome": "partial",
            "user_action_required": False,
            **dict(extra_data or {}),
        },
        "permission_error": False,
        "fallback_used": False,
    }


def _normalized_music_identity(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _apple_music_identity_matches(query: Any, candidate: Any) -> bool:
    query_identity = _normalized_music_identity(query)
    candidate_identity = _normalized_music_identity(candidate)
    if not query_identity or not candidate_identity:
        return False
    if candidate_identity == query_identity:
        return True
    # A user may omit one sentence-like mark at the end of a catalog title.
    # This is intentionally asymmetric: an explicit `What?` must not match
    # `What!`, and `Foo` must not match an emphatic `Foo!!` variant.
    return bool(
        query_identity[-1] not in "!?"
        and candidate_identity[-1] in "!?"
        and candidate_identity[:-1] == query_identity
    )


def _loose_normalized_music_identity(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _apple_music_terminal_punctuation_variants(value: Any) -> list[str]:
    raw = str(value or "").strip()
    normalized = unicodedata.normalize("NFKC", raw).strip()
    variants: list[str] = []
    candidates = [raw, normalized]
    if normalized and normalized[-1] not in "!?！？":
        candidates.extend(
            (
                f"{normalized}!",
                f"{normalized}！",
                f"{normalized}?",
                f"{normalized}？",
            )
        )
    for candidate in candidates:
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def _apple_music_storefront_country() -> str:
    try:
        locale_name = str(locale.getlocale()[0] or "")
    except Exception:
        locale_name = ""
    match = re.search(r"[_-]([A-Za-z]{2})(?:[.@_-]|$)", locale_name)
    return match.group(1).upper() if match else "US"


def _apple_music_catalog_url_is_valid(value: Any, track_id: Any) -> bool:
    url = str(value or "").strip()
    expected_track_id = str(track_id or "").strip()
    if not url or not expected_track_id.isdigit():
        return False
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "music.apple.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return False
    return parse_qs(parsed.query, keep_blank_values=True).get("i") == [
        expected_track_id
    ]


def _apple_music_catalog_result_is_weak_cover(
    result: Mapping[str, Any],
    *,
    query: str,
) -> bool:
    query_identity = _loose_normalized_music_identity(query)
    candidate_identity = _loose_normalized_music_identity(
        " ".join(
            str(result.get(key) or "")
            for key in ("trackName", "collectionName", "artistName")
        )
    )
    weak_markers = (
        "pianocover",
        "coverversion",
        "instrumental",
        "karaoke",
        "钢琴",
        "翻奏",
        "伴奏",
        "纯音乐",
        "改编",
    )
    return any(
        marker in candidate_identity and marker not in query_identity
        for marker in weak_markers
    )


def _apple_music_catalog_match(
    query: str,
    results: Any,
) -> dict[str, Any] | None:
    query_identity = _normalized_music_identity(query)
    if not query_identity or not isinstance(results, list):
        return None
    collection_candidates: dict[
        tuple[str, ...],
        list[tuple[tuple[int, int, int, int], dict[str, Any]]],
    ] = {}
    track_candidates: list[dict[str, Any]] = []
    for raw in results:
        if not isinstance(raw, Mapping) or str(raw.get("kind") or "") != "song":
            continue
        track_name = str(raw.get("trackName") or "").strip()
        artist = str(raw.get("artistName") or "").strip()
        collection = str(raw.get("collectionName") or "").strip()
        track_exact = _apple_music_identity_matches(query, track_name)
        collection_exact = _apple_music_identity_matches(query, collection)
        if not track_exact and not collection_exact:
            continue
        if _apple_music_catalog_result_is_weak_cover(raw, query=query):
            continue
        track_id = raw.get("trackId")
        if not _apple_music_catalog_url_is_valid(raw.get("trackViewUrl"), track_id):
            continue
        if not track_name or not artist:
            continue
        try:
            track_number = int(raw.get("trackNumber") or 9999)
        except (TypeError, ValueError):
            track_number = 9999
        try:
            disc_number = int(raw.get("discNumber") or 1)
        except (TypeError, ValueError):
            disc_number = 1
        try:
            numeric_track_id = int(track_id)
        except (TypeError, ValueError):
            continue
        collection_id = str(raw.get("collectionId") or "").strip()
        collection_artist = str(raw.get("collectionArtistName") or "").strip()
        candidate = {
            "track_id": str(track_id),
            "track": track_name,
            "artist": artist,
            "collection": collection,
            "collection_id": collection_id,
            "collection_artist": collection_artist,
            "disc_number": disc_number,
            "track_number": track_number,
            "track_url": str(raw.get("trackViewUrl") or "").strip(),
            "match_kind": "collection" if collection_exact else "track",
        }
        if collection_exact:
            # The search API may return several unrelated releases with the
            # same collectionName.  Only select an album when every exact-name
            # result belongs to one collection identity.  collectionId is the
            # authoritative identity; older/partial payloads fall back to the
            # normalized collection and collection artist (or track artist),
            # intentionally failing closed for multi-artist ambiguity.
            collection_identity = (
                ("collection_id", collection_id)
                if collection_id
                else (
                    "collection_fallback",
                    _normalized_music_identity(collection),
                    _normalized_music_identity(collection_artist or artist),
                )
            )
            collection_candidates.setdefault(collection_identity, []).append(
                (
                    (
                        disc_number,
                        0 if track_number > 0 else 1,
                        track_number,
                        numeric_track_id,
                    ),
                    candidate,
                )
            )
        elif track_exact:
            track_candidates.append(candidate)
    if collection_candidates:
        if len(collection_candidates) != 1:
            return None
        sole_collection = next(iter(collection_candidates.values()))
        return min(sole_collection, key=lambda item: item[0])[1]
    # A title alone does not identify a song when the catalog returns different
    # artists or collections. Fail closed instead of selecting an arbitrary ID.
    return track_candidates[0] if len(track_candidates) == 1 else None


def _fetch_apple_music_catalog_match(
    query: str,
    *,
    deadline: float,
) -> tuple[dict[str, Any] | None, str]:
    timeout = _remaining_timeout(deadline, 5.0)
    if timeout <= 0:
        return None, "catalog_deadline_exceeded"
    params = urlencode(
        {
            "term": query,
            "entity": "song",
            "limit": _APPLE_MUSIC_CATALOG_LIMIT,
            "country": _apple_music_storefront_country(),
        }
    )
    request = Request(
        f"https://itunes.apple.com/search?{params}",
        headers={"Accept": "application/json", "User-Agent": "Oha-Yachiyo/1"},
        method="GET",
    )
    try:
        with urlopen_with_bundled_ca(request, timeout=timeout) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            return None, "catalog_response_too_large"
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return None, f"catalog_lookup_{exc.__class__.__name__}"
    if not isinstance(payload, Mapping) or not isinstance(
        payload.get("results"), list
    ):
        return None, "catalog_response_invalid"
    return _apple_music_catalog_match(query, payload.get("results")), ""


def _apple_music_playback_snapshot(*, timeout_seconds: float) -> dict[str, Any]:
    result = _call_with_bounded_timeout(
        _run_osascript,
        """
        if application "Music" is not running then
            return "status|not_running||"
        end if
        tell application "Music"
            try
                set stateText to player state as text
                set trackName to ""
                set artistName to ""
                try
                    set trackName to name of current track
                    set artistName to artist of current track
                end try
                return "status|" & stateText & "|" & trackName & "|" & artistName
            on error errMsg number errNum
                return "error|" & errNum & "|" & errMsg & "|"
            end try
        end tell
        """,
        timeout_seconds=timeout_seconds,
    )
    if result.get("ok") is not True:
        detail = str(result.get("error") or result.get("stderr") or "").strip()
        return {
            "ok": False,
            "error": detail or "Music status unavailable",
            "permission_error": _looks_like_permission_error(detail),
            "raw": result,
        }
    parts = str(result.get("stdout") or "").strip().split("|", 3)
    if not parts or parts[0] != "status":
        detail = "|".join(parts[1:]) if parts and parts[0] == "error" else str(
            result.get("stdout") or ""
        )
        return {
            "ok": False,
            "error": detail or "Music status unavailable",
            "permission_error": _looks_like_permission_error(detail),
            "raw": result,
        }
    while len(parts) < 4:
        parts.append("")
    return {
        "ok": True,
        "player_state": parts[1].strip().lower(),
        "track": parts[2].strip(),
        "artist": parts[3].strip(),
    }


def _apple_music_track_identity_matches(
    snapshot: Mapping[str, Any],
    match: Mapping[str, Any],
) -> bool:
    return bool(
        snapshot.get("ok") is True
        and _normalized_music_identity(snapshot.get("track"))
        == _normalized_music_identity(match.get("track"))
        and _normalized_music_identity(snapshot.get("artist"))
        == _normalized_music_identity(match.get("artist"))
    )


def _apple_music_automation_failure(
    query: str,
    result: Mapping[str, Any],
    *,
    summary: str,
) -> dict[str, Any]:
    raw = result.get("raw") if isinstance(result.get("raw"), Mapping) else result
    return {
        **_with_permission_metadata(
            "media.apple_music_play",
            {
                **dict(raw),
                "ok": False,
                "action": "media.apple_music_play",
                "summary": summary,
                "error": str(result.get("error") or raw.get("error") or summary),
                "permission_error": bool(result.get("permission_error"))
                or _looks_like_permission_error(result.get("error")),
                "data": {"query": query},
            },
        ),
        "fallback_used": False,
    }


def _apple_music_play_catalog_match(
    query: str,
    match: Mapping[str, Any],
    *,
    deadline: float,
) -> dict[str, Any]:
    try:
        frontmost_before_result = _appkit_frontmost_app_name()
    except Exception:
        frontmost_before_result = {}
    frontmost_before = str(
        (
            frontmost_before_result.get("app_name")
            if isinstance(frontmost_before_result, Mapping)
            else ""
        )
        or ""
    ).strip()
    foreground_observation_verified = bool(
        isinstance(frontmost_before_result, Mapping)
        and frontmost_before_result.get("ok") is True
        and frontmost_before
    )
    frontmost_after = frontmost_before
    frontmost_observations = [frontmost_before] if frontmost_before else []
    frontmost_observation_failures = 0 if foreground_observation_verified else 1
    foreground_action_taken = False
    before: dict[str, Any] = {}

    def observe_frontmost() -> bool:
        nonlocal foreground_action_taken, foreground_observation_verified
        nonlocal frontmost_after, frontmost_observation_failures
        try:
            observation = _appkit_frontmost_app_name()
        except Exception:
            observation = {}
        observed = str(
            (
                observation.get("app_name")
                if isinstance(observation, Mapping)
                else ""
            )
            or ""
        ).strip()
        if not (
            isinstance(observation, Mapping)
            and observation.get("ok") is True
            and observed
        ):
            foreground_observation_verified = False
            frontmost_observation_failures += 1
            return False
        frontmost_after = observed
        frontmost_observations.append(observed)
        if _compact_app_match_name(observed) != _compact_app_match_name(
            frontmost_before
        ):
            foreground_action_taken = True
            foreground_observation_verified = False
        return foreground_observation_verified

    def foreground_evidence() -> dict[str, Any]:
        return {
            "frontmost_before": frontmost_before,
            "frontmost_after": frontmost_after,
            "frontmost_observations": list(frontmost_observations),
            "frontmost_observation_failures": frontmost_observation_failures,
            "foreground_observation_verified": foreground_observation_verified,
            "foreground_action_taken": foreground_action_taken,
            "background_safe": bool(
                foreground_observation_verified and not foreground_action_taken
            ),
        }

    def catalog_partial(
        summary: str,
        *,
        status: str = "catalog_playback_unverified",
        extra_data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _apple_music_background_partial_result(
            query,
            status=status,
            summary=summary,
            extra_data={
                **dict(match),
                "catalog_match_verified": True,
                "catalog_dispatch_verified": False,
                "track_identity_verified": False,
                "playback_state_unverified": True,
                "old_track": str(before.get("track") or ""),
                "old_artist": str(before.get("artist") or ""),
                **foreground_evidence(),
                **dict(extra_data or {}),
            },
        )

    if not foreground_observation_verified:
        return catalog_partial(
            "Apple Music catalog playback was skipped because foreground state "
            "could not be observed safely.",
            status="foreground_observation_unverified",
        )

    before_timeout = _remaining_timeout(deadline, 3.0)
    if before_timeout <= 0:
        return catalog_partial(
            "Apple Music found an exact catalog match, but its playback deadline "
            "was reached before dispatch.",
            extra_data={"catalog_deadline_exceeded": True},
        )
    before = _apple_music_playback_snapshot(timeout_seconds=before_timeout)
    if before.get("ok") is not True and before.get("permission_error") is True:
        return _apple_music_automation_failure(
            query,
            before,
            summary="Apple Music automation permission is required.",
        )
    if before.get("ok") is not True:
        return catalog_partial(
            "Apple Music found an exact catalog match, but the initial playback "
            "state could not be verified.",
        )

    dispatch_timeout = _remaining_timeout(deadline, 3.0)
    dispatch = _call_with_bounded_timeout(
        _run_osascript,
        """
        on run argv
            set trackUrl to item 1 of argv
            tell application "Music" to open location trackUrl
            return "dispatched"
        end run
        """,
        [str(match.get("track_url") or "")],
        timeout_seconds=dispatch_timeout,
    ) if dispatch_timeout > 0 else {"ok": False, "error": "catalog_deadline_exceeded"}
    observe_frontmost()
    if dispatch.get("ok") is not True:
        if _looks_like_permission_error(dispatch.get("error") or dispatch.get("stderr")):
            return _apple_music_automation_failure(
                query,
                dispatch,
                summary="Apple Music could not open the matched catalog track.",
            )
        return catalog_partial(
            summary="Apple Music found an exact catalog match but did not accept its URL.",
        )
    if not foreground_observation_verified:
        return catalog_partial(
            "Apple Music accepted the catalog URL, but foreground state changed "
            "or became unavailable before playback.",
            status="foreground_observation_unverified",
            extra_data={"catalog_dispatch_verified": True},
        )

    opened_snapshot: dict[str, Any] = {}
    for attempt in range(_APPLE_MUSIC_CATALOG_POLL_ATTEMPTS):
        snapshot_timeout = _remaining_timeout(deadline, 2.0)
        if snapshot_timeout <= 0:
            break
        opened_snapshot = _apple_music_playback_snapshot(
            timeout_seconds=snapshot_timeout
        )
        observe_frontmost()
        if opened_snapshot.get("ok") is not True and opened_snapshot.get(
            "permission_error"
        ) is True:
            return _apple_music_automation_failure(
                query,
                opened_snapshot,
                summary="Apple Music playback state requires Automation permission.",
            )
        if not foreground_observation_verified:
            break
        if _apple_music_track_identity_matches(opened_snapshot, match):
            break
        if attempt + 1 < _APPLE_MUSIC_CATALOG_POLL_ATTEMPTS:
            time.sleep(0.15)
    if not foreground_observation_verified:
        return catalog_partial(
            "Apple Music found the catalog track, but foreground state changed "
            "or became unavailable before playback.",
            status="foreground_observation_unverified",
            extra_data={
                "catalog_dispatch_verified": True,
                "observed_track": str(opened_snapshot.get("track") or ""),
                "observed_artist": str(opened_snapshot.get("artist") or ""),
            },
        )
    if not _apple_music_track_identity_matches(opened_snapshot, match):
        if foreground_observation_verified:
            observe_frontmost()
        return catalog_partial(
            summary=(
                "Apple Music found an exact catalog match, but the target track "
                "was not verified."
            ),
            extra_data={
                "catalog_dispatch_verified": True,
                "observed_track": str(opened_snapshot.get("track") or ""),
                "observed_artist": str(opened_snapshot.get("artist") or ""),
            },
        )

    play_timeout = _remaining_timeout(deadline, 3.0)
    play_result = _call_with_bounded_timeout(
        _run_osascript,
        """
        on run argv
            set expectedTrack to item 1 of argv
            set expectedArtist to item 2 of argv
            tell application "Music"
                set currentTrackName to ""
                set currentArtistName to ""
                try
                    set currentTrackName to name of current track
                    set currentArtistName to artist of current track
                end try
                if currentTrackName is not expectedTrack then
                    return "identity_changed|" & currentTrackName & "|" & currentArtistName
                end if
                if currentArtistName is not expectedArtist then
                    return "identity_changed|" & currentTrackName & "|" & currentArtistName
                end if
                play current track
                return "played|" & currentTrackName & "|" & currentArtistName
            end tell
        end run
        """,
        [str(match.get("track") or ""), str(match.get("artist") or "")],
        timeout_seconds=play_timeout,
    ) if play_timeout > 0 else {"ok": False, "error": "catalog_deadline_exceeded"}
    observe_frontmost()
    if play_result.get("ok") is not True and _looks_like_permission_error(
        play_result.get("error") or play_result.get("stderr")
    ):
        return _apple_music_automation_failure(
            query,
            play_result,
            summary="Apple Music playback requires Automation permission.",
        )
    play_status, observed_track, observed_artist = _split_status(
        play_result.get("stdout")
    )
    if play_result.get("ok") is True and play_status == "identity_changed":
        return catalog_partial(
            summary=(
                "Apple Music changed tracks before playback, so nothing was played."
            ),
            extra_data={
                "catalog_dispatch_verified": True,
                "identity_changed_before_play": True,
                "observed_track": observed_track,
                "observed_artist": observed_artist,
            },
        )
    if not foreground_observation_verified:
        return catalog_partial(
            "Apple Music playback was requested, but foreground state changed or "
            "became unavailable before it could be verified.",
            status="foreground_observation_unverified",
            extra_data={"catalog_dispatch_verified": True},
        )

    final_snapshot: dict[str, Any] = {}
    for attempt in range(_APPLE_MUSIC_CATALOG_POLL_ATTEMPTS):
        snapshot_timeout = _remaining_timeout(deadline, 2.0)
        if snapshot_timeout <= 0:
            break
        final_snapshot = _apple_music_playback_snapshot(
            timeout_seconds=snapshot_timeout
        )
        observe_frontmost()
        if final_snapshot.get("ok") is not True and final_snapshot.get(
            "permission_error"
        ) is True:
            return _apple_music_automation_failure(
                query,
                final_snapshot,
                summary="Apple Music playback state requires Automation permission.",
            )
        if not foreground_observation_verified:
            break
        if (
            _apple_music_track_identity_matches(final_snapshot, match)
            and str(final_snapshot.get("player_state") or "").lower() == "playing"
        ):
            break
        if attempt + 1 < _APPLE_MUSIC_CATALOG_POLL_ATTEMPTS:
            time.sleep(0.15)
    observe_frontmost()
    identity_verified = _apple_music_track_identity_matches(final_snapshot, match)
    player_state = str(final_snapshot.get("player_state") or "unknown").lower()
    if (
        play_result.get("ok") is not True
        or play_status != "played"
        or not identity_verified
        or player_state != "playing"
        or foreground_action_taken
        or not foreground_observation_verified
    ):
        return catalog_partial(
            summary=(
                "Apple Music found the catalog track, but playback could not be "
                "safely verified."
            ),
            extra_data={
                "catalog_dispatch_verified": True,
                "track_identity_verified": identity_verified,
                "player_state": player_state,
            },
        )
    return {
        "ok": True,
        "action": "media.apple_music_play",
        "summary": f"Playing {match.get('track')} - {match.get('artist')}",
        "data": {
            "query": query,
            "status": "played",
            "track": str(match.get("track") or ""),
            "artist": str(match.get("artist") or ""),
            "collection": str(match.get("collection") or ""),
            "catalog_match_verified": True,
            "catalog_dispatch_verified": True,
            "track_identity_verified": True,
            "player_state": "playing",
            "playback_started": True,
            "background_safe": True,
            "foreground_action_taken": False,
            **foreground_evidence(),
            "old_track": str(before.get("track") or ""),
            "old_artist": str(before.get("artist") or ""),
            "track_url": str(match.get("track_url") or ""),
            "track_id": str(match.get("track_id") or ""),
        },
        "permission_error": False,
        "fallback_used": False,
    }


def _apple_music_play_deadline_result(
    query: str,
    *,
    fallback_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    fallback_payload = dict(fallback_result or {})
    result = _apple_music_background_partial_result(
        query,
        status="playback_unverified",
        summary=f"Apple Music search for {query} reached its execution deadline.",
        extra_data={
            "library_search_completed": isinstance(
                fallback_payload.get("library_search"), Mapping
            ),
            "deadline_exceeded": True,
            "playback_state_unverified": True,
            "track_identity_verified": False,
            "catalog_match_verified": False,
            "catalog_dispatch_verified": False,
        },
    )
    result["fallback_result"] = fallback_payload
    return result


def apple_music_play(query: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("media.apple_music_play")
    clean_query = _clean_required(query, "query")
    deadline = time.monotonic() + _APPLE_MUSIC_PLAY_DEADLINE_SECONDS
    initial_timeout = _remaining_timeout(deadline, 10.0)
    if initial_timeout <= 0:
        return _apple_music_play_deadline_result(clean_query)
    result = _call_with_bounded_timeout(
        _run_osascript,
        """
        on safeText(theValue)
            try
                return theValue as text
            on error
                return ""
            end try
        end safeText

        on sortableInteger(theValue)
            try
                set numericValue to theValue as integer
                if numericValue is less than 1 then return 2147483647
                return numericValue
            on error
                return 2147483647
            end try
        end sortableInteger

        on textListContains(valuesList, expectedValue)
            repeat with existingValue in valuesList
                if (contents of existingValue) is expectedValue then return true
            end repeat
            return false
        end textListContains

        on run argv
            set queryText to item 1 of argv
            set exactNameVariants to items 2 thru -1 of argv
            tell application "Music"
                try
                    set matches to (search library playlist 1 for queryText)
                    if (count of matches) is 0 then
                        return "not_found|" & queryText & "|"
                    end if

                    set exactAlbumTracks to {}
                    set exactAlbumIdentities to {}
                    set exactTrackMatches to {}
                    repeat with candidateRef in matches
                        set candidateTrack to contents of candidateRef
                        set trackName to my safeText(name of candidateTrack)
                        set artistName to my safeText(artist of candidateTrack)
                        set albumName to my safeText(album of candidateTrack)
                        set albumArtistName to my safeText(album artist of candidateTrack)
                        if albumArtistName is "" then set albumArtistName to artistName

                        if my textListContains(exactNameVariants, albumName) then
                            set end of exactAlbumTracks to candidateTrack
                            set albumIdentity to albumName & "\u001f" & albumArtistName
                            if not my textListContains(exactAlbumIdentities, albumIdentity) then
                                set end of exactAlbumIdentities to albumIdentity
                            end if
                        end if
                        if my textListContains(exactNameVariants, trackName) then
                            set end of exactTrackMatches to candidateTrack
                        end if
                    end repeat

                    set exactAlbumIdentityCount to count of exactAlbumIdentities
                    set exactTrackCount to count of exactTrackMatches
                    set selectedTrack to missing value
                    set matchKind to ""
                    if (count of exactAlbumTracks) is greater than 0 then
                        if exactAlbumIdentityCount is not 1 then
                            return "ambiguous_exact_album|" & queryText & "|" & exactAlbumIdentityCount & "|"
                        end if
                        set selectedTrack to item 1 of exactAlbumTracks
                        set bestDiscNumber to my sortableInteger(disc number of selectedTrack)
                        set bestTrackNumber to my sortableInteger(track number of selectedTrack)
                        set bestDatabaseID to my sortableInteger(database ID of selectedTrack)
                        repeat with candidateRef in exactAlbumTracks
                            set candidateTrack to contents of candidateRef
                            set candidateDiscNumber to my sortableInteger(disc number of candidateTrack)
                            set candidateTrackNumber to my sortableInteger(track number of candidateTrack)
                            set candidateDatabaseID to my sortableInteger(database ID of candidateTrack)
                            if candidateDiscNumber is less than bestDiscNumber or (candidateDiscNumber is bestDiscNumber and candidateTrackNumber is less than bestTrackNumber) or (candidateDiscNumber is bestDiscNumber and candidateTrackNumber is bestTrackNumber and candidateDatabaseID is less than bestDatabaseID) then
                                set selectedTrack to candidateTrack
                                set bestDiscNumber to candidateDiscNumber
                                set bestTrackNumber to candidateTrackNumber
                                set bestDatabaseID to candidateDatabaseID
                            end if
                        end repeat
                        set matchKind to "album"
                    else if exactTrackCount is 1 then
                        set selectedTrack to item 1 of exactTrackMatches
                        set matchKind to "track"
                    else if exactTrackCount is greater than 1 then
                        return "ambiguous_exact_track|" & queryText & "|" & exactTrackCount & "|"
                    else
                        return "no_exact_match|" & queryText & "||"
                    end if

                    set selectedTrackName to my safeText(name of selectedTrack)
                    set selectedArtistName to my safeText(artist of selectedTrack)
                    set selectedAlbumName to my safeText(album of selectedTrack)
                    play selectedTrack
                    set currentTrackName to my safeText(name of current track)
                    set currentArtistName to my safeText(artist of current track)
                    set currentAlbumName to my safeText(album of current track)
                    set stateText to player state as text
                    if currentTrackName is not selectedTrackName or currentArtistName is not selectedArtistName or currentAlbumName is not selectedAlbumName then
                        return "playback_unverified|" & currentTrackName & "|" & currentArtistName & "|" & stateText & "|" & matchKind & "|" & currentAlbumName & "|identity_unverified"
                    end if
                    return "played|" & currentTrackName & "|" & currentArtistName & "|" & stateText & "|" & matchKind & "|" & currentAlbumName & "|identity_verified"
                on error errMsg number errNum
                    return "error|" & errNum & "|" & errMsg
                end try
            end tell
        end run
        """,
        [clean_query, *_apple_music_terminal_punctuation_variants(clean_query)],
        timeout_seconds=initial_timeout,
    )
    if time.monotonic() >= deadline:
        return _apple_music_play_deadline_result(
            clean_query,
            fallback_result={"library_search": result},
        )
    if not result["ok"]:
        return {
            **_with_permission_metadata(
                "media.apple_music_play",
                {
                    **result,
                    "action": "media.apple_music_play",
                    "summary": "media.apple_music_play failed",
                },
            ),
            "action": "media.apple_music_play",
            "fallback_used": False,
            "fallback_result": {},
        }
    status, first, second = _split_status(result.get("stdout"))
    library_parts = str(result.get("stdout") or "").strip().split("|")
    if status in {"played", "playback_unverified"}:
        while len(library_parts) < 8:
            library_parts.append("")
        track = library_parts[1].strip()
        artist = library_parts[2].strip()
        player_state = library_parts[3].strip().lower()
        match_kind = library_parts[4].strip().lower()
        album = library_parts[5].strip()
        identity_receipt = library_parts[6].strip().lower()
        track_query_matches = bool(
            match_kind == "track"
            and _apple_music_identity_matches(clean_query, track)
        )
        album_query_matches = bool(
            match_kind == "album"
            and _apple_music_identity_matches(clean_query, album)
        )
        query_identity_matches = bool(
            track_query_matches or album_query_matches
        )
        identity_verified = bool(
            status == "played"
            and identity_receipt == "identity_verified"
            and query_identity_matches
            and track
            and artist
            and album
        )
        detail = f"{track}{f' - {artist}' if artist else ''}"
        if player_state != "playing" or not identity_verified:
            return _apple_music_background_partial_result(
                clean_query,
                status="playback_unverified",
                summary=f"Apple Music selected {detail}, but playback was not verified.",
                extra_data={
                    "track": track,
                    "artist": artist,
                    "album": album,
                    "match_kind": match_kind or "unknown",
                    "player_state": player_state or "unknown",
                    "track_identity_verified": False,
                    "playback_state_unverified": True,
                    "library_match_status": status,
                },
            )
        return {
            "ok": True,
            "action": "media.apple_music_play",
            "summary": f"Playing {detail}",
            "data": {
                "query": clean_query,
                "status": "played",
                "track": track,
                "artist": artist,
                "album": album,
                "match_kind": match_kind,
                "track_identity_verified": True,
                "player_state": "playing",
                "playback_started": True,
                "foreground_action_taken": False,
                "background_safe": True,
            },
            "permission_error": False,
            "fallback_used": False,
        }
    library_miss_statuses = {
        "not_found",
        "no_exact_match",
        "ambiguous_exact_track",
        "ambiguous_exact_album",
    }
    if status not in library_miss_statuses:
        error_text = second or first or "Music did not return a playable track"
        return _with_permission_metadata(
            "media.apple_music_play",
            {
                "ok": False,
                "action": "media.apple_music_play",
                "summary": f"Could not directly play {clean_query}.",
                "error": error_text,
                "data": {
                    "query": clean_query,
                    "status": status or "unknown",
                    "search_opened": False,
                    "dispatch_verified": False,
                    "foreground_verified": False,
                    "search_query_verified": False,
                    "search_query_identity_verified": False,
                    "search_result_changed_from_nonmatching_baseline": False,
                },
                "permission_error": _looks_like_permission_error(
                    f"{first}\n{second}"
                ),
                "fallback_used": False,
            },
        )
    catalog_match, catalog_error = _fetch_apple_music_catalog_match(
        clean_query,
        deadline=deadline,
    )
    if catalog_match is not None:
        catalog_result = _apple_music_play_catalog_match(
            clean_query,
            catalog_match,
            deadline=deadline,
        )
        catalog_data = catalog_result.get("data")
        if isinstance(catalog_data, dict):
            catalog_data.setdefault("library_match_status", status)
        return catalog_result
    if status == "not_found":
        summary = (
            f"Apple Music local library did not contain {clean_query}; official catalog "
            "lookup did not complete, and no foreground search was opened."
            if catalog_error
            else (
                "Apple Music local library and official catalog did not contain an "
                f"exact match for {clean_query}; no foreground search was opened."
            )
        )
    else:
        summary = (
            "Apple Music local library did not provide an unambiguous exact match for "
            f"{clean_query}; official catalog lookup did not complete, and no "
            "foreground search was opened."
            if catalog_error
            else (
                "Apple Music local library and official catalog did not provide one "
                f"unambiguous exact match for {clean_query}; no foreground search "
                "was opened."
            )
        )
    return _apple_music_background_partial_result(
        clean_query,
        summary=summary,
        extra_data={
            "library_match_status": status,
            "catalog_lookup_completed": not bool(catalog_error),
            "catalog_match_verified": False,
            **({"catalog_lookup_status": catalog_error} if catalog_error else {}),
        },
    )


def apple_music_status() -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("media.apple_music_status")
    result = _run_osascript(
        """
        if application "Music" is not running then
            return "status|not_running|||"
        end if
        tell application "Music"
            try
                set stateText to player state as text
                try
                    set trackName to name of current track
                    set artistName to artist of current track
                on error
                    set trackName to ""
                    set artistName to ""
                end try
                return "status|" & stateText & "|" & trackName & "|" & artistName
            on error errMsg number errNum
                return "error|" & errNum & "|" & errMsg & "|"
            end try
        end tell
        """,
        [],
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "media.apple_music_status",
            {
                **result,
                "action": "media.apple_music_status",
                "summary": "media.apple_music_status failed",
            },
        )
    parts = str(result.get("stdout") or "").strip().split("|", 4)
    while len(parts) < 5:
        parts.append("")
    status, state, track, artist, extra = parts
    if status == "status":
        return {
            "ok": True,
            "action": "media.apple_music_status",
            "summary": "Read Apple Music playback status",
            "data": {
                "running": state != "not_running",
                "player_state": state,
                "track": track,
                "artist": artist,
            },
            "permission_error": False,
            "fallback_used": False,
        }
    payload = {
        "ok": False,
        "action": "media.apple_music_status",
        "summary": "Could not read Apple Music playback status",
        "error": track or state or extra or "Music did not return playback status",
        "data": {"status": status},
        "permission_error": status == "error" and _looks_like_permission_error(f"{state}\n{track}"),
        "fallback_used": False,
    }
    return _with_permission_metadata("media.apple_music_status", payload)


_APPLE_MUSIC_CONTROL_RECEIPT_SEPARATOR = "\x1f"


def _clean_apple_music_database_id(value: Any) -> str:
    text = str(value or "").strip()
    try:
        database_id = int(text)
    except (TypeError, ValueError):
        return ""
    return str(database_id) if database_id > 0 else ""


def _clean_apple_music_persistent_id(value: Any) -> str:
    text = str(value or "").strip()
    if text.casefold() in {"", "missing value", "none", "null"}:
        return ""
    if not text.strip("0"):
        return ""
    return text


def _apple_music_track_change_receipt(
    *,
    before_database_id: Any,
    after_database_id: Any,
    before_persistent_id: Any,
    after_persistent_id: Any,
) -> dict[str, Any]:
    before_database = _clean_apple_music_database_id(before_database_id)
    after_database = _clean_apple_music_database_id(after_database_id)
    before_persistent = _clean_apple_music_persistent_id(before_persistent_id)
    after_persistent = _clean_apple_music_persistent_id(after_persistent_id)

    identity_source = ""
    before_identity = ""
    after_identity = ""
    identity_conflict = False
    if before_database and after_database:
        identity_source = "music_database_id"
        before_identity = before_database
        after_identity = after_database
        if before_persistent and after_persistent:
            identity_conflict = (
                (before_database != after_database)
                != (before_persistent.casefold() != after_persistent.casefold())
            )
    elif before_persistent and after_persistent:
        identity_source = "music_persistent_id"
        before_identity = before_persistent
        after_identity = after_persistent

    if identity_conflict:
        return {
            "track_changed": False,
            "track_change_verified": False,
            "track_identity_source": "",
            "before_track_id": "",
            "after_track_id": "",
            "track_id": "",
            "track_identity_conflict": True,
        }

    track_changed = bool(
        before_identity
        and after_identity
        and before_identity.casefold() != after_identity.casefold()
    )
    return {
        "track_changed": track_changed,
        "track_change_verified": track_changed,
        "track_identity_source": identity_source,
        "before_track_id": before_identity,
        "after_track_id": after_identity,
        "track_id": after_identity,
    }


def apple_music_control(action: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("media.apple_music_control")
    clean_action = _clean_music_control_action(action)
    result = _run_osascript(
        """
        on currentMusicTrackIdentity()
            tell application "Music"
                set databaseIDText to ""
                set persistentIDText to ""
                try
                    set databaseIDText to (database ID of current track) as text
                end try
                try
                    set persistentIDText to (persistent ID of current track) as text
                end try
                return {databaseIDText, persistentIDText}
            end tell
        end currentMusicTrackIdentity

        on trackIdentityChanged(beforeIdentity, afterIdentity)
            set beforeDatabaseID to (item 1 of beforeIdentity) as text
            set afterDatabaseID to (item 1 of afterIdentity) as text
            if beforeDatabaseID is not "" and afterDatabaseID is not "" then
                return beforeDatabaseID is not afterDatabaseID
            end if
            set beforePersistentID to (item 2 of beforeIdentity) as text
            set afterPersistentID to (item 2 of afterIdentity) as text
            if beforePersistentID is not "" and afterPersistentID is not "" then
                return beforePersistentID is not afterPersistentID
            end if
            return false
        end trackIdentityChanged

        on run argv
            set controlAction to item 1 of argv
            if application "Music" is not running then
                return "not_running|0|Music is not running"
            end if
            tell application "Music"
                try
                    set isTrackNavigation to controlAction is "next" or controlAction is "previous"
                    set beforeIdentity to {"", ""}
                    if isTrackNavigation then
                        set beforeIdentity to my currentMusicTrackIdentity()
                    end if
                    if controlAction is "toggle" then
                        playpause
                    else if controlAction is "play" then
                        play
                    else if controlAction is "pause" then
                        pause
                    else if controlAction is "next" then
                        next track
                    else if controlAction is "previous" then
                        previous track
                    else
                        return "error|-1|unsupported_control"
                    end if
                    set afterIdentity to {"", ""}
                    if isTrackNavigation then
                        repeat with observationAttempt from 1 to 8
                            delay 0.15
                            set afterIdentity to my currentMusicTrackIdentity()
                            if my trackIdentityChanged(beforeIdentity, afterIdentity) then exit repeat
                        end repeat
                    else
                        delay 0.1
                    end if
                    set stateText to player state as text
                    try
                        set trackName to name of current track
                        set artistName to artist of current track
                    on error
                        set trackName to ""
                        set artistName to ""
                    end try
                    if isTrackNavigation then
                        set outputSeparator to character id 31
                        return "controlled-v2" & outputSeparator & controlAction & outputSeparator & stateText & outputSeparator & (item 1 of beforeIdentity) & outputSeparator & (item 1 of afterIdentity) & outputSeparator & (item 2 of beforeIdentity) & outputSeparator & (item 2 of afterIdentity) & outputSeparator & trackName & outputSeparator & artistName
                    end if
                    return "controlled|" & controlAction & "|" & stateText & "|" & trackName & "|" & artistName
                on error errMsg number errNum
                    return "error|" & errNum & "|" & errMsg
                end try
            end tell
        end run
        """,
        [clean_action],
    )
    if not result["ok"]:
        payload = {
            **result,
            "action": "media.apple_music_control",
            "summary": "media.apple_music_control failed",
            "fallback_used": False,
        }
        return _with_permission_metadata("media.apple_music_control", payload)
    raw_stdout = str(result.get("stdout") or "").rstrip("\r\n")
    receipt_prefix = f"controlled-v2{_APPLE_MUSIC_CONTROL_RECEIPT_SEPARATOR}"
    if raw_stdout.startswith(receipt_prefix):
        receipt_parts = raw_stdout.split(_APPLE_MUSIC_CONTROL_RECEIPT_SEPARATOR, 8)
        while len(receipt_parts) < 9:
            receipt_parts.append("")
        (
            _status,
            receipt_action,
            receipt_state,
            before_database_id,
            after_database_id,
            before_persistent_id,
            after_persistent_id,
            receipt_track,
            receipt_artist,
        ) = receipt_parts
        control = receipt_action or clean_action
        data = {
            "control": control,
            "player_state": receipt_state,
            "track": receipt_track,
            "artist": receipt_artist,
        }
        if control in {"next", "previous"}:
            data.update(
                _apple_music_track_change_receipt(
                    before_database_id=before_database_id,
                    after_database_id=after_database_id,
                    before_persistent_id=before_persistent_id,
                    after_persistent_id=after_persistent_id,
                )
            )
        return {
            "ok": True,
            "action": "media.apple_music_control",
            "summary": f"Apple Music {control} executed",
            "data": data,
            "permission_error": False,
            "fallback_used": False,
        }

    parts = raw_stdout.strip().split("|", 4)
    while len(parts) < 5:
        parts.append("")
    status, first, second, third, fourth = parts
    if status == "controlled":
        return {
            "ok": True,
            "action": "media.apple_music_control",
            "summary": f"Apple Music {first} executed",
            "data": {
                "control": first or clean_action,
                "player_state": second,
                "track": third,
                "artist": fourth,
            },
            "permission_error": False,
            "fallback_used": False,
        }
    permission_error = status == "error" and _looks_like_permission_error(f"{first}\n{second}")
    payload = {
        "ok": False,
        "action": "media.apple_music_control",
        "summary": f"Could not control Apple Music with action {clean_action}.",
        "error": second or first or "Music did not accept the control action",
        "data": {"control": clean_action, "status": status},
        "permission_error": permission_error,
        "fallback_used": False,
    }
    return _with_permission_metadata("media.apple_music_control", payload)


def _apple_music_control_media_key_result(
    action: str,
    direct_result: dict[str, Any],
    open_result: dict[str, Any],
) -> dict[str, Any]:
    media_key_fallback = _apple_music_media_key_fallback(action)
    if not media_key_fallback.get("ok"):
        return {}
    warning = _with_permission_metadata(
        "media.apple_music_control",
        {
            "ok": False,
            "action": "media.apple_music_control",
            "summary": "Apple Music automation control failed before media key fallback",
            "error": str(direct_result.get("error") or ""),
            "permission_error": bool(direct_result.get("permission_error")),
        },
    )
    media_key_data = (
        media_key_fallback.get("data") if isinstance(media_key_fallback.get("data"), dict) else {}
    )
    return {
        "ok": True,
        "action": "media.apple_music_control",
        "summary": f"Apple Music {action} attempted via media key fallback",
        "data": {
            "control": action,
            "player_state": "unknown",
            "track": "",
            "artist": "",
            "fallback": "system_media_key",
            "fallback_control": str(media_key_data.get("media_control") or ""),
            "media_key": str(media_key_data.get("media_key") or ""),
            "playback_state_unverified": True,
            "direct_error": str(direct_result.get("error") or ""),
        },
        "permission_error": False,
        "missing_permissions": warning.get("missing_permissions", []),
        "permission_targets": warning.get("permission_targets", []),
        "recovery_hints": warning.get("recovery_hints", []),
        "recovery_actions": warning.get("recovery_actions", []),
        "fallback_used": True,
        "fallback": "system_media_key",
        "fallback_result": {
            "open": open_result,
            "media_key": media_key_fallback,
            "direct": {
                **direct_result,
                "action": "media.apple_music_control",
                "summary": "media.apple_music_control failed",
            },
        },
    }


def _apple_music_media_key_fallback(action: str) -> dict[str, Any]:
    media_key = _APPLE_MUSIC_MEDIA_KEY_FALLBACKS.get(action)
    if not media_key:
        return {
            "ok": False,
            "action": "media.apple_music.media_key",
            "summary": f"No safe media key fallback for Apple Music action {action}",
            "error": "unsupported_media_key_fallback",
            "data": {"requested_control": action},
            "permission_error": False,
            "fallback_used": False,
        }
    key_code, key_label, media_control = media_key
    result = _run_osascript(
        """
        on run argv
            set keyCodeValue to item 1 of argv as integer
            set mediaControl to item 2 of argv
            tell application "System Events" to key code keyCodeValue
            return "pressed|" & mediaControl
        end run
        """,
        [str(key_code), media_control],
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "desktop.safe_key",
            {
                **result,
                "action": "media.apple_music.media_key",
                "summary": "Apple Music media key fallback failed",
                "data": {
                    "requested_control": action,
                    "media_control": media_control,
                    "media_key": key_label,
                    "key_code": key_code,
                },
            },
        )
    return {
        "ok": True,
        "action": "media.apple_music.media_key",
        "summary": f"Pressed {key_label} media key for Apple Music",
        "data": {
            "requested_control": action,
            "media_control": media_control,
            "media_key": key_label,
            "key_code": key_code,
        },
        "permission_error": False,
        "fallback_used": False,
    }


def _system_media_key_press(action_name: str, control: str) -> dict[str, Any]:
    media_key = _APPLE_MUSIC_MEDIA_KEY_FALLBACKS.get(control)
    if not media_key:
        return {
            "ok": False,
            "action": action_name,
            "summary": f"No safe media key mapping for control {control}",
            "error": "unsupported_media_key",
            "data": {"requested_control": control},
            "permission_error": False,
            "fallback_used": False,
        }
    key_code, key_label, media_control = media_key
    result = _run_osascript(
        """
        on run argv
            set keyCodeValue to item 1 of argv as integer
            set mediaControl to item 2 of argv
            tell application "System Events" to key code keyCodeValue
            return "pressed|" & mediaControl
        end run
        """,
        [str(key_code), media_control],
    )
    if not result["ok"]:
        return _with_permission_metadata(
            action_name,
            {
                **result,
                "action": action_name,
                "summary": f"{action_name} media key failed",
                "data": {
                    "requested_control": control,
                    "media_control": media_control,
                    "media_key": key_label,
                    "key_code": key_code,
                },
            },
        )
    return {
        "ok": True,
        "action": action_name,
        "summary": f"Pressed {key_label} media key",
        "data": {
            "requested_control": control,
            "media_control": media_control,
            "media_key": key_label,
            "key_code": key_code,
        },
        "permission_error": False,
        "fallback_used": False,
    }


def system_media_control(action: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("media.system_control")
    clean_action = _clean_music_control_action(action)
    media_key_control = "toggle" if clean_action in {"play", "pause"} else clean_action
    media_key_result = _system_media_key_press("media.system_control", media_key_control)
    media_key_data = (
        media_key_result.get("data") if isinstance(media_key_result.get("data"), dict) else {}
    )
    data = {
        "control": clean_action,
        "media_key_control": media_key_control,
        "player_state": "unknown",
        "playback_state_unverified": True,
    }
    if media_key_data.get("media_key"):
        data["media_key"] = media_key_data.get("media_key") or ""
    if media_key_data.get("media_control"):
        data["fallback_control"] = media_key_data.get("media_control") or ""
    if media_key_result.get("ok"):
        return {
            "ok": True,
            "action": "media.system_control",
            "summary": f"Sent {clean_action} media key control to current media",
            "data": data,
            "permission_error": False,
            "fallback_used": True,
            "fallback": "system_media_key",
            "fallback_result": {"media_key": media_key_result},
        }

    payload = {
        "ok": False,
        "action": "media.system_control",
        "summary": f"Could not send {clean_action} media key control to current media",
        "error": str(media_key_result.get("error") or "system media control failed"),
        "data": data,
        "permission_error": bool(media_key_result.get("permission_error")),
        "fallback_used": False,
        "fallback_result": {"media_key": media_key_result},
    }
    return _with_permission_metadata("media.system_control", payload)


def apple_music_open_and_play() -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("media.apple_music_open_and_play")
    open_result = app_open("Music")
    control_result = apple_music_control("play")
    control_data = control_result.get("data") if isinstance(control_result.get("data"), dict) else {}
    data = {
        "app_name": "Music",
        "open_ok": bool(open_result.get("ok")),
        "open_summary": str(open_result.get("summary") or ""),
        "playback_ok": bool(control_result.get("ok")),
        "control": control_data.get("control") or "play",
        "player_state": control_data.get("player_state") or "",
        "track": control_data.get("track") or "",
        "artist": control_data.get("artist") or "",
    }
    if control_data.get("fallback") or control_result.get("fallback"):
        data["fallback"] = control_data.get("fallback") or control_result.get("fallback") or ""
    if control_data.get("fallback_control"):
        data["fallback_control"] = control_data.get("fallback_control") or ""
    if control_data.get("media_key"):
        data["media_key"] = control_data.get("media_key") or ""
    if control_data.get("playback_state_unverified"):
        data["playback_state_unverified"] = True
    if control_result.get("ok"):
        payload = {
            "ok": True,
            "action": "media.apple_music_open_and_play",
            "summary": (
                "Opened Music and attempted playback with media key fallback"
                if data.get("playback_state_unverified")
                else "Opened Music and started playback"
            ),
            "data": data,
            "permission_error": False,
            "fallback_used": bool(control_result.get("fallback_used")),
        }
        for key in ("missing_permissions", "permission_targets", "recovery_hints", "recovery_actions"):
            if control_result.get(key):
                payload[key] = control_result[key]
        if control_result.get("fallback"):
            payload["fallback"] = control_result["fallback"]
        if control_result.get("fallback_result"):
            payload["fallback_result"] = control_result["fallback_result"]
        return payload

    payload = {
        "ok": False,
        "action": "media.apple_music_open_and_play",
        "summary": (
            "Opened Music but could not start playback"
            if open_result.get("ok")
            else "Could not open Music or start playback"
        ),
        "error": str(control_result.get("error") or open_result.get("error") or "Music playback failed"),
        "data": data,
        "permission_error": bool(control_result.get("permission_error") or open_result.get("permission_error")),
        "fallback_used": bool(open_result.get("ok") or control_result.get("fallback_used")),
        "fallback_result": {
            "open": open_result,
            "control": control_result,
        },
    }
    return _with_permission_metadata("media.apple_music_open_and_play", payload)


def music_app_open_and_play(app_name: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("media.music_app_open_and_play")
    clean_name = _clean_required(app_name, "app_name")
    if clean_name == "Music":
        return apple_music_open_and_play()

    open_result = app_open(clean_name)
    focus_result: dict[str, Any] = {}
    media_key_result: dict[str, Any] = {}
    if open_result.get("ok"):
        focus_result = app_focus(clean_name)
        media_key_result = _system_media_key_press(
            "media.music_app_open_and_play",
            "play",
        )
    open_ok = bool(open_result.get("ok"))
    key_ok = bool(media_key_result.get("ok"))
    data = {
        "app_name": clean_name,
        "open_ok": open_ok,
        "open_summary": str(open_result.get("summary") or ""),
        "focus_ok": bool(focus_result.get("ok")),
        "focus_summary": str(focus_result.get("summary") or ""),
        "playback_ok": key_ok,
        "control": "play",
        "player_state": "unknown",
        "playback_state_unverified": True,
    }
    media_key_data = (
        media_key_result.get("data") if isinstance(media_key_result.get("data"), dict) else {}
    )
    if media_key_data.get("media_key"):
        data["media_key"] = media_key_data.get("media_key") or ""
    if media_key_data.get("media_control"):
        data["fallback_control"] = media_key_data.get("media_control") or ""

    if open_ok and key_ok:
        return {
            "ok": True,
            "action": "media.music_app_open_and_play",
            "summary": f"Opened {clean_name} and attempted playback with media key",
            "data": data,
            "permission_error": False,
            "fallback_used": True,
            "fallback": "system_media_key",
            "fallback_result": {
                "open": open_result,
                "focus": focus_result,
                "media_key": media_key_result,
            },
        }

    payload = {
        "ok": False,
        "action": "media.music_app_open_and_play",
        "summary": (
            f"Opened {clean_name} but could not start playback"
            if open_ok
            else f"Could not open {clean_name} or start playback"
        ),
        "error": str(
            media_key_result.get("error")
            or focus_result.get("error")
            or open_result.get("error")
            or "music app playback failed"
        ),
        "data": data,
        "permission_error": bool(
            media_key_result.get("permission_error")
            or focus_result.get("permission_error")
            or open_result.get("permission_error")
        ),
        "fallback_used": open_ok,
        "fallback_result": {
            "open": open_result,
            "focus": focus_result,
            "media_key": media_key_result,
        },
    }
    return _with_permission_metadata("media.music_app_open_and_play", payload)


def music_app_control(app_name: str, action: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("media.music_app_control")
    clean_name = _clean_required(app_name, "app_name")
    clean_action = _clean_music_control_action(action)
    if clean_name == "Music":
        return apple_music_control(clean_action)

    status_result = app_status(clean_name)
    status_data = status_result.get("data") if isinstance(status_result.get("data"), dict) else {}
    if status_result.get("ok") and status_data.get("running") is False:
        return {
            "ok": False,
            "action": "media.music_app_control",
            "summary": f"{clean_name} is not running",
            "error": "music_app_not_running",
            "data": {
                "app_name": clean_name,
                "control": clean_action,
                "running": False,
                "playback_state_unverified": True,
            },
            "permission_error": False,
            "fallback_used": False,
            "fallback_result": {"status": status_result},
        }
    if not status_result.get("ok"):
        return _with_permission_metadata(
            "media.music_app_control",
            {
                **status_result,
                "action": "media.music_app_control",
                "summary": f"Could not check {clean_name} before media control",
                "data": {
                    "app_name": clean_name,
                    "control": clean_action,
                    "status": status_data,
                },
            },
        )

    focus_result = app_focus(clean_name)
    if not focus_result.get("ok"):
        return _with_permission_metadata(
            "media.music_app_control",
            {
                **focus_result,
                "action": "media.music_app_control",
                "summary": f"Could not focus {clean_name} before media control",
                "data": {
                    "app_name": clean_name,
                    "control": clean_action,
                    "focus_ok": False,
                },
                "fallback_result": {"status": status_result, "focus": focus_result},
            },
        )

    media_key_control = "toggle" if clean_action in {"play", "pause"} else clean_action
    media_key_result = _system_media_key_press("media.music_app_control", media_key_control)
    media_key_data = (
        media_key_result.get("data") if isinstance(media_key_result.get("data"), dict) else {}
    )
    data = {
        "app_name": clean_name,
        "control": clean_action,
        "media_key_control": media_key_control,
        "focus_ok": True,
        "player_state": "unknown",
        "playback_state_unverified": True,
    }
    if media_key_data.get("media_key"):
        data["media_key"] = media_key_data.get("media_key") or ""
    if media_key_data.get("media_control"):
        data["fallback_control"] = media_key_data.get("media_control") or ""
    if media_key_result.get("ok"):
        return {
            "ok": True,
            "action": "media.music_app_control",
            "summary": f"Sent {clean_action} media key control to {clean_name}",
            "data": data,
            "permission_error": False,
            "fallback_used": True,
            "fallback": "system_media_key",
            "fallback_result": {
                "status": status_result,
                "focus": focus_result,
                "media_key": media_key_result,
            },
        }

    payload = {
        "ok": False,
        "action": "media.music_app_control",
        "summary": f"Could not send {clean_action} media key control to {clean_name}",
        "error": str(media_key_result.get("error") or "music app media control failed"),
        "data": data,
        "permission_error": bool(media_key_result.get("permission_error")),
        "fallback_used": bool(focus_result.get("ok")),
        "fallback_result": {
            "status": status_result,
            "focus": focus_result,
            "media_key": media_key_result,
        },
    }
    return _with_permission_metadata("media.music_app_control", payload)


def _apple_music_search_result_evidence(
    query: str,
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Read a bounded, Music-specific search-results subtree.

    Music exposes the search page as the standard window's direct split group,
    whose main scroll area contains one direct AXList.  Result sections are
    direct AXList children and their result rows are direct AXCell children.
    Keeping those levels and counts fixed avoids an expensive/brittle traversal
    of the app's complete accessibility tree.
    """

    clean_query = _clean_required(query, "query")
    result = _call_with_bounded_timeout(
        _run_osascript,
        """
        tell application "System Events"
            if not (exists process "Music") then return "no_music_process"
            tell process "Music"
                set standardWindows to (every window whose subrole is "AXStandardWindow")
                if (count of standardWindows) is 0 then return "no_standard_window"
                set targetWindow to item 1 of standardWindows

                -- A matching result row is only conclusive on its own when
                -- Music's visible search control still contains this query.
                -- Keep the lookup shallow and capped: window -> toolbar/group
                -- -> direct field (or one direct group -> field).
                set queryIdentityText to ""
                set queryIdentitySource to ""
                set queryIdentityRole to ""
                set queryIdentityDescription to ""
                set toolbarTotal to count of UI elements of targetWindow
                if toolbarTotal is greater than 0 then
                    repeat with toolbarIndex from 1 to toolbarTotal
                        if toolbarIndex is greater than 12 then exit repeat
                        set toolbarItem to UI element toolbarIndex of targetWindow
                        set toolbarRole to ""
                        try
                            set toolbarRole to role of toolbarItem as text
                        end try
                        if toolbarRole is "AXToolbar" or toolbarRole is "AXGroup" then
                            set toolbarChildTotal to count of UI elements of toolbarItem
                            if toolbarChildTotal is greater than 0 then
                                repeat with toolbarChildIndex from 1 to toolbarChildTotal
                                    if toolbarChildIndex is greater than 24 then exit repeat
                                    set toolbarChild to UI element toolbarChildIndex of toolbarItem
                                    set toolbarChildRole to ""
                                    try
                                        set toolbarChildRole to role of toolbarChild as text
                                    end try
                                    if toolbarChildRole is "AXSearchField" or toolbarChildRole is "AXTextField" then
                                        try
                                            set queryIdentityText to value of toolbarChild as text
                                        end try
                                        if queryIdentityText is not "" then
                                            set queryIdentitySource to "search_field"
                                            set queryIdentityRole to toolbarChildRole
                                            try
                                                set queryIdentityDescription to description of toolbarChild as text
                                            end try
                                            exit repeat
                                        end if
                                    else if toolbarChildRole is "AXGroup" then
                                        set searchContainerTotal to count of UI elements of toolbarChild
                                        if searchContainerTotal is greater than 0 then
                                            repeat with searchContainerChildIndex from 1 to searchContainerTotal
                                                if searchContainerChildIndex is greater than 16 then exit repeat
                                                set searchContainerChild to UI element searchContainerChildIndex of toolbarChild
                                                set searchContainerChildRole to ""
                                                try
                                                    set searchContainerChildRole to role of searchContainerChild as text
                                                end try
                                                if searchContainerChildRole is "AXSearchField" or searchContainerChildRole is "AXTextField" then
                                                    try
                                                        set queryIdentityText to value of searchContainerChild as text
                                                    end try
                                                    if queryIdentityText is not "" then
                                                        set queryIdentitySource to "search_field"
                                                        set queryIdentityRole to searchContainerChildRole
                                                        try
                                                            set queryIdentityDescription to description of searchContainerChild as text
                                                        end try
                                                        exit repeat
                                                    end if
                                                end if
                                            end repeat
                                        end if
                                    end if
                                    if queryIdentityText is not "" then exit repeat
                                end repeat
                            end if
                        end if
                        if queryIdentityText is not "" then exit repeat
                    end repeat
                end if

                set identityText to ""
                if queryIdentityText is not "" then
                    set identityText to "identity" & tab & queryIdentitySource & tab & queryIdentityText & linefeed
                    set identityText to identityText & "identity_role" & tab & queryIdentityRole & linefeed
                    if queryIdentityDescription is not "" then
                        set identityText to identityText & "identity_description" & tab & queryIdentityDescription & linefeed
                    end if
                end if

                set targetSplitGroup to missing value
                set windowChildTotal to count of UI elements of targetWindow
                if windowChildTotal is greater than 0 then
                    repeat with windowChildIndex from 1 to windowChildTotal
                        if windowChildIndex is greater than 12 then exit repeat
                        set windowChild to UI element windowChildIndex of targetWindow
                        try
                            if (role of windowChild as text) is "AXSplitGroup" then
                                set targetSplitGroup to windowChild
                                exit repeat
                            end if
                        end try
                    end repeat
                end if
                if targetSplitGroup is missing value then return "no_split_group"

                set markerText to ""
                set markerCount to 0
                set scrollAreaTotal to count of UI elements of targetSplitGroup
                if scrollAreaTotal is greater than 0 then
                    repeat with scrollAreaIndex from 1 to scrollAreaTotal
                        if scrollAreaIndex is greater than 8 then exit repeat
                        set scrollArea to UI element scrollAreaIndex of targetSplitGroup
                        try
                            if (role of scrollArea as text) is "AXScrollArea" then
                                set listTotal to count of UI elements of scrollArea
                                if listTotal is greater than 0 then
                                    repeat with listIndex from 1 to listTotal
                                        if listIndex is greater than 12 then exit repeat
                                        set resultList to UI element listIndex of scrollArea
                                        try
                                            if (role of resultList as text) is "AXList" then
                                                set sectionTotal to count of UI elements of resultList
                                                if sectionTotal is greater than 0 then
                                                    repeat with sectionIndex from 1 to sectionTotal
                                                        if sectionIndex is greater than 12 then exit repeat
                                                        set sectionItem to UI element sectionIndex of resultList
                                                        try
                                                            if (role of sectionItem as text) is "AXList" then
                                                                set sectionDescription to description of sectionItem as text
                                                                if sectionDescription is not "" then
                                                                    set markerText to markerText & "marker" & tab & sectionDescription & linefeed
                                                                    set markerCount to markerCount + 1
                                                                    if markerCount is greater than or equal to 64 then return "results" & linefeed & identityText & markerText
                                                                end if
                                                                set cellTotal to count of UI elements of sectionItem
                                                                if cellTotal is greater than 0 then
                                                                    repeat with cellIndex from 1 to cellTotal
                                                                        if cellIndex is greater than 20 then exit repeat
                                                                        set cellItem to UI element cellIndex of sectionItem
                                                                        try
                                                                            if (role of cellItem as text) is "AXCell" then
                                                                                set cellDescription to description of cellItem as text
                                                                                if cellDescription is not "" then
                                                                                    set markerText to markerText & "marker" & tab & cellDescription & linefeed
                                                                                    set markerCount to markerCount + 1
                                                                                    if markerCount is greater than or equal to 64 then return "results" & linefeed & identityText & markerText
                                                                                end if
                                                                            end if
                                                                        end try
                                                                    end repeat
                                                                end if
                                                            end if
                                                        end try
                                                    end repeat
                                                end if
                                            end if
                                        end try
                                    end repeat
                                end if
                            end if
                        end try
                    end repeat
                end if
                if markerCount is 0 then return "no_result_marker"
                return "results" & linefeed & identityText & markerText
            end tell
        end tell
        """,
        timeout_seconds=timeout_seconds,
    )
    if not result.get("ok"):
        return _with_permission_metadata(
            "desktop.ui_elements",
            {
                **result,
                "action": "media.apple_music.search.result_evidence",
                "summary": "Could not inspect Apple Music search results",
                "data": {
                    "query": clean_query,
                    "result_marker": False,
                    "query_match": False,
                    "normalized_query_match": "",
                    "fingerprint": "",
                    "search_query_identity_verified": False,
                    "search_query_identity_source": "",
                    "search_query_identity_role": "",
                    "search_query_identity_description": "",
                },
            },
        )

    lines = [line.strip() for line in str(result.get("stdout") or "").splitlines()]
    identity_source = ""
    identity_value = ""
    identity_role = ""
    identity_description = ""
    marker_lines: list[str] = []
    if lines[:1] == ["results"]:
        for line in lines[1:]:
            if not line:
                continue
            if line.startswith("identity\t"):
                _, identity_source, identity_value = (line.split("\t", 2) + ["", ""])[
                    :3
                ]
                continue
            if line.startswith("identity_role\t"):
                identity_role = line.split("\t", 1)[1]
                continue
            if line.startswith("identity_description\t"):
                identity_description = line.split("\t", 1)[1]
                continue
            marker_lines.append(line.removeprefix("marker\t"))
    normalized_query = re.sub(r"[\W_]+", "", clean_query.casefold())
    normalized_identity = re.sub(r"[\W_]+", "", identity_value.casefold())
    search_query_identity_verified = bool(
        identity_source == "search_field"
        and len(normalized_query) >= 2
        and normalized_identity == normalized_query
    )
    normalized_markers = [
        re.sub(r"[\W_]+", "", marker.casefold()) for marker in marker_lines
    ]
    query_match = bool(
        len(normalized_query) >= 2
        and any(normalized_query in marker for marker in normalized_markers)
    )
    fingerprint = (
        hashlib.sha256("\n".join(normalized_markers).encode("utf-8")).hexdigest()
        if marker_lines
        else ""
    )
    return {
        "ok": bool(marker_lines),
        "action": "media.apple_music.search.result_evidence",
        "summary": (
            "Observed Apple Music search results"
            if marker_lines
            else "Apple Music search results were not observable"
        ),
        **({} if marker_lines else {"error": lines[0] if lines else "no_result_marker"}),
        "data": {
            "query": clean_query,
            "result_marker": bool(marker_lines),
            "query_match": query_match,
            "normalized_query_match": normalized_query if query_match else "",
            "search_query_identity_verified": search_query_identity_verified,
            "search_query_identity_source": identity_source,
            "search_query_identity_value": identity_value[:160],
            "search_query_identity_role": identity_role,
            "search_query_identity_description": identity_description[:160],
            "fingerprint": fingerprint,
            "marker_count": len(marker_lines),
            "marker_samples": [marker[:160] for marker in marker_lines[:3]],
        },
        "permission_error": False,
        "fallback_used": False,
    }


def _activate_apple_music_after_search_dispatch(
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Request one explicit Music activation without sending keyboard input."""

    result = _call_with_bounded_timeout(
        _run_osascript,
        """
        tell application "Music" to activate
        tell application "System Events"
            if exists process "Music" then
                set visible of process "Music" to true
                set frontmost of process "Music" to true
            end if
        end tell
        return "activated"
        """,
        timeout_seconds=timeout_seconds,
    )
    if not result.get("ok"):
        return _with_permission_metadata(
            "app.focus",
            {
                **result,
                "action": "app.focus",
                "summary": "Could not request Apple Music foreground activation",
                "data": {
                    "app_name": "Music",
                    "focus_requested": False,
                },
            },
        )
    return {
        "ok": True,
        "action": "app.focus",
        "summary": "Requested Apple Music foreground activation",
        "data": {
            "app_name": "Music",
            "focus_requested": True,
        },
        "permission_error": False,
        "fallback_used": False,
    }


def _focus_apple_music_after_search_dispatch(
    *,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    """Use the packaged native bridge when present, otherwise one AX activation."""

    if _electron_native_bridge_config() is None:
        result = _call_with_bounded_timeout(
            _activate_apple_music_after_search_dispatch,
            timeout_seconds=timeout_seconds,
        )
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return {
            **result,
            "data": {
                **data,
                "focus_strategy": "system_events_activate",
            },
        }

    native_result = _call_with_bounded_timeout(
        _electron_native_focus_app,
        "Music",
        timeout_seconds=timeout_seconds,
    )
    native_data = (
        native_result.get("data")
        if isinstance(native_result.get("data"), dict)
        else {}
    )
    focus_verified = bool(
        native_result.get("ok") is True
        and native_data.get("focus_verified") is True
    )
    return {
        **native_result,
        "ok": focus_verified,
        "action": "app.focus",
        "summary": (
            "Focused Apple Music via Electron native bridge"
            if focus_verified
            else "Could not focus Apple Music via Electron native bridge"
        ),
        **(
            {}
            if focus_verified
            else {
                "error": str(
                    native_result.get("error")
                    or "electron_native_focus_unverified"
                ),
                "blocking_condition": "foreground_focus_unverified",
            }
        ),
        "data": {
            **native_data,
            "app_name": "Music",
            "focus_strategy": "electron_native_bridge",
        },
    }


def _open_apple_music_search(
    query: str,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    clean_query = _clean_required(query, "query")
    deadline = (
        float(deadline)
        if deadline is not None
        else time.monotonic() + _APPLE_MUSIC_PLAY_DEADLINE_SECONDS
    )
    search_url = f"https://music.apple.com/search?term={quote_plus(clean_query)}"
    command = ["open", "-a", "Music", search_url]
    baseline_evidence: dict[str, Any] = {}
    foreground_observations: list[dict[str, Any]] = []
    evidence_observations: list[dict[str, Any]] = []
    focus_result: dict[str, Any] = {}
    dispatch_receipt: dict[str, Any] = {"command": command}
    dispatch_verified = False
    foreground_verified = False

    def deadline_result() -> dict[str, Any]:
        partial_evidence_available = bool(
            baseline_evidence
            or foreground_observations
            or evidence_observations
            or focus_result
        )
        return {
            "ok": False,
            "action": "media.apple_music.search",
            "summary": (
                "Apple Music search stopped after reaching its execution deadline"
            ),
            "error": "apple_music_search_deadline_exceeded",
            "blocking_condition": "search_deadline_exceeded",
            "data": {
                "query": clean_query,
                "url": search_url,
                "target_app": "Music",
                "open_target": "apple_music_search",
                "search_opened": False,
                "dispatch_verified": dispatch_verified,
                "foreground_verified": foreground_verified,
                "search_query_verified": False,
                "search_query_identity_verified": False,
                "search_result_changed_from_nonmatching_baseline": False,
                "deadline_exceeded": True,
                "partial_evidence_available": partial_evidence_available,
                "poll_attempts": len(foreground_observations),
            },
            "permission_error": False,
            "fallback_used": dispatch_verified,
            "fallback_result": {
                "dispatch": dispatch_receipt,
                "focus": focus_result,
                "foreground_observations": foreground_observations,
                "baseline_evidence": baseline_evidence,
                "result_evidence_observations": evidence_observations,
            },
        }

    baseline_timeout = _remaining_timeout(deadline, 10.0)
    if baseline_timeout <= 0:
        return deadline_result()
    baseline_evidence = _call_with_bounded_timeout(
        _apple_music_search_result_evidence,
        clean_query,
        timeout_seconds=baseline_timeout,
    )
    if time.monotonic() >= deadline:
        return deadline_result()
    dispatch_timeout = _remaining_timeout(deadline, 10.0)
    if dispatch_timeout <= 0:
        return deadline_result()
    try:
        dispatch = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=dispatch_timeout,
            check=False,
        )
    except Exception as exc:
        if time.monotonic() >= deadline:
            return deadline_result()
        payload = _error("media.apple_music.search", exc)
        payload["data"] = {
            "query": clean_query,
            "url": search_url,
            "target_app": "Music",
            "open_target": "apple_music_search",
            "search_opened": False,
            "dispatch_verified": False,
            "foreground_verified": False,
            "search_query_verified": False,
            "search_query_identity_verified": False,
            "search_result_changed_from_nonmatching_baseline": False,
        }
        payload["fallback_result"] = {"baseline_evidence": baseline_evidence}
        return payload
    dispatch_receipt = {
        "command": command,
        "returncode": dispatch.returncode,
    }
    dispatch_verified = dispatch.returncode == 0
    if time.monotonic() >= deadline:
        return deadline_result()
    if dispatch.returncode != 0:
        payload = _failed("media.apple_music.search", dispatch)
        payload["data"] = {
            "query": clean_query,
            "url": search_url,
            "target_app": "Music",
            "open_target": "apple_music_search",
            "search_opened": False,
            "dispatch_verified": False,
            "foreground_verified": False,
            "search_query_verified": False,
            "search_query_identity_verified": False,
            "search_result_changed_from_nonmatching_baseline": False,
        }
        payload["fallback_result"] = {"baseline_evidence": baseline_evidence}
        return payload

    # Universal-link dispatch is asynchronous.  Let LaunchServices hand the
    # URL to Music, then explicitly restore Music once.  The host app may take
    # foreground while it renders runtime updates, so the URL's process exit
    # code alone is not useful evidence.  This does not send keyboard input.
    launch_settle_seconds = _remaining_timeout(deadline, 1.0)
    if launch_settle_seconds <= 0:
        return deadline_result()
    time.sleep(launch_settle_seconds)
    if time.monotonic() >= deadline:
        return deadline_result()
    observation_timeout = _remaining_timeout(deadline, 10.0)
    if observation_timeout <= 0:
        return deadline_result()
    initial_observation = _call_with_bounded_timeout(
        active_window,
        timeout_seconds=observation_timeout,
    )
    foreground_observations.append(initial_observation)
    if time.monotonic() >= deadline:
        return deadline_result()
    initial_data = (
        initial_observation.get("data")
        if isinstance(initial_observation.get("data"), dict)
        else {}
    )
    initial_app_name = str(initial_data.get("app_name") or "").strip()
    foreground_verified = bool(
        initial_observation.get("ok") is True
        and (
            _compact_app_match_name(initial_app_name) in {"music", "applemusic"}
            or _app_name_matches_expected("Music", initial_app_name)
        )
    )
    # In the packaged app, always cross the Electron bridge once so the
    # backend receipt proves that the host-to-native focus path is alive.  A
    # source/dev runtime has no bridge and can keep the already-frontmost fast
    # path without a redundant System Events activation.
    packaged_native_focus_available = _electron_native_bridge_config() is not None
    if foreground_verified and not packaged_native_focus_available:
        focus_result = {
            "ok": True,
            "action": "app.focus",
            "summary": "Apple Music was already foreground",
            "data": {
                "app_name": "Music",
                "focus_verified": True,
                "focus_strategy": "already_frontmost",
            },
            "permission_error": False,
            "fallback_used": False,
        }
    else:
        focus_timeout = _remaining_timeout(deadline, 8.0)
        if focus_timeout <= 0:
            return deadline_result()
        focus_result = _call_with_bounded_timeout(
            _focus_apple_music_after_search_dispatch,
            timeout_seconds=focus_timeout,
        )
        if time.monotonic() >= deadline:
            return deadline_result()
    focus_data = (
        focus_result.get("data")
        if isinstance(focus_result.get("data"), dict)
        else {}
    )
    if focus_result.get("ok") is not True:
        payload = {
            "ok": False,
            "action": "media.apple_music.search",
            "summary": "Apple Music search was dispatched, but Music could not be focused",
            "error": str(focus_result.get("error") or "apple_music_search_focus_unverified"),
            "blocking_condition": "foreground_focus_unverified",
            "data": {
                "query": clean_query,
                "url": search_url,
                "target_app": "Music",
                "open_target": "apple_music_search",
                "search_opened": False,
                "dispatch_verified": True,
                "foreground_verified": False,
                "search_query_verified": False,
                "search_query_identity_verified": False,
                "search_result_changed_from_nonmatching_baseline": False,
                "observed_app": str(
                    focus_data.get("frontmost_app")
                    or initial_data.get("app_name")
                    or ""
                ),
                "poll_attempts": len(foreground_observations),
            },
            "permission_error": bool(focus_result.get("permission_error")),
            "recommended_tools": ["app.focus", "desktop.active_window"],
            "fallback_used": True,
            "fallback_result": {
                "dispatch": {
                    "command": command,
                    "returncode": dispatch.returncode,
                },
                "focus": focus_result,
                "foreground_observations": foreground_observations,
                "baseline_evidence": baseline_evidence,
            },
        }
        for key in (
            "missing_permissions",
            "permission_targets",
            "recovery_hints",
            "recovery_actions",
        ):
            if focus_result.get(key):
                payload[key] = focus_result[key]
        return _with_permission_metadata("app.focus", payload)

    if not foreground_verified:
        for attempt in range(7):
            if attempt:
                poll_sleep_seconds = _remaining_timeout(deadline, 0.2)
                if poll_sleep_seconds <= 0:
                    return deadline_result()
                time.sleep(poll_sleep_seconds)
                if time.monotonic() >= deadline:
                    return deadline_result()
            observation_timeout = _remaining_timeout(deadline, 10.0)
            if observation_timeout <= 0:
                return deadline_result()
            observation = _call_with_bounded_timeout(
                active_window,
                timeout_seconds=observation_timeout,
            )
            foreground_observations.append(observation)
            if time.monotonic() >= deadline:
                return deadline_result()
            data = (
                observation.get("data")
                if isinstance(observation.get("data"), dict)
                else {}
            )
            app_name = str(data.get("app_name") or "").strip()
            if observation.get("ok") is True and (
                _compact_app_match_name(app_name) in {"music", "applemusic"}
                or _app_name_matches_expected("Music", app_name)
            ):
                foreground_verified = True
                break
    if not foreground_verified:
        last_observation = foreground_observations[-1] if foreground_observations else {}
        last_data = (
            last_observation.get("data")
            if isinstance(last_observation.get("data"), dict)
            else {}
        )
        return {
            "ok": False,
            "action": "media.apple_music.search",
            "summary": (
                "Apple Music search URL was dispatched, but Music did not "
                "reach the foreground"
            ),
            "error": str(
                last_observation.get("error")
                or "apple_music_search_foreground_unverified"
            ),
            "blocking_condition": "foreground_focus_unverified",
            "data": {
                "query": clean_query,
                "url": search_url,
                "target_app": "Music",
                "open_target": "apple_music_search",
                "search_opened": False,
                "dispatch_verified": True,
                "foreground_verified": False,
                "search_query_verified": False,
                "search_query_identity_verified": False,
                "search_result_changed_from_nonmatching_baseline": False,
                "observed_app": str(last_data.get("app_name") or ""),
                "poll_attempts": len(foreground_observations),
            },
            "permission_error": bool(last_observation.get("permission_error")),
            "recommended_tools": ["app.focus", "desktop.active_window"],
            "recovery_actions": [
                {
                    "label": "重新切到 Apple Music",
                    "tool": "app.focus",
                    "input": {"app_name": "Music"},
                    "permission_target": "foreground_focus",
                    "risk_level": "low",
                }
            ],
            "fallback_used": True,
            "fallback_result": {
                "dispatch": {
                    "command": command,
                    "returncode": dispatch.returncode,
                },
                "focus": focus_result,
                "foreground_observations": foreground_observations,
                "baseline_evidence": baseline_evidence,
            },
        }

    baseline_data = (
        baseline_evidence.get("data")
        if isinstance(baseline_evidence.get("data"), dict)
        else {}
    )
    baseline_result_marker = bool(
        baseline_evidence.get("ok") is True
        and baseline_data.get("result_marker") is True
    )
    baseline_query_match = bool(
        baseline_result_marker and baseline_data.get("query_match") is True
    )
    baseline_fingerprint = str(baseline_data.get("fingerprint") or "")

    verified_evidence: dict[str, Any] | None = None
    verified_by_identity = False
    verified_by_nonmatching_baseline_change = False
    last_identity_verified = False
    last_changed_from_nonmatching_baseline = False
    for attempt in range(8):
        if attempt:
            evidence_sleep_seconds = _remaining_timeout(deadline, 0.25)
            if evidence_sleep_seconds <= 0:
                return deadline_result()
            time.sleep(evidence_sleep_seconds)
            if time.monotonic() >= deadline:
                return deadline_result()
        evidence_timeout = _remaining_timeout(deadline, 10.0)
        if evidence_timeout <= 0:
            return deadline_result()
        evidence = _call_with_bounded_timeout(
            _apple_music_search_result_evidence,
            clean_query,
            timeout_seconds=evidence_timeout,
        )
        evidence_observations.append(evidence)
        if time.monotonic() >= deadline:
            return deadline_result()
        evidence_data = (
            evidence.get("data")
            if isinstance(evidence.get("data"), dict)
            else {}
        )
        result_marker = bool(
            evidence.get("ok") is True
            and evidence_data.get("result_marker") is True
        )
        current_fingerprint = str(evidence_data.get("fingerprint") or "")
        last_identity_verified = bool(
            evidence_data.get("search_query_identity_verified") is True
        )
        last_changed_from_nonmatching_baseline = bool(
            baseline_result_marker
            and not baseline_query_match
            and baseline_fingerprint
            and current_fingerprint
            and current_fingerprint != baseline_fingerprint
        )
        if (
            result_marker
            and evidence_data.get("query_match") is True
            and (
                last_identity_verified
                or last_changed_from_nonmatching_baseline
            )
        ):
            verified_evidence = evidence
            verified_by_identity = last_identity_verified
            verified_by_nonmatching_baseline_change = (
                last_changed_from_nonmatching_baseline
            )
            break

    if verified_evidence is None:
        last_evidence = evidence_observations[-1] if evidence_observations else {}
        payload = {
            "ok": False,
            "action": "media.apple_music.search",
            "summary": (
                "Apple Music opened, but the requested search results were not verified"
            ),
            "error": str(
                last_evidence.get("error") or "apple_music_search_results_unverified"
            ),
            "blocking_condition": "search_ui_evidence_unverified",
            "data": {
                "query": clean_query,
                "url": search_url,
                "target_app": "Music",
                "open_target": "apple_music_search",
                "search_opened": False,
                "dispatch_verified": True,
                "foreground_verified": True,
                "search_query_verified": False,
                "search_query_identity_verified": last_identity_verified,
                "search_result_changed_from_nonmatching_baseline": (
                    last_changed_from_nonmatching_baseline
                ),
                "poll_attempts": len(foreground_observations),
            },
            "permission_error": bool(last_evidence.get("permission_error")),
            "fallback_used": True,
            "fallback_result": {
                "dispatch": {
                    "command": command,
                    "returncode": dispatch.returncode,
                },
                "focus": focus_result,
                "foreground_observations": foreground_observations,
                "baseline_evidence": baseline_evidence,
                "result_evidence_observations": evidence_observations,
            },
        }
        return _with_permission_metadata("desktop.ui_elements", payload)

    verified_data = (
        verified_evidence.get("data")
        if isinstance(verified_evidence.get("data"), dict)
        else {}
    )
    # Result evidence can become stale if the host regains focus while the
    # agent is rendering progress.  Treat foreground as a final postcondition,
    # not a one-time precondition before inspecting the Music UI.
    final_observation_timeout = _remaining_timeout(deadline, 10.0)
    if final_observation_timeout <= 0:
        return deadline_result()
    final_foreground_observation = _call_with_bounded_timeout(
        active_window,
        timeout_seconds=final_observation_timeout,
    )
    if time.monotonic() >= deadline:
        return deadline_result()
    final_foreground_data = (
        final_foreground_observation.get("data")
        if isinstance(final_foreground_observation.get("data"), dict)
        else {}
    )
    final_app_name = str(final_foreground_data.get("app_name") or "").strip()
    final_foreground_verified = bool(
        final_foreground_observation.get("ok") is True
        and (
            _compact_app_match_name(final_app_name) in {"music", "applemusic"}
            or _app_name_matches_expected("Music", final_app_name)
        )
    )
    if (
        not final_foreground_verified
        and final_foreground_observation.get("ok") is True
        and bool(final_app_name)
    ):
        # The search postcondition is already verified.  A later foreground
        # change can be deliberate user input, so yield instead of fighting
        # for focus or turning a completed search into a runtime failure.
        return {
            "ok": True,
            "action": "media.apple_music.search",
            "summary": (
                f"Opened Apple Music search for {clean_query}; foreground focus moved"
            ),
            "data": {
                "query": clean_query,
                "url": search_url,
                "target_app": "Music",
                "open_target": "apple_music_search",
                "search_opened": True,
                "dispatch_verified": True,
                "foreground_verified": False,
                "focus_changed_after_search": True,
                "search_query_verified": True,
                "search_query_identity_verified": verified_by_identity,
                "search_result_changed_from_nonmatching_baseline": (
                    verified_by_nonmatching_baseline_change
                ),
                "result_fingerprint": str(verified_data.get("fingerprint") or ""),
                "observed_app": final_app_name,
                "poll_attempts": len(foreground_observations),
            },
            "permission_error": False,
            "fallback_used": False,
            "fallback_result": {
                "dispatch": {
                    "command": command,
                    "returncode": dispatch.returncode,
                },
                "focus": focus_result,
                "foreground_observations": foreground_observations,
                "final_foreground_observation": final_foreground_observation,
                "baseline_evidence": baseline_evidence,
                "result_evidence_observations": evidence_observations,
            },
        }
    if not final_foreground_verified:
        payload = {
            "ok": False,
            "action": "media.apple_music.search",
            "summary": (
                "Apple Music search results matched, but the final foreground "
                "state could not be observed"
            ),
            "error": str(
                final_foreground_observation.get("error")
                or "apple_music_search_final_foreground_unverified"
            ),
            "blocking_condition": "foreground_focus_unverified",
            "data": {
                "query": clean_query,
                "url": search_url,
                "target_app": "Music",
                "open_target": "apple_music_search",
                "search_opened": False,
                "dispatch_verified": True,
                "foreground_verified": False,
                "search_query_verified": False,
                "search_query_identity_verified": verified_by_identity,
                "search_result_changed_from_nonmatching_baseline": (
                    verified_by_nonmatching_baseline_change
                ),
                "observed_app": final_app_name,
                "poll_attempts": len(foreground_observations),
            },
            "permission_error": bool(
                final_foreground_observation.get("permission_error")
            ),
            "recommended_tools": ["desktop.permissions", "desktop.active_window"],
            "fallback_used": True,
            "fallback_result": {
                "dispatch": {
                    "command": command,
                    "returncode": dispatch.returncode,
                },
                "focus": focus_result,
                "foreground_observations": foreground_observations,
                "final_foreground_observation": final_foreground_observation,
                "baseline_evidence": baseline_evidence,
                "result_evidence_observations": evidence_observations,
            },
        }
        for key in (
            "missing_permissions",
            "permission_targets",
            "recovery_hints",
            "recovery_actions",
        ):
            if final_foreground_observation.get(key):
                payload[key] = final_foreground_observation[key]
        return _with_permission_metadata("app.focus", payload)

    return {
        "ok": True,
        "action": "media.apple_music.search",
        "summary": f"Opened Apple Music search for {clean_query}",
        "data": {
            "query": clean_query,
            "url": search_url,
            "target_app": "Music",
            "open_target": "apple_music_search",
            "search_opened": True,
            "dispatch_verified": True,
            "foreground_verified": True,
            "search_query_verified": True,
            "search_query_identity_verified": verified_by_identity,
            "search_result_changed_from_nonmatching_baseline": (
                verified_by_nonmatching_baseline_change
            ),
            "result_fingerprint": str(verified_data.get("fingerprint") or ""),
            "poll_attempts": len(foreground_observations),
        },
        "permission_error": False,
        "fallback_used": False,
        "fallback_result": {
            "dispatch": {
                "command": command,
                "returncode": dispatch.returncode,
            },
            "focus": focus_result,
            "foreground_observations": foreground_observations,
            "final_foreground_observation": final_foreground_observation,
            "baseline_evidence": baseline_evidence,
            "result_evidence_observations": evidence_observations,
        },
    }


def system_volume(action: str, *, level: Any = None, step: Any = None) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("system.volume")
    clean_action = _clean_system_volume_action(action)
    current = _read_system_volume()
    if not current.get("ok"):
        return current
    old_level = int(current["data"]["level"])
    old_muted = bool(current["data"]["muted"])
    target_level = old_level
    target_muted = old_muted
    if clean_action == "status":
        return {
            "ok": True,
            "action": "system.volume",
            "summary": f"System volume is {old_level}%{' and muted' if old_muted else ''}",
            "data": {
                "requested_action": clean_action,
                "old_level": old_level,
                "level": old_level,
                "muted": old_muted,
                "changed": False,
            },
            "permission_error": False,
            "fallback_used": False,
        }
    if clean_action == "set":
        target_level = _coerce_percentage(level, default=old_level)
        target_muted = False
    elif clean_action == "up":
        target_level = min(100, old_level + _coerce_percentage(step, default=10))
        target_muted = False
    elif clean_action == "down":
        target_level = max(0, old_level - _coerce_percentage(step, default=10))
        target_muted = False
    elif clean_action == "mute":
        target_muted = True
    elif clean_action == "unmute":
        target_muted = False
    result = _run_osascript(
        """
        on run argv
            set targetLevel to item 1 of argv as integer
            set targetMuted to item 2 of argv
            if targetMuted is "true" then
                set volume with output muted true
            else
                set volume output volume targetLevel
                set volume with output muted false
            end if
            delay 0.05
            set volumeSettings to get volume settings
            return (output volume of volumeSettings as text) & "|" & (output muted of volumeSettings as text)
        end run
        """,
        [str(target_level), "true" if target_muted else "false"],
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "system.volume",
            {**result, "action": "system.volume", "summary": "system.volume failed"},
        )
    new_level, muted = _parse_system_volume(result.get("stdout"))
    return {
        "ok": True,
        "action": "system.volume",
        "summary": _system_volume_summary(clean_action, old_level, new_level, muted),
        "data": {
            "requested_action": clean_action,
            "old_level": old_level,
            "old_muted": old_muted,
            "level": new_level,
            "muted": muted,
            "changed": old_level != new_level or old_muted != muted,
        },
        "permission_error": False,
        "fallback_used": False,
    }


def system_brightness(action: str, *, step: Any = None) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("system.brightness")
    clean_action = _clean_system_brightness_action(action)
    clean_step = _clean_brightness_step(step)
    key_code = 145 if clean_action == "up" else 144
    result = _run_osascript(
        """
        on run argv
            set keyCodeValue to item 1 of argv as integer
            set repeatCount to item 2 of argv as integer
            repeat repeatCount times
                tell application "System Events" to key code keyCodeValue
                delay 0.05
            end repeat
            return "adjusted"
        end run
        """,
        [str(key_code), str(clean_step)],
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "system.brightness",
            {**result, "action": "system.brightness", "summary": "system.brightness failed"},
        )
    return {
        "ok": True,
        "action": "system.brightness",
        "summary": (
            "Display brightness increased"
            if clean_action == "up"
            else "Display brightness decreased"
        ),
        "data": {
            "requested_action": clean_action,
            "step": clean_step,
            "key_code": key_code,
        },
        "permission_error": False,
        "fallback_used": False,
    }


def system_display_sleep() -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("system.display_sleep")
    try:
        result = subprocess.run(
            ["pmset", "displaysleepnow"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return _error("system.display_sleep", exc)
    if result.returncode != 0:
        return _failed("system.display_sleep", result)
    return {
        "ok": True,
        "action": "system.display_sleep",
        "summary": "Display sleep requested",
        "data": {
            "requested_action": "sleep",
            "command": "pmset displaysleepnow",
        },
        "permission_error": False,
        "fallback_used": False,
    }


def system_screen_saver_start() -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("system.screen_saver_start")
    target = "/System/Library/CoreServices/ScreenSaverEngine.app"
    try:
        result = subprocess.run(
            ["open", target],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return _error("system.screen_saver_start", exc)
    if result.returncode != 0:
        return _failed("system.screen_saver_start", result)
    return {
        "ok": True,
        "action": "system.screen_saver_start",
        "summary": "Screen saver start requested",
        "data": {
            "requested_action": "start",
            "target": target,
            "command": f"open {target}",
        },
        "permission_error": False,
        "fallback_used": False,
    }


def clipboard_write(text: str) -> dict[str, Any]:
    clean_text = _clean_required(text, "text")
    command = _clipboard_write_command()
    if not command:
        return _unsupported("clipboard.write")
    try:
        result = subprocess.run(
            command,
            input=clean_text,
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except Exception as exc:
        return _error("clipboard.write", exc)
    if result.returncode != 0:
        return _failed("clipboard.write", result)
    read_command = _clipboard_read_command()
    readback = None
    if read_command:
        try:
            readback = subprocess.run(
                read_command,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except Exception:
            readback = None
    postcondition_verified = bool(
        readback is not None
        and readback.returncode == 0
        and str(readback.stdout or "") == clean_text
    )
    response = {
        "ok": True,
        "action": "clipboard.write",
        "summary": f"Copied {len(clean_text)} characters to clipboard",
        "data": {
            "text_length": len(clean_text),
            "platform": _desktop_platform(),
            "postcondition_verified": postcondition_verified,
        },
        "permission_error": False,
        "fallback_used": False,
    }
    if postcondition_verified:
        return {**response, "postcondition_verified": True}
    return {
        **response,
        "ok": False,
        "verification_failed": True,
        "retryable": True,
        "error": "clipboard_write_readback_unverified",
    }


def clipboard_read(max_chars: Any = 2000) -> dict[str, Any]:
    command = _clipboard_read_command()
    if not command:
        return _unsupported("clipboard.read")
    try:
        clean_max_chars = _clean_clipboard_read_limit(max_chars)
    except ValueError as exc:
        return _error("clipboard.read", exc)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return _error("clipboard.read", exc)
    if result.returncode != 0:
        return _failed("clipboard.read", result)
    text = str(result.stdout or "")
    preview = text[:clean_max_chars]
    return {
        "ok": True,
        "action": "clipboard.read",
        "summary": f"Read {len(text)} characters from clipboard",
        "data": {
            "text": preview,
            "text_length": len(text),
            "truncated": len(text) > clean_max_chars,
            "max_chars": clean_max_chars,
            "platform": _desktop_platform(),
        },
        "permission_error": False,
        "fallback_used": False,
    }


def notes_create(body: str, *, title: str = "", folder_name: str = "") -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("notes.create")
    clean_body = _clean_required(body, "body")
    clean_title = str(title or "").strip() or _note_title_from_body(clean_body)
    clean_folder_name = str(folder_name or "").strip()
    result = _run_osascript(
        """
        on run argv
            set noteTitle to item 1 of argv
            set noteBody to item 2 of argv
            set folderName to item 3 of argv
            tell application "Notes"
                set targetAccount to default account
                if folderName is "" then
                    set targetFolder to first folder of targetAccount
                else
                    set targetFolder to folder folderName of targetAccount
                end if
                set newNote to make new note at targetFolder with properties {name:noteTitle, body:noteBody}
                try
                    return id of newNote
                on error
                    return "created"
                end try
            end tell
        end run
        """,
        [clean_title, clean_body, clean_folder_name],
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "notes.create",
            {**result, "action": "notes.create", "summary": "notes.create failed"},
        )
    note_id = str(result.get("stdout") or "").strip()
    return {
        "ok": True,
        "action": "notes.create",
        "summary": f"Created note: {clean_title}",
        "postcondition_verified": bool(note_id),
        "data": {
            "title": clean_title,
            "body_length": len(clean_body),
            "folder_name": clean_folder_name,
            "note_id": note_id,
            "postcondition_verified": bool(note_id),
        },
        "permission_error": False,
        "fallback_used": False,
    }


def reminders_create(title: str, *, due_at: Any = None, list_name: str = "") -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("reminders.create")
    clean_title = _clean_required(title, "title")
    due = _parse_optional_local_datetime(due_at, "due_at")
    clean_list_name = str(list_name or "").strip()
    args = [clean_title, clean_list_name, *_datetime_argv(due)]
    result = _run_osascript(
        """
        on run argv
            set reminderTitle to item 1 of argv
            set listName to item 2 of argv
            set hasDueDate to item 3 of argv
            tell application "Reminders"
                if listName is "" then
                    set targetList to default list
                else
                    set targetList to list listName
                end if
                set newReminder to make new reminder at end of reminders of targetList with properties {name:reminderTitle}
                if hasDueDate is "true" then
                    set dueDate to current date
                    set year of dueDate to (item 4 of argv as integer)
                    set month of dueDate to (item 5 of argv as integer)
                    set day of dueDate to (item 6 of argv as integer)
                    set hours of dueDate to (item 7 of argv as integer)
                    set minutes of dueDate to (item 8 of argv as integer)
                    set seconds of dueDate to 0
                    set due date of newReminder to dueDate
                end if
                try
                    return id of newReminder
                on error
                    return "created"
                end try
            end tell
        end run
        """,
        args,
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "reminders.create",
            {**result, "action": "reminders.create", "summary": "reminders.create failed"},
        )
    reminder_id = str(result.get("stdout") or "").strip()
    return {
        "ok": True,
        "action": "reminders.create",
        "summary": (
            f"Created reminder: {clean_title}"
            if due is None
            else f"Created reminder: {clean_title} at {_format_local_datetime(due)}"
        ),
        "postcondition_verified": bool(reminder_id),
        "data": {
            "title": clean_title,
            "due_at": _format_local_datetime(due) if due is not None else "",
            "list_name": clean_list_name,
            "reminder_id": reminder_id,
            "postcondition_verified": bool(reminder_id),
        },
        "permission_error": False,
        "fallback_used": False,
    }


def calendar_create_event(
    title: str,
    *,
    start_at: Any,
    end_at: Any = None,
    calendar_name: str = "",
) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("calendar.create_event")
    clean_title = _clean_required(title, "title")
    start = _parse_required_local_datetime(start_at, "start_at")
    end = _parse_optional_local_datetime(end_at, "end_at")
    if end is None:
        end = start + timedelta(hours=1)
    if end <= start:
        raise ValueError("end_at must be after start_at")
    clean_calendar_name = str(calendar_name or "").strip()
    args = [
        clean_title,
        clean_calendar_name,
        *_datetime_argv(start),
        *_datetime_argv(end),
    ]
    result = _run_osascript(
        """
        on run argv
            set eventTitle to item 1 of argv
            set calendarName to item 2 of argv
            set startDate to current date
            set year of startDate to (item 4 of argv as integer)
            set month of startDate to (item 5 of argv as integer)
            set day of startDate to (item 6 of argv as integer)
            set hours of startDate to (item 7 of argv as integer)
            set minutes of startDate to (item 8 of argv as integer)
            set seconds of startDate to 0
            set endDate to current date
            set year of endDate to (item 10 of argv as integer)
            set month of endDate to (item 11 of argv as integer)
            set day of endDate to (item 12 of argv as integer)
            set hours of endDate to (item 13 of argv as integer)
            set minutes of endDate to (item 14 of argv as integer)
            set seconds of endDate to 0
            tell application "Calendar"
                if calendarName is "" then
                    set targetCalendar to first calendar
                else
                    set targetCalendar to calendar calendarName
                end if
                set newEvent to make new event at end of events of targetCalendar with properties {summary:eventTitle, start date:startDate, end date:endDate}
                try
                    return uid of newEvent
                on error
                    return "created"
                end try
            end tell
        end run
        """,
        args,
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "calendar.create_event",
            {**result, "action": "calendar.create_event", "summary": "calendar.create_event failed"},
        )
    event_id = str(result.get("stdout") or "").strip()
    return {
        "ok": True,
        "action": "calendar.create_event",
        "summary": (
            f"Created calendar event: {clean_title} from {_format_local_datetime(start)} "
            f"to {_format_local_datetime(end)}"
        ),
        "postcondition_verified": bool(event_id),
        "data": {
            "title": clean_title,
            "start_at": _format_local_datetime(start),
            "end_at": _format_local_datetime(end),
            "calendar_name": clean_calendar_name,
            "event_id": event_id,
            "postcondition_verified": bool(event_id),
        },
        "permission_error": False,
        "fallback_used": False,
    }


def desktop_hide_app() -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.hide_app")
    result = _run_osascript(
        """
        on run argv
            tell application "System Events" to keystroke "h" using {command down}
            return "hidden_app"
        end run
        """,
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "desktop.hide_app",
            {
                **result,
                "action": "desktop.hide_app",
                "summary": "desktop.hide_app failed",
            },
        )
    return {
        "ok": True,
        "action": "desktop.hide_app",
        "summary": "Hid the foreground app",
        "data": {"key": "h", "modifiers": ["command"]},
        "permission_error": False,
        "fallback_used": False,
    }


def desktop_show_all_apps() -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.show_all_apps")
    result = _run_osascript(
        """
        on run argv
            set shownCount to 0
            tell application "System Events"
                repeat with processRef in application processes
                    try
                        if visible of processRef is false then
                            set visible of processRef to true
                            set shownCount to shownCount + 1
                        end if
                    end try
                end repeat
            end tell
            return "shown_all|" & shownCount
        end run
        """,
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "desktop.show_all_apps",
            {
                **result,
                "action": "desktop.show_all_apps",
                "summary": "desktop.show_all_apps failed",
            },
        )
    stdout = str(result.get("stdout") or "").strip()
    parts = stdout.split("|") if stdout else []
    shown_count = _int_value(parts[1] if len(parts) > 1 else 0)
    return {
        "ok": True,
        "action": "desktop.show_all_apps",
        "summary": "Showed hidden apps",
        "data": {"shown_app_count": shown_count},
        "permission_error": False,
        "fallback_used": False,
    }


def desktop_minimize_window() -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.minimize_window")
    result = _run_osascript(
        """
        on run argv
            tell application "System Events" to keystroke "m" using {command down}
            return "minimized_window"
        end run
        """,
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "desktop.minimize_window",
            {
                **result,
                "action": "desktop.minimize_window",
                "summary": "desktop.minimize_window failed",
            },
        )
    return {
        "ok": True,
        "action": "desktop.minimize_window",
        "summary": "Minimized the foreground window",
        "data": {"key": "m", "modifiers": ["command"]},
        "permission_error": False,
        "fallback_used": False,
    }


def desktop_close_window() -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.close_window")
    result = _run_osascript(
        """
        on run argv
            tell application "System Events" to keystroke "w" using {command down}
            return "closed_window"
        end run
        """,
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "desktop.close_window",
            {
                **result,
                "action": "desktop.close_window",
                "summary": "desktop.close_window failed",
            },
        )
    return {
        "ok": True,
        "action": "desktop.close_window",
        "summary": "Closed the foreground window",
        "data": {"key": "w", "modifiers": ["command"]},
        "permission_error": False,
        "fallback_used": False,
    }


def desktop_quit_app() -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.quit_app")
    result = _run_osascript(
        """
        on run argv
            tell application "System Events" to keystroke "q" using {command down}
            return "quit_foreground_app"
        end run
        """,
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "desktop.quit_app",
            {
                **result,
                "action": "desktop.quit_app",
                "summary": "desktop.quit_app failed",
            },
        )
    return {
        "ok": True,
        "action": "desktop.quit_app",
        "summary": "Sent quit request to the foreground app",
        "data": {"key": "q", "modifiers": ["command"]},
        "permission_error": False,
        "fallback_used": False,
    }


def desktop_safe_key(action: str, *, repeat_count: Any = 1) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.safe_key")
    clean_action = _clean_safe_key_action(action)
    clean_repeat_count = _clean_key_repeat_count(repeat_count)
    key_code, label = _SAFE_KEYS[clean_action]
    modifier_clause = " using {shift down}" if clean_action == "shift_tab" else ""
    result = _run_osascript(
        f"""
        on run argv
            set keyCodeValue to item 1 of argv as integer
            set repeatCount to item 2 of argv as integer
            repeat repeatCount times
                tell application "System Events" to key code keyCodeValue{modifier_clause}
                delay 0.05
            end repeat
            return "pressed"
        end run
        """,
        [str(key_code), str(clean_repeat_count)],
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "desktop.safe_key",
            {**result, "action": "desktop.safe_key", "summary": "desktop.safe_key failed"},
        )
    return {
        "ok": True,
        "action": "desktop.safe_key",
        "summary": (
            f"Pressed safe foreground key: {label}"
            if clean_repeat_count == 1
            else f"Pressed safe foreground key: {label} x{clean_repeat_count}"
        ),
        "data": {
            "key_action": clean_action,
            "key_label": label,
            "key_code": key_code,
            "repeat_count": clean_repeat_count,
            "explicit_user_key": True,
        },
        "permission_error": False,
        "fallback_used": False,
    }


def desktop_type_text(text: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.type_text")
    return _send_desktop_text(
        "desktop.type_text",
        text,
        summary="Typed text into the foreground app",
    )


def desktop_safe_type_text(text: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.safe_type_text")
    payload = _send_desktop_text(
        "desktop.safe_type_text",
        text,
        summary="Typed user-provided text into the foreground app",
    )
    if payload.get("ok"):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        payload["data"] = {**data, "explicit_user_text": True}
    return payload


def _send_desktop_text(
    action_name: str,
    text: str,
    *,
    summary: str,
) -> dict[str, Any]:
    clean_text = _clean_required(text, "text")
    result = _run_osascript(
        """
        on run argv
            set textToType to item 1 of argv
            tell application "System Events" to keystroke textToType
            return "typed"
        end run
        """,
        [clean_text],
    )
    if not result["ok"]:
        return _with_permission_metadata(
            action_name,
            {**result, "action": action_name, "summary": f"{action_name} failed"},
        )
    return {
        "ok": True,
        "action": action_name,
        "summary": summary,
        "data": {"character_count": len(clean_text)},
        "permission_error": False,
        "fallback_used": False,
    }


def desktop_click(x: Any, y: Any, *, click_count: Any = 1) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.click")
    return _send_desktop_click(
        "desktop.click",
        x,
        y,
        click_count=click_count,
    )


def desktop_safe_click(x: Any, y: Any) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.safe_click")
    payload = _send_desktop_click(
        "desktop.safe_click",
        x,
        y,
        click_count=1,
    )
    if payload.get("ok"):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        payload["summary"] = f"Clicked explicit foreground coordinate at ({data.get('x')}, {data.get('y')})"
        payload["data"] = {**data, "explicit_user_coordinates": True}
    return payload


def desktop_safe_scroll(direction: str, *, pages: Any = 1) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.safe_scroll")
    clean_direction = _clean_scroll_direction(direction)
    clean_pages = _clean_scroll_pages(pages)
    key_code = 121 if clean_direction == "down" else 116
    result = _run_osascript(
        """
        on run argv
            set keyCodeValue to item 1 of argv as integer
            set pageCount to item 2 of argv as integer
            repeat pageCount times
                tell application "System Events" to key code keyCodeValue
                delay 0.05
            end repeat
            return "scrolled"
        end run
        """,
        [str(key_code), str(clean_pages)],
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "desktop.safe_scroll",
            {**result, "action": "desktop.safe_scroll", "summary": "desktop.safe_scroll failed"},
        )
    label = "down" if clean_direction == "down" else "up"
    return {
        "ok": True,
        "action": "desktop.safe_scroll",
        "summary": f"Scrolled foreground desktop {label} {clean_pages} page{'s' if clean_pages != 1 else ''}",
        "data": {
            "direction": clean_direction,
            "pages": clean_pages,
            "key_code": key_code,
            "explicit_user_scroll": True,
        },
        "permission_error": False,
        "fallback_used": False,
    }


def _send_desktop_click(
    action_name: str,
    x: Any,
    y: Any,
    *,
    click_count: Any = 1,
) -> dict[str, Any]:
    clean_x = _clean_coordinate(x, "x")
    clean_y = _clean_coordinate(y, "y")
    clean_count = _clean_click_count(click_count)
    result = _run_osascript(
        """
        on run argv
            set xCoord to item 1 of argv as integer
            set yCoord to item 2 of argv as integer
            set clickCount to item 3 of argv as integer
            repeat clickCount times
                tell application "System Events" to click at {xCoord, yCoord}
                delay 0.05
            end repeat
            return "clicked"
        end run
        """,
        [str(clean_x), str(clean_y), str(clean_count)],
    )
    if not result["ok"]:
        return _with_permission_metadata(
            action_name,
            {**result, "action": action_name, "summary": f"{action_name} failed"},
        )
    return {
        "ok": True,
        "action": action_name,
        "summary": f"Clicked foreground desktop at ({clean_x}, {clean_y})",
        "data": {"x": clean_x, "y": clean_y, "click_count": clean_count},
        "permission_error": False,
        "fallback_used": False,
    }


def desktop_hotkey(key: str, modifiers: list[str] | None = None) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.hotkey")
    payload = _send_desktop_keystroke("desktop.hotkey", key, modifiers or [])
    if not payload.get("ok"):
        return payload
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    clean_key = str(data.get("key") or "").strip()
    clean_modifiers = data.get("modifiers") if isinstance(data.get("modifiers"), list) else []
    payload["summary"] = f"Pressed hotkey { '+'.join([*clean_modifiers, clean_key]) }"
    return payload


def desktop_submit_foreground(action: str = "submit") -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.submit_foreground")
    clean_action = str(action or "submit").strip().lower()
    if clean_action not in {"send", "submit", "confirm"}:
        clean_action = "submit"
    payload = _send_desktop_keystroke("desktop.submit_foreground", "return", [])
    if not payload.get("ok"):
        return payload
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    data["submit_action"] = clean_action
    payload["data"] = data
    payload["summary"] = f"Submitted foreground {clean_action} action"
    return payload


def desktop_search_submit() -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.search_submit")
    payload = _send_desktop_keystroke("desktop.search_submit", "return", [])
    if not payload.get("ok"):
        return payload
    payload["summary"] = "Submitted foreground search query"
    return payload


def desktop_safe_shortcut(action: str) -> dict[str, Any]:
    if _desktop_platform() != "macos":
        return _unsupported("desktop.safe_shortcut")
    clean_action = _clean_safe_shortcut_action(action)
    if clean_action == "copy_current_page_link":
        return _desktop_copy_current_page_link_shortcut()
    key, modifiers, label = _SAFE_SHORTCUTS[clean_action]
    payload = _send_desktop_keystroke("desktop.safe_shortcut", key, list(modifiers))
    if not payload.get("ok"):
        return payload
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    payload["summary"] = f"Executed safe shortcut: {label}"
    payload["data"] = {
        **data,
        "shortcut_action": clean_action,
        "shortcut_label": label,
    }
    return payload


def _desktop_copy_current_page_link_shortcut() -> dict[str, Any]:
    select_payload = _send_desktop_keystroke("desktop.safe_shortcut", "l", ["command"])
    if not select_payload.get("ok"):
        return select_payload
    copy_payload = _send_desktop_keystroke("desktop.safe_shortcut", "c", ["command"])
    if not copy_payload.get("ok"):
        return copy_payload
    data = copy_payload.get("data") if isinstance(copy_payload.get("data"), dict) else {}
    copy_payload["summary"] = "Executed safe shortcut: copy current page link"
    copy_payload["data"] = {
        **data,
        "shortcut_action": "copy_current_page_link",
        "shortcut_label": "copy current page link",
        "steps": [
            {"key": "l", "modifiers": ["command"]},
            {"key": "c", "modifiers": ["command"]},
        ],
    }
    return copy_payload


def _send_desktop_keystroke(
    action_name: str,
    key: str,
    modifiers: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    clean_key = _clean_required(key, "key")
    clean_modifiers = _clean_modifiers(list(modifiers or []))
    modifier_clause = ""
    if clean_modifiers:
        modifier_clause = " using {" + ", ".join(f"{item} down" for item in clean_modifiers) + "}"
    key_code = _HOTKEY_KEY_CODES.get(clean_key.lower())
    if key_code is None:
        result = _run_osascript(
            f"""
            on run argv
                set keyName to item 1 of argv
                tell application "System Events" to keystroke keyName{modifier_clause}
                return "hotkey"
            end run
            """,
            [clean_key],
        )
    else:
        result = _run_osascript(
            f"""
            on run argv
                set keyCodeValue to item 1 of argv as integer
                tell application "System Events" to key code keyCodeValue{modifier_clause}
                return "hotkey"
            end run
            """,
            [str(key_code)],
        )
    if not result["ok"]:
        return _with_permission_metadata(
            action_name,
            {**result, "action": action_name, "summary": f"{action_name} failed"},
        )
    data = {"key": clean_key, "modifiers": clean_modifiers}
    if key_code is not None:
        data["key_code"] = key_code
    return {
        "ok": True,
        "action": action_name,
        "summary": f"Pressed hotkey { '+'.join([*clean_modifiers, clean_key]) }",
        "data": data,
        "permission_error": False,
        "fallback_used": False,
    }


def _run_osascript(
    script: str,
    args: list[str] | None = None,
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["osascript", "-e", script, *(args or [])],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except Exception as exc:
        return _error("osascript", exc)
    if result.returncode != 0:
        return _failed("osascript", result)
    return {
        "ok": True,
        "stdout": str(result.stdout or "").strip(),
        "stderr": str(result.stderr or "").strip(),
    }


def _run_jxa(script: str, args: list[str] | None = None) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script, *(args or [])],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return _error("jxa", exc)
    if result.returncode != 0:
        return _failed("jxa", result)
    return {
        "ok": True,
        "stdout": str(result.stdout or "").strip(),
        "stderr": str(result.stderr or "").strip(),
    }


def _desktop_platform() -> str:
    system = platform.system()
    if system == "Darwin":
        return "macos"
    if system == "Windows":
        return "windows"
    if system == "Linux":
        return "linux"
    return str(system or "unknown").lower()


def _unsupported(action: str) -> dict[str, Any]:
    return {
        "ok": False,
        "action": action,
        "summary": f"{action} is not supported on this platform yet.",
        "error": "unsupported_platform",
        "data": {"platform": _desktop_platform()},
        "permission_error": False,
        "fallback_used": False,
    }


def _error(action: str, exc: Exception) -> dict[str, Any]:
    payload = {
        "ok": False,
        "action": action,
        "summary": f"{action} failed",
        "error": str(exc),
        "data": {},
        "permission_error": _looks_like_permission_error(str(exc)),
        "fallback_used": False,
    }
    return _with_permission_metadata(action, payload)


def _failed(action: str, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    output = "\n".join(
        part.strip()
        for part in (result.stderr, result.stdout)
        if isinstance(part, str) and part.strip()
    )
    payload = {
        "ok": False,
        "action": action,
        "summary": f"{action} failed",
        "error": output or f"exit code {result.returncode}",
        "data": {},
        "returncode": result.returncode,
        "permission_error": _looks_like_permission_error(output),
        "fallback_used": False,
    }
    return _with_permission_metadata(action, payload)


def _app_open_failed(app_name: str, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    payload = _failed("app.open", result)
    payload["data"] = {"app_name": app_name}
    error = str(payload.get("error") or "")
    if _looks_like_app_not_found(error):
        payload["error_code"] = "app_not_found"
        payload["recovery_hints"] = [
            (
                "确认应用已安装，或换用 macOS 里的精确应用名，例如 "
                "Google Chrome、Safari、Music、Visual Studio Code。"
            )
        ]
        payload["recovery_actions"] = [
            {
                "label": "打开应用程序文件夹",
                "tool": "desktop.open_path",
                "input": {"path": "/Applications"},
                "permission_target": "app_not_found",
                "risk_level": "low",
            },
            {
                "label": "打开 App Store",
                "tool": "app.open",
                "input": {"app_name": "App Store"},
                "permission_target": "app_not_found",
                "risk_level": "low",
            },
        ]
    return payload


def _app_running_verification(app_name: str) -> dict[str, Any]:
    result = _run_osascript(
        """
        on run argv
            set appName to item 1 of argv
            if application appName is running then
                return "running"
            end if
            return "not_running"
        end run
        """,
        [app_name],
    )
    if result.get("ok"):
        status = str(result.get("stdout") or "").strip()
        return {
            "launch_verified": True if status == "running" else False if status == "not_running" else None,
            "launch_status": status or "unknown",
        }
    return {
        "launch_verified": None,
        "launch_status": "unknown",
        "launch_verification_error": str(result.get("error") or result.get("stderr") or ""),
    }


def _parse_app_focus_output(value: Any, fallback_app_name: str) -> dict[str, Any]:
    parts = str(value or "").strip().split("|", 5)
    app_name = parts[1] if len(parts) > 1 and parts[1] else fallback_app_name
    frontmost_text = parts[2] if len(parts) > 2 else ""
    frontmost_app = parts[3] if len(parts) > 3 else ""
    visible_text = parts[4] if len(parts) > 4 else ""
    window_count_text = parts[5] if len(parts) > 5 else ""
    focus_verified = (
        bool(app_name)
        and bool(frontmost_app)
        and _compact_app_match_name(app_name) == _compact_app_match_name(frontmost_app)
    )
    data: dict[str, Any] = {
        "app_name": app_name,
        "focus_verified": focus_verified,
        "focus_status": "frontmost" if focus_verified else "not_frontmost",
        "frontmost_app": frontmost_app,
    }
    system_events_reported_frontmost = _parse_optional_bool(frontmost_text)
    if system_events_reported_frontmost is not None:
        data["system_events_reported_frontmost"] = system_events_reported_frontmost
    process_visible = _parse_optional_bool(visible_text)
    if process_visible is not None:
        data["process_visible"] = process_visible
    window_count = _parse_optional_int(window_count_text)
    if window_count is not None:
        data["window_count"] = window_count
    return data


def _parse_dock_focus_output(value: Any, fallback_app_name: str) -> dict[str, Any]:
    parts = str(value or "").strip().split("|", 7)
    app_name = parts[1] if len(parts) > 1 and parts[1] else fallback_app_name
    dock_status = parts[2] if len(parts) > 2 else ""
    frontmost_text = parts[3] if len(parts) > 3 else ""
    frontmost_app = parts[4] if len(parts) > 4 else ""
    visible_text = parts[5] if len(parts) > 5 else ""
    window_count_text = parts[6] if len(parts) > 6 else ""
    dock_item_name = parts[7] if len(parts) > 7 else ""
    focus_verified = (
        bool(app_name)
        and bool(frontmost_app)
        and _compact_app_match_name(app_name) == _compact_app_match_name(frontmost_app)
    )
    data: dict[str, Any] = {
        "app_name": app_name,
        "focus_verified": focus_verified,
        "focus_status": "frontmost" if focus_verified else "not_frontmost",
        "frontmost_app": frontmost_app,
        "dock_status": dock_status,
    }
    dock_reported_frontmost = _parse_optional_bool(frontmost_text)
    if dock_reported_frontmost is not None:
        data["dock_reported_frontmost"] = dock_reported_frontmost
    if dock_item_name:
        data["dock_item_name"] = dock_item_name
    process_visible = _parse_optional_bool(visible_text)
    if process_visible is not None:
        data["process_visible"] = process_visible
    window_count = _parse_optional_int(window_count_text)
    if window_count is not None:
        data["window_count"] = window_count
    return data


def _parse_optional_bool(value: Any) -> bool | None:
    text = str(value or "").strip().casefold()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _parse_optional_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _focus_surface_retry_needed(*snapshots: dict[str, Any]) -> bool:
    for snapshot in snapshots:
        if not snapshot or snapshot.get("focus_verified") is True:
            continue
        if snapshot.get("process_visible") is False:
            return True
        if snapshot.get("window_count") == 0:
            return True
    return False


def _latest_focus_observation(*snapshots: dict[str, Any]) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for snapshot in snapshots:
        if not snapshot:
            continue
        for key in (
            "focus_verified",
            "focus_status",
            "frontmost_app",
            "process_visible",
            "window_count",
            "dock_status",
            "dock_item_name",
            "launchservices_returncode",
            "native_bridge",
            "native_bridge_available",
            "native_attempts",
        ):
            if key in snapshot and snapshot[key] not in (None, ""):
                latest[key] = snapshot[key]
    return latest


def _app_bundle_id(app_name: str) -> str:
    clean_name = str(app_name or "").strip()
    if not clean_name:
        return ""
    result = _run_osascript(
        """
        on run argv
            set appName to item 1 of argv
            return id of application appName
        end run
        """,
        [clean_name],
    )
    if not result.get("ok"):
        return ""
    return str(result.get("stdout") or "").strip()


def _appkit_activate_app(app_name: str, *, bundle_id: str = "") -> dict[str, Any]:
    return _run_jxa(
        """
        function run(argv) {
            ObjC.import("AppKit");
            const requestedName = String(argv[0] || "");
            const requestedBundleId = String(argv[1] || "");
            const workspace = $.NSWorkspace.sharedWorkspace;
            const apps = workspace.runningApplications;
            let target = null;
            for (let index = 0; index < apps.count; index += 1) {
                const app = apps.objectAtIndex(index);
                const localizedName = app.localizedName ? ObjC.unwrap(app.localizedName) : "";
                const bundleId = app.bundleIdentifier ? ObjC.unwrap(app.bundleIdentifier) : "";
                if (
                    (requestedBundleId && bundleId === requestedBundleId)
                    || localizedName === requestedName
                ) {
                    target = app;
                    break;
                }
            }
            const frontBefore = workspace.frontmostApplication;
            const frontBeforeName = frontBefore && frontBefore.localizedName ? ObjC.unwrap(frontBefore.localizedName) : "";
            if (!target) {
                return "appkit|" + requestedName + "|missing|false|" + frontBeforeName;
            }
            const activateResult = target.activateWithOptions(
                $.NSApplicationActivateAllWindows | $.NSApplicationActivateIgnoringOtherApps
            );
            delay(0.2);
            const frontAfter = workspace.frontmostApplication;
            const frontAfterName = frontAfter && frontAfter.localizedName ? ObjC.unwrap(frontAfter.localizedName) : "";
            return "appkit|" + requestedName + "|" + activateResult + "|" + target.active + "|" + frontAfterName;
        }
        """,
        [app_name, str(bundle_id or "").strip()],
    )


def _appkit_frontmost_app_name() -> dict[str, Any]:
    result = _run_jxa(
        """
        ObjC.import("AppKit");
        const frontmost = $.NSWorkspace.sharedWorkspace.frontmostApplication;
        frontmost && frontmost.localizedName ? ObjC.unwrap(frontmost.localizedName) : "";
        """
    )
    return {
        "ok": result.get("ok") is True,
        "app_name": str(result.get("stdout") or "").strip(),
        **({"error": str(result.get("error") or "")} if result.get("error") else {}),
    }


def _desktop_session_is_locked(frontmost_app: Any) -> bool:
    return _compact_app_match_name(str(frontmost_app or "")) == "loginwindow"


def _desktop_runtime_blocking_conditions(
    *,
    use_cache: bool = True,
    cached_only: bool = False,
) -> dict[str, list[str]]:
    try:
        from apps.shell.yachiyo_agent.desktop_permissions import (
            cached_desktop_runtime_blocking_conditions_by_capability,
            desktop_runtime_blocking_conditions_by_capability,
        )
    except Exception:
        return {}
    try:
        if cached_only:
            raw_blockers = cached_desktop_runtime_blocking_conditions_by_capability()
        else:
            raw_blockers = desktop_runtime_blocking_conditions_by_capability(
                use_cache=use_cache,
            )
    except Exception:
        return {}
    return _clean_missing_permissions_by_capability(raw_blockers)


def _desktop_session_locked_by_runtime_probe() -> bool:
    blockers = _desktop_runtime_blocking_conditions(use_cache=False)
    return any(
        "desktop_session_locked" in conditions
        for conditions in blockers.values()
    )


def _focus_failure_indicates_locked_session(
    frontmost_app: Any,
    focus_attempts: list[dict[str, Any]],
) -> bool:
    if not _desktop_session_is_locked(frontmost_app):
        return False
    return not _focus_attempts_observed_unlocked_frontmost(focus_attempts)


def _focus_attempts_observed_unlocked_frontmost(
    focus_attempts: list[dict[str, Any]],
) -> bool:
    for attempt in focus_attempts:
        name = _compact_app_match_name(str(attempt.get("frontmost_app") or ""))
        if name and name != "loginwindow":
            return True
    return False


def _desktop_session_locked_result(
    action: str,
    *,
    app_name: str = "",
    data: dict[str, Any] | None = None,
    fallback_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_app = str(app_name or "").strip()
    retry_tool = "app.focus" if clean_app else "desktop.active_window"
    retry_input = {"app_name": clean_app} if clean_app else {}
    payload: dict[str, Any] = {
        "ok": False,
        "action": action,
        "summary": "macOS desktop session is locked",
        "error": "desktop_session_locked",
        "blocking_condition": "desktop_session_locked",
        "retryable": True,
        "data": {
            **dict(data or {}),
            "desktop_session_locked": True,
            "blocking_condition": "desktop_session_locked",
            "retryable": True,
        },
        "recommended_tools": [retry_tool],
        "recovery_hints": [
            "Unlock the active macOS user session, then retry the foreground desktop action."
        ],
        "recovery_actions": [
            {
                "label": f"解锁后重试{clean_app}" if clean_app else "解锁后重新检查前台窗口",
                "tool": retry_tool,
                "input": retry_input,
                "permission_target": "desktop_session_unlocked",
                "risk_level": "low",
            }
        ],
        "permission_error": False,
        "fallback_used": fallback_result is not None,
    }
    if fallback_result is not None:
        payload["fallback_result"] = fallback_result
    return payload


def _parse_appkit_focus_output(value: Any, fallback_app_name: str) -> dict[str, Any]:
    parts = str(value or "").strip().split("|", 4)
    app_name = parts[1] if len(parts) > 1 and parts[1] else fallback_app_name
    activate_result = parts[2] if len(parts) > 2 else ""
    active_text = parts[3] if len(parts) > 3 else ""
    frontmost_app = parts[4] if len(parts) > 4 else ""
    focus_verified = (
        bool(app_name)
        and bool(frontmost_app)
        and _compact_app_match_name(app_name) == _compact_app_match_name(frontmost_app)
    )
    data: dict[str, Any] = {
        "app_name": app_name,
        "focus_verified": focus_verified,
        "focus_status": "frontmost" if focus_verified else "not_frontmost",
        "frontmost_app": frontmost_app,
        "appkit_activate_result": activate_result,
    }
    appkit_reported_active = _parse_optional_bool(active_text)
    if appkit_reported_active is not None:
        data["appkit_reported_active"] = appkit_reported_active
    return data


def _app_focus_attempt(
    strategy: str,
    result: dict[str, Any],
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attempt: dict[str, Any] = {
        "strategy": strategy,
        "ok": bool(result.get("ok")),
    }
    if data:
        attempt.update(
            {
                key: data[key]
                for key in (
                    "focus_verified",
                    "focus_status",
                    "frontmost_app",
                    "process_visible",
                    "window_count",
                    "appkit_activate_result",
                    "dock_status",
                    "dock_item_name",
                    "launchservices_returncode",
                    "native_bridge",
                    "native_bridge_available",
                )
                if key in data
            }
        )
    if result.get("error"):
        attempt["error"] = str(result.get("error") or "")
    stderr = str(result.get("stderr") or "").strip()
    if stderr:
        attempt["stderr"] = stderr[:500]
    return attempt


def _app_focus_recovery_actions(app_name: str) -> list[dict[str, Any]]:
    clean_name = str(app_name or "").strip()
    actions = [
        {
            "label": f"重新打开{clean_name}",
            "tool": "app.open",
            "input": {"app_name": clean_name},
            "permission_target": "foreground_focus",
            "risk_level": "low",
        },
        {
            "label": "查看前台窗口",
            "tool": "desktop.active_window",
            "input": {},
            "permission_target": "foreground_focus",
            "risk_level": "low",
        },
        {
            "label": "截图确认前台",
            "tool": "screen.capture",
            "input": {"reason": "verify foreground app after focus failure"},
            "permission_target": "foreground_focus",
            "risk_level": "low",
        },
    ]
    actions.extend(_permission_recovery_actions_for_targets(["foreground_focus"]))
    return actions


def _inspect_app_recommended_tools(
    *,
    app_found: bool,
    running: bool,
    focus_requested: bool,
    focus_verified: bool,
    window_count: int,
    ui_count: int,
    control_like_count: int,
    visibility_limited: bool,
) -> list[str]:
    tools: list[str] = []
    if not app_found:
        tools.extend(["desktop.list_apps", "app.open"])
    if app_found and not running:
        tools.append("app.open")
    if app_found and focus_requested and not focus_verified:
        tools.extend(["app.focus", "desktop.permissions"])
    if visibility_limited or (running and window_count == 0):
        tools.extend(["app.show", "screen.capture", "desktop.windows", "desktop.ui_elements"])
    if ui_count == 0:
        tools.extend(["screen.capture", "desktop.permissions"])
    if focus_verified and control_like_count > 0:
        tools.extend(["desktop.click_ui_element", "desktop.type_into_ui_element"])
    return _ordered_unique(tools)


def _inspect_app_recovery_actions(
    app_name: str,
    *,
    focus_result: dict[str, Any] | None,
    ui_result: dict[str, Any] | None,
    app_found: bool,
    running: bool,
    focus_verified: bool,
    visibility_limited: bool,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if not app_found or not running:
        actions.append(
            {
                "label": f"打开{app_name}",
                "tool": "app.open",
                "input": {"app_name": app_name},
                "permission_target": "open_command",
                "risk_level": "low",
            }
        )
    if app_found and not focus_verified:
        focus_actions = (
            focus_result.get("recovery_actions")
            if isinstance(focus_result, dict) and isinstance(focus_result.get("recovery_actions"), list)
            else _app_focus_recovery_actions(app_name)
        )
        actions.extend(action for action in focus_actions if isinstance(action, dict))
    if visibility_limited or _tool_data(ui_result).get("visibility_limited") is True:
        actions.extend(
            [
                {
                    "label": "显示应用窗口",
                    "tool": "app.show",
                    "input": {"app_name": app_name},
                    "permission_target": "foreground_focus",
                    "risk_level": "low",
                },
                {
                    "label": "截图确认当前桌面",
                    "tool": "screen.capture",
                    "input": {"reason": f"inspect {app_name} after limited UI visibility"},
                    "permission_target": "screen_observation",
                    "risk_level": "low",
                },
            ]
        )
    return _dedupe_recovery_actions(actions)


def _dedupe_recovery_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    deduped: list[dict[str, Any]] = []
    for action in actions:
        tool_name = str(action.get("tool") or "").strip()
        label = str(action.get("label") or tool_name).strip()
        raw_input = action.get("input") if isinstance(action.get("input"), dict) else {}
        input_key = tuple(sorted((str(key), str(value)) for key, value in raw_input.items()))
        key = (tool_name, label, input_key)
        if not tool_name or key in seen:
            continue
        seen.add(key)
        deduped.append(action)
    return deduped


def _inspect_app_summary(
    app_name: str,
    *,
    ready_for_foreground_action: bool,
    focus_verified: bool,
    inspection_level: str,
    window_count: int,
    ui_count: int,
    visibility_limited: bool,
) -> str:
    if ready_for_foreground_action:
        return f"Inspected {app_name}: focused with accessible controls"
    if visibility_limited:
        return f"Inspected {app_name}: limited to menu-level UI; foreground action is not ready"
    if not focus_verified:
        return f"Inspected {app_name}: focus is not verified; foreground action is not ready"
    if ui_count:
        return f"Inspected {app_name}: {inspection_level} UI with {ui_count} elements"
    if window_count:
        return f"Inspected {app_name}: {window_count} windows but no readable UI controls"
    return f"Inspected {app_name}: no visible windows or UI controls"


def _looks_like_app_not_found(value: Any) -> bool:
    normalized = str(value or "").lower()
    return any(
        marker in normalized
        for marker in (
            "application not found",
            "unable to find application",
            "was not found",
            "can't find application",
            "can’t find application",
            "does not exist",
        )
    )


def _clean_required(value: str, field: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{field} is required")
    return clean


def _clean_bool(value: Any, *, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "y", "on"}:
        return True
    if normalized in {"false", "0", "no", "n", "off"}:
        return False
    return default


def _note_title_from_body(value: str) -> str:
    first_line = next((line.strip() for line in str(value or "").splitlines() if line.strip()), "")
    if not first_line:
        return "New Note"
    return first_line[:80]


def _parse_required_local_datetime(value: Any, field: str) -> datetime:
    if value in (None, ""):
        raise ValueError(f"{field} is required")
    return _parse_local_datetime(value, field)


def _parse_optional_local_datetime(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    return _parse_local_datetime(value, field)


def _parse_local_datetime(value: Any, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO local datetime") from exc
    return parsed.replace(tzinfo=None, second=0, microsecond=0)


def _datetime_argv(value: datetime | None) -> list[str]:
    if value is None:
        return ["false", "0", "1", "1", "0", "0"]
    return [
        "true",
        str(value.year),
        str(value.month),
        str(value.day),
        str(value.hour),
        str(value.minute),
    ]


def _format_local_datetime(value: datetime) -> str:
    return value.isoformat(timespec="minutes")


def _int_value(value: Any) -> int:
    try:
        return int(float(str(value or 0).strip()))
    except (TypeError, ValueError):
        return 0


def _clean_music_control_action(value: str) -> str:
    aliases = {
        "toggle": "toggle",
        "playpause": "toggle",
        "play_pause": "toggle",
        "pause": "pause",
        "play": "play",
        "resume": "play",
        "next": "next",
        "next_track": "next",
        "previous": "previous",
        "prev": "previous",
        "previous_track": "previous",
    }
    clean = aliases.get(str(value or "").strip().lower())
    if not clean:
        raise ValueError("action must be one of toggle, play, pause, next, or previous")
    return clean


def _clean_system_volume_action(value: str) -> str:
    aliases = {
        "status": "status",
        "read": "status",
        "get": "status",
        "set": "set",
        "up": "up",
        "increase": "up",
        "down": "down",
        "decrease": "down",
        "mute": "mute",
        "unmute": "unmute",
    }
    clean = aliases.get(str(value or "").strip().lower())
    if not clean:
        raise ValueError("action must be one of status, set, up, down, mute, or unmute")
    return clean


def _clean_system_brightness_action(value: str) -> str:
    aliases = {
        "up": "up",
        "increase": "up",
        "brighter": "up",
        "down": "down",
        "decrease": "down",
        "dimmer": "down",
    }
    clean = aliases.get(str(value or "").strip().lower())
    if not clean:
        raise ValueError("action must be one of up or down")
    return clean


def _read_system_volume() -> dict[str, Any]:
    result = _run_osascript(
        """
        set volumeSettings to get volume settings
        return (output volume of volumeSettings as text) & "|" & (output muted of volumeSettings as text)
        """
    )
    if not result["ok"]:
        return _with_permission_metadata(
            "system.volume",
            {**result, "action": "system.volume", "summary": "system.volume failed"},
        )
    level, muted = _parse_system_volume(result.get("stdout"))
    return {
        "ok": True,
        "action": "system.volume",
        "summary": f"System volume is {level}%{' and muted' if muted else ''}",
        "data": {"level": level, "muted": muted},
        "permission_error": False,
        "fallback_used": False,
    }


def _clipboard_write_command() -> list[str]:
    platform_name = _desktop_platform()
    if platform_name == "macos":
        return ["pbcopy"]
    if platform_name == "windows":
        return ["clip"]
    if platform_name == "linux":
        for command in (
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ):
            if shutil.which(command[0]):
                return command
    return []


def _clipboard_read_command() -> list[str]:
    platform_name = _desktop_platform()
    if platform_name == "macos":
        return ["pbpaste"]
    if platform_name == "windows":
        return ["powershell", "-NoProfile", "-Command", "Get-Clipboard"]
    if platform_name == "linux":
        for command in (
            ["wl-paste", "--no-newline"],
            ["xclip", "-selection", "clipboard", "-out"],
            ["xsel", "--clipboard", "--output"],
        ):
            if shutil.which(command[0]):
                return command
    return []


def _clean_clipboard_read_limit(value: Any) -> int:
    if value in (None, ""):
        return 2000
    if isinstance(value, bool):
        raise ValueError("max_chars must be an integer from 1 to 12000")
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_chars must be an integer from 1 to 12000") from exc
    return max(1, min(12000, limit))


def _parse_system_volume(value: Any) -> tuple[int, bool]:
    parts = str(value or "").strip().split("|", 1)
    level_text = parts[0] if parts else "0"
    muted_text = parts[1] if len(parts) > 1 else "false"
    return _coerce_percentage(level_text, default=0), muted_text.strip().lower() == "true"


def _coerce_percentage(value: Any, *, default: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError("percentage must be a number from 0 to 100")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("percentage must be a number from 0 to 100") from exc
    return max(0, min(100, int(round(number))))


def _system_volume_summary(action: str, old_level: int, level: int, muted: bool) -> str:
    if action == "mute":
        return "System volume muted"
    if action == "unmute":
        return f"System volume unmuted at {level}%"
    if action == "set":
        return f"System volume set to {level}%"
    if action == "up":
        return f"System volume increased from {old_level}% to {level}%"
    if action == "down":
        return f"System volume decreased from {old_level}% to {level}%"
    return f"System volume is {level}%{' and muted' if muted else ''}"


def _clean_coordinate(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative screen coordinate")
    try:
        coordinate = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative screen coordinate") from exc
    if not math.isfinite(coordinate) or coordinate < 0 or coordinate > 100000:
        raise ValueError(f"{field} must be a non-negative screen coordinate")
    return int(round(coordinate))


def _clean_click_count(value: Any) -> int:
    if value in (None, ""):
        return 1
    if isinstance(value, bool):
        raise ValueError("click_count must be an integer from 1 to 3")
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("click_count must be an integer from 1 to 3") from exc
    if count < 1 or count > 3:
        raise ValueError("click_count must be an integer from 1 to 3")
    return count


def _clean_scroll_direction(value: str) -> str:
    aliases = {
        "down": "down",
        "page_down": "down",
        "pagedown": "down",
        "scroll_down": "down",
        "向下": "down",
        "下": "down",
        "往下": "down",
        "下滚": "down",
        "下滑": "down",
        "up": "up",
        "page_up": "up",
        "pageup": "up",
        "scroll_up": "up",
        "向上": "up",
        "上": "up",
        "往上": "up",
        "上滚": "up",
        "上滑": "up",
    }
    clean = aliases.get(str(value or "").strip().lower().replace("-", "_").replace(" ", "_"))
    if not clean:
        raise ValueError("direction must be up or down")
    return clean


def _clean_scroll_pages(value: Any) -> int:
    if value in (None, ""):
        return 1
    if isinstance(value, bool):
        raise ValueError("pages must be an integer from 1 to 10")
    try:
        pages = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("pages must be an integer from 1 to 10") from exc
    if pages < 1 or pages > 10:
        raise ValueError("pages must be an integer from 1 to 10")
    return pages


def _clean_modifiers(modifiers: list[str]) -> list[str]:
    aliases = {
        "cmd": "command",
        "command": "command",
        "shift": "shift",
        "option": "option",
        "alt": "option",
        "control": "control",
        "ctrl": "control",
    }
    clean: list[str] = []
    for modifier in modifiers:
        name = aliases.get(str(modifier or "").strip().lower())
        if name and name not in clean:
            clean.append(name)
    return clean


def _clean_safe_shortcut_action(action: str) -> str:
    clean = str(action or "").strip().lower().replace("-", "_")
    if clean not in _SAFE_SHORTCUTS:
        raise ValueError(f"unsupported safe shortcut action: {action}")
    return clean


def _clean_safe_key_action(action: str) -> str:
    clean = str(action or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "esc": "escape",
        "退出": "escape",
        "取消": "escape",
        "tab_key": "tab",
        "制表": "tab",
        "制表键": "tab",
        "shift_tab": "shift_tab",
        "shifttab": "shift_tab",
        "shift+tab": "shift_tab",
        "shift_tab_key": "shift_tab",
        "上一个输入框": "shift_tab",
        "上一输入框": "shift_tab",
        "up": "arrow_up",
        "down": "arrow_down",
        "left": "arrow_left",
        "right": "arrow_right",
        "arrowup": "arrow_up",
        "arrowdown": "arrow_down",
        "arrowleft": "arrow_left",
        "arrowright": "arrow_right",
        "上": "arrow_up",
        "下": "arrow_down",
        "左": "arrow_left",
        "右": "arrow_right",
        "上箭头": "arrow_up",
        "下箭头": "arrow_down",
        "左箭头": "arrow_left",
        "右箭头": "arrow_right",
        "向上箭头": "arrow_up",
        "往上箭头": "arrow_up",
        "朝上箭头": "arrow_up",
        "向下箭头": "arrow_down",
        "往下箭头": "arrow_down",
        "朝下箭头": "arrow_down",
        "向左箭头": "arrow_left",
        "往左箭头": "arrow_left",
        "朝左箭头": "arrow_left",
        "向右箭头": "arrow_right",
        "往右箭头": "arrow_right",
        "朝右箭头": "arrow_right",
        "向上键": "arrow_up",
        "向下键": "arrow_down",
        "向左键": "arrow_left",
        "向右键": "arrow_right",
        "home_key": "home",
        "end_key": "end",
        "pageup": "page_up",
        "page_up_key": "page_up",
        "pagedown": "page_down",
        "page_down_key": "page_down",
    }
    clean = aliases.get(clean, clean)
    if clean not in _SAFE_KEYS:
        raise ValueError(f"unsupported safe key action: {action}")
    return clean


def _clean_key_repeat_count(value: Any) -> int:
    if value in (None, ""):
        return 1
    if isinstance(value, bool):
        raise ValueError("repeat_count must be an integer from 1 to 20")
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("repeat_count must be an integer from 1 to 20") from exc
    if count < 1 or count > 20:
        raise ValueError("repeat_count must be an integer from 1 to 20")
    return count


def _clean_brightness_step(value: Any) -> int:
    if value in (None, ""):
        return 2
    if isinstance(value, bool):
        raise ValueError("step must be an integer from 1 to 10")
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("step must be an integer from 1 to 10") from exc
    if count < 1 or count > 10:
        raise ValueError("step must be an integer from 1 to 10")
    return count


def _parse_running_apps(value: Any) -> list[dict[str, Any]]:
    apps: list[dict[str, Any]] = []
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        name, pid_text, frontmost_text = _split_status(line)
        if not name:
            continue
        apps.append(
            {
                "name": name,
                "pid": int(pid_text) if pid_text.isdigit() else None,
                "frontmost": frontmost_text.strip().lower() == "true",
            }
        )
    return apps


def _tool_data(result: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def _tool_running(result: dict[str, Any] | None) -> bool | None:
    data = _tool_data(result)
    if isinstance(data.get("running"), bool):
        return bool(data.get("running"))
    if isinstance(data.get("launch_verified"), bool):
        return bool(data.get("launch_verified"))
    return None


def _resolved_tool_app_name(result: dict[str, Any] | None, fallback: str) -> str:
    data = _tool_data(result)
    for key in ("app_name", "resolved_app_name"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return str(fallback or "").strip()


def _discovered_app_names(result: dict[str, Any]) -> list[str]:
    apps = _tool_data(result).get("apps")
    if not isinstance(apps, list):
        return []
    names: list[str] = []
    for item in apps:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _best_discovered_app_name(requested: str, names: list[str]) -> str:
    clean_requested = str(requested or "").strip()
    if not names:
        return ""
    requested_key = _compact_app_match_name(clean_requested)
    for name in names:
        if _compact_app_match_name(name) == requested_key:
            return name
    return names[0]


def _parse_window_rows(value: Any) -> list[dict[str, Any]]:
    windows_payload: list[dict[str, Any]] = []
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t", 4)
        while len(parts) < 5:
            parts.append("")
        app_name, pid_text, index_text, frontmost_text, title = parts
        if not app_name:
            continue
        windows_payload.append(
            {
                "app_name": app_name,
                "pid": int(pid_text) if pid_text.isdigit() else None,
                "index": int(index_text) if index_text.isdigit() else None,
                "frontmost": frontmost_text.strip().lower() == "true",
                "title": title.strip(),
            }
        )
    return windows_payload


def _parse_ui_elements_output(
    value: Any,
    role_filter: str = "",
    limit: int = 80,
) -> dict[str, Any]:
    app_name = ""
    pid: int | None = None
    title = ""
    window_id: int | None = None
    elements: list[dict[str, Any]] = []
    normalized_filter = str(role_filter or "").strip().lower()
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if parts[0] == "META":
            while len(parts) < 5:
                parts.append("")
            app_name = parts[1].strip()
            pid = int(parts[2]) if parts[2].strip().isdigit() else None
            title = parts[3].strip()
            window_id = int(parts[4]) if parts[4].strip().isdigit() else None
            continue
        while len(parts) < 11:
            parts.append("")
        depth, role, subrole, name, description, element_value, enabled, x, y, width, height = parts[:11]
        searchable = " ".join((role, subrole, name, description, element_value)).lower()
        if normalized_filter and normalized_filter not in searchable:
            continue
        element: dict[str, Any] = {
            "depth": _int_or_none(depth) or 0,
            "role": role.strip(),
            "subrole": subrole.strip(),
            "name": name.strip(),
            "description": description.strip(),
            "value": element_value.strip(),
            "enabled": enabled.strip().lower() == "true" if enabled.strip() else None,
        }
        frame = _ui_element_frame(x, y, width, height)
        if frame:
            element["frame"] = frame
            element["center"] = {
                "x": int(round(frame["x"] + frame["width"] / 2)),
                "y": int(round(frame["y"] + frame["height"] / 2)),
            }
        elements.append(element)
        if len(elements) >= limit:
            break
    return {
        "app_name": app_name,
        "pid": pid,
        "title": title,
        **({"window_id": window_id} if window_id is not None else {}),
        "elements": elements,
        "count": len(elements),
        "truncated": len(elements) >= limit,
    }


def _ui_role_counts(elements: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for element in elements:
        role = str(element.get("role") or "").strip()
        if not role:
            continue
        counts[role] = counts.get(role, 0) + 1
    return counts


def _ui_inspection_metadata(
    elements: list[dict[str, Any]],
    *,
    app_name: str = "",
    title: str = "",
) -> dict[str, Any]:
    role_counts = _ui_role_counts(elements)
    role_bearing_count = sum(role_counts.values())
    unclassified_count = max(0, len(elements) - role_bearing_count)
    menu_level_count = sum(count for role, count in role_counts.items() if "Menu" in role)
    control_like_count = sum(
        count for role, count in role_counts.items() if role in UI_CONTROL_LIKE_ROLES
    )
    has_elements = bool(elements)
    menu_level_only = (
        has_elements
        and role_bearing_count > 0
        and menu_level_count == role_bearing_count
        and control_like_count == 0
    )
    if not has_elements:
        inspection_level = "empty"
    elif control_like_count > 0:
        inspection_level = "control"
    elif menu_level_only:
        inspection_level = "menu"
    else:
        inspection_level = "structural"
    window_title_missing = not str(title or "").strip()
    visibility_limited = bool(app_name) and menu_level_only and window_title_missing
    return {
        "role_counts": role_counts,
        "unclassified_count": unclassified_count,
        "menu_level_count": menu_level_count,
        "control_like_count": control_like_count,
        "inspection_level": inspection_level,
        "menu_level_only": menu_level_only,
        "window_title_missing": window_title_missing,
        "visibility_limited": visibility_limited,
        "visibility_status": (
            "menu_level_only"
            if visibility_limited
            else "control_accessible"
            if control_like_count > 0
            else "empty"
            if not has_elements
            else "structural_only"
        ),
    }


def _window_visibility_metadata(
    app_name: str,
    windows_payload: list[dict[str, Any]],
) -> dict[str, Any]:
    clean_app = str(app_name or "").strip()
    if windows_payload:
        return {
            "visibility_limited": False,
            "window_visibility_status": "visible_windows",
        }
    if not clean_app:
        return {
            "visibility_limited": False,
            "window_visibility_status": "no_visible_windows",
        }
    verification = _app_running_verification(clean_app)
    is_running = verification.get("launch_verified") is True
    return {
        "visibility_limited": is_running,
        "window_visibility_status": (
            "running_without_visible_windows" if is_running else "not_running_or_unverified"
        ),
        "window_visibility_hint": (
            "System Events returned no app windows even though the app is running; "
            "fall back to ui_elements, screen capture, shortcuts, or permission diagnostics."
            if is_running
            else "System Events returned no app windows and the app could not be verified as running."
        ),
        **verification,
    }


def _matching_ui_elements(
    elements: list[Any],
    target: str,
    role_filter: str = "",
) -> list[dict[str, Any]]:
    target_text = _normalize_ui_match_text(target)
    if not target_text:
        return []
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, raw_element in enumerate(elements):
        if not isinstance(raw_element, dict):
            continue
        element = dict(raw_element)
        if element.get("enabled") is False:
            continue
        center = element.get("center") if isinstance(element.get("center"), dict) else {}
        if center.get("x") is None or center.get("y") is None:
            continue
        score = _ui_element_match_score(element, target_text, role_filter)
        if score <= 0:
            continue
        depth = element.get("depth") if isinstance(element.get("depth"), int) else 0
        scored.append((score - depth, -index, element))
    scored.sort(reverse=True, key=lambda item: (item[0], item[1]))
    return [element for _, _, element in scored]


def _ui_element_match_score(
    element: dict[str, Any],
    normalized_target: str,
    role_filter: str = "",
) -> int:
    label_texts = [_normalize_ui_match_text(_ui_element_field(element, key)) for key in ("name", "description", "value")]
    label_texts = [text for text in label_texts if text]
    searchable = _normalize_ui_match_text(
        " ".join(
            _ui_element_field(element, key)
            for key in ("role", "subrole", "name", "description", "value")
        )
    )
    if not searchable:
        return 0
    score = 0
    if normalized_target in label_texts:
        score = 100
    elif any(normalized_target in text for text in label_texts):
        score = 85
    elif any(text in normalized_target for text in label_texts if len(text) >= 2):
        score = 70
    elif normalized_target in searchable:
        score = 55
    if not score:
        return 0
    normalized_filter = _normalize_ui_match_text(role_filter)
    if normalized_filter and normalized_filter in searchable:
        score += 10
    if str(element.get("role") or "").strip():
        score += 2
    return score


def _candidate_ui_element_previews(elements: list[Any], limit: int = 8) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = []
    for raw_element in elements:
        if not isinstance(raw_element, dict):
            continue
        label = _ui_element_display_label(raw_element)
        center = raw_element.get("center") if isinstance(raw_element.get("center"), dict) else None
        if not label and not center:
            continue
        preview: dict[str, Any] = {
            "role": str(raw_element.get("role") or ""),
            "label": label,
            "enabled": raw_element.get("enabled"),
        }
        if center:
            preview["center"] = center
        previews.append(preview)
        if len(previews) >= limit:
            break
    return previews


def _ui_element_not_found_recovery_actions(
    target: str,
    role_filter: str,
    click_count: int,
) -> list[dict[str, Any]]:
    clean_target = str(target or "").strip()
    clean_filter = str(role_filter or "").strip()
    retry_input: dict[str, Any] = {"click_count": click_count}
    retry_input_schema: dict[str, Any] = {
        "type": "object",
        "required": ["x", "y"],
        "properties": {
            "x": {"type": "number", "minimum": 0},
            "y": {"type": "number", "minimum": 0},
            "click_count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3,
                "default": click_count,
            },
        },
    }
    return [
        {
            "label": "截取屏幕重新定位控件",
            "tool": "screen.capture",
            "input": {
                "reason": (
                    f"desktop.click_ui_element could not find {clean_target}; "
                    "capture screen before coordinate click"
                )
            },
            "permission_target": "screen_observation",
            "risk_level": "low",
            "retry_tool": "desktop.click",
            "recovery_retry_tool": "desktop.click",
            "retry_input": retry_input,
            "recovery_retry_input": retry_input,
            "retry_input_schema": retry_input_schema,
            "recovery_retry_input_schema": retry_input_schema,
            "retry_input_source": "screen_capture_artifact",
            "recovery_retry_input_source": "screen_capture_artifact",
            "retry_artifact_tool": "screen.capture",
            "recovery_retry_artifact_tool": "screen.capture",
            "retry_artifact_kind": "image",
            "recovery_retry_artifact_kind": "image",
            "required_retry_fields": ["x", "y"],
            "recommended_tools": ["screen.capture", "desktop.click"],
            "retry_prompt": (
                f"根据截图定位「{clean_target}」并用 desktop.click 点击坐标"
                if clean_target
                else "根据截图定位目标并用 desktop.click 点击坐标"
            ),
            "recovery_retry_prompt": (
                f"根据截图定位「{clean_target}」并用 desktop.click 点击坐标"
                if clean_target
                else "根据截图定位目标并用 desktop.click 点击坐标"
            ),
            "target": clean_target,
            "role_filter": clean_filter,
            "click_count": click_count,
        }
    ]


def _ui_element_type_not_found_recovery_actions(
    target: str,
    role_filter: str,
    character_count: int,
) -> list[dict[str, Any]]:
    clean_target = str(target or "").strip()
    clean_filter = str(role_filter or "").strip()
    retry_input: dict[str, Any] = {"click_count": 1}
    retry_input_schema: dict[str, Any] = {
        "type": "object",
        "required": ["x", "y"],
        "properties": {
            "x": {"type": "number", "minimum": 0},
            "y": {"type": "number", "minimum": 0},
            "click_count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3,
                "default": 1,
            },
        },
    }
    prompt = (
        f"根据截图定位「{clean_target}」输入框，先用 desktop.click 点击坐标，"
        "再用 desktop.type_text 输入原请求文本"
        if clean_target
        else "根据截图定位输入框，先用 desktop.click 点击坐标，再用 desktop.type_text 输入原请求文本"
    )
    return [
        {
            "label": "截取屏幕重新定位输入框",
            "tool": "screen.capture",
            "input": {
                "reason": (
                    f"desktop.type_into_ui_element could not find {clean_target}; "
                    "capture screen before coordinate focus and text entry"
                )
            },
            "permission_target": "screen_observation",
            "risk_level": "low",
            "retry_tool": "desktop.click",
            "recovery_retry_tool": "desktop.click",
            "retry_input": retry_input,
            "recovery_retry_input": retry_input,
            "retry_input_schema": retry_input_schema,
            "recovery_retry_input_schema": retry_input_schema,
            "retry_input_source": "screen_capture_artifact",
            "recovery_retry_input_source": "screen_capture_artifact",
            "retry_artifact_tool": "screen.capture",
            "recovery_retry_artifact_tool": "screen.capture",
            "retry_artifact_kind": "image",
            "recovery_retry_artifact_kind": "image",
            "required_retry_fields": ["x", "y"],
            "followup_tool": "desktop.type_text",
            "recovery_followup_tool": "desktop.type_text",
            "followup_input": {
                "text_source": "original_request",
                "character_count": character_count,
            },
            "recovery_followup_input": {
                "text_source": "original_request",
                "character_count": character_count,
            },
            "recommended_tools": ["screen.capture", "desktop.click", "desktop.type_text"],
            "retry_prompt": prompt,
            "recovery_retry_prompt": prompt,
            "target": clean_target,
            "role_filter": clean_filter,
            "character_count": character_count,
        }
    ]


def _ui_element_display_label(element: dict[str, Any]) -> str:
    return (
        _ui_element_field(element, "name")
        or _ui_element_field(element, "description")
        or _ui_element_field(element, "value")
        or _ui_element_field(element, "role")
    )


def _ui_element_field(element: dict[str, Any], key: str) -> str:
    return str(element.get(key) or "").strip()


def _normalize_ui_match_text(value: Any) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return normalized.strip("\"'“”‘’[]()（） ")


def _ui_element_frame(x: Any, y: Any, width: Any, height: Any) -> dict[str, int] | None:
    values = [_int_or_none(item) for item in (x, y, width, height)]
    if any(value is None for value in values):
        return None
    frame_x, frame_y, frame_width, frame_height = values
    if frame_width is None or frame_height is None or frame_width < 0 or frame_height < 0:
        return None
    return {
        "x": int(frame_x or 0),
        "y": int(frame_y or 0),
        "width": int(frame_width),
        "height": int(frame_height),
    }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(round(float(str(value or "").strip())))
    except (TypeError, ValueError):
        return None


def _ui_elements_summary(elements: list[dict[str, Any]], app_name: str = "") -> str:
    if not elements:
        return f"No visible UI elements found for {app_name}" if app_name else "No visible UI elements found"
    visible = []
    for item in elements[:5]:
        role = str(item.get("role") or "element").strip()
        label = (
            str(item.get("name") or "").strip()
            or str(item.get("description") or "").strip()
            or str(item.get("value") or "").strip()
        )
        visible.append(f"{role}: {label}" if label else role)
    suffix = f" (+{len(elements) - len(visible)} more)" if len(elements) > len(visible) else ""
    prefix = f"{app_name} UI elements" if app_name else "UI elements"
    return f"{prefix}: {', '.join(visible)}{suffix}"


def _running_apps_summary(names: list[str]) -> str:
    if not names:
        return "No foreground apps are running"
    visible = names[:5]
    suffix = f" (+{len(names) - len(visible)} more)" if len(names) > len(visible) else ""
    return f"Running apps: {', '.join(visible)}{suffix}"


def _windows_summary(windows_payload: list[dict[str, Any]], app_name: str = "") -> str:
    if not windows_payload:
        return f"No windows found for {app_name}" if app_name else "No desktop windows found"
    visible = []
    for item in windows_payload[:5]:
        app = str(item.get("app_name") or "").strip()
        title = str(item.get("title") or "").strip()
        visible.append(f"{app}: {title}" if title else app)
    suffix = (
        f" (+{len(windows_payload) - len(visible)} more)"
        if len(windows_payload) > len(visible)
        else ""
    )
    return f"Open windows: {', '.join(visible)}{suffix}"


def _clean_missing_permissions_by_capability(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    clean: dict[str, list[str]] = {}
    for capability, raw_targets in value.items():
        capability_id = str(capability or "").strip()
        if not capability_id:
            continue
        if isinstance(raw_targets, (list, tuple, set)):
            targets = _ordered_unique(str(item or "").strip() for item in raw_targets)
        else:
            targets = _ordered_unique([str(raw_targets or "").strip()])
        clean[capability_id] = [target for target in targets if target]
    return clean


def _affected_tools_for_missing_permissions(
    missing_by_capability: dict[str, list[str]],
) -> list[str]:
    tools: list[str] = []
    for capability_id, missing_targets in missing_by_capability.items():
        if not missing_targets:
            continue
        if capability_id == "desktop_execution":
            tools.extend(
                tool
                for capability_tools in _PERMISSION_CAPABILITY_TOOLS.values()
                for tool in capability_tools
            )
            continue
        if capability_id == "media_control":
            tools.extend(_PERMISSION_CAPABILITY_TOOLS.get(capability_id, ()))
            continue
        tools.extend(_PERMISSION_CAPABILITY_TOOLS.get(capability_id, ()))
    return _ordered_unique(tools)


def _desktop_permissions_summary(
    missing_targets: list[str],
    affected_tools: list[str],
    *,
    blocking_conditions: list[str] | None = None,
) -> str:
    blockers = list(blocking_conditions or [])
    if not missing_targets and not blockers:
        return "Desktop execution permissions are ready."
    parts: list[str] = []
    if missing_targets:
        targets = ", ".join(missing_targets[:6])
        target_suffix = "..." if len(missing_targets) > 6 else ""
        parts.append(f"Missing desktop permissions: {targets}{target_suffix}")
    if blockers:
        conditions = ", ".join(blockers[:6])
        condition_suffix = "..." if len(blockers) > 6 else ""
        parts.append(f"Desktop runtime blockers: {conditions}{condition_suffix}")
    if not affected_tools:
        return ". ".join(parts)
    tools = ", ".join(affected_tools[:6])
    tool_suffix = "..." if len(affected_tools) > 6 else ""
    parts.append(f"Affected tools: {tools}{tool_suffix}")
    return ". ".join(parts)


def _permission_recovery_actions_for_targets(targets: list[str]) -> list[dict[str, Any]]:
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    actions: list[dict[str, Any]] = []
    for target in targets:
        for action in _PERMISSION_RECOVERY_ACTIONS.get(str(target or "").strip(), ()):
            tool_name = str(action.get("tool") or "").strip()
            raw_input = action.get("input") if isinstance(action.get("input"), dict) else {}
            input_key = tuple(
                sorted((str(key), str(value)) for key, value in raw_input.items())
            )
            key = (tool_name, input_key)
            if not tool_name or key in seen:
                continue
            seen.add(key)
            actions.append(
                {
                    "label": str(action.get("label") or tool_name),
                    "tool": tool_name,
                    "input": dict(raw_input),
                    "permission_target": str(action.get("permission_target") or target),
                    "risk_level": str(action.get("risk_level") or "low"),
                }
            )
    return actions


def _runtime_blocking_recovery_actions_for_conditions(
    conditions: list[str],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if "desktop_permission_diagnostics_not_checked" in conditions:
        actions.append(
            {
                "label": "经你确认后验证桌面权限",
                "tool": "desktop.permissions.verify",
                "input": {},
                "permission_target": "desktop_permission_diagnostics",
                "risk_level": "medium",
            }
        )
    if "desktop_session_locked" in conditions:
        actions.append(
            {
                "label": "解锁后重新检查桌面权限",
                "tool": "desktop.permissions.verify",
                "input": {},
                "permission_target": "desktop_session_unlocked",
                "risk_level": "medium",
            }
        )
    if "foreground_focus_unavailable" in conditions:
        actions.extend(
            [
                {
                    "label": "查看前台窗口",
                    "tool": "desktop.active_window",
                    "input": {},
                    "permission_target": "foreground_focus",
                    "risk_level": "low",
                },
                {
                    "label": "截图确认前台",
                    "tool": "screen.capture",
                    "input": {"reason": "verify foreground app after focus blocker"},
                    "permission_target": "foreground_focus",
                    "risk_level": "low",
                },
            ]
        )
    if "screen_capture_blank" in conditions:
        actions.extend(
            [
                {
                    "label": "重新截图确认桌面画面",
                    "tool": "screen.capture",
                    "input": {"reason": "verify desktop is visible after blank screenshot"},
                    "permission_target": "desktop_screen_visible",
                    "risk_level": "low",
                },
                {
                    "label": "查看当前前台窗口",
                    "tool": "desktop.active_window",
                    "input": {},
                    "permission_target": "desktop_screen_visible",
                    "risk_level": "low",
                },
            ]
        )
    return actions


def _runtime_blocking_recovery_hints_for_conditions(conditions: list[str]) -> list[str]:
    hints: list[str] = []
    if "desktop_permission_diagnostics_not_checked" in conditions:
        hints.append(
            "Use desktop.permissions.verify only after explicit approval when you "
            "want a fresh interactive macOS permission check."
        )
    if "desktop_session_locked" in conditions:
        hints.append(
            "Unlock the active macOS user session, then approve desktop.permissions.verify "
            "or retry the foreground desktop action."
        )
    if "foreground_focus_unavailable" in conditions:
        hints.append(
            "The current runtime can observe apps but cannot bring the target app "
            "to the foreground; inspect active_window/screen evidence before retrying."
        )
    if "screen_capture_blank" in conditions:
        hints.append(
            "The current screen capture is blank/black; wake or unlock the desktop "
            "session and verify the remote display is visible before retrying."
        )
    return hints


def _ordered_unique(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        clean = str(value or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def _split_status(value: Any) -> tuple[str, str, str]:
    parts = str(value or "").strip().split("|", 2)
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def _looks_like_permission_error(value: Any) -> bool:
    if value.__class__.__name__ == "ScreenCapturePermissionError":
        return True
    normalized = str(value or "").lower()
    return any(
        marker in normalized
        for marker in (
            "not authorized",
            "not allowed",
            "not permitted",
            "accessibility",
            "automation",
            "privacy",
            "screen capture",
            "screen recording",
            "tcc",
        )
    )


def _with_permission_metadata(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not payload.get("permission_error"):
        return payload
    missing_permissions = _missing_permissions_for_action(action)
    permission_targets = _permission_targets_for_action(action)
    if missing_permissions:
        payload["missing_permissions"] = missing_permissions
    if permission_targets:
        payload["permission_targets"] = permission_targets
    if action == "screen.capture" and permission_targets:
        payload["recovery_requires_user_targets"] = permission_targets
    recovery_hints = _permission_recovery_hints_for_targets(permission_targets)
    if recovery_hints:
        payload["recovery_hints"] = recovery_hints
    recovery_actions = _permission_recovery_actions_for_targets(permission_targets)
    if recovery_actions:
        payload["recovery_actions"] = recovery_actions
    return payload


def _missing_permissions_for_action(action: str) -> list[str]:
    return {
        "screen.capture": ["screen_recording"],
        "desktop.active_window": ["automation_or_accessibility"],
        "desktop.running_apps": ["automation_or_accessibility"],
        "desktop.windows": ["automation_or_accessibility"],
        "desktop.list_windows": ["automation_or_accessibility"],
        "desktop.ui_elements": ["automation_or_accessibility"],
        "desktop.read_ui": ["automation_or_accessibility"],
        "desktop.inspect_app": ["automation_or_accessibility"],
        "desktop.verify": ["automation_or_accessibility"],
        "desktop.click_ui_element": ["automation_or_accessibility"],
        "desktop.type_into_ui_element": ["automation_or_accessibility"],
        "desktop.focus_app": ["automation"],
        "app.focus": ["automation"],
        "app.focus_window": ["automation", "accessibility"],
        "app.show": ["automation", "accessibility"],
        "app.hide": ["accessibility"],
        "app.minimize": ["accessibility"],
        "app.quit": ["automation"],
        "media.apple_music_play": ["music_app", "automation"],
        "media.apple_music_status": ["music_app", "automation"],
        "media.apple_music_open_and_play": ["music_app", "automation"],
        "media.apple_music_control": ["music_app", "automation"],
        "media.system_control": ["accessibility"],
        "media.music_app_open_and_play": ["accessibility", "open_command"],
        "media.music_app_control": ["accessibility"],
        "system.brightness": ["accessibility"],
        "notes.create": ["automation"],
        "reminders.create": ["automation"],
        "calendar.create_event": ["automation"],
        "desktop.hide_app": ["accessibility"],
        "desktop.show_all_apps": ["accessibility"],
        "desktop.minimize_window": ["accessibility"],
        "desktop.close_window": ["accessibility"],
        "desktop.quit_app": ["accessibility"],
        "desktop.safe_shortcut": ["accessibility"],
        "desktop.safe_key": ["accessibility"],
        "desktop.search_submit": ["accessibility"],
        "desktop.submit_foreground": ["accessibility"],
        "desktop.safe_type_text": ["accessibility"],
        "desktop.safe_click": ["accessibility"],
        "desktop.safe_scroll": ["accessibility"],
        "app.open_and_safe_type_text": ["accessibility", "open_command"],
        "app.focus_and_safe_type_text": ["accessibility", "automation"],
        "app.open_and_safe_shortcut": ["accessibility", "open_command"],
        "app.focus_and_safe_shortcut": ["accessibility", "automation"],
        "app.open_and_safe_key": ["accessibility", "open_command"],
        "app.focus_and_safe_key": ["accessibility", "automation"],
        "app.open_and_hotkey": ["accessibility", "open_command"],
        "app.focus_and_hotkey": ["accessibility", "automation"],
        "app.open_and_safe_scroll": ["accessibility", "open_command"],
        "app.focus_and_safe_scroll": ["accessibility", "automation"],
        "app.open_and_safe_click": ["accessibility", "open_command"],
        "app.focus_and_safe_click": ["accessibility", "automation"],
        "app.open_and_click_ui_element": ["automation_or_accessibility", "open_command"],
        "app.focus_and_click_ui_element": ["automation_or_accessibility", "automation"],
        "app.open_and_type_into_ui_element": ["automation_or_accessibility", "open_command"],
        "app.focus_and_type_into_ui_element": ["automation_or_accessibility", "automation"],
        "desktop.click": ["accessibility"],
        "desktop.type": ["accessibility"],
        "desktop.type_text": ["accessibility"],
        "desktop.shortcut": ["accessibility"],
        "desktop.hotkey": ["accessibility"],
        "osascript": ["automation"],
    }.get(action, [])


def _permission_targets_for_action(action: str) -> list[str]:
    return {
        "screen.capture": ["screen_recording"],
        "desktop.active_window": ["automation", "accessibility"],
        "desktop.running_apps": ["automation", "accessibility"],
        "desktop.windows": ["automation", "accessibility"],
        "desktop.list_windows": ["automation", "accessibility"],
        "desktop.ui_elements": ["automation", "accessibility"],
        "desktop.read_ui": ["automation", "accessibility"],
        "desktop.inspect_app": ["automation", "accessibility"],
        "desktop.verify": ["automation", "accessibility"],
        "desktop.focus_app": ["automation"],
        "app.focus": ["automation"],
        "app.focus_window": ["automation", "accessibility"],
        "app.show": ["automation", "accessibility"],
        "app.hide": ["accessibility"],
        "app.minimize": ["accessibility"],
        "app.quit": ["automation"],
        "media.apple_music_play": ["music_app", "automation"],
        "media.apple_music_status": ["music_app", "automation"],
        "media.apple_music_open_and_play": ["music_app", "automation"],
        "media.apple_music_control": ["music_app", "automation"],
        "media.system_control": ["accessibility"],
        "media.music_app_control": ["accessibility"],
        "system.brightness": ["accessibility"],
        "notes.create": ["automation"],
        "reminders.create": ["automation"],
        "calendar.create_event": ["automation"],
        "desktop.hide_app": ["accessibility"],
        "desktop.show_all_apps": ["accessibility"],
        "desktop.minimize_window": ["accessibility"],
        "desktop.close_window": ["accessibility"],
        "desktop.quit_app": ["accessibility"],
        "desktop.safe_shortcut": ["accessibility"],
        "desktop.safe_key": ["accessibility"],
        "desktop.search_submit": ["accessibility"],
        "desktop.submit_foreground": ["accessibility"],
        "desktop.safe_type_text": ["accessibility"],
        "desktop.safe_click": ["accessibility"],
        "desktop.safe_scroll": ["accessibility"],
        "app.open_and_safe_type_text": ["accessibility", "open_command"],
        "app.focus_and_safe_type_text": ["accessibility", "automation"],
        "app.open_and_safe_shortcut": ["accessibility", "open_command"],
        "app.focus_and_safe_shortcut": ["accessibility", "automation"],
        "app.open_and_safe_key": ["accessibility", "open_command"],
        "app.focus_and_safe_key": ["accessibility", "automation"],
        "app.open_and_hotkey": ["accessibility", "open_command"],
        "app.focus_and_hotkey": ["accessibility", "automation"],
        "app.open_and_safe_scroll": ["accessibility", "open_command"],
        "app.focus_and_safe_scroll": ["accessibility", "automation"],
        "app.open_and_safe_click": ["accessibility", "open_command"],
        "app.focus_and_safe_click": ["accessibility", "automation"],
        "app.open_and_click_ui_element": ["automation", "accessibility", "open_command"],
        "app.focus_and_click_ui_element": ["automation", "accessibility"],
        "app.open_and_type_into_ui_element": ["automation", "accessibility", "open_command"],
        "app.focus_and_type_into_ui_element": ["automation", "accessibility"],
        "desktop.click": ["accessibility"],
        "desktop.click_ui_element": ["automation", "accessibility"],
        "desktop.type_into_ui_element": ["automation", "accessibility"],
        "desktop.type": ["accessibility"],
        "desktop.type_text": ["accessibility"],
        "desktop.shortcut": ["accessibility"],
        "desktop.hotkey": ["accessibility"],
        "osascript": ["automation"],
    }.get(action, [])


def _permission_recovery_hints_for_targets(targets: list[str]) -> list[str]:
    hints_by_target = {
        "accessibility": (
            "Grant Accessibility permission to Oha-Yachiyo or the current terminal "
            "in macOS System Settings > Privacy & Security > Accessibility."
        ),
        "automation": (
            "Grant Automation permission so Oha-Yachiyo can control System Events "
            "or the target app in macOS System Settings > Privacy & Security > Automation."
        ),
        "automation_or_accessibility": (
            "Grant Automation and Accessibility permissions to Oha-Yachiyo or the current "
            "runtime in macOS System Settings > Privacy & Security."
        ),
        "foreground_focus": (
            "Allow the current Oha-Yachiyo runtime to bring target apps to the foreground. "
            "Check Automation and Accessibility permissions in macOS System Settings > Privacy & Security."
        ),
        "music_app": (
            "Open Music.app once, confirm the track exists in the local library, "
            "and allow Automation when macOS asks for Music control."
        ),
        "screen_recording": (
            "Grant Screen Recording permission to Oha-Yachiyo or the current terminal "
            "in macOS System Settings > Privacy & Security > Screen Recording."
        ),
        "screen_capture_probe_failed": (
            "Open Screen Recording permission in macOS System Settings and confirm "
            "Oha-Yachiyo or the current runtime is allowed."
        ),
        "input_monitoring": (
            "Grant Input Monitoring permission to Oha-Yachiyo or the current runtime "
            "in macOS System Settings > Privacy & Security > Input Monitoring."
        ),
        "full_disk_access": (
            "Grant Full Disk Access to Oha-Yachiyo or the current runtime in macOS "
            "System Settings > Privacy & Security > Full Disk Access."
        ),
        "files_and_folders": (
            "Grant Files and Folders permission to Oha-Yachiyo or the current runtime "
            "in macOS System Settings > Privacy & Security > Files and Folders."
        ),
        "microphone": (
            "Grant Microphone permission to Oha-Yachiyo or the current runtime in "
            "macOS System Settings > Privacy & Security > Microphone."
        ),
        "camera": (
            "Grant Camera permission to Oha-Yachiyo or the current runtime in macOS "
            "System Settings > Privacy & Security > Camera."
        ),
        "chrome_cdp": (
            "Open or configure Google Chrome with a reachable Chrome DevTools/CDP endpoint "
            "before retrying browser control."
        ),
        "open_command": (
            "macOS open command is unavailable in this environment, so local app launch "
            "cannot be recovered from System Settings."
        ),
        "unsupported_platform": (
            "Desktop execution is currently implemented for macOS in this runtime."
        ),
    }
    hints: list[str] = []
    for target in targets:
        hint = hints_by_target.get(str(target or "").strip())
        if hint and hint not in hints:
            hints.append(hint)
    return hints
