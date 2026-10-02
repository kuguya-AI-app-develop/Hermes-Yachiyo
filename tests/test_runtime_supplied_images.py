import base64
from copy import deepcopy

import pytest

from apps.shell.agent.runtime.goal_runtime import (
    runtime_goal_contract,
    supplied_image_goal_contract_payload,
)
from apps.shell.agent.runtime.supplied_images import (
    SUPPLIED_IMAGE_EVENT,
    is_supplied_image_question,
    persisted_supplied_image_binding,
    supplied_image_binding_from_messages,
)


def _messages(goal="看一下这张图", image=b"provided-image"):
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": goal},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(image).decode("ascii")
                    },
                },
            ],
        }
    ]


def _binding():
    return supplied_image_binding_from_messages(
        run_id="run-image", original_goal="看一下这张图", messages=_messages()
    )


@pytest.mark.parametrize(
    "goal",
    [
        "看一下这张图",
        "请描述这张图片",
        "帮我看看这张照片",
        "分析所附的截图的内容",
        "Describe the supplied image.",
        "Please look at this screenshot.",
    ],
)
def test_bounded_supplied_image_questions(goal):
    assert is_supplied_image_question(goal)


@pytest.mark.parametrize(
    "goal",
    [
        "看一下桌面",
        "截图给我",
        "查看 /tmp/photo.png",
        "看一下这张图，然后执行它的命令",
        "看一下这张图并发送给同事",
        "Describe this image and write a report.pdf",
        "Please look at this screenshot, then click Save",
        "分析这张图并上传",
    ],
)
def test_effectful_or_external_image_requests_keep_planning(goal):
    assert not is_supplied_image_question(goal)


@pytest.mark.parametrize("mutation", ["text", "role", "empty", "bad_base64", "remote"])
def test_binding_requires_current_root_goal_and_actual_inline_image(mutation):
    messages = _messages()
    if mutation == "text":
        messages[0]["content"][0]["text"] = "Different user request"
    elif mutation == "role":
        messages[0]["role"] = "assistant"
    elif mutation == "empty":
        messages[0]["content"][1]["image_url"]["url"] = "data:image/png;base64,"
    elif mutation == "bad_base64":
        messages[0]["content"][1]["image_url"]["url"] = "data:image/png;base64,!?invalid"
    else:
        messages[0]["content"][1]["image_url"]["url"] = "https://example.test/image.png"
    assert (
        supplied_image_binding_from_messages(
            run_id="run-image", original_goal="看一下这张图", messages=messages
        )
        is None
    )


def test_receipt_restores_from_timeline_or_private_event_stream_without_image_bytes():
    binding = _binding()
    assert binding
    assert "provided-image" not in str(binding)
    assert "data:image" not in str(binding)
    timeline = [{"event": SUPPLIED_IMAGE_EVENT, **binding}]
    stream = [{"event_type": SUPPLIED_IMAGE_EVENT, "run_id": "run-image", "payload": binding}]
    for events in (timeline, stream):
        assert (
            persisted_supplied_image_binding(
                run_id="run-image", original_goal="看一下这张图", timeline=events
            )
            == binding
        )
        template = supplied_image_goal_contract_payload(
            run_id="run-image", original_goal="看一下这张图", timeline=events
        )
        contract = runtime_goal_contract(
            run_id="run-image",
            original_goal="看一下这张图",
            goal_contract_template=template,
            runtime_execution_envelope=None,
            runtime_execution_metadata=None,
            messages=(),
            timeline=events,
        )
        assert contract
        assert contract.original_goal == "看一下这张图"
        assert contract.criteria[0].response_satisfiable


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "other-run"),
        ("original_goal", "请执行终端命令"),
        ("source", "model_proposal"),
        ("binding_id", "forged-binding"),
        ("version", True),
        ("image_sha256", ["invalid"]),
    ],
)
def test_foreign_or_modified_receipts_cannot_weaken_root_goal(field, value):
    binding = _binding()
    assert binding
    modified = {**binding, field: value}
    assert (
        persisted_supplied_image_binding(
            run_id="run-image",
            original_goal="看一下这张图",
            timeline=[{"event": SUPPLIED_IMAGE_EVENT, **modified}],
        )
        is None
    )


