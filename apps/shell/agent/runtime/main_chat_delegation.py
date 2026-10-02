"""Runtime-owned bridge for legacy delegation proposals and native goal evidence.

Model JSON proposes a dispatch. The saved catalog, immutable parent request,
planner contract and durable child identities are the execution authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from .goal_contract import GoalContract, GoalCoordinator
from .run_group_attachments import (
    RUN_GROUP_ATTACHMENT_PAYLOAD_KEY,
    issue_run_group_child_attachment,
)
from .tool_outcomes import OutcomeStatus, ToolOutcome, VerificationStatus

PLAN_EVENT = "agent.delegation.plan.bound"
CHILDREN_EVENT = "agent.delegation.children.bound"
COMPLETED_EVENT = "agent.delegation.children.verified"
SUMMARY_BOUND_EVENT = "agent.delegation.summary.bound"
SUMMARY_EVENT = "agent.delegation.summary.verified"
SOURCE = "runtime_main_chat_delegation"


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def requires_summary(goal: str) -> bool:
    return bool(
        re.search(
            r"[,，]\s*(?:然后给我结论|然后总结结果|并总结结果|then\s+summari[sz]e\s+the\s+results)[。.!！]?\s*$",
            goal,
            re.I,
        )
    )


def _payload(event: Mapping[str, Any]) -> dict[str, Any]:
    nested = event.get("payload")
    return {**event, **(nested if isinstance(nested, dict) else {})}


def is_delegation_proposal(content: str) -> bool:
    try:
        value = json.loads(content.strip().removeprefix("```json").removesuffix("```").strip())
    except (ValueError, TypeError):
        return False
    return isinstance(value, dict) and value.get("action") in {"run_oha_agent", "run_oha_workflow"}


def bound_plan(run: Mapping[str, Any]) -> dict[str, Any] | None:
    plans = [
        _payload(event)
        for event in run.get("timeline") or []
        if isinstance(event, Mapping)
        and (event.get("event") or event.get("event_type")) == PLAN_EVENT
    ]
    if len(plans) != 1:
        return None
    plan = plans[0]
    if plan.get("run_id") and plan["run_id"] != run.get("run_id"):
        return None
    binding = plan.get("binding")
    if isinstance(plan.get("binding_json"), str):
        try:
            binding = json.loads(plan["binding_json"])
        except (ValueError, TypeError):
            return None
    if not isinstance(binding, dict) or plan.get("source") != SOURCE:
        return None
    targets = binding.get("targets")
    if (
        not isinstance(targets, list)
        or not 1 <= len(targets) <= 3
        or any(
            not isinstance(target, Mapping)
            or not isinstance(target.get("runnable_id"), str)
            or not target["runnable_id"]
            or not isinstance(target.get("goal"), str)
            or not target["goal"]
            for target in targets
        )
        or len({target["runnable_id"] for target in targets}) != len(targets)
    ):
        return None
    if binding.get("parent_run_id") != run.get("run_id") or binding.get("original_goal") != run.get(
        "user_goal"
    ):
        return None
    return {**plan, "binding": binding} if plan.get("binding_id") == _digest(binding) else None


def inherited_policy(child: Mapping[str, Any], parent: Mapping[str, Any]) -> dict[str, Any]:
    """Intersect automatic delegation with the parent's compiled authority."""
    child_tools = child.get("tool_policy") or {}
    parent_tools = parent.get("tool_policy") or {}
    allowed = sorted(
        set(child_tools.get("allowed_tools") or []) & set(parent_tools.get("allowed_tools") or [])
    )
    child_ws = child.get("workspace_policy") or {}
    parent_ws = parent.get("workspace_policy") or {}
    base = Path(str(parent_ws.get("default_workdir") or ".")).expanduser().resolve()
    child_base = Path(str(child_ws.get("default_workdir") or ".")).expanduser().resolve()
    scopes: dict[str, list[str]] = {}
    for key in ("readable_scopes", "writable_scopes"):
        intersections = []
        for left in parent_ws.get(key) or []:
            parent_root = (base / str(left)).resolve()
            for right in child_ws.get(key) or []:
                child_root = (child_base / str(right)).resolve()
                root = (
                    child_root
                    if child_root.is_relative_to(parent_root)
                    else parent_root
                    if parent_root.is_relative_to(child_root)
                    else None
                )
                if root is not None and root.is_relative_to(base):
                    intersections.append(str(root.relative_to(base)))
        scopes[key] = sorted(set(intersections))
    # ToolBroker's legacy empty-scope fallback means no scopes alone cannot
    # deny access. Remove every workspace/process tool if there is no overlap.
    if not scopes["readable_scopes"]:
        allowed = [
            name
            for name in allowed
            if not name.startswith(
                (
                    "workspace.",
                    "terminal.",
                    "python.",
                    "data.",
                    "file.",
                    "desktop.open_path",
                    "desktop.reveal_path",
                )
            )
        ]
    if not scopes["writable_scopes"]:
        allowed = [
            name
            for name in allowed
            if name not in {"workspace.write_patch", "terminal.run", "python.run", "file.organize"}
        ]
    approvals = {
        name: True
        for name in allowed
        if (child_tools.get("approval_required") or {}).get(name)
        or (parent_tools.get("approval_required") or {}).get(name)
    }
    # Skills add implicit tool authority during compilation; automatic
    # delegation must not add skill.read that the parent did not permit.
    return {
        "tool_policy": {"allowed_tools": allowed, "approval_required": approvals},
        "workspace_policy": {"default_workdir": str(base), **scopes},
        "skill_ids": list(child.get("skill_ids") or []) if "skill.read" in allowed else [],
    }


