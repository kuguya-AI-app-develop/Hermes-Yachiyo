"""Private native byte observations require one exact owner and request."""

from copy import deepcopy

import pytest

from apps.shell.agent.runtime.private_native_observation import PrivateNativeObservationChannel

_BINDING = object()


def _channel():
    return PrivateNativeObservationChannel(
        lambda request: request.get("binding") is _BINDING, authority=object()
    )


def _request():
    return {
        "run_id": "run",
        "plan_id": "plan",
        "request_id": "request",
        "tool_call_id": "call",
        "step_id": "step",
        "tool": "desktop.ui_elements",
        "binding": _BINDING,
    }


def _raw():
    return {
        "ok": True,
        "data": {
            "app_name": "WeChat",
            "pid": 1,
            "window_id": 2,
            "title": "张三",
            "focused_element": {
                "value": "  草稿\t\r\n🚀 sk-proj-test-key-value ",
                "role": "AXTextArea",
                "focused": True,
                "identifier": "Message",
            },
            "elements": [{"role": "AXHeading", "value": "张三", "depth": 1}],
            "unrelated": "must not be retained",
        },
    }


def test_raw_bytes_and_recipient_tree_are_frozen_and_token_is_consumed_once():
    owner, request, raw = _channel(), _request(), _raw()
    expected = deepcopy(raw["data"])
    token = owner.capture(request, raw, local_broker_executed=True)
    raw["data"]["focused_element"]["value"] = "changed"
    raw["data"]["elements"][0]["value"] = "李四"
    record = owner.consume(token, request, run_id="run")
    assert record["data"]["focused_element"] == expected["focused_element"]
    assert record["data"]["elements"] == expected["elements"]
    assert record["data"]["title"] == "张三"
    assert "unrelated" not in record["data"]
    assert token.data == {}
    assert owner.consume(token, request, run_id="run") == {}


@pytest.mark.parametrize(
    "bad",
    [
        "mapping",
        "owner",
        "run_id",
        "plan_id",
        "request_id",
        "tool_call_id",
        "step_id",
        "tool",
        "binding",
    ],
)
def test_wrong_scope_or_forged_public_output_cannot_be_consumed(bad):
    owner, request = _channel(), _request()
    token = owner.capture(request, _raw(), local_broker_executed=True)
    consumer = owner
    changed = dict(request)
    if bad == "mapping":
        token = {"channel": owner, "scope": request, "data": _raw()["data"], "used": False}
    elif bad == "owner":
        consumer = _channel()
    else:
        changed[bad] = "foreign"
    # The trusted caller's run does not override a changed request's run.
    assert consumer.consume(token, changed, run_id="run") == {}
    assert owner.consume(token, request, run_id="run") == {}


@pytest.mark.parametrize(
    "bad",
    ["not_native", "unbound", "effectful", "failed", "permission", "approval", "missing_focus"],
)
def test_capture_does_not_mint_an_unbound_or_failed_read_capability(bad):
    owner, request, raw = _channel(), _request(), _raw()
    if bad == "unbound":
        request["binding"] = "public-forged-marker"
    elif bad == "effectful":
        request["tool"] = "desktop.safe_type_text"
    elif bad == "failed":
        raw["ok"] = False
    elif bad == "permission":
        raw["permission_error"] = True
    elif bad == "approval":
        raw["approval_required"] = True
    elif bad == "missing_focus":
        raw["data"].pop("focused_element")
    assert owner.capture(request, raw, local_broker_executed=bad != "not_native") is None


def test_read_channel_cannot_advertise_mutation_tools_or_unbounded_recipient_tree():
    with pytest.raises(ValueError, match="private_native_observation_tools_invalid"):
        PrivateNativeObservationChannel(
            lambda _request: True, authority=object(), tools=frozenset({"desktop.safe_type_text"})
        )
    owner, request, raw = _channel(), _request(), _raw()
    raw["data"]["elements"] *= 81
    token = owner.capture(request, raw, local_broker_executed=True)
    data = owner.consume(token, request, run_id="run")["data"]
    assert len(data["elements"]) == 80
    assert data["truncated"] is True
