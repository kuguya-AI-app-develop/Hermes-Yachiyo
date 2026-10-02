"""Agent prompt context helpers for the runtime execution loop."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any


def agent_output_contract_rules(contract: Any) -> str:
    value = str(contract or "chat").strip().lower() or "chat"
    rules = {
        "chat": (
            "Return a direct chat response. Include enough detail for the user and the main model to understand "
            "what was done, what failed, or what needs approval. Do not create an artifact unless the user goal "
            "explicitly asks for one."
        ),
        "markdown": (
            "Return polished Markdown with clear sections. If the user explicitly asks for a saved document and "
            "artifact.write is allowed, write the Markdown as an artifact and mention the artifact path; otherwise "
            "include the Markdown inline."
        ),
        "report": (
            "Return a concise report with task, result, evidence, risks, and next steps. Use artifacts only when "
            "the user asks for a saved report or the task naturally produces a file."
        ),
        "diff": (
            "Return a change-oriented answer: summarize intended code changes and include a unified diff or patch "
            "text only when the user asked for a patch. Do not call workspace.write_patch merely because the output "
            "contract is diff; call it only when the user goal asks you to modify workspace files and the tool is "
            "allowed. If no file change is requested, provide code inline."
        ),
        "artifacts": (
            "Prefer producing named artifacts for concrete deliverables. If artifact.write is allowed, write each "
            "deliverable artifact and mention its path in the final answer. If artifact.write is not allowed, state "
            "that no artifact could be written and provide the content inline."
        ),
    }
    return f"Contract: {value}\nRules: {rules.get(value, rules['chat'])}"


def user_goal_from_agent_messages(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "")
        match = re.search(r"^# User Goal\s*\n(.*?)(?:\n# |\Z)", content, re.DOTALL | re.MULTILINE)
        if match:
            return match.group(1).strip()
    return ""


def agent_goal_disallows_tool(user_goal: str, tool_name: str) -> str:
    text = " ".join(str(user_goal or "").split()).strip().lower()
    if not text:
        return ""

    no_file_patterns = (
        r"不(?:需要|用|必|要).{0,12}(?:创建|保存|写入|写|修改|改动).{0,8}文件",
        r"无需.{0,12}(?:创建|保存|写入|写|修改|改动).{0,8}文件",
        r"不(?:创建|保存|写入|修改|改动).{0,8}文件",
        r"只(?:需要)?(?:展示|给出|贴出).{0,12}(?:代码|内容|方案)",
        r"代码完整展示即可",
        r"do not (?:create|save|write|modify|change).{0,24}file",
        r"don't (?:create|save|write|modify|change).{0,24}file",
        r"without (?:creating|saving|writing|modifying|changing).{0,24}file",
        r"no file (?:changes?|writes?|creation)",
        r"(?:inline|show|display) (?:code|content) only",
    )
    no_command_patterns = (
        r"不(?:需要|用|必|要).{0,12}(?:运行|执行).{0,12}(?:命令|脚本|代码|测试)?",
        r"无需.{0,12}(?:运行|执行).{0,12}(?:命令|脚本|代码|测试)?",
        r"不要.{0,12}(?:运行|执行).{0,12}(?:命令|脚本|代码|测试)?",
        r"do not (?:run|execute)",
        r"don't (?:run|execute)",
        r"without (?:running|executing)",
        r"no command execution",
    )
    explicit_terminal_patterns = (
        r"(?:必须|需要|请求|调用|使用).{0,24}terminal\.run",
        r"只(?:需要|使用|调用).{0,12}terminal\.run",
        r"(?:must|should|please).{0,24}(?:use|call|request).{0,24}terminal\.run",
        r"(?:only|just).{0,12}(?:use|call).{0,12}terminal\.run",
    )

    if tool_name in {"workspace.write_patch", "artifact.write"} and any(re.search(pattern, text) for pattern in no_file_patterns):
        return "用户目标明确要求不要创建、保存或修改文件；请改为 inline 交付内容。"
    if (
        tool_name == "terminal.run"
        and not any(re.search(pattern, text) for pattern in explicit_terminal_patterns)
        and any(re.search(pattern, text) for pattern in no_command_patterns)
    ):
        return "用户目标明确要求不要运行命令或脚本；请改为给出代码、示例或说明。"
    return ""


class AgentContextBuilder:
    """Builds the model-visible Agent context without owning execution state."""

    def __init__(
        self,
        *,
        compile_agent_runtime: Callable[[dict[str, Any]], dict[str, Any]],
        load_agent_skills: Callable[[list[str]], list[dict[str, Any]]],
        long_term_memory_context: Callable[[], str],
        operating_doctrine: str,
        agent_desk_context: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        self._compile_agent_runtime = compile_agent_runtime
        self._load_agent_skills = load_agent_skills
        self._long_term_memory_context = long_term_memory_context
        self._operating_doctrine = operating_doctrine
        self._agent_desk_context = agent_desk_context

    def build(
        self,
        agent: dict[str, Any],
        user_goal: str,
        upstream: str = "",
        *,
        skills: list[dict[str, Any]] | None = None,
    ) -> str:
        skills = skills if skills is not None else self._load_agent_skills(agent.get("skill_ids") or [])
        runtime = self._compile_agent_runtime(agent)
        tool_policy = runtime["tool_policy"]
        workspace_policy = runtime["workspace_policy"]
        skill_blocks = []
        for skill in skills:
            summary = str(skill.get("content_summary") or skill.get("description") or "").strip()
            skill_blocks.append(
                f"- skill_id: {skill['skill_id']}\n"
                f"  name: {skill['name']}\n"
                f"  description: {skill.get('description') or 'No description.'}\n"
                f"  summary: {summary[:600] or 'No summary.'}\n"
                f"  assets/templates: {', '.join(skill.get('asset_paths') or []) or 'none'}"
            )
        mounted_skills = (
            "Skill summary index (progressive disclosure). "
            "Call skill.read with skill_id before relying on detailed Skill instructions.\n"
            + "\n".join(skill_blocks)
            if skill_blocks
            else "No mounted skills."
        )
        memory_context = self._long_term_memory_context()
        sections = [
            f"# Agent\nName: {agent['name']}\nNickname: {agent.get('nickname') or agent['name']}\nCategory: {agent.get('category') or 'custom'}",
            f"# Functional Instructions\n{agent.get('instructions') or 'No extra functional instructions.'}",
            f"# Persona Prompt\n{agent.get('persona_prompt') or 'No persona override.'}",
            f"# Operating Doctrine\n{self._operating_doctrine}",
            f"# Mounted Skills\n{mounted_skills}",
            f"# Long-term Memory\n{memory_context}",
            "# Runtime\n"
            "Runtime: Oha Agent Runtime\n"
            f"Allowed tools: {', '.join(tool_policy.get('allowed_tools') or [])}\n"
            f"Approval required: {json.dumps(tool_policy.get('approval_required') or {}, ensure_ascii=False)}\n"
            f"Workspace: {json.dumps(workspace_policy, ensure_ascii=False)}\n"
            "For shell, git, tests, builds, package managers, or command-line searches, "
            "prefer terminal.run when allowed. Combine related read-only commands when helpful. "
            "Use workspace tools only within configured scopes. If no trusted working directory "
            "is available, ask the user to configure one instead of repeating failed workspace calls. "
            "Respect approvals and workspace scopes for every tool.",
        ]
        if self._agent_desk_context is not None:
            desk_context = self._agent_desk_context(agent).strip()
            if desk_context:
                sections.append(f"# Agent Desk\n{desk_context}")
        sections.extend(
            [
                f"# Upstream Context\n{upstream or 'None'}",
                f"# User Goal\n{user_goal}",
                f"# Output Contract\n{agent_output_contract_rules(agent.get('output_contract'))}",
            ]
        )
        return "\n\n".join(sections)
