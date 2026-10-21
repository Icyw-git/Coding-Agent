# 编码标准

[设计准则](design-principles.md)

适用于根目录 `harness.py`、`src/` 与 `tests/` 的新增及修改代码。

## 目录与入口

核心 agent 循环与 CLI 编排放根目录 `harness.py`；可复用原语（压缩、记忆、hooks、
会话存储、runtime 服务、传输网关）放 `src/`，按职责单文件命名，公共逻辑归所属层，
不在 CLI 与网关之间复制。`tests/` 在根目录，与被测模块一一对应。

入口三个：CLI REPL（`harness.py`）、控制面 API（`uvicorn api:create_app --factory`，
即 `src/harness_api.py`）、飞书网关（`src/feishu_gateway.py`，lark-oapi WS 长连接）。
模块导入和普通构造器不启动网络、线程或子进程；openai SDK 与 MCP 桥接懒加载
（`_LazyLLMClient` 透明代理 + PEP 562 模块 `__getattr__`），预热只挂 CLI 启动与
API lifespan 的后台线程。

CLI 只适配输入输出，runtime 与工具不反向依赖入口和网关。`src/` 模块不 import
harness：工作区、工具表、技能目录与 model client 由入口采集后注入（如
`prompt_assembly.collect_context()`、`session_title.generate_session_title(messages, client)`），
保证每个 `src` 模块都能脱离 CLI 单独导入与测试。路径一律 `Path`；
环境变量用 `HARNESS_` 前缀（`HARNESS_PROGRESS_INTERVAL`、`HARNESS_APPROVAL_TIMEOUT`），
模型上下文窗口查 `MODEL_WINDOWS` 表再允许 `LLM_CONTEXT_WINDOW` 覆盖，未知模型
打印告警并保守回落，不静默降级。

## 类型与状态

Append-only JSONL 事件流（`session_store.py`）是会话的唯一恢复真相源；事件字段
固定为 `v / seq / session_id / type / ts` + 载荷，新增事件类型必须能在
`rebuild_openai_history` 确定性回放，并在 `FORK_EVENT_TYPES` 中登记是否随分叉复制。

`SessionEventStore` 拥有 seq 分配、路径锁与事件缓存；实例级 `_events_cache` 的
清空必须发生在 `__init__` 的 `_read_last_seq()` 之前（它会触发首次读取）。每次
append 单行写入并 `os.fsync`；跨实例同路径共享类级锁。

FastAPI 的请求/响应模型定义在模块级——`from __future__ import annotations` 下，
函数内定义的 Pydantic 模型会解析失败、参数被误判为 query（422 的历史教训）。
固定结构的结果用 dataclass；事件载荷与协议容器用 dict，JSON 转换只发生在
store 边界（rebuild 内部用 json 往返代替 deepcopy）。

## 注释、规则与日志

块注释用中文说明「为什么」——约束、教训与不变式，重点覆盖事件顺序、缓存稳定、
跨线程通信与失败恢复；docstring 说明整体契约，不复述显然语句。形成过事故的
约束把实测数据写进注释（如「无迟滞时越线每回合滑动，实测命中率 6-19%」）。

用户可见输出使用约定前缀行：`[tokens]`、`[compact]`、`[transcript saved:…]`、
`[session log:…]`；`[HOOK]` 调试打印必须经 `HARNESS_DEBUG` 门控。业务代码不向
stdout 打印诊断；进度展示只暴露 `phase / tool / elapsed`，工具内部耗时留在诊断。

规则表（斜杠命令、白名单、前缀集合）与文档同步更新：新增斜杠命令必须同步
`tests/test_cli_input.py` 的 SlashCompleter 前缀断言；改工具行为必须同步
`tool_schemas.py` 与相关文档；顺序由 `tests/test_tool_schemas.py` 锁定。

## 空行与逻辑块

| 位置 | 空行 |
|---|---|
| 模块级函数或类之间 | 两行 |
| 类的方法之间 | 一行 |
| 循环内不同阶段之间 | 一行 |
| import 分组之间 | 遵循现有分组（标准库 / 三方 / 项目内） |

`agent_loop` 主循环按固定阶段分段：checkpoint → 压缩预处理 → 注入（todo/cron）→
LLM 调用与错误恢复 → assistant 落盘 → 工具批次 → 后台结果注入。同一阶段的
连续语句保持紧凑；注释属于其后的逻辑块。

## 资源与测试

后台线程（housekeeping、记忆提炼）一律 daemon + 登记 + `wait_for_housekeeping()`
供测试同步；线程内异常只告警不影响主流程。定时任务、后台任务、任务管理器的
存储分别归属各自模块，任务执行目录是 `.cache/os_tasks`（不是 task_system 的
`.cache/tasks`）。

测试写入必须隔离：`tests/conftest.py` 的 `_isolate_storage_dirs` autouse fixture
把 `SESSIONS_DIR / CHECKPOINT_DIR / TRANSCRIPT_DIR` 重定向到 tmp；涉及落盘工具
输出的测试自行 monkeypatch `TOOL_RESULTS_DIR`。禁止写真实 `.cache`（历史上灌入
336 个测试会话）。断言行为与事件效果，不镜像实现细节。

```sh
D:\Anaconda_envs\envs\aitest01_py310\python.exe -m pytest tests/ -q
```

全量回归必须用 aitest01_py310 解释器（base Anaconda 无 fastapi/textual，
收集即报错）。
