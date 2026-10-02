# Oha-Yachiyo macOS 打包与发布

本文记录当前 macOS DMG 打包链路。目标是把 Yachiyo 做成独立桌面应用，而不是依赖开发机上的 Python、Node 或源码工作树。

## 发布渠道

- `main` 分支发布正式版 DMG。
- `oha-develop` 分支发布 Oha 实验版 DMG，并在 GitHub Release 中标记为 prerelease。
- `develop` 分支保留给彻底重构前的旧版发布线，不触发 Oha DMG。
- Oha-Yachiyo 使用内置 Native Agent 运行时；正式版/实验版只由本仓库的发布渠道决定。

## 本地打包

```bash
python -m pip install -e ".[packaging]"
npm ci --prefix apps/frontend
python scripts/build_release_candidate_artifacts.py --channel experimental --repository kuguya-AI-app-develop/Hermes-Yachiyo
```

输出位置：

- Python 后端：`dist/backend/oha-yachiyo-backend`
- Electron DMG：`dist/electron/*.dmg`

## 免费自签名打包

免费试用阶段可以使用自签名证书让 `.app` 具备签名完整性，但不要自签名 `.dmg`。实测自签名且未公证的 DMG 会被 macOS 在挂载前直接拒绝，提示“Apple 无法验证此 DMG 是否包含可能危害 Mac 安全或泄漏隐私的恶意软件”。当前策略是：`.app` 自签名，`.dmg` 保持未签名，让用户至少能挂载安装包；首次启动应用时仍会看到未知开发者 / Gatekeeper 提示，需要使用 Finder 的 Control-click -> Open，或在系统设置的“隐私与安全性”中允许打开。

本地生成自签名证书：

```bash
scripts/create_macos_self_signed_cert.sh
```

该脚本会在 `dist/signing/` 下生成 p12、base64 和 GitHub Secrets 辅助文件，并默认导入当前用户的 login keychain。`dist/` 已被 `.gitignore` 排除，不要把这些文件提交到仓库。

需要在 GitHub 仓库 Secrets 中配置：

```text
MACOS_CODESIGN_CERTIFICATE_BASE64
MACOS_CODESIGN_CERTIFICATE_PASSWORD
MACOS_CODESIGN_IDENTITY
```

本地使用自签名证书构建 DMG：

```bash
python scripts/build_backend.py --clean
scripts/build_macos_self_signed_dmg.sh "Oha-Yachiyo Self Signed"
```

`MACOS_CODESIGN_IDENTITY` 是证书名，不是发布渠道名。自签名阶段建议使用中性的 `Oha-Yachiyo Self Signed`；`main` 和 `oha-develop` 可以共用同一张自签名证书。发布渠道由分支、release tag、DMG 文件名和下载链接区分。

CI 中如果检测到 `MACOS_CODESIGN_CERTIFICATE_BASE64`，会自动导入证书、构建 `.app`、签名 `.app`，再打包未签名 `.dmg`。如果没有配置该 Secret，workflow 会正常使用免证书发布路径，不会因缺少 Developer ID 或公证凭据失败。Electron 可能为 `.app` 生成 ad-hoc 签名；workflow 会对最终 DMG 做只读挂载检查，在 latest JSON 中分别记录 `signing=unsigned` 和实际 `signature_kind=adhoc|unsigned`，不把 ad-hoc 冒充成 Developer ID。

## Developer ID 与 notarization

面向普通用户的正式分发使用 Apple Developer ID Application 证书、Hardened Runtime、secure timestamp 和 Apple notarization。公证通过后，workflow 会把票据 staple 到 DMG，并用 `stapler validate` 与 Gatekeeper `spctl` 再验证一次。自签名和完全 unsigned 构建仍作为没有 Apple 凭据时的回退，不会被删除。

Developer ID 模式复用以下签名 Secrets；其中 `MACOS_CODESIGN_IDENTITY` 必须是完整的 `Developer ID Application: ...` identity：

```text
MACOS_CODESIGN_CERTIFICATE_BASE64
MACOS_CODESIGN_CERTIFICATE_PASSWORD
MACOS_CODESIGN_IDENTITY
```

App Store Connect API Key 使用以下 Secrets：

```text
APPLE_NOTARY_KEY_BASE64
APPLE_NOTARY_KEY_ID
APPLE_NOTARY_ISSUER_ID
```

`APPLE_NOTARY_KEY_BASE64` 是 `.p8` 私钥的 base64 内容；`APPLE_NOTARY_ISSUER_ID` 对 Team API Key 必填，对 Individual API Key 留空。workflow 只把私钥解码到 runner 临时目录，权限设为 `0600`，公证结束后立即删除。密钥不会写入 release metadata、命令输出或应用包。

当 identity 是 Developer ID 时，只要签名证书或 notary key 配置不完整，release job 就会失败，不会静默降级成自签名发布。完整流程是：

```text
unsigned app directory -> Developer ID sign + timestamp -> DMG -> notarytool submit --wait -> notarytool log -> stapler staple/validate -> spctl
```

成功创建 submission 后，提交结果与 Apple 审计日志分别保存为 `release/notarization.json` 和 `release/notarization-log.json`。stapling 与 Gatekeeper 检查通过后还会生成 `release/notarization-evidence.json`，把 submission id、`Accepted` 状态和最终 stapled DMG 的 SHA256 绑定在一起；三份证据会跟随 CI artifact 上传。release verifier 会把 evidence hash 与 latest JSON 的 `sha256` 交叉校验，因此不能只改 `signing` 标签来伪装成已公证构建。鉴权或网络错误如果没有产生 submission id，job 会以受控错误直接失败。latest JSON 的 `signing` 值为 `developer-id-app-notarized-dmg`；自签名与无签名回退仍分别使用 `self-signed-app-unsigned-dmg` 和 `unsigned`。

## 真实 macOS VM release smoke

release workflow 可以把已打包的 Guest Provider 和 Host Bridge 安装到独立 macOS VM，运行不会抢占 CI host 键鼠的 `--public-release` provider 全链。需要同时配置以下 GitHub Secrets：

```text
OHA_YACHIYO_VIRTUAL_DESKTOP_SSH_TARGET
OHA_YACHIYO_VIRTUAL_DESKTOP_SSH_PRIVATE_KEY
OHA_YACHIYO_VIRTUAL_DESKTOP_SSH_KNOWN_HOSTS
```

三个 Secrets 全空时，workflow 会写出 `release/virtual-desktop-provider-smoke.json`，状态为 `not_configured`，并继续现有免费发布；只配置其中一部分会直接失败，避免误以为已经收集真实 VM evidence。完整配置时，workflow 使用临时 `0600` 私钥、固定 `BatchMode=yes`、`IdentitiesOnly=yes`、`StrictHostKeyChecking=yes` 和显式 `UserKnownHostsFile`，调用 `install_virtual_desktop_guest.py` 安装当前构建产物，再由 `smoke_oha_desktop_agent_release.py --public-release` 验证 app 发现/打开、UI 读取、click/type/shortcut、结果验证和 provider lifecycle。私钥、known_hosts、token、manifest 与安装报告只保留在 runner 临时目录并在步骤结束时删除；归档内容只有脱敏的 `release/virtual-desktop-provider-smoke.json`。该报告同时作为第四个 source report 传给 `summarize_release_smoke.py`：`not_configured` 不会产生通过证据；真实 VM 报告只有同时满足 release backend、provider contract 和 conformance 条件时才会补齐 `oha_real_virtual_desktop_backend`。

## 打包结构

Electron packaged 模式会启动：

```text
Oha-Yachiyo.app/Contents/Resources/backend/oha-yachiyo-backend
```

这由 `apps/frontend/electron/main.ts` 中的 packaged backend 路径控制。`scripts/build_backend.py` 使用 PyInstaller 把 `apps.desktop_backend.app` 冻结为单文件后端，`apps/frontend/electron-builder.yml` 再把它放进 Electron Resources。

嵌入在 `.app` 内的 backend 与单独分发/验收时使用的 standalone backend 现在都固定使用
稳定 identifier `io.github.arisataki.oha-yachiyo.backend`，并与 `Oha-Yachiyo.app`
复用同一条长期签名身份链。backend 的专用 entitlement 只保留最小所需的
`com.apple.security.cs.disable-library-validation`，原因是要让同一 identifier +
同一长期签名身份在重建后仍能稳定命中同一组 Keychain ACL。`identity=-` 或其他
unsigned / ad-hoc fallback 只适合无证书构建回退；它们不保证跨版本继续访问同一批
Keychain 凭据。只有真正的 self-signed 证书或 Apple Developer ID 签名，才保证这条
跨版本 Keychain 访问路径稳定。

同一发布构建还会生成可复制到 macOS VM 的独立桌面 guest-agent：

```text
Oha-Yachiyo.app/Contents/Resources/desktop-provider/oha-yachiyo-desktop-provider
Oha-Yachiyo.app/Contents/Resources/desktop-provider/oha-yachiyo-virtual-desktop-bridge
```

`scripts/build_virtual_desktop_guest.py` 使用独立 PyInstaller work/dist 目录构建它；
guest-agent 与 host-side SSH bridge 都不要求 VM/Host 保留源码仓库或 Python 环境。详细的 VM marker、token file 和
SSH bridge 部署方式见 `docs/desktop-provider-contract.md`。

打包版默认 Bridge 地址是 `http://127.0.0.1:18420`，开发模式默认是
`http://127.0.0.1:8420`。如果打包版启动时发现 `18420` 已被占用，会临时
分配一个空闲本地端口并传给内置 Python backend，避免连接到本地开发
环境的旧 backend。

### 内置后台操作组件（Cua Driver）

macOS 发布包把 Cua Driver 作为 Oha-Yachiyo 的内置 sidecar；终端用户不需要
另外安装 `CuaDriver.app`、CLI 或 pip 包，应用运行时也不会联网下载驱动。发布
构建以 `packaging/cua-driver.lock.json` 为唯一版本与来源记录，固定官方 archive、
许可证和各自 SHA256。`python scripts/prepare_cua_driver.py` 校验 lock、下载或读取
缓存、验证 universal binary 后，只生成以下三个打包输入：

```text
dist/cua-driver/macos/cua-driver
dist/cua-driver/macos/LICENSE.md
dist/cua-driver/macos/manifest.json
```

electron-builder 将它们复制到
`Oha-Yachiyo.app/Contents/Resources/computer-use/macos/`。打包版只从这个固定资源
路径启动。负责直接创建子进程的是 Electron 主进程，不是 Python backend 或额外的
gateway：

