# Hermes、模型与微信接入

本页只描述 Hermes 参考宿主。Hermes 更新较快，以其官方文档和当前 `--help` 为准。

## 1. 先让 Hermes 独立工作

按 [Hermes 官方安装文档](https://hermes-agent.nousresearch.com/docs/getting-started/installation)安装。先完成普通对话，再叠加 Skill、微信和定时同步：

```bash
hermes doctor
hermes model
hermes
```

不要在普通聊天都无法完成时同时排查微信、Google、视觉和健康账本。

## 2. 选择模型或登录方式

`hermes model` 是终端中的完整 provider 配置入口；会话里的 `/model` 只能切换已经配置好的 provider。

### 使用 ChatGPT 登录

在 `hermes model` 中选择 **OpenAI Codex**，完成设备码 OAuth。Hermes 的[官方 provider 文档](https://hermes-agent.nousresearch.com/docs/integrations/providers)明确把 OpenAI Codex 列为“ChatGPT OAuth, uses Codex models”。这与以下方式不同：

- `openai-api` + `OPENAI_API_KEY`：OpenAI API 计费与权限；
- OpenRouter/Nous/其他 provider：由对应账户、模型和条款处理；
- 自建 endpoint：能力取决于所选模型与服务。

不要把 ChatGPT 订阅登录说成通用 API key，也不要把一个 provider 的视觉能力自动套到另一个 provider。

### 确认多模态能力

分别测试：

1. 主聊天模型是否能真正读取入站图片；
2. 主模型是纯文本时，Hermes 的 `auxiliary.vision` 是否配置且可用；
3. 语音消息是否带微信转写文本；若没有，是否有经过真实测试的转码/STT；
4. 主模型、辅助视觉和 STT 各自的提供商、隐私、留存和区域规则是否可接受。

OpenAI 当前 vision-capable GPT/Codex、Claude vision 和 Gemini 可以直接处理图片；DeepSeek V4 官方直连 API 是纯文本。Hermes 可以先让另一个视觉模型描述图片，再把描述交给 DeepSeek，但这不是 DeepSeek 原生看图，而且照片会经过第二个提供商。聚合器和自建 endpoint 的能力取决于最终模型与协议，不能从 provider 名称推断。完整矩阵见[兼容性说明](compatibility.md)。

主模型不能看图且辅助视觉也不可用时，Skill 必须请用户用文字补充，不能从文件名或聊天上下文猜食物/仪表读数。语音同理。本机 Hermes v0.18.0 的 Weixin 无转写音频会缓存为 SILK，而内置 STT 格式清单不含 SILK；没有明确转码/自定义 STT 时不能承诺自动转写。

## 3. 安装 Open Health Agent

从仓库根目录运行：

```bash
./install.sh --help
./install.sh --agent hermes --timezone Asia/Shanghai
```

如果用户还希望从 Codex 调用同一个私有账本，在**同一次**安装中使用：

```bash
./install.sh --agent hermes --agent codex --timezone Asia/Shanghai
```

微信场景仍以 Hermes 为必选宿主，因为 Hermes gateway 才接收 Weixin 消息；只安装 Codex 不会获得微信入口。

首次初始化只能选一次工作簿路径；要把 Excel 放进 iCloud，应在这条首次安装命令上直接加 `--workbook '<路径>'`，不要先普通安装再运行第二遍。已经安装后改路径或时区，使用 `open-health-agent init --force --workbook '<路径>' --timezone Asia/Shanghai`；installer 的 `--force` 不会改私有配置。完整选择见[安装文档](../skills/open-health-agent/references/installation.md)。

安装器会建立稳定的 `open-health-agent` 命令，并打印带私有 `--home` 的 `Command:` 前缀。它只安装本项目的 Skill、runtime、wrapper 和私有账本，不安装 Hermes 或 `ghealth`。默认目录可运行 `open-health-agent doctor`；自定义目录或命令不在 `PATH` 时使用安装器打印的完整前缀。不要改用未安装 `openpyxl` 的任意系统 Python。

如果用了自定义 `--home`，请在 Hermes gateway 的持久启动环境中设置同一个 `OPEN_HEALTH_AGENT_HOME`，或让调用始终保留安装器打印的 `--home` 前缀。只在同一受信任系统用户下这样做；不要把私有目录写进公开 `SKILL.md`。交互式 shell 临时设置的环境变量不一定会被后台 gateway 或 scheduler 继承。

Hermes 的 Skill 真源目录通常是 `~/.hermes/skills/`；安装器会处理 Skill 放置。Hermes 的 [Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/)兼容 Agent Skills 标准，安装后的 Skill 可在 CLI 或消息平台用 `/open-health-agent` 调用。

测试新会话：

```bash
hermes chat -q "/open-health-agent 帮我开始建立健康档案"
```

第一条非紧急回复应先说明本地账本、可选数据链路、照片/语音能力、第三方处理和非医疗边界，然后再执行用户已经明确要求的动作。第三方说明至少点明设备厂商、Apple Health/Health Connect 的系统账号与健康数据层、Google、微信/腾讯、模型与独立 STT/视觉服务，以及用户选择的 iCloud/云盘。若第一条消息是急症或明确紧急情况，先给当地急救/紧急处置方向，不能先运行说明、同步、记账或 context。

说明完成后，可把它作为本地安装审计记录下来；这不是跨会话免说明标志：

```bash
open-health-agent onboarding mark-explained --delivery-confirmed
```

只有 outbound-success hook 或下一轮用户消息确认说明已成功送达后，才运行这条命令；若 Hermes 在最终回复前执行工具，本轮不能先标记。每个新会话仍须先说明。连接 Google/微信等外部账号或安装后台任务前，取得针对该动作的明确同意，再运行 `open-health-agent onboarding grant-consent --scope <scope>`，固定 scope 列表以 `--help` 为准。状态时间戳不能代替当前会话的说明或扩大同意范围。

例如要启用 Google Health，先确认说明送达，再记录这条独立同意：

```bash
open-health-agent onboarding grant-consent --scope google-health
```

它只允许 OHA 使用这条链路，不会替用户完成 Google OAuth 或安装 scheduler。项目安装器也不会自动安装后台健康任务。

如果配置的 Excel 视图位于 iCloud、OneDrive、Dropbox 等同步目录，写入任何健康投影前还要单独记录：

```bash
open-health-agent onboarding grant-consent --scope cloud-workbook
```

缺少该 consent 时，Hermes 仍可把记录可靠写入 SQLite，但必须如实报告 `*_export_pending` / `blocked_by_consent`，不能声称 Excel 已更新。授权后运行 `open-health-agent export` 恢复视图；若先前的 Google 手动同步被云投影阻塞，还需重新真实同步一次才能取得 scheduler 安装资格。

## 4. 连接微信

Hermes 的个人微信适配器使用腾讯 **iLink Bot API**。它创建独立 bot 身份（例如 `...@im.bot`），不是脚本化普通个人微信；普通群事件往往不会送达，DM 最可靠。详情见 [Hermes Weixin 文档](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/weixin)。

```bash
hermes gateway setup
```

在向导中选择 Weixin，扫码并在手机确认。然后先用前台方式验证：

```bash
hermes gateway run
```

完成前台测试后，在这个终端按 `Ctrl-C` 并确认进程退出，再安装后台服务。同一个 Weixin token 只能由一个 gateway 进程使用。

验证完成后，可在 macOS/Linux 安装后台服务：

```bash
hermes gateway install
hermes gateway start
hermes gateway status
```

Hermes 的 [CLI reference](https://hermes-agent.nousresearch.com/docs/reference/cli-commands)将 `install/start/status` 分别定义为安装、启动和检查 launchd/systemd gateway 服务；WSL 更适合前台 `run`。

### 访问策略

当前 Hermes Weixin 的默认入站 DM 策略是 `open`；个人健康场景不能沿用它。扫码后先在 `~/.hermes/.env` 设置 `WEIXIN_DM_POLICY=pairing` 和 `WEIXIN_GROUP_POLICY=disabled`，重启或前台运行 gateway，再从自己的微信只发送一条不敏感测试消息。Hermes 应给出 pairing code；在电脑上执行：

```bash
hermes pairing list
hermes pairing approve weixin '<刚才显示的-code>'
hermes pairing list
```

批准后再发一条不敏感消息，确认本人能得到正常 Agent 回复。若能用另一个未批准账号测试，它最多应进入 pairing 流程，不能得到 Agent 健康回复。`hermes pairing list` 会显示完整的待批准/已批准身份；普通 Weixin gateway 日志可能会截断 ID，不能从截断日志拼接 `WEIXIN_ALLOWED_USERS`。保持 `pairing` 策略即可只授权已批准用户；若明确改用 `allowlist`，只能复制 pairing 列表中的完整 ID。**批准完成前不要发送任何健康文字、照片或语音。** iLink bot 是独立联系人；pairing/allowlist 都是入站访问控制，不是邀请机制。

同一个 Hermes profile 中启用的飞书、QQ、Slack 等其他平台也必须限制到本人；微信已 pairing 不能弥补另一个渠道的 allow-all。健康场景优先使用独立 Hermes profile/iLink bot，只暴露 `open-health-agent` 专用命令和必要的只读诊断，不给任意来信者通用 terminal、file、memory 或 `execute_code`。禁止用 `execute_code`/openpyxl 直接保存旧健康 Excel；所有受管健康写入都必须进入 OHA SQLite 和同一把锁。

微信入站可包含图片、文件、视频和语音。Hermes 会下载/解密媒体供 Agent 处理；当前版本可能缓存于 `~/.hermes/cache/images`、`audio`、`videos`、`documents`。缓存行为随 Hermes 版本和媒体类型变化，不能承诺自动删除，尤其音频/视频可能保留。限制该目录的本机访问权限，按当前版本核查并清理不再需要的媒体。媒体会经过腾讯和本机，也可能发送给已配置模型、STT 或视觉服务商。语音只有在微信提供转写或另有 STT 时才是可用文本。

## 5. 配置 Google Health 与 ghealth

先在手机侧验证目标指标已经出现在 Google Health：

```text
设备 → 厂商 App → Health Connect/Apple Health → Google Health
```

`ghealth` 使用的是 [Google-Health-API/google-health-cli](https://github.com/Google-Health-API/google-health-cli)。先让已安装的版本输出配置说明，再按说明创建自己的 **Desktop application** OAuth client。OHA 当前 14 类导入只需要 `activity_and_fitness.readonly`、`health_metrics_and_measurements.readonly` 和 `sleep.readonly`；不要用覆盖 nutrition、profile、settings、location、ECG 和 IRN 的全部只读预设。不要复制别人的 OAuth client secret 或 token。

```bash
ghealth setup --instructions
ghealth setup --scopes activity_and_fitness.readonly,health_metrics_and_measurements.readonly,sleep.readonly
```

在 Google Cloud OAuth consent screen 的 **Data Access → Add or remove scopes** 中也加入这三个完整的 Google Health scope，并在 **Audience** 中把实际同步账号加入 test users（若项目仍为 Testing）。`ghealth --scopes` 只限制本机请求，不会替你修改 Cloud consent screen。

Google 的通用 [Health API OAuth 设置页](https://developers.google.com/health/setup)目前介绍的是开发者自己编写直接 API 客户端时使用的 **Web Server** client 和 `https://www.google.com` redirect。它不是这套 `ghealth` CLI 的授权方式；不要为了 `ghealth` 创建该 Web client，也不要把这两种回调流程混用。

### 有浏览器：本机交互式授权

`ghealth` 的 Desktop client 使用临时 loopback/PKCE 回调。在运行账本的电脑执行：

```bash
ghealth auth login --scopes activity_and_fitness.readonly,health_metrics_and_measurements.readonly,sleep.readonly
ghealth auth status --validate
```

### 无图形浏览器：非交互式完成授权

无头环境仍然使用同一个 **Desktop application** client。先启动非交互式流程，在自己的浏览器中私下打开命令输出的地址，再从回跳地址栏只复制 `code` 查询参数交给完成命令：

```bash
ghealth auth login --non-interactive --scopes activity_and_fitness.readonly,health_metrics_and_measurements.readonly,sleep.readonly
ghealth auth login --complete '<code>'
ghealth auth status --validate
```

以已安装 `ghealth` 输出的 URL 和完成说明为准。client JSON、client secret、授权 URL、完整 redirect URL、授权 code、refresh token 及包含这些信息的命令输出都只留在私有本机环境，不要发进聊天、日志或仓库。

如果 OAuth consent screen 仍处于 **Testing**，refresh token 约 7 天后可能失效；定时任务随后会变成未授权。当前项目不会替你发送失效告警：Testing 期间至少每周检查一次 `ghealth auth status --validate` 和 `open-health-agent scheduler status`，且最后成功同步超过两个计划间隔时立即排查。重新授权仍使用上面的三个最小 scope；长期使用则按 Google 当前发布、验证和受限 scope 要求处理，而不是把 token 过期误判成没有健康数据。

把 ghealth 当前活动 profile 的时区显式设成与 Open Health Agent 完全相同的 IANA 名称。例如账本初始化使用了 `--timezone Asia/Shanghai`，则执行：

```bash
ghealth config set timezone Asia/Shanghai
open-health-agent doctor
```

如果使用命名 profile，执行上述命令时保留同一个 `GHEALTH_PROFILE`，或使用 ghealth 的 `--profile <name>` 参数。`doctor` 只读取并比较，不会悄悄修改 ghealth；真实 `sync` 在两边时区不同或 ghealth 仍使用隐式机器时区时会先停止。不要把 `ghealth config show` 的完整输出发到聊天中，其中可能包含项目配置。

先手动鉴权、验证小范围查询，再启用每小时任务。某项数据没有出现时，按设备 → 厂商 App → Health Connect/Apple Health → Google Health → OAuth scope → API 查询的顺序排查。

## 6. 定时同步与保持唤醒

健康同步和 Hermes gateway 是两个服务：gateway 接消息，健康任务读 Google 并写 SQLite/Excel。它们必须共用同一个账本写入器，不能各自直接保存 Excel。

SQLite 写入采用可恢复的 workbook-projection outbox：在数据库变化可能持久化前先写 pending 标记，提交 SQLite 后才尝试 Excel 原子投影，投影成功再清除。缺少云工作簿同意、文件占用、权限错误或崩溃都只会留下待恢复的 Excel 投影；Hermes 必须报告数据库已成功而 Excel pending，并在原因修复后运行 `open-health-agent export`，不能改用 openpyxl 反向覆盖真源。

在安装新任务前停用所有旧 cron/launchd/systemd/importer 和任何直接保存 Excel 的 Hermes 路径，正式环境只留一个 OHA writer。完整停写、预演、备份、冲突中止和回滚流程见[旧 Excel / Hermes 单写入迁移清单](migration.md)。

先确认首次说明已经送达并记录有效 `google-health` 同意；工作簿在同步目录时还必须有 `cloud-workbook` 同意。再用同一系统用户、活动 profile、已认证账号、Cloud 项目、IANA 时区和 OHA runtime 完成一次真实手动 `open-health-agent sync`，且 JSON 必须同时明确 `status=success` 与 `workbook_export=succeeded`。`--fixture` 只用于合成测试；fixture、云投影被阻塞的同步或 scheduler 自己的运行都不能作为安装凭据。真实手动同步成功后 **30 分钟内**，取得新的、一次性的 `scheduler` 同意并立即安装：

```bash
open-health-agent onboarding grant-consent --scope scheduler
open-health-agent scheduler install --interval-seconds 3600
open-health-agent scheduler status
# 停用并移除本项目的小时任务
open-health-agent scheduler uninstall
```

`scheduler` 同意是一次性的；任务进入操作系统安装尝试前就会消费它，因此后续安装失败后的重试也需要新的同意。超过 30 分钟、SQLite 中的手工同步证据不匹配或运行时有变化时，先重新真实手动同步，再取得新同意。安装器不会替用户自动完成这些步骤。

安装定义会绑定 `ghealth` 可执行文件、Python runtime、OHA 入口与代码、活动 profile、账号、Cloud 项目、授权 scope/认证方式和时区，并固定 JSON 与 scheduled-only 触发标记。一次性 grant 只允许安装动作；成功后另把 ongoing installed authorization 绑定到最近合格手动同步的静态 runtime 与账号 runtime 两个指纹。每次后台读取健康数据前都会验证 active consent、installed authorization 和指纹；漂移会被拒绝，不会回落到 default profile 或另一个账号/项目。

撤回 scheduler consent 会先让本地运行授权失效，再尝试移除 launchd/systemd 定义；即使删除失败留下孤儿任务，它也无法再次读取健康数据。撤回后单独重新 grant 不会复活孤儿，只有新的合格手动同步和成功重装才能绑定新授权。旧版 OHA plist/service 缺少受管 scheduled 标记、installed authorization 或指纹，必须先 `scheduler uninstall`，再按“真实手动同步 → 新同意 → install”重装，不能手工改旧定义绕过校验。

若 `ghealth` 只有经过本机代理才能访问，在安装命令上显式加 `--inherit-proxy-env`，或从 owner-only 文件读取 `--proxy-env-file <文件>`。只接受 HTTP/HTTPS/ALL/NO_PROXY 白名单；文件在 macOS/Linux 上必须归当前用户所有、权限不宽于 `0600`，除注释/空行外只能出现这些键。HTTP/HTTPS/ALL URL 只能指向 `localhost` 或回环 IP，必须含有效端口，不能有凭据、路径、查询或片段。远程代理、通用 `.env`、token 和其他密钥会被拒绝；状态只显示代理键名。

`scheduler status` 的首次自动运行验收只看 `last_scheduled_sync_status`、`last_scheduled_sync_at`、`last_successful_scheduled_sync_at` 和 `last_scheduled_sync_error_code`；`last_any_*` 可能来自手动同步。`scheduled_trigger_pinned`、`runtime_fingerprint_matches` 和其余定义校验也须通过。`recent_manual_ghealth_sync_eligible` 只表示 30 分钟内的手工证据当前仍可用于一次安装，不代表自动任务已运行。

同步的外部网络 fetch 不应长期占用 writer lock：先短暂持锁固定 consent、配置、日期、profile 和 runtime，释放锁执行 `ghealth` 读取，再重新持锁校验同一授权与身份后才写库。这样微信记录可在网络等待期间继续；若撤权或配置/账号漂移，已抓取批次在落库前整体拒绝。

Linux 使用 systemd user timer；用户退出登录后是否继续运行取决于该用户的 linger 状态。用 `open-health-agent scheduler status` 检查，并仅在理解系统影响时由管理员配置 linger。未启用 linger 时，不要承诺退出登录后仍会同步。

macOS 在完全睡眠时不会持续执行整点任务。插电使用时可到：

**系统设置 → 电池 → 选项 → 开启“显示器关闭时防止在电源适配器供电时自动进入睡眠”**

[Apple 的电池设置文档](https://support.apple.com/en-ca/guide/mac-help/-mchlfc3b7879/mac)说明此项仅在连接电源时防止自动睡眠。“Wake for network access”是另一项功能，不能替代保持唤醒。合上 MacBook 屏幕通常会让电脑睡眠。

如果选择本项目的 `caffeinate` 适配，只让它在 AC 电源有效，并让用户清楚它会增加耗电；不要默认在电池上阻止睡眠：

```bash
open-health-agent onboarding grant-consent --scope keep-awake
open-health-agent keep-awake-on-ac install
open-health-agent keep-awake-on-ac status
open-health-agent keep-awake-on-ac uninstall
```

`keep-awake` grant 也是一次性的，只能在授予后 30 分钟内用于一次安装尝试；超时、已消费或安装失败后重试都必须取得新的明确同意。

这不会让合盖或完全睡眠的 Mac 保证执行任务。手机端也不是本仓库的 scheduler 主机：iPhone/Android 负责设备/Google/微信数据入口和查看同步文件，本地 SQLite 与后台任务仍在电脑上运行。

本地同意可以按 scope 撤回；撤回 `scheduler` 时会同时尝试移除受管任务：

```bash
open-health-agent onboarding revoke-consent --scope scheduler
open-health-agent onboarding revoke-consent --scope google-health
```

本地撤回不会自动注销 Google token，也不会替用户关闭 Hermes 中的 Weixin、视觉、语音或其他宿主渠道；撤回 `cloud-workbook` 会阻止后续云投影，但不会删除云端已有工作簿副本或关闭提供商同步。相关外部 provider/host 必须分别撤销、解绑、停用或删除。`scheduler uninstall` 也会撤回 scheduler 同意。

## 7. Hermes 上下文注意事项

Hermes 自动发现项目上下文时的顺序是 `.hermes.md/HERMES.md` → `AGENTS.md` → `CLAUDE.md` → `.cursorrules`，详见[官方 Context Files 文档](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files)。Open Health Agent 不依赖当前工作目录的自动发现：Skill 必须直接读取私有数据目录中的 `AGENTS.md`。

这个私有文件是本 Skill 最高的持久化用户规范，但仍低于系统/开发者/安全规则；用户新确认的目标应立即写入后再用于建议。

## 8. 验收

在独立新会话里至少测试：

- `/open-health-agent 我想改善健康`：先解释，再冷启动；
- “我买了一个汉堡”：不记为摄入；
- “我刚吃了这个”并附图：仅视觉可用时估算，给份量范围与置信度；
- 一条无转写语音：不编造文本；
- 用户声明新目标：先写本地目标，再给计划；
- 重复 ghealth 同步：行数不增长；
- 当天建议：显示数据截止时间，并标注今天尚未结束；
- 高风险血压/症状：先安全处置，不继续普通训练建议。