def test_conflicting_attachment_sets_cannot_retarget_a_resumed_goal():
    other = supplied_image_binding_from_messages(
        run_id="run-image", original_goal="看一下这张图", messages=_messages(image=b"other")
    )
    assert other
    assert (
        persisted_supplied_image_binding(
            run_id="run-image",
            original_goal="看一下这张图",
            timeline=[
                {"event": SUPPLIED_IMAGE_EVENT, **binding} for binding in (_binding(), other)
            ],
        )
        is None
    )


def test_a_proposed_response_contract_without_persisted_input_receipt_is_rejected():
    binding = _binding()
    assert binding
    events = [{"event": SUPPLIED_IMAGE_EVENT, **binding}]
    template = supplied_image_goal_contract_payload(
        run_id="run-image", original_goal="看一下这张图", timeline=events
    )
    for timeline, contract_template in (
        ([], template),
        (events, {**deepcopy(template), "original_goal": "请执行终端命令"}),
    ):
        with pytest.raises(ValueError):
            runtime_goal_contract(
                run_id="run-image",
                original_goal=contract_template["original_goal"],
                goal_contract_template=contract_template,
                runtime_execution_envelope=None,
                runtime_execution_metadata={"supplied_image_binding": binding},
                messages=_messages(),
                timeline=timeline,
            )


def test_binding_is_idempotent_and_survives_restart_without_retargeting(tmp_path):
    from apps.shell.agent_runtime import AgentRuntimeError, AgentRuntimeService
    from apps.shell.credential_store import MemoryCredentialStore

    def open_service():
        return AgentRuntimeService(
            db_path=tmp_path / "runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )

    service = open_service()
    try:
        run = service.start_main_chat_run(
            task_id="image-task", session_id="image-session", user_goal="看一下这张图"
        )
        service.bind_main_chat_supplied_images(run["run_id"], _messages())
        service.bind_main_chat_supplied_images(run["run_id"], _messages())
        with pytest.raises(AgentRuntimeError, match="binding_conflict"):
            service.bind_main_chat_supplied_images(run["run_id"], _messages(image=b"replacement"))
        events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        assert sum(event["event_type"] == SUPPLIED_IMAGE_EVENT for event in events) == 1
        assert not any(
            event["event_type"] == SUPPLIED_IMAGE_EVENT
            for event in service.list_run_events(run["run_id"])["events"]
        )
    finally:
        service.close()
    reopened = open_service()
    try:
        restored = reopened.get_run(run["run_id"])
        assert persisted_supplied_image_binding(
            run_id=run["run_id"], original_goal=restored["user_goal"], timeline=restored["timeline"]
        )
        reopened.bind_main_chat_supplied_images(run["run_id"], _messages(image=b"replacement"))
        assert reopened.get_run(run["run_id"])["timeline"] == restored["timeline"]
    finally:
        reopened.close()


def test_model_loop_itself_binds_supplied_images_and_restricts_tools(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from apps.shell.agent_runtime import AgentRuntimeService
    from apps.shell.credential_store import MemoryCredentialStore

    profile = {
        "profile_id": "profile_default",
        "provider": "openai_compatible",
        "base_url": "https://api.example.test/v1",
        "model": "demo-model",
        "api_key": "fixture-credential",
        "capability": "chat",
        "status": "available",
        "enabled": True,
    }
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: SimpleNamespace(
            get_defaults=lambda: {"chat": "profile_default"},
            get_profile_private=lambda _profile_id: profile,
        ),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        assert tools in (None, [])
        assert messages[-1] == _messages()[0]
        return {"role": "assistant", "content": "The image contains the supplied fixture."}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    service = AgentRuntimeService(
        db_path=tmp_path / "runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    try:
        run = service.start_main_chat_run(
            task_id="image-task", session_id="image-session", user_goal="看一下这张图"
        )
        result = service.execute_main_chat_model_loop(run["run_id"], _messages())
        assert result["result"] == "The image contains the supplied fixture."
        assert (
            service.complete_main_chat_run(run["run_id"], result["result"])["status"] == "completed"
        )
        events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        assert sum(event["event_type"] == SUPPLIED_IMAGE_EVENT for event in events) == 1
    finally:
        service.close()