```text
cua-driver mcp --embedded --host-bundle-id io.github.arisataki.oha-yachiyo
```

Python backend 不再创建 driver 子进程，只通过带随机 token 的 loopback JSONL bridge
使用 Electron 创建的 child。每次 backend restart 都会轮换 token 和不含密钥的
generation，并断开上一代连接。bridge 最多同时接受两条会话：一条长期执行会话和
一条短时健康探测会话；backend 重启或应用退出会关闭 socket、停止 listener，并清理
对应 driver child。

`--embedded` 与这条直接父子进程链建立了 Oha-Yachiyo 作为权限责任主体所需的进程
结构，但仍须用最终包完成下文真实 TCC 验收，不能仅凭源码结构宣称权限归属已经验证。
打包模式的 transport sentinel 是权威配置：内置资源缺失、listener 启动失败或 bridge
配置损坏时必须失败关闭，不能退回 `PATH`、自定义命令或开发机上偶然存在的全局
驱动，也不会在运行时下载驱动。只有没有该 sentinel 的源码开发环境仍可通过显式
命令、`PATH` 或已有 Cua Driver 安装进行 fallback。

这个能力不是 VM、远程桌面、独立用户会话、独立光标或第二套键盘：它仍在当前
macOS 登录会话中，使用面向目标应用/窗口的后台事件投递。可用范围受 Cua API 和
目标应用自身的辅助功能/事件支持限制；不支持的应用或动作必须失败关闭，不能悄悄
降级成前台鼠标、键盘或焦点抢占，也不能把“后台投递”描述成与用户桌面完全隔离。

在线构建会把 lock 指定的 archive 和许可证写入构建缓存；离线构建必须预先提供
完全匹配 SHA256 的缓存，或同时使用 `--archive` 与 `--license` 指向本地副本，再加
`--offline`。macOS CI 在打包前显式运行 prepare 和 focused tests；release verifier
及 workflow 的 packaged-app 门禁检查三个资源均为普通非符号链接文件、二进制可执行、
许可证/manifest 与 lock 一致、`--version` 和 `manifest` 契约正确、架构同时包含
`arm64` 与 `x86_64`，并验证嵌套二进制签名。

Cua Driver 不继承 Electron 的 JIT entitlement。二进制作为
`OhaCuaDriver.app/Contents/MacOS/cua-driver` 的主执行文件打包；Helper 的
`LSBackgroundOnly=true` 是发布门禁的一部分，防止 driver 注册成前台应用并抢占键盘
焦点。electron-builder 只对该精确嵌套路径使用 `signIgnore`，避免通用的
`allow-jit`、`allow-unsigned-executable-memory` 或 `disable-library-validation` 污染
Helper。`packaging/entitlements.cua-driver.plist` 是专用策略，且必须精确只含
`com.apple.security.automation.apple-events=true` 与
`com.apple.security.device.screen-capture=true`。本地自签名和 Developer ID 构建会
把嵌套 Helper 作为独立签名单元，先用同一发布身份及该专用 entitlement 显式签名，
再以非递归方式只签名外层 app（递归 `--deep --force` 会覆盖 Helper 的专用权限）；
无证书 CI 构建也会按同样顺序显式使用 ad-hoc 身份。最终门禁始终
执行 sidecar `codesign --verify`、精确 entitlement 比较和 app
`codesign --verify --deep --strict`，不会因没有 Developer ID 而跳过。
打包桥接还固定以 `--no-overlay` 启动嵌入式 MCP；CuaDriver 的可视化 Agent 光标
会创建 AppKit UI runloop 并把 Helper 注册成前台应用，而后台 AX/CGEvent 投递不依赖该覆盖层。

lock 除 archive/license SHA256 外，还以 `mach-o-without-code-signature-v1` 固定
剥离代码签名后的内容 SHA256。Electron/codesign 重签会合理地改变 raw Mach-O 的
签名字节，因此 verifier 只在临时副本上移除签名并比对 canonical hash，不修改正式
包原件。官方二进制含上游兼容帮助文本；legacy token 扫描豁免仅适用于上述精确
内置资源路径，并仍以该 canonical hash 门禁兜底。

升级内置驱动时：先审阅新的官方 release 与许可证；再一次性更新 lock 中的 version、
tag、URL、SHA256 和架构；运行 `python scripts/prepare_cua_driver.py --clean`；运行
prepare/runtime/release-verifier 测试及 `npm --prefix apps/frontend run pack:mac`；最后
对这次生成的 `.app` 完成下述真实 TCC 和后台行为验收。不要直接替换 `dist/` 二进制，
也不要只改版本号而沿用旧 hash。

## 权限与首次启动

主动桌面观察依赖 macOS 屏幕录制权限。开发模式下，TCC 权限可能落在 Terminal、Python、Electron 或启动器进程上，表现会不稳定；打包后用户只需要给 `Oha-Yachiyo.app` 授权，链路更清楚。

首次启动需要检查：

- 系统设置 -> 隐私与安全性 -> 屏幕录制：允许 Oha-Yachiyo。
- 如果读取当前窗口失败，再检查辅助功能或自动化权限。
- Web/Image/TTS provider 与模型配置仍按工具中心和主控台读取用户本机配置。
- GPT-SoVITS 等本地 TTS 服务不会被打进 DMG，仍需要用户自己启动服务并填写地址。

每一个最终发布候选包都要在安装后的真实 macOS 上重新完成以下检查；本文不把尚未
执行的本机检查写成已经通过：

- “屏幕录制”和“辅助功能”列表中只需要看到并授权 Oha-Yachiyo，不应要求添加
  CuaDriver、Python 或 Terminal。
- 如果重装、换签名或替换 app 后旧授权失效，先移除旧的 Oha-Yachiyo 条目，再把
  当前安装路径的 `Oha-Yachiyo.app` 加回并重新授权，然后重启应用。
- 首次从旧的 ad-hoc backend 迁移到上述稳定签名 backend 后，现有用户需要在“模型配置”
  中把 API Key 重新保存一次；Agent 自定义 API 则在 Agent Studio 的对应 Agent 中重新
  保存一次。完成这次迁移后，只要后续升级继续使用同一张证书和
  `io.github.arisataki.oha-yachiyo.backend`，就不应重复要求用户重存同一凭据。
- 如果当前构建仍走 `identity=-`、unsigned 或 ad-hoc fallback，则不要把跨版本
  Keychain 凭据可读性当成发布契约；出现凭据不可访问时，恢复方式仍是让用户重新保存
  对应 API Key。确认恢复流程不读取或显示旧 Key 的明文，并且只在数据库成功切换到新
  凭据后尝试清理旧钥匙串项。
- 从打包版读取 embedded driver 的权限状态，确认 host bundle id 是
  `io.github.arisataki.oha-yachiyo`，而且权限来源归属于 host。
- 用一个不会破坏用户数据的目标应用验证打开、读取、搜索/输入和结果确认；全过程
  观察当前前台窗口、系统鼠标指针和用户键盘输入，确认没有焦点或键鼠抢占。
- 拒绝或撤销权限后再测一次，确认任务给出可恢复的权限说明，而不是误报 Oha-Yachiyo
  自身执行失败，或偷偷使用前台 fallback。

## 自动发布

`.github/workflows/release-macos.yml` 会在 `main` 和 `oha-develop` push 后执行：

1. 先运行 release-facing product identity and security guards，确认发布配置、旧产品身份扫描、debug route、CredentialStore fallback 和关键 smoke 清单没有退化。
2. 安装 Python 与 Node 依赖。
3. 按 Cua Driver lock 准备内置后台操作组件，并运行其 focused distribution/runtime tests；下载、hash、manifest、架构或 embedded 契约任一不匹配即失败。
4. 运行其余关键 smoke tests。
5. 通过 `python scripts/prepare_app_build_metadata.py` 写入当前 channel / commit / latest URL 的 build metadata。
6. PyInstaller 构建主后端和独立 virtual desktop guest provider，并把 build metadata 打入主后端可执行文件。
7. electron-builder 生成 `.app` 目录后，由脚本以配置的自签名 / Developer ID 身份签名；未配置证书时使用 ad-hoc 身份。脚本对 Cua Driver 应用专用 entitlement，再签名外层 `.app` 并创建未签名 DMG。
8. Verify packaged app resources 会检查 `.app` 结构、主后端、virtual desktop guest provider、内置 Cua Driver 三文件、`app.asar`、关键 UI selector 和 packaged resources 旧身份扫描；所有签名模式都必须通过 Cua Driver 签名及精确 entitlement 校验，并对最终 packaged `.app` 运行 `codesign --verify --deep --strict --verbose=2`。
9. 生成带版本、commit 和架构的版本化 DMG / `.app.zip`，同时生成固定 latest DMG / ZIP、各自 SHA256、latest JSON 和 release notes。ZIP 使用 macOS `ditto --sequesterRsrc --keepParent` 创建，保留 app bundle 结构与扩展属性。
10. 对 `release/` 目录执行 binary-safe release artifact scan，确认最终 DMG、ZIP、JSON、checksum 和 notes 没有旧产品身份或旧执行内核 token，并校验每个 DMG / Oha ZIP 的 `.sha256` 文件、latest JSON 的 `name` / `channel` / `branch` / `source_branch` / `version` / `commit` / `short_commit` / `build_number` / `run_number` / `run_id` / `tag` / `signing` / `signature_kind` / `architecture` / `published_at` / `changelog` / `dirty` / `source_tree_fingerprint` / `release_publishable` 元数据格式和一致性，以及 latest JSON 的 DMG / ZIP 文件名、SHA 和同目录内容一致；可发布 metadata 的 DMG、ZIP 与 JSON URL 必须精确指向官方 HTTPS GitHub release，`signing` / `signature_kind` 还必须与只读挂载 DMG 后实测的 App 签名一致；随后运行最终 RC gate，并在配置真实 provider smoke secrets 时把 opt-in streaming/tool-call/native Agent/native Workflow full-chain provider smoke 结果写入 `release/rc-verification.json`。
11. 上传 workflow artifact，并创建或更新 GitHub Release 与 latest channel release。

Release tag 格式：

```text
stable-v<产品版本>-build.<构建号>-<短SHA>
alpha-v<产品版本>-build.<构建号>-<短SHA>
experimental-v<产品版本>-build.<构建号>-<短SHA>
```

