# Oha-Yachiyo 使用手册

适用对象：第一次安装并使用 Oha-Yachiyo 的 macOS 用户。

Oha-Yachiyo 是桌面优先的本地个人 Agent 应用。当前版本使用内置 Native Agent Runtime 执行任务，不要求用户安装外部执行内核。应用保留完整对话窗口、Bubble 悬浮入口、Live2D 角色入口、Agent Studio、Workflow、Run Detail、审批 UI、主动关怀、语音播报、资源导入、诊断、备份和卸载管理。

## 1. 首次启动

1. 将 `Oha-Yachiyo.app` 放入 `/Applications`。
2. 打开应用。若 macOS 因未知开发者 / Gatekeeper 阻止首次打开，请在 Finder 中按住 Control 点击 `Oha-Yachiyo.app` 并选择“打开”，或到系统设置“隐私与安全性”中允许打开。
3. 首次启动后进入主窗口或配置向导。
4. 配置默认对话模型。
5. 需要图片附件、主动关怀或语音时，再配置对应能力。

如果没有配置默认对话模型，Chat、主动关怀和 Agent 执行入口会保留，但会返回结构化的模型配置提示。入口不会被隐藏或静默禁用。

## 2. 配置模型

在模型配置页维护模型 Profile。常用配置包括：

- Provider 或自定义 OpenAI-compatible Base URL。
- 模型名称。
- API Key。
- 对话能力。
- 图片输入能力。

点击测试连接后，应用会用真实请求验证 Profile。测试失败不会把模型保存为可用 Profile。

使用自签名或 ad-hoc 安装包时，应用更新、重装或重签后，macOS 可能不再允许新版本读取旧的钥匙串凭据。若提示“模型凭据不可访问”，请到「模型配置」重新填写并保存 API Key；若是 Agent 的自定义 API，则在 Agent Studio 的对应 Agent 中重新保存。随后重新测试连接。恢复过程不会读取或显示旧 API Key 的明文；数据库成功切换到新凭据后，应用才会尝试清理旧钥匙串项。

建议至少配置：

- 默认对话模型：用于 Chat、Agent Studio、Workflow、群聊和自动委派总结。
- 图片输入模型或支持图片的默认模型：用于图片附件和主动桌面观察。

## 3. 工作空间

应用默认使用本地工作空间保存配置、数据库、附件、导入资源和日志：

```text
~/.oha-yachiyo/
```

工作空间中包含：

- 聊天数据库。
- Task 与 Run 映射。
- RunEvent replay 数据。
- Agent Studio 与 Workflow 配置。
- 导入的 Live2D 与 TTS 资源。
- 附件缓存和运行日志。

## 4. 对话窗口

Chat Window 支持：

- 文本对话。
- 多轮上下文。
- 新建、切换和删除会话。
- 停止正在生成的任务。
- 图片附件和图片预览。
- 工具审批卡。
- 从消息跳转到对应 Run Detail。

发送消息后，产品级 Task 由 TaskRunner 管理，NativeAgentExecutor 会创建对应 Native Run，并把执行进度投影回 ChatSession。用户可见 transcript 仍以 ChatSession 为准，Run Detail 用于诊断和回放执行事实。

## 5. 图片附件

点击 Chat 输入区或标题栏中的图片按钮可以添加图片。图片会作为附件进入 Chat API，并根据模型能力传给 NativeRunEngine。

如果当前模型不支持图片输入，应用会返回结构化不支持错误，而不是删除图片入口。建议使用正常尺寸截图或图片，避免极小图片被上游模型拒绝。

## 6. 群聊与自动委派

群聊和自动委派继续使用原有 Chat UI 与 ChatSession metadata：

- 群聊会保留成员、派发目标、Agent 回复和总结任务。
- 自动委派会调用已保存、已启用的 Agent 或 Workflow。
- 总结结果会回到主 ChatSession。
- 委派 Run、总结 Run 和主聊天 Task 会通过 TaskRunLink 和 RunGroup 保持可追踪。

群聊和自动委派由 NativeRunEngine 执行，不需要额外执行后端。

## 7. Agent Studio

Agent Studio 用于管理持久 Agent、技能挂载和运行记录。

常用操作：

