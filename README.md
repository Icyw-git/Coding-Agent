# Harness Agent

Harness Agent 是一个面向大语言模型应用的 Agent Harness：它负责组织多轮对话、工具调用、会话持久化、上下文压缩和后台任务，并提供终端 UI 与 Web 可视化界面。

## 主要功能

- Agent 主循环与多轮对话
- OpenAI 兼容 API 调用
- 工具注册、权限控制与 MCP Server 接入
- Session JSONL 持久化、恢复与事件重放
- 上下文压缩、输出截断和模型错误恢复
- 后台任务与 cron 定时任务
- Subagent、Teammate、任务板和 Git Worktree 隔离
- Textual 终端 UI 与 React/Vite 可视化面板

## 环境要求

- Python 3.10+
- Node.js 18+（仅在使用 `visualizer` 时需要）
- 一个 OpenAI 兼容的模型服务

## 交互命令

在命令行对话模式输入 `/` 或 `/help` 查看分类命令，输入 `/help rewind` 查看单条命令用法。常用命令：

交互终端中，输入 `/` 会自动显示带说明的补全菜单，使用 Tab 或方向键选择、Enter 确认。
`/resume ` 可补全会话 ID，`/cd ` 可补全当前工作区下的目录（支持带空格的路径），
`/help ` 和 `/rewind --` 也支持补全。普通对话不弹出命令菜单，非交互输入仍按纯文本读取。

- `/new`（`/n`）：新建会话。
- `/resume`（`/r`）：打开会话选择器，展示名称、工作目录、最近任务和更新时间；也可用主题、目录、ID 前缀搜索后直接恢复。
- `/sessions`（`/s`）：打开会话管理，与不带参数的 `/resume` 使用同一界面。
- `/cd "D:\My Project"`：切换工作区，支持带空格的路径；`/pwd` 查看路径。
- `/checkpoint [标签]`、`/checkpoints`、`/rewind`：创建、查看和回溯快照。
- `/sandbox`：查看并切换执行档位与审批档位，见「执行安全与审批」。
- `/status` 查看状态，`/quit`（`/exit`）退出。

新建会话保留当前工作区；恢复会话会同时恢复已记录的工作目录。旧会话未记录目录时，明确提示并保留当前目录；目录不存在时不会切换。`/sessions` 只读取本地摘要，不调用模型。工作区切换不会自动新建会话。

会话主题在任务结束后自动在后台生成，无需手动命名。生成前使用任务内容作为临时名称；标题生成不阻塞回复。

会话管理采用双栏界面：左侧按工作区分组，显示主题、状态和相对时间；右侧预览最近任务与回复。
默认查看全部工作区，输入关键词搜索主题、任务或目录；↑↓ 选择、PageUp/PageDown 翻动、
Enter 切换、Esc 返回，Ctrl+A 切换当前/全部工作区。Tab/Shift+Tab 筛选全部、当前、历史、空会话。
状态描述本地会话关系，不将历史日志中的未完成调用视为仍在运行。界面隐藏内部 ID 和事件数，
终端宽度不足 100 列时隐藏右侧预览。非交互输入仅输出列表，不自动切换。

会话显示时间和排序统一使用最后一条用户或 Agent 消息的时间，后台生成标题、
切换目录和文件复制不影响活跃顺序。创建时间取首条事件，旧记录无需迁移；空会话使用创建时间。

`/archive` 多选归档 30 天未活跃的旧会话，`/archive 90` 调整期限，
`/archive empty` 仅选择空会话。当前会话不可归档，选择后还需确认。
会话日志和关联快照移动到会话目录的 `.archive` 下，不影响项目文件、记忆和任务。
`/archive restore` 多选恢复归档，恢复后可通过 `/resume` 继续聊天。
`/archive all` 可手动选择任意非当前会话，不受天数限制。
`/delete` 多选永久删除已归档会话及其关联快照，需输入“删除”确认，不可恢复。
活动会话不会出现在删除列表中；项目文件、记忆和任务不受影响。

快照记录所属工作区，跨工作区的快照链禁止恢复文件，`--force` 不绕过此限制。
旧快照未记录工作区时，同样仅支持 `/rewind <快照> --session` 恢复对话，不猜测文件所在目录。
清理前请停止其他正在使用同一会话的 CLI/API 服务；归档不支持跨进程协调。

### 飞书会话管理

