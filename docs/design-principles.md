# 系统设计准则

[编码标准](coding-standards.md)

## 分层与控制权

| 层                        | 职责                                                                                                 |
| ------------------------- | ---------------------------------------------------------------------------------------------------- |
| `harness.py` agent_loop   | 唯一 OpenAI 消息主循环：checkpoint、压缩预处理、注入、LLM 调用、工具批次、housekeeping 编排          |
| `src/agent_core.py`       | 可复用原语：压缩四级流水、记忆系统、hooks 注册表——agent_loop 与 agent_teams 共用同一份，避免双份漂移 |
| `src/prompt_assembly.py`  | system prompt 分段组装与上下文采集；按 context 指纹缓存，依赖由入口注入                              |
| `src/session_title.py`    | 会话标题副通道：取材、LLM 调用与本地兜底，client 由入口注入，失败不阻塞回复                          |
| `src/session_store.py`    | append-only 事件流、确定性回放、分叉与跨会话搜索；唯一恢复真相源                                     |
| `src/agent_runtime.py`    | 会话服务（列表/恢复/分叉/搜索/重命名），摘要缓存归属于此                                             |
| `src/harness_api.py`      | FastAPI 控制面 + SSE 进度流（直接 tail 会话 JSONL，无事件总线）                                      |
| `src/tools.py` + registry | 工具接线与注册表；schema 在 tool_schemas.py，实现分域在 tool_*.py；统一走 exec_tool_call             |
| 任务子系统                | `task_manager`（.cache/os_tasks）、`cron_scheduler`、`background_task` 各自拥有存储与锁              |
| 传输网关                  | `feishu_gateway` / `gateway_sim` 只做协议适配，不持有业务状态                                        |

`harness.py` 拥有主循环，session_store 拥有事件流，网关不拥有业务状态。
网关与 API 只消费事件流和 runtime 服务；新增能力不增加网关内的业务分支。

## 事件流不变式（最高优先级）

1. **append-only**：任何信息不删改；压缩、截断、改写全部以新事件表达决策，
   原始事件保留（undo/rewind 仍可回溯）。
2. **原序写回**：tool 结果事件的顺序必须与 `assistant.tool_calls` 对齐——
   并行执行也按模型给定顺序登记 started 与写回结果，乱序即孤儿。
3. **内存形态 == 重建形态**：一切影响请求历史的决策（`tool_compacted` 占位、
   `history_snipped` 切片索引、`compaction` 摘要+tail）持久化为事件并由
   rebuild 确定性应用；只在内存改写的决策在重启后失效，属于缺陷。
4. **确定性回放**：`rebuild_openai_history` 对同一事件流永远产出逐字相同的
   历史；新事件类型必须满足该性质才能合入。

## 前缀缓存纪律

供应商 KV 缓存按前缀命中，命中价与 miss 价差 25-50 倍——**历史中部的逐字
稳定是本项目的第一性能不变式**：

- 改写决策必须持久化为事件（见上），且触发必须稀疏：snip 触发后武装迟滞
  （压缩后条数 + 阈值/4）才允许再次触发；micro_compact 攒批 ≥4 条 + 水位
  0.5×CONTEXT_LIMIT。滑动窗口（越线后每轮触发）是命中率杀手，实测 6-19%。
- 大输出在工具边界入口截断（`_bound_tool_output` 保头 8000 + 保尾 4000 字符、
  全量落盘），历史中从此不存在超大工具结果——事件流存的即最终形态。
- 压缩判据用真实用量：agent_loop 逐调用维护 `ctx_tokens`（上次请求的
  `prompt_tokens` 权威锚点 + 新增消息估算增量），每调用写 `usage` 事件；
  estimate_size 仅作无锚点时的回落。CJK 估算是除法（1.3 字符/token）。
- 记忆注入放在请求末尾的独立 system 消息，主前缀（system + 全部历史）
  跨轮逐字稳定。
- 项目说明（`AGENTS.md`）与技能目录属于主前缀：项目说明按 mtime/size 缓存（改文件或换
  工作区才变）；技能扫描按 manifest 签名去抖——不要在每轮向里面拼时间戳或状态。

## 状态所有权

| 状态                             | owner                                                          |
| -------------------------------- | -------------------------------------------------------------- |
| 事件流、seq、路径锁、事件缓存    | `SessionEventStore`                                            |
| 快照与文件 hash                  | `CheckpointManager`（ContextVar 绑定当回合）                   |
| 任务清单                         | `task_manager`（`.cache/os_tasks`）                            |
| 定时任务 / 后台任务              | `cron_scheduler` / `background_task` 各自注册表                |
| 会话-飞书聊天绑定、per-chat 顺序 | feishu gateway（每 chat 一把锁）                               |
| 标题来源                         | `session_title` 事件（`source='user'` 受保护，重生成永不覆盖） |

其他组件通过 owner 方法更新；跨线程经 ContextVar（checkpoint）或显式参数传递，
不共享可变全局。压缩判定参数（`CONTEXT_LIMIT`、阈值）集中在 harness.py，
`agent_core` 通过 `_h()` 回读，测试 monkeypatch 一处生效。

## 并发与安全

只读工具白名单（read / glob / grep / ls / list_tasks / get_task / list_crons / check_inbox /
load_skill）用 4 线程池并行，批内含任一可变工具则整批串行兜底；整批 bash 且每条
命令都落在只读命令白名单（git status / ls / cat…，无重定向、无解释器、无外发，
`exec_policy.is_readonly_command` 与 read-only 档同源）时同样并行。write / edit
与第三方工具永不并行（同文件冲突、前像顺序、副作用未知）。线程内
`contextvars.copy_context()` 传播 checkpoint 上下文。结果原序写回（见不变式）。

