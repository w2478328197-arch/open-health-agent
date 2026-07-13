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
2. Hermes 的视觉辅助模型是否配置且可用；
3. 语音消息是否带微信转写文本；若没有，是否有可用 STT；
4. 相关提供商的隐私、留存和区域规则是否可接受。

文本模型看不到图片时，Skill 必须请用户用文字补充，不能从文件名或聊天上下文猜食物/仪表读数。语音同理。

## 3. 安装 Open Health Agent

从仓库根目录运行：

```bash
./install.sh --help
./install.sh
```

安装器会建立稳定的 `open-health-agent` 命令，并打印带私有 `--home` 的 `Command:` 前缀。默认目录可运行 `open-health-agent doctor`；自定义目录或命令不在 `PATH` 时使用安装器打印的完整前缀。不要改用未安装 `openpyxl` 的任意系统 Python。

如果用了自定义 `--home`，请在 Hermes gateway 的持久启动环境中设置同一个 `OPEN_HEALTH_AGENT_HOME`，或让调用始终保留安装器打印的 `--home` 前缀。只在同一受信任系统用户下这样做；不要把私有目录写进公开 `SKILL.md`。交互式 shell 临时设置的环境变量不一定会被后台 gateway 或 scheduler 继承。

Hermes 的 Skill 真源目录通常是 `~/.hermes/skills/`；安装器会处理 Skill 放置。Hermes 的 [Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/)兼容 Agent Skills 标准，安装后的 Skill 可在 CLI 或消息平台用 `/open-health-agent` 调用。

测试新会话：

```bash
hermes chat -q "/open-health-agent 帮我开始建立健康档案"
```

第一条回复应先说明本地账本、可选数据链路、照片/语音能力、第三方处理和非医疗边界，然后再执行用户已经明确要求的动作。第三方说明至少点明设备厂商、Apple Health/Health Connect 的系统账号与健康数据层、Google、微信/腾讯、模型与独立 STT/视觉服务，以及用户选择的 iCloud/云盘。

说明完成后，可把它作为本地安装审计记录下来；这不是跨会话免说明标志：

```bash
open-health-agent onboarding mark-explained
```

每个新会话仍须先说明。连接 Google/微信等外部账号或安装后台任务前，取得针对该动作的明确同意，再运行 `open-health-agent onboarding grant-consent`。状态时间戳不能代替当前会话的说明或扩大同意范围。

## 4. 连接微信

Hermes 的个人微信适配器使用腾讯 **iLink Bot API**。它创建独立 bot 身份（例如 `...@im.bot`），不是脚本化普通个人微信；普通群事件往往不会送达，DM 最可靠。详情见 [Hermes Weixin 文档](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/weixin)。

```bash
hermes gateway setup
```

在向导中选择 Weixin，扫码并在手机确认。然后先用前台方式验证：

```bash
hermes gateway
```

验证完成后，可在 macOS/Linux 安装后台服务：

```bash
hermes gateway install
hermes gateway start
hermes gateway status
```

