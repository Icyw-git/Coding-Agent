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
- `/sessions`（`/s`）：列出最近会话。
- `/cd "D:\My Project"`：切换工作区，支持带空格的路径；`/pwd` 查看路径。
- `/checkpoint [标签]`、`/checkpoints`、`/rewind`：创建、查看和回溯快照。
- `/status` 查看状态，`/quit`（`/exit`）退出。

新建会话保留当前工作区；恢复会话会同时恢复已记录的工作目录。旧会话未记录目录时，明确提示并保留当前目录；目录不存在时不会切换。`/sessions` 只读取本地摘要，不调用模型。工作区切换不会自动新建会话。

会话主题在任务结束后自动在后台生成，无需手动命名。生成前使用任务内容作为临时名称；标题生成不阻塞回复。空会话默认从列表隐藏。

恢复选择器支持直接输入关键词搜索主题、最近任务和目录，↑↓ 选择、Enter 恢复、Esc 取消。
默认查看当前工作区，Ctrl+A 切换全部工作区。列表使用相对时间，隐藏内部 ID 和事件数；
主题与最近任务相同时不重复显示。空会话只隐藏、不删除，仍可通过完整 ID 恢复。

会话显示时间和排序统一使用最后一条用户或 Agent 消息的时间，后台生成标题、
切换目录和文件复制不影响活跃顺序。创建时间取首条事件，旧记录无需迁移；空会话使用创建时间。

`/archive` 多选归档 30 天未活跃的旧会话，`/archive 90` 调整期限，
`/archive empty` 仅选择空会话。当前会话不可归档，选择后还需确认。
会话日志和关联快照移动到会话目录的 `.archive` 下，不影响项目文件、记忆和任务。
`/archive restore` 多选恢复归档，恢复后可通过 `/resume` 继续聊天。
清理前请停止其他正在使用同一会话的 CLI/API 服务；归档不支持跨进程协调。

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
```

请勿提交 `.env` 或任何真实 API 密钥；`.gitignore` 已默认忽略该文件。

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
  `GET /v1/sessions/{id}/events`（SSE）。所有 `/v1/` 请求都需要
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

测试文件位于 `tests/`，覆盖会话存储、上下文压缩、记忆、任务系统和 Worktree 等模块。
在受限环境中，可为 pytest 指定一个可写的临时目录：

```bash
python -m pytest --basetemp .test-tmp
```

## 项目结构

| 路径 | 说明 |
| --- | --- |
| `entrypoint.py` | 命令行入口、服务启动和退出清理 |
| `harness.py` | Agent 主循环、REPL 和运行时编排 |
| `src/` | Agent 子系统、工具、会话、任务、MCP 和 Worktree 等 Python 源码 |
| `src/agent_runtime.py` | 可复用的会话运行时，供 CLI、SDK 和 API 适配层调用 |
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
