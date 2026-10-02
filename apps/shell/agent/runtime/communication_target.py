"""Observe a named conversation in actual AX data, never public goal metadata."""

import re
from collections.abc import Mapping
from typing import Any

_HEADER = re.compile(
    r"(?:conversation|chat|message)\s*(?:header|title|recipient)|"
    r"(?:会话|聊天)(?:标题|对象|名称)|消息接收人",
    re.IGNORECASE,
)
_SEARCH = re.compile(r"search\s*results?|搜索结果|查找结果", re.IGNORECASE)


def conversation_recipient_matches(data: Mapping[str, Any], recipient: str) -> bool:
    """Require one exact conversation header outside search/list result rows.

    Callers separately verify the native provider and same positive pid/window.
    Absence of an explicit recipient imposes no invented conversation choice.
    """
    if not recipient:
        return True
    if not isinstance(data, Mapping) or data.get("truncated") is True:
        return False
    # A dedicated conversation window can expose the recipient as its title.
    title_matches = isinstance(data.get("title"), str) and data["title"] == recipient
    elements = data.get("elements")
    if not isinstance(elements, list):
        return title_matches
    stack: list[tuple[int, bool]] = []
    matched = 0
    for element in elements:
        if not isinstance(element, Mapping):
            continue
        depth = element.get("depth")
        if type(depth) is not int or depth < 0:
            continue
        while stack and stack[-1][0] >= depth:
            stack.pop()
        role = str(element.get("role") or "").casefold().removeprefix("ax")
        text = " ".join(
            value for key in ("name", "description", "value")
            if isinstance(value := element.get(key), str)
        )
        excluded = (
            any(flag for _level, flag in stack)
            or role in {"table", "row", "list", "outline"}
            or bool(_SEARCH.search(text))
        )
        stack.append((depth, excluded))
        if excluded or role not in {"heading", "statictext", "text"}:
            continue
        if role != "heading" and not _HEADER.search(str(element.get("description") or "")):
            continue
        value = element.get("value")
        name = element.get("name")
        candidate = value if isinstance(value, str) and value else name
        if candidate != recipient:
            return False
        matched += 1
    return matched == 1 or (matched == 0 and title_matches)

def is_message_composer(element: Mapping[str, Any]) -> bool:
    """A send draft must occupy an actual message/body slot, never search."""
    identity = " ".join(
        value for key in ("name", "label", "title", "identifier", "description")
        if isinstance(value := element.get(key), str)
    )
    if re.search(r"search|find|query|recipient|搜索|查找|收件人|接收人", identity, re.I):
        return False
    return bool(re.search(
        r"\b(?:message|composer|compose|body)\b|message[_-]?(?:input|editor|box)|"
        r"消息(?:输入|编辑)?(?:框|区域)?|聊天输入框|正文(?:输入框)?",
        identity, re.I,
    ))