- `/sessions` 或 `/resume`：按工作区分组的会话卡片，每页 5 条，按钮支持详情、切换和翻页。
- `/sessions 2`：文字翻页；卡片不可用时仍可通过列表序号操作。最近消息只读预览，不调用模型。
- `/resume 1`：按最近一次列表的序号切换；`/resume 关键词` 搜索主题、目录或任务；`/use` 保留兼容。
- `/archive [天数|all|empty|restore]`：列出可归档或恢复的会话，默认 30 天未活跃。
- `/archive select 1 3`：选择列表中的会话，预览后用 `/confirm` 执行，或 `/cancel` 取消。
- `/delete`、`/delete select 1 3`：选择已归档会话；必须用 `/confirm 删除` 才永久删除。
- `/sandbox [档位|approvals <档位>]`：查看或切换服务端的执行/审批档位，对所有会话立即生效。

会话列表最多读取 1000 条，每次重新展示列表会让旧列表按钮失效，10 分钟后也需重新 `/sessions`。
按钮受用户白名单及聊天来源校验，会话已删除或目录失效时不切换。
归档/删除候选每次展示最多 20 条，操作预览 5 分钟后失效。仍被任意飞书聊天绑定的会话不参与归档/删除，
可先在对应聊天 `/quit` 解绑。API 会拒绝处理本进程正在执行任务的会话。
飞书 `/cd <目录>` 切换当前会话工作区，普通对话的文件工具、shell、提示词和 `/run` 使用同一目录；
`/cd` 或 `/pwd` 查看当前目录。相对路径基于当前会话工作区，支持带引号的空格路径。
`/new` 保留当前工作区，`/resume` 恢复所选会话目录；目录保存在会话日志中，重启后仍有效。
工作区必须存在并位于服务端 `HARNESS_WORKSPACE`（默认启动目录）下；目录失效时拒绝执行，
可用绝对路径重新 `/cd`。Agent 正在执行当前回合时暂不允许切换目录，其他会话不受影响；
已启动的后台任务继续使用启动时的目录。
会话日志、快照、记忆和任务的存储位置仍由服务端配置管理，不随 `/cd` 移动。

API 使用 `GET /v1/sessions/{id}/workspace` 查看、`PUT /v1/sessions/{id}/workspace`
（请求体 `{"path":"子目录"}`）切换；创建会话时也可传 `workspace`。
SDK 对应 `client.sessions.workspace(id)`、`client.sessions.change_workspace(id, path)`，
以及 `client.sessions.create(workspace=path)`。

审批消息使用独立处理通道；即使普通消息处理线程全部占用，仍可通过审批卡片、
`/approve y|n` 或 `y/n` 回复解除等待，用户白名单同样生效。
并发会话的文件写入快照按各自 turn 隔离，不会写入其他会话的 checkpoint。

普通对话流式回复会在等待时每 5 秒更新处理状态、当前工具及耗时，完成后替换为最终答案。
断流或报错会更新原卡片为“处理未完成”，不会自动重复执行任务。
运行超过 60 秒时，API 会在 `.cache/api/api-access.log` 记录工作线程调用栈；
设置 `HARNESS_API_LOG` 后使用对应日志路径。这里的进度心跳不代表任务已完成。
同步 shell 工具执行超过 120 秒会请求终止进程树，并向 Agent 返回超时结果。

## 执行安全与审批

所有内建工具中会动到系统的调用都经过同一层策略判定（`src/exec_policy.py`）。四个执行入口——CLI 的
PreToolUse 钩子、工具边界（`run_bash` / `run_write` / `run_edit`）、Teammate 线程、后台与
HTTP 任务——共用同一套规则和同一句拒绝文案，不再各写一份黑名单。

两档配置，命名与 Codex 对齐，默认值就是本项目重构前的实际行为：

| 档位 | 取值 | 默认 |
| --- | --- | --- |
| `sandbox_mode` | `read-only`、`workspace-write`、`danger-full-access` | `workspace-write`：工作区内写改放行，破坏性命令询问 |
| `approval_policy` | `untrusted`、`on-request`、`never` | `on-request`：只在策略层判定有风险时询问 |

- `read-only` 只放公认不改动的命令（`dir`、`ls`、`git status` 等）；解释器、命令替换（`$()` / 反引号）
  和带重定向的命令仍要审批。
- `danger-full-access` 取消提示级限制和工作区路径约束，只保留黑名单硬拒。
- `never` 的语义是「不用问我」：`danger-full-access` 下普通询问直接放行；其余档位没有 OS 沙箱兜底，
  需要审批的操作直接失败（fail-closed），而不是静默执行。
- 黑名单、根删（`rm -rf --no-preserve-root /`）、`cmd /c` 包装、`su""do` 拆引号等变体在硬拒层，
  档位和白名单都越不过去。
