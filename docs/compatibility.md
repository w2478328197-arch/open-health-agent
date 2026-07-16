# 兼容性与能力边界

兼容性分三层：Agent 宿主能力、操作系统运行能力、健康数据链路。三层都满足，才是“可运行”。

## Agent 宿主

| 能力 | Hermes | WorkBuddy | Antigravity | 其他 Agent Skills 宿主 |
|---|---|---|---|---|
| 读取标准 `SKILL.md` | 官方支持 | 按当前版本验证 | 按当前版本验证 | 必须支持或写适配器 |
| 本地文件/命令 | 本地部署通常可用 | 验证沙箱策略 | 验证沙箱策略 | 宿主相关 |
| 私有 `AGENTS.md` | Skill 直接读取；不依赖自动发现 | 必须测试 | 必须测试 | 必须测试 |
| 微信 | Weixin iLink Bot adapter | 不假设 | 不假设 | 不假设 |
| 图片理解 | 模型/视觉工具相关 | 模型/工具相关 | 模型/工具相关 | 模型/工具相关 |
| 语音转写 | 微信转写或 STT 相关 | 通道/STT 相关 | 通道/STT 相关 | 通道/STT 相关 |
| 持久小时任务 | 推荐 OS scheduler | 推荐 OS scheduler | 推荐 OS scheduler | 推荐 OS scheduler |

“Skill 规范可移植”不等于“所有功能无配置可用”。没有本地执行权限的宿主只能解释规则，不能声称已经写入或读取健康档案。

## 操作系统

| 平台 | 本地账本 | Excel 导出 | 小时调度 | 备注 |
|---|---|---|---|---|
| macOS | 支持 | 支持 | launchd | 睡眠时不保证运行；插电可调整电池设置 |
| Linux | 支持 | 支持 | systemd user timer | 服务用户需有数据目录和 PATH 权限；退出登录后持续运行需检查 user linger |
| WSL | 核心可运行 | 支持 | 需逐环境验证 | Hermes gateway 更适合前台运行 `hermes gateway`；systemd 不一 |
| Windows native | Python 核心原则上可用 | 支持 | 当前仓库调度适配需验证 | 不应声称已经完全支持 |
| iOS/Android 单独运行 | 不支持本地账本服务 | 可查看同步文件 | 不支持仓库 scheduler | 手机只作为设备/Google/微信数据入口或同步文件查看端；SQLite 与小时任务仍在电脑 |

## 可穿戴与 Google Health