产品版本由 `pyproject.toml` 管理，采用 SemVer，例如 Phase 4 对应 `0.4.0`。每次 workflow 运行只更新 `build_number`，不会再把 `GITHUB_RUN_NUMBER` 加进 patch 版本；同一产品版本下的不同构建由 `build_number`、短 SHA 和 tag 区分。需要升级产品版本时运行：

```bash
python scripts/app_version.py set 0.4.0
python scripts/app_version.py check
```

本地重新打包 RC 时，优先使用 `build_release_candidate_artifacts.py`。它会先记录 HEAD、dirty 状态和包含 tracked diff / untracked 内容的 `source_tree_fingerprint`，临时刷新 `.app` 和 packaged backend 共用的 build metadata、运行 PyInstaller、清理旧 `dist/electron` 后进入与 CI 相同的 macOS 签名脚本；即使没有证书，也会用 ad-hoc 身份分别签名后台 Helper 和外层 App，避免 electron-builder 的通用签名覆盖 Helper 专用权限。构建器只接受唯一且 version/arch 精确匹配的 DMG 及唯一 unpacked `.app`；构建结束和 rolling metadata 落盘后还会再次核对源码指纹没有漂移。随后它把实际 DMG 与 `.app.zip` 投影为 `release/` 下当前 rolling channel 的 DMG、ZIP、各自 SHA256、git changelog 与完整 latest JSON，并恢复 tracked `apps/frontend/public/oha-yachiyo-build.json` 开发占位。默认 dirty 工作树会在构建命令执行前失败；仅需本地检查时可以显式加 `--allow-dirty-local-rc`，此时 App 内外 metadata 都固定记录 `dirty=true`、实际 fingerprint 和 `release_publishable=false`，普通 local-inspection smoke 可读取它，但 public/latest/final signoff 必然拒绝。`signing` 始终由只读挂载最终 DMG 并检查其中 App 的 codesign / Developer ID stapler 结果推导；ad-hoc 归类为 `unsigned` 并另行记录 `signature_kind=adhoc`，`--signing-mode` 只作为预期值断言，不能覆盖实测结果：

```bash
python scripts/build_release_candidate_artifacts.py --channel experimental --repository kuguya-AI-app-develop/Hermes-Yachiyo
```

显式检查 dirty 本地工作树时使用：

```bash
python scripts/build_release_candidate_artifacts.py --channel experimental --repository kuguya-AI-app-develop/Hermes-Yachiyo --allow-dirty-local-rc
python scripts/verify_release_artifacts.py --allow-binary --allow-nonpublishable-local-rc release
```

CI 仍直接运行 `python scripts/prepare_app_build_metadata.py` 和 `python scripts/build_backend.py --clean`，随后使用 `scripts/build_macos_self_signed_dmg.sh` 构建、签名 `.app` 并生成未签名 DMG；workflow 工作区不会把临时 metadata 改动提交回仓库。macOS release workflow 会在安装 frontend dependencies 后、smoke tests 前运行 `python scripts/run_public_release_gate.py`，并上传 `release/public-release-gate.json`、`release/public-release-gate.md` 与 `release/public-release-gate/*.json` / `.md` / `.zip` nested evidence；手动触发 workflow 时可把 `public_demo` 输入设为 `full`，显式启用 required UI public-demo evidence。手动运行默认 `publish_release=false`，只生成可下载的 GitHub Actions DMG / ZIP artifact；只有显式设为 `true` 且 `stable -> main`、`alpha -> alpha`、`experimental -> oha-develop` 分支匹配时才会更新 GitHub Release。同一 ref 的发布使用 workflow concurrency 串行，避免 rolling JSON、DMG 和 ZIP 被并发 run 交错覆盖；真实桌面和 provider Workflow 仍可作为 optional diagnostics 单独收证。

在真正刷新本地 RC evidence 前，可以先跑低成本 public-release preflight：

```bash
python scripts/run_public_release_gate.py \
  --output-json tmp/public-release-gate.json \
  --output-markdown tmp/public-release-gate.md
```

该入口会运行 release artifact guard、secret redaction、Agent market-parity、Planner-to-runtime tool parity、Oha desktop-agent product smoke、focused release pytest、安全 public-demo smoke 和本轮 gate evidence 的脱敏 diagnostics bundle，并把 public-demo JSON、Oha 产品级 smoke JSON 与 diagnostics zip 投影成非阻断的 `tmp/public-release-gate/release-smoke.json` / `.md` 评估。默认模式用于快速发现文档、secret、Oha 主链路、release-smoke/public-demo/diagnostics 回归；当 public demo 仍是 `partial_demo_ready` 或 10 项 release-smoke 用户路径证据不完整时，报告会显示 `status=needs_release_evidence`、缺失 demo flow、缺失 user path 和下一步命令，但不会因为缺少 opt-in/RC 证据返回失败。已有 RC report、Oha smoke report 和 diagnostics bundle 可通过重复 `--release-smoke-report` 与 `--diagnostics-zip` 合入同一份 assessment。最终发布前加 `--require-release-ready`，让缺少完整 public-demo release evidence 或完整 10 项 release-smoke evidence 的候选版本直接失败。

通用桌面 Agent 的恢复与执行闭环可以单独做低成本回归：

```bash
python scripts/smoke_generic_agent_release.py \
  --report-json tmp/generic-agent-release-smoke.json
```

该 smoke 覆盖文件定位、应用定位、浏览器检索、媒体别名恢复、权限恢复、后台不抢占、有界恢复和内部执行细节隐藏 8 类场景。它已经嵌入 Oha desktop-agent product smoke 和 public release gate；单独运行主要用于快速定位某一类通用 Agent 行为回归。

安全 public-demo smoke 包含隔离桌面 provider 的 app 打开、UI 读取、点击、输入、快捷键和 verify 序列证据，用来证明默认发布门禁可以验证桌面执行工具链而不抢占用户当前鼠标键盘；真实 macOS app 打开、UI 读取和交互仍然必须显式 opt-in 补证。发布级 virtual desktop provider 必须满足 `docs/desktop-provider-contract.md` 中的 `oha-yachiyo.desktop-provider.v1` contract；loopback harness 只能作为安全工具链证据，不能作为真实可发布后端证据。

当剩余项依赖本机授权或 provider 凭证时，gate JSON 会额外写入 `external_requirement_count` 和 `external_requirements`，Markdown 会写入 `External Requirements` 小节。这里会把缺口归并成 `real_desktop_smoke_opt_in`、`provider_smoke_credentials` 等可执行类别，并列出缺失的 demo flow、缺失的 `OHA_YACHIYO_SMOKE_*` 环境变量、blocking condition 和补证命令。

通过 `--release-smoke-report` 或 `--public-demo-report` 合入的外部 JSON 如果包含 `source_revision.commit` 或 packaged `build_metadata.commit`，gate 会和当前 Git HEAD 对比；不匹配时会新增 `external_report_freshness` release blocker，避免旧 packaged/RC evidence 被误当成当前提交的发布证明。

本地增量收证时，`run_public_release_gate.py` 也可以直接透传 `--include-real-desktop-open`、`--include-real-desktop-ui-inspection`、`--include-real-desktop-interaction`、`--include-provider-workflow` 和 `--include-ui` 到 public-demo runner。真实桌面 flags 与 live provider flag 现在是 optional diagnostics，用来补充人工验收或现场供应商凭据证据；required public-demo 发布基线默认依赖隔离 provider、只读真实桌面发现和 deterministic `native_provider_contract`，不再要求预检抢占当前鼠标键盘或持有外部 provider secret。当 public demo 仍是 partial 时，Next Actions 会按 UI 等 required 依赖拆分补证命令，并优先只包含缺失 required flow flags；只有遇到未知未来 flow 时才回退到 full-demo 命令。已生成的分批 public-demo JSON 可以用重复的 `--public-demo-report` 合回 gate、`refresh_local_rc_signoff.py` 和 release-smoke summary；聚合时只承认 required flow 的 `status=passed`。`--include-provider-workflow` 在缺少 `OHA_YACHIYO_SMOKE_*` 凭证时会写入 `skipped=true`、`provider_smoke_credentials_missing` 和缺失变量名作为 diagnostic evidence，但不会把可发布基线伪装成产品失败。

如果真实桌面 interaction smoke 因 `app_already_running` 阻塞，可以在明确接受修改该应用当前状态时追加 `--allow-existing-real-desktop-app`。默认不追加该参数，以避免发布预检误操作用户已经打开的应用窗口。

如果要一次刷新当前 HEAD 的本地 RC evidence、Gatekeeper readiness diagnostics、Screen Recording attempt、provider-not-applicable 草稿和 final signoff preview，运行：

```bash
python scripts/refresh_local_rc_signoff.py --channel experimental --repository kuguya-AI-app-develop/Hermes-Yachiyo
```

该入口默认同样拒绝 dirty source；仅为本地调试收集 evidence 时，必须由操作者显式附加 `--allow-dirty-local-rc`。refresh 会把 flag 原样传给 venv 或当前解释器中的 builder，并保留 final preview 的 clean/publishable blocker，不能把 dirty evidence 误标成最终签核通过。

如果项目 `.venv/bin/python` 存在，该 helper 的构建阶段会优先用 `.venv` 解释器运行 `scripts/build_release_candidate_artifacts.py`，因此维护者可以从普通 `python` 启动刷新命令，而不必手动记住 PyInstaller 安装在哪个解释器里。