- 默认档下，`read`/`write`/`edit` 文件工具的工作区外访问直接硬拒；`danger-full-access` 会放开这条路径检查。
  **这不约束 shell 命令的写入路径**：例如重定向、`mkdir` 或解释器代码可能访问工作区外；Stage 1 不能视为文件系统隔离。
- 第三方 MCP 工具当前不经过这一层判定（P0.2 计划覆盖）；启用 MCP 时不要把 `read-only` 当成隔离。

配置入口：`/sandbox` 查看和切换（CLI、飞书、`gateway_sim`、`GET/POST /v1/policy` 都走服务端同一份
判定，运行时立即生效）；也可以写进环境 `HARNESS_SANDBOX_MODE` 与 `HARNESS_APPROVAL_POLICY`
（旧开关 `HARNESS_APPROVAL_ALL=1` 等价于 `approval_policy=untrusted`，仅在没有显式配置新档位时兜底）。
档位名写错不会静默回落到默认档：`/sandbox` 报用法，环境变量按默认档并在 stderr 提示一次，
`/status` 和 `--doctor` 始终显示实际生效的档位。

审批有三条通道：CLI 主循环读 stdin；HTTP 服务模式（`create_app`）把请求登记到 `/v1/permissions`
队列，由飞书卡片或 `/approvals` 决议；Teammate、后台 worker、任务 runner 这些不能抢控制台的线程
只排队等远程决议。超时（`HARNESS_APPROVAL_TIMEOUT`，默认 300 秒）未决议一律拒绝。
一次批准会以一次性凭据传到工具边界，同一次调用不会被问第二遍；「本次会话允许同类」的白名单按键
按会话分区，不会让某个会话批过的 `rm` 在别的会话也免审。

⚠ 这一层是策略级判定，不是操作系统沙箱：它不隔离文件读取和网络，也不保证进程无法绕开。
内核级隔离（Windows Job Object / AppContainer、Linux seccomp）属于后续阶段。

## 快速开始

### 1. 获取代码

```bash
git clone <你的 GitHub 仓库地址>
cd harness-agent
```

### 2. 配置模型服务

在项目根目录创建 `.env`，示例：

```env
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL_ID=your-model-id

# 可选：MCP 配置文件
MCP_SERVERS_FILE=.cache/mcp_servers.json

# 可选：textual 或 ansi
HARNESS_UI=ansi

# 可选：记忆召回 llm（默认）、local（本地筛选，无网络请求）、off
HARNESS_MEMORY_RECALL=local

# 可选：执行档位 read-only / workspace-write（默认）/ danger-full-access
HARNESS_SANDBOX_MODE=workspace-write

# 可选：审批档位 untrusted / on-request（默认）/ never；也可运行中用 /sandbox 切换
HARNESS_APPROVAL_POLICY=on-request

# 可选：启用分段计时，启动前设置
HARNESS_PERF=1
```

`local` 使用中英文关键词匹配，语义召回能力与 `llm` 不同；不设置时默认 `local`（每轮省一次 LLM 筛选往返，约 2s）。
旧开关 `HARNESS_LOAD_MEMORIES=0` 仍会关闭召回。
性能统计可通过 `perf.snapshot()` 读取当前执行上下文的数据；不同请求独立统计，
飞书网关与 API 服务也分别统计，尚未自动合并为端到端报告。
嵌套分段存在包含关系，不能直接相加。

常用交互命令还包括 `/diff`（只读查看当前工作区改动）、`/compact`（保存完整记录后压缩当前上下文）、
`/undo`（回到最近一次快照）和 `/approvals`（查看待审批操作）。飞书网关同步支持
`/model`、`/approvals`、`/perms` 和 `/sandbox`；文件查看与上下文压缩仍建议在 CLI 中执行。

请勿提交 `.env` 或任何真实 API 密钥；`.gitignore` 已默认忽略该文件。

MCP 配置文件使用 JSON 数组，例如：

```json
[
  {
    "name": "fetch",
    "command": "D:/Anaconda_envs/envs/aitest01_py310/python.exe",
    "args": ["-m", "mcp_server_fetch"]
  }
]
```

示例需要在对应环境安装 `mcp-server-fetch`。仅支持通过本地命令启动的 stdio MCP。
CLI 启动、默认 SDK 客户端创建、FastAPI 服务启动都会加载相同配置；
飞书通过 API 使用这些工具，无需在网关重复配置。同一进程只注册一次，修改配置后需重启 Agent 服务。
API 在启动完成前连接 MCP，关闭时清理连接；外部注入 runtime 的工具生命周期由调用方管理。
启动日志 `[mcp] fetch: ... tool(s)` 表示连接成功，失败的服务会输出原因并跳过。

### 3. 安装 Python 依赖