- 创建或编辑 Agent。
- 配置 instructions、persona、output contract、workspace policy 和 tool policy。
- 挂载或移除 Skill。
- Quick Run 创建 Agent Run。
- 在 Run Detail 查看 timeline、result、artifacts、approval 和 replay events。

高风险工具默认需要审批。批准、拒绝、取消和超时都是幂等操作，重复点击不会重复执行工具。

## 8. Workflow

Workflow Studio 用于把 Agent、审批节点和 artifact 节点组成流程。

常用操作：

- 新建 Workflow。
- 添加 Agent 节点。
- 添加人工审批节点。
- 添加 artifact 输出节点。
- 保存后运行 Workflow。
- 在 Run Detail 查看父 Workflow Run、子 Agent Run、RunGroup、Workflow steps 和 RunEvent replay。

Workflow 子 Agent 等待审批时，父 Run 会保持等待状态。批准、拒绝或取消子 Run 后，父 Workflow 会通过共享的恢复边界继续或收口。

## 9. Run Detail 与审批

Run Detail 展示 Native Run 的执行事实：

- Run 状态。
- Task link。
- RunGroup。
- Timeline。
- RunEvent replay。
- Pending approval。
- Artifacts。
- Rerun 入口。

审批 UI 会展示工具名称和脱敏输入摘要。原始敏感输入只保留在后端私有审批上下文中，UI 和 replay 会使用清洗后的公开 payload。

## 10. Activity

Activity 页面展示用户可见活动流。它不决定 Run 生命周期，只消费 Chat、Task、Run 和系统事件的投影。

Activity 支持：

- 查看最近任务。
- 打开关联 Run Detail。
- 查看失败、取消、审批等待等状态。
- 删除不需要保留的活动项。

## 11. Bubble 模式

Bubble 是轻量悬浮入口。它可以显示：

- 最近会话摘要。
- 未读状态。
- 处理中状态。
- 最近回复。
- 快捷输入入口。

Bubble 与主 Chat Window 共享同一套 ChatSession、TaskRunner 和 NativeRunEngine 执行路径。

## 12. Live2D 模式

Live2D 是角色桌面入口。首次使用需要导入 Live2D 资源 ZIP。

导入步骤：

1. 打开 Live2D 设置。
2. 选择资源包 ZIP。
3. 等待模型路径、表达式和动作信息识别完成。
4. 保存设置。
5. 重新打开 Live2D 表现态。

即使没有导入模型，Live2D 入口也会显示 fallback shell。导入资源后，Live2D 支持回复气泡、快捷输入、资源提示、预览 fallback 和本地模型渲染。

## 13. 主动关怀与本地截图

主动关怀会按间隔读取桌面截图，通过图片模型判断是否需要提醒用户，然后生成适合桌面入口展示的消息。

使用步骤：

1. 启用主动桌面观察。
2. 设置观察间隔和触发概率。
3. 配置可用图片模型。
4. 如果需要语音，配置主动关怀语音。
5. 保存后可点击测试触发。

macOS 可能会要求屏幕录制权限。请到“系统设置 -> 隐私与安全性 -> 屏幕录制”允许 Oha-Yachiyo。权限不足时，应用会返回结构化错误并保留用户消息和任务语义。

## 14. 语音与手动 TTS

主动关怀语音和手动 TTS 支持多种 provider：

- 不启用语音。
- HTTP TTS。
- 本地命令 TTS。
- GPT-SoVITS 本地服务。
- OpenAI-compatible 或其他已支持的语音来源。

本地命令 TTS 会在清洗后的子进程环境中执行，不会继承 API Key、token、云凭据或 SSH agent socket。

GPT-SoVITS 音色包导入后，页面会填入权重、参考音频、语言、切分方式和 API 地址。首次加载权重可能较慢，建议给第一次测试留出更长等待时间。

## 15. 工具与安全边界

本地工具通过 NativeRunEngine 的 ToolBroker 执行。默认安全边界包括：

- workspace 读写范围限制。
- `workspace.write_patch` 只接受单文件 UTF-8 unified diff patch。
- `terminal.run` 默认需要审批。
- shell 模式需要显式批准。
- terminal 超时会终止进程组。
- stdout、stderr、artifact、日志、crash 和 UI 错误会清洗明显 secret。

