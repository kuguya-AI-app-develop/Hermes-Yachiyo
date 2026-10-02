"""Bind bounded image questions to actual user-supplied model input.

An image already attached to the request needs model interpretation, rather
than a fresh desktop observation. Bind that input before vision conversion;
neither model prose nor a caller-supplied GoalContract can invent the binding.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

SUPPLIED_IMAGE_EVENT = "agent.input.image.bound"
_SOURCE = "runtime_main_chat_supplied_image"
_MAX_IMAGES = 8
_MAX_IMAGE_BYTES = 20 * 1024 * 1024


def is_supplied_image_question(original_goal: str) -> bool:
    text = " ".join(str(original_goal or "").strip().split())
    if not text or len(text) > 120:
        return False
    return bool(
        re.fullmatch(
            r"(?:(?:请|帮我)\s*)?(?:看一下|看一眼|看看|查看|描述|说明|分析)"
            r"\s*(?:这张|附带的|所附的|提供的)(?:图片|照片|截图|图)"
            r"(?:的?(?:内容|含义)|里(?:是|有)什么)?[。？！!?]?|"
            r"(?:please\s+)?(?:describe|explain|analy[sz]e|look\s+at|review)\s+"
            r"(?:this|the\s+(?:attached|supplied|provided))\s+"
            r"(?:image|photo|picture|screenshot)[.!?]?",
            text,
            flags=re.IGNORECASE,
        )
    )


def supplied_image_binding_from_messages(
    *, run_id: str, original_goal: str, messages: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    if not run_id or not is_supplied_image_question(original_goal) or not messages:
        return None
    message = messages[-1]
    content = message.get("content")
    if message.get("role") != "user" or not isinstance(content, list):
        return None
    text_parts = [
        str(part.get("text") or "")
        for part in content
        if isinstance(part, Mapping) and part.get("type") == "text"
    ]
    # History, follow-up instructions and image captions cannot replace the
    # persisted root goal or introduce an unrelated image into its authority.
    if "\n".join(text_parts).strip() != original_goal:
        return None
    image_parts = [
        part for part in content if isinstance(part, Mapping) and part.get("type") == "image_url"
    ]
    if not 1 <= len(image_parts) <= _MAX_IMAGES:
        return None
    hashes: list[str] = []
    for part in image_parts:
        image = part.get("image_url")
        url = image.get("url") if isinstance(image, Mapping) else None
        if not isinstance(url, str) or len(url) > (_MAX_IMAGE_BYTES * 4 // 3 + 128):
            return None
        match = re.fullmatch(r"data:image/(?:png|jpe?g|gif|webp);base64,(.+)", url)
        if match is None:
            return None
        try:
            data = base64.b64decode(match.group(1), validate=True)
        except (ValueError, binascii.Error):
            return None
        if not data or len(data) > _MAX_IMAGE_BYTES:
            return None
        hashes.append(hashlib.sha256(data).hexdigest())
    payload = {
        "version": 1,
        "source": _SOURCE,
        "run_id": run_id,
        "original_goal": original_goal,
        "image_sha256": hashes,
    }
    return {**payload, "binding_id": _binding_id(payload)}


def persisted_supplied_image_binding(
    *, run_id: str, original_goal: str, timeline: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    if not is_supplied_image_question(original_goal):
        return None
    valid: dict[str, dict[str, Any]] = {}
    for outer_event in timeline:
        if (outer_event.get("event_type") or outer_event.get("event")) != SUPPLIED_IMAGE_EVENT:
            continue
        if outer_event.get("run_id") and outer_event.get("run_id") != run_id:
            continue
        nested = outer_event.get("payload")
        event = nested if isinstance(nested, Mapping) else outer_event
        hashes = event.get("image_sha256")
        if (
            type(event.get("version")) is not int
            or event.get("version") != 1
            or event.get("source") != _SOURCE
            or event.get("run_id") != run_id
            or event.get("original_goal") != original_goal
            or not isinstance(hashes, list)
            or not 1 <= len(hashes) <= _MAX_IMAGES
            or any(
                not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                for value in hashes
            )
        ):
            continue
        payload = {
            key: event[key]
            for key in ("version", "source", "run_id", "original_goal", "image_sha256")
        }
        binding_id = _binding_id(payload)
        if event.get("binding_id") != binding_id:
            continue
        valid[binding_id] = {**payload, "binding_id": binding_id}
    # A changed attachment set on a resumed Run cannot silently retarget its
    # original response criterion.
    return next(iter(valid.values())) if len(valid) == 1 else None


def _binding_id(payload: Mapping[str, Any]) -> str:
    data = json.dumps(
        dict(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return "supplied-image-" + hashlib.sha256(data).hexdigest()