该命令会生成 `tmp/rc-verification-<short-commit>-source-capabilities.json`、`tmp/rc-verification-<short-commit>-packaged-batch.json`、`tmp/rc-verification-<short-commit>-screen.json`、`tmp/rc-verification-<short-commit>-native-capability-matrix.json`、`tmp/rc-verification-<short-commit>-release-readiness.json`、`tmp/rc-verification-<short-commit>-release-readiness.md`、`tmp/oha-yachiyo-diagnostics-<short-commit>.zip`、`tmp/rc-verification-<short-commit>-oha-desktop-agent-release-smoke.json`、`tmp/rc-verification-<short-commit>-public-demo.json`、`tmp/rc-verification-<short-commit>-public-demo.md`、`tmp/rc-verification-<short-commit>-release-smoke.json`、`tmp/rc-verification-<short-commit>-release-smoke.md`、`tmp/rc-signoff-<short-commit>-current.json`、`tmp/rc-signoff-<short-commit>-current.md` 和 `tmp/rc-signoff-<short-commit>-preview.json`；其中 source capabilities report 归档 source-level planner/artifact/approval/entrypoint/desktop discovery evidence，packaged batch report 通过 `--run-full-local-native-agent-rc` 同时归档 DMG mount、Gatekeeper readiness、packaged backend bridge identity、packaged app startup、packaged UI sampling、packaged Chat native file smoke、真实桌面 app open、真实桌面 UI inspection 和真实桌面 interaction evidence，native capability matrix report 会按 capability id 合并多份 RC report 并标出 source/provider/packaged 缺口，release readiness diagnostics 会把同一矩阵转成面向签核人/维护者的 blocker 摘要和下一步命令，diagnostics zip 会收集并脱敏本轮 RC/signoff/readiness evidence，Oha desktop-agent product smoke 会记录新 Core、Executor、Studio 主链路和 isolated desktop provider evidence，证明日常 app/media 桌面执行不需要抢占用户前台鼠标键盘，public-demo summary 会记录 `release_level`、`missing_required_flow_ids` 和 `release_blockers`，release smoke summary 会按 Oha desktop-agent product smoke、packaged launch、Chat desktop task、approval card、Agent Studio run timeline、GroupRun、Workflow、public demo、artifact readback 和 diagnostics export 汇总 10 项用户路径覆盖度。`summarize_oha_parity.py` 会把同一轮 Oha desktop-agent product smoke 作为独立 `oha_desktop_agent_product` area，避免最终 parity/readiness 摘要只看到 Native Agent capability matrix 而看不到 Core/Executor/Studio 主链路。如果 final signoff 只因为 Gatekeeper / Screen Recording 仍为 `manual_required` 而失败，命令仍返回成功，方便把“还差多少”作为状态刷新而不是构建失败处理。签核人可以直接填写 Markdown checklist，再用 `--manual-checks-markdown` 进入最终 gate。

只查看当前 HEAD 还剩哪些签核项时，运行：

```bash
python scripts/refresh_local_rc_signoff.py --print-status
```

该命令只读取 `tmp/rc-signoff-<short-commit>-current.json` 并打印剩余项，不运行 build、DMG 或 UI gate；如果当前 commit 的 draft 还不存在，它会失败但列出最近可用的 signoff / readiness / Oha product smoke / release-smoke / public-demo evidence，并打印刷新当前 draft 的 `--reuse-current-reports` 命令和随后要 rerun 的 `--print-status` 命令；如果同一 commit 的 `tmp/rc-verification-<short-commit>-release-readiness.json` 存在，也会同步打印 30 项能力矩阵通过数、缺失 capability 和 blocker 摘要；如果 `tmp/rc-verification-<short-commit>-release-smoke.json` 存在，也会打印 10 项发布用户路径通过数和缺失项，并带出 public demo 的 `release_level`、缺失 demo flow 和 blocker；如果 `tmp/rc-verification-<short-commit>-public-demo.json` 存在，也会直接打印 public-demo release level、required demo 覆盖率、缺失 flow 和 opt-in blocker。

需要按当前 draft 完成最后的 Gatekeeper / Screen Recording 收证时，可以先打印聚合操作指南；该命令只读已有 draft / screen report，不会写入 evidence，也不会把人工项标为通过：

```bash
python scripts/refresh_local_rc_signoff.py --print-os-signoff-guide
```

输出会列出当前 `tmp/rc-signoff-<short-commit>-current.json`、DMG、Gatekeeper readiness 诊断命令、稳定 Screen Recording app path、稳定 backend executable path、可执行的 `open -R ...` / System Settings Screen Recording URL、授权后要 rerun 的 `--run-dmg-screen-smoke` 命令、`--write-os-evidence` 命令和最终 `--require-manual-checks-complete` 命令。只有在签核人实际完成 Finder Gatekeeper 首启和 Screen Recording 授权验证后，才能把对应 evidence 写入 OS evidence JSON。

Gatekeeper / Screen Recording 已人工确认后，可以生成只包含剩余 OS evidence 的小 JSON；该文件会继承当前 `tmp/rc-signoff-<short-commit>-current.json` 里的 `manual_release_candidate_check_source_revisions`，避免最终 gate 因人工证据缺少源码版本而失败：

如果从 `--print-status` 或 `--print-os-signoff-guide` 复制命令，必须先把 `<record ...>` 占位符替换成真实 evidence；占位 evidence 会被拒绝，不会写入 OS evidence JSON。

```bash
SHORT_COMMIT="$(git rev-parse --short=8 HEAD)"
python scripts/refresh_local_rc_signoff.py \
  --write-os-evidence "tmp/rc-signoff-${SHORT_COMMIT}-os-evidence.json" \
  --gatekeeper-evidence "Mounted dist/electron/Oha-Yachiyo-0.4.0-arm64.dmg and opened Oha-Yachiyo.app through Finder Control-click -> Open." \
  --screen-recording-evidence "Granted Screen Recording to tmp/rc-screen-smoke/Oha-Yachiyo-0.4.0-arm64/Oha-Yachiyo.app and its Contents/Resources/backend/oha-yachiyo-backend helper, then reran --run-dmg-screen-smoke successfully."
```

最终签核时先传当前自动 evidence draft，再传这个 OS evidence 文件，让后者只覆盖两个手动 OS 项：

```bash
python scripts/verify_release_candidate.py \
  --require-artifacts \
  --manual-checks-json "tmp/rc-signoff-${SHORT_COMMIT}-current.json" \
  --manual-checks-json "tmp/rc-signoff-${SHORT_COMMIT}-os-evidence.json" \
  --require-manual-checks-complete \
  --report-json "tmp/rc-signoff-${SHORT_COMMIT}-final.json"
```

如果一次刷新在 batch 或 screen 阶段后中断，可以断点续跑：

```bash
python scripts/refresh_local_rc_signoff.py --reuse-current-reports
```

该模式只会复用 `source_revision.commit` 能以当前 `<short-commit>` 为前缀且 `dirty=false` 的已有 batch / screen report；不匹配、缺失、损坏或 dirty 的 report 会照常重新生成，避免把旧 commit 的自动 evidence 合入当前 final signoff。

固定下载链接：

- 最新正式版 DMG：<https://github.com/kuguya-AI-app-develop/Hermes-Yachiyo/releases/latest/download/Oha-Yachiyo-main-latest.dmg>
- 最新正式版滚动 release：<https://github.com/kuguya-AI-app-develop/Hermes-Yachiyo/releases/download/main-latest/Oha-Yachiyo-main-latest.dmg>
- 最新 Alpha 版 DMG：<https://github.com/kuguya-AI-app-develop/Hermes-Yachiyo/releases/download/alpha-latest/Oha-Yachiyo-alpha-latest.dmg>
- 最新实验版 DMG：<https://github.com/kuguya-AI-app-develop/Hermes-Yachiyo/releases/download/oha-develop-latest/Oha-Yachiyo-oha-develop-latest.dmg>

`main` 的版本化 release 会显式标记为 GitHub Latest，并额外上传 `Oha-Yachiyo-main-latest.dmg`，因此门户网站可以使用 `releases/latest/download/...`。`alpha` 与 `oha-develop` 都是 prerelease，GitHub 的 `releases/latest` 不会稳定指向它们，所以 workflow 分别维护 `alpha-latest` 与 `oha-develop-latest` 滚动 release。

渠道区分规则：

- `main` -> `stable` release，固定 DMG 名为 `Oha-Yachiyo-main-latest.dmg`。
- 手动 `alpha` -> `alpha` prerelease，固定 DMG 名为 `Oha-Yachiyo-alpha-latest.dmg`。
- `oha-develop` -> `experimental` prerelease，固定 DMG 名为 `Oha-Yachiyo-oha-develop-latest.dmg`。

固定 DMG 旁边会同时发布架构明确的 `.app.zip`、两种包的同名 `.sha256` 和 `.json` 文件，门户或安装页可以用它们展示版本、commit、架构、实际签名类型和校验值。应用内 updater 仍只使用 latest JSON 中经官方 HTTPS URL 与 SHA256 绑定的 DMG，不会因增加 ZIP 而放宽下载来源。

Release workflow 会基于当前渠道上一条 `stable-v*` / `alpha-v*` / `experimental-v*` tag 生成更新日志。更新日志来源是 `git log`，会按 commit 前缀粗分为“新增/改进”“修复”“工程/发布”“文档”“测试”“重构/优化”等分组，并同时写入：

- 版本化 GitHub release notes。
- `main-latest` / `alpha-latest` / `oha-develop-latest` 滚动 release notes。
- 固定 latest JSON 的 `changelog` 字段，应用内“应用更新”页面会直接展示这份更新内容。

## 本地 RC 验收

本地 release candidate 产物生成后，先运行统一验收入口：

```bash
python scripts/verify_release_candidate.py --require-artifacts
```

该命令会运行 source-level release guard，默认执行数据分析 CSV/JSON/text-table/XLSX sample dataset -> Markdown/CSV/HTML/PNG artifact readback smoke、Browser/Web planner -> browser tool/artifact smoke、Desktop planner -> discovery/operate/verify smoke、Real desktop discovery smoke、Planner/Runtime tool parity smoke、Approval/Policy gate smoke、Approval resume timeline smoke、Runtime approval resume smoke、Yachiyo route approval smoke，并对已生成的 `dist/backend`、`dist/electron` 和 `release` 执行 binary/package verifier。需要确认 DMG 内真实 `.app` 也可扫描时运行：

```bash
python scripts/verify_release_candidate.py --require-artifacts --check-dmg-mount
```

需要证明源码侧真实桌面 `app.open` 能从应用发现进入实际启动和运行态验证时，显式运行 opt-in app open smoke：

```bash
python scripts/verify_release_candidate.py --source-only --run-real-desktop-app-open-smoke --report-json tmp/source-only-real-desktop-app-open.json
```

`--run-real-desktop-app-open-smoke` 会在 macOS 上通过 runtime dispatch 执行真实 `desktop.list_apps -> desktop.open_app -> desktop.verify -> app.status` 链路，默认目标是 Calculator；如果 Calculator 在 smoke 前未运行，脚本会在验证后尝试清理退出，如果它原本已经运行则不会退出用户的 app。该 gate 默认不运行，避免普通 source-only 或完整 RC gate 自动打开本机应用；它证明的是通用桌面 app 发现、打开和验证能力，不替代 `--run-dmg-app-smoke` 的发布包自身启动证据。

需要证明通用桌面执行型 Agent 的“按能力找应用，而不是只打开写死 app”时，可以给同一个 gate 加能力查询：

```bash
python scripts/verify_release_candidate.py --source-only --run-real-desktop-app-open-smoke --real-desktop-app-open-capability-query browser --report-json tmp/source-only-real-desktop-browser-open.json
```

