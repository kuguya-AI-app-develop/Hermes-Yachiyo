"""Structured Browser/CDP tools for the Agent runtime."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen


_OWNED_BROWSER_TARGET_ID: ContextVar[str] = ContextVar(
    "oha_yachiyo_owned_browser_target_id",
    default="",
)
_BROWSER_TARGET_ID_PATTERN = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")


def is_valid_target_id(value: Any) -> bool:
    """Return whether a CDP target id is safe to bind into a run context."""

    return bool(_BROWSER_TARGET_ID_PATTERN.fullmatch(str(value or "").strip()))


@contextmanager
def owned_browser_target(target_id: str) -> Iterator[None]:
    """Bind CDP operations to one run-owned page target for this call context."""

    clean_target_id = str(target_id or "").strip()
    if not is_valid_target_id(clean_target_id):
        raise RuntimeError("No run-owned browser target is bound")
    token = _OWNED_BROWSER_TARGET_ID.set(clean_target_id)
    try:
        yield
    finally:
        _OWNED_BROWSER_TARGET_ID.reset(token)


def open_url(
    url: str,
    *,
    allow_system_browser_fallback: bool = False,
) -> dict[str, Any]:
    # macOS `open` can activate the user's real browser.  Keep it as an
    # explicit internal escape hatch; model-routed browser tools are CDP-only.
    clean_url = _clean_url(url)
    cdp_url = _configured_browser_cdp_url()
    if cdp_url:
        try:
            page = _cdp_new_page(cdp_url, clean_url)
            return {
                "ok": True,
                "action": "browser.open_url",
                "summary": f"Opened browser page: {clean_url}",
                "data": _page_summary(page, fallback_url=clean_url),
                "permission_error": False,
                "fallback_used": False,
            }
        except Exception as exc:
            if allow_system_browser_fallback:
                fallback = _open_url_fallback(clean_url)
                return {
                    **fallback,
                    "fallback_reason": str(exc),
                }
            return _cdp_unavailable("browser.open_url", exc)
    if allow_system_browser_fallback:
        return _open_url_fallback(clean_url)
    return _cdp_unavailable("browser.open_url")


def current_page() -> dict[str, Any]:
    try:
        page = _current_page()
    except Exception as exc:
        return _cdp_unavailable("browser.current_page", exc)
    data = _page_summary(page)
    return {
        "ok": True,
        "action": "browser.current_page",
        "summary": f"Current browser page: {data.get('title') or data.get('url') or 'Untitled'}",
        "data": data,
        "permission_error": False,
        "fallback_used": False,
    }


def close_target(target_id: str) -> dict[str, Any]:
    """Close one exact run-owned CDP page without touching browser focus."""

    clean_target_id = str(target_id or "").strip()
    if not is_valid_target_id(clean_target_id):
        return {
            "ok": False,
            "action": "browser.close_target",
            "error": "browser_owned_target_invalid",
            "fallback_used": False,
        }
    cdp_url = _configured_browser_cdp_url()
    if not cdp_url:
        return _cdp_unavailable("browser.close_target")
    try:
        pages = _cdp_list_pages(cdp_url)
        target = next(
            (
                page
                for page in pages
                if str(page.get("id") or "").strip() == clean_target_id
                and str(page.get("webSocketDebuggerUrl") or "").strip()
            ),
            None,
        )
        if target is None:
            return {
                "ok": True,
                "action": "browser.close_target",
                "summary": "Run-owned browser target was already unavailable",
                "data": {"target_id": clean_target_id, "already_closed": True},
                "fallback_used": False,
            }
        _cdp_close_page(cdp_url, clean_target_id)
        return {
            "ok": True,
            "action": "browser.close_target",
            "summary": "Closed run-owned browser target",
            "data": {"target_id": clean_target_id, "already_closed": False},
            "fallback_used": False,
        }
    except Exception as exc:
        return _cdp_unavailable("browser.close_target", exc)


def click(
    selector: str,
    *,
    fallback_x: Any = None,
    fallback_y: Any = None,
    click_count: Any = 1,
    foreground_fallback: Callable[[Any, Any, Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    clean_selector = _clean_required(selector, "selector")
    expression = f"""
    (() => {{
      const selector = {json.dumps(clean_selector)};
      const requestedClickCount = {json.dumps(click_count)};
      const textSelectorPrefix = 'text=';
      const pointSelectorPrefix = 'point=';
      const searchResultSelectorPrefix = 'search-result=';
      function normalized(value) {{
        return String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      }}
      function labelFor(el) {{
        return (
          el.innerText ||
          el.value ||
          el.getAttribute('aria-label') ||
          el.getAttribute('title') ||
          el.getAttribute('name') ||
          ''
        ).trim();
      }}
      function findByText(label) {{
        const target = normalized(label);
        const elements = Array.from(document.querySelectorAll(
          'button,a,[role="button"],input[type="button"],input[type="submit"],' +
          'input[type="reset"],[aria-label],[title]'
        ));
        return elements.find((el) => normalized(labelFor(el)).includes(target));
      }}
      function parsePoint(value) {{
        const parts = value.split(',').map((part) => Number(part.trim()));
        if (parts.length !== 2 || parts.some((part) => !Number.isFinite(part))) return null;
        return {{ x: parts[0], y: parts[1] }};
      }}
      function parseIndex(value) {{
        const index = Number.parseInt(value, 10);
        return Number.isFinite(index) && index > 0 ? index : 1;
      }}
      function isVisible(el) {{
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
      }}
      function cleanHref(el) {{
        try {{
          return new URL(el.href, window.location.href);
        }} catch (_) {{
          return null;
        }}
      }}
      function searchResultLinks() {{
        const roots = [
          document.querySelector('#search'),
          document.querySelector('main'),
          document.querySelector('[role="main"]'),
          document.body,
        ].filter(Boolean);
        const seen = new Set();
        const links = [];
        for (const root of roots) {{
          for (const el of Array.from(root.querySelectorAll('a[href]'))) {{
            if (seen.has(el)) continue;
            seen.add(el);
            if (!isVisible(el)) continue;
            const label = labelFor(el);
            if (!label) continue;
            const url = cleanHref(el);
            if (!url || !/^https?:$/.test(url.protocol)) continue;
            if (el.closest('nav,header,footer')) continue;
            if (/google\\.[^/]+\\/search/.test(url.href)) continue;
            if (/\\/preferences|\\/advanced_search|\\/intl\\//.test(url.pathname)) continue;
            links.push(el);
          }}
          if (links.length) break;
        }}
        return links;
      }}
      function findSearchResult(value) {{
        const links = searchResultLinks();
        return links[parseIndex(value) - 1] || null;
      }}
      const point = selector.startsWith(pointSelectorPrefix)
        ? parsePoint(selector.slice(pointSelectorPrefix.length))
        : null;
      const isSearchResultSelector = selector.startsWith(searchResultSelectorPrefix);
      const searchResult = isSearchResultSelector
        ? findSearchResult(selector.slice(searchResultSelectorPrefix.length))
        : null;
      const el = selector.startsWith(textSelectorPrefix)
        ? findByText(selector.slice(textSelectorPrefix.length))
        : isSearchResultSelector
          ? searchResult
        : point
          ? document.elementFromPoint(point.x, point.y)
        : document.querySelector(selector);
      if (!el) return {{ ok: false, error: 'selector_not_found', selector }};
      const sourceUrl = window.location.href;
      const navigationUrl = el.tagName === 'A' ? el.href : '';
      const linkTarget = el.tagName === 'A' ? (el.getAttribute('target') || '').toLowerCase() : '';
      if (!point) el.scrollIntoView({{ block: 'center', inline: 'center' }});
      const clickCount = Math.max(1, Math.min(3, Number(requestedClickCount) || 1));
      for (let index = 0; index < clickCount; index += 1) el.click();
      const label = labelFor(el);
      return {{ ok: true, selector, tag: el.tagName, label: label.slice(0, 200), x: point && point.x, y: point && point.y, click_count: clickCount,
        source_url: sourceUrl, navigation_url: navigationUrl, link_target: linkTarget }};
    }})()
    """
    try:
        value = _evaluate_current_page(expression)
    except Exception as exc:
        # Never turn a failed browser-profile action into a global desktop
        # click unless a trusted caller supplied that foreground capability.
        if foreground_fallback is None:
            return _cdp_unavailable("browser.click", exc)
        fallback = foreground_fallback
        return _click_fallback(
            clean_selector,
            fallback_x,
            fallback_y,
            click_count,
            exc,
            fallback,
        )
    if not value.get("ok"):
        return {
            "ok": False,
            "action": "browser.click",
            "summary": f"Browser selector not found: {clean_selector}",
            "data": value,
            "permission_error": False,
            "fallback_used": False,
        }
    return {
        "ok": True,
        "action": "browser.click",
        "summary": f"Clicked browser selector: {clean_selector}",
        "data": value,
        "permission_error": False,
        "fallback_used": False,
    }


def _click_fallback(
    selector: str,
    fallback_x: Any,
    fallback_y: Any,
    click_count: Any,
    cdp_error: Any,
    foreground_fallback: Callable[[Any, Any, Any], dict[str, Any]],
) -> dict[str, Any]:
    if fallback_x in (None, "") or fallback_y in (None, ""):
        return {
            "ok": False,
            "action": "browser.click",
            "summary": (
                "Chrome CDP is unavailable; browser.click needs explicit screen coordinates "
                "for foreground fallback."
            ),
            "error": "browser_click_fallback_coordinates_required",
            "data": {
                "selector": selector,
                "required_fallback_fields": ["fallback_x", "fallback_y"],
                "recommended_tools": ["screen.capture", "desktop.click"],
            },
            "permission_error": True,
            "fallback_used": False,
            "fallback": "desktop.click",
            "missing_permissions": ["chrome_cdp"],
            "permission_targets": ["chrome_cdp"],
            "recovery_actions": _permission_recovery_actions_for_targets(["chrome_cdp"]),
            "detail": str(cdp_error),
        }
    try:
        fallback_result = foreground_fallback(fallback_x, fallback_y, click_count)
    except Exception as exc:
        return _click_fallback_unavailable(cdp_error, exc)
    if not fallback_result.get("ok"):
        return _click_fallback_unavailable(
            cdp_error,
            fallback_result.get("error") or fallback_result.get("summary") or fallback_result,
            fallback_result=fallback_result,
        )
    data = dict(fallback_result.get("data") or {})
    data["selector"] = selector
    payload = {
        "ok": True,
        "action": "browser.click",
        "summary": "Clicked foreground desktop coordinate as browser fallback",
        "data": data,
        "permission_error": False,
        "fallback_used": True,
        "fallback": "desktop.click",
        "fallback_reason": str(cdp_error),
        "missing_permissions": ["chrome_cdp"],
        "permission_targets": ["chrome_cdp"],
    }
    if fallback_result.get("foreground_lock"):
        payload["foreground_lock"] = fallback_result["foreground_lock"]
    return payload


def _click_foreground_fallback(x: Any, y: Any, click_count: Any) -> dict[str, Any]:
    from apps.shell.agent.tools import desktop

    return desktop.desktop_click(x, y, click_count=click_count)


def _click_fallback_unavailable(
    cdp_error: Any,
    fallback_error: Any,
    *,
    fallback_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    missing_permissions = ["chrome_cdp"]
    permission_targets = ["chrome_cdp"]
    result = fallback_result if isinstance(fallback_result, dict) else {}
    missing_permissions.extend(_string_list(result.get("missing_permissions")))
    permission_targets.extend(_string_list(result.get("permission_targets")))
    if not result and _looks_like_foreground_permission_error(fallback_error):
        missing_permissions.append("accessibility")
        permission_targets.append("accessibility")
    payload = {
        "ok": False,
        "action": "browser.click",
        "summary": "Chrome CDP is unavailable and foreground click fallback failed",
        "error": "browser_foreground_click_fallback_unavailable",
        "permission_error": True,
        "fallback_used": True,
        "fallback": "desktop.click",
        "missing_permissions": _dedupe(missing_permissions),
        "permission_targets": _dedupe(permission_targets),
        "detail": f"cdp: {cdp_error}; desktop.click: {fallback_error}",
        "fallback_result": fallback_result or {},
    }
    recovery_actions = _permission_recovery_actions_for_targets(payload["permission_targets"])
    if recovery_actions:
        payload["recovery_actions"] = recovery_actions
    for key in ("foreground_lock_busy", "locked_by", "foreground_lock"):
        if key in result:
            payload[key] = result[key]
    return payload


def type_text(
    selector: str,
    text: str,
    *,
    fallback_x: Any = None,
    fallback_y: Any = None,
    foreground_fallback: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    clean_selector = _clean_required(selector, "selector")
    clean_text = _clean_required(text, "text")
    expression = f"""
    (() => {{
      const selector = {json.dumps(clean_selector)};
      const text = {json.dumps(clean_text)};
      const pointSelectorPrefix = 'point=';
      function parsePoint(value) {{
        const parts = value.split(',').map((part) => Number(part.trim()));
        if (parts.length !== 2 || parts.some((part) => !Number.isFinite(part))) return null;
        return {{ x: parts[0], y: parts[1] }};
      }}
      function editableTarget(target) {{
        if (!target) return null;
        if (target.matches('input,textarea,[contenteditable],[role="textbox"]')) return target;
        return target.closest('input,textarea,[contenteditable],[role="textbox"]') || target;
      }}
      const point = selector.startsWith(pointSelectorPrefix)
        ? parsePoint(selector.slice(pointSelectorPrefix.length))
        : null;
      const el = editableTarget(point ? document.elementFromPoint(point.x, point.y) : document.querySelector(selector));
      if (!el) return {{ ok: false, error: 'selector_not_found', selector }};
      if (!point) el.scrollIntoView({{ block: 'center', inline: 'center' }});
      el.focus();
      if ('value' in el) {{
        el.value = text;
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
      }} else {{
        el.textContent = text;
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
      }}
      const observedText = 'value' in el ? el.value : el.textContent;
      return {{
        ok: true,
        selector,
        tag: el.tagName,
        length: text.length,
        content_verified: observedText === text,
        x: point && point.x,
        y: point && point.y,
      }};
    }})()
    """
    try:
        value = _evaluate_current_page(expression)
    except Exception as exc:
        # A CDP failure must not type into whichever app the user currently
        # has focused.  Foreground fallback is an explicit internal capability.
        if foreground_fallback is None:
            return _cdp_unavailable("browser.type_text", exc)
        fallback = foreground_fallback
        return _type_text_fallback(clean_selector, clean_text, fallback_x, fallback_y, exc, fallback)
    if not value.get("ok"):
        return {
            "ok": False,
            "action": "browser.type_text",
            "summary": f"Browser selector not found: {clean_selector}",
            "data": value,
            "permission_error": False,
            "fallback_used": False,
        }
    return {
        "ok": True,
        "action": "browser.type_text",
        "summary": f"Typed text into browser selector: {clean_selector}",
        "data": value,
        "permission_error": False,
        "fallback_used": False,
    }


def _type_text_fallback(
    selector: str,
    text: str,
    fallback_x: Any,
    fallback_y: Any,
    cdp_error: Any,
    foreground_fallback: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    has_coordinates = fallback_x not in (None, "") and fallback_y not in (None, "")
    fallback_name = "desktop.click+desktop.type_text" if has_coordinates else "desktop.type_text"
    try:
        fallback_result = (
            foreground_fallback(fallback_x, fallback_y, text)
            if has_coordinates
            else foreground_fallback(text)
        )
    except Exception as exc:
        return _foreground_fallback_unavailable("browser.type_text", cdp_error, exc, fallback=fallback_name)
    if not fallback_result.get("ok"):
        return _foreground_fallback_unavailable(
            "browser.type_text",
            cdp_error,
            fallback_result.get("error") or fallback_result.get("summary") or fallback_result,
            fallback_result=fallback_result,
            fallback=fallback_name,
        )
    data = dict(fallback_result.get("data") or {})
    data["selector"] = selector
    if has_coordinates:
        data.setdefault("x", fallback_x)
        data.setdefault("y", fallback_y)
    data.setdefault("character_count", len(text))
    payload = {
        "ok": True,
        "action": "browser.type_text",
        "summary": "Typed text into the foreground app as browser fallback",
        "data": data,
        "permission_error": False,
        "fallback_used": True,
        "fallback": fallback_name,
        "fallback_reason": str(cdp_error),
        "missing_permissions": ["chrome_cdp"],
        "permission_targets": ["chrome_cdp"],
    }
    if fallback_result.get("foreground_lock"):
        payload["foreground_lock"] = fallback_result["foreground_lock"]
    return payload


def _type_text_foreground_fallback(*args: Any) -> dict[str, Any]:
    from apps.shell.agent.tools import desktop

    if len(args) == 3:
        x, y, text = args
        click_result = desktop.desktop_click(x, y)
        if not click_result.get("ok"):
            return click_result
        type_result = desktop.desktop_type_text(text)
        if not type_result.get("ok"):
            return type_result
        click_data = click_result.get("data") if isinstance(click_result.get("data"), dict) else {}
        type_data = type_result.get("data") if isinstance(type_result.get("data"), dict) else {}
        return {
            "ok": True,
            "action": "desktop.click+desktop.type_text",
            "summary": "Clicked foreground coordinate and typed text",
            "data": {
                **type_data,
                "x": click_data.get("x", x),
                "y": click_data.get("y", y),
                "character_count": type_data.get("character_count", len(str(text))),
            },
            "permission_error": False,
            "fallback_used": False,
        }
    (text,) = args
    return desktop.desktop_type_text(text)


def _foreground_fallback_unavailable(
    action: str,
    cdp_error: Any,
    fallback_error: Any,
    *,
    fallback_result: dict[str, Any] | None = None,
    fallback: str = "desktop.type_text",
) -> dict[str, Any]:
    missing_permissions = ["chrome_cdp"]
    permission_targets = ["chrome_cdp"]
    result = fallback_result if isinstance(fallback_result, dict) else {}
    missing_permissions.extend(_string_list(result.get("missing_permissions")))
    permission_targets.extend(_string_list(result.get("permission_targets")))
    if not result and _looks_like_foreground_permission_error(fallback_error):
        missing_permissions.append("accessibility")
        permission_targets.append("accessibility")
    payload = {
        "ok": False,
        "action": action,
        "summary": "Chrome CDP is unavailable and foreground input fallback failed",
        "error": "browser_foreground_fallback_unavailable",
        "permission_error": True,
        "fallback_used": True,
        "fallback": fallback,
        "missing_permissions": _dedupe(missing_permissions),
        "permission_targets": _dedupe(permission_targets),
        "detail": f"cdp: {cdp_error}; desktop.type_text: {fallback_error}",
        "fallback_result": fallback_result or {},
    }
    recovery_actions = _permission_recovery_actions_for_targets(payload["permission_targets"])
    if recovery_actions:
        payload["recovery_actions"] = recovery_actions
    for key in ("foreground_lock_busy", "locked_by", "foreground_lock"):
        if key in result:
            payload[key] = result[key]
    return payload


def extract_text(selector: str = "") -> dict[str, Any]:
    clean_selector = str(selector or "").strip()
    selector_json = json.dumps(clean_selector)
    expression = f"""
    (() => {{
      const selector = {selector_json};
      const root = selector
        ? document.querySelector(selector)
        : (document.body || document.documentElement);
      if (!root) {{
        if (selector) return {{ ok: false, error: 'selector_not_found', selector }};
        const rawPageUrl = String(location.href || '');
        return {{
          ok: true,
          text: '',
          page_url: rawPageUrl.slice(0, 2048),
          page_url_truncated: rawPageUrl.length > 2048,
          link_contexts: [],
        }};
      }}
      const compactText = (value) => String(value || '')
        .replace(/[\\r\\n]+/g, ' · ')
        .replace(/\\s+/g, ' ')
        .trim();
      const anchors = [];
      if (root.matches && root.matches('a[href]')) anchors.push(root);
      if (root.querySelectorAll) {{
        for (const anchor of root.querySelectorAll('a[href]')) {{
          if (anchors.length >= 40) break;
          anchors.push(anchor);
        }}
      }}
      const seen = new Set();
      const linkContexts = [];
      for (const anchor of anchors) {{
        if (linkContexts.length >= 40) break;
        const rawHref = String(anchor.href || '').trim();
        if (!rawHref || rawHref.length > 2048) continue;
        const href = rawHref;
        let node = anchor;
        let contextText = compactText(anchor.innerText || anchor.textContent);
        if (contextText.length > 800) continue;
        for (let depth = 0; depth < 4 && node.parentElement; depth += 1) {{
          const parent = node.parentElement;
          const candidate = compactText(parent.innerText || parent.textContent);
          if (candidate.length > 800) break;
          if (candidate) contextText = candidate;
          node = parent;
        }}
        const key = `${{href}}\\n${{contextText}}`;
        if (seen.has(key)) continue;
        seen.add(key);
        linkContexts.push({{ href, text: contextText }});
      }}
      const rawPageUrl = String(location.href || '');
      return {{
        ok: true,
        text: (root.innerText || root.textContent || '').trim().slice(0, 20001),
        page_url: rawPageUrl.slice(0, 2048),
        page_url_truncated: rawPageUrl.length > 2048,
        link_contexts: linkContexts,
      }};
    }})()
    """
    try:
        value = _evaluate_current_page(expression)
    except Exception as exc:
        return _cdp_unavailable("browser.extract_text", exc)
    if not value.get("ok"):
        return {
            "ok": False,
            "action": "browser.extract_text",
            "summary": "Browser text selector not found",
            "data": value,
            "permission_error": False,
            "fallback_used": False,
        }
    text_value = str(value.get("text") or "")
    truncated = len(text_value) > 20000
    if truncated:
        text_value = text_value[:20000]
    raw_page_url = str(value.get("page_url") or "").strip()
    page_url_truncated = bool(value.get("page_url_truncated")) or len(raw_page_url) > 2048
    page_url = "" if page_url_truncated else raw_page_url
    link_contexts: list[dict[str, str]] = []
    raw_link_contexts = value.get("link_contexts")
    if isinstance(raw_link_contexts, list):
        for raw_context in raw_link_contexts[:40]:
            if not isinstance(raw_context, dict):
                continue
            href = str(raw_context.get("href") or "").strip()
            context_text = " ".join(str(raw_context.get("text") or "").split())
            if href and len(href) <= 2048 and len(context_text) <= 800:
                link_contexts.append({"href": href, "text": context_text})
    return {
        "ok": True,
        "action": "browser.extract_text",
        "summary": f"Extracted {len(text_value)} characters from browser page",
        "data": {
            "selector": clean_selector,
            "text": text_value,
            "truncated": truncated,
            "page_url": page_url,
            "page_url_truncated": page_url_truncated,
            "link_contexts": link_contexts,
        },
        "permission_error": False,
        "fallback_used": False,
    }


def screenshot(
    target_path: Path,
    *,
    allow_screen_fallback: bool = False,
) -> dict[str, Any]:
    target = Path(target_path)
    try:
        websocket_url = _page_websocket_url()
        result = _cdp_command(
            websocket_url,
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True},
        )
    except Exception as exc:
        if allow_screen_fallback:
            return _screenshot_fallback(target, exc)
        return _cdp_unavailable("browser.screenshot", exc)
    data = str(result.get("data") or "")
    if not data:
        return {
            "ok": False,
            "action": "browser.screenshot",
            "summary": "Browser screenshot did not return image data",
            "data": {},
            "permission_error": False,
            "fallback_used": False,
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    image_bytes = base64.b64decode(data)
    target.write_bytes(image_bytes)
    return {
        "ok": True,
        "action": "browser.screenshot",
        "summary": "Captured current browser page",
        "data": {
            "path": str(target),
            "mime_type": "image/png",
            "format": "png",
            "size": len(image_bytes),
        },
        "permission_error": False,
        "fallback_used": False,
    }


def _screenshot_fallback(target_path: Path, cdp_error: Any) -> dict[str, Any]:
    try:
        metadata = _capture_screen_fallback(target_path)
    except Exception as exc:
        return _screenshot_fallback_unavailable(cdp_error, exc)
    data = dict(metadata if isinstance(metadata, dict) else {})
    data.setdefault("path", str(target_path))
    data.setdefault("mime_type", "image/png")
    data.setdefault("format", "png")
    return {
        "ok": True,
        "action": "browser.screenshot",
        "summary": "Captured screen as browser screenshot fallback",
        "data": data,
        "permission_error": False,
        "fallback_used": True,
        "fallback": "screen.capture",
        "fallback_reason": str(cdp_error),
        "missing_permissions": ["chrome_cdp"],
        "permission_targets": ["chrome_cdp"],
    }


def _capture_screen_fallback(target_path: Path) -> dict[str, Any]:
    from apps.locald.screenshot import capture_screenshot_to_file

    metadata = capture_screenshot_to_file(target_path)
    return dict(metadata if isinstance(metadata, dict) else {})


def _screenshot_fallback_unavailable(cdp_error: Any, fallback_error: Any) -> dict[str, Any]:
    missing_permissions = ["chrome_cdp"]
    permission_targets = ["chrome_cdp"]
    if _looks_like_screen_capture_permission_error(fallback_error):
        missing_permissions.append("screen_recording")
        permission_targets.append("screen_recording")
    permission_targets = _dedupe(permission_targets)
    payload = {
        "ok": False,
        "action": "browser.screenshot",
        "summary": "Chrome CDP is unavailable and screen capture fallback failed",
        "error": "browser_screenshot_unavailable",
        "permission_error": True,
        "fallback_used": True,
        "fallback": "screen.capture",
        "missing_permissions": _dedupe(missing_permissions),
        "permission_targets": permission_targets,
        "detail": f"cdp: {cdp_error}; screen.capture: {fallback_error}",
    }
    recovery_actions = _permission_recovery_actions_for_targets(permission_targets)
    if recovery_actions:
        payload["recovery_actions"] = recovery_actions
    return payload


def _evaluate_current_page(expression: str) -> dict[str, Any]:
    websocket_url = _page_websocket_url()
    result = _cdp_command(
        websocket_url,
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
    )
    value = result.get("result", {}).get("value")
    return value if isinstance(value, dict) else {"ok": True, "value": value}


def _current_page() -> dict[str, Any]:
    cdp_url = _configured_browser_cdp_url()
    if not cdp_url:
        raise RuntimeError("browser.cdp_url is not configured")
    pages = _cdp_list_pages(cdp_url)
    owned_target_id = _OWNED_BROWSER_TARGET_ID.get().strip()
    page = (
        next(
            (
                candidate
                for candidate in pages
                if str(candidate.get("id") or "").strip() == owned_target_id
            ),
            None,
        )
        if owned_target_id
        else _select_page(pages)
    )
    if page is None:
        if owned_target_id:
            raise RuntimeError(
                f"Run-owned browser target is unavailable: {owned_target_id}"
            )
        raise RuntimeError("No debuggable browser page found")
    return page


def _page_websocket_url() -> str:
    page = _current_page()
    websocket_url = str(page.get("webSocketDebuggerUrl") or "").strip()
    if not websocket_url:
        raise RuntimeError("Current browser page has no websocket debugger URL")
    return websocket_url


def _cdp_new_page(cdp_url: str, url: str) -> dict[str, Any]:
    endpoint = _cdp_endpoint(cdp_url, f"/json/new?{quote(url, safe='')}")
    try:
        return _http_json(endpoint, method="PUT")
    except HTTPError as exc:
        # Older CDP endpoints accepted GET.  Retry only when the server
        # explicitly rejects PUT; a timeout may already have created a page,
        # so retrying it could leave an unowned duplicate tab behind.
        if exc.code not in {404, 405}:
            raise
        return _http_json(endpoint, method="GET")


def _cdp_list_pages(cdp_url: str) -> list[dict[str, Any]]:
    payload = _http_json(_cdp_endpoint(cdp_url, "/json/list"))
    if not isinstance(payload, list):
        raise RuntimeError("CDP /json/list did not return a list")
    return [item for item in payload if isinstance(item, dict)]


def _cdp_close_page(cdp_url: str, target_id: str) -> None:
    endpoint = _cdp_endpoint(cdp_url, f"/json/close/{quote(target_id, safe='')}")
    request = Request(endpoint, method="GET")
    with urlopen(request, timeout=2.0) as response:
        response.read()


def _http_json(url: str, *, method: str = "GET") -> Any:
    request = Request(url, method=method)
    with urlopen(request, timeout=2.0) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw or "{}")


def _cdp_command(
    websocket_url: str,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _CdpWebSocket(websocket_url) as websocket:
        websocket.send_json({"id": 1, "method": method, "params": params or {}})
        while True:
            message = websocket.recv_json()
            if message.get("id") != 1:
                continue
            if "error" in message:
                raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
            result = message.get("result")
            return result if isinstance(result, dict) else {}


def _configured_browser_cdp_url() -> str:
    for env_name in ("YACHIYO_BROWSER_CDP_URL", "BROWSER_CDP_URL"):
        value = str(os.environ.get(env_name) or "").strip()
        if value:
            if not _browser_cdp_url_is_loopback(value):
                return (
                    value
                    if _truthy_config_value(
                        os.environ.get("YACHIYO_BROWSER_CDP_EXTERNAL_EXPLICIT")
                    )
                    else ""
                )
            profile_dir = str(
                os.environ.get("YACHIYO_BROWSER_CDP_PROFILE_DIR") or ""
            ).strip()
            pid = _positive_int(os.environ.get("YACHIYO_BROWSER_CDP_PID"))
            return (
                value
                if _truthy_config_value(
                    os.environ.get("YACHIYO_BROWSER_CDP_OWNED")
                )
                and _browser_cdp_process_matches(
                    value,
                    pid=pid,
                    profile_dir=profile_dir,
                )
                else ""
            )
    try:
        from apps.shell import config as shell_config

        path = Path(shell_config._CONFIG_DIR) / "native_tool_config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    config = data.get("config")
    if not isinstance(config, dict):
        return ""
    value = str(config.get("browser.cdp_url") or "").strip()
    if not value:
        return ""
    if not _browser_cdp_url_is_loopback(value):
        return value if _truthy_config_value(
            config.get("browser.cdp_external_explicit")
        ) else ""
    if str(config.get("browser.cdp_owner") or "").strip() != "oha-yachiyo":
        return ""
    pid = _positive_int(config.get("browser.cdp_pid"))
    profile_dir = str(config.get("browser.cdp_profile_dir") or "").strip()
    return (
        value
        if _browser_cdp_process_matches(
            value,
            pid=pid,
            profile_dir=profile_dir,
        )
        else ""
    )


def _browser_cdp_url_is_loopback(value: str) -> bool:
    try:
        hostname = str(urlparse(value).hostname or "").strip().lower()
    except ValueError:
        return False
    return hostname in {"127.0.0.1", "localhost", "::1"}


def _browser_cdp_process_matches(
    cdp_url: str,
    *,
    pid: int | None,
    profile_dir: str,
    run: Callable[..., Any] | None = None,
    which: Callable[[str], str | None] | None = None,
    path_is_file: Callable[[str], bool] | None = None,
    home_dir: str | Path | None = None,
) -> bool:
    if pid is None or pid <= 0 or not profile_dir:
        return False
    runner = run or subprocess.run
    resolve_executable = which or shutil.which
    is_file = path_is_file or os.path.isfile
    try:
        parsed = urlparse(cdp_url)
        port = parsed.port
        expected_profile = Path(profile_dir).expanduser().resolve(strict=False)
        dedicated_profile = (
            Path(home_dir or Path.home()) / ".oha-yachiyo" / "chrome-debug"
        ).resolve(strict=False)
    except (OSError, ValueError):
        return False
    if (
        port is None
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or expected_profile != dedicated_profile
    ):
        return False
    try:
        result = runner(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    try:
        tokens = shlex.split(result.stdout.strip())
    except ValueError:
        return False
    if not tokens or not _browser_cdp_executable_is_google_chrome(
        tokens[0],
        path_is_file=is_file,
    ):
        return False
    expected_port_arg = f"--remote-debugging-port={port}"
    profile_args = [
        token.split("=", 1)[1]
        for token in tokens
        if token.startswith("--user-data-dir=")
    ]
    if expected_port_arg not in tokens or len(profile_args) != 1:
        return False
    try:
        actual_profile = Path(profile_args[0]).expanduser().resolve(strict=False)
    except OSError:
        return False
    if actual_profile != expected_profile:
        return False
    return _browser_cdp_pid_owns_loopback_listener(
        pid,
        host=str(parsed.hostname or ""),
        port=port,
        run=runner,
        which=resolve_executable,
        path_is_file=is_file,
    )


def _browser_cdp_executable_is_google_chrome(
    executable: str,
    *,
    path_is_file: Callable[[str], bool],
) -> bool:
    try:
        path = Path(executable).expanduser().resolve(strict=False)
    except OSError:
        return False
    parts = path.parts
    expected_suffix = (
        "Google Chrome.app",
        "Contents",
        "MacOS",
        "Google Chrome",
    )
    return (
        len(parts) >= len(expected_suffix)
        and tuple(parts[-4:]) == expected_suffix
        and path_is_file(str(path))
    )


def _browser_cdp_pid_owns_loopback_listener(
    pid: int,
    *,
    host: str,
    port: int,
    run: Callable[..., Any],
    which: Callable[[str], str | None],
    path_is_file: Callable[[str], bool],
) -> bool:
    lsof = str(which("lsof") or "").strip()
    if not lsof and path_is_file("/usr/sbin/lsof"):
        lsof = "/usr/sbin/lsof"
    if not lsof:
        return False
    try:
        result = run(
            [
                lsof,
                "-nP",
                "-a",
                "-p",
                str(pid),
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                "-Fpn",
            ],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    lines = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if f"p{pid}" not in lines:
        return False
    accepted_hosts = {host}
    if host == "localhost":
        accepted_hosts.update({"127.0.0.1", "::1"})
    expected_names = {
        f"n{candidate}:{port}" if ":" not in candidate else f"n[{candidate}]:{port}"
        for candidate in accepted_hosts
    }
    return bool(lines & expected_names)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _truthy_config_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _open_url_fallback(url: str) -> dict[str, Any]:
    if sys.platform != "darwin":
        return _cdp_unavailable("browser.open_url")
    try:
        result = subprocess.run(
            ["open", url],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return _cdp_unavailable("browser.open_url", exc)
    if result.returncode != 0:
        return _cdp_unavailable("browser.open_url", result.stderr or result.stdout)
    return {
        "ok": True,
        "action": "browser.open_url",
        "summary": f"Opened URL in the system browser: {url}",
        "data": {"url": url},
        "permission_error": False,
        "fallback_used": True,
        "fallback": "system_browser",
    }


def _cdp_unavailable(action: str, detail: Any = None) -> dict[str, Any]:
    payload = {
        "ok": False,
        "action": action,
        "summary": "Chrome CDP is unavailable",
        "error": "chrome_cdp_unavailable",
        "permission_error": True,
        "fallback_used": False,
        "missing_permissions": ["chrome_cdp"],
        "permission_targets": ["chrome_cdp"],
        "recovery_requires_user_targets": ["chrome_cdp"],
        "recovery_hints": [
            (
                "启动 Chrome DevTools/CDP，或在 Native Agent diagnostics 里配置 "
                "browser.cdp_url 后再重试浏览器自动化。"
            )
        ],
        "recovery_actions": _permission_recovery_actions_for_targets(["chrome_cdp"]),
    }
    if detail:
        payload["detail"] = str(detail)
    return payload


def _permission_recovery_actions_for_targets(targets: list[str]) -> list[dict[str, Any]]:
    actions_by_target = {
        "chrome_cdp": (
            {
                "label": "打开 Google Chrome",
                "tool": "app.open",
                "input": {"app_name": "Google Chrome"},
                "permission_target": "chrome_cdp",
                "risk_level": "low",
            },
        ),
        "screen_recording": (
            {
                "label": "打开屏幕录制权限",
                "tool": "app.open",
                "input": {"app_name": "屏幕录制权限"},
                "permission_target": "screen_recording",
                "risk_level": "low",
            },
        ),
        "accessibility": (
            {
                "label": "打开辅助功能权限",
                "tool": "app.open",
                "input": {"app_name": "辅助功能权限"},
                "permission_target": "accessibility",
                "risk_level": "low",
            },
        ),
    }
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    actions: list[dict[str, Any]] = []
    for target in targets:
        for action in actions_by_target.get(str(target or "").strip(), ()):
            tool_name = str(action.get("tool") or "").strip()
            raw_input = action.get("input") if isinstance(action.get("input"), dict) else {}
            input_key = tuple(sorted((str(key), str(value)) for key, value in raw_input.items()))
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


def _looks_like_screen_capture_permission_error(value: Any) -> bool:
    if value.__class__.__name__ == "ScreenCapturePermissionError":
        return True
    normalized = str(value or "").lower()
    return any(
        marker in normalized
        for marker in (
            "screen recording",
            "screen capture",
            "not authorized",
            "not permitted",
            "privacy",
            "tcc",
        )
    )


def _looks_like_foreground_permission_error(value: Any) -> bool:
    normalized = str(value or "").lower()
    return any(
        marker in normalized
        for marker in (
            "accessibility",
            "not authorized",
            "not allowed",
            "not permitted",
            "privacy",
            "tcc",
        )
    )


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _dedupe(values: list[str]) -> list[str]:
    clean: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in clean:
            clean.append(item)
    return clean


def _select_page(pages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for page in pages:
        is_page = str(page.get("type") or "") == "page"
        is_devtools = str(page.get("url") or "").startswith("devtools://")
        if is_page and not is_devtools:
            return page
    return pages[0] if pages else None


def _page_summary(page: dict[str, Any], *, fallback_url: str = "") -> dict[str, Any]:
    return {
        "target_id": str(page.get("id") or "").strip(),
        "target_websocket_available": bool(
            str(page.get("webSocketDebuggerUrl") or "").strip()
        ),
        "title": str(page.get("title") or ""),
        "url": str(page.get("url") or fallback_url),
        "type": str(page.get("type") or ""),
    }


def _cdp_endpoint(cdp_url: str, path: str) -> str:
    base = str(cdp_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("browser.cdp_url is not configured")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _clean_url(url: str) -> str:
    clean = str(url or "").strip()
    parsed = urlparse(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("browser.open_url only accepts absolute http(s) URLs")
    return clean


def _clean_required(value: str, field_name: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{field_name} is required")
    return clean


class _CdpWebSocket:
    def __init__(self, websocket_url: str, *, timeout: float = 5.0) -> None:
        self.websocket_url = websocket_url
        self.timeout = timeout
        self._socket: socket.socket | ssl.SSLSocket | None = None

    def __enter__(self) -> "_CdpWebSocket":
        self._connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def _connect(self) -> None:
        parsed = urlparse(self.websocket_url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise RuntimeError("Invalid CDP websocket URL")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        sock: socket.socket | ssl.SSLSocket = socket.create_connection(
            (parsed.hostname, port),
            timeout=self.timeout,
        )
        if parsed.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
        sock.settimeout(self.timeout)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = sock.recv(4096)
            if not chunk:
                break
            header += chunk
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            sock.close()
            raise RuntimeError("CDP websocket upgrade failed")
        self._socket = sock

    def send_json(self, payload: dict[str, Any]) -> None:
        self._send_text(json.dumps(payload, ensure_ascii=False))

    def recv_json(self) -> dict[str, Any]:
        while True:
            opcode, payload = self._recv_frame()
            if opcode == 1:
                return json.loads(payload.decode("utf-8"))
            if opcode == 8:
                raise RuntimeError("CDP websocket closed")

    def _send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.extend([0x80 | 126, (length >> 8) & 0xFF, length & 0xFF])
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, "big"))
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._require_socket().sendall(bytes(header) + mask + masked)

    def _recv_frame(self) -> tuple[int, bytes]:
        sock = self._require_socket()
        first, second = self._recv_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(self._recv_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._recv_exact(8), "big")
        mask = self._recv_exact(4) if second & 0x80 else b""
        payload = self._recv_exact(length) if length else b""
        if mask:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def _recv_exact(self, size: int) -> bytes:
        data = b""
        sock = self._require_socket()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise RuntimeError("CDP websocket disconnected")
            data += chunk
        return data

    def _require_socket(self) -> socket.socket | ssl.SSLSocket:
        if self._socket is None:
            raise RuntimeError("CDP websocket is not connected")
        return self._socket
