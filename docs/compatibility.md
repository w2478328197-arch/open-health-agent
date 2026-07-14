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

Google 的[第三方设备连接说明](https://support.google.com/googlehealth/answer/14236613?hl=en-GB)列出 Apple Watch、Garmin、Samsung、Whoop、Oura 等示例，也明确不同设备/指标路径不同。正确表述是：

> 只要设备数据经厂商 App 写入 Health Connect 或 Apple Health、同步到 Google Health、获得相应 OAuth scope，并被 Google Health API 返回，本项目就能导入对应指标。

不要表述成“凡是手表都支持”或“支持品牌就支持所有指标”。

| 来源示例 | 典型前置链路 | 必须验证 |
|---|---|---|
| Apple Watch | Watch → Apple Health → Google Health | 机型支持、Apple Health 权限、Google 显示的指标 |
| Garmin | Watch → Garmin Connect → Health Connect/Apple Health → Google Health | Garmin 定期同步；Google 不直连设备 |
| Xiaomi | Watch/Band → Mi Fitness 或当前厂商 App → Health Connect/Apple Health → Google Health | 厂商当前是否写目标指标；地区/版本差异 |
| Samsung | Galaxy Watch/Samsung Health → Health Connect → Google Health | 数据类型与读写权限 |
| Whoop/Oura | 厂商 App → Health Connect/Apple Health → Google Health | 订阅、授权与指标映射 |
| 手工设备 | 微信/CLI 文字、语音转写、视觉读数 | 单位、时间、来源、置信度 |

`ghealth` 是 [Google-Health-API/google-health-cli](https://github.com/Google-Health-API/google-health-cli)，查询 [Google Health API](https://developers.google.com/health)；不是直接 Health Connect 客户端。

## 模型能力

| 输入 | 最低能力 | 不具备时 |
|---|---|---|
| 文字饮食/测量 | 文本模型 + 本地命令 | 仍可使用 |
| 微信语音 | 可靠转写文本或 STT | 请用户补文字，不猜 |
| 食物照片 | 视觉模型/工具 | 请用户描述食物与份量 |
| 血压计/报告照片 | 视觉 + 清晰图片 + 人工确认歧义 | 手动输入数值/单位 |
| 微量营养素 | 食物身份、份量和可靠营养来源 | 留空并报告覆盖，不编造 |

每次换 provider/model 后重新测试图片和语音。ChatGPT OAuth 在 Hermes 中是 OpenAI Codex provider；它不代表任意辅助模型或外部 API 自动获得相同能力。

## 最小兼容性测试

一个宿主/平台只有通过以下测试，才可标记为“完整可用”：

1. 新会话先解释 Skill，再执行明确请求。
2. 能读取私有 `AGENTS.md` 和 context；断开读取时诚实报错。
3. 同一手工记录重复提交不重复。
4. ghealth fixture/真实小查询可分页、去重、标注来源和截止时间。
5. 购买、菜单、计划和背景食物不记；专用健康对话中的近距离餐食照只有在无上述线索时才可按默认约定记为已摄入。
6. 无视觉时不描述图片；无 STT 时不编造语音。
7. 当天建议标注 partial；训练日与休息日建议不同。
8. `total_energy` 不再叠加 REE/运动/TEF。
9. Excel 原子写入并保留非受管 sheet。
10. 凭据、真实健康数据和媒体不会进入 Git、日志或测试产物。
