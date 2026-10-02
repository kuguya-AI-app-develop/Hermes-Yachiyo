"""Runtime integration helpers for immutable goal contracts and evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from apps.shell.agent.runtime.desktop_execution_providers import (
    LOCAL_DESKTOP_PROVIDER_ID,
    LOCAL_DESKTOP_PROVIDER_KIND,
)
from apps.shell.agent.runtime.dispatch_semantics import (
    exact_native_dispatch_receipt_matches,
    intrinsic_native_postcondition_state,
    intrinsic_native_postcondition_target_matches,
    is_semantic_safe_key,
)
from apps.shell.agent.runtime.events import (
    RUNTIME_EXECUTION_PROVENANCE_KEY,
    RUNTIME_EXECUTION_PROVENANCE_VERSION,
    RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
)
from apps.shell.agent.runtime.goal_contract import (
    BoundedSubgoal,
    GoalAssessment,
    GoalContract,
    GoalCoordinator,
    GoalCriterion,
)
from apps.shell.agent.runtime.input_bindings import (
    validate_workspace_file_resolution_receipt,
)
from apps.shell.agent.runtime.tool_capabilities import capability_ids_for_tool
from apps.shell.agent.runtime.process_receipts import verified_terminal_process_observation
from apps.shell.agent.runtime.supplied_images import persisted_supplied_image_binding
from apps.shell.agent.runtime.tool_outcomes import (
    OutcomeStatus,
    ToolOutcome,
    VerificationStatus,
    canonical_media_playback_state,
    from_tool_result,
    media_track_change_verified,
)
from apps.shell.agent.runtime.verification_receipts import (
    EXACT_FILE_CONTENT_PRESENT_PREDICATE,
    EXACT_FILE_READBACK_VERIFIER_TOOLS,
    RUNTIME_SEMANTIC_ARTIFACT_VERIFIER_AUTHORITY,
    SEMANTIC_ARTIFACT_ADEQUACY_PREDICATE,
    SEMANTIC_ARTIFACT_ASSESSED_EVENT,
    declared_workspace_output_path,
    normalized_workspace_relative_path,
)

DELEGATED_WORKFLOW_RESPONSE_ONLY_GOAL = (
    "Respond with the result for this delegated workflow step."
)

_TERMINAL_TOOL_EVENTS = frozenset(
    {"agent.tool.call", "agent.tool.failed", "agent.tool.skipped"}
)


def runtime_goal_contract(
    *,
    run_id: str,
    original_goal: str | None = None,
    goal_contract_template: GoalContract | Mapping[str, Any] | None = None,
    runtime_execution_envelope: Mapping[str, Any] | None,
    runtime_execution_metadata: Mapping[str, Any] | None,
    messages: Sequence[Mapping[str, Any]],
    timeline: Sequence[Mapping[str, Any]],
) -> GoalContract | None:
    """Restore a persisted contract or bind the planner template to this run."""

    clean_run_id = str(run_id or "").strip()
    if not clean_run_id:
        return None
    # Kept only as a compatibility parameter; mutable conversation history is
    # not authority for the root objective.
    del messages
    immutable_original_goal = str(original_goal or "").strip()
    candidates = _persisted_goal_contract_payloads(
        timeline,
        run_id=clean_run_id,
    )
    candidates.extend(
        _explicit_goal_contract_template_payloads(goal_contract_template)
    )
    for container in (runtime_execution_envelope, runtime_execution_metadata):
        candidates.extend(_goal_contract_payloads_from_container(container))
    if candidates:
        restored: list[GoalContract] = []
        try:
            restored = [
                _strict_goal_contract_from_payload(candidate).bind_run(clean_run_id)
                for candidate in candidates
            ]
        except (TypeError, ValueError) as exc:
            raise ValueError("goal_contract_invalid") from exc
        canonical_payloads = {
            _canonical_goal_contract_payload(contract)
            for contract in restored
        }
        if len(canonical_payloads) != 1:
            raise ValueError("goal_contract_conflict")
        contract = restored[0]
        if (
            immutable_original_goal
            and contract.original_goal != immutable_original_goal
        ):
            raise ValueError("goal_contract_conflict: original_goal")
        if _response_only_contract_is_unsafe_for_goal(
            contract,
            supplied_image_bound=_contract_has_supplied_image_binding(contract, timeline),
        ):
            raise ValueError("goal_contract_invalid: response_only_nonconversation")
        return contract
    if not immutable_original_goal:
        return None
    if not _explicit_pure_conversation_goal(immutable_original_goal):
        raise ValueError("goal_contract_missing")
    contract_id = _stable_id(
        "goal-contract",
        clean_run_id,
        immutable_original_goal,
    )
    return GoalContract(
        contract_id=contract_id,
        run_id=clean_run_id,
        original_goal=immutable_original_goal,
        intent_kind="general",
        criteria=(
            GoalCriterion(
                criterion_id=_stable_id("goal-criterion", contract_id, "response"),
                description="Provide the response requested by the original user goal",
                response_satisfiable=True,
            ),
        ),
    )


def supplied_image_goal_contract_payload(
    *, run_id: str, original_goal: str, timeline: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    binding = persisted_supplied_image_binding(
        run_id=run_id, original_goal=original_goal, timeline=timeline
    )
    if binding is None:
        return {}
    contract_id = _stable_id("goal-contract", run_id, original_goal, binding["binding_id"])
    return GoalContract(
        contract_id=contract_id,
        run_id=run_id,
        original_goal=original_goal,
        intent_kind="general",
        criteria=(GoalCriterion(
            criterion_id=_stable_id("goal-criterion", contract_id, "supplied-image"),
            description="Describe the image already supplied with the original request",
            response_satisfiable=True,
            expected={"supplied_image_binding_id": binding["binding_id"]},
        ),),
    ).to_payload()


def _contract_has_supplied_image_binding(
    contract: GoalContract, timeline: Sequence[Mapping[str, Any]]
) -> bool:
    binding = persisted_supplied_image_binding(
        run_id=contract.run_id, original_goal=contract.original_goal, timeline=timeline
    )
    return bool(
        binding
        and len(contract.criteria) == 1
        and dict(contract.criteria[0].expected)
        == {"supplied_image_binding_id": binding["binding_id"]}
    )


def planned_goal_contract_payload(
    user_goal: str,
    *,
    allowed_tools: Iterable[str],
    planning_goal: str | None = None,
) -> dict[str, Any]:
    """Compile completion semantics only from this Run's immutable authority.

    ``planning_goal`` remains a compatibility-only execution hint.  It must
    never select, weaken, or replace completion criteria; callers use it for
    execution planning/context outside this authority compiler.
    """

    from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner

    clean_goal = str(user_goal or "").strip()
    if not clean_goal:
        return {}
    del planning_goal
    try:
        decision = RuntimePlanner().decision(
            clean_goal,
            allowed_tools=allowed_tools,
        )
        task_core = decision.plan.task_core
        goal_contract = task_core.goal_contract if task_core is not None else None
        if goal_contract is None:
            raise ValueError("planner returned no goal contract")
        payload = goal_contract.model_dump()
        if str(payload.get("original_goal") or "") != clean_goal:
            raise ValueError("planner changed the immutable original goal")
        if _response_only_contract_is_unsafe_for_goal(goal_contract):
            raise ValueError("planner weakened executable goal to response-only")
        return payload
    except (TypeError, ValueError) as exc:
        if _explicit_pure_conversation_goal(clean_goal):
            return _pure_conversation_goal_contract_payload(clean_goal)
        raise ValueError("goal_contract_compile_failed") from exc


def _explicit_exact_response_goal(user_goal: str) -> bool:
    """Recognize one bounded response payload without execution authority."""

    raw_text = str(user_goal or "")
    if (
        not raw_text
        or len(raw_text) > 120
        or "\n" in raw_text
        or "\r" in raw_text
    ):
        return False
    text = raw_text.strip()
    match = re.fullmatch(
        r"(?:(?:(?:please|can\s+you|could\s+you|would\s+you)\s+)?(?:"
        r"(?:reply|respond|answer)\s+with\s+(?:exactly|only|just)\s+|"
        r"(?:reply|respond|answer)\s+(?:only|just)\s+with\s+|"
        r"(?:return|output|say)\s+(?:exactly|only|just)\s+)|"
        r"(?:请)?(?:只|仅)(?:需|要)?(?:回复|回答|输出|说)"
        r"(?:以下|这句|这段|内容)?[：:\t ]*)"
        r"(?P<payload>.+)",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return False
    payload = match.group("payload").strip()
    if re.fullmatch(
        r"(?:\"[^\"\r\n]{1,100}\"|'[^'\r\n]{1,100}'|"
        r"`[^`\r\n]{1,100}`|“[^”\r\n]{1,100}”|‘[^’\r\n]{1,100}’)"
        r"[.!?！。？]*",
        payload,
    ):
        return True
    unquoted = re.fullmatch(
        r"(?P<body>[a-z0-9_./:+-]+(?:[\t ]+[a-z0-9_./:+-]+)*)"
        r"[!?！。？]*",
        payload,
        flags=re.IGNORECASE,
    )
    if unquoted is None:
        return False
    body = unquoted.group("body")
    if len(body) > 100:
        return False
    # Unquoted recipient/channel tails are ambiguous with effectful
    # communication. Require quoting for those payloads so they cannot turn a
    # Slack/email request into response-only work.
    if re.search(
        r"\b(?:slack|teams|discord|gmail|outlook|e-?mail)\b|"
        r"\b(?:to|for)\s+(?:the\s+)?[a-z0-9_]",
        body,
        flags=re.IGNORECASE,
    ):
        return False
    from apps.shell.yachiyo_agent.runtime_planner import (
        text_has_authorized_action_request,
    )

    return not text_has_authorized_action_request(body)


def _explicit_pure_conversation_goal(user_goal: str) -> bool:
    """Allow a narrow, auditable response-only fallback when planning is down."""

    text = " ".join(str(user_goal or "").strip().split()).lower()
    if not text or len(text) > 120:
        return False
    if text == DELEGATED_WORKFLOW_RESPONSE_ONLY_GOAL.lower():
        return True
    if re.fullmatch(
        r"(?:你好|您好|嗨|哈喽|早上好|下午好|晚上好|"
        r"谢谢|感谢|再见|聊聊天|陪我聊聊|"
        r"hello|hi|hey|good\s+(?:morning|afternoon|evening)|"
        r"thanks?|thank\s+you|bye|goodbye|let'?s\s+chat)"
        r"[!,.?！，。？~～]*",
        text,
        flags=re.IGNORECASE,
    ):
        return True
    # Exact-response instructions address this Chat assistant itself. Keep
    # their payload grammar separate from generic ``reply`` requests so
    # recipient/channel/action tails remain effectful.
    if _explicit_exact_response_goal(user_goal):
        return True
    # Workflow steps commonly ask the model to summarize or review context
    # that the runtime already supplies (upstream output, confirmed memory,
    # or the current conversation).  Those are genuine response-only goals,
    # even though the speech-act router intentionally treats verbs such as
    # ``summarize`` and ``review`` as actions.  Keep this carve-out narrower
    # than the general action grammar: any external source, application, or
    # side-effect verb still requires a planned capability and cannot be
    # satisfied by prose alone.
    if _explicit_contextual_advisory_goal(user_goal):
        return True
    # A mixed request can contain explanatory, quoted, conditional, or
    # negated clauses while still carrying an affirmative action elsewhere.
    # Never let those constraints weaken the root Goal into a response-only
    # contract when planning is unavailable.
    from apps.shell.yachiyo_agent.runtime_planner import (
        text_has_authorized_action_request,
    )

    if text_has_authorized_action_request(text):
        return False
    action = (
        r"(?:打开|启动|运行|执行|读取|查看|搜索|查找|播放|创建|新建|写入?|"
        r"保存|发送|回复|删除|移动|复制|整理|分析|生成|提醒|预约|关闭|"
        r"调整|设置|下载|上传|点击|输入|选择|截图|截屏|录屏|"
        r"\b(?:open|launch|run|execute|read|view|search|find|play|create|"
        r"write|save|send|reply|delete|move|copy|organize|analyse|analyze|"
        r"generate|remind|schedule|close|adjust|set|download|upload|click|"
        r"type|select|capture)\b)"
    )
    if re.search(
        rf"^(?:(?:你能|你可以|可以)\s*)?(?:请)?"
        rf"(?:解释|说明|告诉我|教我|演示|翻译)?\s*"
        rf"(?:为什么|为何|如何|怎么(?:样)?|怎样|什么是|我想知道).{{0,100}}{action}|"
        rf"^(?:please\s+)?(?:explain|describe|tell\s+me|show\s+me|teach\s+me|"
        rf"translate)?\s*(?:why|how(?:\s+do|\s+to|\s+can)|"
        rf"what\s+(?:is|does|happens?)).{{0,140}}{action}",
        text,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        rf"(?:不要|不用|不必|(?<!能)不能|不该|(?<!可)不可|无需|别|禁止|停止|切勿)"
        rf"[^。！？?!;\n]{{0,40}}{action}|"
        rf"\b(?:never|do\s+not|don't|cannot|can't|should\s+not|shouldn't|without)\b"
        rf"[^.!?;\n]{{0,56}}{action}",
        text,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        rf"(?:他说|她说|有人说|文档(?:说|写|写着)|例如|比如|示例|翻译|"
        rf"是什么意思).{{0,100}}[‘’“”'\"`]?{action}|"
        rf"\b(?:he|she|they|the\s+document)\s+(?:said|says)|"
        rf"\b(?:example|translate|meaning)\b.{{0,140}}{action}",
        text,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        rf"{action}.{{0,100}}(?:是什么意思|会发生什么|会怎样|如何工作|翻译|"
        rf"\bwhat\s+(?:does\s+it\s+mean|happens)|"
        rf"\bhow\s+does\s+it\s+work)",
        text,
        flags=re.IGNORECASE,
    ):
        return True
    return bool(
        re.fullmatch(
            r"(?:(?:please|can\s+you|could\s+you|would\s+you)\s+)?"
            r"(?:explain|describe|define|tell\s+me\s+about|what\s+is|why\s+(?:is|does|do|are)|"
            r"how\s+(?:does|do|is|are))\b.+|"
            r"(?:请)?(?:解释|说明|介绍|定义).+|"
            r".+(?:是什么|为什么|怎么理解)[？?]?",
            text,
            flags=re.IGNORECASE,
        )
    )


def _explicit_contextual_advisory_goal(user_goal: str) -> bool:
    """Recognize bounded model-only work over already supplied context."""

    text = " ".join(str(user_goal or "").strip().split())
    if not text or len(text) > 600:
        return False
    context_cue = re.search(
        r"\b(?:confirmed|upstream|previous\s+result|workflow\s+result|"
        r"provided\s+context|supplied\s+context|available\s+context|"
        r"conversation\s+context|current\s+conversation|above|following)\b|"
        r"已确认|上游|前一步(?:结果)?|工作流结果|提供的?上下文|给定上下文|"
        r"现有上下文|已有上下文|当前对话|上述|以下|全局目标|整条流程",
        text,
        flags=re.IGNORECASE,
    )
    if context_cue is None:
        return False
    advisory = re.search(
        r"\b(?:summari[sz]e|recap|condense|synthesi[sz]e|explain|describe|"
        r"review|assess|evaluate|compare|translate|outline|identify|"
        r"classify|recommend|propose)\b|"
        r"总结|汇总|概括|归纳|解释|说明|审查|评审|复盘|评估|比较|翻译|"
        r"整理|列出|列举|归类|提出建议|给出(?:实现)?方案|提供依据|最终汇报",
        text,
        flags=re.IGNORECASE,
    )
    if advisory is None:
        return False
    external_effect = re.search(
        r"(?:打开|启动|运行|执行|读取|查看|搜索|查找|播放|创建|新建|写入?|"
        r"保存|发送|发给|回复|删除|移动|复制|粘贴|生成|输出|提醒|预约|关闭|"
        r"调整|设置|下载|上传|安装|卸载|导入|导出|点击|输入|选择|截图|截屏|录屏)|"
        r"\b(?:open|run|execute|read|view|search|find|play|create|write|"
        r"save|send|reply|delete|move|copy|paste|generate|output|remind|"
        r"schedule|close|adjust|set|download|upload|install|uninstall|import|"
        r"export|click|type|select|capture)\b|"
        r"(?:^|[.!?;,]|\b(?:and|then)\b)\s*launch\b",
        text,
        flags=re.IGNORECASE,
    )
    if external_effect is not None:
        return False
    external_source = re.search(
        r"https?://|www\.|(?:^|\s)(?:~?/|\.{1,2}/)|"
        r"\b[^\s/\\]+\.(?:txt|md|pdf|docx?|xlsx?|csv|tsv|json|ya?ml|"
        r"html?|png|jpe?g|gif|svg|py|js|jsx|ts|tsx)\b|"
        r"\b(?:file|document|web\s*page|website|browser|window|screen|"
        r"clipboard|slack|teams|discord|gmail|outlook|e-?mail|calendar|"
        r"application|app)\b|"
        r"文件|文档|网页|网站|浏览器|窗口|屏幕|剪贴板|邮件|日历|应用|软件|目录",
        text,
        flags=re.IGNORECASE,
    )
    return external_source is None


def _response_only_contract_is_unsafe_for_goal(
    contract: GoalContract, *, supplied_image_bound: bool = False
) -> bool:
    """Reject prose-only completion for a goal that still needs discovery.

    A deterministic router may classify an underspecified action as `general`,
    but that uncertainty is a clarification/capability-discovery state, not
    permission for model prose to satisfy the task.  Only the narrow audited
    conversation grammar may own a response-only contract.
    """

    criteria = tuple(contract.criteria)
    response_only = bool(
        criteria
        and all(
            criterion.response_satisfiable
            and not criterion.effectful
            and not criterion.required_capabilities
            and not criterion.required_effects
            for criterion in criteria
        )
    )
    return bool(
        response_only
        and not _explicit_pure_conversation_goal(contract.original_goal)
        and not supplied_image_bound
    )


def _pure_conversation_goal_contract_payload(user_goal: str) -> dict[str, Any]:
    contract_id = _stable_id("goal-contract", "pure-conversation", user_goal)
    return GoalContract(
        contract_id=contract_id,
        original_goal=user_goal,
        intent_kind="conversation",
        criteria=(
            GoalCriterion(
                criterion_id=_stable_id(
                    "goal-criterion",
                    contract_id,
                    "response",
                ),
                description="Provide the conversational response requested by the user",
                response_satisfiable=True,
            ),
        ),
    ).to_payload()


def runtime_goal_assessment(
    contract: GoalContract,
    timeline: Sequence[Mapping[str, Any]],
) -> GoalAssessment:
    """Rebuild the evidence ledger from authoritative terminal tool events."""

    coordinator = GoalCoordinator()
    assessment = coordinator.initial(contract)
    serialized_subgoals: list[Mapping[str, Any]] = []
    opened_subgoals: dict[str, BoundedSubgoal] = {}
    opened_subgoal_lineages: dict[str, Mapping[str, Any]] = {}
    source_attempts: dict[str, dict[str, Any]] = {}
    approval_pause_by_call_id: dict[str, Mapping[str, Any]] = {}
    for raw_event in timeline:
        if not isinstance(raw_event, Mapping):
            continue
        event = _flatten_event(raw_event)
        event_type = str(event.get("event") or event.get("event_type") or "").strip()
        if event_type == "agent.delegation.children.verified":
            from .main_chat_delegation import completion_outcome, same_goal_evidence

            inherited_evidence = same_goal_evidence(contract, event, timeline)
            if inherited_evidence is not None:
                payload = assessment.to_payload()
                payload["evidence"].extend(inherited_evidence)
                assessment = coordinator.restore_assessment(contract, payload)
                continue
            delegated = completion_outcome(contract, event, timeline)
            if delegated is not None:
                outcome, observed, plan_id = delegated
                assessment = coordinator.record_tool_outcome(
                    contract, assessment, outcome, run_id=contract.run_id,
                    source_tool_call_id=f"delegation:{observed['delegation_binding_id']}",
                    source_step_id="group-multi_agent", plan_id=plan_id, observed=observed,
                )
            continue
        if event_type == "agent.goal.subgoal.opened":
            subgoal = _mapping_from_json(event.get("subgoal_json"))
            if subgoal is None:
                subgoal = event.get("subgoal")
            if isinstance(subgoal, Mapping):
                restored = _validated_opened_subgoal(
                    contract,
                    assessment,
                    event,
                    subgoal,
                    opened_subgoals=opened_subgoals,
                    source_attempts=source_attempts,
                )
                if restored is not None:
                    opened_subgoals[restored.subgoal_id] = restored
                    opened_subgoal_lineages[restored.subgoal_id] = event
                    serialized_subgoals.append(restored.to_payload())
                    restored_assessment = assessment.to_payload()
                    restored_assessment["subgoals"] = list(serialized_subgoals)
                    assessment = coordinator.restore_assessment(
                        contract,
                        restored_assessment,
                    )
            continue
        if event_type == SEMANTIC_ARTIFACT_ASSESSED_EVENT:
            assessment = _record_semantic_artifact_assessment(
                coordinator,
                contract,
                assessment,
                event,
            )
            continue
        if event_type not in _TERMINAL_TOOL_EVENTS:
            continue
        tool_name = str(event.get("tool") or event.get("detail") or "").strip()
        tool_call_id = str(event.get("tool_call_id") or "").strip()
        result = event.get("result")
        if not tool_name or not tool_call_id or not isinstance(result, Mapping):
            continue
        if not _event_run_matches_contract(event, contract):
            continue
        if _nonterminal_tool_pause_event(event, result):
            # An approval/action pause is a request lifecycle projection, not
            # the terminal fact for this tool-call generation.  It neither
            # records Goal evidence nor consumes the first-terminal winner.
            if _explicit_approval_pause_event(event, result):
                approval_pause_by_call_id.setdefault(tool_call_id, event)
            continue
        if _effectful_terminal_authority_required(
            contract,
            event,
            tool_name=tool_name,
            result=result,
        ):
            event_is_runtime_owned = _runtime_owned_terminal_event(
                event,
                result,
                run_id=contract.run_id,
                plan_id=str(event.get("plan_id") or "").strip(),
            )
            if not event_is_runtime_owned:
                event_is_runtime_owned = (
                    _runtime_owned_correlated_native_receipt(
                        event,
                        result,
                        contract=contract,
                        source_attempts=source_attempts,
                    )
                )
            if not event_is_runtime_owned:
                continue
        if tool_call_id in source_attempts:
            # One Runtime call id has one terminal winner across every tool
            # class. Exact compatibility projections are idempotent; a
            # conflicting later terminal fact is quarantined behind the same
            # first-winner rule and cannot rewrite Goal evidence.
            continue
        approval_pause = approval_pause_by_call_id.get(tool_call_id)
        if approval_pause is not None:
            if not _trusted_approval_terminal_after_pause(
                approval_pause,
                event,
                result,
                contract=contract,
            ):
                # Once a call was paused for approval/action, only the exact
                # executor-owned, provider-attested continuation may become
                # its first terminal winner. Public/model projections are
                # ignored and cannot manufacture completion evidence.
                continue
        capabilities = tuple(
            dict.fromkeys(
                (
                    *capability_ids_for_tool(tool_name),
                    str(event.get("capability_id") or "").strip(),
                )
            )
        )
        capabilities = tuple(value for value in capabilities if value)
        outcome = from_tool_result(tool_name, result, capabilities=capabilities)
        goal_event = _event_with_opened_subgoal_lineage(
            event,
            opened_subgoal_lineages=opened_subgoal_lineages,
        )
        eligible_criterion_ids = _eligible_criterion_ids_for_event(
            contract,
            goal_event,
            result=result,
            capabilities=capabilities,
            opened_subgoals=opened_subgoals,
            source_attempts=source_attempts,
        )
        process_observation = verified_terminal_process_observation(
            contract,
            goal_event,
            result,
            runtime_owned=_runtime_owned_terminal_event(
                goal_event,
                result,
                run_id=contract.run_id,
                plan_id=str(goal_event.get("plan_id") or ""),
            ),
            eligible_criterion_ids=tuple(eligible_criterion_ids),
        )
        if process_observation:
            outcome = replace(outcome, verification=VerificationStatus.VERIFIED)
        observed = _canonical_observed_payload(
            contract,
            goal_event,
            result,
            capabilities=capabilities,
            eligible_criterion_ids=eligible_criterion_ids,
            timeline=timeline,
        )
        observed.update(process_observation)
        recovery_root_target = _recovery_root_target(
            goal_event,
            source_attempts=source_attempts,
            eligible_criterion_ids=eligible_criterion_ids,
        )
        if recovery_root_target:
            observed["target"] = recovery_root_target
        evidence_source_event: Mapping[str, Any] = goal_event
        if str(event.get("source") or "").strip() == "runtime_internal_recovery":
            root_attempt = _trusted_recovery_root_source_attempt(
                contract,
                goal_event,
                retry_result=result,
                source_attempts=source_attempts,
                eligible_criterion_ids=eligible_criterion_ids,
            )
            root_event = (
                root_attempt.get("event")
                if isinstance(root_attempt, Mapping)
                else None
            )
            if isinstance(root_event, Mapping):
                evidence_source_event = root_event
        assessment = coordinator.record_tool_outcome(
            contract,
            assessment,
            outcome,
            run_id=contract.run_id,
            source_tool_call_id=tool_call_id,
            source_step_id=str(
                evidence_source_event.get("step_id")
                or evidence_source_event.get("planner_step_id")
                or ""
            ).strip(),
            plan_id=str(
                evidence_source_event.get("plan_id")
                or goal_event.get("root_plan_id")
                or ""
            ).strip(),
            observed=observed,
            eligible_criterion_ids=eligible_criterion_ids,
        )
        verifier_link = _trusted_verifier_link(
            goal_event,
            result,
            contract=contract,
        )
        if verifier_link is not None:
            source_call_id = verifier_link["source_tool_call_id"]
            source_attempt = source_attempts.get(source_call_id)
            if source_attempt is not None and _verifier_matches_source_attempt(
                verifier_link,
                source_attempt,
            ):
                assessment = _record_correlated_verifier_evidence(
                    coordinator,
                    contract,
                    assessment,
                    source_attempt=source_attempt,
                    verifier_event=goal_event,
                    verifier_result=result,
                    verifier_link=verifier_link,
                    verifier_tool_call_id=tool_call_id,
                )
        source_attempts[tool_call_id] = {
            "event": goal_event,
            "result": result,
            "tool": tool_name,
            "observed": observed,
            "eligible_criterion_ids": eligible_criterion_ids,
            "outcome": outcome,
        }
    if serialized_subgoals:
        restored_payload = assessment.to_payload()
        restored_payload["subgoals"] = [dict(item) for item in serialized_subgoals]
        try:
            assessment = coordinator.restore_assessment(contract, restored_payload)
        except (TypeError, ValueError):
            # Corrupt or cross-goal subgoal events cannot weaken the contract.
            pass
    return assessment


def pending_semantic_artifact_assessment_candidates(
    contract: GoalContract,
    timeline: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Return exact readbacks that still need semantic adequacy assessment."""

    assessment = runtime_goal_assessment(contract, timeline)
    semantic_identities = {
        _semantic_artifact_assessment_identity(evidence)
        for evidence in assessment.evidence
        if evidence.verification_predicate
        == SEMANTIC_ARTIFACT_ADEQUACY_PREDICATE
    }
    semantic_identities.discard(None)
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    exact_evidence = sorted(
        (
            evidence
            for evidence in assessment.evidence
            if evidence.verification_predicate
            == EXACT_FILE_CONTENT_PRESENT_PREDICATE
        ),
        key=_semantic_artifact_exact_evidence_preference,
    )
    for evidence in exact_evidence:
        criterion = contract.criterion(evidence.criterion_id)
        if (
            criterion is None
            or SEMANTIC_ARTIFACT_ADEQUACY_PREDICATE
            not in criterion.required_verification_predicates
            or evidence.verification_predicate
            != EXACT_FILE_CONTENT_PRESENT_PREDICATE
            or not evidence.verified
        ):
            continue
        lineage = _semantic_artifact_evidence_lineage(evidence)
        identity = _semantic_artifact_assessment_identity(evidence)
        if (
            lineage is None
            or identity is None
            or identity in semantic_identities
            or identity in seen
        ):
            continue
        content = evidence.observed.get("content")
        if not isinstance(content, str) or not content:
            continue
        seen.add(identity)
        candidates.append(
            {
                "contract_id": contract.contract_id,
                "criterion_id": criterion.criterion_id,
                "run_id": contract.run_id,
                "plan_id": evidence.plan_id,
                "source_tool_call_id": evidence.source_tool_call_id,
                "source_step_id": evidence.source_step_id,
                "structural_verifier_tool_call_id": (
                    evidence.verifier_tool_call_id
                ),
                "structural_verifier_step_id": evidence.verifier_step_id,
                "observed_path": str(
                    evidence.observed.get("observed_path") or ""
                ).strip(),
                "content_sha256": str(
                    evidence.observed.get("content_sha256") or ""
                ).strip().casefold(),
                "content_length": evidence.observed.get("content_length"),
                "content": content,
                "original_goal": contract.original_goal,
                "criterion_description": criterion.description,
                "criterion_expected": _plain_json(criterion.expected),
                "semantic_rubric_sha256": _semantic_artifact_rubric_sha256(
                    contract,
                    criterion,
                ),
                "verification_predicate": (
                    SEMANTIC_ARTIFACT_ADEQUACY_PREDICATE
                ),
            }
        )
    return tuple(candidates)


