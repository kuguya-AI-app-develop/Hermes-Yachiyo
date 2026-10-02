"""Shared desktop intent parsing hints for planner snapshots and execution."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .app_name_hints import is_legacy_app_name_hint, legacy_music_app_name_hint
from .hotkey_hints import legacy_normalize_hotkey_token, legacy_parse_hotkey_combo

_GENERIC_MUSIC_QUERIES = {
    "",
    "music",
    "apple music",
    "音乐",
    "歌",
    "歌曲",
    "播放器",
    "音乐播放器",
    "状态",
    "播放状态",
    "播放进度",
    "在播状态",
    "进度",
    "播",
    "apple",
    "个",
    "点",
    "东西",
    "听听",
    "音乐听听",
    "点音乐",
    "一点音乐",
    "点歌",
    "一首歌",
    "首歌",
    "一下",
    "一首",
    "首",
    "吗",
    "嘛",
    "吧",
    "呢",
    "么",
    "some music",
    "youtube music",
    "something",
    "anything",
    "a song",
    "song",
    "songs",
}

_MEDIA_APP_NAME_PATTERN = (
    r"apple\s*music|苹果音乐|youtube\s*music|spotify|网易云|qq\s*音乐|qq music"
)

_SELECTED_DESKTOP_APP_NAME = "<selected app from desktop.list_apps>"

_FINDER_SAFE_SHORTCUT_PHRASES: tuple[tuple[str, str], ...] = (
    ("finder_quick_look", "快速查看"),
    ("finder_quick_look", "快速查看选中项"),
    ("finder_quick_look", "快速查看选中文件"),
    ("finder_quick_look", "快速预览"),
    ("finder_quick_look", "预览选中项"),
    ("finder_quick_look", "预览选中文件"),
    ("finder_quick_look", "按空格"),
    ("finder_quick_look", "按空格键"),
    ("finder_quick_look", "空格"),
    ("finder_quick_look", "space"),
    ("finder_quick_look", "pressspace"),
    ("new_folder", "新建文件夹"),
    ("new_folder", "新建一个文件夹"),
    ("new_folder", "创建文件夹"),
    ("new_folder", "创建一个文件夹"),
    ("new_folder", "新建目录"),
    ("new_folder", "创建目录"),
    ("new_folder", "newfolder"),
    ("new_folder", "makeanewfolder"),
    ("new_folder", "createanewfolder"),
    ("rename_selected", "重命名选中项"),
    ("rename_selected", "重命名选中文件"),
    ("rename_selected", "重命名当前选中项"),
    ("rename_selected", "重命名当前选中文件"),
    ("rename_selected", "renameselected"),
    ("rename_selected", "renameselectedfile"),
    ("parent_folder", "上一级"),
    ("parent_folder", "上一级文件夹"),
    ("parent_folder", "上一级目录"),
    ("parent_folder", "打开上一级文件夹"),
    ("parent_folder", "回到上级目录"),
    ("parent_folder", "parentfolder"),
    ("parent_folder", "openparentfolder"),
    ("finder_get_info", "显示简介"),
    ("finder_get_info", "查看简介"),
    ("finder_get_info", "显示选中文件简介"),
    ("finder_get_info", "显示选中项简介"),
    ("finder_get_info", "getinfo"),
    ("finder_get_info", "showinfo"),
    ("copy", "复制选中项"),
    ("copy", "复制选中文件"),
    ("copy", "复制选中文本"),
    ("copy", "复制当前选中项"),
    ("copy", "复制当前选中文件"),
    ("copy", "复制当前选中文本"),
    ("copy", "copyselectedfile"),
)

_NON_CONTENT_TYPE_TEXTS = {
    "进去",
    "进来",
    "里面",
    "里",
    "这里",
    "那里",
    "这儿",
    "那儿",
    "上去",
    "下去",
    "进去吧",
    "进去。",
}

_CLICK_ACTION_PATTERN = (
    r"(?:双击|点击|点一下|点按|单击|按一下|按(?!钮)|点(?!名)|"
    r"\b(?:double\s+click|click(?:ing)?|press(?:ing)?|tap(?:ping)?)\b)"
)
_TYPE_ACTION_PATTERN = (
    r"(?:帮我打(?!开)(?:字|上|入)?|打字|打上|打入|输入(?!框|栏)|键入|填写|填入|"
    r"写入|写下|记录下|记下|改成|改为|设为|设置为|填成|填为|写成|写为|"
    r"更新为|置为|"
    r"\b(?:type|typing|input(?:ting)?|enter(?:ing)?|fill(?:ing)?|write|writing|"
    r"set|change|update)\b)"
)
_OPEN_ACTION_PATTERN = (
    r"(?:打开|启动|开启|运行|拉起|开起来|开一下|开下|"
    r"\b(?:open(?:ing)?|launch(?:ing)?|start(?:ing)?(?:\s+up)?|run(?:ning)?)\b)"
)
_FOCUS_ACTION_PATTERN = (
    r"(?:切换到?|切到|切回|回到|切一下|切下|聚焦|激活|置前|"
    r"\b(?:focus(?:ing)?|switch(?:ing)?(?:\s+to)?|activate|activating|"
    r"bring(?:ing)?|go(?:ing)?\s+back\s+to|back\s+to)\b)"
)
_DESKTOP_MUTATION_ACTION_PATTERN = (
    rf"(?:{_CLICK_ACTION_PATTERN}|{_TYPE_ACTION_PATTERN}|{_OPEN_ACTION_PATTERN}|"
    rf"{_FOCUS_ACTION_PATTERN})"
)
_NEGATION_CUE_PATTERN = (
    r"(?:不要|不许|不能|不可|不必|不需要|不用|无需|无法|没法|没有办法|未能|"
    r"别|禁止|避免|请勿|勿)|"
    r"\b(?:do\s+not|don['’]t|dont|cannot|can\s+not|can['’]t|never|without|"
    r"could\s+not|couldn['’]t|will\s+not|won['’]t|"
    r"must\s+not|mustn['’]t|should\s+not|shouldn['’]t|"
    r"need\s+not|no\s+need\s+to|avoid|refrain\s+from)\b"
)


def _affirmative_action_text(text: str, action_pattern: str) -> str:
    """Remove only clauses whose matching desktop action is negated.

    Strong punctuation and explicit sequence/adversative words reset negation.
    Soft punctuation is intentionally retained so one negator still covers an
    enumeration such as ``不要输入、点击或切换焦点``.
    """

    value = str(text or "").strip()
    if not value:
        return ""
    separator_pattern = re.compile(
        r"[。；;！!？?\.\n]+|"
        r"(?:但是|不过|然而|而是|反而|然后|接着|随后|而后|之后再|但|而)|"
        r"\b(?:but|however|instead|then|afterwards|subsequently)\b|"
        rf"[，,](?=\s*(?:{_NEGATION_CUE_PATTERN}))|(?=\bwithout\b)",
        flags=re.IGNORECASE,
    )
    clauses: list[str] = []
    cursor = 0
    for separator in separator_pattern.finditer(value):
        clause = value[cursor : separator.start()]
        if clause.strip():
            clauses.extend(_split_negation_scope_commas(clause, action_pattern))
        cursor = separator.end()
    clause = value[cursor:]
    if clause.strip():
        clauses.extend(_split_negation_scope_commas(clause, action_pattern))
    kept: list[str] = []
    removed_negated_clause = False
    for clause in clauses:
        if _action_clause_is_negated(clause, action_pattern):
            removed_negated_clause = True
        else:
            kept.append(clause.strip(" ，,"))
    if not removed_negated_clause:
        return value
    return ". ".join(part for part in kept if part).strip()


def _split_negation_scope_commas(clause: str, action_pattern: str) -> list[str]:
    """Split a negated prefix from an explicit affirmative action after a comma."""

    value = str(clause or "")
    parts: list[str] = []
    cursor = 0
    for separator in re.finditer(r"[，,]", value):
        left = value[cursor : separator.start()]
        right = value[separator.end() :]
        if not _comma_starts_affirmative_action(left, right, action_pattern):
            continue
        if left.strip():
            parts.append(left)
        cursor = separator.end()
    tail = value[cursor:]
    if tail.strip():
        parts.append(tail)
    return parts


def _comma_starts_affirmative_action(
    left: str,
    right: str,
    action_pattern: str,
) -> bool:
    if not _has_effective_negation_cue(left):
        return False
    if not re.search(
        _DESKTOP_MUTATION_ACTION_PATTERN,
        left,
        flags=re.IGNORECASE,
    ):
        return False
    if re.search(
        rf"(?:[，,]\s*|\s+)(?:or\b|或|或者)\s*"
        rf"(?:{_DESKTOP_MUTATION_ACTION_PATTERN})",
        right,
        flags=re.IGNORECASE,
    ):
        return False
    head = re.split(r"[，,]", right, maxsplit=1)[0]
    if re.fullmatch(rf"\s*(?:{action_pattern})\s*", head, flags=re.IGNORECASE):
        return False
    action_scope = (
        r"(?:(?:在|到|向|于|用|通过)\s*[^.！!？?，,;]{0,32}?|"
        r"(?:in|into|inside|within)\s+(?:the\s+)?[^.！!？?,;]{0,32}?)?"
    )
    return bool(
        re.match(
            rf"\s*(?:(?:请|麻烦)(?:你|您)?\s*)?{action_scope}"
            rf"(?:{action_pattern})",
            right,
            flags=re.IGNORECASE,
        )
    )


def _has_effective_negation_cue(value: str) -> bool:
    return any(
        not _polite_request_negation_cue(value, match)
        for match in re.finditer(
            _NEGATION_CUE_PATTERN,
            value,
            flags=re.IGNORECASE,
        )
    )


def _action_clause_is_negated(clause: str, action_pattern: str) -> bool:
    matches = tuple(re.finditer(action_pattern, clause, flags=re.IGNORECASE))
    return bool(matches) and all(
        _action_match_is_negated(clause, match.start()) for match in matches
    )


def _action_match_is_negated(clause: str, action_start: int) -> bool:
    prefix = clause[:action_start]
    if re.search(r"(?:^|[^\w])不\s*(?:再\s*)?$", prefix):
        return True
    cue_matches = tuple(
        match
        for match in re.finditer(
            _NEGATION_CUE_PATTERN,
            prefix,
            flags=re.IGNORECASE,
        )
        if not _polite_request_negation_cue(prefix, match)
    )
    if not cue_matches:
        return False
    between = prefix[cue_matches[-1].end() :]
    if len(between) > 100:
        return False
    soft_separator = max(between.rfind("，"), between.rfind(","))
    if soft_separator >= 0:
        before_separator = between[:soft_separator]
        if not re.search(
            _DESKTOP_MUTATION_ACTION_PATTERN,
            before_separator,
            flags=re.IGNORECASE,
        ) and not re.search(r"(?:操作|交互|\binteract(?:ion|ing)?\b)", before_separator):
            return False
    return True


def _polite_request_negation_cue(prefix: str, match: re.Match[str]) -> bool:
    """Exclude Chinese can-you phrasing from destructive negation scope."""

    token = match.group(0)
    previous = prefix[match.start() - 1 : match.start()] if match.start() else ""
    if not ((token == "不能" and previous == "能") or (token == "不可" and previous == "可")):
        return False
    request_start = match.start() - 1
    request_prefix = prefix[:request_start].rstrip()
    if not request_prefix or re.search(r"[，,。！!？?；;\n]$", request_prefix):
        return True
    return bool(
        re.search(
            r"(?:^|[，,。！!？?；;\n\s])"
            r"(?:请问(?:一下|下)?|请(?:你|您)?|"
            r"麻烦(?:(?:你|您)|问(?:一下|下)?)?|劳驾|帮我|"
            r"我|你|您|(?:我)?想问(?:一下)?)$",
            request_prefix,
        )
    )


def affirmative_desktop_action_text(text: str, action: str) -> str:
    """Return text with negated clauses for one desktop mutation removed."""

    action_pattern = {
        "click": _CLICK_ACTION_PATTERN,
        "type": _TYPE_ACTION_PATTERN,
        "open": _OPEN_ACTION_PATTERN,
        "focus": _FOCUS_ACTION_PATTERN,
    }.get(str(action or "").strip().lower())
    if not action_pattern:
        return str(text or "").strip()
    return _affirmative_action_text(text, action_pattern)


def desktop_action_requested(text: str, action: str) -> bool:
    """Return whether a supported desktop mutation has an affirmative mention."""

    action_pattern = {
        "click": _CLICK_ACTION_PATTERN,
        "type": _TYPE_ACTION_PATTERN,
        "open": _OPEN_ACTION_PATTERN,
        "focus": _FOCUS_ACTION_PATTERN,
    }.get(str(action or "").strip().lower())
    if not action_pattern:
        return False
    affirmative_text = affirmative_desktop_action_text(text, action)
    return bool(re.search(action_pattern, affirmative_text, flags=re.IGNORECASE))


def desktop_app_control_only_negated(text: str) -> bool:
    """Return true when every mentioned open/focus action is explicitly negated."""

    return _desktop_actions_only_negated(text, ("open", "focus"))


def desktop_mutation_only_negated(text: str) -> bool:
    """Return true when every mentioned desktop mutation is explicitly negated."""

    return _desktop_actions_only_negated(text, ("click", "type", "open", "focus"))


def _desktop_actions_only_negated(text: str, actions: Iterable[str]) -> bool:
    patterns = {
        "click": _CLICK_ACTION_PATTERN,
        "type": _TYPE_ACTION_PATTERN,
        "open": _OPEN_ACTION_PATTERN,
        "focus": _FOCUS_ACTION_PATTERN,
    }
    mentioned_actions = tuple(
        action
        for action in actions
        if (pattern := patterns.get(str(action or "").strip().lower()))
        if re.search(pattern, str(text or ""), flags=re.IGNORECASE)
    )
    return bool(mentioned_actions) and not any(
        desktop_action_requested(text, action) for action in mentioned_actions
    )


def app_control_mode(text: str) -> str:
    text = _affirmative_action_text(text, _FOCUS_ACTION_PATTERN)
    return (
        "focus"
        if contains_any(
            text,
            [
                "切换到",
                "切到",
                "切回",
                "回到",
                "切一下",
                "切下",
                "聚焦",
                "激活",
                "置前",
                "focus",
                "switch to",
                "switch ",
                "activate ",
                "bring ",
                "go back to",
                "switch back to",
                "back to",
            ],
        )
        else "open"
    )


def app_control_tool_candidates(mode: str) -> tuple[str, ...]:
    if mode == "focus":
        return ("app.focus", "desktop.focus_app", "app.open", "desktop.open_app")
    return ("app.open", "desktop.open_app", "app.focus", "desktop.focus_app")


def app_management_tool_candidates(action: str) -> tuple[str, ...]:
    return {
        "status": ("app.status",),
        "show": ("app.show",),
        "hide": ("app.hide", "desktop.hide_app"),
        "minimize": ("app.minimize", "desktop.minimize_window"),
        "quit": ("app.quit", "desktop.quit_app"),
    }.get(str(action or "").strip(), ())


def app_foreground_tool_candidates(mode: str, action: str) -> tuple[str, ...]:
    prefix = "focus" if mode == "focus" else "open"
    alternate = "open" if prefix == "focus" else "focus"
    return (f"app.{prefix}_and_{action}", f"app.{alternate}_and_{action}")


def click_target_hint(text: str) -> dict[str, Any] | None:
    value = _affirmative_action_text(text, _CLICK_ACTION_PATTERN)
    if safe_click_hint(value) is not None:
        return None
    if _looks_like_audio_level_request(value):
        return None
    conditional_click_request = bool(
        re.search(
            r"(?:判断.{0,12}(?:点击|点按|点|按|操作)|"
            r"能否.{0,8}(?:点击|点按|点|按|操作)|"
            r"是否(?:可以)?.{0,8}(?:点击|点按|点|按|操作)|"
            r"可不可以.{0,8}(?:点击|点按|点|按|操作)|"
            r"能不能.{0,8}(?:点击|点按|点|按|操作)|"
            r"如果.{0,12}(?:点击|点按|点|按|操作))",
            value,
            flags=re.IGNORECASE,
        )
    )
    direct_click_request = bool(
        not conditional_click_request
        and (
            re.search(
                r"(?:双击|点击|点一下|点按|单击|按一下|点(?!名))\s*"
                r"(?:可见(?:的)?|当前(?:的)?|这个|该)?[^。！？!?，,]{1,60}?"
                r"(?:按钮|控件|元素|菜单项|菜单|复选框)",
                value,
                flags=re.IGNORECASE,
            )
            or re.search(
                r"(?:^|[，,]|并|然后|再|接着|之后|后)\s*"
                r"(?:双击|点击|点一下|点按|单击|按一下|点(?!名))\s*"
                r"[^。！？!?，,]{1,60}$",
                value,
                flags=re.IGNORECASE,
            )
            or re.search(
                r"(?:找到|找|定位|选择|选中)\s*[^。！？!?，,]{1,60}?"
                r"(?:按钮|控件|元素|菜单项|菜单|复选框|项目|条目)?\s*"
                r"(?:并|然后|再|之后|后)?\s*"
                r"(?:双击|点击|点一下|点按|单击|打开|进入)",
                value,
                flags=re.IGNORECASE,
            )
        )
    )
    if _looks_like_ui_click_advice_request(value) and not direct_click_request:
        return None
    if re.search(
        r"\b(?:press|hit|tap)\s+(?:command|cmd|control|ctrl|option|alt|shift)\b",
        value,
        flags=re.IGNORECASE,
    ) or re.search(
        r"(?:按|敲).{0,6}(?:command|cmd|⌘|control|ctrl|option|alt|shift)",
        value,
        flags=re.IGNORECASE,
    ):
        return None
    patterns = (
        r"(?:找到|找|定位|选择|选中)\s*(?P<target_find>[^。！？!?，,]+?)"
        r"(?:按钮|控件|元素|菜单项|菜单|复选框|项目|条目)?\s*"
        r"(?:并|然后|再|之后|后)?\s*(?:双击|点击|点一下|点按|单击|打开|进入)"
        r"(?:[，,。；;！!？?]?\s*(?:并|然后|再|后|之后)?\s*"
        r"(?:确认|验证|检查|查看|看看|判断).{0,24}"
        r"(?:成功|结果|状态|是否成功))?$",
        r"(?:find|locate|choose|select)\s+(?:the\s+)?(?P<target_find_en>[^.!?,]+?)\s*"
        r"(?:and\s+then|then|and)?\s*(?:click|press|tap|open)\s+(?:it|that|them)?"
        r"(?:\s+(?:and|then)\s+(?:verify|confirm|check).{0,24}"
        r"(?:success|result|state|status))?$",
        r"(?P<target_post>[^。！？!?，,]{1,60}?)(?:按钮|控件|元素|菜单项|菜单|复选框)?\s*(?:双击|点击|点一下|点按|单击)$",
        r"(?:双击|点击|点一下|点按|单击|按一下|按(?!钮)|点(?!击|按|一下|名))\s*(?P<target>[^。！？!?，,]+)",
        r"(?:double\s+click|click|press|tap)\s+(?:the\s+)?(?P<target_en>[^.!?,]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        if conditional_click_request:
            continue
        raw_target = (
            match.groupdict().get("target")
            or match.groupdict().get("target_post")
            or match.groupdict().get("target_en")
            or match.groupdict().get("target_find")
            or match.groupdict().get("target_find_en")
            or ""
        )
        target = clean_target(raw_target)
        if not target and re.search(
            r"(?:按钮|button)\s*$",
            raw_target,
            flags=re.IGNORECASE,
        ):
            target = re.sub(
                r"\s*(?:按钮|button)\s*$",
                "",
                raw_target,
                flags=re.IGNORECASE,
            ).strip(" .，,。")
        if _looks_like_keyboard_key_target(raw_target, target):
            continue
        if not target:
            continue
        return {
            "target": target,
            "role_filter": role_filter(match.group(0)),
            "click_count": 2 if contains_any(match.group(0), ["双击", "double click"]) else 1,
        }
    return None


def type_into_ui_hint(text: str, *, app_name: str = "") -> dict[str, Any] | None:
    original_text = str(text or "")
    text = _affirmative_action_text(original_text, _TYPE_ACTION_PATTERN)
    removed_negated_clause = clean(text) != clean(original_text)
    # Recovery controls serialize the exact selected label and payload. Decode
    # those strings as operands, so punctuation and action words stay data.
    json_string = r'"(?:[^"\\]|\\.)*"'
    selected_field = re.search(
        rf"(?:在|向)前台控件\s*(?P<target>{json_string})\s*"
        rf"输入\s*(?P<text>{json_string})",
        text,
    )
    if selected_field:
        try:
            target = json.loads(selected_field.group("target"))
            typed_text = json.loads(selected_field.group("text"))
        except json.JSONDecodeError:
            return None
        if target and typed_text:
            return {"target": target, "text": typed_text, "role_filter": "text"}
    field_cn = (
        r"搜索框|搜索栏|消息框|聊天框|地址栏|输入框|文本框|输入栏|"
        r"收件人|发件人|联系人|主题|标题|姓名|名称|邮箱|邮件地址|电话|"
        r"用户名|账号|账户|密码"
    )
    field_en = (
        r"search box|search field|message field|address bar|input field|"
        r"text box|input|field|title(?: field)?|name(?: field)?|"
        r"email(?: field| address)?|phone(?: field| number)?|username|"
        r"account|password|subject|recipient|sender|contact"
    )
    patterns = (
        r"(?:把|将)?\s*(?:单元格|cell)?\s*(?P<cell>[A-Z]{1,3}\d{1,7})\s*"
        r"(?:改成|改为|设为|设置为|填成|填为|写成|写为|更新为|置为|=)\s*"
        r"(?P<cell_text>[^。！？!?，,]+)",
        r"(?:set|change|update|fill|write)\s+(?:cell\s+)?"
        r"(?P<cell_en>[A-Z]{1,3}\d{1,7})\s+(?:to|as|=)\s*"
        r"(?P<cell_text_en>[^.!?,]+)",
        rf"(?:把|将)\s*(?P<target_update_cn>{field_cn})\s*"
        r"(?:改成|改为|设为|设置为|填成|填为|写成|写为|更新为|置为|=)\s*"
        r"(?P<text_update_cn>[^。！？!?，,]+)",
        rf"(?P<target_update_plain_cn>{field_cn})\s*"
        r"(?:改成|改为|设为|设置为|填成|填为|写成|写为|更新为|置为|=)\s*"
        r"(?P<text_update_plain_cn>[^。！？!?，,]+)",
        r"(?:set|change|update|fill|write)\s+(?:the\s+)?"
        rf"(?P<target_update_en>{field_en})\s+(?:to|as|=)\s*"
        r"(?P<text_update_en>[^.!?,]+)",
        r"(?P<target>[^。！？!?，,]{1,40}?(?:搜索框|搜索栏|消息框|聊天框|地址栏|输入框|文本框|输入栏|"
        r"收件人|发件人|联系人|主题|标题|姓名|名称|邮箱|邮件地址|电话|用户名|账号|账户|密码))"
        r"\s*(?:输入|键入|填写|填入|写入|写)\s*(?P<text>[^。！？!?，,]+)",
        r"(?:type|enter|fill)\s+(?P<text_en2>[^.!?,]+?)\s+"
        r"(?:into|in|inside)\s+(?:the\s+)?"
        r"(?P<target_en2>[^.!?,]{1,40}?(?:search box|search field|message field|address bar|input|field|text box))",
        r"(?P<target_en>[^.!?,]{1,40}?(?:search box|search field|message field|address bar|input|field|text box))\s*(?:type|enter|fill)\s*(?P<text_en>[^.!?,]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        raw_target = (
            match.groupdict().get("cell")
            or match.groupdict().get("cell_en")
            or ""
        )
        if raw_target:
            raw_text = (
                match.groupdict().get("cell_text")
                or match.groupdict().get("cell_text_en")
                or ""
            )
            target = clean_type_target(raw_target, app_name=app_name)
            typed_text = clean_followup_text(raw_text)
            if target and typed_text:
                return {
                    "target": target.upper(),
                    "text": typed_text,
                    "role_filter": "text",
                }
            continue

        raw_target = (
            match.groupdict().get("target_update_cn")
            or match.groupdict().get("target_update_plain_cn")
            or match.groupdict().get("target_update_en")
            or ""
        )
        if raw_target:
            raw_text = (
                match.groupdict().get("text_update_cn")
                or match.groupdict().get("text_update_plain_cn")
                or match.groupdict().get("text_update_en")
                or ""
            )
            target = clean_type_target(raw_target, app_name=app_name)
            typed_text = clean_followup_text(raw_text)
            if target and typed_text:
                return {"target": target, "text": typed_text, "role_filter": "text"}
            continue

        raw_target = (
            match.groupdict().get("target")
            or match.groupdict().get("target_en")
            or match.groupdict().get("target_en2")
            or ""
        )
        raw_text = (
            match.groupdict().get("text")
            or match.groupdict().get("text_en")
            or match.groupdict().get("text_en2")
            or ""
        )
        target = clean_type_target(raw_target, app_name=app_name)
        if removed_negated_clause:
            target = _explicit_type_field_label(raw_target) or target
        typed_text = clean_followup_text(raw_text)
        if _looks_like_current_input_target(raw_target, target):
            continue
        if target and typed_text:
            return {"target": target, "text": typed_text, "role_filter": "text"}
    return None


def _explicit_type_field_label(value: str) -> str:
    match = re.search(
        r"(?:搜索框|搜索栏|消息框|聊天框|地址栏|输入框|文本框|输入栏|"
        r"search box|search field|message field|address bar|input field|text box)$",
        clean(value),
        flags=re.IGNORECASE,
    )
    return match.group(0) if match else ""


def safe_type_text_hint(text: str) -> str:
    text = _affirmative_action_text(text, _TYPE_ACTION_PATTERN)
    patterns = (
        r"^(?:请|麻烦)?(?:帮我打(?:字|上|入)?|打字|打上|打入)\s+"
        r"(?P<text_help>[^。！？!?，,]+)",
        r"(?:输入(?!框|栏)|键入|填写|填入|写入|写下|记录下|记下|写)\s*(?P<text>[^。！？!?，,]+)",
        r"\b(?:type|enter|fill)(?:\s+|\s*[:：]\s*)(?P<text_en>[^.!?,]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        typed_text = clean_followup_text(
            match.groupdict().get("text_help")
            or match.groupdict().get("text")
            or match.groupdict().get("text_en")
            or ""
        )
        typed_text = re.sub(
            r"^(?:文本|文字|内容|text)\s+",
            "",
            typed_text,
            flags=re.IGNORECASE,
        ).strip()
        if re.fullmatch(
            r"(?:in|into)\s+(?:the\s+)?"
            r"(?:(?:current|active|foreground)\s+)?"
            r"(?:input|input\s+field|field|text\s+box)",
            typed_text,
            flags=re.IGNORECASE,
        ):
            continue
        typed_text = re.sub(
            r"\s+(?:in|into)\s+(?:the\s+)?"
            r"(?:(?:current|active|foreground)\s+)?"
            r"(?:input|input\s+field|field|text\s+box)$",
            "",
            typed_text,
            flags=re.IGNORECASE,
        ).strip()
        if _looks_like_non_content_type_text(typed_text):
            continue
        if typed_text:
            return typed_text
    return ""


def standalone_safe_type_text_hint(text: str) -> str:
    """Return literal text only for a whole-utterance foreground typing command.

    ``safe_type_text_hint`` intentionally remains broad because app-scoped flows use
    it to extract text from larger instructions.  A bare foreground mutation needs a
    stricter boundary: otherwise words such as ``输入`` or ``写`` inside a code,
    report, communication, or click request can turn the entire task into an
    unapproved typing action.
    """

    value = _affirmative_action_text(text, _TYPE_ACTION_PATTERN)
    if not value:
        return ""
    match = re.fullmatch(
        r"(?:请|麻烦)?\s*"
        r"(?:(?:在)?(?:当前|前台)(?:界面|输入框|文本框|应用|窗口)"
        r"(?:里|中|上)?\s*)?"
        r"(?:帮我打(?:字|上|入)?|打字|打上|打入|输入|键入|"
        r"type|enter)"
        r"(?:\s+|\s*[:：]\s*|(?=(?:文本|文字|内容)\s+))"
        r"(?P<text>[^\r\n。！？!?，,；;]+?)\s*[.!。]?",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    typed_text = clean_followup_text(match.group("text"))
    typed_text = re.sub(
        r"^(?:文本|文字|内容|text)\s+",
        "",
        typed_text,
        flags=re.IGNORECASE,
    ).strip()
    typed_text = re.sub(
        r"\s+(?:in|into)\s+(?:the\s+)?"
        r"(?:(?:current|active|foreground)\s+)?"
        r"(?:input|input\s+field|field|text\s+box)$",
        "",
        typed_text,
        flags=re.IGNORECASE,
    ).strip()
    if not typed_text or _looks_like_non_content_type_text(typed_text):
        return ""
    if _standalone_type_text_has_task_semantics(typed_text):
        return ""
    return typed_text


def _standalone_type_text_has_task_semantics(text: str) -> bool:
    """Reject payloads that read like another task rather than literal keystrokes."""

    value = str(text or "").strip()
    lowered = value.lower()
    task_patterns = (
        r"(?:一份|一个|一段|这份|这个|这段)\s*"
        r"(?:代码|脚本|程序|报告|报表|总结|摘要|文档|文件|笔记|任务)",
        r"(?:生成|创建|制作|撰写|改写|翻译|总结|整理).{0,24}"
        r"(?:代码|脚本|程序|报告|报表|摘要|文档|文件|笔记|任务)",
        r"(?:发送|发给|发消息|回复|发邮件|打电话|打给).{0,40}",
        r"(?:点击|单击|点按|选择|按下).{0,24}(?:按钮|控件|链接|菜单)?",
        r"(?:剪贴板|当前网页|当前页面|选中的?内容).{0,30}"
        r"(?:写入|填入|输入|发送|保存|整理|总结)",
    )
    if any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in task_patterns):
        return True
    return bool(
        re.search(
            r"\b(?:generate|create|compose|summari[sz]e|rewrite|translate|send|"
            r"email|message|call|click|press|select|paste)\b.{0,48}"
            r"\b(?:code|script|report|artifact|document|file|note|task|button|"
            r"message|email|clipboard|page)?\b",
            lowered,
            flags=re.IGNORECASE,
        )
    )


def _looks_like_non_content_type_text(text: str) -> bool:
    value = clean(text)
    return value in _NON_CONTENT_TYPE_TEXTS


def _looks_like_audio_level_request(text: str) -> bool:
    value = clean(text)
    lowered = value.lower()
    return bool(
        re.search(
            r"(?:音量|声音|volume|sound|大点声|大一点声|小点声|小一点声|"
            r"别出声|静音|放大音量|缩小音量|调大|调小|调高|调低|"
            r"volume\s+up|volume\s+down|sound\s+up|sound\s+down|louder|quieter)",
            lowered,
            flags=re.IGNORECASE,
        )
    )


def _looks_like_type_field_opener_name(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:打开|启动|开启|切到|聚焦|open|launch|focus|switch\s+to)",
            clean(text),
            flags=re.IGNORECASE,
        )
    )


def _looks_like_keyboard_key_target(raw_target: str, clean_target_value: str) -> bool:
    raw = clean(raw_target).lower()
    target = clean(clean_target_value).lower()
    keyboard_targets = {
        "回车",
        "回车键",
        "enter",
        "return",
        "esc",
        "escape",
        "tab",
        "制表",
        "制表键",
        "上方向键",
        "下方向键",
        "左方向键",
        "右方向键",
        "向上箭头",
        "向下箭头",
        "向左箭头",
        "向右箭头",
        "up",
        "down",
        "left",
        "right",
        "up arrow",
        "down arrow",
        "left arrow",
        "right arrow",
    }
    return bool(raw in keyboard_targets or target in keyboard_targets)


def _looks_like_current_input_target(raw_target: str, clean_target_value: str) -> bool:
    raw = clean(raw_target)
    target = clean(clean_target_value)
    if target.lower() in {
        "当前",
        "现在",
        "前台",
        "这个",
        "该",
        "current",
        "active",
        "foreground",
        "frontmost",
        "this",
    }:
        return True
    return bool(
        re.fullmatch(
            r"(?:在|到|往|向)?\s*(?:当前|现在|前台|这个|该)\s*"
            r"(?:输入框|输入栏|文本框|消息框|聊天框|input|field|text\s*box)",
            raw,
            flags=re.IGNORECASE,
        )
        or re.fullmatch(
            r"(?:the\s+)?(?:current|active|foreground|frontmost|this)\s*"
            r"(?:input|field|text\s*box|message\s*field)",
            raw,
            flags=re.IGNORECASE,
        )
    )


def submit_action_hint(text: str) -> str:
    value = str(text or "")
    lowered = value.lower()
    if "发送" in value or re.search(r"\bsend\b", lowered):
        return "send"
    if contains_any(value, ["搜索", "回车", "确认", "提交"]) or re.search(
        r"\b(?:search|enter|return|confirm|submit)\b",
        lowered,
    ):
        return "confirm"
    return ""


def window_list_hint(text: str) -> dict[str, str] | None:
    value = clean(text)
    lowered = value.lower()
    if not re.search(r"(?:窗口|windows?)", value, flags=re.IGNORECASE):
        return None
    if re.search(
        r"(?:当前|现在|这个|前台|该)?(?:窗口|window)"
        r".{0,8}(?:内容|文字|文本|正文|content|text)",
        value,
        flags=re.IGNORECASE,
    ):
        return None
    if re.search(
        r"(?:当前|现在|这个|前台|该)?(?:窗口|界面|屏幕|应用|app|ui|window|interface|screen)"
        r".{0,16}(?:控件|按钮|输入框|文本框|元素|选项|ui|可点击|可操作|"
        r"buttons?|controls?|ui\s+elements?|text\s+fields?)",
        value,
        flags=re.IGNORECASE,
    ):
        return None
    if _looks_like_current_window_observation(value, lowered):
        return None
    patterns = (
        r"(?:list|show|read)\s+(?:open\s+)?windows\s+(?:in|for|of)\s+(?P<app_en>[^.!?]+)",
        r"(?:what|which)\s+(?:open\s+)?windows\s+(?:are\s+)?(?:open\s+)?"
        r"(?:in|for|of)\s+(?P<app_en_question>[^.!?]+)",
        r"(?P<app_en_prefix>[^.!?]+?)\s+(?:list|show|read)\s+(?:open\s+)?windows",
        r"(?:list|show|read)\s+(?P<app_en2>[^.!?]+?)\s+windows",
        r"(?P<app_en3>[^.!?]+?)\s+(?:open\s+)?windows(?:\?|$)",
        r"(?P<app>[^。！？!?，,]+?)\s*(?:的)?\s*(?:窗口|windows?)\s*(?:列表|清单|list)$",
        r"(?P<app_question>[^。！？!?，,]+?)\s*(?:有|打开了|开了|正在显示)?"
        r"(?:哪些|什么|几个|多少).{0,4}(?:窗口|window)",
        r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?(?:列出|查看|看看|看一下|看下|显示|读取)\s*"
        r"(?P<app2>[^。！？!?，,]{1,40}?)\s*(?:的)?\s*(?:窗口|windows?)",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        raw_app = next(
            (
                item
                for item in match.groupdict().values()
                if item is not None and str(item).strip()
            ),
            "",
        )
        app_name = _clean_window_app_name_hint(raw_app)
        return {"app_name": app_name} if app_name else {}
    if re.search(
        r"(?:列出|查看|看看|看一下|看下|显示|读取).{0,12}(?:窗口|windows?)|"
        r"(?:窗口|windows?).{0,8}(?:列表|清单|列出|列一下|列下)|"
        r"\b(?:list|show|read)\s+(?:open\s+)?windows\b",
        value,
        flags=re.IGNORECASE,
    ):
        return {}
    return None


def _looks_like_current_window_observation(value: str, lowered: str) -> bool:
    if re.search(
        r"(?:列表|清单|所有|全部|哪些|几个|多少|list|all|windows)",
        value,
        flags=re.IGNORECASE,
    ):
        return False
    return bool(
        re.search(
            r"^(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:打开|启动|切到|聚焦)?\s*[^。！？!?，,]{1,40}?\s*"
            r"(?:查看|看看|看一下|看下|看|显示|读取)\s*"
            r"(?:当前|现在|前台|这个|该)\s*(?:窗口|window)$",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"^(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:查看|看看|看一下|看下|显示|读取)?\s*"
            r"(?:当前|现在|前台|这个|该)\s*(?:窗口|window)"
            r"\s*(?:是什么|是啥|哪个|什么|标题|名称|名字)?"
            r"(?:一下|下|可以吗|好吗|好么|行吗|吗|嘛|吧|呢)?$",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:show|read|inspect|look\s+at|check)\s+"
            r"(?:the\s+)?(?:current|active|foreground|frontmost|this)\s+window\b",
            lowered,
        )
    )


def focus_window_hint(text: str) -> dict[str, str] | None:
    value = clean(text)
    if re.search(
        r"^(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:切换到|切到|聚焦|激活|置前)\s*"
        r"(?:(?:一个|一款|任意|任何|默认|可用|正在运行|运行中|已打开|打开的)(?:的)?\s*)*"
        r"(?:浏览器|browser)\s*(?:窗口|window)$",
        value,
        flags=re.IGNORECASE,
    ):
        return None
    patterns = (
        r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:切换到|切到|聚焦|激活|置前)\s*(?P<app>[^。！？!?，,]+?)"
        r"\s*的\s*(?:标题(?:包含|为)?|名为|叫)?\s*"
        r"(?P<title>[^。！？!?，,]+?)\s*(?:窗口|window)$",
        r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:切换到|切到|聚焦|激活|置前)\s*(?P<app>[^。！？!?，,]+?)"
        r"\s*(?:标题(?:包含|为)?|名为|叫)\s*"
        r"(?P<title>[^。！？!?，,]+?)\s*(?:窗口|window)$",
        r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:切换到|切到|聚焦|激活|置前)\s*(?P<app>[^。！？!?，,\s]+?)"
        r"\s+(?P<title>[^。！？!?，,]+?)\s*(?:窗口|window)$",
        r"\b(?:focus|activate|switch to)\s+(?P<app_en>.+?)\s+window\s+"
        r"(?:(?:titled|called|matching|containing)\s+)?(?P<title_en>[^.!?]+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        raw_app = match.groupdict().get("app") or match.groupdict().get("app_en") or ""
        raw_title = match.groupdict().get("title") or match.groupdict().get("title_en") or ""
        app_name = _clean_window_app_name_hint(raw_app)
        title = _clean_window_title_hint(raw_title)
        if app_name and title:
            return {"app_name": app_name, "title_contains": title}
    return None


def ui_inspection_hint(text: str) -> dict[str, Any] | None:
    value = clean(text)
    lowered = value.lower()
    presence = ui_control_presence_hint(value)
    if presence:
        return presence
    if not _looks_like_ui_inspection_request(value, lowered):
        return None
    payload: dict[str, Any] = {
        "role_filter": _ui_role_filter_hint(value),
        "limit": 80,
    }
    app_name = _ui_inspection_app_name_hint(value)
    if app_name:
        payload["app_name"] = app_name
    return payload


def screen_capture_hint(text: str) -> dict[str, Any] | None:
    value = clean(text)
    lowered = value.lower()
    if re.search(r"(?:截图工具|截图面板|屏幕截图工具|screenshot\s*(?:tool|toolbar|panel))", value, flags=re.IGNORECASE):
        return None
    if _looks_like_screenshot_file_reference(value, lowered):
        return None
    if (
        click_target_hint(value) is not None
        and _explicit_find_click_target_requested(value)
        and not _explicit_screen_capture_requested(value, lowered)
    ):
        return None
    if not _looks_like_screen_capture_request(value, lowered):
        return None
    payload: dict[str, Any] = {"reason": "user asked to capture the screen"}
    app_name = _screen_capture_app_name_hint(value)
    if app_name:
        payload["app_name"] = app_name
    return payload


def _looks_like_screenshot_file_reference(value: str, lowered: str) -> bool:
    return bool(
        re.search(
            r"(?:刚才(?:的)?|最近|最新|上一张|上一个|下载(?:文件夹|目录)?|桌面|文件夹|目录)"
            r".{0,18}(?:截图|截屏)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:截图|截屏).{0,18}"
            r"(?:文件|图片|照片|重命名|改名|整理|移动|归档|删除|复制|压缩)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:recent|latest|downloads?|desktop|folder|directory).{0,24}"
            r"(?:screenshot|screen\s*shot)",
            lowered,
        )
        or re.search(
            r"(?:screenshot|screen\s*shot).{0,24}"
            r"(?:file|image|photo|rename|move|organize|archive|delete|copy|compress)",
            lowered,
        )
    )


def app_management_hint(text: str) -> dict[str, str] | None:
    value = clean(text)
    if foreground_management_hint(value):
        return None
    compound_hint = _compound_app_management_hint(value)
    if compound_hint:
        return compound_hint
    if _looks_like_open_then_foreground_confirmation(value):
        return None
    patterns: tuple[tuple[str, str], ...] = (
        (
            "hide_other_apps",
            r"^(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:隐藏|hide)\s*(?:其他|其它|其余|别的|other)\s*(?:应用|app|apps|applications)$",
        ),
        (
            "status",
            r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:检查一下|检查|看看|看一下|查看|确认)?\s*"
            r"(?P<app_status>[^。！？!?，,]+?)\s*(?:是否)?"
            r"(?:在运行|运行中|运行|开着|开没开)"
            r"(?:吗|嘛|呢|吧|么|\?|？)?$",
        ),
        (
            "status",
            r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:检查一下|检查|看看|看一下|查看|确认)?\s*"
            r"(?P<app_status_open_whether>[^。！？!?，,]+?)\s*"
            r"(?:是否|有没有|有无)(?:已经)?\s*(?:打开了|开了|打开|开启)"
            r"(?:吗|嘛|呢|吧|么|\?|？)?$",
        ),
        (
            "status",
            r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:检查一下|检查|看看|看一下|查看|确认)?\s*"
            r"(?P<app_status_open>[^。！？!?，,]+?)\s*(?:是否)?"
            r"(?:打开了|开了|打开|开启)(?:吗|嘛|呢|吧|么|\?|？)$",
        ),
        (
            "status",
            r"(?:is|check\s+if|see\s+if|verify(?:\s+that)?|confirm(?:\s+that)?)\s+"
            r"(?P<app_status_en>[^.!?]+?)\s+"
            r"(?:is\s+)?(?:running|open)(?:\s+please)?$",
        ),
        (
            "status",
            r"(?:check|see)\s+whether\s+(?P<app_status_whether_en>[^.!?]+?)\s+"
            r"(?:is\s+)?(?:running|open)(?:\s+please)?$",
        ),
        (
            "show",
            r"^(?:你能(?:不能)?(?:帮我)?|你可以(?:帮我)?|能否帮我|能不能帮我|可以帮我|帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:显示|显示一下|显示出来|调出来|叫出来|还原|恢复|取消隐藏|show|restore|unhide)\s*"
            r"(?P<app>[^。！？!?，,]+)",
        ),
        (
            "show",
            r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:打开|启动|开启|拉起)\s*(?P<app_front_open>[^。！？!?，,]+?)\s*"
            r"(?:并|然后|再)?\s*(?:切到|到)?前台$",
        ),
        (
            "show",
            r"(?P<app2>[^。！？!?，,]+?)\s*"
            r"(?:显示出来|调出来|还原|恢复|取消隐藏|叫出来|show|restore|unhide)$",
        ),
        (
            "hide",
            r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:隐藏一下|隐藏|藏起来|收起来|收起|hide)\s*"
            r"(?P<app3>[^。！？!?，,]+)",
        ),
        (
            "hide",
            r"(?P<app4>[^。！？!?，,]+?)\s*"
            r"(?:隐藏|藏起来|收起来|收起|hide)(?:一下|下)?$",
        ),
        (
            "minimize",
            r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?(?:把|将)?\s*"
            r"(?P<app5>[^。！？!?，,]+?)\s*(?:最小化|minimi[sz]e)(?:一下|下)?$",
        ),
        (
            "minimize",
            r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:最小化|minimi[sz]e)\s*(?P<app6>[^。！？!?，,]+)",
        ),
        (
            "quit",
            r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:退出|关闭|关掉|结束|终止|quit|close|exit|terminate)\s*"
            r"(?P<app7>[^。！？!?，,]+)",
        ),
        (
            "quit",
            r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?(?:把|将)?\s*"
            r"(?P<app8>[^。！？!?，,]+?)\s*(?:退出|关闭|关掉|结束|终止|quit|close|exit|terminate)$",
        ),
    )
    for action, pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        if action == "hide_other_apps":
            return None
        if action == "quit" and re.search(r"(?:窗口|window)", value, flags=re.IGNORECASE):
            continue
        if action == "show" and re.search(
            r"(?:切到|聚焦|focus|switch\s+to|activate)",
            value,
            flags=re.IGNORECASE,
        ) and not re.search(
            r"(?:前台|置前|叫出来|显示|还原|恢复|调出来|show|restore|unhide)",
            value,
            flags=re.IGNORECASE,
        ):
            continue
        raw_app = next(
            (
                item
                for item in match.groupdict().values()
                if item is not None and str(item).strip()
            ),
            "",
        )
        app_name = _clean_management_app_name_hint(raw_app)
        if app_name:
            return {"action": action, "app_name": app_name}
    return None


def _compound_app_management_hint(value: str) -> dict[str, str] | None:
    patterns = (
        re.compile(
            r"^(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:打开|启动|开启|运行|拉起|切到|聚焦)\s*"
            r"(?P<app>.+?)\s*(?:并且|并|然后|再|接着|之后|随后)\s*"
            r"(?P<action>隐藏|藏起来|收起来|收起|最小化|退出|关闭|关掉|结束|终止)"
            r"(?:一下|下)?(?:它|该应用|这个应用)?(?:吗|嘛|呢|吧|么)?[？?]?$",
            flags=re.IGNORECASE,
        ),
        re.compile(
            r"^(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
            r"(?:open|launch|start|focus|activate|switch\s+to|bring)\s+"
            r"(?P<app>.+?)(?:\s+up)?\s+(?:and\s+then|then|and)\s+"
            r"(?P<action>hide|minimi[sz]e|quit|close|exit|terminate)"
            r"(?:\s+(?:it|the\s+app|that\s+app|this\s+app))?"
            r"(?:\s+please)?[.!?]*$",
            flags=re.IGNORECASE,
        ),
    )
    for pattern in patterns:
        match = pattern.search(value)
        if not match:
            continue
        app_name = _clean_management_app_name_hint(match.group("app"))
        raw_action = str(match.group("action") or "").lower()
        if not app_name:
            return None
        if re.search(r"(?:隐藏|藏|收|hide)", raw_action, flags=re.IGNORECASE):
            action = "hide"
        elif re.search(r"(?:最小化|minimi[sz]e)", raw_action, flags=re.IGNORECASE):
            action = "minimize"
        else:
            action = "quit"
        return {"action": action, "app_name": app_name}
    return None


def _looks_like_open_then_foreground_confirmation(value: str) -> bool:
    text = clean(value)
    if not text:
        return False
    return bool(
        re.search(
            r"(?:打开|启动|开启|拉起)\s*[^。！？!?，,]+?"
            r"(?:并|然后|再|接着|之后)?\s*"
            r"(?:确认|检查|验证|看看|看一下|看下)\s*"
            r"(?:它|其|这个(?:应用|app)?|该(?:应用|app)?|应用|app)?\s*"
            r"(?:是否|是不是|有没有|已经)?\s*(?:在|处于)?\s*(?:前台|置前)$",
            text,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:open|launch|start)\s+[A-Za-z][A-Za-z0-9 ._-]{1,40}?\s+"
            r"(?:and|then)\s+"
            r"(?:confirm|check|verify|make\s+sure)\s+"
            r"(?:it|the\s+app|the\s+application)?\s*"
            r"(?:is\s+)?(?:frontmost|foreground|the\s+active\s+app|active)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def foreground_management_hint(text: str) -> dict[str, str] | None:
    value = clean(text)
    lowered = value.lower()
    if _is_compound_app_window_close_request(value, lowered):
        return {"action": "close_window", "scope": "window"}
    if _is_show_all_hidden_apps_request(value, lowered):
        return {"action": "show_all_apps", "scope": "desktop"}
    if _is_foreground_window_close_request(value, lowered):
        return {"action": "close_window", "scope": "window"}
    if _is_foreground_app_quit_request(value, lowered):
        return {"action": "quit_app", "scope": "app"}
    if _is_foreground_window_minimize_request(value, lowered):
        return {"action": "minimize_window", "scope": "window"}
    if _is_foreground_app_hide_request(value, lowered):
        return {"action": "hide_app", "scope": "app"}
    return None


def _is_compound_app_window_close_request(value: str, lowered: str) -> bool:
    return bool(
        re.search(
            r"(?:打开|启动|开启|运行|拉起|切到|聚焦)\s*.+?\s*"
            r"(?:并且|并|然后|再|接着|之后|随后)\s*"
            r"(?:关闭|关掉)\s*(?:当前|这个|该)?\s*窗口$",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:open|launch|start|focus|activate|switch\s+to|bring)\s+.+?\s+"
            r"(?:and\s+then|then|and)\s+close\s+"
            r"(?:the\s+)?(?:(?:current|active|this)\s+)?window\b",
            lowered,
            flags=re.IGNORECASE,
        )
    )


def hotkey_hint(text: str) -> dict[str, Any] | None:
    value = clean(text)
    normalized = re.sub(r"\s+", "", value).lower()
    if normalized in {"退出当前应用", "退出当前app", "关闭当前应用", "关闭当前app"}:
        return {"key": "q", "modifiers": ["command"]}
    if normalized in {"空格一下", "空格下", "敲一下空格", "敲空格一下"}:
        return {"key": "space", "modifiers": []}
    if not contains_any(
        value.lower(),
        ["按", "敲", "快捷键", "press", "hit", "tap", "hotkey", "shortcut"],
    ):
        return None
    patterns = (
        r"(?:按|敲|发送快捷键|快捷键)\s*(?:一下|下)?\s*(?P<combo>[^。！？!?，,]+)",
        r"(?:press|hit|tap)\s+(?:the\s+)?(?:hotkey\s+|shortcut\s+)?(?P<combo>[^.!?,]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        combo = re.sub(
            r"\s+(?:in|on)\s+(?:the\s+)?(?:current|foreground|active)\s+"
            r"(?:window|app|application)\s*$",
            "",
            match.group("combo"),
            flags=re.IGNORECASE,
        )
        parsed = _parse_hotkey_combo(combo)
        if parsed:
            return parsed
    return None


def safe_shortcut_hint(text: str) -> dict[str, str] | None:
    value = clean(text)
    action = _safe_shortcut_action_from_hotkey_hint(value) or _safe_shortcut_action_from_phrase(value)
    if not action:
        for part in reversed(
            [
                item.strip()
                for item in re.split(r"(?:然后|再|接着|之后|and\s+then|then|[,，。])", value)
                if item.strip()
            ]
        ):
            action = _safe_shortcut_action_from_phrase(part)
            if action:
                break
    if not action:
        action = _safe_shortcut_action_from_embedded_create_phrase(value)
    if not action:
        action = _safe_shortcut_action_from_trailing_phrase(value)
    return {"action": action} if action else None


def safe_shortcut_sequence_hint(text: str) -> list[dict[str, str]]:
    value = clean(text)
    actions: list[str] = []
    for part in [
        item.strip()
        for item in re.split(r"(?:然后|再|接着|之后|and\s+then|then|[,，。])", value)
        if item.strip()
    ]:
        compound_actions = _compound_safe_shortcut_actions(part)
        if compound_actions:
            actions.extend(compound_actions)
            continue
        action = (
            _safe_shortcut_action_from_hotkey_hint(part)
            or _safe_shortcut_action_from_phrase(part)
            or _safe_shortcut_action_from_trailing_phrase(part)
        )
        if action:
            actions.append(action)
    return [{"action": action} for action in actions] if len(actions) > 1 else []


def safe_key_hint(text: str) -> dict[str, Any] | None:
    value = clean(text)
    lowered = value.lower()
    if _looks_like_show_desktop_request(value, lowered):
        return {"action": "show_desktop", "repeat_count": 1}
    if _looks_like_next_focus_request(value, lowered):
        return {"action": "tab", "repeat_count": 1}
    if _looks_like_previous_focus_request(value, lowered):
        return {"action": "shift_tab", "repeat_count": 1}
    count = r"(?P<{name}>\d+|[一二两三四五六七八九十]|one|two|three|four|five|six|seven|eight|nine|ten)"
    key = (
        r"(?P<{name}>esc|escape|tab|home|end|page\s*up|page\s*down|pageup|pagedown|"
        r"up\s+arrow|down\s+arrow|left\s+arrow|right\s+arrow|arrow\s+up|arrow\s+down|"
        r"arrow\s+left|arrow\s+right|up|down|left|right|"
        r"退出|取消|制表键|制表|向上箭头|向下箭头|向左箭头|向右箭头|"
        r"上箭头|下箭头|左箭头|右箭头|上方向键|下方向键|左方向键|右方向键|"
        r"上一页键|下一页键|上一页|下一页|home\s*键|end\s*键)"
    )
    patterns = (
        (
            r"^(?:你能帮我|你可以帮我|可以帮我|能帮我|帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:按一下|按下|按|发送|触发)\s*"
            rf"(?:{count.format(name='count_before')}\s*(?:次|下)\s*)?"
            rf"{key.format(name='key')}"
            rf"(?:\s*{count.format(name='count_after')}\s*(?:次|下))?"
            r"\s*(?:键)?(?:一下|下)?(?:可以吗|好吗|好么|行吗|吗|嘛|吧|呢)?$"
        ),
        (
            r"^(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?(?:press|send|hit)\s+(?:the\s+)?"
            rf"{key.format(name='key_en')}"
            rf"(?:\s+{count.format(name='count_en')}\s*(?:times?)?)?"
            r"(?:\s+please)?[.!?]?\s*$"
        ),
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        groups = match.groupdict()
        action = _safe_key_action(
            groups.get("key") or groups.get("key_en") or ""
        )
        repeat_count = _bounded_count(
            groups.get("count_before") or groups.get("count_after") or groups.get("count_en"),
            default=1,
            maximum=20,
        )
        if action and repeat_count:
            return {"action": action, "repeat_count": repeat_count}
    return None


def safe_scroll_hint(text: str) -> dict[str, Any] | None:
    value = clean(text)
    count = r"(?P<{name}>\d+|[一二两三四五六七八九十]|one|two|three|four|five|six|seven|eight|nine|ten)"
    zh_prefix = (
        r"^(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:在|把|将)?\s*(?:当前|前台|这个|该)?"
        r"(?:窗口|界面|应用|app|网页|页面|屏幕)?(?:上|里|中|内)?\s*"
    )
    patterns = (
        (
            zh_prefix
            + r"(?:滚动|滚|滑动|滑|翻页|翻|拉)(?:到|至)?\s*"
            + r"(?P<extent>页面底部|页面顶部|底部|底端|最底下|最下面|顶部|顶端|最上面|最上方)"
            + r"(?:一下|下)?(?:可以吗|好吗|好么|行吗|吗|嘛|吧|呢)?$"
        ),
        (
            zh_prefix
            + r"(?P<direction>向下|往下|朝下|下|向上|往上|朝上|上)"
            + r"(?:滚动|滚|滑动|滑|翻页|翻|拉)"
            + rf"(?:\s*{count.format(name='count')}\s*(?:页|屏|次))?"
            + r"(?:一点|点|一些|一下|下)?(?:可以吗|好吗|好么|行吗|吗|嘛|吧|呢)?$"
        ),
        (
            zh_prefix
            + r"(?P<direction_phrase>下滑|上滑|下滚|上滚|下翻|上翻|下一页|上一页)"
            + rf"(?:\s*{count.format(name='count_phrase')}\s*(?:页|屏|次))?"
            + r"(?:一下|下)?(?:可以吗|好吗|好么|行吗|吗|嘛|吧|呢)?$"
        ),
        (
            r"^(?:please\s+)?(?:scroll|page)\s+"
            r"(?P<direction_en>down|up)"
            + rf"(?:\s+{count.format(name='count_en')}\s*(?:pages?|times?)?)?"
            + r"\s*$"
        ),
        (
            r"^(?:please\s+)?(?:scroll|page)\s+(?:to\s+)?(?:the\s+)?"
            r"(?P<extent_en>bottom|top)\s*$"
        ),
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        groups = match.groupdict()
        direction = (
            groups.get("extent")
            or groups.get("direction")
            or groups.get("direction_phrase")
            or groups.get("direction_en")
            or groups.get("extent_en")
            or ""
        )
        pages = (
            10
            if groups.get("extent") or groups.get("extent_en")
            else _bounded_count(
                groups.get("count") or groups.get("count_phrase") or groups.get("count_en"),
                default=1,
                maximum=10,
            )
        )
        if direction and pages:
            return {"direction": "up" if _scroll_direction_is_up(direction) else "down", "pages": pages}
    if re.search(
        zh_prefix + r"(?:滚动|滚|滑动|滑|翻页|翻|拉)(?:一下|下)?(?:可以吗|好吗|好么|行吗|吗|嘛|吧|呢)?$",
        value,
        flags=re.IGNORECASE,
    ) or re.search(r"^(?:please\s+)?(?:scroll|page)(?:\s+(?:a\s+)?(?:little|bit))?\s*$", value, flags=re.IGNORECASE):
        return {"direction": "down", "pages": 1}
    if re.search(
        zh_prefix + r"(?:翻到|翻至|跳到|跳至)\s*(?P<page>下一页|上一页)(?:一下|下)?(?:可以吗|好吗|好么|行吗|吗|嘛|吧|呢)?$",
        value,
        flags=re.IGNORECASE,
    ):
        return {"direction": "up" if "上一页" in value else "down", "pages": 1}
    return None


def safe_click_hint(text: str) -> dict[str, int | float] | None:
    value = clean(_affirmative_action_text(text, _CLICK_ACTION_PATTERN))
    patterns = (
        r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:(?P<double>双击|double\s+click)|点击|点一下|点按|单击|点|click)\s*"
        r"(?:屏幕坐标|屏幕|坐标|位置)?\s*"
        r"(?P<x>\d+(?:\.\d+)?)\s*(?:,|，|\s)\s*(?P<y>\d+(?:\.\d+)?)",
        r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:在|到)\s*(?:屏幕坐标|屏幕|坐标|位置)?\s*"
        r"(?P<x2>\d+(?:\.\d+)?)\s*(?:,|，|\s)\s*(?P<y2>\d+(?:\.\d+)?)\s*"
        r"(?:(?P<double2>双击|double\s+click)|点击|点一下|点按|单击|点|click)",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        x = match.groupdict().get("x") or match.groupdict().get("x2") or ""
        y = match.groupdict().get("y") or match.groupdict().get("y2") or ""
        click_count = 2 if match.groupdict().get("double") or match.groupdict().get("double2") else 1
        payload: dict[str, int | float] = {"x": _numeric_value(x), "y": _numeric_value(y)}
        if click_count != 1:
            payload["click_count"] = click_count
        return payload
    return None


def media_playback_hint(text: str) -> dict[str, str]:
    action = media_action_hint(text)
    app_name = music_app_name_hint(text)
    control_only = media_control_only_hint(text, action=action)
    query = media_query_hint(text) if action == "play" and not control_only else ""
    if not app_name and action == "play" and query:
        app_name = media_app_scope_hint(text)
    if not app_name and action == "play" and query:
        app_name = "Music"
    if not app_name and _implicit_apple_music_control_hint(text, action=action):
        app_name = "Music"
    return {
        "action": action,
        "app_name": app_name,
        "query": query,
        "control_only": "true" if control_only else "",
    }


def media_non_action_reference_hint(text: str) -> bool:
    value = str(text or "").strip()
    return bool(
        re.fullmatch(r"(?:播放列表|播放队列|播放记录)", value, flags=re.IGNORECASE)
        or re.search(
            r"(?:可以|可|能够|能)?\s*被\s*(?:随时)?\s*(?:停止|暂停)\s*的\s*"
            r"(?:慢\s*)?(?:请求|任务|操作|进程|作业|流程|命令|脚本)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:播放|播|放)\s*(?:到|至)\s*\d+(?:\.\d+)?\s*%?\s*$",
            value,
            flags=re.IGNORECASE,
        )
    )


def media_tool_preview(
    inputs: Mapping[str, Any],
    allowed_tools: Iterable[str] | None,
) -> tuple[str | None, dict[str, Any]]:
    allowed = _allowed_tool_set(allowed_tools)
    action = str(inputs.get("action") or "").strip() or "play"
    app_name = str(inputs.get("app_name") or "").strip()
    query = str(inputs.get("query") or "").strip()
    app_capability = inputs.get("target_app_capability_hint")
    if (
        isinstance(app_capability, Mapping)
        and not app_name
        and (allowed is None or "desktop.list_apps" in allowed)
    ):
        return None, {}
    control_only = str(inputs.get("control_only") or "").strip().lower() == "true"
    is_apple_music = not app_name or app_name == "Music"
    if action == "status":
        return _first_allowed(("media.apple_music_status",), allowed), {}
    if query and is_apple_music:
        apple_tool = _first_allowed(("media.apple_music_play",), allowed)
        if apple_tool:
            return apple_tool, {"query": query}
        # The generic open-and-play tool cannot accept a song/artist query.
        # Returning it here would silently discard the user's requested media.
        return None, {}
    if app_name and not is_apple_music:
        if action == "play":
            return _first_allowed(("media.music_app_open_and_play",), allowed), {"app_name": app_name}
        tool_name = _first_allowed(("media.music_app_control", "media.system_control"), allowed)
        payload = {"app_name": app_name, "action": action} if tool_name == "media.music_app_control" else {"action": action}
        return tool_name, payload
    if action == "play":
        if control_only:
            tool_name = (
                _first_allowed(
                    (
                        "media.apple_music_control",
                        "media.music_app_control",
                        "media.system_control",
                    ),
                    allowed,
                )
                if app_name
                else _first_allowed(("media.system_control", "media.apple_music_control"), allowed)
            )
            if tool_name == "media.music_app_control":
                return tool_name, {"app_name": app_name or "Music", "action": "play"}
            return tool_name, {"action": "play"} if tool_name else {}
        generic_tool = _first_allowed(("media.music_app_open_and_play",), allowed)
        if generic_tool:
            return generic_tool, {"app_name": app_name or "Music"}
        tool_name = _first_allowed(
            ("media.apple_music_open_and_play", "media.apple_music_control", "media.system_control"),
            allowed,
        )
        return tool_name, {"action": "play"} if tool_name in {"media.apple_music_control", "media.system_control"} else {}
    if not app_name:
        tool_name = _first_allowed(("media.system_control", "media.apple_music_control"), allowed)
        return tool_name, {"action": action} if tool_name else {}
    tool_name = _first_allowed(
        ("media.apple_music_control", "media.music_app_control", "media.system_control"),
        allowed,
    )
    if tool_name == "media.music_app_control":
        return tool_name, {"app_name": app_name, "action": action}
    return tool_name, {"action": action} if tool_name else {}


def media_app_query_search_plan(
    inputs: Mapping[str, Any],
    allowed_tools: Iterable[str] | None,
) -> list[tuple[str, dict[str, Any]]]:
    allowed = _allowed_tool_set(allowed_tools)
    action = str(inputs.get("action") or "").strip() or "play"
    app_name = str(inputs.get("app_name") or "").strip()
    query = str(inputs.get("query") or "").strip()
    app_capability = inputs.get("target_app_capability_hint")
    capability_query = ""
    selected_app_payload: dict[str, Any] = {}
    if isinstance(app_capability, Mapping):
        capability_query = str(app_capability.get("query") or "").strip()
        if capability_query and not app_name:
            app_name = _SELECTED_DESKTOP_APP_NAME
            selected_app_payload = {
                "selection_source": "desktop.list_apps",
                "query": capability_query,
            }
    if action != "play" or not app_name or not query:
        return []

    type_tool = _first_allowed(
        (
            "desktop.safe_type_text",
            "desktop.type",
            "app.focus_and_safe_type_text",
            "app.open_and_safe_type_text",
            "desktop.type_text",
        ),
        allowed,
    )
    type_into_tool = _first_allowed(
        (
            "app.focus_and_type_into_ui_element",
            "app.open_and_type_into_ui_element",
            "desktop.type_into_ui_element",
        ),
        allowed,
    )
    submit_tool = _first_allowed(
        (
            "desktop.search_submit",
            "desktop.submit_foreground",
            "desktop.safe_key",
            "desktop.key",
            "desktop.shortcut",
            "desktop.hotkey",
        ),
        allowed,
    )
    verify_tool = _first_allowed(("desktop.ui_elements", "desktop.active_window", "screen.capture"), allowed)
    if (not type_tool and not type_into_tool) or (not submit_tool and not verify_tool):
        return []
    submit_payload = _media_search_submit_payload(submit_tool)
    submit_step = [(submit_tool, submit_payload)] if submit_tool else []
    type_payload = {"text": query}
    if type_tool and type_tool.startswith("app."):
        type_payload = {"app_name": app_name, **selected_app_payload, "text": query}

    discovery_step = []
    discover_tool = _first_allowed(("desktop.list_apps",), allowed)
    if discover_tool:
        discovery_step = [
            (discover_tool, {"query": capability_query or app_name, "limit": 20})
        ]

    app_search_tool = _first_allowed(
        ("app.open_and_safe_shortcut", "app.focus_and_safe_shortcut"),
        allowed,
    )
    if app_search_tool and type_tool:
        app_search_payload = {
            "app_name": app_name,
            **selected_app_payload,
            "action": "find",
        }
        plan = [
            *discovery_step,
            (app_search_tool, app_search_payload),
            (type_tool, type_payload),
            *submit_step,
        ]
        _append_media_search_result_play_step(
            plan,
            app_name=app_name,
            selected_app_payload=selected_app_payload,
            allowed=allowed,
        )
        _append_media_app_verify_step(plan, allowed)
        return plan

    app_tool = _first_allowed(
        ("app.open", "desktop.open_app", "app.focus", "desktop.focus_app"),
        allowed,
    )
    if app_tool and type_into_tool:
        type_into_payload = {
            "target": "search 搜索",
            "text": query,
            "role_filter": "text",
            "limit": 80,
        }
        if type_into_tool.startswith("app."):
            type_into_payload = {
                "app_name": app_name,
                **selected_app_payload,
                **type_into_payload,
            }
        plan = [
            *discovery_step,
            (app_tool, {"app_name": app_name, **selected_app_payload}),
            (type_into_tool, type_into_payload),
            *submit_step,
        ]
        _append_media_search_result_play_step(
            plan,
            app_name=app_name,
            selected_app_payload=selected_app_payload,
            allowed=allowed,
        )
        _append_media_app_verify_step(plan, allowed)
        return plan

    shortcut_tool = _first_allowed(
        ("desktop.safe_shortcut", "desktop.shortcut", "desktop.hotkey"),
        allowed,
    )
    if app_tool and shortcut_tool and type_tool:
        plan = [
            *discovery_step,
            (app_tool, {"app_name": app_name, **selected_app_payload}),
            (shortcut_tool, _media_search_shortcut_payload(shortcut_tool)),
            (type_tool, type_payload),
            *submit_step,
        ]
        _append_media_search_result_play_step(
            plan,
            app_name=app_name,
            selected_app_payload=selected_app_payload,
            allowed=allowed,
        )
        _append_media_app_verify_step(plan, allowed)
        return plan

    observed_type_tool = _first_allowed(("desktop.type_into_ui_element",), allowed)
    if not observed_type_tool and _media_observed_type_fallback_available(allowed):
        observed_type_tool = "desktop.type_into_ui_element"
    if (
        app_tool
        and type_tool
        and observed_type_tool
        and _media_observed_type_fallback_available(allowed)
    ):
        type_into_payload = {
            "app_name": app_name,
            **selected_app_payload,
            "target": "search 搜索",
            "text": query,
            "role_filter": "text",
            "limit": 80,
        }
        plan = [
            *discovery_step,
            (app_tool, {"app_name": app_name, **selected_app_payload}),
            (observed_type_tool, type_into_payload),
            *submit_step,
        ]
        _append_media_search_result_play_step(
            plan,
            app_name=app_name,
            selected_app_payload=selected_app_payload,
            allowed=allowed,
            allow_observed_fallback=True,
        )
        _append_media_app_verify_step(plan, allowed)
        return plan

    return []


def _media_search_shortcut_payload(tool_name: str | None) -> dict[str, Any]:
    if tool_name in {"desktop.shortcut", "desktop.hotkey"}:
        return {"key": "f", "modifiers": ["command"]}
    return {"action": "find"}


def _media_search_submit_payload(tool_name: str | None) -> dict[str, Any]:
    if tool_name == "desktop.submit_foreground":
        return {"action": "confirm"}
    if tool_name in {"desktop.safe_key", "desktop.key"}:
        return {"key": "return", "modifiers": []}
    if tool_name in {"desktop.shortcut", "desktop.hotkey"}:
        return {"key": "return", "modifiers": []}
    return {}


def _append_media_search_result_play_step(
    plan: list[tuple[str, dict[str, Any]]],
    *,
    app_name: str,
    selected_app_payload: Mapping[str, Any],
    allowed: set[str] | None,
    allow_observed_fallback: bool = False,
) -> None:
    play_tool = _first_allowed(("media.music_app_open_and_play",), allowed)
    if play_tool:
        plan.append((play_tool, {"app_name": app_name, **dict(selected_app_payload)}))
        return
    click_tool = _first_allowed(
        (
            "app.focus_and_click_ui_element",
            "app.open_and_click_ui_element",
            "desktop.click_ui_element",
        ),
        allowed,
    )
    if not click_tool:
        if not allow_observed_fallback or not _media_observed_click_fallback_available(allowed):
            return
        plan.append(
            (
                "desktop.click_ui_element",
                {
                    "app_name": app_name,
                    **dict(selected_app_payload),
                    "target": "first result",
                    "role_filter": "",
                    "limit": 80,
                    "click_count": 1,
                },
            )
        )
        return
    click_payload = {
        "target": "first result",
        "role_filter": "",
        "limit": 80,
        "click_count": 1,
    }
    if click_tool.startswith("app."):
        click_payload = {
            "app_name": app_name,
            **dict(selected_app_payload),
            **click_payload,
        }
    plan.append((click_tool, click_payload))


def _media_observed_type_fallback_available(allowed: set[str] | None) -> bool:
    if not _first_allowed(("desktop.ui_elements", "desktop.read_ui"), allowed):
        return False
    if not _first_allowed(
        ("desktop.safe_type_text", "desktop.type_text", "desktop.type"),
        allowed,
    ):
        return False
    return bool(
        _first_allowed(
            (
                "app.focus_and_click_ui_element",
                "app.open_and_click_ui_element",
                "desktop.click_ui_element",
                "desktop.safe_click",
                "desktop.click",
            ),
            allowed,
        )
    )


def _media_observed_click_fallback_available(allowed: set[str] | None) -> bool:
    if not _first_allowed(("desktop.ui_elements", "desktop.read_ui"), allowed):
        return False
    return bool(_first_allowed(("desktop.safe_click", "desktop.click"), allowed))


def media_app_prepare_plan(
    inputs: Mapping[str, Any],
    allowed_tools: Iterable[str] | None,
) -> list[tuple[str, dict[str, Any]]]:
    allowed = _allowed_tool_set(allowed_tools)
    action = str(inputs.get("action") or "").strip() or "play"
    app_name = str(inputs.get("app_name") or "").strip()
    query = str(inputs.get("query") or "").strip()
    app_capability = inputs.get("target_app_capability_hint")
    capability_query = ""
    selected_app_payload: dict[str, Any] = {}
    if isinstance(app_capability, Mapping):
        capability_query = str(app_capability.get("query") or "").strip()
        if capability_query and not app_name:
            app_name = _SELECTED_DESKTOP_APP_NAME
            selected_app_payload = {
                "selection_source": "desktop.list_apps",
                "query": capability_query,
            }
    if action != "play" or not app_name or (query and not selected_app_payload):
        return []

    plan: list[tuple[str, dict[str, Any]]] = []
    discover_tool = _first_allowed(("desktop.list_apps",), allowed)
    if discover_tool:
        plan.append((discover_tool, {"query": capability_query or app_name, "limit": 20}))

    app_tool = _first_allowed(
        ("app.open", "desktop.open_app", "app.focus", "desktop.focus_app"),
        allowed,
    )
    if not app_tool:
        return []
    plan.append((app_tool, {"app_name": app_name, **selected_app_payload}))
    _append_media_generic_play_step(
        plan,
        app_name=app_name,
        selected_app_payload=selected_app_payload,
        allowed=allowed,
    )
    _append_media_app_verify_step(plan, allowed)
    return plan


def _append_media_generic_play_step(
    plan: list[tuple[str, dict[str, Any]]],
    *,
    app_name: str,
    selected_app_payload: Mapping[str, Any],
    allowed: set[str] | None,
) -> None:
    click_tool = _first_allowed(
        (
            "app.focus_and_click_ui_element",
            "app.open_and_click_ui_element",
            "desktop.click_ui_element",
        ),
        allowed,
    )
    if not click_tool:
        return
    click_payload = {
        "target": "play 播放",
        "role_filter": "button",
        "limit": 80,
        "click_count": 1,
    }
    if click_tool.startswith("app."):
        click_payload = {
            "app_name": app_name,
            **dict(selected_app_payload),
            **click_payload,
        }
    plan.append((click_tool, click_payload))


def _append_media_app_verify_step(
    plan: list[tuple[str, dict[str, Any]]],
    allowed: set[str] | None,
) -> None:
    tool_name = _first_allowed(
        ("desktop.ui_elements", "desktop.active_window", "screen.capture"),
        allowed,
    )
    if not tool_name:
        return
    payload = (
        {"role_filter": "", "limit": 80}
        if tool_name in {"desktop.ui_elements", "desktop.read_ui"}
        else {}
    )
    plan.append((tool_name, payload))


def media_action_hint(text: str) -> str:
    if _media_failure_condition_hint(text):
        return ""
    text = _strip_polite_media_action_prefix(text)
    lowered = str(text or "").lower()
    if media_non_action_reference_hint(text):
        return ""
    if re.fullmatch(r"(?:apple\s+music|music)\s+播放暂停", lowered.strip()):
        return "toggle"
    if contains_any(
        lowered,
        [
            "当前播放",
            "现在播放",
            "正在播放",
            "当前在播",
            "现在在播",
            "在播什么",
            "播放什么",
            "播放状态",
            "播放进度",
            "在播状态",
            "音乐状态",
            "status",
            "currently playing",
            "what is playing",
            "what's playing",
        ],
    ):
        return "status"
    if contains_any(
        lowered,
        [
            "下一首",
            "下一曲",
            "下首",
            "切下一首",
            "跳下一首",
            "切歌",
            "换一首",
            "换首歌",
            "换歌",
        ],
    ) or re.search(
        r"(?:跳过|跳)(?:这首|当前(?:这)?首|当前歌曲|这首歌)",
        str(text or ""),
    ) or re.fullmatch(
        r"\s*(?:apple\s+music|music)\s*[,;:]?\s*(?:next|skip)\s*",
        lowered,
    ) or re.fullmatch(r"\s*(?:next|skip)\s*", lowered) or re.search(
        r"\b(?:next|skip)\s+(?:this\s+|the\s+|current\s+)?"
        r"(?:media\s+)?(?:song|track)\b|\bnext\s+media\b",
        lowered,
    ):
        return "next"
    if contains_any(lowered, ["上一首", "上一曲", "previous"]) or re.search(
        r"\b(?:prev(?:ious)?|back(?:\s+one)?)\s+(?:track|song)\b",
        lowered,
    ):
        return "previous"
    if contains_any(
        lowered,
        [
            "暂停",
            "停一下",
            "停止",
            "停止播放",
            "别放了",
            "关掉",
            "pause",
            "stop playing",
            "stop playback",
        ],
    ) or re.fullmatch(r"\s*stop\s*", lowered) or (
        re.search(r"\bstop\b", lowered)
        and bool(music_app_name_hint(text))
    ):
        return "pause"
    if _media_resume_play_hint(str(text or "")) or (
        re.search(r"\bresume\b", lowered)
        and bool(music_app_name_hint(text))
    ):
        return "play"
    if re.search(
        r"(?:音乐|歌|歌曲).{0,4}(?:听听|听一下|听下)|"
        r"(?:听听|听一下|听下).{0,4}(?:音乐|歌|歌曲)",
        lowered,
        flags=re.IGNORECASE,
    ):
        return "play"
    if re.search(r"\bput\s+.+\s+on\s+(?:apple\s*music|music)\b", lowered):
        return "play"
    if contains_any(
        lowered,
        ["来点", "听点", "听一首", "听首", "想听", "听音乐", "听歌", "listen to music", "put on some music"],
    ):
        return "play"
    if re.search(r"\bstart\s+playing\b", lowered):
        return "play"
    if re.search(r"\bplay\b", lowered):
        return "play"
    if music_app_name_hint(text) and re.search(r"(?:播放|播|放)", str(text or "")):
        return "play"
    if re.search(
        r"^(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:在|用|通过|打开|启动)\s*[^。！？!?，,]{1,60}?"
        r"(?:里|中|上|内|里面)?\s*"
        r"(?:播放|播|放)(?!到|进|在|置|左|右|上|下|大|小|回|前|后)",
        str(text or "").strip(),
        flags=re.IGNORECASE,
    ):
        return "play"
    if re.search(
        r"(?:音乐\s*(?:应用|app|软件|播放器)?|播放器|music\s+app|music\s+player)",
        lowered,
        flags=re.IGNORECASE,
    ) and re.search(r"(?:播放|播|放)|\bplay\b", lowered, flags=re.IGNORECASE):
        return "play"
    if re.search(r"(?:音乐|歌曲|歌).{0,12}(?:播放|播|放)", lowered) or re.search(
        r"(?:播放|播|放).{0,12}(?:音乐|歌曲|歌)",
        lowered,
    ):
        return "play"
    if re.search(
        r"^(?:(?:你)?(?:能不能|能否|可以|可不可以)?(?:帮我)?|请|麻烦)?(?:直接)?"
        r"(?:播放|播|放)(?:一下|下|一首|首|个|点|一点)?\s*"
        r"(?!到|进|在|置|左|右|上|下|大|小|回|前|后)"
        r"[^。！？!?，,]{1,80}$",
        str(text or "").strip(),
        flags=re.IGNORECASE,
    ):
        return "play"
    if re.search(
        r"[^。！？!?，,]{1,80}(?:播放|播|放)(?:一下|下)?$",
        str(text or "").strip(),
        flags=re.IGNORECASE,
    ):
        return "play"
    return ""


def media_control_only_hint(text: str, *, action: str = "") -> bool:
    if action in {"next", "previous", "pause"}:
        return not bool(music_app_name_hint(text))
    if action == "play":
        return (
            _media_resume_play_hint(str(text or ""))
            or (
                re.search(r"\bresume\b", str(text or ""), flags=re.IGNORECASE)
                and bool(music_app_name_hint(text))
            )
            or bool(
            re.fullmatch(r"\s*(?:播放|播|放)(?:一下|下)?\s*", str(text or ""), flags=re.IGNORECASE)
            )
        )
    return False


def _media_resume_play_hint(text: str) -> bool:
    value = clean(text)
    lowered = value.lower()
    return bool(
        re.fullmatch(r"(?:播放继续|继续播放|恢复播放|接着播放)", value, flags=re.IGNORECASE)
        or re.fullmatch(r"resume", lowered)
        or re.search(
            r"(?:继续|恢复|接着).{0,8}(?:当前|现在|正在播放的)?(?:音乐|歌曲|歌|媒体|播放)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:resume|continue)\s+(?:the\s+)?(?:current\s+)?(?:music|song|track|media|playback|playing)\b",
            lowered,
        )
    )


def _implicit_apple_music_control_hint(text: str, *, action: str = "") -> bool:
    value = clean(text)
    if action == "next":
        return bool(re.fullmatch(r"(?:下一首|下一曲|下首)", value, flags=re.IGNORECASE))
    if action == "play":
        return bool(re.fullmatch(r"(?:播放|播|放)(?:一下|下)?", value, flags=re.IGNORECASE))
    return False


def music_app_name_hint(text: str) -> str:
    app_name = legacy_music_app_name_hint(text)
    if app_name:
        return app_name
    if re.search(r"\bMusic\b", str(text or "")):
        return "Music"
    return ""


def media_app_scope_hint(text: str) -> str:
    value = clean(text)
    if _generic_music_app_scope_requested(value):
        return "Music"
    patterns = (
        r"^(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:在|用|通过|打开|启动)\s*"
        r"(?P<app_zh>[^。！？!?，,]{1,60}?)\s*"
        r"(?:里|中|上|内|里面)?\s*"
        r"(?:搜索|搜一下|搜|查找|找|检索)?\s*"
        r"(?:并|然后|再|接着|之后)?\s*"
        r"(?:播放|播|放|play|start\s+playing)",
        r"^(?:open|launch|start|use|using|with|in|on)\s+"
        r"(?P<app_en>[A-Za-z0-9][\w .+&'-]{1,60}?)\s+"
        r"(?:and\s+|to\s+)?(?:search|find|play|start\s+playing)\b",
        r"^(?:play|start\s+playing)\s+.+?\s+"
        r"(?:on|in|with|using)\s+"
        r"(?P<app_en_suffix>[A-Za-z0-9][\w .+&'-]{1,60}?)(?:\s+app)?$",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        app_name = (
            match.groupdict().get("app_zh")
            or match.groupdict().get("app_en")
            or match.groupdict().get("app_en_suffix")
            or ""
        )
        app_name = _clean_media_app_scope(app_name)
        if app_name:
            return app_name
    return ""


def _generic_music_app_scope_requested(text: str) -> bool:
    value = clean(text)
    if not value:
        return False
    return bool(
        re.search(
            r"(?:任意|任何|默认|一个|个|some|any)?\s*"
            r"(?:音乐\s*(?:应用|app|软件|播放器)?|播放器|music\s+app|music\s+player)",
            value,
            flags=re.IGNORECASE,
        )
        and re.search(
            r"(?:搜索|搜一下|搜|查找|找|检索|播放|播|放|play|search|find)",
            value,
            flags=re.IGNORECASE,
        )
    )


def media_query_hint(text: str) -> str:
    value = clean(text)
    if _media_failure_condition_hint(value):
        return ""
    if re.fullmatch(r"(?:播放|播|放)(?:一下|下)?", value, flags=re.IGNORECASE):
        return ""
    if re.fullmatch(r"(?:play|start playing)", value, flags=re.IGNORECASE):
        return ""
    quoted_match, quoted_query = _quoted_media_query_hint(value)
    if quoted_match:
        return quoted_query
    if _generic_music_playback_without_query(value):
        return ""
    patterns = (
        r"(?:put|play)\s+(?P<query_put>.+?)\s+(?:on|in|with)\s+(?:apple\s*music|music)",
        r"(?:搜索|搜一下|搜|查找|找|检索)\s*(?:apple\s*music|苹果音乐|音乐(?:应用|app)?)\s+"
        r"for\s+(?P<query_mixed_app_for>.+?)\s+(?:and\s+)?(?:play|start)",
        r"(?:search|find)\s+(?:apple\s*music|music)\s+for\s+(?P<query_search>.+?)\s+(?:and\s+)?(?:play|start)",
        r"(?:search|find|look\s+up)\s+(?:for\s+)?(?P<query_search_in>.+?)\s+"
        r"(?:in|on|with|using)\s+(?:apple\s*music|music)(?:\s+app)?\s+"
        r"(?:and\s+)?(?:play|start)",
        r"(?:apple\s*music|music)(?:\s+app)?\s+(?:search|find|look\s+up)\s+"
        r"(?:for\s+)?(?P<query_app_search>.+?)\s+(?:and\s+)?(?:play|start)",
        r"(?:open|launch|start)\s+(?:apple\s*music|music)\s+(?:and\s+)?(?:search|find)\s+(?P<query_open_search>.+?)\s+(?:and\s+)?(?:play|start)",
        r"(?:play|start playing)\s+(?P<query_en_scoped_app>.+?)\s+"
        r"(?:on|in|with|using)\s+"
        r"(?:[A-Za-z0-9][\w .+&'-]{1,60}?)(?:\s+app)?$",
        r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:在|用|通过|打开|启动)?\s*(?:apple\s*music|苹果音乐|音乐(?:应用|app)?)"
        r"(?:里|中|上|内|里面)?\s*"
        r"(?:搜索|搜一下|搜|查找|找|检索)(?:一下|下)?\s*"
        r"(?P<query_zh_scoped_search>[^。！？!?，,]+?)\s*"
        r"(?:[，,]\s*)?(?:(?:并|然后|再|接着|之后)\s*)?(?:播放|播|放)(?:一下)?"
        r"(?:\s*(?:第?一首|第?一个(?:结果|条目)?|第一条|首个))?",
        r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:打开|启动|找一个|找个|用|在|通过)?\s*"
        r"(?:(?:(?:我(?:的)?|这台|这个)?(?:电脑|机器|mac|Mac|系统)|本机|本地|桌面)"
        r"(?:里|中|上|里面|内|下)?(?:的)?\s*)?"
        r"(?:任意|任何|默认|一个|个|可用)?\s*"
        r"(?:能|可以|可|会)?\s*(?:播放|播|放|听)?\s*音乐(?:的)?"
        r"(?:应用(?:程序)?|app|软件|播放器|工具|程序)"
        r"(?:里|中|上|内|里面)?\s*(?:[，,]\s*)?"
        r"(?:(?:并|然后|再|接着|之后)\s*)?"
        r"(?:播放|播|放|play)(?:一下|下|一首|首|个|点)?\s*"
        r"(?P<query_music_capability_play>[^。！？!?，,]+)",
        r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:打开|启动|找一个|找个|用|在|通过)?\s*"
        r"(?:任意|任何|默认|一个|个|可用)?\s*(?:音乐\s*(?:应用|app|软件|播放器)?|播放器|music\s+app|music\s+player)"
        r"(?:里|中|上|内|里面)?\s*(?:[，,]\s*)?"
        r"(?:播放|播|放|play)(?:一下|下|一首|首|个|点)?\s*"
        r"(?P<query_generic_music_play>[^。！？!?，,]+)",
        r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:打开|启动|找一个|找个|用|在|通过)?\s*"
        r"(?:任意|任何|默认|一个|个|可用)?\s*(?:音乐\s*(?:应用|app|软件|播放器)?|播放器|music\s+app|music\s+player)"
        r"(?:里|中|上|内|里面)?\s*(?:[，,]\s*)?"
        r"(?:搜索|搜一下|搜|查找|找|检索|播放|播|放)(?:一下|下)?\s*"
        r"(?P<query_generic_music_search>[^。！？!?，,]+?)\s*"
        r"(?:[，,]\s*)?(?:(?:并|然后|再|接着|之后)\s*)?(?:播放|播|放|play)(?:一下|下)?"
        r"(?:\s*(?:第?一首|第?一个(?:结果|条目)?|第一条|首个|first\s+(?:result|song|track)|it))?",
        r"(?:搜索|搜一下|搜|查找|找|检索)(?:一下|下)?\s*(?P<query_zh_search>[^。！？!?，,]+?)"
        r"(?:[，,]\s*)?(?:并|然后|再)?(?:播放|播|放)(?:一下)?",
        r"(?P<query_zh_suffix>[^。！？!?，,]+?)(?:播放(?!器)|播(?!放?器)|放)(?:一下)?$",
        r"(?:想听|听听|听一首|听首|听点|来点)\s*(?P<query_listen>[^。！？!?，,]+)",
        r"(?:播放|播|放)(?:一下|一首|首|个|点)?\s*(?P<query>[^。！？!?，,]+)",
        r"(?:play|start playing)\s+(?P<query_en>[^.!?,]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        groups = match.groupdict()
        query_group = next(
            (
                name
                for name in (
                    "query_put",
                    "query_mixed_app_for",
                    "query_search",
                    "query_search_in",
                    "query_app_search",
                    "query_open_search",
                    "query_en_scoped_app",
                    "query_zh_scoped_search",
                    "query_music_capability_play",
                    "query_generic_music_play",
                    "query_generic_music_search",
                    "query_zh_search",
                    "query_zh_suffix",
                    "query_listen",
                    "query",
                    "query_en",
                )
                if groups.get(name)
            ),
            "",
        )
        if not query_group:
            continue
        raw_query = str(groups[query_group])
        if _unquoted_media_query_is_conditional(
            value,
            query_span=match.span(query_group),
        ):
            continue
        query = _clean_media_query(raw_query)
        if _negative_modal_query_fragment(query):
            continue
        if query:
            return query
    return ""


def _quoted_media_query_hint(value: str) -> tuple[bool, str]:
    action = (
        r"(?:搜索|搜一下|搜|查找|找|检索|播放|播|放|想听|听听|听一首|听首|听点|来点|"
        r"search|find|look\s+up|play|start\s+playing|listen\s+to)"
    )
    bridge = (
        r"(?:一下|下|一首|首|个|点)?\s*"
        r"(?:(?:the\s+)?(?:song|track)|歌曲?|音乐|曲目)?\s*"
        r"(?:名为|叫作?|名字是|named)?\s*"
        r"(?:(?:apple\s*music|苹果音乐|音乐(?:应用|app)?)"
        r"\s*(?:里的|中的|上的|内的|里面的|里|中|上|内|里面|的)?\s*)?"
        r"(?:for\s+)?[:：]?\s*"
    )
    quote_pairs = (
        ("“", "”"),
        ("‘", "’"),
        ("「", "」"),
        ("『", "』"),
        ("《", "》"),
        ('"', '"'),
        ("'", "'"),
    )
    for opening, closing in quote_pairs:
        pattern = rf"{action}{bridge}{re.escape(opening)}"
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        query_start = match.end()
        query_end = _closing_media_quote_index(
            value,
            start=query_start,
            opening=opening,
            closing=closing,
        )
        if query_end is None:
            continue
        query = clean(value[query_start:query_end]).strip(" .，,。")
        if _media_query_match_is_failure_condition(
            value,
            query_span=(query_start, query_end),
        ):
            return True, ""
        if not query or query.lower() in _GENERIC_MUSIC_QUERIES:
            return True, ""
        return True, query
    return False, ""


def _closing_media_quote_index(
    value: str,
    *,
    start: int,
    opening: str,
    closing: str,
) -> int | None:
    scan_end = min(len(value), start + 160)
    if opening == closing == "'":
        candidates = [
            index
            for index in range(start, scan_end)
            if value[index] == closing
            and not (
                index > start
                and index + 1 < len(value)
                and value[index - 1].isalnum()
                and value[index + 1].isalnum()
            )
        ]
        index = candidates[0] if candidates else -1
    else:
        index = value.find(closing, start, scan_end)
    return index if index >= start else None


def _unquoted_media_query_is_conditional(
    value: str,
    *,
    query_span: tuple[int, int],
) -> bool:
    return _media_query_match_is_failure_condition(value, query_span=query_span)


def _media_query_match_is_failure_condition(
    value: str,
    *,
    query_span: tuple[int, int],
) -> bool:
    start, end = query_span
    clause_start_matches = list(re.finditer(r"[。！？!?,，；;\n]", value[:start]))
    clause_start = clause_start_matches[-1].end() if clause_start_matches else 0
    clause_end_match = re.search(r"[。！？!?,，；;\n]", value[end:])
    clause_end = end + clause_end_match.start() if clause_end_match else len(value)
    clause = value[clause_start:clause_end].strip()
    return bool(
        re.match(r"^(?:如果|假如|若)", clause)
        and re.search(r"(?:只能|(?<!能)不能|无法)", clause)
    )


def _negative_modal_query_fragment(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:(?:而|但|却|不过|只是)\s*)?(?:不能|无法|不可|不会)",
            str(value or "").strip(),
            flags=re.IGNORECASE,
        )
    )


def _media_failure_condition_hint(text: str) -> bool:
    value = clean(text)
    if not value:
        return False
    chinese_condition = re.match(
        r"^(?:(?:请问|请确认|想问(?:一下)?|我想问(?:一下)?)\s*)?"
        r"(?:如果|假如|若)",
        value,
        flags=re.IGNORECASE,
    )
    if chinese_condition and re.search(
        r"(?:只能|(?<!能)不能|无法).{0,60}"
        r"(?:打开|启动|搜索|搜|查找|播放|播|放)",
        value,
        flags=re.IGNORECASE,
    ):
        return True
    lowered = value.lower()
    return bool(
        re.match(r"^(?:please\s+)?if\b", lowered)
        and re.search(
            r"\b(?:can\s+only|can\s*not|cannot|can't|(?:is\s+)?unable\s+to)\b"
            r".{0,80}\b(?:open|launch|search|find|play)\b",
            lowered,
            flags=re.IGNORECASE,
        )
    )


def _strip_polite_media_action_prefix(text: str) -> str:
    value = clean(text)
    value = re.sub(r"^请问\s*", "", value)
    return re.sub(
        r"^(?:如果|假如|若)\s*(?:可以|方便|可行)(?:的话)?\s*[，,]?\s*",
        "",
        value,
        count=1,
        flags=re.IGNORECASE,
    )


def _generic_music_playback_without_query(value: str) -> bool:
    text = clean(value)
    if not text:
        return False
    if re.search(r"(?:搜索|搜一下|搜|查找|找|检索|search|find|look\s+up)", text, flags=re.IGNORECASE):
        return False
    generic_play_tail = re.search(
        r"(?:播放|播|放|听)(?:一下|下|一首|首|个|点|一点)?\s*"
        r"(?:音乐|歌|歌曲)?\s*$",
        text,
        flags=re.IGNORECASE,
    )
    if not generic_play_tail:
        return False
    if re.search(
        r"(?:任意|任何|默认|一个|个|可用)?\s*"
        r"(?:音乐\s*(?:应用|app|软件|播放器)?|播放器|music\s+app|music\s+player)",
        text,
        flags=re.IGNORECASE,
    ):
        return True
    return bool(
        re.fullmatch(
            r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?\s*"
            r"(?:播放|播|放|听)(?:一下|下|一首|首|个|点|一点)?\s*"
            r"(?:音乐|歌|歌曲)?",
            text,
            flags=re.IGNORECASE,
        )
    )


def _clean_media_app_scope(value: str) -> str:
    app_name = clean(value)
    app_name = re.sub(r"^(?:the|a|an)\s+", "", app_name, flags=re.IGNORECASE).strip()
    app_name = re.sub(
        r"\s*(?:app|application|客户端|桌面客户端)$",
        "",
        app_name,
        flags=re.IGNORECASE,
    ).strip()
    if not app_name:
        return ""
    normalized = legacy_music_app_name_hint(app_name)
    if normalized:
        return normalized
    if clean(app_name).lower() in _GENERIC_MUSIC_QUERIES:
        return ""
    return app_name


def _clean_media_query(value: str) -> str:
    query = clean(value)
    query = re.sub(r"^some\s+(?=[A-Za-z])", "", query)
    query = re.sub(r"^(?:in|on|with|using)\s+", "", query, flags=re.IGNORECASE)
    query = re.sub(
        r"^(?:打开|启动|找一个|找个|用|在|通过)?\s*"
        r"(?:任意|任何|默认|一个|个|可用)?\s*(?:音乐\s*(?:应用|app|软件|播放器)?|播放器|music\s+app|music\s+player)"
        r"(?:里|中|上|内|里面)?\s*(?:[，,]\s*)?"
        r"(?:搜索|搜一下|搜|查找|找|检索|播放|播|放)(?:一下|下)?\s*",
        "",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"^(?:能|可以|可用于|用来|用于)?\s*(?:播放|播|放)?\s*音乐(?:的)?"
        r"(?:应用(?:程序)?|app|软件|播放器|工具|程序)"
        r"(?:里|中|上|内|里面)?\s*(?:[，,]\s*)?"
        r"(?:搜索|搜一下|搜|查找|找|检索|播放|播|放)(?:一下|下)?\s*",
        "",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"^(?:搜索|搜一下|搜|查找|找|检索)\s*(?:apple\s*music|苹果音乐|音乐(?:应用|app)?)\s+for\s+",
        "",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"(?:用|在|打开|启动|通过)?\s*"
        rf"(?:{_MEDIA_APP_NAME_PATTERN}|音乐(?:应用|app)|网易云音乐?)",
        "",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(r"^\s*(?:里的|中的|里面|里|中|上|内|的)\s*", "", query)
    query = re.split(
        r"(?:并|然后|再|接着|之后|后|and\s+then|then)",
        query,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    query = re.sub(
        r"\s+(?:and\s+)?(?:play|start(?:\s+playing)?)"
        r"(?:\s+(?:it|first\s+(?:result|song|track)))?$",
        "",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"\s*(?:播放|播|放)(?:一下|下)?"
        r"(?:\s*(?:第?一首|第?一个(?:结果|条目)?|第一条|首个))?$",
        "",
        query,
        flags=re.IGNORECASE,
    )
    query = query.strip(" .，,。")
    query = re.sub(r"(?:吧|嘛|吗|呢|么)$", "", query).strip(" .，,。")
    if re.fullmatch(
        r"(?:把|让|请|帮我|麻烦|打开|启动|开启|运行|拉起|直接|\s)+",
        query,
        flags=re.IGNORECASE,
    ):
        return ""
    return "" if query.lower() in _GENERIC_MUSIC_QUERIES else query


def _strip_foreground_scope_prefix(value: str) -> str:
    target = clean(value)
    stripped = re.sub(
        r"^(?:(?:在|向|到|往|至)\s*)?"
        r"(?:(?:当前|这个|该|本|前台|活动|活跃)\s*"
        r"(?:窗口|界面|屏幕|应用|页面|网页|页|视图|ui|app)"
        r"(?:的|里|中|上|内|里面|之中|里的|中的|上的|内的)?|"
        r"(?:当前|这个|该|本|前台)\s*(?:的|里|中|上|内)|"
        r"(?:current|foreground|frontmost)\s+"
        r"(?:window|app|application|page|view|screen|ui)(?:'s)?)\s*",
        "",
        target,
        count=1,
        flags=re.IGNORECASE,
    ).strip(" .，,。")
    return stripped


def clean_target(value: str) -> str:
    target = _strip_foreground_scope_prefix(value)
    target = re.split(
        r"(?:然后|并且|并|再|接着|之后|后|输入|键入|填写|填入|写入|写|and\s+then|then|and|type|enter|fill)",
        target,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    target = re.sub(
        r"\s*(?:按钮|控件|元素|菜单项|菜单|复选框|输入框|文本框|输入栏|"
        r"button|control|element|menu item|menu|checkbox|field|input|text field|text box|textbox)$",
        "",
        target,
        flags=re.IGNORECASE,
    )
    target = re.sub(
        r"\s+(?:in|inside|within|using|with)\s+[A-Za-z][A-Za-z0-9 ._-]{1,40}$",
        "",
        target,
        flags=re.IGNORECASE,
    )
    target = re.sub(
        r"\s*(?:button|control|element|menu item|menu|checkbox|field|input|text field|text box|textbox)$",
        "",
        target,
        flags=re.IGNORECASE,
    )
    target = re.sub(
        r"^(?:在|用|通过)\s*[\w .·-]{1,40}?(?:里|中|上|内|的)\s*",
        "",
        target,
        flags=re.IGNORECASE,
    )
    target = re.sub(r"^(?:的|里|中|上|内)\s*", "", target, flags=re.IGNORECASE)
    target = re.sub(r"^(?:可见的?|visible|shown)\s*", "", target, flags=re.IGNORECASE)
    target = re.sub(r"^(?:搜索框|搜索栏)$", "搜索", target, flags=re.IGNORECASE)
    target = re.sub(r"^(?:地址栏)$", "地址", target, flags=re.IGNORECASE)
    target = re.sub(r"^(?:消息框|聊天框)$", "消息", target, flags=re.IGNORECASE)
    return target.strip(" .，,。")


def clean_type_target(value: str, *, app_name: str = "") -> str:
    named_field_suffix = re.search(
        r"^(?:并|然后|再|接着|之后|后|,|，)?\s*"
        r"(?:把|将)?\s*(?:在|向|到|往)?\s*"
        r"(?P<name>[^。！？!?，,]{1,40}?)\s*"
        r"(?:搜索框|搜索栏|消息框|聊天框|地址栏|输入框|文本框|输入栏)$",
        clean(value),
        flags=re.IGNORECASE,
    )
    if named_field_suffix:
        name = named_field_suffix.group("name").strip(" .，,。")
        clean_app_name = clean(app_name)
        name = _strip_foreground_scope_prefix(name)
        name = re.sub(
            r"^(?:(?:在|通过)\s*|用\s+|(?:in|inside|within|using|with)\s+)",
            "",
            name,
            flags=re.IGNORECASE,
        ).strip(" .，,。")
        if clean_app_name and name.lower().startswith(clean_app_name.lower()):
            name = name[len(clean_app_name) :].strip(" .，,。")
        name = re.sub(
            r"^(?:的|里|中|上|内|里的|中的|上的|内的|"
            r"点击|点一下|点按|单击|按一下|按|click|press|tap)\s*",
            "",
            name,
            flags=re.IGNORECASE,
        ).strip(" .，,。")
        if (
            name
            and name not in {"把", "将", "在", "向", "到", "往"}
            and not _looks_like_type_field_opener_name(name)
        ):
            return name

    raw_field_suffix = re.search(
        r"(?:^|并|然后|再|接着|之后|后|,|，)\s*"
        r"(?:把|将)?\s*(?:在|向|到|in|inside|into)?\s*"
        r"(?P<field>搜索框|搜索栏|消息框|聊天框|地址栏|输入框|文本框|输入栏|"
        r"search box|search field|message field|address bar|input field|text box)$",
        clean(value),
        flags=re.IGNORECASE,
    )
    if raw_field_suffix:
        field = raw_field_suffix.group("field").strip()
        return clean_target(field) or field

    named_field_match = re.search(
        r"(?:名为|叫做|叫)\s*(?P<name>[^。！？!?，,]{1,40}?)\s*(?:的)?\s*"
        r"(?:搜索框|搜索栏|消息框|聊天框|地址栏|输入框|文本框|输入栏)",
        clean(value),
        flags=re.IGNORECASE,
    )
    if named_field_match:
        return f"名为 {named_field_match.group('name').strip()} 的"

    target = clean_target(value)
    target = re.sub(
        r"^(?:打开|启动|切到|聚焦|open|launch|focus|switch to)\s*",
        "",
        target,
        flags=re.IGNORECASE,
    ).strip()
    target = _strip_foreground_scope_prefix(target)
    target = re.sub(
        r"^(?:当前打开|当前已打开|已打开|打开的|正在运行|运行中|当前运行|"
        r"开着|已开启|前台|当前)(?:的)?\s*",
        "",
        target,
        flags=re.IGNORECASE,
    ).strip()
    target = re.sub(
        r"^(?:文档|文本|文章|表单|表格|电子表格|邮件|聊天|消息|浏览器)"
        r"(?:应用(?:程序)?|app|软件|客户端|工具|程序|编辑器|阅读器|查看器|窗口)?"
        r"\s*(?:里|中|上|内|里面|窗口)(?:的)?\s*",
        "",
        target,
        flags=re.IGNORECASE,
    ).strip()
    target = re.sub(r"^(?:给|将|把)\s*", "", target, flags=re.IGNORECASE).strip()
    target = re.sub(
        r"^(?:并|然后|再|接着|之后|后)\s*",
        "",
        target,
        flags=re.IGNORECASE,
    ).strip()
    target = re.sub(
        r"^(?:(?:在|通过)\s*|用\s+|(?:in|inside|within|using|with)\s+)",
        "",
        target,
        flags=re.IGNORECASE,
    ).strip()
    target = re.sub(
        r"^(?:并|然后|再|接着|之后|后)?\s*(?:把|将)\s*",
        "",
        target,
        flags=re.IGNORECASE,
    ).strip()
    clean_app_name = clean(app_name)
    if clean_app_name and target.lower().startswith(clean_app_name.lower()):
        target = target[len(clean_app_name):].strip()
    if clean_app_name:
        target = _strip_app_prefix_from_type_target(target)
    target = re.sub(r"^(?:的|在|里|中|上|in|inside)\s*", "", target, flags=re.IGNORECASE)
    target = re.sub(
        r"^(?:点击|点一下|点按|单击|按一下|按|click|press|tap)\s*",
        "",
        target,
        flags=re.IGNORECASE,
    )
    target = re.sub(r"^.*?(消息框|聊天框)$", r"\1", target, flags=re.IGNORECASE)
    raw_field_match = re.search(
        r"(搜索框|搜索栏|消息框|聊天框|地址栏|输入框|文本框|输入栏|"
        r"search box|search field|message field|address bar|input field|text box)$",
        clean(value),
        flags=re.IGNORECASE,
    )
    if raw_field_match and clean_app_name and target in {"搜索", "消息", "地址"}:
        target = raw_field_match.group(1)
    if not clean_app_name:
        target = re.sub(r"^(?:搜索框|搜索栏)$", "搜索", target, flags=re.IGNORECASE)
        target = re.sub(r"^(?:地址栏)$", "地址", target, flags=re.IGNORECASE)
        target = re.sub(r"^(?:消息框|聊天框)$", "消息", target, flags=re.IGNORECASE)
    return target.strip(" .，,。") or clean_target(value)


def _strip_app_prefix_from_type_target(value: str) -> str:
    return re.sub(
        r"^[\w .·-]{1,40}?(?:的|里(?:的)?|中(?:的)?|上(?:的)?|内(?:的)?)?\s*"
        r"(?=(?:搜索框|搜索栏|消息框|聊天框|地址栏|输入框|文本框|输入栏|"
        r"search box|search field|message field|address bar|input field|text box))",
        "",
        value,
        count=1,
        flags=re.IGNORECASE,
    )


def clean_followup_text(value: str) -> str:
    text = clean(value)
    # Treat punctuation that introduces an explicit payload as syntax rather
    # than text to type. Strip the separator before quotes so `：“hello”`
    # normalizes to `hello` instead of leaving either delimiter behind.
    text = re.sub(r"^(?:[:：]\s*)+", "", text).strip()
    text = re.sub(r"^[\"'`“”‘’]+|[\"'`“”‘’]+$", "", text).strip()
    text = re.sub(r"^(?:[:：]\s*)+", "", text).strip()
    text = re.split(
        r"(?:并且|然后|再|接着|之后|随后|后|并|and\s+then|then|and)?\s*"
        r"(?:按一下|按下|按|敲|点击|点|press|hit|tap)?\s*"
        r"(?:发送|提交|确认|回车键?|搜索|send|submit|confirm|enter|return|search)",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    text = re.sub(r"\s*(?:到|至)?(?:当前|前台)(?:输入框|窗口|应用)?$", "", text, flags=re.IGNORECASE)
    return text.strip(" .，,。")


def role_filter(value: str) -> str:
    lowered = value.lower()
    cleaned = re.sub(
        r"^(?:双击|点击|点一下|点按|单击|按一下|按|点)\s*",
        "",
        clean(value),
        flags=re.IGNORECASE,
    )
    if contains_any(lowered, ["按钮", "button"]):
        return "button"
    if contains_any(lowered, ["菜单", "menu"]):
        return "menu"
    if contains_any(lowered, ["复选框", "checkbox"]):
        return "checkbox"
    if contains_any(
        lowered,
        ["搜索框", "搜索栏", "消息框", "聊天框", "地址栏", "输入框", "文本框", "输入栏", "field", "input", "text"],
    ):
        return "text"
    if cleaned in {"搜索", "登录", "创建", "确认", "发送", "提交"}:
        return "button"
    if re.search(r"\b(?:click|press|tap)\b", lowered):
        if re.search(r"\b(?:in|inside|within)\s+[A-Z][A-Za-z0-9 ._-]*$", value):
            return ""
        return "button"
    return ""


def _clean_window_app_name_hint(value: str) -> str:
    app = clean(value)
    app = re.sub(
        r"^(?:帮我|请|麻烦|能否|能不能|可以|直接|列出|查看|看看|看一下|看下|显示|读取|"
        r"list|show|read|the)\s*",
        "",
        app,
        flags=re.IGNORECASE,
    ).strip()
    app = re.sub(r"\s*(?:窗口|windows?)\s*$", "", app, flags=re.IGNORECASE)
    app = re.sub(
        r"\s*(?:所有|全部|哪些|什么|几个|多少|all|open)\s*$",
        "",
        app,
        flags=re.IGNORECASE,
    )
    app = re.sub(
        r"\s*(?:有|打开了|开了|正在显示|open|opened|running)\s*$",
        "",
        app,
        flags=re.IGNORECASE,
    )
    app = re.sub(r"\s*(?:的)$", "", app, flags=re.IGNORECASE)
    app = app.strip(" .，,。")
    if re.fullmatch(
        r"(?:当前|现在|前台)?(?:所有|全部)?(?:打开|打开的|开启|开着|正在显示|open|opened)?",
        app,
        flags=re.IGNORECASE,
    ):
        return ""
    if re.fullmatch(
        r"(?:当前|现在|前台|这个|该)?(?:应用|app|软件|程序|窗口|window)(?:的)?",
        app,
        flags=re.IGNORECASE,
    ):
        return ""
    generic = {
        "",
        "app",
        "application",
        "desktop",
        "window",
        "windows",
        "current",
        "active",
        "foreground",
        "all",
        "应用",
        "应用程序",
        "桌面",
        "窗口",
        "所有",
        "全部",
        "当前",
        "前台",
    }
    return "" if app.lower() in generic else app


def _clean_window_title_hint(value: str) -> str:
    title = clean(value)
    title = re.sub(
        r"^(?:标题(?:包含|为)?|名为|叫|titled|called|matching|containing)\s*",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\s*(?:窗口|window)$", "", title, flags=re.IGNORECASE)
    return title.strip(" .，,。")


def _looks_like_ui_inspection_request(value: str, lowered: str) -> bool:
    if _looks_like_ui_click_advice_request(value):
        return True
    if _looks_like_foreground_mutation(value, lowered):
        return False
    return bool(
        re.search(
            r"(?:当前|现在|这个|前台|该)?(?:窗口|界面|屏幕|应用|app|ui)"
            r".{0,8}(?:文字|文本|内容|正文)"
            r".{0,8}(?:是什么|是啥|有哪些|有什么|读取|读一下|查看|看看|识别)?",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:读取|阅读|读一下|读下|读一读|读|查看|看看|观察|识别|提取|抓取|获取)"
            r".{0,8}(?:当前|现在|这个|前台|该)?(?:窗口|界面|屏幕|应用|app|ui)"
            r".{0,8}(?:文字|文本|内容|正文)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:读取|阅读|读一下|读下|读一读|读|查看|观察|识别|获取)"
            r".{0,40}(?:当前|现在|这个|前台|该)?(?:窗口|界面|屏幕|应用|app|ui)"
            r"(?:一下|下)?(?:可以吗|好吗|好么|行吗|吗|嘛|吧|呢)?$",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:当前|现在|这个|前台|该)?(?:窗口|界面|屏幕|应用|app|ui)?"
            r".{0,10}(?:有哪些|有什么|列出|列一下|显示|查看|看看|看一下|读取|识别)"
            r".{0,10}(?:控件|按钮|输入框|文本框|元素|选项|ui|可点击|可操作)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:控件|按钮|输入框|文本框|元素|选项|ui|可点击|可操作)"
            r".{0,10}(?:有哪些|有什么|列表|列一下|显示|查看|看看|看一下|读取|识别)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:read|inspect|show|extract)\b.{0,16}\b"
            r"(?:current|this|active|foreground)\s+(?:window|ui|interface|screen)\b"
            r"(?:.{0,16}\b(?:text|content)\b)?",
            lowered,
        )
        or re.search(
            r"\b(?:read|inspect|show|view|check)\s+(?:the\s+)?"
            r"[a-z][a-z0-9 ._-]{1,40}?\s+(?:ui|interface|app|application|window)\b",
            lowered,
        )
        or re.search(
            r"\b[a-z][a-z0-9 ._-]{1,40}?\s+"
            r"(?:read|inspect|show|view|check)\s+"
            r"(?:ui|interface|app|application|window|ui elements|buttons|text fields|controls)\b",
            lowered,
        )
        or re.search(
            r"\b(?:list|show|read|inspect)\b.{0,24}\b(?:ui elements|buttons|text fields|controls)\b",
            lowered,
        )
        or re.search(r"\b(?:what|which)\b.{0,24}\b(?:buttons|controls|ui elements)\b", lowered)
        or re.search(
            r"\b(?:visible|shown|available)\s+(?:buttons|controls|ui elements|text fields)\b",
            lowered,
        )
        or re.search(
            r"\bwhere\s+(?:is|are)\s+(?:the\s+)?[^.!?]{0,40}?"
            r"(?:button|control|ui element|text field)\b",
            lowered,
        )
        or re.search(
            r"(?:控件|按钮|输入框|文本框|元素|选项|ui|可点击|可操作)"
            r".{0,12}(?:在哪|在哪里|哪里|位置|坐标)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(r"\bwhat\s+can\s+i\s+(?:click|press|use)\b", lowered)
    )


def ui_control_presence_hint(text: str) -> dict[str, Any] | None:
    value = clean(text)
    lowered = value.lower()
    if not re.search(
        r"(?:有没有|是否有|有无|是否存在|是否显示|能否看到|能不能看到|看得到|找得到|"
        r"is\s+there|are\s+there|has|have|contains?|visible|shown|available)",
        value,
        flags=re.IGNORECASE,
    ):
        return None
    if not re.search(
        r"(?:控件|按钮|输入框|文本框|输入栏|菜单项|菜单|复选框|元素|选项|"
        r"button|control|ui\s+element|text\s+field|textbox|input|menu|checkbox)",
        value,
        flags=re.IGNORECASE,
    ):
        return None
    app_name = _ui_control_presence_app_name_hint(value)
    payload: dict[str, Any] = {
        "role_filter": _ui_role_filter_hint(value),
        "limit": 80,
    }
    if app_name:
        payload["app_name"] = app_name
    return payload


def _ui_control_presence_app_name_hint(value: str) -> str:
    current_scope = (
        r"(?:(?:当前|现在|这个|前台|该)\s*(?:窗口|界面|屏幕|应用|app|ui)|"
        r"(?:current|active|foreground|this)\s*(?:window|interface|screen|app|application|ui)?)"
    )
    explicit_app_before_current_scope = re.search(
        r"^(?:帮我|请|麻烦|能否|能不能|可以|直接|检查|查看|看看|确认|识别)?\s*"
        r"(?!当前|现在|这个|前台|该\b)"
        r"[\w .·-]{1,40}?\s*"
        r"(?:当前|现在|这个|前台|该)?\s*(?:窗口|界面|屏幕|应用|app|ui|UI)",
        value,
        flags=re.IGNORECASE,
    )
    if re.search(current_scope, value, flags=re.IGNORECASE) and not explicit_app_before_current_scope:
        return ""
    patterns = (
        r"\b(?:what|which)\s+(?:buttons|controls|ui\s+elements|text\s+fields)\s+"
        r"(?:are\s+)?(?:visible|shown|available|there)?\s*(?:in|on|for|of)\s+"
        r"(?:the\s+)?(?P<app_en_visible>[A-Za-z][A-Za-z0-9 ._-]{1,40}?)\b(?:[.!?]|$)",
        r"^(?:帮我|请|麻烦|能否|能不能|可以|直接|检查|查看|看看|确认|识别)?\s*"
        r"(?P<app_surface>[\w .·-]{1,40}?)\s*"
        r"(?:当前|现在|这个|前台|该)?\s*(?:窗口|界面|屏幕|应用|app|ui|UI)?"
        r"(?:里|中|上|内)?\s*"
        r"(?:有没有|是否有|有无|是否存在|是否显示|能否看到|能不能看到|看得到|找得到)",
        r"^(?:帮我|请|麻烦|能否|能不能|可以|直接|检查|查看|看看|确认|识别)?\s*"
        r"(?P<app>[\w .·-]{1,40}?)\s*(?:里|中|上|内)?\s*"
        r"(?:有没有|是否有|有无|是否存在|是否显示|能否看到|能不能看到|看得到|找得到)",
        r"\b(?:check|see|inspect|verify)\s+(?:whether|if)?\s*(?:the\s+)?"
        r"(?P<app_en>[A-Za-z][A-Za-z0-9 ._-]{1,40}?)\s+"
        r"(?:has|have|contains?|shows?)\b",
        r"\b(?:is\s+there|are\s+there)\s+[^.!?]{1,50}?\s+"
        r"(?:in|on|inside)\s+(?:the\s+)?"
        r"(?P<app_en_post>[A-Za-z][A-Za-z0-9 ._-]{1,40}?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        raw_app = next(
            (
                item
                for item in match.groupdict().values()
                if item is not None and str(item).strip()
            ),
            "",
        )
        app_name = _clean_ui_app_name_hint(raw_app)
        if app_name:
            return app_name
    return ""


def _looks_like_foreground_mutation(value: str, lowered: str) -> bool:
    if ui_control_presence_hint(value):
        return False
    if re.search(r"\bwhat\s+can\s+i\s+(?:click|press|use)\b", lowered):
        return False
    if _looks_like_ui_click_advice_request(value):
        return False
    return bool(
        re.search(
            r"(?:双击|点击|点一下|点按|单击|按一下|按下|输入|键入|填写|填入|写入|发送|提交)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(r"\b(?:double\s+click|click|press|tap|type|enter|fill|send|submit)\b", lowered)
    )


def _looks_like_ui_click_advice_request(value: str) -> bool:
    text = clean(value)
    lowered = text.lower()
    return bool(
        re.search(
            r"(?:当前|现在|这个|前台|该)?(?:窗口|界面|屏幕|应用|app|ui).{0,40}"
            r"(?:下一步|接下来|然后|现在|我|用户)?(?:该|应该|可以|能|需要).{0,8}"
            r"(?:点|点击|点按|按|操作|做)(?:哪里|哪个|什么)",
            text,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:读一下|读取|查看|看看|观察|识别).{0,40}"
            r"(?:下一步|接下来|然后|现在|我|用户)?(?:该|应该|可以|能|需要).{0,8}"
            r"(?:点|点击|点按|按|操作|做)(?:哪里|哪个|什么)",
            text,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:检查|查看|看看|列出|列一下|显示|读取|识别)?"
            r".{0,12}(?:当前|现在|这个|前台|该)?(?:窗口|界面|屏幕|应用|app|ui)?"
            r".{0,16}(?:有哪些|有什么|哪些|可用|可见|可以|能)"
            r".{0,16}(?:按钮|控件|元素|选项|ui|可点击|可操作)"
            r".{0,12}(?:可以|能)?(?:点击|点|点按|按|操作)?",
            text,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:what|where)\s+(?:can|should)\s+i\s+"
            r"(?:click|press|tap|do|use)\b",
            lowered,
        )
        or re.search(
            r"\b(?:what|which)\s+(?:buttons|controls|ui elements)\s+can\s+i\s+"
            r"(?:click|press|tap|do|use)\b",
            lowered,
        )
    )


def _ui_role_filter_hint(value: str) -> str:
    if re.search(r"(?:文字|文本|正文|内容|content|text)", value, flags=re.IGNORECASE):
        return "text"
    if re.search(r"(?:按钮|button)", value, flags=re.IGNORECASE):
        return "button"
    if re.search(r"(?:输入框|文本框|输入栏|text field|textbox|input)", value, flags=re.IGNORECASE):
        return "text"
    if re.search(r"(?:菜单|menu)", value, flags=re.IGNORECASE):
        return "menu"
    if re.search(r"(?:复选框|checkbox)", value, flags=re.IGNORECASE):
        return "checkbox"
    return ""


def _ui_inspection_app_name_hint(value: str) -> str:
    patterns = (
        r"\b(?:list|show|read|inspect)\s+(?:the\s+)?"
        r"(?:ui\s+elements|buttons|text\s+fields|controls)\s+(?:in|on|for|of)\s+(?P<app_en>[^.!?]+)",
        r"\b(?:what|which)\s+(?:buttons|controls|ui\s+elements|text\s+fields)\s+"
        r"(?:are\s+)?(?:visible|shown|available|there)?\s*(?:in|on|for|of)\s+(?P<app_en2>[^.!?]+)",
        r"\bwhat\s+can\s+i\s+(?:click|press|use)\s+(?:in|on)\s+(?P<app_en3>[^.!?]+)",
        r"\b(?:list|show|read|inspect)\s+(?P<app_en4>[^.!?]+?)\s+"
        r"(?:ui\s+elements|buttons|text\s+fields|controls)",
        r"\b(?:read|inspect|show|view|check)\s+(?:the\s+)?"
        r"(?P<app_en_ui>[^.!?]+?)\s+(?:ui|interface|app|application|window)\b",
        r"\b(?P<app_en_post_ui>[A-Za-z][A-Za-z0-9 ._-]{1,40}?)\s+"
        r"(?:read|inspect|show|view|check)\s+"
        r"(?:ui|interface|app|application|window|ui\s+elements|buttons|text\s+fields|controls)\b",
        r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:列出|查看|看看|看一下|看下|显示|读取|观察|识别)\s*"
        r"(?P<app_surface>[^。！？!?，,]+?)\s*(?:的)?\s*"
        r"(?:当前|现在|这个|前台|该)?(?:窗口|界面|屏幕|应用|app|ui)",
        r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:列出|查看|看看|看一下|看下|显示|读取|观察|识别)\s*"
        r"(?P<app>[^。！？!?，,]+?)\s*(?:有哪些|有什么|有啥|有哪个|有哪几个)"
        r".{0,6}(?:控件|按钮|输入框|文本框|元素|ui|可点击|可操作)",
        r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?:列出|查看|看看|看一下|看下|显示|读取|观察|识别)\s*"
        r"(?P<app2>[^。！？!?，,]+?)\s*(?:的)?\s*(?:控件|按钮|输入框|文本框|元素|ui|可点击|可操作)",
        r"(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
        r"(?P<app3>[^。！？!?，,]+?)\s*(?:有哪些|有什么|有啥|有哪个|有哪几个)"
        r".{0,6}(?:控件|按钮|输入框|文本框|元素|ui|可点击|可操作)",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        raw_app = next(
            (
                item
                for item in match.groupdict().values()
                if item is not None and str(item).strip()
            ),
            "",
        )
        app_name = _clean_ui_app_name_hint(raw_app)
        if app_name:
            return app_name
    return ""


def _clean_ui_app_name_hint(value: str) -> str:
    app = clean(value)
    app = re.sub(
        r"^(?:帮我|请|麻烦|你能|能否|能不能|可以|直接|把|将|列出|查看|看看|看一下|看下|显示|读取|观察|识别|检查|确认|"
        r"list|show|read|inspect|the)\s*",
        "",
        app,
        flags=re.IGNORECASE,
    ).strip()
    app = re.sub(r"^(?:打开|启动|开启|运行|拉起|切到|聚焦)\s*", "", app)
    app = re.sub(
        r"^(?:open|launch|start|focus|activate|switch\s+to)\s+",
        "",
        app,
        flags=re.IGNORECASE,
    ).strip()
    app = re.sub(r"^(?:在|用|通过)\s*", "", app, flags=re.IGNORECASE).strip()
    app = re.sub(r"\s*(?:并|然后|再|接着|之后|后|and|then)\s*$", "", app, flags=re.IGNORECASE)
    app = re.sub(
        r"\s*(?:里|里面|中|上|内)(?:的)?$",
        "",
        app,
        flags=re.IGNORECASE,
    ).strip()
    app = re.split(
        r"(?:看看|看一下|看下|查看|检查|确认|读一下|读一读|读|读取|观察|识别|有哪些|有什么|有啥|"
        r"\b(?:look\s+at|check|inspect|view|show\s+me|show|read|which|what)\b)",
        app,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    app = re.sub(
        r"\s*(?:里|里面|中|上|内)(?:的)?$",
        "",
        app,
        flags=re.IGNORECASE,
    ).strip()
    app = re.sub(r"\s*(?:并|然后|再|接着|之后|后|and|then)\s*$", "", app, flags=re.IGNORECASE).strip()
    called_app_match = re.match(
        r"^(?:一个|一款|这个|那个)?(?:叫|名叫|名称是|名字是)\s*(?P<app>.+?)\s*(?:的)?(?:应用(?:程序)?|软件)$",
        app,
        flags=re.IGNORECASE,
    )
    if called_app_match:
        app = called_app_match.group("app").strip()
    app = re.sub(
        r"^(?:一个|一款|这个|那个)?"
        r"(?:任意(?:的)?|(?:(?:我)?(?:没|没有)提过的|从未提过的|没见过的|未知(?:的)?|陌生的|新(?:的)?))\s*"
        r"(?:新\s*)?(?:应用(?:程序)?|软件)?"
        r"(?:\s*(?:叫|名叫|名称是|名字是))?\s*",
        "",
        app,
        flags=re.IGNORECASE,
    ).strip(" .，,。")
    app = re.sub(
        r"^(?:(?:an?|the)\s+)?"
        r"(?:(?:any|new|unknown|unmentioned|unfamiliar)\s+)+"
        r"(?:(?:app|application|software)\s+)?"
        r"(?:(?:called|named)\s+)?",
        "",
        app,
        flags=re.IGNORECASE,
    ).strip(" .，,。")
    app = re.sub(
        r"\s*(?:有哪些|有什么|有啥|有哪个|有哪几个|visible|shown|available|there)?\s*"
        r"(?:控件|按钮|输入框|文本框|元素|选项|ui|可点击|可操作|"
        r"ui\s+elements|buttons|text\s+fields|controls)?$",
        "",
        app,
        flags=re.IGNORECASE,
    )
    app = re.sub(
        r"\s*(?:当前|现在|这个|前台|该|current|active|foreground|this)$",
        "",
        app,
        flags=re.IGNORECASE,
    ).strip()
    app = re.sub(
        r"\s*(?:当前|现在|这个|前台|该|current|active|foreground|this)?\s*"
        r"(?:界面|窗口|屏幕|页面|网页|标签页|应用|"
        r"ui|interface|window|screen|page|webpage|app|application)$",
        "",
        app,
        flags=re.IGNORECASE,
    ).strip()
    app = re.split(
        r"\s*(?:并|然后|再|接着|之后|后|,|，)?\s*"
        r"(?:保存(?:成|为)?|导出|输出|写入|生成)",
        app,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    app = re.split(
        r"\s*(?:and\s+then|then|and)?\s*(?:save|export|output|write)\b",
        app,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    app = re.sub(r"\s*(?:的)$", "", app).strip()
    if re.fullmatch(
        r"(?:当前|现在|这个|前台|该)?"
        r"(?:应用|app|界面|窗口|屏幕|页面|网页|标签页|"
        r"ui|interface|window|screen|page|webpage)",
        app,
        flags=re.IGNORECASE,
    ):
        return ""
    generic = {
        "",
        "app",
        "application",
        "desktop",
        "window",
        "interface",
        "screen",
        "page",
        "webpage",
        "ui",
        "current",
        "active",
        "foreground",
        "my",
        "me",
        "its",
        "any",
        "任意",
        "the",
        "this",
        "and",
        "then",
        "并",
        "然后",
        "再",
        "接着",
        "之后",
        "后",
        "先",
        "你能",
        "我",
        "我的",
        "我现在",
        "我现在的",
        "现在的",
        "当前的",
        "这个",
        "那个",
        "该",
        "应用",
        "应用程序",
        "桌面",
        "窗口",
        "界面",
        "屏幕",
        "图",
        "个屏",
        "截屏",
        "截图",
        "截个屏",
        "截个图",
        "页面",
        "网页",
        "标签页",
        "当前窗口",
        "当前窗口里",
        "当前窗口中",
        "当前界面",
        "当前界面里",
        "当前界面中",
        "当前屏幕",
        "当前屏幕上",
        "前台窗口",
        "前台界面",
        "当前页面",
        "当前页面里",
        "当前页面中",
        "当前网页",
        "当前网页里",
        "当前网页中",
        "这个页面",
        "这个网页",
        "当前",
        "前台",
        "并",
        "然后",
        "再",
        "接着",
        "之后",
        "后",
    }
    return "" if app.lower().strip(" .，,。") in generic else app.strip(" .，,。")


def _looks_like_screen_capture_request(value: str, lowered: str) -> bool:
    return bool(
        re.search(r"(?:截(?:一下|下)图|截个?图|截个?屏|截图|截屏|屏幕截图|抓屏|拍屏)", value)
        or re.search(
            r"(?:当前|现在|这个|我的|我现在的)?(?:屏幕|桌面|界面|画面|窗口)"
            r".{0,8}(?:截图|截屏|截一下|截个图|抓屏|拍屏)",
            value,
        )
        or re.search(
            r"(?:截取|截图|截屏|截一下|截个图|截|抓屏|拍屏)"
            r".{0,16}(?:当前|现在|这个|我的|我现在的)?(?:屏幕|桌面|界面|画面|窗口)",
            value,
        )
        or re.search(r"(?:拍一下|拍下|拍一张|拍个).{0,8}(?:屏幕|桌面|界面|画面|窗口)", value)
        or re.search(
            r"(?:看一下|看看|看下|查看|读取|观察(?:一下|下)?|识别(?:一下|下)?)"
            r".{0,12}(?:当前|现在|这个|我的|我现在的)?(?:屏幕|桌面|界面|画面)",
            value,
        )
        or _looks_like_app_observation_capture_request(value)
        or re.search(
            r"(?:当前|现在|这个|我的|我现在的)?(?:屏幕|桌面|界面|画面)"
            r".{0,8}(?:是什么|是啥|内容|画面|有什么|有啥)",
            value,
        )
        or re.search(
            r"(?:打开|启动|开启|拉起|切到|聚焦|把|将).{1,40}"
            r"(?:看看|看一下|看下|查看|读取|读一下|读下|看).{0,16}"
            r"(?:消息|聊天|未读|新消息|cpu|CPU)",
            value,
        )
        or re.search(
            r"\b(?:open|launch|start|focus)\s+.+?\s+(?:and|then)\s+"
            r"(?:read|check|view|look\s+at)\s+(?:messages?|cpu)\b",
            lowered,
        )
        or "take a screenshot" in lowered
        or "capture the screen" in lowered
        or "screen capture" in lowered
        or re.search(
            r"\b(?:capture|screenshot|grab)\s+(?:the\s+)?"
            r"[a-z][a-z0-9 ._-]{1,40}?\s+(?:screen|window|interface|ui)\b",
            lowered,
        )
        or re.search(
            r"\b[a-z][a-z0-9 ._-]{1,40}?\s+(?:screen|window|interface|ui)?\s*screenshot\b",
            lowered,
        )
        or re.search(r"\bscreenshot\s+(?:my|the|this|current)?\s*(?:screen|desktop)?\b", lowered)
        or re.search(
            r"\b(?:look at|inspect|view|read|show me|show)\s+"
            r"(?:my|the|this|current)?\s*(?:screen|desktop|interface|ui)\b",
            lowered,
        )
        or re.search(r"\bwhat(?:'s| is)?\s+on\s+(?:my|the|this|current)?\s*(?:screen|desktop)\b", lowered)
    )


def _looks_like_app_observation_capture_request(value: str) -> bool:
    match = re.fullmatch(
        r"(?P<app>[^.。！！？?，,]{1,40}?)\s*"
        r"(?:看一下|看看|看下|查看|观察(?:一下|下)?)",
        value,
        flags=re.IGNORECASE,
    )
    if match is None:
        return False
    app_name = str(match.group("app") or "").strip()
    if is_legacy_app_name_hint(app_name):
        return True
    # Dynamic app names are supported when they look like a product name,
    # while conversational subjects such as "这个项目" stay out of the capture path.
    return bool(re.fullmatch(r"[A-Z][A-Za-z0-9 ._-]{1,39}", app_name))


def _explicit_screen_capture_requested(value: str, lowered: str) -> bool:
    return bool(
        re.search(
            r"(?:截(?:一下|下)图|截个?图|截个?屏|截图|截屏|屏幕截图|抓屏|拍屏)",
            value,
        )
        or "take a screenshot" in lowered
        or "capture the screen" in lowered
        or "screen capture" in lowered
        or re.search(r"\bscreenshot\b", lowered)
    )


def _explicit_find_click_target_requested(value: str) -> bool:
    return bool(
        re.search(
            r"(?:找到|找|定位|选择|选中)\s*[^。！？!?，,]+?"
            r"(?:按钮|控件|元素|菜单项|菜单|复选框|项目|条目)?\s*"
            r"(?:并|然后|再|之后|后)?\s*(?:双击|点击|点一下|点按|单击|打开|进入)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:find|locate|choose|select)\s+(?:the\s+)?[^.!?,]+?\s*"
            r"(?:and\s+then|then|and)?\s*(?:click|press|tap|open)",
            value,
            flags=re.IGNORECASE,
        )
    )


def _screen_capture_app_name_hint(value: str) -> str:
    patterns = (
        r"^(?:截取|截图|截屏|截一下|截下|截个图|截个屏|截一屏|截|抓屏|拍屏)\s*"
        r"(?P<app_capture>[^。！？!?，,]+?)\s*(?:的)?\s*"
        r"(?:当前|现在|这个|前台)?(?:窗口|界面|画面|屏幕)?$",
        r"^(?P<app_suffix>[^。！？!?，,]+?)\s*(?:的)?\s*"
        r"(?:当前|现在|这个|前台)?(?:窗口|界面|画面|屏幕)?\s*"
        r"(?:截图|截屏|截一下|截下|截个图|抓屏|拍屏)$",
        r"(?:看一下|看看|看下|查看|读取|观察(?:一下|下)?|识别(?:一下|下)?)\s*"
        r"(?P<app>[^。！？!?，,]+?)\s*(?:界面|画面)",
        r"(?P<app2>[^。！？!?，,]+?)\s*(?:界面|画面).{0,8}(?:截图|截屏|看一下|看看|查看|观察)",
        r"(?P<app3>[^。！？!?，,]+?)\s*(?:看一下|看看|看下|查看|观察(?:一下|下)?)\s*(?:界面|画面)",
        r"^(?P<app_observe>[^。！？!?，,]+?)\s*(?:看一下|看看|看下|查看|观察(?:一下|下)?)$",
        r"^(?:把|将)?\s*(?P<app_preopen>[^。！？!?，,]+?)\s*"
        r"(?:打开|启动|开启|拉起)\s*(?:然后|并|再|接着|之后)?\s*"
        r"(?:看看|看一下|看下|查看|读取|读一下|读下|看).{0,16}"
        r"(?:消息|聊天|未读|新消息|cpu|CPU)",
        r"^(?:打开|启动|开启|拉起|切到|聚焦)?\s*(?P<app_messages>[^。！？!?，,]+?)\s*"
        r"(?:然后|并|再|接着|之后)?\s*"
        r"(?:看看|看一下|看下|查看|读取|读一下|读下|看).{0,16}"
        r"(?:消息|聊天|未读|新消息|cpu|CPU)",
        r"\b(?:open|launch|start|focus)\s+(?P<app_messages_en>[A-Za-z][A-Za-z0-9 ._-]{1,40}?)\s+"
        r"(?:and|then)\s+(?:read|check|view|look\s+at)\s+(?:messages?|cpu)\b",
        r"\b(?:capture|screenshot|grab)\s+(?:the\s+)?"
        r"(?P<app_capture_en>[A-Za-z][A-Za-z0-9 ._-]{1,40}?)\s+"
        r"(?:screen|window|interface|ui)\b",
        r"\b(?P<app_screenshot_en>[A-Za-z][A-Za-z0-9 ._-]{1,40}?)\s+"
        r"(?:screen|window|interface|ui)?\s*screenshot\b",
        r"\b(?:look at|inspect|view|show me|show)\s+(?P<app_en>.+?)\s+"
        r"(?:screen|interface|ui)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        raw_app = next(
            (
                item
                for item in match.groupdict().values()
                if item is not None and str(item).strip()
            ),
            "",
        )
        app_name = _clean_ui_app_name_hint(raw_app)
        if app_name:
            return app_name
    return ""


def _clean_management_app_name_hint(value: str) -> str:
    app = clean(value)
    app = re.sub(
        r"^(?:你能(?:不能)?(?:帮我)?|你可以(?:帮我)?|帮我|请|麻烦|能否|能不能|可以|直接|把|将|the)\s*",
        "",
        app,
        flags=re.IGNORECASE,
    )
    app = re.sub(r"\s*(?:并|然后|再|接着|之后|后|then)\s*$", "", app, flags=re.IGNORECASE)
    app = re.sub(
        r"^(?:检查一下|检查|查看|看一下|看下|看看|确认|验证|核对|打开|启动|开启|运行|拉起|切到|聚焦)\s*",
        "",
        app,
    )
    app = re.sub(
        r"^(?:(?:check|see)\s+(?:if|whether)\s+|verify\s+|confirm\s+|"
        r"open|launch|start|focus|activate|bring)\s+|^switch\s+to\s+",
        "",
        app,
        flags=re.IGNORECASE,
    )
    app = re.sub(
        r"\s*(?:一下|下|起来|掉|显示出来|还原|恢复|取消隐藏|隐藏|藏起来|收起|收起来|"
        r"调出来|打开|启动|开启|运行|拉起|切到|聚焦|到前台|切到前台|置前|前台|叫出来|"
        r"open|launch|start|focus|activate|bring|"
        r"最小化|退出|关闭|关掉|结束|终止|show|restore|unhide|hide|minimi[sz]e|"
        r"quit|close|exit|terminate|please|pls|吗|嘛|呢|吧|么|\?|？)$",
        "",
        app,
        flags=re.IGNORECASE,
    )
    app = re.sub(r"\s*(?:当前|现在)?(?:正在|正|是否)?$", "", app, flags=re.IGNORECASE)
    app = app.strip(" .，,。")
    generic = {
        "",
        "app",
        "application",
        "desktop",
        "window",
        "应用",
        "应用程序",
        "桌面",
        "窗口",
        "当前",
        "前台",
        "来",
    }
    return "" if app.lower() in generic else app


def _is_foreground_window_close_request(value: str, lowered: str) -> bool:
    return bool(
        re.search(
            r"(?:关闭(?:一下|下)?|关掉|关上|关(?:一下|下|了)?)\s*"
            r"(?:当前|现在|前台|这个|该)?\s*(?:窗口|window)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:当前|现在|前台|这个|该)?\s*(?:窗口|window)\s*(?:关闭|关掉|关上|关(?:一下|下|了)?)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:close|dismiss)\s+(?:the\s+)?(?:current|foreground|active|this)\s+window\b",
            lowered,
        )
    )


def _is_foreground_app_quit_request(value: str, lowered: str) -> bool:
    return bool(
        re.search(
            r"(?:退出|关闭|关掉|结束|终止)\s*(?:当前|现在|前台|这个|该)?\s*(?:应用|app|软件|程序)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:当前|现在|前台|这个|该)?\s*(?:应用|app|软件|程序)\s*(?:退出|关闭|关掉|结束|终止)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:quit|close|exit|terminate)\s+(?:the\s+)?"
            r"(?:current|foreground|active|this)\s+(?:app|application)\b",
            lowered,
        )
    )


def _is_foreground_window_minimize_request(value: str, lowered: str) -> bool:
    return bool(
        re.search(
            r"(?:最小化|收起|收起来|隐藏)\s*(?:当前|现在|前台|这个|该)?\s*(?:窗口|window)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:当前|现在|前台|这个|该)?\s*(?:窗口|window)\s*(?:最小化|收起|收起来|隐藏)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:minimi[sz]e|hide)\s+(?:the\s+)?(?:current|foreground|active|this)\s+window\b",
            lowered,
        )
        or re.search(
            r"(?:最小化)\s*(?:当前|现在|前台|这个|该)\s*(?:应用|app|软件|程序)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:当前|现在|前台|这个|该)\s*(?:应用|app|软件|程序)\s*(?:最小化)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\bminimi[sz]e\s+(?:the\s+)?(?:current|foreground|active|this)\s+"
            r"(?:app|application)\b",
            lowered,
        )
    )


def _is_foreground_app_hide_request(value: str, lowered: str) -> bool:
    return bool(
        re.search(
            r"(?:隐藏|收起|藏起|藏起来)(?:一下|下)?\s*"
            r"(?:当前|现在|前台|这个|该)?\s*(?:应用|app|软件|程序)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:当前|现在|前台|这个|该)?\s*(?:应用|app|软件|程序)\s*"
            r"(?:隐藏|收起|藏起|藏起来)(?:一下|下)?",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\bhide\s+(?:the\s+)?(?:current|foreground|active|this)\s+(?:app|application)\b",
            lowered,
        )
    )


def _is_show_all_hidden_apps_request(value: str, lowered: str) -> bool:
    return bool(
        re.search(
            r"(?:显示|展示|恢复|还原|取消隐藏)\s*(?:所有|全部)?\s*(?:已)?隐藏(?:的)?\s*(?:应用|app|软件|程序)?",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:所有|全部)?\s*(?:已)?隐藏(?:的)?\s*(?:应用|app|软件|程序)\s*"
            r"(?:显示|展示|恢复|还原|取消隐藏)(?:出来)?",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:show|restore|unhide)\s+(?:all\s+)?hidden\s+(?:apps?|applications?)\b",
            lowered,
        )
        or re.search(
            r"\bshow\s+all\s+(?:apps?|applications?)\b",
            lowered,
        )
    )


def _parse_hotkey_combo(value: str) -> dict[str, Any] | None:
    combo = clean(value)
    combo = re.sub(r"\s*(?:吗|嘛|呢|please)$", "", combo, flags=re.IGNORECASE).strip()
    if re.search(r"(?:to\s+send|发送|提交|确认)", combo, flags=re.IGNORECASE):
        return None
    combo = re.sub(r"\bkey\b", " ", combo, flags=re.IGNORECASE)
    return legacy_parse_hotkey_combo(combo)


def _safe_shortcut_action_from_hotkey_hint(value: str) -> str:
    hotkey = hotkey_hint(value)
    if not hotkey:
        return ""
    key = str(hotkey.get("key") or "").strip().lower()
    modifiers = frozenset(str(item).strip().lower() for item in hotkey.get("modifiers") or [])
    mapping = {
        ("c", frozenset({"command"})): "copy",
        ("v", frozenset({"command"})): "paste",
        ("a", frozenset({"command"})): "select_all",
        ("z", frozenset({"command"})): "undo",
        ("z", frozenset({"command", "shift"})): "redo",
        ("f", frozenset({"command"})): "find",
        ("l", frozenset({"command"})): "focus_address_bar",
        ("t", frozenset({"command"})): "new_tab",
        ("n", frozenset({"command"})): "new_window",
        ("n", frozenset({"command", "shift"})): "new_private_window",
        ("w", frozenset({"command"})): "close_tab",
        ("r", frozenset({"command"})): "refresh",
        ("d", frozenset({"command"})): "bookmark_page",
        ("y", frozenset({"command"})): "show_history",
        ("i", frozenset({"command", "option"})): "open_devtools",
        ("]", frozenset({"command"})): "browser_forward",
        ("[", frozenset({"command"})): "browser_back",
        ("t", frozenset({"command", "shift"})): "reopen_closed_tab",
    }
    return mapping.get((key, modifiers), "")


def _safe_shortcut_action_from_phrase(value: str) -> str:
    phrase = re.sub(
        r"^(?:你能帮我|你可以帮我|可以帮我|能帮我|帮我|请|麻烦|能否|能不能|可以|直接|"
        r"can\s+you|could\s+you|would\s+you|please)\s*",
        "",
        clean(value),
        flags=re.IGNORECASE,
    )
    phrase = re.sub(r"\s*[?.!。！？]+$", "", phrase)
    phrase = re.sub(r"^(?:please)\s*", "", phrase, flags=re.IGNORECASE)
    for _ in range(2):
        phrase = re.sub(
            r"\s*(?:一下|下|一次|可以吗|好吗|好么|行吗|吗|嘛|吧|呢|please)$",
            "",
            phrase,
            flags=re.IGNORECASE,
        )
    normalized = re.sub(r"\s+", "", phrase).lower()
    normalized = re.sub(
        r"^(复制|拷贝|粘贴|全选|撤销|重做|查找|搜索|刷新|后退|前进)"
        r"(?:一下|下(?!一页)|一次)",
        r"\1",
        normalized,
    )
    scoped_normalized = _strip_foreground_shortcut_scope(normalized)
    if scoped_normalized != normalized:
        scoped_action = _safe_shortcut_action_from_phrase(scoped_normalized)
        if scoped_action:
            return scoped_action
    if (
        re.fullmatch(
            r"(?:把|将)?(?:当前|这个|该)?(?:网页|页面|页|标签页)?(?:链接|网址|地址)"
            r"(?:复制|拷贝|复制给我|拷贝给我|放到剪贴板|放进剪贴板|放到系统剪贴板|放进系统剪贴板)",
            normalized,
        )
        or re.fullmatch(
            r"(?:复制|拷贝)(?:当前|前台)?(?:窗口|应用|app)?(?:网页|页面|页|标签页)?(?:链接|网址|地址)",
            normalized,
        )
        or re.fullmatch(
            r"(?:copy|put)(?:the)?(?:current|active|foreground)?(?:page|tab|window|app|application)?(?:link|url)"
            r"(?:to(?:the)?(?:system)?clipboard|tome)?",
            normalized,
        )
    ):
        return "copy_current_page_link"
    if re.fullmatch(
        r"(?:把|将)?(?:当前|这个|该)?(?:选中|选中的)?(?:文本|文字|内容)"
        r"(?:复制|拷贝)(?:到|至|进|放到|放进)?(?:系统)?(?:剪贴板|粘贴板)",
        normalized,
    ) or re.fullmatch(
        r"(?:copy|put)(?:the)?(?:current|selected)?(?:text|selection|content)"
        r"(?:to(?:the)?(?:system)?clipboard)?",
        normalized,
    ):
        return "copy"
    if re.fullmatch(
        r"(?:复制|拷贝)(?:当前)?(?:选中|选中的)(?:文本|文字|内容)",
        normalized,
    ):
        return "copy"
    screenshot_action = _screenshot_safe_shortcut_action(normalized)
    if screenshot_action:
        return screenshot_action
    mapping = {
        "复制": "copy",
        "复制这个": "copy",
        "复制选中内容": "copy",
        "复制选中的内容": "copy",
        "复制选中文本": "copy",
        "复制当前选中内容": "copy",
        "复制当前选中文本": "copy",
        "当前链接复制给我": "copy_current_page_link",
        "当前网页链接复制给我": "copy_current_page_link",
        "当前页面链接复制给我": "copy_current_page_link",
        "复制链接": "copy_current_page_link",
        "拷贝链接": "copy_current_page_link",
        "复制当前网页链接": "copy_current_page_link",
        "复制当前页面链接": "copy_current_page_link",
        "当前页地址复制": "copy_current_page_link",
        "copy": "copy",
        "copyselection": "copy",
        "copyselectedtext": "copy",
        "copycurrentpagelink": "copy_current_page_link",
        "copycurrenturl": "copy_current_page_link",
        "粘贴": "paste",
        "前台粘贴": "paste",
        "粘贴到当前窗口": "paste",
        "paste": "paste",
        "pasteintocurrentwindow": "paste",
        "粘贴到当前输入框": "paste",
        "把剪贴板内容粘贴到当前输入框": "paste",
        "pasteintocurrentinput": "paste",
        "pasteintocurrentfield": "paste",
        "隐藏其他应用": "hide_other_apps",
        "隐藏其它应用": "hide_other_apps",
        "隐藏其余应用": "hide_other_apps",
        "隐藏别的应用": "hide_other_apps",
        "hideotherapps": "hide_other_apps",
        "hideotherapplications": "hide_other_apps",
        "任务控制中心": "mission_control",
        "打开任务控制中心": "mission_control",
        "显示任务控制中心": "mission_control",
        "调出任务控制中心": "mission_control",
        "missioncontrol": "mission_control",
        "openmissioncontrol": "mission_control",
        "showmissioncontrol": "mission_control",
        "打开聚焦搜索": "spotlight_search",
        "显示聚焦搜索": "spotlight_search",
        "聚焦搜索": "spotlight_search",
        "spotlight": "spotlight_search",
        "spotlightsearch": "spotlight_search",
        "openspotlight": "spotlight_search",
        "showspotlight": "spotlight_search",
        "打开emoji面板": "emoji_picker",
        "显示emoji面板": "emoji_picker",
        "emoji面板": "emoji_picker",
        "emojipicker": "emoji_picker",
        "showemojipicker": "emoji_picker",
        "打开强制退出窗口": "force_quit_dialog",
        "显示强制退出窗口": "force_quit_dialog",
        "强制退出窗口": "force_quit_dialog",
        "forcequitapplications": "force_quit_dialog",
        "showforcequitapplications": "force_quit_dialog",
        "锁屏": "lock_screen",
        "锁一下屏": "lock_screen",
        "锁下屏": "lock_screen",
        "锁定屏幕": "lock_screen",
        "lockscreen": "lock_screen",
        "全选": "select_all",
        "selectall": "select_all",
        "撤销": "undo",
        "undo": "undo",
        "重做": "redo",
        "redo": "redo",
        "查找": "find",
        "打开查找": "find",
        "搜索": "find",
        "打开搜索": "find",
        "find": "find",
        "刷新": "refresh",
        "浏览器刷新": "refresh",
        "网页刷新": "refresh",
        "当前网页刷新": "refresh",
        "当前页刷新": "refresh",
        "刷新当前页面": "refresh",
        "刷新当前页": "refresh",
        "刷新当前网页": "refresh",
        "刷新这个页面": "refresh",
        "刷新这个网页": "refresh",
        "刷新页面": "refresh",
        "refresh": "refresh",
        "refreshpage": "refresh",
        "refreshthecurrentpage": "refresh",
        "reload": "refresh",
        "reloadpage": "refresh",
        "reloadthecurrentpage": "refresh",
        "新建标签": "new_tab",
        "新建标签页": "new_tab",
        "新标签页": "new_tab",
        "打开新标签页": "new_tab",
        "开新标签页": "new_tab",
        "新开标签页": "new_tab",
        "开一个新标签页": "new_tab",
        "新开一个标签页": "new_tab",
        "newtab": "new_tab",
        "opennewtab": "new_tab",
        "openanewtab": "new_tab",
        "新建窗口": "new_window",
        "打开新窗口": "new_window",
        "打开一个新窗口": "new_window",
        "新建浏览器窗口": "new_window",
        "newwindow": "new_window",
        "opennewwindow": "new_window",
        "新建笔记": "new_note",
        "新建一个笔记": "new_note",
        "新建一条笔记": "new_note",
        "新建一篇笔记": "new_note",
        "新笔记": "new_note",
        "创建笔记": "new_note",
        "创建一个笔记": "new_note",
        "新建备忘录": "new_note",
        "新建一个备忘录": "new_note",
        "新建一条备忘录": "new_note",
        "新建一篇备忘录": "new_note",
        "新备忘录": "new_note",
        "创建备忘录": "new_note",
        "创建一个备忘录": "new_note",
        "新建提醒事项": "new_reminder",
        "新建一个提醒事项": "new_reminder",
        "新建一条提醒事项": "new_reminder",
        "新建一项提醒事项": "new_reminder",
        "新建提醒": "new_reminder",
        "新提醒": "new_reminder",
        "创建提醒事项": "new_reminder",
        "创建一个提醒事项": "new_reminder",
        "创建提醒": "new_reminder",
        "创建一个提醒": "new_reminder",
        "新建日程": "new_event",
        "新建一个日程": "new_event",
        "新建一条日程": "new_event",
        "新建日历事件": "new_event",
        "新建一个日历事件": "new_event",
        "新建事件": "new_event",
        "新建一个事件": "new_event",
        "新建会议": "new_event",
        "新会议": "new_event",
        "创建日程": "new_event",
        "创建一个日程": "new_event",
        "创建事件": "new_event",
        "创建一个事件": "new_event",
        "newnote": "new_note",
        "makeanewnote": "new_note",
        "createanewnote": "new_note",
        "makenewnote": "new_note",
        "createnewnote": "new_note",
        "newreminder": "new_reminder",
        "makeanewreminder": "new_reminder",
        "createanewreminder": "new_reminder",
        "makenewreminder": "new_reminder",
        "createnewreminder": "new_reminder",
        "newevent": "new_event",
        "newmeeting": "new_event",
        "newcalendarevent": "new_event",
        "makeanewevent": "new_event",
        "createanewevent": "new_event",
        "makenewevent": "new_event",
        "createnewevent": "new_event",
        "makeanewmeeting": "new_event",
        "createanewmeeting": "new_event",
        "新建消息": "new_message",
        "新消息": "new_message",
        "创建消息": "new_message",
        "创建一条消息": "new_message",
        "写消息": "new_message",
        "写新消息": "new_message",
        "撰写消息": "new_message",
        "新建聊天": "new_message",
        "新聊天": "new_message",
        "创建聊天": "new_message",
        "新建会话": "new_message",
        "新会话": "new_message",
        "新建邮件": "new_message",
        "新邮件": "new_message",
        "创建邮件": "new_message",
        "创建一封邮件": "new_message",
        "写邮件": "new_message",
        "写新邮件": "new_message",
        "撰写邮件": "new_message",
        "撰写新邮件": "new_message",
        "发邮件": "new_message",
        "发送邮件": "new_message",
        "composemessage": "new_message",
        "newmessage": "new_message",
        "newchat": "new_message",
        "newconversation": "new_message",
        "startconversation": "new_message",
        "composeemail": "new_message",
        "composemail": "new_message",
        "newemail": "new_message",
        "newmail": "new_message",
        "createemail": "new_message",
        "createmail": "new_message",
        "writeemail": "new_message",
        "writemail": "new_message",
        "新建文档": "new_document",
        "新建一个文档": "new_document",
        "新建一份文档": "new_document",
        "新文档": "new_document",
        "新建页面": "new_document",
        "新建一个页面": "new_document",
        "创建页面": "new_document",
        "创建一个页面": "new_document",
        "新增页面": "new_document",
        "新页面": "new_document",
        "新建文件": "new_document",
        "新建一个文件": "new_document",
        "新建一份文件": "new_document",
        "新文件": "new_document",
        "新建图片": "new_document",
        "新建一个图片": "new_document",
        "新建一张图片": "new_document",
        "创建图片": "new_document",
        "创建一个图片": "new_document",
        "创建一张图片": "new_document",
        "新图片": "new_document",
        "新建图像": "new_document",
        "新建一个图像": "new_document",
        "新建一张图像": "new_document",
        "创建图像": "new_document",
        "创建一个图像": "new_document",
        "创建一张图像": "new_document",
        "新图像": "new_document",
        "新建画布": "new_document",
        "新建一个画布": "new_document",
        "创建画布": "new_document",
        "创建一个画布": "new_document",
        "新画布": "new_document",
        "新建表格": "new_document",
        "新建一个表格": "new_document",
        "新建一份表格": "new_document",
        "新表格": "new_document",
        "新建工作簿": "new_document",
        "新建一个工作簿": "new_document",
        "新工作簿": "new_document",
        "新建演示": "new_document",
        "新建一个演示": "new_document",
        "新建演示文稿": "new_document",
        "新建一个演示文稿": "new_document",
        "新建一份演示文稿": "new_document",
        "新演示文稿": "new_document",
        "新建幻灯片": "new_document",
        "新建一个幻灯片": "new_document",
        "新幻灯片": "new_document",
        "新建ppt": "new_document",
        "新ppt": "new_document",
        "新建项目": "new_document",
        "新建一个项目": "new_document",
        "创建项目": "new_document",
        "创建一个项目": "new_document",
        "新项目": "new_document",
        "新建工单": "new_task",
        "创建工单": "new_task",
        "新建任务": "new_task",
        "创建任务": "new_task",
        "新建卡片": "new_task",
        "创建卡片": "new_task",
        "新建ticket": "new_task",
        "创建ticket": "new_task",
        "创建一个ticket": "new_task",
        "新建issue": "new_task",
        "创建issue": "new_task",
        "创建一个issue": "new_task",
        "新建bug": "new_task",
        "创建bug": "new_task",
        "创建一个bug": "new_task",
        "新建bugticket": "new_task",
        "创建bugticket": "new_task",
        "创建一个bugticket": "new_task",
        "新建工作区": "new_document",
        "新建一个工作区": "new_document",
        "创建工作区": "new_document",
        "创建一个工作区": "new_document",
        "新建workspace": "new_document",
        "创建workspace": "new_document",
        "创建新workspace": "new_document",
        "新workspace": "new_document",
        "newdocument": "new_document",
        "newfile": "new_document",
        "newimage": "new_document",
        "newpicture": "new_document",
        "newcanvas": "new_document",
        "newworkbook": "new_document",
        "newspreadsheet": "new_document",
        "newpresentation": "new_document",
        "newslide": "new_document",
        "newproject": "new_document",
        "newticket": "new_task",
        "newissue": "new_task",
        "newtask": "new_task",
        "newcard": "new_task",
        "newbug": "new_task",
        "newbugticket": "new_task",
        "newworkspace": "new_document",
        "makeanewdocument": "new_document",
        "createanewdocument": "new_document",
        "makenewdocument": "new_document",
        "createnewdocument": "new_document",
        "makeanewfile": "new_document",
        "createanewfile": "new_document",
        "makenewfile": "new_document",
        "createnewfile": "new_document",
        "makeanewimage": "new_document",
        "createanewimage": "new_document",
        "makenewimage": "new_document",
        "createnewimage": "new_document",
        "makeanewpicture": "new_document",
        "createanewpicture": "new_document",
        "makenewpicture": "new_document",
        "createnewpicture": "new_document",
        "makeanewcanvas": "new_document",
        "createanewcanvas": "new_document",
        "makenewcanvas": "new_document",
        "createnewcanvas": "new_document",
        "makeanewworkbook": "new_document",
        "createanewworkbook": "new_document",
        "makenewworkbook": "new_document",
        "createnewworkbook": "new_document",
        "makeanewspreadsheet": "new_document",
        "createanewspreadsheet": "new_document",
        "makenewspreadsheet": "new_document",
        "createnewspreadsheet": "new_document",
        "makeanewpresentation": "new_document",
        "createanewpresentation": "new_document",
        "makenewpresentation": "new_document",
        "createnewpresentation": "new_document",
        "makeanewproject": "new_document",
        "createanewproject": "new_document",
        "makenewproject": "new_document",
        "createnewproject": "new_document",
        "createanewticket": "new_task",
        "makenewticket": "new_task",
        "createnewticket": "new_task",
        "createanewissue": "new_task",
        "makenewissue": "new_task",
        "createnewissue": "new_task",
        "createanewtask": "new_task",
        "makenewtask": "new_task",
        "createnewtask": "new_task",
        "createanewcard": "new_task",
        "makenewcard": "new_task",
        "createnewcard": "new_task",
        "createanewbug": "new_task",
        "makenewbug": "new_task",
        "createnewbug": "new_task",
        "createanewbugticket": "new_task",
        "makenewbugticket": "new_task",
        "createnewbugticket": "new_task",
        "makeanewworkspace": "new_document",
        "createanewworkspace": "new_document",
        "makenewworkspace": "new_document",
        "createnewworkspace": "new_document",
        "新建无痕窗口": "new_private_window",
        "打开无痕窗口": "new_private_window",
        "新建隐身窗口": "new_private_window",
        "打开隐身窗口": "new_private_window",
        "新建私密窗口": "new_private_window",
        "打开私密窗口": "new_private_window",
        "newprivatewindow": "new_private_window",
        "openprivatewindow": "new_private_window",
        "newincognitowindow": "new_private_window",
        "openincognitowindow": "new_private_window",
        "incognitowindow": "new_private_window",
        "关闭标签页": "close_tab",
        "关闭当前标签页": "close_tab",
        "关闭当前网页": "close_tab",
        "关闭这个网页": "close_tab",
        "把当前网页关掉": "close_tab",
        "把这个网页关掉": "close_tab",
        "closetab": "close_tab",
        "closethistab": "close_tab",
        "closecurrenttab": "close_tab",
        "closethecurrenttab": "close_tab",
        "closethispage": "close_tab",
        "下一个标签": "next_tab",
        "下一个标签页": "next_tab",
        "切到下一个标签页": "next_tab",
        "切换到下一个标签页": "next_tab",
        "nexttab": "next_tab",
        "switchtonexttab": "next_tab",
        "下一个窗口": "next_window",
        "切到下一个窗口": "next_window",
        "切换到下一个窗口": "next_window",
        "nextwindow": "next_window",
        "switchtonextwindow": "next_window",
        "下一个应用": "switch_next_app",
        "切到下一个应用": "switch_next_app",
        "切换到下一个应用": "switch_next_app",
        "nextapp": "switch_next_app",
        "switchtonextapp": "switch_next_app",
        "上一个标签": "previous_tab",
        "上一个标签页": "previous_tab",
        "切到上一个标签页": "previous_tab",
        "切换到上一个标签页": "previous_tab",
        "previoustab": "previous_tab",
        "switchtoprevioustab": "previous_tab",
        "上一个窗口": "previous_window",
        "切到上一个窗口": "previous_window",
        "切换到上一个窗口": "previous_window",
        "previouswindow": "previous_window",
        "switchtopreviouswindow": "previous_window",
        "上一个应用": "switch_previous_app",
        "切到上一个应用": "switch_previous_app",
        "切换到上一个应用": "switch_previous_app",
        "previousapp": "switch_previous_app",
        "switchtopreviousapp": "switch_previous_app",
        "重新打开关闭的标签页": "reopen_closed_tab",
        "重新打开刚才关闭的标签页": "reopen_closed_tab",
        "重新打开刚关闭的标签页": "reopen_closed_tab",
        "reopenclosedtab": "reopen_closed_tab",
        "reopenlastclosedtab": "reopen_closed_tab",
        "前进下一页": "browser_forward",
        "前进": "browser_forward",
        "forwardpage": "browser_forward",
        "goforward": "browser_forward",
        "返回上一页": "browser_back",
        "后退上一页": "browser_back",
        "后退": "browser_back",
        "goback": "browser_back",
        "gobackonepage": "browser_back",
        "backpage": "browser_back",
        "加入书签": "bookmark_page",
        "添加书签": "bookmark_page",
        "收藏当前网页": "bookmark_page",
        "把当前网页加入书签": "bookmark_page",
        "bookmarkthispage": "bookmark_page",
        "bookmarkcurrentpage": "bookmark_page",
        "打开历史记录": "show_history",
        "显示历史记录": "show_history",
        "浏览器历史记录": "show_history",
        "打开浏览器历史记录": "show_history",
        "browserhistory": "show_history",
        "browsinghistory": "show_history",
        "showhistory": "show_history",
        "showbrowserhistory": "show_history",
        "showbrowsinghistory": "show_history",
        "openhistory": "show_history",
        "openbrowserhistory": "show_history",
        "openbrowsinghistory": "show_history",
        "打开开发者工具": "open_devtools",
        "显示开发者工具": "open_devtools",
        "开发者工具": "open_devtools",
        "打开当前网页开发者工具": "open_devtools",
        "打开当前网页的开发者工具": "open_devtools",
        "opendevtools": "open_devtools",
        "showdevtools": "open_devtools",
        "网页放大": "zoom_in",
        "页面放大": "zoom_in",
        "zoominpage": "zoom_in",
        "zoomin": "zoom_in",
        "网页缩小": "zoom_out",
        "页面缩小": "zoom_out",
        "zoomoutpage": "zoom_out",
        "zoomout": "zoom_out",
        "实际大小": "reset_zoom",
        "恢复实际大小": "reset_zoom",
        "resetzoom": "reset_zoom",
        "显示应用窗口": "application_windows",
        "显示当前应用窗口": "application_windows",
        "显示当前应用所有窗口": "application_windows",
        "显示当前应用的所有窗口": "application_windows",
        "显示前台应用窗口": "application_windows",
        "显示前台应用所有窗口": "application_windows",
        "应用窗口": "application_windows",
        "应用窗口都显示": "application_windows",
        "showappwindows": "application_windows",
        "showapplicationwindows": "application_windows",
        "applicationwindows": "application_windows",
        "最大化": "toggle_full_screen",
        "窗口最大化": "toggle_full_screen",
        "当前窗口最大化": "toggle_full_screen",
        "全屏": "toggle_full_screen",
        "窗口全屏": "toggle_full_screen",
        "当前窗口全屏": "toggle_full_screen",
        "进入全屏": "toggle_full_screen",
        "进入全屏模式": "toggle_full_screen",
        "maximize": "toggle_full_screen",
        "maximizewindow": "toggle_full_screen",
        "maximizecurrentwindow": "toggle_full_screen",
        "maximizethecurrentwindow": "toggle_full_screen",
        "fullscreen": "toggle_full_screen",
        "fullscreencurrentwindow": "toggle_full_screen",
        "fullscreenthecurrentwindow": "toggle_full_screen",
        "enterfullscreen": "toggle_full_screen",
        "聚焦地址栏": "focus_address_bar",
        "打开地址栏": "focus_address_bar",
        "选中地址栏": "focus_address_bar",
        "focusaddressbar": "focus_address_bar",
        "focusurlbar": "focus_address_bar",
        "addressbar": "focus_address_bar",
    }
    action = mapping.get(normalized, "")
    if action:
        return action
    return _finder_safe_shortcut_action(normalized, mode="exact")


def _strip_foreground_shortcut_scope(normalized: str) -> str:
    value = str(normalized or "").strip()
    foreground_prefixes = (
        "当前标签页",
        "当前浏览器",
        "当前网页",
        "当前页面",
        "当前页",
        "这个标签页",
        "这个浏览器",
        "这个网页",
        "这个页面",
        "该标签页",
        "该浏览器",
        "该网页",
        "该页面",
        "标签页",
        "浏览器",
        "网页",
        "页面",
        "在当前窗口",
        "在当前应用",
        "在当前app",
        "在前台窗口",
        "在前台应用",
        "在前台app",
        "当前窗口",
        "当前应用",
        "当前app",
        "前台窗口",
        "前台应用",
        "前台app",
        "当前界面",
        "当前屏幕",
        "inthecurrentwindow",
        "inthecurrentapp",
        "inthecurrentapplication",
        "incurrentwindow",
        "incurrentapp",
        "incurrentapplication",
        "currentwindow",
        "currentapp",
        "currentapplication",
        "foregroundwindow",
        "foregroundapp",
        "foregroundapplication",
        "currenttab",
        "currentbrowser",
        "currentpage",
        "thistab",
        "thisbrowser",
        "thispage",
        "browser",
        "tab",
        "page",
    )
    foreground_suffixes = (
        "当前标签页",
        "当前浏览器",
        "当前网页",
        "当前页面",
        "当前页",
        "这个标签页",
        "这个浏览器",
        "这个网页",
        "这个页面",
        "该标签页",
        "该浏览器",
        "该网页",
        "该页面",
        "标签页",
        "浏览器",
        "网页",
        "页面",
        "当前窗口",
        "当前应用",
        "当前app",
        "前台窗口",
        "前台应用",
        "前台app",
        "当前界面",
        "当前屏幕",
        "inthecurrentwindow",
        "inthecurrentapp",
        "inthecurrentapplication",
        "incurrentwindow",
        "incurrentapp",
        "incurrentapplication",
        "currentwindow",
        "currentapp",
        "currentapplication",
        "foregroundwindow",
        "foregroundapp",
        "foregroundapplication",
        "currenttab",
        "currentbrowser",
        "currentpage",
        "thistab",
        "thisbrowser",
        "thispage",
        "browser",
        "tab",
        "page",
    )
    for prefix in foreground_prefixes:
        if value.startswith(prefix):
            return value[len(prefix) :]
    for suffix in foreground_suffixes:
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def _screenshot_safe_shortcut_action(normalized: str) -> str:
    if normalized in {
        "选区截图",
        "截图选区",
        "截取选区",
        "区域截图",
        "选择区域截图",
        "选取区域截图",
        "框选截图",
        "screenshotselection",
        "screenshotselectedarea",
        "selectedareascreenshot",
        "regionscreenshot",
        "captureselectedarea",
        "capturearegion",
        "capturearea",
    }:
        return "screenshot_selection"
    if normalized in {
        "截图工具",
        "打开截图工具",
        "显示截图工具",
        "启动截图工具",
        "截图面板",
        "打开截图面板",
        "显示截图面板",
        "启动截图面板",
        "屏幕截图工具",
        "打开屏幕截图工具",
        "屏幕截图面板",
        "打开屏幕截图面板",
        "录屏",
        "屏幕录制",
        "录屏工具",
        "打开录屏工具",
        "录屏面板",
        "打开录屏面板",
        "开始录屏",
        "screenshottoolbar",
        "openscreenshottoolbar",
        "showscreenshottoolbar",
        "launchscreenshottoolbar",
        "screenshottool",
        "openscreenshottool",
        "screenshotpanel",
        "openscreenshotpanel",
        "screencapturetoolbar",
        "openscreencapturetoolbar",
        "screencapturetool",
        "openscreencapturetool",
        "screencapturepanel",
        "openscreencapturepanel",
        "screenrecording",
        "screenrecordingtoolbar",
        "openscreenrecordingtoolbar",
        "screenrecordingtool",
        "openscreenrecordingtool",
        "screenrecordingpanel",
        "openscreenrecordingpanel",
    }:
        return "screenshot_toolbar"
    return ""


def _safe_shortcut_action_from_trailing_phrase(value: str) -> str:
    normalized = re.sub(r"[\s._·-]+", "", clean(value).lower())
    if not normalized:
        return ""
    screenshot_action = _screenshot_safe_shortcut_action(normalized)
    if screenshot_action:
        return screenshot_action
    if contains_any(normalized, ["音量", "声音", "亮度", "volume", "sound", "brightness"]):
        return ""
    browser_suffix_actions = (
        (
            "new_private_window",
            (
                "新建无痕窗口",
                "打开无痕窗口",
                "新建隐身窗口",
                "打开隐身窗口",
                "新建私密窗口",
                "打开私密窗口",
                "newprivatewindow",
                "openprivatewindow",
                "newincognitowindow",
                "openincognitowindow",
            ),
        ),
        (
            "focus_address_bar",
            (
                "聚焦地址栏",
                "打开地址栏",
                "选中地址栏",
                "focusaddressbar",
                "focusurlbar",
            ),
        ),
        (
            "show_history",
            (
                "打开历史记录",
                "显示历史记录",
                "打开浏览器历史记录",
                "showbrowsinghistory",
                "openbrowsinghistory",
            ),
        ),
        (
            "open_devtools",
            ("打开开发者工具", "显示开发者工具", "opendevtools", "showdevtools"),
        ),
        ("refresh", ("刷新", "刷新页面", "刷新当前网页", "refresh", "refreshpage")),
        ("browser_forward", ("前进下一页", "前进", "forwardpage", "goforward")),
        ("browser_back", ("返回上一页", "后退上一页", "后退", "goback", "backpage")),
        ("bookmark_page", ("加入书签", "把当前网页加入书签", "bookmarkthispage")),
        ("zoom_in", ("网页放大", "页面放大", "zoominpage", "zoomin")),
        ("zoom_out", ("网页缩小", "页面缩小", "zoomoutpage", "zoomout")),
        ("reset_zoom", ("实际大小", "恢复实际大小", "resetzoom")),
    )
    for action, suffixes in browser_suffix_actions:
        if any(normalized.endswith(suffix) for suffix in suffixes):
            return action
    full_screen_suffixes = (
        "窗口最大化",
        "当前窗口最大化",
        "最大化",
        "窗口全屏",
        "当前窗口全屏",
        "进入全屏模式",
        "进入全屏",
        "全屏",
        "maximizewindow",
        "maximize",
        "fullscreencurrentwindow",
        "fullscreenwindow",
        "fullscreen",
        "enterfullscreen",
    )
    if any(normalized.endswith(suffix) for suffix in full_screen_suffixes):
        return "toggle_full_screen"
    new_task_suffixes = (
        "新建工单",
        "创建工单",
        "新建任务",
        "创建任务",
        "新建卡片",
        "创建卡片",
        "新建ticket",
        "创建ticket",
        "创建一个ticket",
        "新建issue",
        "创建issue",
        "创建一个issue",
        "新建bug",
        "创建bug",
        "创建一个bug",
        "新建bugticket",
        "创建bugticket",
        "创建一个bugticket",
        "newticket",
        "newissue",
        "newtask",
        "newcard",
        "newbug",
        "newbugticket",
        "makeanewticket",
        "createanewticket",
        "makeanewissue",
        "createanewissue",
        "makeanewtask",
        "createanewtask",
        "makeanewcard",
        "createanewcard",
        "makeanewbug",
        "createanewbug",
        "makeanewbugticket",
        "createanewbugticket",
    )
    if any(normalized.endswith(suffix) for suffix in new_task_suffixes):
        return "new_task"
    new_document_suffixes = (
        "新建文档",
        "新建一个文档",
        "新建一份文档",
        "新建页面",
        "新建一个页面",
        "创建页面",
        "创建一个页面",
        "新增页面",
        "新建文件",
        "新建一个文件",
        "新建一份文件",
        "新建图片",
        "新建一个图片",
        "新建一张图片",
        "创建图片",
        "创建一个图片",
        "创建一张图片",
        "新建图像",
        "新建一个图像",
        "新建一张图像",
        "创建图像",
        "创建一个图像",
        "创建一张图像",
        "新建画布",
        "新建一个画布",
        "创建画布",
        "创建一个画布",
        "新建表格",
        "新建一个表格",
        "新建一份表格",
        "新建工作簿",
        "新建一个工作簿",
        "新建演示",
        "新建一个演示",
        "新建演示文稿",
        "新建一个演示文稿",
        "新建一份演示文稿",
        "新建幻灯片",
        "新建一个幻灯片",
        "新建ppt",
        "新建项目",
        "新建一个项目",
        "创建项目",
        "创建一个项目",
        "新建工单",
        "创建工单",
        "新建任务",
        "创建任务",
        "新建卡片",
        "创建卡片",
        "新建ticket",
        "创建ticket",
        "创建一个ticket",
        "新建issue",
        "创建issue",
        "创建一个issue",
        "新建bug",
        "创建bug",
        "创建一个bug",
        "新建bugticket",
        "创建bugticket",
        "创建一个bugticket",
        "新建工作区",
        "新建一个工作区",
        "创建工作区",
        "创建一个工作区",
        "新建workspace",
        "创建workspace",
        "创建新workspace",
        "新workspace",
        "newdocument",
        "newfile",
        "newimage",
        "newpicture",
        "newcanvas",
        "newworkbook",
        "newspreadsheet",
        "newpresentation",
        "newslide",
        "newproject",
        "newticket",
        "newissue",
        "newtask",
        "newcard",
        "newbug",
        "newbugticket",
        "newworkspace",
        "makeanewdocument",
        "createanewdocument",
        "makeanewfile",
        "createanewfile",
        "makeanewimage",
        "createanewimage",
        "makeanewpicture",
        "createanewpicture",
        "makeanewcanvas",
        "createanewcanvas",
        "makeanewworkbook",
        "createanewworkbook",
        "makeanewspreadsheet",
        "createanewspreadsheet",
        "makeanewpresentation",
        "createanewpresentation",
        "makeanewproject",
        "createanewproject",
        "makeanewticket",
        "createanewticket",
        "makeanewissue",
        "createanewissue",
        "makeanewtask",
        "createanewtask",
        "makeanewcard",
        "createanewcard",
        "makeanewbug",
        "createanewbug",
        "makeanewbugticket",
        "createanewbugticket",
        "makeanewworkspace",
        "createanewworkspace",
    )
    if any(normalized.endswith(suffix) for suffix in new_document_suffixes):
        return "new_document"
    finder_action = _finder_safe_shortcut_action(normalized, mode="suffix")
    if finder_action:
        return finder_action
    return ""


def _safe_shortcut_action_from_embedded_create_phrase(value: str) -> str:
    text = clean(value)
    lowered = text.lower()
    if not re.search(
        r"(?:打开|启动|开启|切到|聚焦|写下|写入|记录下|记下|在|用|通过|"
        r"\b(?:open|launch|start|focus|switch|type|enter|write|create|make|record|file)\b)",
        text,
        flags=re.IGNORECASE,
    ):
        return ""
    if re.search(
        r"(?:新建|创建|新增)\s*(?:一个|一条|一篇|一份|一则)?\s*"
        r"(?:今天的|今日的|新的|新|关于.+?的)?\s*"
        r"(?:(?:标题|名称|名字|题目)\s*(?:是|为|叫|:|：)\s*[^。！？!?，,]{1,80}?\s*的\s*)?"
        r"(?:笔记|备忘录|日志|日记)"
        r"(?=$|[。！？!?，,:：]|(?:\s*(?:并|然后|再|接着|之后|后|"
        r"标题|名称|名字|题目|内容|正文|写下|写入|记录下|记下|写)))",
        text,
        flags=re.IGNORECASE,
    ) or re.search(
        r"\b(?:make|create|open)?\s*(?:a\s+)?new\s+"
        r"(?:note|journal|diary)\b",
        lowered,
        flags=re.IGNORECASE,
    ):
        return "new_note"
    if re.search(
        r"(?:新建|创建|新增|记录成|记成|登记成|转成|作为)\s*(?:一个|一份|一篇|一条|一张|一幅)?\s*"
        r"(?:(?:标题|名称|名字|题目)\s*(?:是|为|叫|:|：)\s*[^。！？!?，,]{1,80}?\s*的\s*)?"
        r"(?:工单|任务|卡片|ticket|issue|bug|bug\s*ticket)"
        r"(?=$|[。！？!?，,:：]|(?:\s*(?:并|然后|再|接着|之后|后|"
        r"标题|名称|名字|题目|内容|正文|写下|写入|记录下|记下|写)))",
        text,
        flags=re.IGNORECASE,
    ) or re.search(
        r"\b(?:make|create|open)?\s*(?:a\s+)?new\s+"
        r"(?:ticket|issue|task|card|bug|bug\s*ticket)\b",
        lowered,
        flags=re.IGNORECASE,
    ) or re.search(
        r"\b(?:create|make|open|record|file)\s+(?:a|an|the)?\s*"
        r"(?:ticket|issue|task|card|bug|bug\s*ticket)\b",
        lowered,
        flags=re.IGNORECASE,
    ):
        return "new_task"
    if re.search(
        r"(?:新建|创建|新增)\s*(?:一个|一份|一篇|一条|一张|一幅)?\s*"
        r"(?:\d{2,5}\s*(?:x|×|X|\*)\s*\d{2,5}\s*)?"
        r"(?:(?:标题|名称|名字|题目)\s*(?:是|为|叫|:|：)\s*[^。！？!?，,]{1,80}?\s*的\s*)?"
        r"(?:页面|文档|文件(?!夹)|图片|图像|画布|表格|工作簿|演示|演示文稿|幻灯片|项目)"
        r"(?=$|[。！？!?，,:：]|(?:\s*(?:并|然后|再|接着|之后|后|"
        r"标题|名称|名字|题目|内容|正文|写下|写入|记录下|记下|写)))",
        text,
        flags=re.IGNORECASE,
    ) or re.search(
        r"\b(?:make|create|open)?\s*(?:a\s+)?new\s+"
        r"(?:document|file|image|picture|canvas|spreadsheet|workbook|presentation|slide|project|ticket|issue|task|card)\b",
        lowered,
        flags=re.IGNORECASE,
    ):
        return "new_document"
    return ""


def _finder_safe_shortcut_action(normalized: str, *, mode: str) -> str:
    for action, phrase in _FINDER_SAFE_SHORTCUT_PHRASES:
        clean_phrase = re.sub(r"[\s._·-]+", "", phrase.lower())
        if mode == "exact" and normalized == clean_phrase:
            return action
        if mode == "suffix" and normalized.endswith(clean_phrase):
            return action
    return ""


def _compound_safe_shortcut_actions(value: str) -> list[str]:
    normalized = re.sub(r"[\s._·-]+", "", clean(value).lower())
    if not normalized:
        return []
    if "全选" in normalized and "复制" in normalized and normalized.index("全选") < normalized.index("复制"):
        return ["select_all", "copy"]
    if (
        "selectall" in normalized
        and "copy" in normalized
        and normalized.index("selectall") < normalized.index("copy")
    ):
        return ["select_all", "copy"]
    return []


def _looks_like_show_desktop_request(value: str, lowered: str) -> bool:
    return bool(
        re.search(
            r"^(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:显示|露出|查看|看看|看一下|切到|切换到|回到|返回到|回)\s*"
            r"(?:当前|现在)?(?:桌面|desktop)"
            r"(?:一下|下)?(?:可以吗|好吗|好么|行吗|吗|嘛|吧|呢)?$",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(r"^(?:show|reveal|switch\s+to|go\s+to)\s+(?:the\s+)?desktop\s*(?:please)?$", lowered)
    )


def _looks_like_next_focus_request(value: str, lowered: str) -> bool:
    return bool(
        re.search(
            r"^(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:切到|切换到|跳到|跳转到|移到|移动到|聚焦到|聚焦|焦点到)?\s*"
            r"(?:下一个|下一项|下个|next)\s*"
            r"(?:输入框|文本框|输入栏|字段|控件|元素|项目)?"
            r"(?:一下|下)?(?:可以吗|好吗|好么|行吗|吗|嘛|吧|呢)?$",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(r"^(?:focus|move|go|jump|tab)\s+(?:to\s+)?(?:the\s+)?next\s+(?:field|input|control|element)\s*$", lowered)
    )


def _looks_like_previous_focus_request(value: str, lowered: str) -> bool:
    return bool(
        re.search(
            r"^(?:帮我|请|麻烦|能否|能不能|可以)?(?:直接)?"
            r"(?:切到|切换到|跳到|跳转到|移到|移动到|聚焦到|聚焦|焦点到)?\s*"
            r"(?:上一个|上一项|上个|previous|prev)\s*"
            r"(?:输入框|文本框|输入栏|字段|控件|元素|项目)?"
            r"(?:一下|下)?(?:可以吗|好吗|好么|行吗|吗|嘛|吧|呢)?$",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(r"^(?:focus|move|go|jump)\s+(?:to\s+)?(?:the\s+)?(?:previous|prev)\s+(?:field|input|control|element)\s*$", lowered)
    )


def _safe_key_action(value: str) -> str:
    compact = re.sub(r"[\s._-]+", "", str(value or "").strip().lower())
    return {
        "esc": "escape",
        "escape": "escape",
        "退出": "escape",
        "取消": "escape",
        "tab": "tab",
        "制表": "tab",
        "制表键": "tab",
        "up": "arrow_up",
        "uparrow": "arrow_up",
        "arrowup": "arrow_up",
        "上箭头": "arrow_up",
        "上方向键": "arrow_up",
        "向上箭头": "arrow_up",
        "down": "arrow_down",
        "downarrow": "arrow_down",
        "arrowdown": "arrow_down",
        "下箭头": "arrow_down",
        "下方向键": "arrow_down",
        "向下箭头": "arrow_down",
        "left": "arrow_left",
        "leftarrow": "arrow_left",
        "arrowleft": "arrow_left",
        "左箭头": "arrow_left",
        "左方向键": "arrow_left",
        "向左箭头": "arrow_left",
        "right": "arrow_right",
        "rightarrow": "arrow_right",
        "arrowright": "arrow_right",
        "右箭头": "arrow_right",
        "右方向键": "arrow_right",
        "向右箭头": "arrow_right",
        "home": "home",
        "home键": "home",
        "end": "end",
        "end键": "end",
        "pageup": "page_up",
        "上一页键": "page_up",
        "上一页": "page_up",
        "pagedown": "page_down",
        "下一页键": "page_down",
        "下一页": "page_down",
    }.get(compact, "")


def _scroll_direction_is_up(value: str) -> bool:
    direction = str(value or "").strip().lower()
    return direction in {
        "向上",
        "往上",
        "朝上",
        "上",
        "上滑",
        "上滚",
        "上翻",
        "上一页",
        "页面顶部",
        "顶部",
        "顶端",
        "最上面",
        "最上方",
        "up",
        "top",
    }


def _bounded_count(value: str | None, *, default: int, maximum: int) -> int:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    if raw.isdigit():
        count = int(raw)
    else:
        count = {
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
        }.get(raw, 0)
    return count if 1 <= count <= maximum else 0


def _numeric_value(value: str) -> int | float:
    number = float(str(value or "0"))
    return int(number) if number.is_integer() else number


def _normalize_hotkey_token(value: str) -> str:
    return legacy_normalize_hotkey_token(value)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def contains_any(text: str, needles: list[str] | tuple[str, ...]) -> bool:
    lowered = str(text or "").lower()
    return any(str(needle).lower() in lowered for needle in needles)


_TARGET_PATH_PATTERN = (
    r"(?:~|/|\./|\../)?[^\s\"'“”‘’，,。；;]+"
    r"\.(?:pdf|md|markdown|txt|csv|tsv|xlsx|xls|json|jsonl|doc|docx|rtf|"
    r"pages|numbers|py|js|jsx|ts|tsx|java|go|rs|swift|kt|kts|c|cc|cpp|h|hpp|"
    r"png|jpg|jpeg|heic|gif|webp|ppt|pptx|key)"
)

_TARGET_PATH_PENDING_ACTION_PATTERN = (
    r"(?:裁剪|剪裁|压缩|打包|解压|筛选|过滤|排序|编辑|处理|转换|调整|标注|"
    r"保存|导出|另存为|重命名|合并|拆分|"
    r"crop|compress|zip|archive|unzip|filter|sort|edit|process|convert|"
    r"resize|annotate|save|export|rename|merge|split)"
)


def _target_path_pending_user_action(text: str) -> str:
    value = clean(text)
    if not value:
        return ""
    match = re.search(
        rf"{_TARGET_PATH_PATTERN}\s*"
        r"(?:(?:，|,|并且|并|然后|再|接着|之后|后|\band\b|\bthen\b)\s*)*"
        rf"(?P<action>{_TARGET_PATH_PENDING_ACTION_PATTERN}.*)$",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    return str(match.group("action") or "").strip(" .，,。")


def discovered_app_open_needs_model_followup(
    inputs: Mapping[str, Any],
    user_goal: str,
) -> bool:
    app_capability = inputs.get("app_capability_hint")
    if not isinstance(app_capability, Mapping) or not app_capability:
        return False
    if isinstance(inputs.get("safe_shortcut_hint"), Mapping):
        return False
    if isinstance(inputs.get("app_search_hint"), Mapping):
        return False
    if isinstance(inputs.get("communication_compose_hint"), Mapping):
        return False
    if str(inputs.get("foreground_compose_text_hint") or "").strip():
        return False
    text = clean(user_goal)
    if not text:
        return False
    if str(inputs.get("selected_app_target_path_hint") or "").strip():
        return bool(_target_path_pending_user_action(text))
    return bool(
        re.search(
            r"(?:应用(?:程序)?|app|软件|工具|程序|编辑器|阅读器|查看器|浏览器|客户端)"
            r".{0,24}?(?:，|,|并|然后|再|接着|之后|后|\band\b|\bthen\b)"
            r".{0,80}(?:画|绘制|创建|制作|生成|编辑|处理|保存|导出|写入|输入|填入|"
            r"标注|设计|点击|点一下|点按|单击|双击|按下|按一下|发送|触发|"
            r"滚动|滑动|翻页|下滑|上滑|下滚|上滚|draw|paint|create|make|"
            r"edit|process|save|export|write|type|fill|annotate|design|click|"
            r"tap|press|hit|send|trigger|scroll|page)",
            text,
            flags=re.IGNORECASE,
        )
    )


def _strip_pending_action_prefix(action: str) -> str:
    value = clean(action)
    if not value:
        return ""
    for _ in range(4):
        previous = value
        value = re.sub(r"^[\s，,。；;:：]+", "", value).strip()
        value = re.sub(r"^(?:并且|并|然后|再|接着|之后)\s*", "", value).strip()
        value = re.sub(
            r"^(?:and\s+then|then|and)\s+",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        value = re.sub(
            r"^(?:打开|启动|开启|运行|使用|用|open|launch|start|run|use)\s*"
            r"(?:它|这个|该|应用|app|application|tool|program)?\s*",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()
        if value == previous:
            break
    return value


def discovered_app_pending_user_action(user_goal: str) -> str:
    text = clean(user_goal)
    if not text:
        return ""
    target_path_action = _target_path_pending_user_action(text)
    if target_path_action:
        return _strip_pending_action_prefix(target_path_action)[:160]
    match = re.search(
        r"(?:应用(?:程序)?|app|软件|工具|程序|编辑器|阅读器|查看器|浏览器|客户端)"
        r"\s*(?P<direct_action>"
        r"(?:点击|点一下|点按|单击|双击|按下|按一下|按|发送|触发|"
        r"滚动|滑动|翻页|下滑|上滑|下滚|上滚|输入|写入|保存|导出|"
        r"click|tap|press|hit|send|trigger|scroll|page|type|save|export)"
        r".+)$",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"(?:应用(?:程序)?|app|软件|工具|程序|编辑器|阅读器|查看器|浏览器|客户端)"
            r".{0,24}?(?:，|,|并|然后|再|接着|之后|后|\band\b|\bthen\b)"
            r"(?P<action>.+)$",
            text,
            flags=re.IGNORECASE,
        )
    if not match:
        return _strip_pending_action_prefix(text)[:160]
    raw_action = (
        match.groupdict().get("action")
        or match.groupdict().get("direct_action")
        or ""
    )
    action = re.sub(
        r"\s+",
        " ",
        _strip_pending_action_prefix(str(raw_action)),
    )
    return action[:160]


def _allowed_tool_set(allowed_tools: Iterable[str] | None) -> set[str] | None:
    if allowed_tools is None:
        return None
    return {str(tool or "").strip() for tool in allowed_tools if str(tool or "").strip()}


_DESKTOP_HINT_TOOL_ALIASES = {
    "app.open": "desktop.open_app",
    "app.focus": "desktop.focus_app",
    "desktop.windows": "desktop.list_windows",
    "desktop.ui_elements": "desktop.read_ui",
    "desktop.hotkey": "desktop.shortcut",
    "desktop.type_text": "desktop.type",
}


def _first_allowed(tools: Iterable[str], allowed: set[str] | None) -> str | None:
    for tool in tools:
        if allowed is None or tool in allowed:
            return tool
        alias = _DESKTOP_HINT_TOOL_ALIASES.get(tool)
        if allowed is not None and alias in allowed:
            return alias
    return None
