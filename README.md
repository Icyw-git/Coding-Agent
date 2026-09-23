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
python harness.py
```

首次启动时，程序会进入交互式 Agent 会话。使用 `HARNESS_UI=ansi` 可在不支持全屏终端的环境中运行。

启动前检查本地配置和目录：

```bash
python harness.py --doctor
```

恢复已保存的会话：

```bash
python harness.py --resume <session_id>
```

交互过程中可使用 `/status` 查看当前运行状态，使用 `/sessions` 查看已保存会话，使用 `/resume <session_id>` 切换到已有会话。

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

测试文件位于项目根目录，覆盖会话存储、上下文压缩、记忆、任务系统和 Worktree 等模块。
在受限环境中，可为 pytest 指定一个可写的临时目录：

```bash
python -m pytest --basetemp .test-tmp
```

## 项目结构

| 路径 | 说明 |
| --- | --- |
| `harness.py` | Agent 主循环与程序入口 |
| `agent_core.py` | Hook、记忆、上下文压缩和提示词上下文 |
| `tools.py` | 工具 Schema、注册表和工具执行 |
| `session_store.py` | Session JSONL 存储与历史恢复 |
| `recovery.py` | API 错误、重试和输出恢复 |
| `mcp_bridge.py` | MCP Server 连接与动态工具注册 |
| `agent_teams.py` | Teammate 协作与 Worktree 上下文 |
| `task_system.py` | 持久化任务板和任务依赖 |
| `visualizer/` | React/Vite Web 可视化前端 |
| `test_*.py` | 自动化测试 |

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