远程权限审批走 `hooks.PermissionGate`：超时默认 300s、fail-safe deny、
已决议历史自动淘汰。工具执行统一经过 PreToolUse/PostToolUse hooks 与
入口截断；hooks 拿到的是全量输出，历史存的是截断形态。

文件工具的路径判定集中在 `exec_policy.classify_path`（workspace / outside /
secret / system）：PreToolUse 与 `tools.safe_path` 共用同一次解析，避免「批准
的是一个路径、写入的是另一个」；秘密文件在策略层硬拒，不产生审批请求。
秘密名单与快照层共用 `is_secret_path`：`.env.*`、`*.key`、`id_ed25519` 等
都在内，`.env.example` 这类公开模板显式例外——名单只有一份，例外也只解释一次。

网络出口在策略层单独判一遍：`curl` / `wget` / `http` / `nc` / `Invoke-WebRequest`
等登记的客户端命令一律询问（GET 也算），带请求体、改方法或管道进解释器的请求
按不透明载荷处理。「本会话允许同类」只给 curl/wget 的普通读请求，键是
`命令:主机`；发送、改方法、多目标、裸主机名都不记同类。复合命令（`&&` / `;` /
`|` / 换行）要求每段同类一致才记——一条批准的含义是「这一条我认了」，不是
「第一个动词我认了」。判定是文本分析，不是 shell 解析器；真正的出口约束
属于 OS 级沙箱/防火墙，不属于这一层。

ASK 决策的三个类别（解释器命令、破坏性命中、外发网络）先经 LLM 审查官复核
（`HARNESS_LLM_APPROVAL`，默认开；单次审查独立超时 `HARNESS_JUDGE_TIMEOUT`
默认 30s，不得占用审批 fail-safe 的等待预算）。审查口径对齐 Codex
workspace-write 沙箱：**沙箱内的常规操作默认放行**——工作区内文件的删除/重建、
解释器与脚本、本地状态查询都不问人；只有沙箱外的事升级人工（工作区外删除/
覆盖、系统修改、本机数据外传、秘密文件、下载并执行）。外发请求中普通读
（公共 API/包源/localhost 的 GET 类）可放行，带请求体/改方法/上传必升级。
放行记入审批历史（`resolved_by='llm-judge'`），升级或审查不可用一律回落
人工——fail-closed，审查官只在「少问」方向起作用。放行缓存归
`PermissionGate` 持有：`/revoke` 与档位切换连带清空（审查官批出的免审也是
预授权，撤销必须能收回），且只在交互式主循环生效——无头线程的审批语义是
排队等远程决议，不被旁路。审查官的定位是降噪，不承担安全责任：被审文本
就是模型生成的不可信字符串（提示词注入面），硬拒与秘密文件永远在策略层，
不由 LLM 覆盖；真边界属于 OS 级沙箱。工作区外写与第三方 MCP 工具不进 L1
（缺用户意图上下文 / 副作用不可见，人批 + 同类记忆）；`untrusted` / `never`
档语义不受影响；破坏性列表按命令词匹配而非子串。

飞书入口按个人单聊设计：未配置 `FEISHU_ALLOWED_USERS` 时 `main()` 拒绝启动
（本地测试只能用 `FEISHU_ALLOW_ANY_USER=1` 显式放开），群聊/话题群消息在入口
忽略。审批归属先于决议：请求按登记时的 `session_id` 只推给绑定该会话的聊天，
卡片按钮与 `/approve <id>` 还要求请求在本聊天的待审批 FIFO 中——拿到 req_id
不等于有权决议。没有会话上下文的后台任务审批仍广播给所有绑定聊天。

飞书网关用 4 线程 worker 池消费事件，阻塞回调会冻结整个分发循环；卡片更新
按内容变化触发并在实例级 0.45s 合流，防限流。markdown 回复走交互卡片
（lark_md），更新用仅接受 content 的专用 PATCH 端点。

## 展示与生命周期

所有会话列表、卡片、选择器的时间戳统一 `relative_time`（相对时间，跨年补
YYYY）；任务时长统一 `mm:ss`（≥1h 用 `hh:mm:ss`）；进度只暴露阶段与已用时。
会话标题由 LLM 异步生成（取材：首条有效 user 目标 + 最近结论 + 工具名），
每 8 回合重生成，用户 rename 永不覆盖；系统注入消息（reminder/通知/压缩备注）
不出现在 last_user / preview / 标题取材。

改动生效需重启 uvicorn API 与 feishu*gateway（CLI 每次新起）。诊断脚本
（`\_diag*_.py`、`*bench*_.py`）在问题闭环后删除，测试文件保留作回归护栏。

## 当前边界

以下为已知未做事项，新增能力继续遵守上述不变式：`TOOL_RESULTS_DIR` 已有 `/cache gc`
（默认只预览、保留 7 天、按会话引用保护，需显式 `--apply`），但没有自动定时回收；
`/watch` 第四阶段与飞书任务卡片事件驱动刷新
未实施；`/review` 只覆盖未提交 diff 与工作区内文件，不追踪跨仓库/依赖代码，
也不替代人工判断；长期 serving 与多 worker 并发未支持——当前
一次运行一个回合循环，取消在协作边界收敛。出口审批只覆盖登记的网络客户端
命令：解释器代码、未登记的程序仍可直连网络，路径边界不等于完整的外发边界；
需要内核级网络约束时应依赖 OS 级沙箱或防火墙。`web_fetch` 直连时会把连接固定
在已校验的 IP 上，但配置 HTTP 代理后 DNS 与连接都交给代理，地址预检不再代表
代理侧的解析结果——代理场景的 SSRF 边界依赖代理自身策略。