需要继续证明打开后的应用已经达到前台可操作状态时，再加严格前台 ready 检查：

```bash
python scripts/verify_release_candidate.py --source-only --run-real-desktop-app-open-smoke --real-desktop-app-open-capability-query browser --require-real-desktop-app-foreground-ready --report-json tmp/source-only-real-desktop-browser-foreground.json
```

严格模式会在 `desktop.verify` 后追加 `desktop.inspect_app(focus=true)`，必要时尝试一次 `app.show` recovery 并复检；如果当前 macOS 会话被锁定、前台焦点无法验证、只能读到菜单层 UI 或没有可操作控件，RC report 会在 `foreground_readiness`、`blocking_condition(s)`、`recovery_hints` 和 `recovery_actions` 中保留根因，而不会把“进程已启动”误报成“可前台操作”。

需要继续证明源码侧真实桌面 UI 读取链路时，显式运行 opt-in UI inspection smoke：

```bash
python scripts/verify_release_candidate.py --source-only --run-real-desktop-ui-inspection-smoke --report-json tmp/source-only-real-desktop-ui-inspection.json
```

`--run-real-desktop-ui-inspection-smoke` 会在 macOS 上通过 runtime dispatch 执行真实 `desktop.open_app`、`desktop.running_apps`、`desktop.list_windows`、`desktop.focus_app`、`desktop.active_window`、指定 app 的 `desktop.read_ui(app_name=...)` 和 `desktop.verify`。report 会记录 `focus_verified`、`window_count`、`ui_role_counts`、`menu_level_count` 和 `control_like_count`；当前环境如果只能读到菜单层或无法验证焦点，也会作为 evidence 保留，而不会宣称控件级 UI 操作已完全达标。新诊断字段 `ui_inspection_level`、`ui_unclassified_count`、`ui_visibility_limited` 和 `window_visibility_status` 用来区分控件级可读、仅菜单级可读、窗口列表不可见等状态。焦点失败时，report 顶层会带 `error`、`blocking_condition(s)`、`recovery_hints` 和 `recovery_actions`，方便 RC 报告直接定位环境 blocker。`desktop.permissions` 同时会把 `runtime_blocking_conditions` / `blocking_conditions` 和 `permission_targets` 分开，避免把 `desktop_session_locked` 误报成需要重新授权的权限问题。该 gate 默认不运行，避免普通源码验收打开本机应用。

需要证明源码侧已形成真实输入、UI 读取、语义控件点击和结果复核闭环时，显式运行 opt-in interaction smoke：

```bash
python scripts/verify_release_candidate.py --source-only --run-real-desktop-interaction-smoke --report-json tmp/source-only-real-desktop-interaction.json
```

`--run-real-desktop-interaction-smoke` 默认只操作 smoke 前未运行的 Calculator：输入 `42`，读取可见值，通过 `desktop.click_ui_element` 点击“更改数值符号”控件，再确认结果变为 `-42`。脚本要求每一步都验证目标 app 与前台状态，启动、聚焦、UI app 匹配或值读取失败时会立即停止，不会继续向未知前台输入；Calculator 原本已运行时也会拒绝修改用户现有状态。macOS 会话锁定时 report 顶层会记录 `desktop_session_locked`、可执行 recovery action 和焦点尝试证据，而不会误报为 Automation 或 Accessibility 权限缺失。该 gate 会真实输入和点击本机 UI，只证明源码 Runtime 的桌面执行闭环，不替代 packaged app / DMG 验收。

需要在 Gatekeeper 首次启动人工签核前归档 macOS 签名、spctl 和隔离属性诊断时运行：

```bash
python scripts/verify_release_candidate.py --require-artifacts --check-gatekeeper-readiness --report-json tmp/rc-verification-gatekeeper-readiness.json
```

`--check-gatekeeper-readiness` 会挂载 DMG、定位 `Oha-Yachiyo.app`，并记录 DMG / app 的 `com.apple.quarantine`、`codesign --verify`、`codesign -dv` 和 `spctl --assess --type execute` 结果。该 gate 只证明诊断已采集；自签名或未公证 app 被 `spctl` 拒绝仍符合当前免费分发策略，因此它不会自动把 `gatekeeper_first_launch` 标为 `passed`。最终 release signoff 仍必须由签核人实际通过 Finder Control-click -> Open 或系统设置 allow-open 完成首次启动，并写入人工 evidence。

需要从 DMG 内真实 `.app` 启动并等待 packaged Bridge `/status` 时运行：

```bash
python scripts/verify_release_candidate.py --require-artifacts --run-dmg-app-smoke
```

需要只启动已构建的 backend binary、快速确认 packaged Bridge 身份和 build metadata 时运行：

```bash
python scripts/verify_release_candidate.py --require-artifacts --run-packaged-backend-bridge-smoke
```

`--run-packaged-backend-bridge-smoke` 会用临时 `HOME` / `OHA_YACHIYO_HOME` 和随机 loopback `OHA_YACHIYO_BRIDGE_URL` 启动 `dist/backend/oha-yachiyo-backend`，等待 `/status.service=oha-yachiyo`，并把 `backend_path`、`bridge_url`、`service`、`version`、`native_agent_ready` 和 `build_metadata` 写入 `packaged_backend_bridge_smoke.bridge_statuses`。该 gate 只作为外部集成前置 supporting evidence，不会替代 `--run-dmg-app-smoke` 的 packaged app 启动证据，也不会自动完成 `external_integrations_smoke`。

`--run-packaged-backend-bridge-smoke`、`--run-dmg-app-smoke`、`--run-dmg-screen-smoke` 和 `--run-dmg-ui-sampling-smoke` 的 RC report 都会在对应 section 写入 `bridge_statuses`，记录 artifact 路径、`service`、`version`、`native_agent_ready` 和 packaged Bridge `/status` 返回的 `build_metadata`。`--run-dmg-chat-native-file-smoke` 还会在 `dmg_chat_native_file_smoke.uploads[*].app_build_metadata` 归档 packaged Electron app 暴露的 build metadata。如果打包时已刷新 `oha-yachiyo-build.json`，这些记录会把真实启动的 packaged backend、DMG 内 Bridge 或 Electron app 追溯到 commit / short commit；当当前 `source_revision.commit` 可用时，RC gate 也会要求 packaged `build_metadata.commit` / `app_build_metadata.commit` 与它一致，避免 stale DMG、旧 backend 或旧 Electron app 被误用于最终签核。

需要从 DMG 内真实 `.app` 抽样 packaged renderer 关键页面时运行：

```bash
python scripts/verify_release_candidate.py --require-artifacts --run-dmg-ui-sampling-smoke
```

`--run-dmg-ui-sampling-smoke` 会启动 DMG 内 `.app`、等待 packaged Bridge `/status`，再通过 Chromium DevTools 端口抽样 Chat、Agent Studio、Workflow、Activity、Diagnostics、Proactive TTS 和 Live2D settings 的稳定 selector。通过时同一轮 gate 会把 `packaged_bridge_isolation` 和 `packaged_ui_sampling` 自动标为 `passed`；该 gate 不会打开系统原生文件选择器，因此 `chat_native_file_upload` 仍需人工确认 packaged app 的 OS file picker 弹窗。

需要从 DMG 内真实 `.app` 验证 Chat 原生图片选择、预览、发送、图片查看器，以及“Chat 隐藏执行细节、Agent Studio 保留内部 replay”的界面边界时运行：

```bash
python scripts/verify_release_candidate.py --require-artifacts --run-dmg-chat-native-file-smoke
```

`--run-dmg-chat-native-file-smoke` 会启动 DMG 内 packaged Electron app，并在显式 smoke mode 下让主进程 `chooseChatImages` IPC 读取脚本生成的本地图片路径；随后通过 Chromium DevTools 验证 attachment preview、发送 payload、message attachment 和 image viewer open/close。smoke 会在 mock 回复中注入 internal activity、tool 和 recovery 证据，断言 Chat 回复与当前页面都不出现这些内部文本、技术 activity/tool/recovery 卡片、黄色警告 item 或 Run Detail action，再直接导航到 Agent Studio 验证同一 run 的内部 replay 仍可审计。这份证据仅覆盖固定 mock 负载与 packaged Chat 路由，不等于对所有真实 provider 输出的穷尽证明。为避免碰撞用户正在运行的实例，smoke 使用独立临时 Electron `userData` 与单实例隔离，所有 UI 等待共享一次全局 deadline；Node 正常退出时执行 TERM→KILL 有界清理，Python 外层超时时仅会在严格校验本轮 `pid`/`pgid`/executable/smoke root ledger 后终止对应进程组，不使用模糊 `pkill`。report 会归档 `internal_execution_hidden=true` 和 packaged Electron app 的 `app_build_metadata`，并在当前 `source_revision.commit` 可用时拒绝 stale app build。通过时同一轮 gate 会把 `chat_native_file_upload` 自动标为 `passed`。正常用户运行不设置 smoke env，仍使用系统原生 file picker。

需要从 DMG 内真实 `.app` 验证屏幕录制权限和 `/screen/current` 路径时运行：

```bash
python scripts/verify_release_candidate.py --require-artifacts --run-dmg-screen-smoke
```

`--run-dmg-screen-smoke` 会从 DMG 内复制 `.app` 到稳定的 `tmp/rc-screen-smoke/<dmg-name>/Oha-Yachiyo.app`，再启动该副本、等待 packaged Bridge `/status`，并请求 `/screen/current`。如果 macOS Screen Recording 权限未授权，RC report 会在 `dmg_screen_probe.app_launch_paths` 和 manual checklist supporting note 中写入这个稳定 app path 以及 `Contents/Resources/backend/oha-yachiyo-backend` backend executable path，方便签核人对实际执行截图的 helper 进程授权后复跑，而不是每次授权随机 mount path。通过时 RC report 只记录截图 `width`、`height`、`format`、`captured_at` 等元数据，不归档 `image_base64`；同一轮 gate 会把 `packaged_bridge_isolation` 和 `screen_recording_permission` 自动标为 `passed`。

具备真实 OpenAI-compatible provider smoke 凭据时，运行：

```bash
python scripts/verify_release_candidate.py --require-artifacts --run-provider-smoke
```