def compile_plan(
    *,
    run: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    parent_runtime: Mapping[str, Any],
    selected_ids: set[str],
    group_scope: bool = False,
    direct_group: bool = False,
) -> dict[str, Any] | None:
    from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner

    goal = str(run.get("user_goal") or "").strip()
    assignment_prefix = (
        r"(?:^|[；;。\n])\s*(?:请|麻烦|please\s+)?"
        r"(?:让|安排|委派给|交给|派给|ask\s+|assign\s+|delegate\s+to\s+)\s*"
    )
    starts = [match.start() for match in re.finditer(assignment_prefix, goal, re.I)]
    if starts and starts[0] != 0:
        return None
    targets = [
        target
        for target in targets
        if target.get("kind") == "agent"
        and target.get("enabled", True)
        and (not group_scope or str(target.get("id")) in selected_ids)
    ]
    assignments, covered = [], set()
    for target in targets:
        aliases = {str(target.get("name") or ""), str(target.get("nickname") or "")}
        matches = {}
        for alias in sorted(aliases - {""}, key=len, reverse=True):
            pattern = assignment_prefix + re.escape(alias) + r"(?=\s|[:：])\s*[:：]?\s*"
            for match in re.finditer(pattern, goal, re.I):
                matches.setdefault(match.start(), (match.end(), alias))
        if not matches:
            continue
        if len(matches) != 1:
            return None  # A runnable cannot silently collapse repeated clauses.
        start, (end, alias) = next(iter(matches.items()))
        if start in covered:
            return None  # Ambiguous catalog aliases are not authority.
        covered.add(start)
        next_start = next((position for position in starts if position > start), len(goal))
        segment = goal[end:next_start].strip()
        segment = re.sub(
            r"[,，]\s*(?:然后给我结论|然后总结结果|并总结结果|then\s+summari[sz]e\s+the\s+results)[。.!！]?\s*$",
            "",
            segment,
            flags=re.I,
        ).strip()
        if not segment or len(segment) > 4000:
            return None
        policy = (
            {
                "tool_policy": deepcopy(target.get("tool_policy") or {}),
                "workspace_policy": deepcopy(target.get("workspace_policy") or {}),
                "skill_ids": list(target.get("skill_ids") or []),
            }
            if str(target.get("id")) in selected_ids
            else inherited_policy(target, parent_runtime)
        )
        assignments.append(
            {
                "runnable_id": str(target["id"]),
                "name": str(target.get("name") or alias),
                "alias": alias,
                "kind": "agent",
                "goal": segment,
                "policy": policy,
                "position": start,
            }
        )
    if covered != set(starts):
        return None  # Unknown, disabled or excluded clauses must remain unresolved.
    assignments.sort(key=lambda target: target.pop("position"))
    if not assignments and direct_group and group_scope:
        from apps.shell.chat_api import ChatAPI

        # This is a server-parsed collaboration request, never model JSON.
        # Each participant receives the entire immutable request and its
        # existing group role; do not invent a less constrained subgoal.
        participants = [dict(target) for target in targets if target.get("id") in selected_ids]
        directives = ChatAPI._direct_group_dispatch_directives(goal, {"participants": participants})
        aliases = {
            alias
            for target in participants
            for alias in (target.get("name"), target.get("nickname"))
            if alias
        }
        prefix = (
            r"(?:请|麻烦|please\s+)?\s*(?:"
            + "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
            + r")"
        )
        if (
            not aliases
            or not re.match(prefix, goal, re.I)
            or not re.search(r"(?:一起|分别|协作|配合|together|parallel)", goal, re.I)
        ):
            return None
        header = re.split(
            r"(?:一起|分别|协作|配合|together|parallel)", goal, maxsplit=1, flags=re.I
        )[0]
        header = re.sub(r"^(?:请|麻烦|please\s+)?\s*", "", header, flags=re.I).strip()
        names = [
            name.strip()
            for name in re.split(r"\s*(?:和|与|、|,|，|&|\band\b)\s*", header, flags=re.I)
        ]
        declared = []
        for name in names:
            matches = [
                target
                for target in participants
                if name.casefold()
                in {
                    str(target.get("name") or "").casefold(),
                    str(target.get("nickname") or "").casefold(),
                }
            ]
            if len(matches) != 1 or matches[0]["id"] in declared:
                return None
            declared.append(matches[0]["id"])
        if len(directives) != len(declared) or {
            directive.runnable_id for directive in directives
        } != set(declared):
            return None
        for directive in directives:
            target = next(
                (item for item in participants if item["id"] == directive.runnable_id), None
            )
            if target is None:
                return None
            assignments.append(
                {
                    "runnable_id": target["id"],
                    "kind": "agent",
                    "name": target["name"],
                    "alias": target.get("nickname") or target["name"],
                    "goal": goal,
                    "policy": {
                        "tool_policy": deepcopy(target.get("tool_policy") or {}),
                        "workspace_policy": deepcopy(target.get("workspace_policy") or {}),
                        "skill_ids": list(target.get("skill_ids") or []),
                    },
                }
            )
    if not assignments or len(assignments) > 3:
        return None
    decision = RuntimePlanner().decision(
        goal,
        allowed_tools=["group.start"],
        metadata={"available_agent_groups": [item["alias"] for item in assignments]},
    )
    if decision.selected_intent.kind != "multi_agent" or decision.plan.task_core is None:
        return None
    contract = decision.plan.task_core.goal_contract.model_dump()
    if any(
        set(item.get("required_capabilities") or []) != {"group.multi_agent"}
        for item in contract["criteria"]
    ):
        return None
    binding = {
        "parent_run_id": run["run_id"],
        "original_goal": goal,
        "targets": assignments,
        "plan_id": decision.plan.plan_id,
        "summary_required": group_scope and requires_summary(goal),
        "direct_group": direct_group,
    }
    binding_id = _digest(binding)
    for criterion in contract["criteria"]:
        criterion["expected"] = {**criterion["expected"], "delegation_binding_id": binding_id}
    contract["run_id"] = run["run_id"]
    return {
        "source": SOURCE,
        "binding_id": binding_id,
        "binding": binding,
        "goal_contract": contract,
        "planner_decision_id": decision.decision_id,
    }


