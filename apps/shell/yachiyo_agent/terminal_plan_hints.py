"""Terminal command hints for capability-first planning."""

from __future__ import annotations

import re


def terminal_command_hint(text: str) -> dict[str, str]:
    value = str(text or "").strip()
    if not value:
        return {}
    patterns = (
        r"(?:帮我|请|麻烦|能否|能不能|可以)?\s*"
        r"(?:打开|启动|开启|拉起|open|launch|start)?\s*"
        r"(?:一个|一款|任意|任何|默认|可用|合适|适合|an?\s+|the\s+|any\s+)?"
        r"(?:终端|命令行|terminal|shell)\s*(?:里|中|上|内|app|应用)?\s*"
        r"(?:运行|执行|跑|run|execute)\s*(?P<command>[^。！？!?]+)",
        r"(?:运行|执行|跑|run|execute)\s+(?P<command>[^。！？!?]+?)\s*"
        r"(?:在|用|通过|in|with|using)\s*(?:终端|命令行|terminal|shell)",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        command = _clean_terminal_command(match.group("command"))
        if command:
            return {"command": command}
    bare_match = re.search(
        r"(?:^|[\s，,。])(?:帮我|请|麻烦|能否|能不能|可以)?\s*"
        r"(?:运行|执行|跑|run|execute)\s+(?P<command>[^。！？!?]+)",
        value,
        flags=re.IGNORECASE,
    )
    if bare_match:
        command = _clean_terminal_command(bare_match.group("command"))
        if command and _looks_like_shell_command(command):
            return {"command": command}
    return {}


def _clean_terminal_command(value: str) -> str:
    command = str(value or "").strip()
    command = re.split(
        r"\s*(?:并|然后|再|之后|随后|并且)\s*(?:把|将)?\s*"
        r"(?:命令)?(?:结果|输出|返回值)?\s*"
        r"(?:告诉|展示|显示|打印|总结|概括|解释|说明|汇报|发给|给我看).*$",
        command,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    command = re.split(
        r"\s+(?:看一下|看下|看看|查看|检查|列出|显示|确认)(?:一下|下)?"
        r"(?:当前目录|当前文件夹|当前路径|当前工作区|结果|输出)?$",
        command,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    command = re.split(
        r"\s+(?:and|then|to)\s+"
        r"(?:show|see|check|list|print|display|tell|summari[sz]e|explain|report|describe)\b.*$",
        command,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    command = re.sub(r"\s*(?:一下|下|吧|吗|嘛|呢)$", "", command, flags=re.IGNORECASE).strip()
    quote_pairs = {"「": "」", "『": "』", "“": "”", '"': '"', "'": "'", "`": "`"}
    if command and command[0] in quote_pairs and command[-1] == quote_pairs[command[0]]:
        command = command[1:-1]
    if re.match(r"(?:the\s+exact\s+command|the\s+following\s+command|these\s+exact\s+inputs|command\s+payload)\b", command, flags=re.IGNORECASE):
        return ""
    if command in {"", "起来", "一下", "下"}:
        return ""
    return command


def _looks_like_shell_command(command: str) -> bool:
    value = str(command or "").strip()
    if not value:
        return False
    first = re.split(r"[\s;&|<>]", value, maxsplit=1)[0].strip()
    # Shell punctuation inside a description does not make that description
    # an executable command. Leave prose and structured options to planning.
    if first.lower() in {"the", "a", "an", "these", "following", "exact", "command", "commands"}:
        return False
    if re.search(r"\b(?:shell|timeout_seconds)\s*=", value):
        return False
    if re.search(r"(?:&&|\|\||[|;<>])", value):
        return True
    if first.startswith(("./", "../", "/", "~/")):
        return True
    return first.lower() in {
        "awk",
        "brew",
        "cat",
        "cd",
        "chmod",
        "cp",
        "curl",
        "df",
        "docker",
        "du",
        "echo",
        "find",
        "git",
        "go",
        "grep",
        "head",
        "ls",
        "make",
        "mkdir",
        "mv",
        "node",
        "npm",
        "pnpm",
        "ps",
        "printf",
        "pwd",
        "python",
        "python3",
        "pytest",
        "rg",
        "rm",
        "sed",
        "tail",
        "touch",
        "uv",
        "wget",
        "yarn",
    }