单次 Run 默认允许 50 次模型调用、100 次工具调用、50 次实际 terminal 执行，时长预算为 600 秒。审批等待和实际执行分别计数，同一对话的新 Run 使用新的预算。

需要调整 terminal 预算时，在启动应用前设置 `OHA_YACHIYO_AGENT_MAX_TERMINAL_CALLS`，接受 0–1000；0 禁止 terminal 实际执行。工具循环上限默认 200，可用 `OHA_YACHIYO_AGENT_TOOL_ITERATION_LIMIT` 设置为 10–1000。非整数值使用默认值，超出范围的整数值按上下界限制。即使提高其中一项，其余模型、工具、时长、workspace 和审批限制仍生效。

工具失败会进入 Run projection 和 RunEvent replay，但不会把明显 secret 写入用户可见输出。

长期记忆使用确认后召回：Agent 工具提取的内容先成为不可召回候选，只有与原 Run、用户消息、内容哈希和作用域完全匹配的用户确认才会启用。手动创建的记忆视为用户明确操作。删除与停用会立即阻止 Agent、Chat 和 Workflow 召回。当前“删除”是保留审计事件的软删除；不会重写所有历史聊天、RunEvent 或备份中的原始文本。如需介质级彻底擦除，还需按数据保留策略删除相关聊天/运行记录与备份。

## 16. 诊断

诊断页用于查看当前应用、Bridge、模型、桌面权限、资源和运行缓存状态。

常用检查：

- Bridge 是否运行在 loopback 地址。
- 模型 Profile 是否可用。
- 截图权限是否可用。
- Live2D 资源路径是否有效。
- TTS 测试结果。
- release-like build 下 debug routes 是否关闭。

维护者排查发布包问题时，可以生成脱敏诊断包。诊断包会收集本轮 RC、signoff、release readiness、release smoke 和日志类文本，并在写入 zip 前清洗明显 secret：

```bash
SHORT_COMMIT="$(git rev-parse --short=8 HEAD)"
python scripts/collect_release_diagnostics.py \
  --label "$SHORT_COMMIT" \
  --include-app-logs \
  --output-zip "tmp/oha-yachiyo-diagnostics-${SHORT_COMMIT}.zip"
```

如果需要确认当前发布用户路径覆盖度，维护者可以查看 `tmp/rc-verification-<short-commit>-release-smoke.json` / `.md`，或运行 `python scripts/refresh_local_rc_signoff.py --print-status`。这类诊断材料只用于排障和签核沟通，不会替代最终人工确认。

## 17. 备份与恢复

应用维护页可以生成配置和工作空间备份。备份内容包括：

- Oha-Yachiyo 应用配置。
- 工作空间数据。
- 聊天数据库。
- Agent Studio 与 Workflow 配置。
- 导入资源。
- 附件缓存和日志。

恢复备份会把备份内容导回本地配置和工作空间。恢复前建议先确认当前数据是否需要另存。

## 18. 卸载

卸载页会先生成可删除清单，并要求输入确认短语。支持范围包括：

- 删除应用配置和工作空间。
- 选择是否保留配置快照。
- 选择是否同时清理本地语音服务目录与基础模型。

卸载预览不会直接删除数据，必须显式确认。

## 19. 常见问题

### 未配置模型时还能使用应用吗？

可以。成熟功能入口会保留，但需要模型执行的路径会返回结构化模型配置提示。

### 图片不可用怎么办？

先确认默认模型或图片模型支持图片输入，并完成连接测试。请避免使用极小图片。

### 审批卡重复点击会重复执行工具吗？

不会。审批、拒绝、取消和超时都通过幂等边界处理，重复操作不会重复执行已 claim 的工具。

### Live2D 没有显示角色怎么办？

确认资源包已导入，并且设置页显示资源路径有效。保存模型路径后重新打开 Live2D 表现态。

### TTS 没声音怎么办？

确认语音 Provider 已启用并通过手动测试。本地服务类 TTS 还需要确认服务进程、权重、参考音频和 API 地址可用。

### 主动关怀不触发怎么办？

确认主动桌面观察已开启，触发概率不是 0，macOS 屏幕录制权限已授权，并等待至少一个观察间隔。
