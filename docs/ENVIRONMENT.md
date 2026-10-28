# 环境变量参考

全部开关的权威清单。代码里有 79 个 `HARNESS_*` 变量，按用途分组。

约定：

- **默认**列写的是「不设该变量时」的行为。
- 布尔开关接受 `1/true/yes/on` 为开、`0/off/false/no` 为关（大小写不敏感）。
- 「安全」标记的项建议保持默认；关掉它们通常是排查事故，不是日常配置。

---

## 关于读取方式

参数由 `src/settings.py` 统一读取（`flag` / `number` / `text` / `csv`），保证：

- 默认值只写一次（本文件的默认值与代码同源，不会漂移）
- 非法值一律**回落到默认**而不是报错——配置写错不该让一次已批准的操作失败
- **不缓存**：`/sandbox` 换档直接写环境变量，改完下一条工具调用就生效

安全档位（`HARNESS_SANDBOX_MODE` / `HARNESS_APPROVAL_POLICY` /
`HARNESS_APPROVAL_ALL`）**故意只认进程启动时的真实环境**，`.env` 里的值会被忽略。
原因：项目根的 `.env` 就在工作区里，写权限在默认档免审，Agent 改一行就能把下次
启动的权限降到 `never`。这是真实提权路径，不是理论风险。

**为什么不提供一个统一的 config.yaml**：三条约束会让它变成倒退——安全档位必须只认
启动环境（否则可被 Agent 改写）、换档必须即时生效（配置对象一旦缓存就失效）、参数
在调用时读取（不是导入期）。详见 [架构复查](ARCHITECTURE_REVIEW.md)。

---

## 1. 安全与审批（第三轮新增）

| 变量 | 默认 | 说明 |
|---|---|---|
| `HARNESS_NEVER_DO` | `1` | 第四档 `MUST_DO_SELF`：改密码/支付/2FA/凭据填表只能人自己做。**安全** |
| `HARNESS_APPROVAL_SCOPE` | `once` | 批准后的授权粒度：`once`/`times`/`until`/`session`。`session` 恢复旧行为（同类永久免审）。**安全** |
| `HARNESS_APPROVAL_POLICY` | `on-request` | `untrusted`（每条都问）/ `on-request` / `never` |
| `HARNESS_APPROVAL_ALL` | — | 设 `1` 等价 `APPROVAL_POLICY=untrusted` |
| `HARNESS_APPROVAL_TIMEOUT` | 见代码 | 审批等待秒数，超时即拒绝 |
| `HARNESS_SANDBOX_MODE` | `workspace-write` | `read-only` / `workspace-write` / `danger-full-access` |
| `HARNESS_UNTRUSTED_MARK` | `1` | 给 `web_fetch`/MCP 结果加 `<untrusted>` 围栏。关掉等于放弃注入防护。**安全** |
| `HARNESS_TAINT_EGRESS` | `1` | 污点出网：本回合读过敏感情报后联网强制升级审批。**安全** |
| `HARNESS_REDACT_MEMORY_SECRETS` | `1` | 记忆落盘前把凭据替换成 `[redacted:kind]`。**安全** |
| `HARNESS_LLM_APPROVAL` | 见代码 | 是否启用 LLM 审查官自动放行噪音类命令 |
| `HARNESS_JUDGE_MODEL` / `HARNESS_JUDGE_TIMEOUT` | — | 审查官用哪个模型、超时秒数 |

## 2. 记忆

| 变量 | 默认 | 说明 |
|---|---|---|
| `HARNESS_LOAD_MEMORIES` | `1` | 全局：是否注入已有记忆（进程级） |
| `HARNESS_MEMORY_RECALL` | `local` | `local`（本地关键词，零往返）/ `llm`（语义筛选，每条一次请求）/ `off` |
| `HARNESS_MEMORY_CONTRIBUTE` | `1` | 全局：会话结束后是否抽取成记忆（进程级） |
| `HARNESS_MEMORY_AGING` | `1` | 是否淘汰长期未用的记忆 |
| `HARNESS_MEMORY_MAX_AGE_DAYS` | `90` | 超过多少天未用到就归档（`preference`/`correction` 不过期） |
| `HARNESS_MEMORY_EVAL` | **`0`** | 异步 LLM-as-judge 记忆评测（判召回相关性 + 沉淀质量）。默认关：多数人不该为此付费 |
| `HARNESS_MEMORY_EVAL_EVERY` | `10` | 每 N 轮采样一次 |