```bash
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

macOS / Linux：

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 4. 启动 Agent

```bash
python entrypoint.py
```

首次启动时，程序会进入交互式 Agent 会话。使用 `HARNESS_UI=ansi` 可在不支持全屏终端的环境中运行。

启动前检查本地配置和目录：

```bash
python entrypoint.py --doctor
```

恢复已保存的会话：

```bash
python entrypoint.py --resume <session_id>
```

交互过程中可使用 `/status` 查看当前运行状态，使用 `/sessions` 查看已保存会话，使用 `/resume <session_id>` 切换到已有会话。

启动 HTTP API（可选）：

  ```bash
  python -m pip install -r requirements.txt
  python -m pip install -r requirements-api.txt
  # PowerShell: $env:HARNESS_API_TOKEN = "replace-with-a-long-random-token"
  uvicorn api:create_app --factory --host 127.0.0.1 --port 8000
  ```

  API 提供 `POST /v1/sessions`、`GET /v1/sessions/{id}`、`POST /v1/sessions/{id}/messages` 和
  `GET /v1/sessions/{id}/events`（SSE）；`GET/POST /v1/tasks` 系列管理长命令，
  `GET /v1/permissions` 与 `POST /v1/permissions/{id}/resolve` 用于远程决议审批，
  `GET/POST /v1/policy` 读写执行/审批档位。所有 `/v1/` 请求都需要
  `Authorization: Bearer <HARNESS_API_TOKEN>`；请求元数据写入 `.cache/api/api-access.log`（如需文件日志，
  通过 SDK/应用工厂传入 `access_log_path`）。API 默认启用每个客户端和路径每分钟 60 次的轻量限流。

  局域网手机访问：将监听地址改为 `--host 0.0.0.0`，确保电脑和手机在同一 Wi-Fi，查找电脑 IPv4 地址后在手机打开
  `http://<电脑IPv4>:8000/docs`。首次使用时在 Swagger 页面点击 **Authorize**，填入
  `Bearer <HARNESS_API_TOKEN>`。Windows 防火墙需要允许该端口的专用网络访问；不要在公网直接暴露此端口。

## 启动可视化界面

```bash
cd visualizer
npm install
npm run dev
```

生产构建：

```bash
npm run build
npm run preview
```

## 测试

```bash
python -m pytest
```

测试文件位于 `tests/`，覆盖会话存储、上下文压缩、记忆、任务系统、Worktree、执行策略
（`test_exec_policy.py` 测判定矩阵，`test_execution_paths.py` 测四条执行入口的接线）等模块。
在受限环境中，可为 pytest 指定一个可写的临时目录：

```bash
python -m pytest --basetemp /tmp/harness-pytest
```

`--basetemp` 请指向仓库外的目录：落在 Git 工作树内时，依赖「当前目录不是 Git 仓库」的用例会失真。

## 项目结构

| 路径 | 说明 |
| --- | --- |
| `entrypoint.py` | 命令行入口、服务启动和退出清理 |
| `harness.py` | Agent 主循环、REPL 和运行时编排 |
| `src/` | Agent 子系统、工具、会话、任务、MCP 和 Worktree 等 Python 源码 |
| `src/agent_runtime.py` | 可复用的会话运行时，供 CLI、SDK 和 API 适配层调用 |
| `src/exec_policy.py` | 执行策略层：档位、黑名单与审批分级（纯判定，不做 OS 强制） |
| `src/hooks.py` | PreToolUse 闸门与审批中心（控制台 / 远程 / 无头排队三种后端） |
| `src/harness_sdk.py` | Python SDK facade，封装会话和消息操作 |
| `src/harness_api.py` | FastAPI app factory 和 HTTP/SSE 适配层 |
| `tests/` | 自动化测试与测试辅助程序 |
| `docs/` | 架构、检查点设计和开发记录 |
| `archive/legacy/` | 已停用的历史实现 |
| `visualizer/` | React/Vite Web 可视化前端 |

## 分支说明

- `main`：主开发分支，包含可合并和发布的基础功能。
- `feature/ui`：终端 UI、会话指标和可视化相关功能。
- `feature/cd-support`：持久化 `cd` 和当前工作目录支持。

建议从 `main` 创建功能分支，完成测试后通过 Pull Request 合并。

## 推送到 GitHub

在 GitHub 创建一个空仓库后执行：

```bash
git add README.md
git commit -m "docs: add project README"
git remote add origin https://github.com/<用户名>/<仓库名>.git
git push -u origin main
```

如需同时上传功能分支：

```bash
git push -u origin feature/ui
git push -u origin feature/cd-support
```

## License

License 待补充。