def _semantic_artifact_exact_evidence_preference(
    evidence: Any,
) -> tuple[int, str, str]:
    verifier_call_id = str(evidence.verifier_tool_call_id or "").strip()
    return (
        0
        if verifier_call_id.endswith(":exact-file-readback-receipt")
        else 1,
        verifier_call_id,
        str(evidence.evidence_id or "").strip(),
    )


def _semantic_artifact_assessment_identity(
    evidence: Any,
) -> tuple[Any, ...] | None:
    observed = evidence.observed
    observed_path = str(observed.get("observed_path") or "").strip()
    content_sha256 = str(
        observed.get("content_sha256") or ""
    ).strip().casefold()
    content_length = observed.get("content_length")
    identity = (
        evidence.contract_id,
        evidence.run_id,
        evidence.criterion_id,
        evidence.plan_id,
        evidence.source_tool_call_id,
        evidence.source_step_id,
        observed_path,
        content_sha256,
        content_length,
    )
    if (
        any(not value for value in identity[:-1])
        or normalized_workspace_relative_path(observed_path) != observed_path
        or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
        or isinstance(content_length, bool)
        or not isinstance(content_length, int)
        or content_length <= 0
    ):
        return None
    return identity


def _semantic_artifact_evidence_lineage(
    evidence: Any,
) -> tuple[Any, ...] | None:
    observed = evidence.observed
    observed_path = str(observed.get("observed_path") or "").strip()
    content_sha256 = str(
        observed.get("content_sha256") or ""
    ).strip().casefold()
    content_length = observed.get("content_length")
    content = observed.get("content")
    identity = (
        evidence.criterion_id,
        evidence.source_tool_call_id,
        evidence.source_step_id,
        evidence.verifier_tool_call_id,
        evidence.verifier_step_id,
        evidence.plan_id,
        observed_path,
        content_sha256,
        content_length,
    )
    if (
        any(not value for value in identity[:-1])
        or normalized_workspace_relative_path(observed_path) != observed_path
        or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
        or isinstance(content_length, bool)
        or not isinstance(content_length, int)
        or content_length <= 0
        or not isinstance(content, str)
        or not content
        or len(content.encode("utf-8")) != content_length
        or hashlib.sha256(content.encode("utf-8")).hexdigest()
        != content_sha256
    ):
        return None
    return identity