Google 的[第三方设备连接说明](https://support.google.com/googlehealth/answer/14236613?hl=en)列出 Apple Watch、Garmin、Xiaomi、Samsung、Whoop、Oura、Withings、Zepp/Amazfit 等路径，也明确不同设备和指标的缺口。正确表述是：

> 只有当设备数据经厂商 App 写入 Health Connect、Apple Health 或 Google 的直接路径，目标指标已出现在 Google Health，获得相应 OAuth 只读 scope，被 Google Health API 返回，而且属于 OHA 当前映射的 14 类查询时，本项目才会自动导入该指标。

不要表述成“凡是手表都支持”或“支持品牌就支持所有指标”。

`ghealth` 当前列出 40 类已验证 API 数据；OHA 只自动查询：`steps`、`distance`、`active-energy-burned`、`active-minutes`、`daily-resting-heart-rate`、`daily-heart-rate-variability`、`daily-oxygen-saturation`、`daily-respiratory-rate`、`daily-vo2-max`、`weight`、`body-fat`、`height`、`sleep --detail`、`exercise`。上游其余类型不会因为安装了 `ghealth` 就自动进入档案。

下表按 2026-07-14 的 Google 官方页面整理；它不是硬件白名单。

| 来源 | 典型链路 | Google 列出的代表性数据 | 明确缺口/条件 |
|---|---|---|---|
| Fitbit / Pixel Watch | Google 第一方路径 | 步数、距离、睡眠、训练、静息心率和部分型号的夜间生命体征 | 型号、地区和资格相关；OHA 不映射全天心率、皮温、ECG/心律提醒 |
| Apple Watch | Apple Health → Google Health | 活动、睡眠、训练/路线、体重、VO₂ max、心率和夜间生命体征 | 无运动分钟、站立小时、ECG/心律提醒和全天生命体征 |
| Garmin | Garmin Connect → Health Connect/Apple Health → Google Health | 活动、睡眠、训练摘要、心率/静息心率、体重 | 无 HRV、呼吸率、SpO₂、VO₂ max、皮温、路线/分圈 |
| Mi Fitness / Xiaomi | Health Connect → Google Health；Android only | 运动中心率、活动、睡眠、训练/地图、体重 | 无 HRV、呼吸率、SpO₂、VO₂ max、皮温或运动外心率 |
| Samsung Galaxy Watch | Samsung Health → Health Connect → Google Health；Android only | 活动、睡眠、训练、心率、SpO₂、VO₂ max、体重 | 无静息心率、HRV、呼吸率、皮温、路线/分圈；需额外同意健康数据处理 |
| Oura | Oura App → Health Connect/Apple Health → Google Health | 活动、睡眠、训练摘要、心率、HRV、体重 | 无静息心率、呼吸率、SpO₂、VO₂ max、皮温、路线/分圈 |
| Whoop | Whoop App → Health Connect → Google Health；Android only | 活动、睡眠、训练、静息心率、呼吸率、SpO₂、体重 | 无 HRV、VO₂ max、皮温、运动外心率、路线/分圈 |
| Withings | Withings App → Health Connect/Apple Health → Google Health | 逐项验证 | Google 明确说明 Withings 血压尚不支持；改用微信/CLI 手工记录 |
| Zepp / Amazfit | Zepp → Health Connect/Apple Health → Google Health | 活动、睡眠、训练、静息心率、体重、呼吸率、VO₂ max、SpO₂、路线 | 无 HRV、皮温、楼层、分圈和心律提醒 |

`ghealth` 是 [Google-Health-API/google-health-cli](https://github.com/Google-Health-API/google-health-cli)，查询 [Google Health API](https://developers.google.com/health)；不是直接 Health Connect 客户端。

## 模型能力

| Hermes provider / 模型路径 | 原生图片输入 | 结论 |
|---|---|---|
| OpenAI Codex（ChatGPT OAuth） | 当前支持 vision 的 Codex 模型可以 | 仍需从 Weixin 端实测；OAuth 不等于 API key |
| OpenAI API | 仅选中的 vision-capable GPT 模型 | 核对具体 model ID |
| Anthropic Claude | Claude vision 模型支持 | 需走正确原生图像格式 |
| Google Gemini | 当前 Gemini 文档列为多模态 | 仍受 endpoint/model/Hermes 版本影响 |
| Nous Portal/OpenRouter/Copilot/Bedrock | 取决于其中选中的模型 | 聚合器名称本身不证明能力 |
| DeepSeek 官方 API | 当前直接 chat API 为 text-only | 可由 Hermes `auxiliary.vision` 使用第二个视觉模型先描述图片，但这不是 DeepSeek 原生视觉 |
| 本地/自建 endpoint | 取决于模型和服务协议 | Qwen-VL/MiMo-VL 等视觉模型与文本模型分开验收 |

图片链路必须同时通过消息入站、Hermes 媒体路由和模型/辅助模型三层。语音是独立能力：入站音频不等于已有转写。当前本机 Hermes v0.18.0 的 Weixin 无转写语音为 SILK，而内置 STT 格式列表不含 SILK；没有明确转码/自定义 STT 时请用户补文字。每次换 Hermes/provider/model 后分别测试文字、无敏感图片和固定语音。

模型即使看得见照片，也不能可靠恢复所有隐藏用油、份量、钠或微量营养；缺字段留空并报告覆盖率。

## 最小兼容性测试

一个宿主/平台只有通过以下测试，才可标记为“完整可用”：

1. 新会话先解释 Skill，再执行明确请求。
2. 能读取私有 `AGENTS.md` 和 context；断开读取时诚实报错。
3. 同一手工记录重复提交不重复。
4. ghealth fixture/真实小查询可分页、去重、标注来源和截止时间。
5. 购买、菜单、计划和背景食物不记；单独餐食照默认不等于已摄入，只有用户已明确选择并在私有 `AGENTS.md` 保存专用对话约定，且无上述线索时才可省略重复确认。
6. 无视觉时不描述图片；无 STT 时不编造语音。
7. 当天建议标注 partial；训练日与休息日建议不同。
8. `total_energy` 不再叠加 REE/运动/TEF。
9. Excel 原子写入并保留非受管 sheet。
10. 凭据、真实健康数据和媒体不会进入 Git、日志或测试产物。