`--run-provider-smoke` 会复用 release workflow 的 opt-in provider smoke 合同，要求 `OHA_YACHIYO_SMOKE_BASE_URL`、`OHA_YACHIYO_SMOKE_MODEL` 和 `OHA_YACHIYO_SMOKE_API_KEY` 都已配置；它会分别验证文本流 `finish_reason=stop`、`workspace_read` tool-call、`README.md` 参数、`path=README.md` JSON 字段、`finish_reason=tool_calls`、synthetic tool-result follow-up 的 `finish_reason=stop`，以及 native Agent full-chain smoke 中的 ModelProfile readiness、Agent tool use、artifact write、workflow child Agent、approval resume 和 main chat model loop；随后运行 native Workflow full-chain smoke，覆盖真实 provider 下的 condition、subworkflow、workflow approval、parallel fan-in、loop exit、artifact 写入和 Workflow budget 边界。

无真实 provider 凭据时，默认 source-level RC gate 仍会运行 `native_provider_contract_smoke`，用本地 fake OpenAI-compatible SSE provider 经过同一套 stream parser 验证 text stream、tool-call stream、synthetic tool-result follow-up、Native Agent full-chain 和 Native Workflow full-chain。该 evidence 会让能力矩阵中的 `provider_text_stream` 与 `provider_tool_call_stream` 标记为 `passed`，并写入 `evidence_source=native_provider_contract_smoke`；但它不替代最终签核里的 `real_provider_smoke`，具备真实凭据时仍应运行 `--run-provider-smoke` 归档真实 provider evidence。

同一份 RC report 还会写入 `native_agent_capability_matrix`，把 source planner/artifact/approval/entrypoint/Agent Studio planner orchestration/desktop discovery、真实 macOS app open/UI inspection/interaction、provider text stream、provider tool-call stream、model profile readiness、workspace read、artifact write、multi-tool pipeline、Workflow child Agent artifact、terminal approval resume、main chat model loop、advanced Workflow orchestration、Workflow budget boundary、packaged backend bridge identity、packaged app bridge isolation 和 Agent market parity 汇总为 31 项 `passed` / `missing` 能力矩阵。矩阵同时写入 `category_status_counts`、`missing_by_category` 和 `next_actions`，把缺口分成 source、provider、packaged 三类并给出可直接复跑的命令。后续用 `--manual-checks-json` 合并多个 RC report 时，consolidated report、signoff draft、signoff Markdown 和 `--print-manual-checks-status` 都会保留并显示这份矩阵，便于最终签核报告直接展示 Native Agent 覆盖状态。需要从既有 RC report 单独重建矩阵时，可运行 `python scripts/summarize_native_agent_capabilities.py tmp/rc-verification-source-capabilities.json tmp/rc-verification-provider-smoke.json tmp/rc-verification-dmg-app.json --output-json tmp/native-agent-capability-matrix.json`；该命令会按 capability id 合并多份 report，优先保留通过项，也可以直接传入已经包含 `native_agent_capability_matrix` 的综合 RC report，避免已合并 evidence 被重新按顶层 smoke section 误判；矩阵未完整通过时仍以非零退出码提醒后续补证。

需要给维护者或发布签核人一份更短的“为什么还没到 release parity”诊断摘要时，运行：

```bash
python scripts/summarize_release_readiness.py \
  tmp/rc-verification-source-capabilities.json \
  tmp/rc-verification-provider-smoke.json \
  tmp/rc-verification-dmg-app.json \
  --output-json tmp/release-readiness-diagnostics.json \
  --output-markdown tmp/release-readiness-diagnostics.md
```

该摘要基于同一份 Native Agent capability matrix，保留失败 smoke 的 `stage`、`error`、`blocking_condition(s)`、`missing_permissions`、`recovery_hints` 和可重试 `recovery_actions`；Markdown 输出也会显示 blocker stage、error 和恢复动作，方便签核人直接看到解锁桌面、重跑前台检查或补权限的路径。provider smoke 缺失时，摘要只列出需要配置的 `OHA_YACHIYO_SMOKE_*` 变量名而不是值。它适合附在 release issue、签核记录或用户支持诊断里；如果矩阵仍未完整通过，命令会返回非零退出码，避免把 `desktop_session_locked`、Screen Recording/Accessibility 缺口或真实 provider evidence 缺口误写成已发布能力。

需要把当前 RC/signoff/readiness evidence 和现场日志打包给维护者排障时，使用脱敏诊断包命令：

```bash
SHORT_COMMIT="$(git rev-parse --short=8 HEAD)"
python scripts/collect_release_diagnostics.py \
  --label "$SHORT_COMMIT" \
  --include-app-logs \
  --output-zip "tmp/oha-yachiyo-diagnostics-${SHORT_COMMIT}.zip"
```

该命令默认收集 `tmp/rc-verification-<short-commit>-*.json/.md`、`tmp/rc-signoff-<short-commit>-*.json/.md`、同 label 的外部集成/Parity 摘要，并可通过重复 `--include <file-or-dir>` 追加崩溃日志或手工 evidence。进入 zip 前，JSON 会按结构递归脱敏，日志/Markdown 会走同一套 `packages.security` 文本脱敏；二进制、超大文件、无法读取或脱敏后仍命中 secret pattern 的文件会被跳过并记录到 `diagnostics/manifest.json`。该支持包只用于排障和签核沟通，不替代 `verify_release_candidate.py --require-manual-checks-complete` 最终 gate。

需要按 Phase 11 的用户路径口径汇总 release smoke 覆盖度时，使用：

```bash
SHORT_COMMIT="$(git rev-parse --short=8 HEAD)"
python scripts/summarize_release_smoke.py \
  "tmp/rc-verification-${SHORT_COMMIT}-source-capabilities.json" \
  "tmp/rc-verification-${SHORT_COMMIT}-packaged-batch.json" \
  "tmp/rc-verification-${SHORT_COMMIT}-screen.json" \
  "tmp/rc-verification-${SHORT_COMMIT}-oha-desktop-agent-release-smoke.json" \
  "tmp/rc-verification-${SHORT_COMMIT}-public-demo.json" \
  --diagnostics-zip "tmp/oha-yachiyo-diagnostics-${SHORT_COMMIT}.zip" \
  --output-json "tmp/rc-verification-${SHORT_COMMIT}-release-smoke.json" \
  --output-markdown "tmp/rc-verification-${SHORT_COMMIT}-release-smoke.md"
```

该脚本不启动 NativeRunEngine、Electron 或 provider；它只聚合已有 RC report、public-demo JSON、单个 smoke JSON 和诊断包 manifest，检查 10 个发布用户路径是否已有证据：Oha desktop-agent product smoke、packaged launch、Chat desktop task、approval card、Agent Studio run timeline、GroupRun、Workflow、public demo、artifact readback、diagnostics export。未覆盖时会返回非零，并在 `next_actions` 里列出要补跑的命令；当 public demo 仍是 `partial_demo_ready` 或 `blocked` 时，release-smoke 会保留 `public_demo` 缺失并显示缺失 demo flow 和 blocker，补证命令只带当前缺失 required flow 对应的 opt-in flags。为避免重复验收，summary 会把同一轮 RC capability matrix 里已通过且与 public-demo flow 等价的能力投影为 supporting evidence，例如 `source_real_desktop_app_open`、`source_real_desktop_ui_inspection` 和 `source_real_desktop_interaction` 可以补齐对应真实桌面 optional diagnostics；UI flow 仍必须由行为级 smoke 或人工/现场 evidence 证明，其中归档的 `release/electron-ui-smoke.json` 只会把精确通过的 Run Detail / Workflow UI smoke scripts（`scripts/smoke_agent_run_detail_ui.mjs`、`scripts/smoke_workflow_save_run_ui.mjs`）投影成 `studio_replay_ui` 和 `workflow_ui`，普通 packaged UI sampling 不替代这两个行为级 UI flows；provider workflow 可从真实 `provider_smoke` 里通过的 `native_workflow_full_chain` 检查，或独立 `scripts/smoke_native_workflow_full_chain.py` report，投影成 optional diagnostic `workflow_provider`，但 required provider path 由 deterministic `native_provider_contract` 证明。完整通过也不替代最终人工签核，只说明 release smoke 用户路径已有可复盘 evidence。`refresh_local_rc_signoff.py` 已在每次刷新时自动生成这份 summary；上面的手动命令用于重建、合并额外单项 smoke JSON 或排查历史 evidence。

使用一次性临时 provider key 做本地验收时，优先用安全 prompt wrapper，避免把 key 放进 shell history 或进程参数：

```bash
python scripts/run_provider_smoke_with_prompt.py \
  --base-url https://token-plan-cn.xiaomimimo.com/v1 \
  --model <xiaomi-model-name> \
  -- --require-artifacts --check-dmg-mount --run-dmg-app-smoke \
  --report-json tmp/rc-verification-provider-smoke.json
```

该 wrapper 会用隐藏 prompt 读取 `OHA_YACHIYO_SMOKE_API_KEY`，只通过子进程环境传给 `verify_release_candidate.py`，并自动补上 `--run-provider-smoke`。如果三项 `OHA_YACHIYO_SMOKE_*` 已在环境中配置，也可以继续直接运行原始 RC gate。

需要把 Electron UI smoke 也纳入本地 RC gate 时运行：

```bash
python scripts/verify_release_candidate.py --require-artifacts --run-ui-smoke
```

需要在现场用真实 Live2D ZIP、真实 GPT-SoVITS 音色包/API 服务和 AstrBot 插件桥接链路归档外部生态 evidence 时，使用 opt-in 外部集成验收脚本。该脚本不会自动下载素材或启动外部服务；只验证已经准备好的真实资源与正在运行的 Oha Bridge：

```bash
python scripts/smoke_external_integrations.py \
  --bridge-url http://127.0.0.1:18420 \
  --bridge-only \
  --report-json tmp/external-integrations-bridge-preflight.json
```

```bash
python scripts/smoke_external_integrations.py \
  --bridge-url http://127.0.0.1:18420 \
  --live2d-archive /path/to/yachiyo-live2d.zip \
  --tts-voice-archive /path/to/yachiyo-gpt-sovits.zip \
  --gpt-sovits-base-url http://127.0.0.1:9880 \
  --astrbot \
  --report-json tmp/external-integrations-smoke.json
```