def _semantic_artifact_rubric_sha256(
    contract: GoalContract,
    criterion: GoalCriterion,
) -> str:
    payload = {
        "contract_id": contract.contract_id,
        "original_goal": contract.original_goal,
        "criterion_id": criterion.criterion_id,
        "criterion_description": criterion.description,
        "criterion_expected": _plain_json(criterion.expected),
        "required_verification_predicates": sorted(
            criterion.required_verification_predicates
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _effectful_terminal_authority_required(
    contract: GoalContract,
    event: Mapping[str, Any],
    *,
    tool_name: str,
    result: Mapping[str, Any],
) -> bool:
    """Keep the legacy read-only evidence path explicit and narrowly scoped."""

    if str(event.get("source") or "").strip() == "runtime_internal_recovery":
        return any(
            criterion.required and criterion.effectful
            for criterion in contract.criteria
        )
    result_data = (
        result.get("data") if isinstance(result.get("data"), Mapping) else {}
    )
    if any(
        source.get("verification_context_trusted") is True
        or source.get("verification_satisfied_by_native_receipt") is True
        for source in (result, result_data)
    ):
        return any(
            criterion.required and criterion.effectful
            for criterion in contract.criteria
        )
    capabilities = {
        *capability_ids_for_tool(tool_name),
        str(event.get("capability_id") or "").strip(),
    }
    capabilities.discard("")
    step_id = str(
        event.get("step_id") or event.get("planner_step_id") or ""
    ).strip()
    return any(
        criterion.required
        and criterion.effectful
        and (
            set(criterion.required_capabilities).issubset(capabilities)
            or step_id in criterion.verifier_step_ids
        )
        for criterion in contract.criteria
    )


def _event_run_matches_contract(
    event: Mapping[str, Any],
    contract: GoalContract,
) -> bool:
    event_run_id = str(event.get("run_id") or "").strip()
    contract_run_id = str(contract.run_id or "").strip()
    return bool(contract_run_id and event_run_id == contract_run_id)


_NONTERMINAL_TOOL_PAUSE_STATUSES = frozenset(
    {
        "action_required",
        "approval_required",
        "awaiting_approval",
        "pending",
        "waiting_approval",
    }
)


def _nonterminal_tool_pause_event(
    event: Mapping[str, Any],
    result: Mapping[str, Any],
) -> bool:
    """Classify pre-execution approval/action projections as nonterminal."""

    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    for source in (event, result, data):
        if source.get("approval_required") is True:
            return True
        if str(source.get("status") or "").strip().lower() in (
            _NONTERMINAL_TOOL_PAUSE_STATUSES
        ):
            return True
    return False


def _explicit_approval_pause_event(
    event: Mapping[str, Any],
    result: Mapping[str, Any],
) -> bool:
    """Recognize only the executor's pre-execution approval projection."""

    return bool(
        str(event.get("event") or event.get("event_type") or "").strip()
        == "agent.tool.call"
        and result.get("ok") is False
        and result.get("approval_required") is True
        and str(result.get("status") or "").strip().lower()
        == "approval_required"
        and not _runtime_execution_provider_identity(result)
    )


_APPROVAL_RESOLUTION_REQUIRED_IDENTITY_KEYS = (
    "run_id",
    "plan_id",
    "step_id",
    "tool_call_id",
)

_APPROVAL_RESOLUTION_OPTIONAL_IDENTITY_KEYS = (
    "request_id",
    "decision_id",
    "tool_plan_id",
    "materialization_binding_id",
    "materialized_content_sha256",
)


def _trusted_approval_terminal_after_pause(
    pending_event: Mapping[str, Any],
    terminal_event: Mapping[str, Any],
    terminal_result: Mapping[str, Any],
    *,
    contract: GoalContract,
) -> bool:
    """Allow one exact approved execution to resolve a provisional pause."""

    pending_plan_id = str(pending_event.get("plan_id") or "").strip()
    terminal_plan_id = str(terminal_event.get("plan_id") or "").strip()
    if (
        terminal_event.get("approved") is not True
        or not _runtime_executor_terminal_identity(
            pending_event,
            run_id=contract.run_id,
            plan_id=pending_plan_id,
        )
        or not _runtime_owned_terminal_event(
            terminal_event,
            terminal_result,
            run_id=contract.run_id,
            plan_id=terminal_plan_id,
        )
    ):
        return False
    pending_tool = str(
        pending_event.get("tool") or pending_event.get("detail") or ""
    ).strip()
    terminal_tool = str(
        terminal_event.get("tool") or terminal_event.get("detail") or ""
    ).strip()
    if not pending_tool or pending_tool != terminal_tool:
        return False
    for key in _APPROVAL_RESOLUTION_REQUIRED_IDENTITY_KEYS:
        pending_value = str(pending_event.get(key) or "").strip()
        terminal_value = str(terminal_event.get(key) or "").strip()
        if not pending_value or terminal_value != pending_value:
            return False
    for key in _APPROVAL_RESOLUTION_OPTIONAL_IDENTITY_KEYS:
        pending_value = str(pending_event.get(key) or "").strip()
        if pending_value and str(terminal_event.get(key) or "").strip() != pending_value:
            return False
    return True


_SUBGOAL_LINEAGE_KEYS = (
    "root_source_tool_call_id",
    "root_source_step_id",
    "root_verifier_step_id",
    "root_plan_id",
    "root_provider_kind",
    "root_provider_id",
    "recovery_origin_tool_call_id",
)


def _event_with_opened_subgoal_lineage(
    event: Mapping[str, Any],
    *,
    opened_subgoal_lineages: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    merged = dict(event)
    subgoal_id = str(event.get("goal_subgoal_id") or "").strip()
    opened = opened_subgoal_lineages.get(subgoal_id)
    if not subgoal_id or not isinstance(opened, Mapping):
        return merged
    if (
        str(opened.get("source") or "").strip() != "runtime_goal_coordinator"
        or str(opened.get("actor") or "").strip() != "native_runtime"
        or str(opened.get("execution_authority") or "").strip()
        != "runtime_goal_coordinator"
        or not str(opened.get("run_id") or "").strip()
        or str(opened.get("run_id") or "").strip()
        != str(event.get("run_id") or "").strip()
    ):
        merged["recovery_context_trusted"] = False
        return merged
    for key in _SUBGOAL_LINEAGE_KEYS:
        opened_value = str(opened.get(key) or "").strip()
        event_value = str(event.get(key) or "").strip()
        if opened_value and event_value and event_value != opened_value:
            merged["recovery_context_trusted"] = False
            return merged
        if opened_value:
            merged[key] = opened_value
    return merged


def _validated_opened_subgoal(
    contract: GoalContract,
    assessment: GoalAssessment,
    event: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    opened_subgoals: Mapping[str, BoundedSubgoal],
    source_attempts: Mapping[str, Mapping[str, Any]],
) -> BoundedSubgoal | None:
    """Accept only the exact Runtime-opened child of an earlier root attempt."""

    if (
        not _event_run_matches_contract(event, contract)
        or str(event.get("status") or "").strip() != "opened"
        or str(event.get("visibility") or "").strip() != "internal"
    ):
        return None
    try:
        subgoal = BoundedSubgoal.from_payload(payload)
    except (TypeError, ValueError):
        return None
    if subgoal.subgoal_id in opened_subgoals:
        return None
    for event_key, expected in (
        ("contract_id", subgoal.contract_id),
        ("criterion_id", subgoal.criterion_id),
        ("source_tool_call_id", subgoal.source_tool_call_id),
    ):
        if str(event.get(event_key) or "").strip() != expected:
            return None
    source_attempt = source_attempts.get(subgoal.source_tool_call_id)
    source_outcome = (
        source_attempt.get("outcome")
        if isinstance(source_attempt, Mapping)
        else None
    )
    criterion = contract.criterion(subgoal.criterion_id)
    normal_failed_source = bool(
        isinstance(source_outcome, ToolOutcome)
        and source_outcome.status in {OutcomeStatus.FAILED, OutcomeStatus.PARTIAL}
        and source_outcome.retryable
        and subgoal.criterion_id
        in tuple(source_attempt.get("eligible_criterion_ids") or ())
    )
    declared_verifier_failure = bool(
        criterion is not None
        and isinstance(source_attempt, Mapping)
        and _declared_verifier_failure_recovery_lineage(
            contract,
            criterion,
            event,
            source_attempt=source_attempt,
            source_attempts=source_attempts,
        )
    )
    if (
        not normal_failed_source
        and not declared_verifier_failure
    ):
        return None
    _, expected = GoalCoordinator().open_subgoal(
        contract,
        assessment,
        criterion_id=subgoal.criterion_id,
        action=subgoal.action,
        description=subgoal.description,
        source_tool_call_id=subgoal.source_tool_call_id,
    )
    return (
        subgoal
        if expected is not None and expected.to_payload() == subgoal.to_payload()
        else None
    )


def _declared_verifier_failure_recovery_lineage(
    contract: GoalContract,
    criterion: GoalCriterion,
    lineage_event: Mapping[str, Any],
    *,
    source_attempt: Mapping[str, Any],
    source_attempts: Mapping[str, Mapping[str, Any]],
) -> bool:
    source_outcome = source_attempt.get("outcome")
    source_event = source_attempt.get("event")
    source_result = source_attempt.get("result")
    if (
        not isinstance(source_outcome, ToolOutcome)
        or source_outcome.status not in {OutcomeStatus.FAILED, OutcomeStatus.PARTIAL}
        or not isinstance(source_event, Mapping)
        or not isinstance(source_result, Mapping)
    ):
        return False
    failed_verifier_step_id = str(
        source_event.get("step_id") or source_event.get("planner_step_id") or ""
    ).strip()
    root_source_tool_call_id = str(
        lineage_event.get("root_source_tool_call_id") or ""
    ).strip()
    root_source_step_id = str(
        lineage_event.get("root_source_step_id") or ""
    ).strip()
    root_verifier_step_id = str(
        lineage_event.get("root_verifier_step_id") or ""
    ).strip()
    root_plan_id = str(lineage_event.get("root_plan_id") or "").strip()
    root_attempt = source_attempts.get(root_source_tool_call_id)
    root_event = root_attempt.get("event") if isinstance(root_attempt, Mapping) else None
    root_outcome = (
        root_attempt.get("outcome") if isinstance(root_attempt, Mapping) else None
    )
    root_result = (
        root_attempt.get("result") if isinstance(root_attempt, Mapping) else None
    )
    if (
        failed_verifier_step_id not in criterion.verifier_step_ids
        or root_verifier_step_id != failed_verifier_step_id
        or root_source_step_id not in criterion.source_step_ids
        or not root_source_tool_call_id
        or not root_plan_id
        or not isinstance(root_event, Mapping)
        or not isinstance(root_result, Mapping)
        or not isinstance(root_outcome, ToolOutcome)
        or root_outcome.status is not OutcomeStatus.SUCCESS
        or criterion.criterion_id
        not in tuple(root_attempt.get("eligible_criterion_ids") or ())
        or str(
            root_event.get("step_id") or root_event.get("planner_step_id") or ""
        ).strip()
        != root_source_step_id
    ):
        return False
    if not _runtime_owned_terminal_event(
        source_event,
        source_result,
        run_id=contract.run_id,
        plan_id=root_plan_id,
    ) or not _runtime_owned_terminal_event(
        root_event,
        root_result,
        run_id=contract.run_id,
        plan_id=root_plan_id,
    ):
        return False
    source_provider = _runtime_execution_provider_identity(source_result)
    root_provider = _runtime_execution_provider_identity(root_result)
    return bool(source_provider and source_provider == root_provider)


def _eligible_criterion_ids_for_event(
    contract: GoalContract,
    event: Mapping[str, Any],
    *,
    result: Mapping[str, Any],
    capabilities: Iterable[str],
    opened_subgoals: Mapping[str, BoundedSubgoal],
    source_attempts: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    capability_set = set(capabilities)
    is_recovery = bool(
        str(event.get("source") or "").strip() == "runtime_internal_recovery"
        or str(event.get("recovery_link_kind") or "").strip()
        or str(event.get("goal_subgoal_id") or "").strip()
    )
    eligible: list[str] = []
    if not is_recovery:
        step_id = str(
            event.get("step_id") or event.get("planner_step_id") or ""
        ).strip()
        lacks_effectful_completion_authority = bool(
            event.get("observation_only") is True
            or event.get("goal_completion_authority") is False
        )
        eligible.extend(
            criterion.criterion_id
            for criterion in contract.criteria
            if set(criterion.required_capabilities).issubset(capability_set)
            and (not criterion.source_step_ids or step_id in criterion.source_step_ids)
            and not (
                criterion.effectful and lacks_effectful_completion_authority
            )
        )

    recovery_criterion_id = _recovery_bound_criterion_id(
        contract,
        event,
        retry_result=result,
        capability_set=capability_set,
        opened_subgoals=opened_subgoals,
        source_attempts=source_attempts,
    )
    recovery_criterion = (
        contract.criterion(recovery_criterion_id)
        if recovery_criterion_id
        else None
    )
    if recovery_criterion_id and not (
        recovery_criterion is not None
        and recovery_criterion.effectful
        and (
            event.get("observation_only") is True
            or event.get("goal_completion_authority") is False
        )
    ):
        eligible.append(recovery_criterion_id)
    return tuple(dict.fromkeys(eligible))


def _recovery_bound_criterion_id(
    contract: GoalContract,
    event: Mapping[str, Any],
    *,
    retry_result: Mapping[str, Any],
    capability_set: set[str],
    opened_subgoals: Mapping[str, BoundedSubgoal],
    source_attempts: Mapping[str, Mapping[str, Any]],
) -> str:
    if (
        str(event.get("source") or "").strip() != "runtime_internal_recovery"
        or str(event.get("recovery_link_kind") or "").strip()
        != "coordinator_action"
        or event.get("recovery_context_trusted") is not True
        or event.get("root_goal_unchanged") is not True
        or str(event.get("goal_contract_id") or "").strip()
        != contract.contract_id
    ):
        return ""
    criterion_id = str(event.get("goal_criterion_id") or "").strip()
    subgoal_id = str(event.get("goal_subgoal_id") or "").strip()
    source_call_id = str(event.get("source_tool_call_id") or "").strip()
    recovery_action = str(event.get("recovery_action") or "").strip()
    recovery_scope_id = str(event.get("recovery_scope_id") or "").strip()
    if (
        not criterion_id
        or not subgoal_id
        or not source_call_id
        or not recovery_action
        or not recovery_scope_id
        or str(event.get("replan_recovery_identity") or "").strip()
        != recovery_scope_id
    ):
        return ""
    criterion = contract.criterion(criterion_id)
    subgoal = opened_subgoals.get(subgoal_id)
    source_attempt = source_attempts.get(source_call_id)
    source_attempt_is_root = bool(
        isinstance(source_attempt, Mapping)
        and criterion_id
        in tuple(source_attempt.get("eligible_criterion_ids") or ())
    )
    recovery_source_is_declared_verifier = bool(
        criterion is not None
        and isinstance(source_attempt, Mapping)
        and _declared_verifier_failure_recovery_lineage(
            contract,
            criterion,
            event,
            source_attempt=source_attempt,
            source_attempts=source_attempts,
        )
    )
    if (
        criterion is None
        or subgoal is None
        or source_attempt is None
        or subgoal.contract_id != contract.contract_id
        or subgoal.criterion_id != criterion_id
        or subgoal.source_tool_call_id != source_call_id
        or subgoal.action != recovery_action
        or (not source_attempt_is_root and not recovery_source_is_declared_verifier)
        or not set(criterion.required_capabilities).issubset(capability_set)
    ):
        return ""
    root_plan_id = str(
        event.get("root_plan_id") or event.get("plan_id") or ""
    ).strip()
    if not _runtime_owned_terminal_event(
        event,
        retry_result,
        run_id=contract.run_id,
        plan_id=root_plan_id,
    ):
        return ""
    if recovery_source_is_declared_verifier:
        expected_provider = {
            "provider_kind": str(event.get("root_provider_kind") or "").strip(),
            "provider_id": str(event.get("root_provider_id") or "").strip(),
        }
        if (
            not all(expected_provider.values())
            or _runtime_execution_provider_identity(retry_result)
            != expected_provider
        ):
            return ""
    if source_attempt_is_root and not _trusted_direct_recovery_root_lineage(
        contract,
        criterion,
        event,
        retry_result=retry_result,
        source_attempt=source_attempt,
    ):
        return ""
    source_tool = str(source_attempt.get("tool") or "").strip()
    event_tool = str(event.get("tool") or event.get("detail") or "").strip()
    if (
        not source_tool
        or str(event.get("recovery_source_tool") or "").strip() != source_tool
        or str(event.get("recovery_suggested_tool") or "").strip()
        != event_tool
    ):
        return ""
    return criterion_id


def _trusted_recovery_root_source_attempt(
    contract: GoalContract,
    event: Mapping[str, Any],
    *,
    retry_result: Mapping[str, Any],
    source_attempts: Mapping[str, Mapping[str, Any]],
    eligible_criterion_ids: Iterable[str],
) -> Mapping[str, Any] | None:
    criterion_ids = tuple(eligible_criterion_ids)
    if len(criterion_ids) != 1:
        return None
    criterion = contract.criterion(criterion_ids[0])
    failed_source_attempt = source_attempts.get(
        str(event.get("source_tool_call_id") or "").strip()
    )
    if (
        criterion is not None
        and isinstance(failed_source_attempt, Mapping)
        and _trusted_direct_recovery_root_lineage(
            contract,
            criterion,
            event,
            retry_result=retry_result,
            source_attempt=failed_source_attempt,
        )
    ):
        return failed_source_attempt
    if (
        criterion is None
        or not isinstance(failed_source_attempt, Mapping)
        or not _declared_verifier_failure_recovery_lineage(
            contract,
            criterion,
            event,
            source_attempt=failed_source_attempt,
            source_attempts=source_attempts,
        )
    ):
        return None
    return source_attempts.get(
        str(event.get("root_source_tool_call_id") or "").strip()
    )


def _trusted_direct_recovery_root_lineage(
    contract: GoalContract,
    criterion: GoalCriterion,
    event: Mapping[str, Any],
    *,
    retry_result: Mapping[str, Any],
    source_attempt: Mapping[str, Any],
) -> bool:
    """Bind a trusted direct retry back to its exact planner source fact."""

    source_event = source_attempt.get("event")
    source_result = source_attempt.get("result")
    source_outcome = source_attempt.get("outcome")
    if (
        not isinstance(source_event, Mapping)
        or not isinstance(source_result, Mapping)
        or not isinstance(source_outcome, ToolOutcome)
        or source_outcome.status not in {OutcomeStatus.FAILED, OutcomeStatus.PARTIAL}
        or criterion.criterion_id
        not in tuple(source_attempt.get("eligible_criterion_ids") or ())
    ):
        return False
    source_call_id = str(source_event.get("tool_call_id") or "").strip()
    source_step_id = str(
        source_event.get("step_id") or source_event.get("planner_step_id") or ""
    ).strip()
    source_plan_id = str(source_event.get("plan_id") or "").strip()
    source_tool = str(source_attempt.get("tool") or "").strip()
    recovery_scope_id = str(event.get("recovery_scope_id") or "").strip()
    expected_scope_id = (
        "tool-attempt:"
        + hashlib.sha256(
            f"{source_tool}\0{source_call_id}".encode("utf-8")
        ).hexdigest()[:24]
        if source_tool and source_call_id
        else ""
    )
    if (
        not source_call_id
        or not source_step_id
        or source_step_id not in criterion.source_step_ids
        or not source_plan_id
        or str(event.get("source_tool_call_id") or "").strip() != source_call_id
        or str(event.get("source_step_id") or "").strip() != source_step_id
        or str(event.get("plan_id") or "").strip() != source_plan_id
        or not expected_scope_id
        or recovery_scope_id != expected_scope_id
    ):
        return False
    if not _runtime_owned_terminal_event(
        source_event,
        source_result,
        run_id=contract.run_id,
        plan_id=source_plan_id,
    ) or not _runtime_owned_terminal_event(
        event,
        retry_result,
        run_id=contract.run_id,
        plan_id=source_plan_id,
    ):
        return False
    source_provider = _runtime_execution_provider_identity(source_result)
    retry_provider = _runtime_execution_provider_identity(retry_result)
    return bool(source_provider and retry_provider == source_provider)


def _recovery_root_target(
    event: Mapping[str, Any],
    *,
    source_attempts: Mapping[str, Mapping[str, Any]],
    eligible_criterion_ids: Iterable[str],
) -> dict[str, Any]:
    """Keep the immutable semantic target when a trusted subgoal retries an alias."""

    if (
        not tuple(eligible_criterion_ids)
        or str(event.get("source") or "").strip() != "runtime_internal_recovery"
    ):
        return {}
    source_call_id = str(event.get("source_tool_call_id") or "").strip()
    source_attempt = source_attempts.get(source_call_id)
    source_observed = (
        source_attempt.get("observed")
        if isinstance(source_attempt, Mapping)
        else None
    )
    target = (
        source_observed.get("target")
        if isinstance(source_observed, Mapping)
        else None
    )
    return dict(target) if isinstance(target, Mapping) and target else {}


def _trusted_verifier_link(
    event: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    contract: GoalContract,
) -> dict[str, str] | None:
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    sources = (result, data)
    if not any(source.get("postcondition_verified") is True for source in sources):
        return None
    private_context = any(
        source.get("verification_context_trusted") is True for source in sources
    )
    native_receipt = bool(
        str(event.get("source") or "").strip()
        == "runtime_native_postcondition_receipt"
        and any(
            source.get("verification_satisfied_by_native_receipt") is True
            for source in sources
        )
    )
    if not (private_context or native_receipt):
        return None

    def result_text(*keys: str) -> str:
        for source in sources:
            for key in keys:
                value = str(source.get(key) or "").strip()
                if value:
                    return value
        return ""

    def first_text(*keys: str) -> str:
        value = result_text(*keys)
        if value:
            return value
        for key in keys:
            value = str(event.get(key) or "").strip()
            if value:
                return value
        return ""

    trusted_text = result_text if private_context else first_text
    source_call_id = trusted_text("source_tool_call_id")
    if not source_call_id:
        return None
    trusted_run_id = trusted_text("verification_run_id", "run_id")
    if private_context and trusted_run_id != contract.run_id:
        return None
    predicate_kind = first_text("verification_predicate_kind", "predicate_kind")
    if private_context and predicate_kind not in {
        "app_window_present",
        "exact_typed_content_present",
        "exact_submit_dispatch_receipt",
        "exact_app_search_result_present",
        "exact_selected_full_text_copied",
        EXACT_FILE_CONTENT_PRESENT_PREDICATE,
    }:
        return None
    link = {
        "source_tool_call_id": source_call_id,
        "source_tool": trusted_text("source_tool"),
        "source_step_id": trusted_text("source_step_id"),
        "source_request_id": trusted_text("source_request_id"),
        "plan_id": trusted_text("verification_plan_id", "plan_id"),
        "provider_kind": trusted_text(
            "verification_provider_kind",
            "provider_kind",
        ),
        "provider_id": trusted_text("verification_provider_id", "provider_id"),
        "predicate_kind": predicate_kind or "native_postcondition_receipt",
        "verifier_tool": str(
            event.get("tool") or event.get("detail") or ""
        ).strip(),
    }
    if predicate_kind == EXACT_FILE_CONTENT_PRESENT_PREDICATE:
        # A native receipt's event envelope is Runtime-owned, but the exact
        # readback correlation is carried in the projected result too.  Make
        # that result lineage mandatory so a persisted/mutated cross-run
        # receipt cannot fall back to the otherwise-valid event run.
        receipt_run_id = result_text("verification_run_id", "run_id")
        if receipt_run_id != contract.run_id:
            return None
        if any(
            source.get(key) is True
            for source in sources
            for key in (
                "truncated",
                "content_truncated",
                "output_truncated",
                "decoding_lossy",
            )
        ):
            return None
        if (
            not any(source.get("truncated") is False for source in sources)
            or not any(source.get("decoding_lossy") is False for source in sources)
        ):
            return None
        content = result.get("content")
        exact_fields = {
            "observed_path": result_text("observed_path"),
            "read_path": result_text("path"),
            "content_sha256": result_text("content_sha256"),
            "content_length": result_text("content_length"),
            "size_bytes": result_text("size_bytes"),
            "content_bytes": result_text("content_bytes"),
            "verified_observed_state": result_text("verified_observed_state"),
            "content": content if isinstance(content, str) else "",
            "decision_id": result_text("decision_id"),
            "tool_plan_id": result_text("tool_plan_id"),
        }
        if (
            link["verifier_tool"] not in EXACT_FILE_READBACK_VERIFIER_TOOLS
            or not exact_fields["content"]
            or any(
                not exact_fields[key]
                for key in (
                    "observed_path",
                    "read_path",
                    "content_sha256",
                    "content_length",
                    "size_bytes",
                    "content_bytes",
                    "verified_observed_state",
                )
            )
        ):
            return None
        link.update(exact_fields)
    if private_context and any(
        not link[key]
        for key in ("source_tool", "source_step_id", "provider_kind", "provider_id")
    ):
        return None
    return link


def _verifier_matches_source_attempt(
    verifier_link: Mapping[str, str],
    source_attempt: Mapping[str, Any],
) -> bool:
    event = source_attempt.get("event")
    result = source_attempt.get("result")
    if not isinstance(event, Mapping) or not isinstance(result, Mapping):
        return False
    source_tool = str(source_attempt.get("tool") or "").strip()
    claimed_tool = str(verifier_link.get("source_tool") or "").strip()
    if claimed_tool and claimed_tool != source_tool:
        return False
    predicate_kind = str(verifier_link.get("predicate_kind") or "").strip()
    if predicate_kind == "app_window_present" and source_tool not in {
        "app.open",
        "app.show",
        "desktop.open_app",
    }:
        return False
    if predicate_kind == "exact_typed_content_present" and source_tool not in {
        "app.focus_and_safe_type_text",
        "app.focus_and_type_into_ui_element",
        "app.open_and_safe_type_text",
        "app.open_and_type_into_ui_element",
        "desktop.safe_type_text",
        "desktop.type",
        "desktop.type_into_ui_element",
        "desktop.type_text",
    }:
        return False
    if (
        predicate_kind == "exact_app_search_result_present"
        and source_tool != "desktop.search_submit"
    ):
        return False
    if predicate_kind == "exact_selected_full_text_copied":
        source_input = (
            event.get("input_preview")
            if isinstance(event.get("input_preview"), Mapping) else {}
        )
        if source_tool != "desktop.safe_shortcut" or source_input.get("action") != "copy":
            return False
        if (
            str(event.get("step_id") or event.get("planner_step_id") or "")
            != "copy-selected-full-text"
        ):
            return False
    verifier_tool = str(verifier_link.get("verifier_tool") or "").strip()
    if (
        source_tool in {"terminal.run", "python.run"}
        and verifier_tool in EXACT_FILE_READBACK_VERIFIER_TOOLS
        and predicate_kind != EXACT_FILE_CONTENT_PRESENT_PREDICATE
    ):
        return False
    if predicate_kind == EXACT_FILE_CONTENT_PRESENT_PREDICATE:
        if source_tool not in {"terminal.run", "python.run"}:
            return False
        action_target = (
            event.get("action_target")
            if isinstance(event.get("action_target"), Mapping)
            else {}
        )
        output_path = declared_workspace_output_path(action_target)
        observed_path = normalized_workspace_relative_path(
            verifier_link.get("observed_path")
        )
        read_path = normalized_workspace_relative_path(
            verifier_link.get("read_path")
        )
        source_request_id = str(event.get("request_id") or "").strip()
        claimed_request_id = str(
            verifier_link.get("source_request_id") or ""
        ).strip()
        content = str(verifier_link.get("content") or "")
        content_bytes = content.encode("utf-8")
        content_sha256 = str(
            verifier_link.get("content_sha256") or ""
        ).strip().casefold()
        raw_content_length = str(
            verifier_link.get("content_length") or ""
        ).strip()
        raw_size_bytes = str(verifier_link.get("size_bytes") or "").strip()
        raw_observed_content_bytes = str(
            verifier_link.get("content_bytes") or ""
        ).strip()
        if (
            verifier_tool not in EXACT_FILE_READBACK_VERIFIER_TOOLS
            or not output_path
            or observed_path != output_path
            or read_path != output_path
            or not source_request_id
            or claimed_request_id != source_request_id
            or not content
            or not raw_content_length.isdecimal()
            or not raw_size_bytes.isdecimal()
            or not raw_observed_content_bytes.isdecimal()
            or int(raw_content_length) != len(content_bytes)
            or int(raw_size_bytes) != len(content_bytes)
            or int(raw_observed_content_bytes) != len(content_bytes)
            or content_sha256 != hashlib.sha256(content_bytes).hexdigest()
            or str(
                verifier_link.get("verified_observed_state") or ""
            ).strip()
            != "fulfilled"
            or result.get("ok") is not True
        ):
            return False
        result_data = (
            result.get("data") if isinstance(result.get("data"), Mapping) else {}
        )
        return_codes = [
            source.get(key)
            for source in (result, result_data)
            for key in ("returncode", "exit_code")
            if key in source
        ]
        if (
            not return_codes
            or any(
                isinstance(code, bool) or not isinstance(code, int) or code != 0
                for code in return_codes
            )
            or any(source.get("timed_out") is True for source in (result, result_data))
        ):
            return False
        for key in ("decision_id", "tool_plan_id"):
            claimed = str(verifier_link.get(key) or "").strip()
            observed = str(event.get(key) or "").strip()
            if (claimed or observed) and claimed != observed:
                return False
    if predicate_kind == "exact_submit_dispatch_receipt":
        if source_tool != "desktop.submit_foreground":
            return False
        source_input = (
            event.get("input_preview")
            if isinstance(event.get("input_preview"), Mapping)
            else {}
        )
        result_data = (
            result.get("data") if isinstance(result.get("data"), Mapping) else {}
        )
        requested_action = str(source_input.get("action") or "").strip().casefold()
        observed_action = str(
            result_data.get("action")
            or result.get("submitted_action")
            or result.get("action_name")
            or ""
        ).strip().casefold()
        if (
            result.get("ok") is not True
            or requested_action not in {"confirm", "send"}
            or observed_action != requested_action
        ):
            return False
    source_step = str(
        event.get("step_id") or event.get("planner_step_id") or ""
    ).strip()
    claimed_step = str(verifier_link.get("source_step_id") or "").strip()
    if claimed_step and claimed_step != source_step:
        return False
    claimed_plan = str(verifier_link.get("plan_id") or "").strip()
    source_plan = str(event.get("plan_id") or "").strip()
    if claimed_plan and claimed_plan != source_plan:
        return False
    source_provider = _runtime_execution_provider_identity(result)
    for key in ("provider_kind", "provider_id"):
        claimed = str(verifier_link.get(key) or "").strip()
        observed = str(source_provider.get(key) or "").strip()
        if claimed and (not observed or claimed != observed):
            return False
    if predicate_kind == EXACT_FILE_CONTENT_PRESENT_PREDICATE and source_provider != {
        "provider_kind": LOCAL_DESKTOP_PROVIDER_KIND,
        "provider_id": LOCAL_DESKTOP_PROVIDER_ID,
    }:
        return False
    return True


def _runtime_execution_provider_identity(
    result: Mapping[str, Any],
    *,
    expected_tool: str = "",
) -> dict[str, str]:
    """Return only an executor-minted local or fully routed provider identity."""

    provenance = result.get(RUNTIME_EXECUTION_PROVENANCE_KEY)
    if isinstance(provenance, Mapping) and (
        provenance.get("source") == RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE
        and provenance.get("version") == RUNTIME_EXECUTION_PROVENANCE_VERSION
    ):
        if result.get("desktop_execution_provider_routed") is True:
            return {}
        return {
            "provider_kind": LOCAL_DESKTOP_PROVIDER_KIND,
            "provider_id": LOCAL_DESKTOP_PROVIDER_ID,
        }

    provider = (
        result.get("desktop_execution_provider")
        if isinstance(result.get("desktop_execution_provider"), Mapping)
        else {}
    )
    route = (
        result.get("desktop_execution_route")
        if isinstance(result.get("desktop_execution_route"), Mapping)
        else {}
    )
    evidence = (
        result.get("desktop_execution_provider_evidence")
        if isinstance(result.get("desktop_execution_provider_evidence"), Mapping)
        else result.get("desktop_execution_evidence")
        if isinstance(result.get("desktop_execution_evidence"), Mapping)
        else {}
    )
    provider_kind = str(provider.get("provider_kind") or "").strip()
    provider_id = str(provider.get("provider_id") or "").strip()
    routed_tool = str(result.get("tool") or result.get("action") or "").strip()
    if (
        result.get("desktop_execution_provider_routed") is not True
        or provider.get("adapter_registered") is not True
        or not provider_kind
        or not provider_id
        or str(route.get("selected_provider_kind") or "").strip() != provider_kind
        or str(route.get("selected_provider_id") or "").strip() != provider_id
        or not evidence
        or (expected_tool and routed_tool != str(expected_tool or "").strip())
    ):
        return {}
    return {
        "provider_kind": provider_kind,
        "provider_id": provider_id,
    }


def _runtime_owned_terminal_event(
    event: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    run_id: str,
    plan_id: str,
) -> bool:
    """Accept only one executor-authored, provider-attested terminal fact."""

    return bool(
        _runtime_executor_terminal_identity(
            event,
            run_id=run_id,
            plan_id=plan_id,
        )
        and bool(
            _runtime_execution_provider_identity(
                result,
                expected_tool=str(
                    event.get("tool") or event.get("detail") or ""
                ).strip(),
            )
        )
    )


def _runtime_owned_correlated_native_receipt(
    event: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    contract: GoalContract,
    source_attempts: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Authenticate a native verifier through its exact executor source.

    Native postcondition projections intentionally contain the observed fact,
    not a second independently routed desktop-provider receipt.  Their
    provider authority therefore comes only from the already accepted source
    action in this process.  Public/model claims and cross-call/run/plan links
    remain unable to manufacture completion evidence.
    """

    if str(event.get("source") or "").strip() != (
        "runtime_native_postcondition_receipt"
    ):
        return False
    verifier_plan_id = str(event.get("plan_id") or "").strip()
    if not _runtime_executor_terminal_identity(
        event,
        run_id=contract.run_id,
        plan_id=verifier_plan_id,
    ):
        return False
    verifier_link = _trusted_verifier_link(event, result, contract=contract)
    if verifier_link is None or any(
        not str(verifier_link.get(key) or "").strip()
        for key in (
            "source_tool_call_id",
            "source_tool",
            "source_step_id",
            "plan_id",
        )
    ):
        return False
    source_call_id = str(verifier_link["source_tool_call_id"]).strip()
    if source_call_id == str(event.get("tool_call_id") or "").strip():
        return False
    source_attempt = source_attempts.get(source_call_id)
    if source_attempt is None:
        return False
    source_event = source_attempt.get("event")
    source_result = source_attempt.get("result")
    if not isinstance(source_event, Mapping) or not isinstance(
        source_result,
        Mapping,
    ):
        return False
    predicate_kind = str(verifier_link.get("predicate_kind") or "").strip()
    intrinsic_state = _intrinsic_native_state_source_receipt(source_attempt)
    if predicate_kind == "native_postcondition_receipt" and not (
        _exact_dispatch_only_source_receipt(source_attempt) or intrinsic_state
    ):
        # A generic observation after a stateful shortcut (copy, paste,
        # fullscreen, and similar) cannot prove that state changed.  Search
        # submission is the narrow dispatch-only case whose provider receipt
        # itself is the requested effect.
        return False
    if intrinsic_state:
        source_request_id = str(source_event.get("request_id") or "").strip()
        source_provider = _runtime_execution_provider_identity(source_result)
        if (
            not source_request_id
            or str(verifier_link.get("source_request_id") or "").strip()
            != source_request_id
            or str(verifier_link.get("provider_kind") or "").strip()
            != str(source_provider.get("provider_kind") or "").strip()
            or str(verifier_link.get("provider_id") or "").strip()
            != str(source_provider.get("provider_id") or "").strip()
            or str(
                result.get("verified_observed_state")
                or (
                    result.get("data", {}).get("verified_observed_state")
                    if isinstance(result.get("data"), Mapping)
                    else ""
                )
                or ""
            ).strip()
            != intrinsic_state
        ):
            return False
    if not _verifier_matches_source_attempt(verifier_link, source_attempt):
        return False
    source_plan_id = str(source_event.get("plan_id") or "").strip()
    if source_plan_id != verifier_plan_id:
        return False
    return _runtime_owned_terminal_event(
        source_event,
        source_result,
        run_id=contract.run_id,
        plan_id=source_plan_id,
    )


def _intrinsic_native_state_source_receipt(
    source_attempt: Mapping[str, Any],
) -> str:
    source_event = source_attempt.get("event")
    source_result = source_attempt.get("result")
    if not isinstance(source_event, Mapping) or not isinstance(
        source_result, Mapping
    ):
        return ""
    tool_name = str(source_attempt.get("tool") or "").strip()
    source_input = (
        source_event.get("input_preview")
        if isinstance(source_event.get("input_preview"), Mapping)
        else {}
    )
    state = intrinsic_native_postcondition_state(
        tool_name,
        source_input,
        source_result,
    )
    if not state:
        return ""
    target = (
        source_event.get("action_target")
        if isinstance(source_event.get("action_target"), Mapping)
        else {}
    )
    if not intrinsic_native_postcondition_target_matches(
        tool_name,
        source_input,
        target,
    ):
        return ""
    return state


def _exact_dispatch_only_source_receipt(
    source_attempt: Mapping[str, Any],
) -> bool:
    source_event = source_attempt.get("event")
    source_result = source_attempt.get("result")
    if not isinstance(source_event, Mapping) or not isinstance(
        source_result,
        Mapping,
    ):
        return False
    source_input = (
        source_event.get("input_preview")
        if isinstance(source_event.get("input_preview"), Mapping)
        else {}
    )
    tool_name = str(source_attempt.get("tool") or "").strip()
    # An exact key receipt proves delivery, not a changed selection/focus/UI.
    # Keep the dispatch audit but require a separate observation for completion.
    if is_semantic_safe_key(tool_name):
        return False
    event_tool = str(
        source_event.get("tool") or source_event.get("detail") or ""
    ).strip()
    if (
        not tool_name
        or event_tool != tool_name
        or source_result.get("ok") is not True
    ):
        return False
    return exact_native_dispatch_receipt_matches(
        tool_name,
        source_input,
        source_result,
    )


def _runtime_executor_terminal_identity(
    event: Mapping[str, Any],
    *,
    run_id: str,
    plan_id: str,
) -> bool:
    """Validate the process-owned identity shared by pause and terminal facts."""

    return bool(
        str(event.get("event") or event.get("event_type") or "").strip()
        in _TERMINAL_TOOL_EVENTS
        and str(event.get("run_id") or "").strip() == str(run_id or "").strip()
        and bool(str(run_id or "").strip())
        and str(event.get("actor") or "").strip() == "native_runtime"
        and str(event.get("execution_authority") or "").strip()
        == "runtime_tool_executor"
        and str(event.get("plan_id") or "").strip() == str(plan_id or "").strip()
        and bool(str(plan_id or "").strip())
        and bool(
            str(event.get("step_id") or event.get("planner_step_id") or "").strip()
        )
        and bool(str(event.get("request_id") or "").strip())
        and bool(str(event.get("tool_call_id") or "").strip())
    )


def _record_correlated_verifier_evidence(
    coordinator: GoalCoordinator,
    contract: GoalContract,
    assessment: GoalAssessment,
    *,
    source_attempt: Mapping[str, Any],
    verifier_event: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
    verifier_link: Mapping[str, str],
    verifier_tool_call_id: str,
) -> GoalAssessment:
    source_call_id = str(verifier_link["source_tool_call_id"])
    source_event = source_attempt.get("event")
    source_event = source_event if isinstance(source_event, Mapping) else {}
    source_step_id = str(
        source_event.get("step_id") or source_event.get("planner_step_id") or ""
    ).strip()
    source_plan_id = str(source_event.get("plan_id") or "").strip()
    verifier_step_id = str(
        verifier_event.get("step_id")
        or verifier_event.get("planner_step_id")
        or ""
    ).strip()
    recovery_verifier_step_id = _trusted_recovery_root_verifier_step_id(
        contract,
        assessment,
        source_attempt=source_attempt,
        verifier_event=verifier_event,
        verifier_link=verifier_link,
    )
    if recovery_verifier_step_id:
        verifier_step_id = recovery_verifier_step_id
    verifier_plan_id = str(verifier_event.get("plan_id") or "").strip()
    source_observed = source_attempt.get("observed")
    base_observed = (
        dict(source_observed) if isinstance(source_observed, Mapping) else {}
    )
    data = (
        verifier_result.get("data")
        if isinstance(verifier_result.get("data"), Mapping)
        else {}
    )
    verifier_observed = {**dict(verifier_result), **dict(data)}
    original_target = base_observed.get("target")
    base_observed.update(verifier_observed)
    if isinstance(original_target, Mapping):
        base_observed["target"] = dict(original_target)
    explicit_state = str(
        verifier_observed.get("verified_observed_state")
        or verifier_observed.get("observed_state")
        or ""
    ).strip()
    predicate_kind = str(verifier_link.get("predicate_kind") or "").strip()
    source_evidence = [
        item
        for item in assessment.evidence
        if item.kind == "tool_outcome"
        and item.source_tool_call_id == source_call_id
        and item.contract_id == contract.contract_id
        and item.run_id == contract.run_id
    ]
    result = assessment
    for evidence in source_evidence:
        criterion = contract.criterion(evidence.criterion_id)
        if criterion is None:
            continue
        if predicate_kind == EXACT_FILE_CONTENT_PRESENT_PREDICATE and (
            evidence.verified or evidence.status != OutcomeStatus.SUCCESS.value
        ):
            # Exact readback upgrades one successful-but-unverified source
            # attempt. It cannot rehabilitate failed/partial evidence or act
            # as a free-standing workspace-file completion fact.
            continue
        if predicate_kind == EXACT_FILE_CONTENT_PRESENT_PREDICATE:
            if (
                not criterion.verifier_step_ids
                or verifier_step_id not in criterion.verifier_step_ids
                or not source_plan_id
                or verifier_plan_id != source_plan_id
            ):
                # File readback is only evidence for a verifier that the goal
                # contract declared.  It never creates an implicit verifier.
                continue
        elif criterion.verifier_step_ids and (
            verifier_step_id not in criterion.verifier_step_ids
            or not source_plan_id
            or verifier_plan_id != source_plan_id
        ):
            continue
        observed = dict(base_observed)
        expected_state = str(criterion.expected.get("state") or "").strip()
        if predicate_kind == "app_window_present":
            if expected_state.casefold() in {"open", "running", "visible"}:
                observed["state"] = expected_state
        elif predicate_kind == "exact_typed_content_present":
            if expected_state.casefold() in {
                "fulfilled",
                "persisted",
                "typed",
                "visible",
            }:
                observed["state"] = expected_state
        elif predicate_kind == "exact_submit_dispatch_receipt":
            if expected_state.casefold() in {"sent", "submitted"}:
                observed["state"] = expected_state
        elif predicate_kind == EXACT_FILE_CONTENT_PRESENT_PREDICATE:
            if expected_state.casefold() in {"fulfilled", "persisted"}:
                observed["state"] = expected_state
        elif explicit_state:
            observed["state"] = explicit_state
        result = coordinator.record_verifier_evidence(
            contract,
            result,
            criterion_id=criterion.criterion_id,
            run_id=contract.run_id,
            source_tool_call_id=source_call_id,
            verifier_tool_call_id=verifier_tool_call_id,
            source_step_id=evidence.source_step_id or source_step_id,
            verifier_step_id=verifier_step_id,
            plan_id=evidence.plan_id or source_plan_id,
            verification_predicate=predicate_kind,
            observed=observed,
        )
    return result


def _record_semantic_artifact_assessment(
    coordinator: GoalCoordinator,
    contract: GoalContract,
    assessment: GoalAssessment,
    event: Mapping[str, Any],
) -> GoalAssessment:
    """Accept a persisted semantic verdict only over prior exact readback."""

    trusted = _trusted_semantic_artifact_assessment(
        contract,
        assessment,
        event,
    )
    if trusted is None:
        return assessment
    exact_evidence, verdict = trusted
    observed = dict(exact_evidence.observed)
    observed["semantic_artifact_verdict"] = verdict
    return coordinator.record_verifier_evidence(
        contract,
        assessment,
        criterion_id=exact_evidence.criterion_id,
        run_id=contract.run_id,
        source_tool_call_id=exact_evidence.source_tool_call_id,
        verifier_tool_call_id=exact_evidence.verifier_tool_call_id,
        source_step_id=exact_evidence.source_step_id,
        verifier_step_id=exact_evidence.verifier_step_id,
        plan_id=exact_evidence.plan_id,
        verification_predicate=SEMANTIC_ARTIFACT_ADEQUACY_PREDICATE,
        predicate_fulfilled=verdict == "fulfilled",
        status=verdict,
        observed=observed,
    )


def _trusted_semantic_artifact_assessment(
    contract: GoalContract,
    assessment: GoalAssessment,
    event: Mapping[str, Any],
) -> tuple[Any, str] | None:
    if (
        str(event.get("event") or event.get("event_type") or "").strip()
        != SEMANTIC_ARTIFACT_ASSESSED_EVENT
        or str(event.get("actor") or "").strip() != "native_runtime"
        or str(event.get("execution_authority") or "").strip()
        != RUNTIME_SEMANTIC_ARTIFACT_VERIFIER_AUTHORITY
        or str(event.get("source") or "").strip()
        != RUNTIME_SEMANTIC_ARTIFACT_VERIFIER_AUTHORITY
        or str(event.get("visibility") or "").strip() != "internal"
        or str(event.get("run_id") or "").strip() != contract.run_id
        or str(event.get("contract_id") or "").strip()
        != contract.contract_id
    ):
        return None
    criterion_id = str(event.get("criterion_id") or "").strip()
    criterion = contract.criterion(criterion_id)
    if (
        criterion is None
        or SEMANTIC_ARTIFACT_ADEQUACY_PREDICATE
        not in criterion.required_verification_predicates
    ):
        return None
    if (
        str(event.get("semantic_rubric_sha256") or "").strip().casefold()
        != _semantic_artifact_rubric_sha256(contract, criterion)
    ):
        return None
    verdict = str(event.get("verdict") or "").strip()
    if verdict not in {"fulfilled", "insufficient", "uncertain"}:
        return None
    expected_text = {
        "source_tool_call_id": str(
            event.get("source_tool_call_id") or ""
        ).strip(),
        "source_step_id": str(event.get("source_step_id") or "").strip(),
        "verifier_tool_call_id": str(
            event.get("structural_verifier_tool_call_id") or ""
        ).strip(),
        "verifier_step_id": str(
            event.get("structural_verifier_step_id") or ""
        ).strip(),
        "plan_id": str(event.get("plan_id") or "").strip(),
        "observed_path": str(event.get("observed_path") or "").strip(),
        "content_sha256": str(event.get("content_sha256") or "")
        .strip()
        .casefold(),
    }
    if any(not value for value in expected_text.values()):
        return None
    if (
        normalized_workspace_relative_path(expected_text["observed_path"])
        != expected_text["observed_path"]
        or re.fullmatch(r"[0-9a-f]{64}", expected_text["content_sha256"])
        is None
    ):
        return None
    content_length = event.get("content_length")
    if (
        isinstance(content_length, bool)
        or not isinstance(content_length, int)
        or content_length <= 0
    ):
        return None
    for evidence in reversed(assessment.evidence):
        if (
            evidence.criterion_id != criterion_id
            or evidence.kind != "verifier"
            or evidence.verification_predicate
            != EXACT_FILE_CONTENT_PRESENT_PREDICATE
            or not evidence.verified
            or evidence.source_tool_call_id
            != expected_text["source_tool_call_id"]
            or evidence.source_step_id != expected_text["source_step_id"]
            or evidence.verifier_tool_call_id
            != expected_text["verifier_tool_call_id"]
            or evidence.verifier_step_id
            != expected_text["verifier_step_id"]
            or evidence.plan_id != expected_text["plan_id"]
        ):
            continue
        observed_path = str(
            evidence.observed.get("observed_path") or ""
        ).strip()
        content_sha256 = str(
            evidence.observed.get("content_sha256") or ""
        ).strip().casefold()
        observed_length = evidence.observed.get("content_length")
        content = evidence.observed.get("content")
        if (
            observed_path != expected_text["observed_path"]
            or content_sha256 != expected_text["content_sha256"]
            or observed_length != content_length
            or not isinstance(content, str)
            or not content
            or len(content.encode("utf-8")) != content_length
            or hashlib.sha256(content.encode("utf-8")).hexdigest()
            != content_sha256
        ):
            continue
        exact_lineage = _semantic_artifact_evidence_lineage(evidence)
        exact_identity = _semantic_artifact_assessment_identity(evidence)
        if exact_lineage is None or exact_identity is None or any(
            prior.verification_predicate
            == SEMANTIC_ARTIFACT_ADEQUACY_PREDICATE
            and _semantic_artifact_assessment_identity(prior) == exact_identity
            for prior in assessment.evidence
        ):
            # One exact digest has one semantic terminal winner. Re-evaluation
            # requires new structural content, not a contradictory projection.
            return None
        return evidence, verdict
    return None


def _trusted_recovery_root_verifier_step_id(
    contract: GoalContract,
    assessment: GoalAssessment,
    *,
    source_attempt: Mapping[str, Any],
    verifier_event: Mapping[str, Any],
    verifier_link: Mapping[str, str],
) -> str:
    if (
        str(verifier_event.get("source") or "").strip()
        != "runtime_native_postcondition_receipt"
        or verifier_event.get("recovery_context_trusted") is not True
        or verifier_event.get("root_goal_unchanged") is not True
        or str(verifier_event.get("recovery_link_kind") or "").strip()
        != "coordinator_action"
        or str(verifier_event.get("goal_contract_id") or "").strip()
        != contract.contract_id
    ):
        return ""
    criterion_id = str(verifier_event.get("goal_criterion_id") or "").strip()
    subgoal_id = str(verifier_event.get("goal_subgoal_id") or "").strip()
    root_source_tool_call_id = str(
        verifier_event.get("root_source_tool_call_id") or ""
    ).strip()
    root_source_step_id = str(
        verifier_event.get("root_source_step_id") or ""
    ).strip()
    root_verifier_step_id = str(
        verifier_event.get("root_verifier_step_id") or ""
    ).strip()
    root_plan_id = str(verifier_event.get("root_plan_id") or "").strip()
    recovery_scope_id = str(
        verifier_event.get("recovery_scope_id") or ""
    ).strip()
    criterion = contract.criterion(criterion_id)
    subgoal = next(
        (item for item in assessment.subgoals if item.subgoal_id == subgoal_id),
        None,
    )
    source_event = source_attempt.get("event")
    source_result = source_attempt.get("result")
    expected_provider = {
        "provider_kind": str(
            verifier_event.get("root_provider_kind") or ""
        ).strip(),
        "provider_id": str(verifier_event.get("root_provider_id") or "").strip(),
    }
    if (
        criterion is None
        or subgoal is None
        or subgoal.criterion_id != criterion_id
        or subgoal.action
        != str(verifier_event.get("recovery_action") or "").strip()
        or root_source_step_id not in criterion.source_step_ids
        or root_verifier_step_id not in criterion.verifier_step_ids
        or not root_source_tool_call_id
        or not root_plan_id
        or not recovery_scope_id
        or str(verifier_event.get("replan_recovery_identity") or "").strip()
        != recovery_scope_id
        or not isinstance(source_event, Mapping)
        or not isinstance(source_result, Mapping)
        or not _runtime_owned_terminal_event(
            source_event,
            source_result,
            run_id=contract.run_id,
            plan_id=root_plan_id,
        )
        or str(verifier_event.get("run_id") or "").strip() != contract.run_id
        or str(verifier_event.get("actor") or "").strip() != "native_runtime"
        or str(verifier_event.get("execution_authority") or "").strip()
        != "runtime_tool_executor"
        or str(verifier_event.get("plan_id") or "").strip() != root_plan_id
        or not all(expected_provider.values())
        or _runtime_execution_provider_identity(source_result) != expected_provider
        or {
            "provider_kind": str(verifier_link.get("provider_kind") or "").strip(),
            "provider_id": str(verifier_link.get("provider_id") or "").strip(),
        }
        != expected_provider
    ):
        return ""
    if str(verifier_link.get("source_tool_call_id") or "").strip() != str(
        source_event.get("tool_call_id") or ""
    ).strip():
        return ""
    for key in (
        "goal_contract_id",
        "goal_criterion_id",
        "goal_subgoal_id",
        "recovery_scope_id",
        "replan_recovery_identity",
        "root_source_tool_call_id",
        "root_source_step_id",
        "root_verifier_step_id",
        "root_plan_id",
    ):
        if str(source_event.get(key) or "").strip() != str(
            verifier_event.get(key) or ""
        ).strip():
            return ""
    return root_verifier_step_id


def complete_response_only_goal(
    contract: GoalContract,
    assessment: GoalAssessment,
    response_text: str,
) -> GoalAssessment:
    if not all(
        not criterion.effectful and criterion.response_satisfiable
        for criterion in contract.criteria
        if criterion.required
    ):
        return assessment
    return GoalCoordinator().record_final_response(
        contract,
        assessment,
        run_id=contract.run_id,
        response_text=response_text,
    )


def goal_contract_context_message(contract: GoalContract) -> str:
    criteria = [_criterion_context_payload(item) for item in contract.criteria if item.required]
    return (
        "Runtime root-goal contract (trusted and immutable for this run):\n"
        f"Contract id: {contract.contract_id}\n"
        f"Original user goal: {contract.original_goal}\n"
        f"Completion criteria: {json.dumps(criteria, ensure_ascii=False, separators=(',', ':'))}\n"
        "Intermediate discovery, alias resolution, app launch, search results, and model prose "
        "do not complete an effectful criterion. Recovery subgoals must remain bound to an "
        "unsatisfied criterion and return to this original goal. Only correlated Runtime "
        "verification evidence may complete effectful work."
    )


def goal_replan_context_message(
    contract: GoalContract,
    assessment: GoalAssessment,
) -> str:
    unsatisfied = [
        _criterion_context_payload(criterion)
        for criterion in contract.criteria
        if criterion.criterion_id in assessment.unsatisfied_criterion_ids
    ]
    return (
        "Runtime goal verification: the original goal is not complete.\n"
        f"Original user goal (unchanged): {contract.original_goal}\n"
        "Unsatisfied criteria: "
        f"{json.dumps(unsatisfied, ensure_ascii=False, separators=(',', ':'))}\n"
        "Choose the next bounded action or verifier for these criteria. Do not report success "
        "from an intermediate recovery result or from your own prior text."
    )


def goal_contract_event_payload(contract: GoalContract) -> dict[str, Any]:
    serialized = contract.to_payload()
    return {
        "goal_contract": serialized,
        "goal_contract_json": json.dumps(
            serialized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "contract_id": contract.contract_id,
        "run_id": contract.run_id,
        "status": "active",
        "visibility": "internal",
    }


def goal_assessment_event_payload(assessment: GoalAssessment) -> dict[str, Any]:
    serialized = assessment.to_persisted_payload()
    return {
        "goal_assessment": serialized,
        "goal_assessment_json": json.dumps(
            serialized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "contract_id": assessment.contract_id,
        "run_id": assessment.run_id,
        "status": "completed" if assessment.completed else "incomplete",
        "satisfied_criterion_ids": list(assessment.satisfied_criterion_ids),
        "unsatisfied_criterion_ids": list(assessment.unsatisfied_criterion_ids),
        "visibility": "internal",
    }


def _persisted_goal_contract_payloads(
    timeline: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
) -> list[Mapping[str, Any]]:
    payloads: list[Mapping[str, Any]] = []
    for raw_event in timeline:
        if not isinstance(raw_event, Mapping):
            continue
        event = _flatten_event(raw_event)
        event_type = str(event.get("event") or event.get("event_type") or "").strip()
        if event_type != "agent.goal.contract":
            continue
        event_run_id = str(event.get("run_id") or "").strip()
        if event_run_id and event_run_id != run_id:
            raise ValueError("goal_contract_invalid: cross_run_event")

        has_json = "goal_contract_json" in event
        has_mapping = "goal_contract" in event
        if not has_json and not has_mapping:
            raise ValueError("goal_contract_invalid: missing_payload")
        if has_json:
            serialized = _mapping_from_json(event.get("goal_contract_json"))
            if serialized is None:
                raise ValueError("goal_contract_invalid: damaged_json")
            payloads.append(serialized)
        if has_mapping:
            payload = event.get("goal_contract")
            if not isinstance(payload, Mapping):
                raise ValueError("goal_contract_invalid: damaged_mapping")
            payloads.append(payload)
    return payloads


def _mapping_from_json(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _canonical_goal_contract_payload(contract: GoalContract) -> str:
    """Return an order-insensitive identity for one bound contract."""

    payload = contract.to_payload()
    payload.pop("source", None)
    criteria: list[dict[str, Any]] = []
    for criterion in contract.criteria:
        item = criterion.to_payload()
        for key in (
            "required_capabilities",
            "required_effects",
            "required_verification_predicates",
            "source_step_ids",
            "verifier_step_ids",
        ):
            item[key] = sorted(str(value) for value in item.get(key) or [])
        criteria.append(item)
    payload["criteria"] = sorted(
        criteria,
        key=lambda item: str(item.get("criterion_id") or ""),
    )
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _strict_goal_contract_from_payload(
    payload: Mapping[str, Any],
) -> GoalContract:
    """Restore without allowing malformed fields to be silently discarded."""

    for key in ("contract_id", "original_goal"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"goal_contract_invalid: {key}")
    for key in ("run_id", "intent_kind"):
        if key in payload and not isinstance(payload.get(key), str):
            raise ValueError(f"goal_contract_invalid: {key}")
    for key in ("max_total_attempts", "max_subgoal_attempts"):
        value = payload.get(key)
        if key in payload and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError(f"goal_contract_invalid: {key}")

    raw_criteria = payload.get("criteria")
    if not isinstance(raw_criteria, (list, tuple)) or not raw_criteria:
        raise ValueError("goal_contract_invalid: criteria")
    for criterion in raw_criteria:
        if not isinstance(criterion, Mapping):
            raise ValueError("goal_contract_invalid: criterion")
        for key in ("criterion_id", "description"):
            value = criterion.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"goal_contract_invalid: criterion_{key}")
        for key in ("effectful", "required", "response_satisfiable"):
            if key in criterion and not isinstance(criterion.get(key), bool):
                raise ValueError(f"goal_contract_invalid: criterion_{key}")
        for key in (
            "required_capabilities",
            "required_effects",
            "required_verification_predicates",
            "source_step_ids",
            "verifier_step_ids",
        ):
            values = criterion.get(key)
            if key in criterion and not isinstance(values, (list, tuple)):
                raise ValueError(f"goal_contract_invalid: criterion_{key}")
            if isinstance(values, (list, tuple)) and any(
                not isinstance(value, str) or not value.strip()
                for value in values
            ):
                raise ValueError(f"goal_contract_invalid: criterion_{key}")
        if "expected" in criterion and not isinstance(
            criterion.get("expected"),
            Mapping,
        ):
            raise ValueError("goal_contract_invalid: criterion_expected")
    return GoalContract.from_payload(payload)


def _goal_contract_payloads_from_container(
    container: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    if not isinstance(container, Mapping):
        return []
    payloads: list[Mapping[str, Any]] = []
    if "goal_contract_json" in container:
        serialized = _mapping_from_json(container.get("goal_contract_json"))
        if serialized is None:
            raise ValueError("goal_contract_invalid: damaged_explicit_json")
        payloads.append(serialized)
    if "goal_contract" in container:
        direct = container.get("goal_contract")
        if not isinstance(direct, Mapping):
            raise ValueError("goal_contract_invalid: damaged_explicit_mapping")
        payloads.append(direct)
    for key in (
        "task_core",
        "plan",
        "runtime_plan",
        "runtime_execution_envelope",
        "metadata",
    ):
        nested = container.get(key)
        if not isinstance(nested, Mapping):
            continue
        payloads.extend(_goal_contract_payloads_from_container(nested))
    return payloads


def _explicit_goal_contract_template_payloads(
    template: GoalContract | Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    if template is None:
        return []
    if isinstance(template, GoalContract):
        return [template.to_payload()]
    if not isinstance(template, Mapping):
        raise ValueError("goal_contract_invalid: damaged_explicit_template")
    contract_keys = frozenset({"contract_id", "original_goal", "criteria"})
    present_contract_keys = contract_keys.intersection(template)
    if present_contract_keys and present_contract_keys != contract_keys:
        raise ValueError("goal_contract_invalid: partial_explicit_template")
    if present_contract_keys == contract_keys:
        return [template]
    return _goal_contract_payloads_from_container(template)


def _flatten_event(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return {**dict(payload), **dict(event)} if isinstance(payload, Mapping) else dict(event)


def _canonical_observed_payload(
    contract: GoalContract,
    event: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    capabilities: Iterable[str],
    eligible_criterion_ids: Iterable[str],
    timeline: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    observed = {**dict(result), **dict(data)}
    capability_set = set(capabilities)
    matching = [
        criterion
        for criterion in contract.criteria
        if criterion.criterion_id in set(eligible_criterion_ids)
        if set(criterion.required_capabilities).issubset(capability_set)
    ]
    media_identity_conflict = bool(
        "media.playback" in capability_set
        and _explicit_media_identity_conflict(result, data)
    )
    verification_passed = bool(
        _verification_passed(result, data)
        and not media_identity_conflict
    )
    if "media.playback" in capability_set:
        state = (
            ""
            if media_identity_conflict
            else canonical_media_playback_state(result, data)
        )
        if media_track_change_verified(result, data):
            observed["track_change_verified"] = True
    else:
        state = _canonical_observed_state(observed)
    source_tool = str(event.get("tool") or event.get("detail") or "").strip()
    if is_semantic_safe_key(source_tool):
        # Even legacy rows with effectful=False cannot reinterpret a key's
        # own acknowledgement (or self-reported flags) as an observed UI goal.
        verification_passed = False
        state = "dispatched"
    if (
        not state
        and len(matching) == 1
        and (
            verification_passed
            or (
                not matching[0].effectful
                and result.get("ok") is True
            )
        )
    ):
        expected_state = matching[0].expected.get("state")
        if isinstance(expected_state, str) and expected_state.strip():
            state = expected_state.strip()
    if state:
        observed["state"] = state
    action_target = event.get("action_target")
    if isinstance(action_target, Mapping) and action_target:
        resolution_required = action_target.get("resolution_required") is True
        resolution = action_target.get("workspace_file_resolution")
        if not resolution_required:
            observed["target"] = _observed_action_target(
                event,
                result,
                data,
                action_target,
                verification_passed=verification_passed,
            )
        elif isinstance(resolution, Mapping) and (
            validate_workspace_file_resolution_receipt(
                resolution,
                event,
                timeline,
                run_id=contract.run_id,
            )
        ):
            observed["target"] = _observed_action_target(
                event,
                result,
                data,
                action_target,
                verification_passed=verification_passed,
            )
    return observed


def _observed_action_target(
    event: Mapping[str, Any],
    result: Mapping[str, Any],
    data: Mapping[str, Any],
    action_target: Mapping[str, Any],
    *,
    verification_passed: bool,
) -> dict[str, Any]:
    """Attach exact semantic input only when a strong receipt confirms it."""

    target = dict(action_target)
    tool_name = str(event.get("tool") or event.get("detail") or "").strip()
    semantic_field = {
        "desktop.safe_key": "key_action",
        "desktop.safe_shortcut": "shortcut_action",
        "desktop.shortcut": "shortcut_action",
    }.get(tool_name)
    if not semantic_field or not verification_passed:
        return target
    input_preview = (
        event.get("input_preview")
        if isinstance(event.get("input_preview"), Mapping)
        else {}
    )
    requested_action = str(input_preview.get("action") or "").strip().casefold()
    observed_action = str(
        data.get(semantic_field) or result.get(semantic_field) or ""
    ).strip().casefold()
    if not requested_action or observed_action != requested_action:
        return target
    # Goal contracts use one application-independent field for a semantic
    # foreground key/shortcut action.  Keep provider-specific result keys out
    # of the contract while still binding the exact requested action.
    target["shortcut_action"] = observed_action
    requested_repeat = input_preview.get("repeat_count")
    observed_repeat = data.get("repeat_count", result.get("repeat_count"))
    if (
        isinstance(requested_repeat, int)
        and not isinstance(requested_repeat, bool)
        and isinstance(observed_repeat, int)
        and not isinstance(observed_repeat, bool)
        and requested_repeat == observed_repeat
    ):
        target["repeat_count"] = observed_repeat
    return target


def _explicit_media_identity_conflict(
    result: Mapping[str, Any],
    data: Mapping[str, Any],
) -> bool:
    """Preserve explicit negative identity evidence over positive play state."""

    sources: list[Mapping[str, Any]] = [result, data]
    for source in tuple(sources):
        for key in ("verification", "identity", "catalog_identity"):
            nested = source.get(key)
            if isinstance(nested, Mapping):
                sources.append(nested)
    if any(
        key in source and source.get(key) is False
        for source in sources
        for key in (
            "catalog_match_verified",
            "track_identity_verified",
        )
    ):
        return True
    if any(
        source.get(key) is True
        for source in sources
        for key in (
            "catalog_identity_conflict",
            "identity_conflict",
            "identity_changed_before_play",
            "track_identity_conflict",
            "track_mismatch",
            "wrong_track",
        )
    ):
        return True
    conflict_markers = {
        "catalog_identity_conflict",
        "catalog_mismatch",
        "identity_conflict",
        "identity_mismatch",
        "track_identity_conflict",
        "track_mismatch",
        "wrong_track",
    }
    return any(
        str(source.get(key) or "").strip().casefold().replace("-", "_")
        in conflict_markers
        for source in sources
        for key in ("status", "reason", "error_code", "code")
    )


def _verification_passed(
    result: Mapping[str, Any],
    data: Mapping[str, Any],
) -> bool:
    return any(
        candidate.get(key) is True
        for candidate in (result, data)
        for key in ("postcondition_verified", "verification_passed", "verified")
    )


def _canonical_observed_state(observed: Mapping[str, Any]) -> str:
    if observed.get("playback_started") is True:
        return "playing"
    for key in ("playback_state", "state", "status"):
        value = str(observed.get(key) or "").strip().casefold()
        if value in {"playing", "open", "focused", "persisted", "scheduled", "sent"}:
            return value
    return ""


def _criterion_context_payload(criterion: GoalCriterion) -> dict[str, Any]:
    return {
        "criterion_id": criterion.criterion_id,
        "description": criterion.description,
        "effectful": criterion.effectful,
        "required_capabilities": list(criterion.required_capabilities),
        "required_verification_predicates": list(
            criterion.required_verification_predicates
        ),
        "expected": _plain_json(criterion.expected),
        "source_step_ids": list(criterion.source_step_ids),
        "verifier_step_ids": list(criterion.verifier_step_ids),
    }


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha256(
        "\0".join(str(part or "") for part in parts).encode("utf-8")
    ).hexdigest()[:24]
    return f"{prefix}-{digest}"


__all__ = [
    "complete_response_only_goal",
    "goal_assessment_event_payload",
    "goal_contract_context_message",
    "goal_contract_event_payload",
    "goal_replan_context_message",
    "planned_goal_contract_payload",
    "pending_semantic_artifact_assessment_candidates",
    "runtime_goal_assessment",
    "runtime_goal_contract",
]
