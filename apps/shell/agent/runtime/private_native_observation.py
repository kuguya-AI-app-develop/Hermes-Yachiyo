"""One-use process-private bytes from a canonically bound native observation."""

from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

_READ_TOOLS = frozenset({"desktop.ui_elements", "clipboard.read"})
_SCOPE_KEYS = ("run_id", "plan_id", "request_id", "tool_call_id", "step_id", "tool")


class _NativeObservationToken:
    def __init__(self, channel: "PrivateNativeObservationChannel", request, data):
        self.channel = channel
        self.scope = {key: str(request.get(key) or "") for key in _SCOPE_KEYS}
        self.data = data
        self.used = False


class PrivateNativeObservationChannel:
    """An internal owner validates its own opaque, original-plan binding.

    Each instance is an independent authority. Native executor output carries a
    nonserializable token, which its owner must consume before public projection.
    The channel cannot turn ordinary provider/model mapping output into evidence.
    """

    def __init__(
        self,
        bound_request: Callable[[Mapping[str, Any]], bool],
        *,
        authority: object,
        tools: frozenset[str] = frozenset({"desktop.ui_elements"}),
    ):
        if not tools or not tools.issubset(_READ_TOOLS):
            raise ValueError("private_native_observation_tools_invalid")
        self._bound_request = bound_request
        self._authority = authority
        self._tools = tools

    def capture(
        self,
        request: Mapping[str, Any],
        raw_result: Mapping[str, Any],
        *,
        local_broker_executed: bool,
    ) -> Any:
        if not local_broker_executed or not self._bound_request(request):
            return None
        if (
            raw_result.get("ok") is not True
            or raw_result.get("permission_error")
            or raw_result.get("approval_required")
        ):
            return None
        tool = request.get("tool")
        if tool not in self._tools:
            return None
        data = raw_result.get("data")
        if not isinstance(data, Mapping):
            return None
        if tool == "desktop.ui_elements":
            focused = data.get("focused_element")
            if not isinstance(focused, Mapping):
                return None
            raw = {
                key: deepcopy(data.get(key))
                for key in ("app_name", "pid", "window_id", "title", "truncated")
            }
            elements = data.get("elements")
            if isinstance(elements, list):
                raw["elements"] = [
                    {
                        key: deepcopy(element[key])
                        for key in (
                            "role",
                            "name",
                            "value",
                            "description",
                            "depth",
                            "identifier",
                            "focused",
                            "editable",
                            "enabled",
                        )
                        if key in element
                    }
                    for element in elements[:80]
                    if isinstance(element, Mapping)
                ]
                if len(elements) > 80:
                    raw["truncated"] = True
            raw["focused_element"] = {
                key: focused[key]
                for key in (
                    "value",
                    "role",
                    "focused",
                    "editable",
                    "enabled",
                    "identifier",
                    "name",
                    "description",
                )
                if key in focused
            }
        else:
            raw = {
                key: data.get(key)
                for key in (
                    "text",
                    "text_length",
                    "max_chars",
                    "truncated",
                    "pasteboard_revision",
                    "pasteboard_revision_stable",
                )
            }
        return _NativeObservationToken(self, request, raw)

    def consume(
        self,
        token: Any,
        request: Mapping[str, Any],
        *,
        run_id: str,
    ) -> dict[str, Any]:
        if type(token) is not _NativeObservationToken or token.used:
            return {}
        token.used = True
        raw_data, token.data = token.data, {}
        if token.channel is not self:
            return {}
        expected = {key: str(request.get(key) or "") for key in _SCOPE_KEYS}
        if (
            expected["run_id"] != run_id
            or token.scope != expected
            or not all(expected.values())
            or not self._bound_request(request)
        ):
            return {}
        return {"_authority": self._authority, "scope": token.scope, "data": raw_data}