**逐会话开关**用命令，不用环境变量：`/memories on|off|readonly|writeonly`。
会话级选择优先于上面的全局值，且**不改全局设置**。

## 3. 主动性

| 变量 | 默认 | 说明 |
|---|---|---|
| `HARNESS_PROACTIVE` | **`0`** | 只读主动建议。**默认关**——先有真实使用数据再开 |
| `HARNESS_PROACTIVE_MAX` | `3` | 每次最多几条建议（1–5） |
| `HARNESS_HEARTBEAT` | `1` | 每日只读心跳（确定性巡检，不跑模型） |
| `HARNESS_HEARTBEAT_CRON` | 见代码 | 心跳的 cron 表达式（本机时间，五段） |

连续忽略 3 次建议后自动降频到每周一次，不需要手动关。

## 4. 跨渠道

| 变量 | 默认 | 说明 |
|---|---|---|
| `HARNESS_CONTINUE_SESSION` | `1` | 新绑定时接续最近活跃会话（跨 TUI/飞书/API） |
| `HARNESS_CONTINUE_WINDOW_S` | `28800` | 接续时间窗（秒），默认 8 小时 |

## 5. 桌面 / 屏幕 / 剪贴板

| 变量 | 默认 | 说明 |
|---|---|---|
| `HARNESS_DESKTOP_CONTROL` | **关** | 键鼠控制。设 `1` 才启用；每步逐次审批 |
| `HARNESS_SCREEN_TOOLS` | 关 | 截屏/OCR。设 `1` 启用 |
| `HARNESS_CLIPBOARD_TOOLS` | 关 | 剪贴板读写。设 `1` 启用 |
| `HARNESS_CLIPBOARD_MAX_BYTES` | 见代码 | 读取上限（超出截断） |
| `HARNESS_OCR_LANG` | `zh-Hans-CN` | WinRT OCR 语言 |
| `HARNESS_OCR_REGION` | — | 固定 OCR 区域 |
| `HARNESS_SCREEN_DELAY_MS` | — | 截屏前延迟（等动画） |
| `HARNESS_DESKTOP_PAYLOAD` | — | 桌面控制的载荷开关（内部用） |

## 6. PowerShell worker（性能）

| 变量 | 默认 | 说明 |
|---|---|---|
| `HARNESS_PS_WORKER` | `1` | 常驻 PowerShell worker（`desktop_snapshot` 963→15ms） |
| `HARNESS_PS_WORKER_FALLBACK` | `1` | worker 失败时降级回独立进程 |
| `HARNESS_PS_WARMUP` | `1` | API 启动时后台预热 worker |
| `HARNESS_POWERSHELL` | — | 指定 powershell.exe 路径 |

## 7. 资源限额

| 变量 | 默认 | 说明 |
|---|---|---|
| `HARNESS_LIMIT_MEMORY_MB` | 不限额 | 单进程内存上限（Job Object） |
| `HARNESS_LIMIT_PROCESSES` | 不限额 | 进程数上限 |
| `HARNESS_LIMIT_CPU_SECONDS` | 不限额 | CPU 时间上限 |

任何限额设置失败都降级为「不限额照常跑」，不会因此让命令失败。

## 8. 沙箱 / 凭据 / 浏览器

| 变量 | 默认 | 说明 |
|---|---|---|
| `HARNESS_OS_SANDBOX` | `off` | `auto`/`require`/`off`。**仓库作工作区时不要开**——`/T` 打标会让 `.cache/sandbox-tmp` 继承 Low 而无法再标记 |
| `HARNESS_DISABLE_SECURE_CREDENTIALS` | — | 设 `1` 关闭 DPAPI 加密，退回读 `.env` |
| `HARNESS_BROWSER_PROFILE` | `.cache/browser-profile` | 浏览器自动化用的独立 profile |

## 9. 工作区 / 会话存储