`--bridge-only` 只检查 `/status.service` 是否为 `oha-yachiyo`，适合在两套应用共存时先确认没有误连到非 Oha bridge；它不会自动完成 `external_integrations_smoke`。`--live2d-archive` 会通过 `/ui/live2d/archive/import` 导入资源并保存 `display_mode=live2d`；`--tts-voice-archive` 会通过 `/ui/tts/voice-resource/import` 导入音色包、保存 TTS 设置，并在未传 `--skip-tts-test` 时调用 `/ui/tts/test` 真实请求 GPT-SoVITS；`--skip-tts-test` 只适合预检导入/保存链路，最终签核会把该 report 保持为 `manual_required`，直到重新跑真实 TTS 请求；`--astrbot` 会复用仓库里的 AstrBot 插件 handler，对正在运行的 Bridge 执行 `/y status`、`/y do`、`/y tasks`、`/y check`、`/y ask`、`/y screen`、`/y window` 和 `/y cancel`。report 会写入 `required_check_ids`、`selected_required_check_ids`、`missing_required_check_ids`、`resource_inputs`、`readiness` 和 `complete`：其中 `ok` 只表示本次选中的 checks 是否通过，`readiness.status` / `readiness.signoff_ready` / `readiness.completion_blockers` / `readiness.next_actions` 用于现场判断还差哪些资源或真实请求，只有 `complete=true` 且同时包含并通过 `live2d_resource`、`gpt_sovits_tts` 和 `astrbot_plugin_bridge` 时，才足以作为自动完成 `external_integrations_smoke` 的最终 evidence。完整 report 可以作为额外 `--manual-checks-json tmp/external-integrations-smoke.json` 传给 RC gate；只跑了 bridge-only、子集，或跳过了真实 TTS 请求时会作为 supporting note 保留，并明确列出 `missing_required_check_ids` 或 `readiness.completion_blockers`，最终签核仍会要求补齐其余外部集成 evidence。如果现场还需要证明真实 QQ 宿主的消息收发，需要在 AstrBot 宿主中安装插件并把同一份 `tmp/external-integrations-smoke.json` 与 QQ 端收发截图/日志一起归档。

尚未重新打包、只想快速确认源码级 release guard 时，使用 source-only dry run：

```bash
python scripts/verify_release_candidate.py --source-only --report-json tmp/source-only-rc.json
```

`--source-only` 会跳过本机已有 `dist/` 或 `release/` 旧产物，避免 stale `.app` / DMG 干扰源码验收判断；最终 RC 仍必须重新打包并运行 `--require-artifacts`。
`--source-only` 不能和 artifact path、`--require-artifacts`、`--run-full-local-native-agent-rc`、`--check-dmg-mount`、`--check-gatekeeper-readiness`、`--run-packaged-backend-bridge-smoke`、`--run-dmg-app-smoke`、`--run-dmg-screen-smoke`、`--run-dmg-ui-sampling-smoke`、`--run-dmg-chat-native-file-smoke`、`--run-provider-smoke` 或 `--run-ui-smoke` 混用；DMG mount、Gatekeeper readiness、packaged backend bridge smoke、DMG app startup smoke、DMG screen recording smoke、DMG packaged UI sampling smoke、DMG Chat native file smoke、真实 provider smoke 和 Electron UI smoke 只属于完整本地 RC 复验。`--run-real-desktop-app-open-smoke`、`--run-real-desktop-ui-inspection-smoke` 和 `--run-real-desktop-interaction-smoke` 可与 `--source-only` 混用；前两者会真实打开本机系统 app，interaction smoke 还会输入和点击，因此只应在需要收集桌面执行 evidence 时显式启用。

macOS release workflow 会在生成 release metadata 后、上传 DMG 前运行 `python scripts/verify_release_candidate.py --require-artifacts --check-dmg-mount --check-gatekeeper-readiness --run-packaged-backend-bridge-smoke --run-dmg-app-smoke --run-dmg-chat-native-file-smoke --report-json release/rc-verification.json`，确保 CI 与本地 RC 验收入口一致，并把 `release/rc-verification.json` 作为可归档验收报告随 release artifacts 上传；RC report 会写入 `source_revision`，记录当前 git commit、short commit 和 dirty 状态。同一轮 RC report 生成后，workflow 会合并 `release/rc-verification.json`、`release/public-release-gate/oha-desktop-agent-release-smoke.json`、`release/public-release-gate/public-demo.json`、`release/electron-ui-smoke.json` 和 `release/public-release-gate/diagnostics.zip` 生成 `release/release-smoke.json` / `.md`，让 Oha 产品级 smoke、packaged launch、public demo、Electron UI、diagnostics 和 provider workflow evidence 进入同一份 10 项用户路径视图。`chat_desktop_task` 同时要求 source desktop-entrypoint evidence 与 `dmg_chat_native_file_smoke.status=passed`；后者缺失、跳过或失败都会让 summary 变为 `incomplete` 并阻止 publication。最终 RC summary 完成后，workflow 会先以 `always()` 上传 DMG、JSON、Markdown 和其他 evidence，保证失败报告也可下载排查；随后只在分支 push 或手动触发且 `publish_release=true` 时强制要求 `release_smoke_status=0`，缺失、无效或 `incomplete` 的 summary 都会阻止 GitHub Release 更新。手动触发且 `publish_release=false` 时仍可完成构建并下载 evidence，不会因为尚未达到发布就绪而被该 publication hard gate 阻断。同一轮 RC report 生成后，workflow 也会生成并上传 `release/manual-rc-checks.template.json`，用于发布签核人从零填写 Gatekeeper、屏幕录制、原生图片上传、packaged UI 抽样、真实外部集成 smoke 以及任何未被自动 gate 证明的真实 provider evidence；随后输出的 `release/manual-rc-checks.draft.json` 已合并 `automated_rc_gate` evidence、source revision 摘要和 `release/electron-ui-smoke.json` 的 supporting notes，可直接补人工 evidence 后交给 `--manual-checks-json` 做最终签核；最终 RC report 也会把这些 manual evidence source revision 写入 `manual_release_candidate_check_source_revisions`，方便追溯 Gatekeeper / Screen Recording / external integration evidence 对应的源码版本；随后还会输出 `release/manual-rc-checks.md`，作为可读人工签核 checklist，并用同一个 `manual_release_candidate_check_source_revisions` 注释保留 source revision 链，避免 Markdown 签核路径丢失 traceability。打包前的 Electron UI smoke 由 `python scripts/run_electron_ui_smokes.py --report-json release/electron-ui-smoke.json` 动态发现并运行所有 `scripts/smoke_*_ui.mjs`，其结构化结果会作为 `release/electron-ui-smoke.json` 随 artifacts 上传。本地最终 Native Agent RC 复验优先运行 `python scripts/verify_release_candidate.py --run-full-local-native-agent-rc --report-json tmp/full-local-native-agent-rc.json`；该组合入口会启用 `--require-artifacts`、`--check-dmg-mount`、`--check-gatekeeper-readiness`、`--run-packaged-backend-bridge-smoke`、`--run-dmg-app-smoke`、`--run-dmg-ui-sampling-smoke`、`--run-dmg-chat-native-file-smoke`、`--run-real-desktop-app-open-smoke`、`--run-real-desktop-ui-inspection-smoke`、`--run-real-desktop-interaction-smoke` 和 `--allow-real-desktop-interaction-existing-app`，但不会默认运行需要额外系统授权的 `--run-dmg-screen-smoke`、需要真实凭据的 `--run-provider-smoke` 或外部 Live2D/GPT-SoVITS/AstrBot 集成 smoke。`--check-dmg-mount` 会只读挂载发现到的 DMG，并对 DMG 内真实 `.app` 的 `Contents/Resources` 再执行 packaged app scan；`--check-gatekeeper-readiness` 会把 codesign / spctl / quarantine 诊断写进同一份 RC report，但仍要求签核人手动完成 Gatekeeper 首启 evidence；`--run-packaged-backend-bridge-smoke` 会从 `dist/backend/oha-yachiyo-backend` 启动 packaged backend 并把正确 Oha Bridge 身份作为外部集成前置 supporting note；`--run-dmg-app-smoke` 会从 DMG 内启动真实 `.app` 并等待 packaged Bridge `/status`，通过后同一份 RC report 会把 `packaged_bridge_isolation` 标为 `passed`；`--run-dmg-ui-sampling-smoke` 会用真实 DMG 内 `.app` 自动填充 `packaged_ui_sampling` evidence；`--run-dmg-chat-native-file-smoke` 会用真实 DMG 内 packaged Electron app 自动填充 `chat_native_file_upload` evidence。如果 `OHA_YACHIYO_SMOKE_BASE_URL`、`OHA_YACHIYO_SMOKE_MODEL` 和 `OHA_YACHIYO_SMOKE_API_KEY` 都已配置，workflow 会向同一个 RC gate 传入 `--run-provider-smoke`，让 report 的 `provider_smoke` 字段记录真实 provider 文本流、tool-call follow-up、native Agent full-chain 与 native Workflow full-chain 结果；如果这些 secrets 未完整配置，workflow 会向 RC report、draft 和 Markdown 传入 `--mark-provider-smoke-not-applicable-if-missing`，把归档签核材料中的 `real_provider_smoke` 标为 `not_applicable` 并写入缺失变量 evidence。现场生成的 `tmp/external-integrations-smoke.json` 可作为额外 `--manual-checks-json` 输入，完整通过 Live2D、GPT-SoVITS 与 AstrBot plugin bridge 后会自动填充 `external_integrations_smoke`；失败 report 会把该项标成 `failed`，只跑子集则保留为 `manual_required` 并写入 supporting note。带 `--run-dmg-screen-smoke` 的屏幕录制权限检查和带 `--run-ui-smoke` 的完整 Electron UI smoke 仍保留为单独本地 RC 复验，因为它们分别需要 Screen Recording 授权或会启动额外 BrowserWindow。