def validate_proposals(plan: Mapping[str, Any], proposals: Sequence[Mapping[str, Any]]) -> None:
    expected = plan["binding"]["targets"]
    if len(proposals) != len(expected):
        raise ValueError("delegation_proposal_target_count_conflict")
    seen = set()
    for proposal in proposals:
        matches = [
            item
            for item in expected
            if (
                (proposal.get("runnable_id") and proposal.get("runnable_id") == item["runnable_id"])
                or (
                    not proposal.get("runnable_id")
                    and str(proposal.get("name") or proposal.get("target") or "").casefold()
                    in {item["name"].casefold(), item["alias"].casefold()}
                )
            )
        ]
        if len(matches) != 1 or matches[0]["runnable_id"] in seen:
            raise ValueError("delegation_proposal_target_conflict")
        target = matches[0]
        if (
            str(proposal.get("kind") or "agent") != "agent"
            or str(proposal.get("goal") or "").strip() != target["goal"]
        ):
            raise ValueError("delegation_proposal_goal_conflict")
        seen.add(target["runnable_id"])


class MainChatDelegationCoordinator:
    """Uses the existing lifecycle transaction and child run execution leases."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.lifecycle = service.main_chat_runs

    def _record(
        self, run: dict[str, Any], events: list[tuple[str, dict[str, Any]]]
    ) -> dict[str, Any]:
        lifecycle = self.lifecycle
        timeline = list(run.get("timeline") or [])
        timeline.extend(
            lifecycle._timeline(kind, "Native delegation", **{**payload, "visibility": "internal"})
            for kind, payload in events
        )
        updated = lifecycle._update_run(
            run["run_id"],
            timeline=timeline,
            expected_status="running",
            expected_updated_at=run["updated_at"],
            expected_pending_approval_absent=True,
        )
        if updated is None:
            raise ValueError("delegation_parent_state_conflict")
        for kind, payload in events:
            if (
                lifecycle._append_run_event(
                    updated["run_id"],
                    kind,
                    payload,
                    visibility="internal",
                    expected_status="running",
                    expected_updated_at=updated["updated_at"],
                )
                is None
            ):
                raise ValueError("delegation_event_state_conflict")
        return updated

    def prepare(
        self,
        run_id: str,
        *,
        parent_runtime: Mapping[str, Any],
        selected_ids: set[str],
        group_scope: bool = False,
        direct_group: bool = False,
    ) -> dict[str, Any] | None:
        from .goal_runtime import goal_contract_event_payload, runtime_goal_contract

        targets = []
        for summary in self.service.list_delegation_targets().get("agents") or []:
            agent = self.service.get_agent(summary["id"])
            if group_scope and summary["id"] not in selected_ids:
                continue
            targets.append({**agent, "id": summary["id"], "kind": "agent"})
        scope = (
            self.lifecycle._transaction_scope()
            if self.lifecycle._transaction_scope
            else nullcontext()
        )
        with scope:
            run = self.service.get_run(run_id)
            if (
                run.get("kind") != "main_chat_run"
                or run.get("status") != "running"
                or run.get("pending_approval")
            ):
                raise ValueError("delegation_parent_not_running")
            existing = bound_plan(run)
            if existing:
                return existing
            plan = compile_plan(
                run=run,
                targets=targets,
                parent_runtime=parent_runtime,
                selected_ids=selected_ids,
                group_scope=group_scope,
                direct_group=direct_group,
            )
            if plan is None:
                return None
            contract = runtime_goal_contract(
                run_id=run_id,
                original_goal=run["user_goal"],
                goal_contract_template=plan["goal_contract"],
                runtime_execution_envelope=None,
                runtime_execution_metadata=None,
                messages=[],
                timeline=run["timeline"],
            )
            persisted_plan = {
                "source": SOURCE,
                "binding_id": plan["binding_id"],
                "binding_json": json.dumps(plan["binding"], ensure_ascii=False),
                "planner_decision_id": plan["planner_decision_id"],
            }
            self._record(
                run,
                [
                    ("agent.goal.contract", goal_contract_event_payload(contract)),
                    (PLAN_EVENT, persisted_plan),
                ],
            )
            return plan

    def prepare_same_goal(
        self, run_id: str, proposal: Mapping[str, Any], *, parent_runtime: Mapping[str, Any]
    ) -> dict[str, Any]:
        from .goal_runtime import runtime_goal_assessment, runtime_goal_contract

        scope = (
            self.lifecycle._transaction_scope()
            if self.lifecycle._transaction_scope
            else nullcontext()
        )
        with scope:
            run = self.service.get_run(run_id)
            if run.get("status") != "running" or run.get("pending_approval") or bound_plan(run):
                raise ValueError("delegation_parent_state_conflict")
            if proposal.get("kind") != "agent" or proposal.get("goal") != run.get("user_goal"):
                raise ValueError("delegation_requires_same_immutable_goal")
            runnable = self.service.resolve_runnable(
                runnable_id=str(proposal.get("runnable_id") or ""),
                name=str(proposal.get("name") or ""),
            )
            if (
                runnable is None
                or runnable.get("kind") != "agent"
                or not runnable.get("enabled", True)
            ):
                raise ValueError("delegation_target_unavailable")
            catalog_ids = {
                item["id"] for item in self.service.list_delegation_targets().get("agents") or []
            }
            if runnable["id"] not in catalog_ids:
                raise ValueError("delegation_target_unavailable")
            contract = runtime_goal_contract(
                run_id=run_id,
                original_goal=run["user_goal"],
                runtime_execution_envelope=None,
                runtime_execution_metadata=None,
                messages=[],
                timeline=run["timeline"],
            )
            if (
                contract is None
                or runtime_goal_assessment(contract, run["timeline"]).completed
                or not any(criterion.effectful for criterion in contract.criteria)
            ):
                raise ValueError("delegation_requires_effectful_planned_goal")
            agent = self.service.get_agent(runnable["id"])
            target = {
                "runnable_id": runnable["id"],
                "kind": "agent",
                "name": agent["name"],
                "alias": agent.get("nickname") or agent["name"],
                "goal": run["user_goal"],
                "policy": inherited_policy(agent, parent_runtime),
            }
            binding = {
                "mode": "same_goal",
                "parent_run_id": run_id,
                "original_goal": run["user_goal"],
                "parent_contract": contract.to_payload(),
                "targets": [target],
                "plan_id": contract.contract_id,
            }
            plan = {"source": SOURCE, "binding": binding, "binding_id": _digest(binding)}
            self._record(
                run,
                [
                    (
                        PLAN_EVENT,
                        {
                            "source": SOURCE,
                            "binding_id": plan["binding_id"],
                            "binding_json": json.dumps(binding, ensure_ascii=False),
                        },
                    )
                ],
            )
            return plan

    def start(
        self,
        run_id: str,
        proposals: Sequence[Mapping[str, Any]],
        *,
        task_id: str,
        group: bool,
        upstream: str,
    ) -> list[dict[str, Any]]:
        parent = self.service.get_run(run_id)
        initial_plan = bound_plan(parent)
        if (
            initial_plan is None
            or parent.get("status") != "running"
            or parent.get("pending_approval")
        ):
            raise ValueError("delegation_parent_not_running")
        validate_proposals(initial_plan, proposals)
        if any(event.get("event") == CHILDREN_EVENT for event in parent["timeline"]):
            return self.owned_children(parent)
        envelopes = self._direct_child_envelopes(parent, initial_plan)
        callbacks = []
        children = []
        scope = (
            self.lifecycle._transaction_scope()
            if self.lifecycle._transaction_scope
            else nullcontext()
        )
        with scope:
            parent = self.service.get_run(run_id)
            plan = bound_plan(parent)
            if (
                plan is None
                or plan["binding_id"] != initial_plan["binding_id"]
                or parent.get("status") != "running"
                or parent.get("pending_approval")
            ):
                raise ValueError("delegation_parent_not_running")
            validate_proposals(plan, proposals)
            old = [event for event in parent["timeline"] if event.get("event") == CHILDREN_EVENT]
            if old:
                return self.owned_children(parent)
            root_id, group_id = "", ""
            for index, target in enumerate(plan["binding"]["targets"]):
                identity = (
                    f"chat-group-dispatch:{task_id}:{index}:{target['runnable_id']}"
                    if group
                    else f"main-chat-delegation:{run_id}:{index}:{target['runnable_id']}"
                )
                payload: dict[str, Any] = {
                    "agent_id": target["runnable_id"],
                    "user_goal": target["goal"],
                    "source": "agent" if group else "delegation",
                    "client_run_id": identity,
                    "agent_override": {
                        **self.service.agent_run_async_coordinator._get_agent_private(
                            target["runnable_id"]
                        ),
                        **target["policy"],
                    },
                    "runtime_planner_entrypoint": True,
                    "upstream": upstream,
                    "project_root_group": not group,
                }
                if group:
                    payload["upstream"] += f"\n你在群内身份是：{target['alias']}"
                if target["runnable_id"] in envelopes:
                    payload["runtime_execution_envelope"] = envelopes[target["runnable_id"]]
                if group_id:
                    payload["run_group_id"] = group_id
                    payload[RUN_GROUP_ATTACHMENT_PAYLOAD_KEY] = issue_run_group_child_attachment(
                        run_group_id=group_id,
                        parent_run_id=root_id,
                        child_kind="agent_run",
                        child_runnable_id=target["runnable_id"],
                        child_identity=identity,
                    )
                child = self.service.create_agent_run_async(
                    payload, deferred_execution_start_sink=callbacks.append
                )
                if not root_id:
                    root_id, group_id = child["run_id"], child["run_group_id"]
                children.append(child)
            receipts = [
                {
                    "run_id": child["run_id"],
                    "runnable_id": target["runnable_id"],
                    "goal": target["goal"],
                    "client_request_id": child["client_request_id"],
                    "run_group_id": child["run_group_id"],
                }
                for child, target in zip(children, plan["binding"]["targets"])
            ]
            self._record(
                parent,
                [
                    (
                        CHILDREN_EVENT,
                        {
                            "source": SOURCE,
                            "parent_run_id": run_id,
                            "binding_id": plan["binding_id"],
                            "children": receipts,
                        },
                    )
                ],
            )
        for callback in callbacks:
            if self.service.get_run(run_id).get("status") != "running":
                for child in children:
                    self.service.cancel_run(child["run_id"])
                raise ValueError("delegation_parent_not_running")
            callback()
        return children

    def _direct_child_envelopes(
        self, parent: dict[str, Any], plan: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Materialize an exact process goal through the existing planner.

        Legacy terminal templates omit the exact process target. Planning
        happens before the transaction/child execution leases, so cancellation
        can still fence every deferred child effect.
        """
        from apps.shell.yachiyo_agent.runtime_execution import runtime_execution_envelope_payload

        from .goal_runtime import planned_goal_contract_payload
        from .model_intent_planning import ModelIntentClarificationResolution

        envelopes = {}
        if not plan["binding"].get("direct_group"):
            return envelopes
        for target in plan["binding"]["targets"]:
            agent = {
                **self.service.agent_run_async_coordinator._get_agent_private(
                    target["runnable_id"]
                ),
                **target["policy"],
            }
            runtime = self.service._compile_agent_runtime(agent)
            allowed = runtime["tool_policy"].get("allowed_tools") or []
            template = planned_goal_contract_payload(target["goal"], allowed_tools=allowed)
            if not any(
                set(criterion.get("required_capabilities") or []) == {"terminal.execution"}
                and (criterion.get("expected", {}).get("target") or {}).get("action")
                != "run_command"
                for criterion in template.get("criteria") or []
            ):
                continue
            timeline = []
            resolution = self.service.custom_api_agent_loop.resolve_initial_model_plan(
                agent=agent,
                original_goal=target["goal"],
                allowed_tools=list(allowed),
                run_id=parent["run_id"],
                timeline=timeline,
                budget=self.service.main_chat_model_loop._run_budget(
                    parent["run_id"], parent["timeline"]
                ),
                force_planning=True,
            )
            if resolution is None or isinstance(resolution, ModelIntentClarificationResolution):
                raise ValueError("delegation_child_planning_unresolved")
            envelope = runtime_execution_envelope_payload(
                resolution.decision, allowed_tools=allowed, full_plan=True
            )
            if not envelope:
                raise ValueError("delegation_child_planning_unresolved")
            envelopes[target["runnable_id"]] = envelope
            current = self.service.get_run(parent["run_id"])
            self._record(
                current,
                [
                    (
                        "agent.delegation.child.planned",
                        {
                            "source": SOURCE,
                            "binding_id": plan["binding_id"],
                            "runnable_id": target["runnable_id"],
                            "goal": target["goal"],
                            "decision_id": resolution.decision.decision_id,
                            "planning_events_json": json.dumps(timeline, ensure_ascii=False),
                        },
                    )
                ],
            )
        return envelopes

    def owned_children(self, parent: Mapping[str, Any]) -> list[dict[str, Any]]:
        plan = bound_plan(parent)
        bindings = [
            _payload(event)
            for event in parent.get("timeline") or []
            if (event.get("event") or event.get("event_type")) == CHILDREN_EVENT
        ]
        if plan is None or len(bindings) != 1:
            raise ValueError("delegation_child_binding_missing")
        binding = bindings[0]
        receipts = binding.get("children")
        if (
            binding.get("source") != SOURCE
            or binding.get("parent_run_id") != parent["run_id"]
            or binding.get("binding_id") != plan["binding_id"]
            or not isinstance(receipts, list)
            or len(receipts) != len(plan["binding"]["targets"])
            or any(not isinstance(receipt, Mapping) for receipt in receipts)
        ):
            raise ValueError("delegation_child_identity_conflict")
        children, seen = [], set()
        for receipt, target in zip(receipts, plan["binding"]["targets"]):
            child = self.service.get_run(receipt["run_id"])
            if (
                child["run_id"] in seen
                or child.get("kind") != "agent_run"
                or child.get("runnable_id") != target["runnable_id"]
                or child.get("user_goal") != target["goal"]
                or receipt.get("goal") != target["goal"]
                or any(
                    child.get(key) != receipt.get(key)
                    for key in ("runnable_id", "client_request_id", "run_group_id")
                )
            ):
                raise ValueError("delegation_child_identity_conflict")
            seen.add(child["run_id"])
            children.append(child)
        return children

    def cancel_children(self, run_id: str) -> None:
        parent = self.service.get_run(run_id)
        if parent.get("status") == "completed" or bound_plan(parent) is None:
            return
        if not any(event.get("event") == CHILDREN_EVENT for event in parent.get("timeline") or []):
            return
        for child in self.owned_children(parent):
            if child.get("status") not in {"completed", "failed", "cancelled"}:
                self.service.cancel_run(child["run_id"])

        plan = bound_plan(parent)
        summaries = [
            event
            for event in parent.get("timeline") or []
            if event.get("event") == SUMMARY_BOUND_EVENT
        ]
        if len(summaries) != 1:
            return
        binding = summaries[0]
        if (
            binding.get("source") != SOURCE
            or binding.get("parent_run_id") != run_id
            or binding.get("binding_id") != plan["binding_id"]
        ):
            return
        try:
            link = self.service.get_task_run_link(binding["task_id"])
            summary = self.service.get_run(link["run_id"])
        except KeyError:
            return
        starts = [event for event in summary["timeline"] if event.get("event") == "run.started"]
        if (
            link.get("session_id") == binding["session_id"]
            and summary.get("kind") == "main_chat_run"
            and summary.get("user_goal") == binding["goal"]
            and len(starts) == 1
            and (starts[0].get("metadata") or {}).get("response_context_digest")
            == binding["context_digest"]
            and summary.get("status") not in {"completed", "failed", "cancelled"}
        ):
            self.service.cancel_run(summary["run_id"])

    def settle(self, run_id: str, result: str) -> dict[str, Any]:
        from .goal_runtime import runtime_goal_assessment, runtime_goal_contract

        scope = (
            self.lifecycle._transaction_scope()
            if self.lifecycle._transaction_scope
            else nullcontext()
        )
        with scope:
            parent = self.service.get_run(run_id)
            if parent.get("status") != "running":
                return parent
            contract = runtime_goal_contract(
                run_id=run_id,
                original_goal=parent["user_goal"],
                runtime_execution_envelope=None,
                runtime_execution_metadata=None,
                messages=[],
                timeline=parent["timeline"],
            )
            assessment = runtime_goal_assessment(contract, parent["timeline"])
            if not assessment.completed:
                raise ValueError("delegation_parent_goal_unfulfilled")
            self._record(
                parent,
                [("agent.goal.assessed", {"goal_assessment": assessment.to_persisted_payload()})],
            )
            return self.service.complete_main_chat_run(run_id, result)

    def verify(self, run_id: str) -> dict[str, Any]:
        from .goal_runtime import runtime_goal_assessment, runtime_goal_contract

        scope = (
            self.lifecycle._transaction_scope()
            if self.lifecycle._transaction_scope
            else nullcontext()
        )
        with scope:
            parent = self.service.get_run(run_id)
            plan = bound_plan(parent)
            bindings = [
                event for event in parent["timeline"] if event.get("event") == CHILDREN_EVENT
            ]
            if plan is None or len(bindings) != 1 or parent.get("status") != "running":
                raise ValueError("delegation_child_binding_missing")
            completed = []
            children = self.owned_children(parent)
            for receipt, child in zip(bindings[0]["children"], children):
                if child.get("status") != "completed" or child.get("pending_approval"):
                    raise ValueError("delegation_child_goal_unfulfilled")
                if (
                    any(
                        str(child.get(key) or "") != str(receipt.get(key) or "")
                        for key in ("runnable_id", "client_request_id", "run_group_id")
                    )
                    or child.get("user_goal") != receipt["goal"]
                ):
                    raise ValueError("delegation_child_identity_conflict")
                contract = runtime_goal_contract(
                    run_id=child["run_id"],
                    original_goal=receipt["goal"],
                    goal_contract_template=None,
                    runtime_execution_envelope=None,
                    runtime_execution_metadata=None,
                    messages=[],
                    timeline=child["timeline"],
                )
                assessment = runtime_goal_assessment(contract, child["timeline"])
                # Response evidence is recorded by the native run's final
                # settlement; restore the bound ledger, never trust status.
                for event in reversed(child["timeline"]):
                    if event.get("event") == "agent.goal.assessed" and isinstance(
                        event.get("goal_assessment"), dict
                    ):
                        assessment = GoalCoordinator().restore_assessment(
                            contract, event["goal_assessment"]
                        )
                        break
                if not assessment.completed:
                    raise ValueError("delegation_child_goal_unfulfilled")
                completed.append(
                    {
                        **receipt,
                        "goal_contract_json": json.dumps(contract.to_payload(), ensure_ascii=False),
                        "goal_assessment_json": json.dumps(
                            assessment.to_persisted_payload(), ensure_ascii=False
                        ),
                    }
                )
            payload = {
                "source": SOURCE,
                "parent_run_id": run_id,
                "binding_id": plan["binding_id"],
                "children": completed,
            }
            if plan["binding"].get("mode") == "same_goal":
                parent_contract = GoalContract.from_payload(plan["binding"]["parent_contract"])
                candidate_event = {"event": COMPLETED_EVENT, **payload}
                if (
                    same_goal_evidence(
                        parent_contract, candidate_event, [*parent["timeline"], candidate_event]
                    )
                    is None
                ):
                    raise ValueError("delegation_child_contract_semantics_conflict")
            if any(event.get("event") == COMPLETED_EVENT for event in parent["timeline"]):
                return parent
            return self._record(parent, [(COMPLETED_EVENT, payload)])

    def bind_summary(self, run_id: str, task: Any) -> dict[str, Any]:
        """Bind a server-created summary Task without serializing AppState."""
        scope = (
            self.lifecycle._transaction_scope()
            if self.lifecycle._transaction_scope
            else nullcontext()
        )
        with scope:
            parent = self.service.get_run(run_id)
            plan = bound_plan(parent)
            link = self.lifecycle._task_run_links.for_run(run_id)
            if (
                plan is None
                or not plan["binding"].get("summary_required")
                or parent.get("status") != "running"
                or task.description != "Summarize the supplied context."
                or not task.response_context
                or task.task_id == (link or {}).get("task_id")
                or task.chat_session_id != (link or {}).get("session_id")
            ):
                raise ValueError("delegation_summary_binding_conflict")
            payload = {
                "source": SOURCE,
                "parent_run_id": run_id,
                "binding_id": plan["binding_id"],
                "task_id": task.task_id,
                "session_id": task.chat_session_id,
                "goal": task.description,
                "context_digest": _digest(task.response_context),
            }
            old = [
                event for event in parent["timeline"] if event.get("event") == SUMMARY_BOUND_EVENT
            ]
            if old:
                if len(old) != 1 or any(old[0].get(key) != value for key, value in payload.items()):
                    raise ValueError("delegation_summary_binding_conflict")
                return parent
            return self._record(parent, [(SUMMARY_BOUND_EVENT, payload)])

    def owned_summary(self, parent: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve a durable summary only through its exact private binding."""
        plan = bound_plan(parent)
        bindings = [
            _payload(event)
            for event in parent.get("timeline") or []
            if (event.get("event") or event.get("event_type")) == SUMMARY_BOUND_EVENT
        ]
        if plan is None or len(bindings) != 1:
            raise ValueError("delegation_summary_binding_missing")
        binding = bindings[0]
        if (
            binding.get("source") != SOURCE
            or binding.get("parent_run_id") != parent["run_id"]
            or binding.get("binding_id") != plan["binding_id"]
        ):
            raise ValueError("delegation_summary_identity_conflict")
        parent_link = self.lifecycle._task_run_links.for_run(parent["run_id"])
        link = self.service.get_task_run_link(binding["task_id"])
        if (
            not isinstance(parent_link, Mapping)
            or link.get("session_id") != binding["session_id"]
            or link.get("session_id") != parent_link.get("session_id")
            or link.get("task_id") == parent_link.get("task_id")
        ):
            raise ValueError("delegation_summary_identity_conflict")
        summary = self.service.get_run(link["run_id"])
        starts = [event for event in summary["timeline"] if event.get("event") == "run.started"]
        if (
            summary.get("kind") != "main_chat_run"
            or summary.get("user_goal") != binding["goal"]
            or len(starts) != 1
            or (starts[0].get("metadata") or {}).get("response_context_digest")
            != binding["context_digest"]
        ):
            raise ValueError("delegation_summary_identity_conflict")
        return summary

    def verify_summary(self, run_id: str) -> dict[str, Any]:
        from .goal_runtime import runtime_goal_contract

        scope = (
            self.lifecycle._transaction_scope()
            if self.lifecycle._transaction_scope
            else nullcontext()
        )
        with scope:
            parent = self.service.get_run(run_id)
            plan = bound_plan(parent)
            bindings = [
                event for event in parent["timeline"] if event.get("event") == SUMMARY_BOUND_EVENT
            ]
            if plan is None or len(bindings) != 1 or parent.get("status") != "running":
                raise ValueError("delegation_summary_binding_missing")
            binding = bindings[0]
            summary = self.owned_summary(parent)
            if summary.get("status") != "completed" or summary.get("pending_approval"):
                raise ValueError("delegation_summary_goal_unfulfilled")
            contract = runtime_goal_contract(
                run_id=summary["run_id"],
                original_goal=binding["goal"],
                runtime_execution_envelope=None,
                runtime_execution_metadata=None,
                messages=[],
                timeline=summary["timeline"],
            )
            ledgers = [
                event.get("goal_assessment")
                for event in summary["timeline"]
                if event.get("event") == "agent.goal.assessed"
                and isinstance(event.get("goal_assessment"), dict)
            ]
            if (
                contract is None
                or not ledgers
                or not GoalCoordinator().restore_assessment(contract, ledgers[-1]).completed
            ):
                raise ValueError("delegation_summary_goal_unfulfilled")
            payload = {
                key: binding[key]
                for key in (
                    "source",
                    "parent_run_id",
                    "binding_id",
                    "task_id",
                    "session_id",
                    "goal",
                    "context_digest",
                )
            }
            payload.update(
                run_id=summary["run_id"],
                goal_contract_json=json.dumps(contract.to_payload()),
                goal_assessment_json=json.dumps(ledgers[-1]),
            )
            if any(event.get("event") == SUMMARY_EVENT for event in parent["timeline"]):
                return parent
            return self._record(parent, [(SUMMARY_EVENT, payload)])


def completion_outcome(
    contract: GoalContract, event: Mapping[str, Any], timeline: Sequence[Mapping[str, Any]]
) -> tuple[ToolOutcome, dict[str, Any], str] | None:
    """Accept only the exact persisted dispatch generation and child ledgers."""
    event = _payload(event)
    parent = {"run_id": contract.run_id, "user_goal": contract.original_goal, "timeline": timeline}
    plan = bound_plan(parent)
    if (
        plan is None
        or event.get("run_id", contract.run_id) != contract.run_id
        or event.get("source") != SOURCE
        or event.get("parent_run_id") != contract.run_id
        or event.get("binding_id") != plan["binding_id"]
    ):
        return None
    if plan["binding"].get("summary_required") and not _verified_summary(plan, timeline):
        return None
    bound = [
        _payload(item)
        for item in timeline
        if isinstance(item, Mapping)
        and (item.get("event") or item.get("event_type")) == CHILDREN_EVENT
    ]
    children = event.get("children")
    if (
        len(bound) != 1
        or bound[0].get("parent_run_id") != contract.run_id
        or bound[0].get("source") != SOURCE
        or bound[0].get("binding_id") != plan["binding_id"]
        or not isinstance(children, list)
        or len(children) != len(plan["binding"]["targets"])
    ):
        return None
    receipts = bound[0].get("children")
    if (
        not isinstance(receipts, list)
        or len(receipts) != len(children)
        or any(
            not isinstance(item, Mapping)
            for item in [*receipts, *children, *plan["binding"]["targets"]]
        )
    ):
        return None
    seen = set()
    try:
        for child, receipt, target in zip(
            children, bound[0]["children"], plan["binding"]["targets"]
        ):
            if child["run_id"] in seen or any(
                child[key] != receipt[key]
                for key in ("run_id", "runnable_id", "goal", "client_request_id", "run_group_id")
            ):
                return None
            if child["runnable_id"] != target["runnable_id"] or child["goal"] != target["goal"]:
                return None
            seen.add(child["run_id"])
            child_contract = GoalContract.from_payload(json.loads(child["goal_contract_json"]))
            if (
                child_contract.run_id != child["run_id"]
                or child_contract.original_goal != target["goal"]
            ):
                return None
            if (
                not GoalCoordinator()
                .restore_assessment(child_contract, json.loads(child["goal_assessment_json"]))
                .completed
            ):
                return None
    except (KeyError, ValueError, TypeError):
        return None
    observed = {
        "state": "fulfilled",
        "target": {"kind": "orchestration", "action": "start_group_run"},
        "delegation_binding_id": plan["binding_id"],
    }
    outcome = ToolOutcome(
        tool_name="group.start",
        capabilities=("group.multi_agent",),
        status=OutcomeStatus.SUCCESS,
        reason="native_child_goals_fulfilled",
        retryable=False,
        effects=(),
        verification=VerificationStatus.VERIFIED,
        user_action=None,
        recovery_hints=(),
        provenance={"source": SOURCE},
        raw=observed,
    )
    return outcome, observed, plan["binding"]["plan_id"]


def _verified_summary(plan: Mapping[str, Any], timeline: Sequence[Mapping[str, Any]]) -> bool:
    bindings = [
        _payload(event)
        for event in timeline
        if (event.get("event") or event.get("event_type")) == SUMMARY_BOUND_EVENT
    ]
    completions = [
        _payload(event)
        for event in timeline
        if (event.get("event") or event.get("event_type")) == SUMMARY_EVENT
    ]
    if len(bindings) != 1 or len(completions) != 1:
        return False
    binding, completion = bindings[0], completions[0]
    keys = (
        "source",
        "parent_run_id",
        "binding_id",
        "task_id",
        "session_id",
        "goal",
        "context_digest",
    )
    if (
        binding.get("source") != SOURCE
        or binding.get("parent_run_id") != plan["binding"]["parent_run_id"]
        or binding.get("binding_id") != plan["binding_id"]
        or any(binding.get(key) != completion.get(key) for key in keys)
    ):
        return False
    try:
        contract = GoalContract.from_payload(json.loads(completion["goal_contract_json"]))
        return (
            contract.run_id == completion["run_id"]
            and contract.original_goal == binding["goal"]
            and not any(criterion.effectful for criterion in contract.criteria)
            and GoalCoordinator()
            .restore_assessment(contract, json.loads(completion["goal_assessment_json"]))
            .completed
        )
    except (KeyError, TypeError, ValueError):
        return False


def same_goal_evidence(
    contract: GoalContract, event: Mapping[str, Any], timeline: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]] | None:
    if completion_outcome(contract, event, timeline) is None:
        return None
    plan = bound_plan(
        {"run_id": contract.run_id, "user_goal": contract.original_goal, "timeline": timeline}
    )
    if (
        plan["binding"].get("mode") != "same_goal"
        or plan["binding"].get("parent_contract") != contract.to_payload()
    ):
        return None
    children = _payload(event)["children"]
    if len(children) != 1:
        return None
    child = children[0]
    child_contract = GoalContract.from_payload(json.loads(child["goal_contract_json"]))
    child_assessment = GoalCoordinator().restore_assessment(
        child_contract, json.loads(child["goal_assessment_json"])
    )

    def semantics(criterion: Any) -> dict[str, Any]:
        return {
            key: value
            for key, value in criterion.to_payload().items()
            if key not in {"criterion_id", "description"}
        }

    mapped = []
    for parent_criterion in contract.criteria:
        matches = [
            criterion
            for criterion in child_contract.criteria
            if semantics(criterion) == semantics(parent_criterion)
        ]
        if len(matches) != 1:
            return None
        for evidence in child_assessment.evidence:
            if evidence.criterion_id == matches[0].criterion_id and evidence.verified:
                mapped.append(
                    {
                        **evidence.to_payload(),
                        "evidence_id": _digest(
                            [contract.run_id, child["run_id"], evidence.evidence_id]
                        ),
                        "run_id": contract.run_id,
                        "contract_id": contract.contract_id,
                        "criterion_id": parent_criterion.criterion_id,
                    }
                )
    return mapped