| 变量 | 默认 | 说明 |
|---|---|---|
| `HARNESS_HOME` | — | 数据根目录（与会话/记忆/缓存），与工作区解耦 |
| `HARNESS_WORKSPACE` | 当前目录 | 默认工作区 |
| `HARNESS_WORKSPACE_ROOTS` | — | 允许的工作区根（多根用分隔符） |
| `HARNESS_WORKSPACE_FILE` | — | 工作区列表文件 |
| `HARNESS_TASK_ROOT` | — | 后台任务输出目录 |
| `HARNESS_EVENT_CACHE_MAX_BYTES` | 见代码 | 事件缓存上限（超出走流式解析） |

## 10. 模型与 provider

| 变量 | 默认 | 说明 |
|---|---|---|
| `HARNESS_MODEL_MAIN` | 见 `llm_config` | 主模型 |
| `HARNESS_MODEL_APPROVAL` | — | 审批审查官模型 |
| `HARNESS_LLM_CONFIG` | — | provider 配置文件路径 |
| `HARNESS_ALLOW_CROSS_PROVIDER_FALLBACK` | 见代码 | 主 provider 挂了是否跨家回退 |
| `HARNESS_IMAGE_TOKEN_ESTIMATE` | — | 图片 token 估算值 |

## 11. API / 网关

| 变量 | 默认 | 说明 |
|---|---|---|
| `HARNESS_API_HOST` | `127.0.0.1` | HTTP API 监听地址 |
| `HARNESS_API_PORT` | 见代码 | 端口 |
| `HARNESS_API_TOKEN` | — | API 鉴权 token（建议用 `secure_credentials` 加密存） |
| `HARNESS_API_URL` | — | 客户端连的 API 地址 |
| `HARNESS_API_LOG` | — | 访问日志路径（`RotatingFileHandler`） |
| `HARNESS_API_LOG_MAX_BYTES` / `_BACKUPS` | `10MB` / `3` | 日志轮转 |
| `HARNESS_SCHEDULER` | 见代码 | 定时任务服务开关 |
| `HARNESS_NOTIFY` | 见代码 | 本地通知开关 |
| `HARNESS_IDLE` | 见代码 | 空闲/锁屏感知 |

## 12. 调试 / 性能

| 变量 | 默认 | 说明 |
|---|---|---|
| `HARNESS_DEBUG` | — | `1` 打印系统提示组装、前缀缓存命中 |
| `HARNESS_PERF` | — | `1` 打印分段计时 |
| `HARNESS_DEBUG_CACHE` / `_SAMPLE` | — | 前缀缓存调试 |
| `HARNESS_TOOL_RESULT_CHARS` | 见代码 | 工具结果截断长度 |
| `HARNESS_PROGRESS_INTERVAL` | 见代码 | 进度输出间隔 |
| `HARNESS_HISTORY_INITIAL_MESSAGES` / `_PAGE_MESSAGES` | 见代码 | 历史分页 |
| `HARNESS_PROJECT_CONTEXT` | `1` | 是否读 `AGENTS.md`/`CLAUDE.md` |
| `HARNESS_UI` | — | UI 模式 |
| `HARNESS_SOFFICE` | — | LibreOffice 路径（文档转换） |
| `HARNESS_SCREEN_OUT` / `_META` / `_MODE` / `_OCR_IN` / `_OCR_META` | — | 截屏/OCR 内部路径开关 |
| `HARNESS_DEBUG_CACHE_SAMPLE` | — | 缓存采样率 |

---

## 常见组合

**最安全的一档**（敏感环境）：

```bash
HARNESS_SANDBOX_MODE=read-only
HARNESS_APPROVAL_POLICY=untrusted
HARNESS_MEMORY_RECALL=off
```

**日常个人使用**（默认即可，无需设置任何变量）：

```bash
# 全部默认值已经按「安全优先、可用性次之」调过
```

**排查事故时临时关掉某项**：

```bash
HARNESS_NEVER_DO=0        # 第四档误伤
HARNESS_TAINT_EGRESS=0    # 污点升级太吵
HARNESS_UNTRUSTED_MARK=0  # 围栏干扰了某个工具的输出解析
```

关掉之后记得改回来——这三项都是真实攻击面的防线。