脚本仍会列出首次启动 / Gatekeeper、屏幕录制权限、Chat 原生图片上传、packaged UI 抽样、packaged bridge、真实 provider 和外部集成 smoke 的最终签核项，并在 `release/rc-verification.json` 中写入结构化 `manual_release_candidate_check_statuses` 与 `manual_release_candidate_check_summary`。这些条目默认是 `manual_required`，并带有稳定 id、证据说明和下一步动作；当前固定 id 包括 `gatekeeper_first_launch`、`packaged_bridge_isolation`、`screen_recording_permission`、`chat_native_file_upload`、`packaged_ui_sampling`、`real_provider_smoke` 和 `external_integrations_smoke`。summary 会给出 `remaining_count`、`remaining_check_ids`、`remaining_next_actions`、`remaining_commands`、`remaining_notes`、`failed_check_ids` 和 `automated_evidence_check_ids`，用于快速判断最终签核还差多少项、下一步该跑哪个 gate、可直接复制哪条自动收证命令，以及某个剩余项是否已有失败 gate 的 supporting notes。如果同一次 RC gate 中 `--check-gatekeeper-readiness` 通过，`gatekeeper_first_launch` 会保留 `manual_required`，但会写入 codesign / spctl / quarantine 诊断 supporting note；如果 `--run-packaged-backend-bridge-smoke` 通过，`external_integrations_smoke` 会保留 `manual_required`，但会写入 packaged backend Bridge 身份 supporting note；如果 `--run-dmg-app-smoke` 通过，`packaged_bridge_isolation` 会自动标为 `passed` 并写入 `evidence_source=automated_rc_gate`；如果 `--run-dmg-screen-smoke` 通过，`packaged_bridge_isolation` 和 `screen_recording_permission` 会自动标为 `passed`；如果 `--run-dmg-screen-smoke` 已到达 packaged Bridge 但 `/screen/current` 因权限失败，summary/status 输出会保留 `screen_recording_permission` 的 supporting note，直到授权后重新跑通；如果 `--run-dmg-ui-sampling-smoke` 通过，`packaged_bridge_isolation` 和 `packaged_ui_sampling` 会自动标为 `passed`；如果 `--run-dmg-chat-native-file-smoke` 通过，`chat_native_file_upload` 会自动标为 `passed`；如果 `--run-provider-smoke` 通过，`real_provider_smoke` 也会自动标为 `passed`；如果外部集成 smoke report 完整通过 `live2d_resource`、`gpt_sovits_tts` 和 `astrbot_plugin_bridge`，`external_integrations_smoke` 会自动标为 `passed`。自动 evidence 只填充仍为 `manual_required` 的项，不会覆盖签核人已经写入的 `passed`、`failed` 或 `not_applicable`。可先生成人工验收模板：

```bash
python scripts/verify_release_candidate.py --write-manual-checks-template tmp/manual-rc-checks.json
```

已有上一轮 RC report 时，优先从 report 生成可编辑签核草稿，保留自动 gate 已填充的 evidence，并把仍需人工确认的项目留空：

```bash
python scripts/verify_release_candidate.py --manual-checks-json tmp/final-rc.json --write-manual-checks-draft tmp/final-rc-signoff.json
```

如果最终发布环境明确没有真实 provider credentials，可显式把 `real_provider_smoke` 写成 `not_applicable`，并自动附上缺失的 `OHA_YACHIYO_SMOKE_*` 变量名作为 evidence：

```bash
python scripts/verify_release_candidate.py --manual-checks-json tmp/final-rc.json --write-manual-checks-draft tmp/final-rc-signoff.json --mark-provider-smoke-not-applicable-if-missing
```

只想查看现有 RC report、draft 或 Markdown checklist 还剩哪些签核项时，使用只读状态命令；它不会运行 artifact / DMG / UI gate，也不会写出新文件：

```bash
python scripts/verify_release_candidate.py --manual-checks-json tmp/final-rc-signoff.json --print-manual-checks-status
```

已有人工 evidence 时可传入项目内 JSON：

```bash
python scripts/verify_release_candidate.py --require-artifacts --manual-checks-json tmp/final-rc-signoff.json --require-manual-checks-complete --report-json tmp/rc-with-manual-checks.json
```

`--manual-checks-json` 可以重复传入，脚本会按命令行顺序合并 evidence；普通 `{ "checks": [...] }` 或 Markdown 转出的人工 evidence 后传入时可覆盖前一份里的同一 check。previous RC report 的 `manual_release_candidate_check_statuses` 合并时会保留先前已经通过、失败或 not_applicable 的自动 evidence，不会让后一份 report 里的 `manual_required` 抹掉已收集的自动 evidence。最终签核加 `--require-manual-checks-complete` 时，如果当前 git source revision 可用且工作区 dirty，RC gate 会失败并写入 `source_revision_final_signoff_findings`；如果传入外部 `--manual-checks-json` / `--manual-checks-markdown` 但没有 `manual_release_candidate_check_source_revisions`，会以 `final signoff requires manual release-candidate evidence source revisions` 失败；如果传入的 previous RC report / Markdown checklist 携带的 `manual_release_candidate_check_source_revisions` 是 dirty 或与当前 `source_revision.commit` 不一致，RC gate 也会失败并写入 `manual_release_candidate_check_source_revision_findings`。需要先提交或丢弃未提交改动、重新打包，并用当前源码重新生成或记录 manual evidence，再进行最终签核。这样可以先传多份自动 RC report，再传只包含人工补充项的小 JSON：

```bash
python scripts/verify_release_candidate.py --require-artifacts --manual-checks-json tmp/final-rc.json --manual-checks-json tmp/manual-evidence.json --require-manual-checks-complete --report-json tmp/rc-with-manual-checks.json
```

如果最终发布环境明确没有真实 provider credentials，也可以在最终 RC gate 中显式传入 `--mark-provider-smoke-not-applicable-if-missing`，让 `real_provider_smoke` 在当前环境缺少任一 `OHA_YACHIYO_SMOKE_*` 变量时自动写入 `not_applicable` evidence，而不需要先手工编辑签核 JSON：

```bash
python scripts/verify_release_candidate.py --require-artifacts --manual-checks-json tmp/final-rc-signoff.json --mark-provider-smoke-not-applicable-if-missing --require-manual-checks-complete --report-json tmp/rc-with-manual-checks.json
```

也可以直接把填好的 Markdown checklist 作为最终签核输入：

```bash
python scripts/verify_release_candidate.py --require-artifacts --manual-checks-markdown tmp/final-rc-signoff.md --require-manual-checks-complete --report-json tmp/rc-with-manual-checks.json
```

也可以把同一份 JSON 输出为 Markdown checklist：

```bash
python scripts/verify_release_candidate.py --manual-checks-json tmp/final-rc-signoff.json --write-manual-checks-markdown tmp/final-rc-signoff.md
```

如果最终发布环境没有 provider credentials，也可以直接从 RC report 生成带 `real_provider_smoke=not_applicable` evidence 的 Markdown checklist：

```bash
python scripts/verify_release_candidate.py --manual-checks-json tmp/final-rc.json --write-manual-checks-markdown tmp/final-rc-signoff.md --mark-provider-smoke-not-applicable-if-missing
```

`--write-manual-checks-template` 输出的每项都带 `description`、`required_before`、`evidence_prompt`、空 `evidence` 和 `notes`，适合从零开始填写。`--write-manual-checks-draft` 会读取 `--manual-checks-json` 指向的上一轮 RC report 或 evidence JSON，输出同样可编辑、可再喂回 `--manual-checks-json` 的 `{ "checks": [...] }` 草稿；草稿会保留 `automated_rc_gate` evidence，并把仍是 `manual_required` 的项目 `evidence` 留空。`--write-manual-checks-markdown` 会读取同一份 evidence JSON 并输出可读 checklist，列出剩余人工项、下一步动作、可自动收证命令、需要记录的 evidence，以及已通过 / not_applicable 项的 evidence source；签核人也可以勾选并填写这份 Markdown，再用 `--manual-checks-markdown` 作为最终 gate 输入。生成 draft 或 Markdown 时，CLI 会立即打印同一套 progress、remaining ids 和 next actions，因此可以用已有 RC report 快速判断“还差多少”，不必为了看剩余项重跑完整 artifact / UI gate。源 RC report 如果包含已通过的 `electron_ui_smoke`，或把 `release/electron-ui-smoke.json` 作为额外 `--manual-checks-json` 传入，草稿会把通过的脚本列表预填到 `packaged_ui_sampling` 的 `Notes:`，并把 `smoke_chat_image_attachment_ui.mjs` 作为 `chat_native_file_upload` 的辅助 evidence 备注；该 source-level smoke 覆盖桌面 `chooseChatImages` API、hidden input fallback、CDP file input、preview、send、image viewer 和 Run Detail handoff，但仅凭 source-level Electron UI smoke 时，这两项仍保持 `manual_required`，只有 `--run-dmg-chat-native-file-smoke` 或人工 evidence 才会把 packaged `chat_native_file_upload` 标为通过。`tmp/external-integrations-smoke.json` 也可以作为额外 `--manual-checks-json`；完整三项通过会自动完成 `external_integrations_smoke`，失败 report 会让最终 gate 失败，只跑子集会把通过的子项写入 `Notes:` 并继续保留该项为 `manual_required`。Markdown checklist 的填写规则是：保留 ``- [ ] `check_id` `` 表示该项仍是 `manual_required`；改成 ``- [x] `check_id` `` 表示通过，不写显式 `status` 会按 `passed` 解析；需要显式跳过或记录失败时，用 ``- [x] `check_id` - not_applicable`` 或 ``- [x] `check_id` - failed``。所有 `passed`、`failed` 和 `not_applicable` 项都必须填写非空 `Evidence:`，多行 evidence 可放在缩进续行中。`--mark-provider-smoke-not-applicable-if-missing` 可用于 RC report、draft 或 Markdown checklist；它只在 `real_provider_smoke` 仍为 `manual_required` 且当前环境缺少任一 `OHA_YACHIYO_SMOKE_*` 变量时写入 `not_applicable`，不会覆盖已经通过、失败或手工标记的 provider evidence。`--manual-checks-json` 支持顶层 list、`{ "checks": [...] }`，也支持直接传入上一轮 RC report 并读取其中的 `manual_release_candidate_check_statuses`；多份 JSON 会按传入顺序合并，其中 previous RC report 的 `manual_required` 不覆盖已有自动 evidence，后传入的人工 `{ "checks": [...] }` 仍可覆盖先前状态，因此自动 evidence 不需要手工复制到另一个模板文件。`--manual-checks-markdown` 支持脚本生成的 Markdown checklist 格式。每项至少包含 `id`、`status` 和必要时的 `evidence`。`status` 只能是 `manual_required`、`passed`、`failed` 或 `not_applicable`；`passed`、`failed` 和 `not_applicable` 必须带 evidence。未知 id、同一文件内重复 id、非法 status、缺 evidence 或显式 `failed` 都会让 RC gate 失败并写入 `manual_release_candidate_check_findings`。最终发布签核时加 `--require-manual-checks-complete`，任何在自动 evidence 和人工 evidence 合并后仍为 `manual_required` 的检查都会让 RC gate 失败。

代码仓库已经具备 Developer ID 签名、notarization、stapling 和 Gatekeeper 验证路径；真正发布无 Gatekeeper 警告的构建仍需要在 GitHub Secrets 中配置有效的 Apple 凭据，并在 release runner 上留下 Accepted 的 `notarization.json` 与对应审计日志作为证据。
