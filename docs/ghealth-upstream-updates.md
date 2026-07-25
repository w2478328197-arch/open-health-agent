# ghealth 上游同步记录

Open Health Agent 以不可变 commit SHA 锁定 [`Google-Health-API/google-health-cli`](https://github.com/Google-Health-API/google-health-cli)。上游目前没有 Release/tag，因此工作流每 6 小时检查其默认分支；只有新提交是当前锁定提交的快进后代，并通过构建、命令契约与全量回归测试，才会自动合入。

这里记录每次锁定版本变化和它对 Open Health Agent 的含义。自动更新不会扩大 OAuth scope，也不会直接替换用户设备上的已安装二进制。

<!-- GHEALTH_UPDATES:NEWEST_FIRST -->

## 2026-07-20 · `9cf02743d9ca051500b7c1c181eb88a9ae8988a5`

- 上游范围：[`6dad482c528b` → `9cf02743d9ca`](https://github.com/Google-Health-API/google-health-cli/compare/6dad482c528b91d6562eadc829fd3e717df5b75a...9cf02743d9ca051500b7c1c181eb88a9ae8988a5)
- 提交数：3
- 变更文件数：3
- 影响判断：CLI 可执行逻辑发生变化；上游 Agent Skill 指引发生变化；上游文档发生变化
- 合入门槛：上游 Go 测试、ghealth 构建、OHA 命令契约检查、仓库校验和 OHA 全量测试全部通过。

### 上游提交

- [`860dc6bd91ef`](https://github.com/Google-Health-API/google-health-cli/commit/860dc6bd91ef37781e481cb7e0611ed16aa5ffd7) · 2026-07-03 · Add Go installation requirements to README
- [`92f90c5c2674`](https://github.com/Google-Health-API/google-health-cli/commit/92f90c5c2674f3b70db0c9bcf97d588053a770be) · 2026-07-09 · Merge pull request #4 from wazeerc/patch-1
- [`9cf02743d9ca`](https://github.com/Google-Health-API/google-health-cli/commit/9cf02743d9ca051500b7c1c181eb88a9ae8988a5) · 2026-07-10 · docs: fix webhook credential separation and document timezone precedence

### 变更文件

- `README.md`
- `cmd/webhooks.go`
- `skills/ghealth/SKILL.md`

### 对 Open Health Agent 的含义

自动化只更新已审计的源码锁定提交；不会扩大 Google Health OAuth scope，也不会在用户设备上静默替换已安装二进制。后者会改变 scheduler 绑定的运行时指纹，必须走本机升级与重新验收流程。


## 2026-07-02 · `6dad482c528b91d6562eadc829fd3e717df5b75a`

- 初始审计锁定版本。
- 上游提交：[`6dad482c528b`](https://github.com/Google-Health-API/google-health-cli/commit/6dad482c528b91d6562eadc829fd3e717df5b75a) · `feat(client): allow GHEALTH_BASE_URL to override the API base URL`
- OHA 使用 14 类只读查询；OAuth scope 保持 `activity_and_fitness.readonly`、`health_metrics_and_measurements.readonly`、`sleep.readonly`。