Hermes 的 [CLI reference](https://hermes-agent.nousresearch.com/docs/reference/cli-commands)将 `install/start/status` 分别定义为安装、启动和检查 launchd/systemd gateway 服务；WSL 更适合前台 `run`。

### 访问策略

当前 Hermes Weixin 的默认入站 DM 策略是 `open`；个人健康场景不能沿用它。**在发送任何健康文字、照片或语音前**，先启用 pairing，或配置 allowlist 只允许自己的 Weixin user ID；禁用群聊，再用不敏感消息验证实际入站身份。iLink bot 是独立联系人；allowlist 是入站过滤，不是邀请机制。策略仍为 `open` 时不要发送健康数据，并以安装版本的官方文档复核默认值是否变化。

微信入站可包含图片、文件、视频和语音。Hermes 会下载/解密媒体供 Agent 处理；当前版本可能缓存于 `~/.hermes/cache/images`、`audio`、`videos`、`documents`。缓存行为随 Hermes 版本和媒体类型变化，不能承诺自动删除，尤其音频/视频可能保留。限制该目录的本机访问权限，按当前版本核查并清理不再需要的媒体。媒体会经过腾讯和本机，也可能发送给已配置模型、STT 或视觉服务商。语音只有在微信提供转写或另有 STT 时才是可用文本。

## 5. 配置 Google Health 与 ghealth

先在手机侧验证目标指标已经出现在 Google Health：

```text
设备 → 厂商 App → Health Connect/Apple Health → Google Health
```

随后按 [Google Health API OAuth 设置](https://developers.google.com/health/setup)创建自己的 client，并按[官方 scope 列表](https://developers.google.com/health/scopes)只授权需要的读取项。`ghealth` 使用的是 [Google-Health-API/google-health-cli](https://github.com/Google-Health-API/google-health-cli)。不要复制别人的 OAuth client secret 或 token。

当前 Google 指南与 `ghealth` 的交互式向导展示的是两条不同但都可用的授权路径，只选一条，不要混用 client 类型与 redirect URI：

### 路径 A：Desktop client + 本机交互式浏览器

创建 **Desktop application** OAuth client，下载 JSON，然后在运行账本的同一台电脑执行：

```bash
ghealth setup
# 或已经完成配置后：
ghealth auth login --scopes-preset readonly
ghealth auth status --validate
```

该流程临时监听本机 loopback 地址并完成浏览器回调，适合有浏览器的本地 macOS/Linux。不要拿只登记了 `https://www.google.com` 的 Web client 跑这个 loopback 交互流程。

### 路径 B：Google 当前 Web Server client + 非交互式复制 code

按 Google 当前设置页创建 **Web application / Web Server** client，并把授权 redirect URI 精确登记为 `https://www.google.com`。把下载的 JSON 交给 `ghealth` 配置后，以非交互式方式开始授权：

```bash
ghealth setup --project-id '<project-id>' --client-secret '/private/path/client_secret.json' \
  --scopes-preset readonly --non-interactive-auth
# 若 setup 已经建立 pending auth，直接使用它打印的 auth_url/complete_command；否则运行：
ghealth auth login --non-interactive --scopes-preset readonly
# 在自己的浏览器打开输出的 auth_url；同意后从跳转地址栏复制 code 参数
ghealth auth login --complete '<code>'
ghealth auth status --validate
```

以命令输出的 `complete_command` 为准。授权 code 只在本机终端粘贴，不要发进聊天或日志。Web client 的 `https://www.google.com` 回调与 Desktop client 的 loopback 回调不可互换。

如果 OAuth consent screen 仍处于 **Testing**，refresh token 可能约 7 天后失效；定时任务随后会变成未授权。开发测试时把重新授权纳入预期，长期使用则按 Google 当前发布、验证和受限 scope 要求处理，而不是把 token 过期误判成没有健康数据。

把 ghealth 当前活动 profile 的时区显式设成与 Open Health Agent 完全相同的 IANA 名称。例如账本初始化使用了 `--timezone Asia/Shanghai`，则执行：

```bash
ghealth config set timezone Asia/Shanghai
open-health-agent doctor
```

如果使用命名 profile，执行上述命令时保留同一个 `GHEALTH_PROFILE`，或使用 ghealth 的 `--profile <name>` 参数。`doctor` 只读取并比较，不会悄悄修改 ghealth；真实 `sync` 在两边时区不同或 ghealth 仍使用隐式机器时区时会先停止。不要把 `ghealth config show` 的完整输出发到聊天中，其中可能包含项目配置。

先手动鉴权、验证小范围查询，再启用每小时任务。某项数据没有出现时，按设备 → 厂商 App → Health Connect/Apple Health → Google Health → OAuth scope → API 查询的顺序排查。

## 6. 定时同步与保持唤醒

健康同步和 Hermes gateway 是两个服务：gateway 接消息，健康任务读 Google 并写 SQLite/Excel。它们必须共用同一个账本写入器，不能各自直接保存 Excel。

只在手动同步成功后安装一个小时任务，并停用旧 cron/launchd/importer，避免双写：

```bash
open-health-agent scheduler install
open-health-agent scheduler status
# 停用并移除本项目的小时任务
open-health-agent scheduler uninstall
```

安装定时任务时会固定当时的 ghealth 活动 profile，并强制 JSON 输出，防止后台环境回落到 default profile 或输出 table/CSV。以后若主动切换 profile，先核对新 profile 的时区，再重新安装 scheduler；更新失败时旧定义会回滚保留。

Linux 使用 systemd user timer；用户退出登录后是否继续运行取决于该用户的 linger 状态。用 `open-health-agent scheduler status` 检查，并仅在理解系统影响时由管理员配置 linger。未启用 linger 时，不要承诺退出登录后仍会同步。

macOS 在完全睡眠时不会持续执行整点任务。插电使用时可到：

**系统设置 → 电池 → 选项 → 开启“显示器关闭时防止在电源适配器供电时自动进入睡眠”**

[Apple 的电池设置文档](https://support.apple.com/en-ca/guide/mac-help/-mchlfc3b7879/mac)说明此项仅在连接电源时防止自动睡眠。“Wake for network access”是另一项功能，不能替代保持唤醒。合上 MacBook 屏幕通常会让电脑睡眠。

如果选择本项目的 `caffeinate` 适配，只让它在 AC 电源有效，并让用户清楚它会增加耗电；不要默认在电池上阻止睡眠：

```bash
open-health-agent keep-awake-on-ac install
open-health-agent keep-awake-on-ac status
open-health-agent keep-awake-on-ac uninstall
```

这不会让合盖或完全睡眠的 Mac 保证执行任务。手机端也不是本仓库的 scheduler 主机：iPhone/Android 负责设备/Google/微信数据入口和查看同步文件，本地 SQLite 与后台任务仍在电脑上运行。

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
