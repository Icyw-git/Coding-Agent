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
- 工作区项目说明（`AGENTS.md` / `CLAUDE.md`）与内建/项目技能合并
- 一次性无头执行（`-p` / `--json`）与只读代码审阅（`/review`）
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

- `/new`（`/n`）：新建会话；先选择工作区（当前目录、已登记工作区和最近会话目录按最近
  活跃排序；也可以选「浏览电脑中的文件夹…」逐级挑选：在首行「进入此文件夹」按 Enter
  确认，在子目录上按 Enter 进入，不需要输入路径）。`/new --ws <序号|名称|路径>` 直接
  指定，适合脚本；非交互环境不带参数时仍在当前目录新建。
- `/workspaces`：列出已登记工作区（名称、路径、会话数和最近使用时间）。
- `/workspace add <路径> [--name 名称]`、`/workspace remove <序号|名称|路径>`：维护登记表。
  登记表默认在 `.cache/workspaces.json`，跨重启保留；它只用于发现和排序，不扩大授权。
- `/resume`（`/r`）：打开会话选择器，展示名称、工作目录、最近任务和更新时间；也可用主题、目录、ID 前缀搜索后直接恢复。
- `/sessions`（`/s`）：打开会话管理，与不带参数的 `/resume` 使用同一界面。
- `/cd "D:\My Project"`：切换当前会话的工作区，支持带空格的路径；`/pwd` 查看路径。
  换项目建议用 `/new` 开新会话——工作区随会话记录，`/cd` 只作兼容与迁移。
- `/diff`：只读查看当前工作区未提交改动（文件状态 + 逐行 diff）。
- `/review`：只读子代理审阅未提交改动并输出 P0–P3 分级问题。工具面只有
  `read`/`glob`/`grep`/`ls` 且限制在工作区内，敏感文件既不进 diff 也读不到。
- `/checkpoint [标签]`、`/checkpoints`、`/rewind`：创建、查看和回溯快照。
- `/cache [gc [天数] [--apply]]`：查看 `.cache/tool_results` 占用；`/cache gc 7` 预览、
  加 `--apply` 才会删除「超过保留期且未被任何会话引用」的文件。
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
- `/revoke`：撤销当前会话已批准的「同类」预授权（换档时服务端会连带全部撤销）。

网关默认只处理单聊（p2p），群聊/话题群消息直接忽略；启动时必须配置 `FEISHU_ALLOWED_USERS`，
只有本地测试可用 `FEISHU_ALLOW_ANY_USER=1` 显式放开。审批卡片和 `/approve <id>` 只接受已经推送给
当前聊天的请求；没有会话上下文的后台任务审批仍广播给所有绑定聊天。

会话列表最多读取 1000 条，每次重新展示列表会让旧列表按钮失效，10 分钟后也需重新 `/sessions`。
按钮受用户白名单及聊天来源校验，会话已删除或目录失效时不切换。
归档/删除候选每次展示最多 20 条，操作预览 5 分钟后失效。仍被任意飞书聊天绑定的会话不参与归档/删除，
可先在对应聊天 `/quit` 解绑。API 会拒绝处理本进程正在执行任务的会话。
飞书 `/cd <目录>` 切换当前会话工作区，普通对话的文件工具、shell、提示词和 `/run` 使用同一目录；
`/cd` 或 `/pwd` 查看当前目录。相对路径基于当前会话工作区，支持带引号的空格路径。
`/new` 不带参数时发送工作区选择卡片（当前 + 已登记 + 最近使用；卡片不可用时发文本列表），
回复 `/new <序号>` 选择；也可 `/new ws:<序号|名称|路径>`、`/new <路径>`、`/new id:<会话ID>`。
序号和卡片按钮只对最近一次列表有效（10 分钟），`/cd`、`/resume`、`/fork` 后失效；
`/new id:<会话ID>` 与 `/new <会话ID>` 兼容旧用法并沿用当前工作区。
`/workspaces` 列出登记项；`/workspace add <路径> [--name 名称]`、`/workspace remove <序号|名称|路径>`
维护登记表。相对路径 `add` 基于当前会话工作区。
`/resume` 恢复所选会话目录；目录保存在会话日志中，重启后仍有效。
工作区必须存在，且位于服务端启动授权的根内：`HARNESS_WORKSPACE`（默认启动目录）加
`HARNESS_WORKSPACE_ROOTS`（额外根，`os.pathsep` 分隔）。登记表不参与授权——手工往
`workspaces.json` 里写一个未授权目录，只会显示为「未授权」，创建前仍被拒绝。
目录失效时拒绝执行，可用绝对路径重新 `/cd`。Agent 正在执行当前回合时暂不允许切换目录，
其他会话不受影响；已启动的后台任务继续使用启动时的目录。
会话日志、快照、记忆和任务的存储位置仍由服务端配置管理，不随 `/cd` 移动。

API 使用 `GET /v1/sessions/{id}/workspace` 查看、`PUT /v1/sessions/{id}/workspace`
（请求体 `{"path":"子目录"}`）切换；创建会话时也可传 `workspace`。
工作区登记表对应 `GET /v1/workspaces?current=`、`POST /v1/workspaces`
（请求体 `{"path":"...","name":"..."}`）和 `DELETE /v1/workspaces/{id}`；
远程登记只接受已授权根内的路径，返回项带 `saved` / `exists` / `allowed` 状态。
SDK 对应 `client.sessions.workspace(id)`、`client.sessions.change_workspace(id, path)`、
`client.sessions.create(workspace=path)`，以及
`client.workspaces.list()` / `add(path, name)` / `remove(id)`。

审批消息使用独立处理通道；即使普通消息处理线程全部占用，仍可通过审批卡片、
`/approve y|n` 或 `y/n` 回复解除等待，用户白名单同样生效。
LLM 审查官自动放行的请求不进待审批队列（登记与决议在同一次调用里完成），网关从
`/v1/permissions` 的 `recent` 补一条不带按钮的「已放行」通知；只推网关启动之后产生的
请求，重启不补历史，避免一上线把旧放行全推一遍。
并发会话的文件写入快照按各自 turn 隔离，不会写入其他会话的 checkpoint。

普通对话流式回复会在等待时每 5 秒更新处理状态、当前工具及耗时，完成后替换为最终答案。
断流或报错会更新原卡片为“处理未完成”，不会自动重复执行任务。
运行超过 60 秒时，API 会在 `.cache/api/api-access.log` 记录工作线程调用栈；
设置 `HARNESS_API_LOG` 后使用对应日志路径。这里的进度心跳不代表任务已完成。
同步 shell 工具默认 120 秒超时，可用 `timeout` 调整（上限 3600 秒），超时会请求终止进程树
并向 Agent 返回超时结果。`cwd` 让单条命令在指定目录执行；显式 `run_in_background` 或命中
安装/构建/测试等慢命令关键词时转入后台（后台默认上限 3600 秒），用 `background_output`
等待或读取输出、`kill_background` 终止。

## 项目说明与技能

工作区根目录的说明文件会作为 `project` 段落注入主 system prompt 与子代理 system prompt，
按 `AGENTS.override.md` → `AGENTS.md` → `CLAUDE.md` 取第一个命中的文件（文件名大小写不敏感）：

- 只读工作区根目录，不向上查找；指向工作区外的符号链接会被忽略。
- 超过 32 KiB 时截断到最近的换行并附一行说明；`HARNESS_PROJECT_CONTEXT=off` 可整体关闭。
- 按 mtime/size 失效重读；切换工作区后下一轮重新组装，`/review` 也会带上它。
- 它属于 system prompt 的一部分，不进入 canonical history；其中的指令不能覆盖安全与审批规则。

技能目录合并两处扫描：随发行版分发的 `skills/`（`APP_DIR/skills`）与当前工作区的
`skills/`（`SKILLS_DIR`）。同名技能以工作区版本为准；system prompt 只放名称与描述，正文通过
`load_skill` 按需加载。`skill.md` 的 mtime/size 变化或工作区切换（CLI `/cd`、`/new`）后会重新扫描。

## 内建工具

- `read` 支持 `offset` / `limit` 分段读取；超过 500 行且未给 `limit` 时只返回 200 行并提示下一次
  的 `offset`。编码按 BOM → UTF-8 → GB18030 探测，二进制文件（magic/NUL）直接拒绝。
- `view_image` 读取本地 PNG/JPEG/GIF/WebP/BMP，图片以独立的 user 多模态消息附到下一轮请求。
  历史、事件流和转录里只保存缓存文件引用，base64 在每次请求前由 `expand_image_parts` 现读现拼，
  不会把上下文和会话文件撑大；需要模型本身支持视觉输入。上下文预算按图片实际宽高和模型族
  （OpenAI 瓦片 / Claude 面积 / Gemini 768 瓦片 / DeepSeek 1024 上限）估算，未知尺寸时
  用固定兜底值，可用 `HARNESS_IMAGE_TOKEN_ESTIMATE` 调整。DeepSeek 的 `deepseek-flash`
  同样要求图片只出现在 user 消息里，和这里的附件布局一致。
- `read_document` 抽取 PDF/DOCX/PPTX/XLSX 的文本进入上下文：PDF 读文本层，`pages` 支持
  `1-5,8` / `10-` / `all`（默认前 20 页）；PPTX 按幻灯片并附演讲者备注；XLSX 按工作表输出
  Markdown 表格（默认前 8 个工作表）。DOCX/PPTX/XLSX 用标准库解析 OOXML，不额外依赖
  python-docx/pptx，也不需要 LibreOffice；扫描版 PDF 没有文本层，需要先转成图片再用
  `view_image`（不做 OCR）。读 PDF 文本层需要可选的 `pypdf`。
- `render_document` 把 PDF/DOCX/PPTX 的指定页渲染成 PNG，走后一轮 user 多模态附件，
  用来查看排版、图表、表格对齐和扫描页。PDF 用 pypdfium2 直接渲染；DOCX/PPTX 先用
  LibreOffice headless 转 PDF 再渲染（未安装时给出明确提示，不启动 Office 桌面程序）。
  这两条渲染路径需要可选的 `requirements-docs.txt`，DOCX/PPTX 还需要系统里的 `soffice`；
  不需要看排版时直接用 `read_document`。默认前 3 页、单次最多 8 页，`dpi` 支持 72–200；
  XLSX 请用 `read_document` 读单元格。
- `todo_write` / `todo_read` 维护当前会话的任务清单：整表替换、状态只能是
  `pending/in_progress/completed`、最多 100 项、同一时间只允许一个 `in_progress`。
  清单按会话隔离，并作为 `todos` 事件写入事件流，会话恢复后 `todo_read` 仍能读到。
- `write` / `edit` / `apply_patch` 回写时保留原文件编码与换行符（UTF-16/UTF-8 BOM、GBK、CRLF）。
  `edit` 的 `old_text` 在文件中出现多次时默认报错，确认要全改才传 `replace_all=true`，
  不会再静默地只替换第一处。
- `apply_patch` 接受 Codex 风格的 `*** Begin Patch` 补丁，支持 Add/Update/Delete File 和
  `*** Move to:`。所有目标先规划、校验，再统一落盘；任一步失败会按逆序回滚已写的文件，
  不会留下半份补丁。每个目标都走与 `write`/`edit` 相同的路径分区与 checkpoint。
- `bash` 支持 `cwd` / `timeout` / `run_in_background`；后台任务有独立 id，输出在收集后仍保留
  有限历史供 `background_output` 取回，`kill_background` 终止进程树并标记为 cancelled。
- `grep` 在可用时走 ripgrep（`--no-ignore` + 秘密/忽略目录过滤），遇到非 ASCII 模式、
  rg 缺失、编码损坏或异常输出时回落到内置遍历，结果语义保持一致；`glob` / `ls` 与 `grep`
  同样接受 `cwd`，teammate 在 worktree 里不会误搜主工作区。
- `web_fetch` 用于读取公开网页：仅允许 http/https，解析后所有地址必须是公网（拒绝回环、
  私网、链路本地、云元数据主机与 `.internal` 等后缀），每次重定向重新校验；直连时把连接
  固定在校验过的 IP 上（Host/SNI 仍用原主机名），避免两次 DNS 解析之间的重绑定；响应上限
  2 MiB、默认 20 秒超时，HTML 会提取为文本。它和 `curl` 一样要过出口审批，且不能绕过出口
  策略。配置了 HTTP 代理时 DNS 由代理解析，固定地址不生效，预检结果也不代表代理的解析。
- 子代理启动时按当时的工具注册表生成工具面，不会拿到启动时快照里已删除的工具；`task`、
  teammate/计划/定时任务、worktree、`background_output`/`kill_background` 等控制类工具不向
  子代理暴露，bash 也不提供 `run_in_background`。

## 执行安全与审批

所有内建工具中会动到系统的调用都经过同一层策略判定（`src/exec_policy.py`）。四个执行入口——CLI 的
PreToolUse 钩子、工具边界（`run_bash` / `run_read` / `run_view_image` / `run_read_document` /
`run_render_document` / `run_glob` / `run_grep` / `run_ls` / `run_write` / `run_edit` /
`run_apply_patch` / `run_web_fetch`）、Teammate 线程、后台与 HTTP
任务——共用同一套规则和同一句拒绝文案，
不再各写一份黑名单。

两档配置，命名与 Codex 对齐：

| 档位 | 取值 | 默认 |
| --- | --- | --- |
| `sandbox_mode` | `read-only`、`workspace-write`、`danger-full-access` | `workspace-write`：工作区内写改放行，越界写改询问，破坏性命令询问 |
| `approval_policy` | `untrusted`、`on-request`、`never` | `on-request`：只在策略层判定有风险时询问 |

- `read-only` 只放公认不改动的命令（`dir`、`ls`、`git status` 等）；解释器、命令替换（`$()` / 反引号）
  和带重定向的命令仍要审批。
- `danger-full-access` 取消提示级限制和工作区路径约束，只保留黑名单硬拒和秘密文件保护。
- `untrusted` 的语义是「每条都问」：bash 命令一律审批，且会话白名单（「本次会话允许同类」）在这一档
  不参与短路——否则点一次 `a` 就把档位悄悄改回了 on-request。
- 默认档下，解释器调用（`python -c`、`node -e`、`pwsh -Command`…）、SSH/容器/K8s/包管理操作、
  Git 写操作（`push`/`clean`/`reset`/`checkout`…）和 `curl`/`wget`/`http`/`nc`/`Invoke-WebRequest`
  等网络客户端的外发请求需要审批——**GET 也问**：读放开以后，出口问的不是「这个请求危不危险」，
  而是「本机要不要把东西交给外面」；审批文案会带上目标主机，`curl … | sh` 按「远程脚本」归类。
  这些命令大多数不提供「本会话允许同类」——按可执行文件名免审等于批一次就放行后续任意载荷
  （代价是 `docker ps`、`kubectl get pods` 这类只读子命令也要问，判定层不认它们的子命令语义）。
  在弹到人之前，其中解释器命令、破坏性命令命中和外发请求会先经 **LLM 审查官**自动复核
  （`HARNESS_LLM_APPROVAL`，默认开；`HARNESS_JUDGE_TIMEOUT` 控制单次审查超时，默认 30s）：
  对齐 Codex workspace-write 的口径，工作区内的删除/重建、测试/构建/脚本默认放行并显示
  `[permission] LLM 审查放行`（历史记 `resolved_by='llm-judge'`）；工作区外删除/覆盖、系统修改、
  本机数据外传（带请求体/上传/改方法）、秘密文件、下载并执行一律升级人工；审查不可用
  （超时/异常/输出不合法）也回落人工，绝不 fail-open。审查官只在交互式主循环生效，
  teammate/后台/任务线程照旧排队等远程决议；`/revoke` 与档位切换会连带收回审查官批出的
  全部免审。定位是降噪——硬拒与秘密文件在策略层，不由 LLM 覆盖，它不是安全边界。
- 云 CLI（`aws`/`az`/`gcloud`/`rclone`…）、包管理器（`pip`/`npm`/`npx`…）和进程启动器
  （`Start-Process`…）同样要审批：命令行看不出目标与副作用，「安装即执行 postinstall」
  「复制到对象存储」不能靠黑名单兜底；`--version`/`--help` 这类只读查询除外。
  `nohup`/`xargs`/`env`/`timeout`/`watch` 这类前缀包装器按**内层命令**逐段判定与归类——
  按外层名字记同类，等于批一次 `nohup <任意命令>`。命令里点名 `.env`/私钥、又同时出现
  出口或外部操作命令时，审批升级并标出命中的文件（bash 的 argv 不经过文件工具的路径
  分区，这一条补的就是那个缺口）。
- 外发命令的「同类」按 `命令:主机` 记账，只给 curl/wget 的普通读请求：批准一条 GET 只免审再向
  同一台主机发起的读请求；改方法（`-X`/`--request`）、带请求体（`-d`/`-F`/`-T`/`--post-data`…）、
  目标多于一个或只写裸主机名（`nc host 1234`）都没有免审出路。httpie 把方法写在位置参数里，
  aria2c/iwr 的写开关也判不清，只问不记。
- `web_fetch` 不豁免出口审批：合法 URL 一律 `network_egress` 询问，「同类」键为
  `web_fetch:主机`；URL 形状不合法或指向非公网地址在审批前就硬拒（`invalid_url`）。
  这是防 SSRF 的地址校验，不替代人工判断——批准前仍应看清主机。
- 复合命令（`&&`、`;`、`|`、换行）要每一段归到同一类才记「同类」：只按第一段归类的话，
  批准 `ls && rm x` 会写下 `bash:ls`，之后 `ls; rm -rf ~` 就被同一个键放行。
- `never` 的语义是「不用问我」：`danger-full-access` 下普通询问直接放行；其余档位下需要审批的
  操作直接失败（fail-closed），而不是静默执行。开启 OS 沙箱也不改变这一点——沙箱只约束写入，
  不替代审批。
- 黑名单、根删（`rm -rf --no-preserve-root /`）、`cmd /c` 包装、`su""do` 拆引号等变体在硬拒层，
  档位和白名单都越不过去。
- 文件工具按路径分区：工作区内写改免审（`read-only` 档除外）；工作区外可读，写改要审批；
  `C:\Windows`、`Program Files`（POSIX 的 `/etc`、`/usr` 等）系统目录默认只读，写改硬拒，
  `danger-full-access` 才放开；`.env` / `.env.*`、`*.pem`、`*.key`、`*.p12`、`*.pfx`、
  `*_rsa`、`id_ed25519` 这类秘密文件读写都硬拒（`danger-full-access` 也不放开，这是数据保护，
  不是工作区约束；`.env.example` 等公开模板例外，它们是给开发者抄的，拦下来只会把正常工作流
  赶回 bash）。读取不再限制在工作区内，
  个人助手的文档、日志可以按绝对路径直接读。检索工具（`glob` / `grep` / `ls`）同样是只读且
  可指向工作区外；`grep` 在装 ripgrep 时走 rg 快路径，非 ASCII 模式、rg 缺失或输出异常时
  回落到内置遍历，两种后端都跳过秘密文件与 `.git`、`.cache`、`node_modules` 等忽略目录，
  不会成为读取秘密文件的后门。
- `apply_patch` 的多文件补丁只审批一次，但目标路径会先全部解析并按最严格的分区取值：
  只要有一个目标是秘密文件或非法路径就整份拒绝，有越界写则整份询问；不存在「前面几个文件
  免审、到最后一个才拦」的半执行状态。补丁格式错误在策略层直接 `invalid_patch` 硬拒。
- 文件工具的审批没有「本会话允许同类」：一次写 `a.txt` 的批准不该变成整会话任意路径免审。
  目录范围授权留到后续；越界写每次都看卡片。
- bash 的写路径按同一套分区判：`cp`/`mv`/`tee`/`dd`/`tar`/`unzip`/`mkdir`/`install` 等
  写命令的路径参数与重定向目标（含 `bash -c`/`cmd /c` 载荷、`dd of=`、`--output=`）指向
  工作区外或系统目录时询问 `outside_workspace_write`；读命令（`cat`/`find`/`ls`…）不进
  写名单，读照旧放开。带写目标的段没有「本会话允许同类」键，免得批准一次工作区内拷贝
  就免审后续越界写。这一层是文本分析，不是 shell 解析器：被解释器字符串、变量展开、
  命令替换藏起来的路径看不到；需要内核级写入约束时开启下文的 OS 沙箱。
- 第三方 MCP 工具在注册时登记为「副作用未知」的工具：策略层只认识自家实现的工具，
  MCP 工具能写盘、能发请求，静态判不了，所以**三种执行档位下默认都要审批**
  （`never` 同样 fail-closed，不问也不放行）。免审的出路仍是「本次会话允许同类」
  （卡片按钮或 CLI 的 `a`），第三方工具的同类键就是工具名本身，批准一次后同会话内
  同名工具不再重复问。

配置入口：`/sandbox` 查看和切换（CLI、飞书、`gateway_sim`、`GET/POST /v1/policy` 都走服务端同一份
判定，运行时立即生效，只影响当前进程）；也可以在启动服务前设置环境 `HARNESS_SANDBOX_MODE` 与
`HARNESS_APPROVAL_POLICY`（旧开关 `HARNESS_APPROVAL_ALL=1` 等价于 `approval_policy=untrusted`，
仅在没有显式配置新档位时兜底）。这两个键只认启动进程时的真实环境，`.env` 里的值会被忽略，
原因见「快速开始」。换档是一次安全状态变更，会连带撤销所有会话已批的「同类」预授权；
不想等到换档，也可以用 `/revoke` 撤掉当前会话的预授权。
档位名写错不会静默回落到默认档：`/sandbox` 报用法，环境变量按默认档并在 stderr 提示一次，
`/status` 和 `--doctor` 始终显示实际生效的档位。

审批有三条通道：CLI 主循环读 stdin（`y` 允许 / `n` 拒绝 / `a` 允许并把同类操作加入本会话白名单，
与审批卡片的三个按钮一致）；HTTP 服务模式（`create_app`）把请求登记到 `/v1/permissions`
队列，由飞书卡片或 `/approvals` 决议；Teammate、后台 worker、任务 runner 这些不能抢控制台的线程
只排队等远程决议。超时（`HARNESS_APPROVAL_TIMEOUT`，默认 300 秒）未决议一律拒绝；值写成非数字或
≤0 不会挡住启动，也不会静默按你配的走：stderr 提示一次后按默认档。`GATE.timeout` 是普通属性，
长驻服务改它即刻生效，不必重启。

一次批准会以一次性凭据传到工具边界，同一次调用不会被问第二遍；「本次会话允许同类」的白名单按键
按会话分区，不会让某个会话批过的 `rm` 在别的会话也免审。会话 id 与凭据一样要在派发时显式带进
工作线程（ContextVar 不随线程继承，漏带的那次审批会落回进程级键），teammate 与后台 worker 都已
接上；`POST /v1/tasks` 建的 OS 任务本身没有会话归属，按进程级键分区。`approval_policy=untrusted`
是「每条都问」，这一档下白名单不参与短路——否则一次 `a` 就把档位悄悄改回了 on-request。

「同类」的判定键一律由服务端按被审批的内容重算（bash 取命令名，其余取工具名），审批接口
不接受客户端传 `klass`：调用方只能决定「允许吗、要不要扩到同类」，不能决定扩到哪一类——
否则批准一条 `del` 时带上 `klass=rm`，就等于给后续的 `rm` 开门。命令名会解一层包装器
（`cmd /c "del x"`、`bash -c 'rm x'` 归到 `del` / `rm`，而不是宽泛到能覆盖 `cmd /c rd /s /q` 的
`cmd` / `bash`），绝对路径与 `.exe` 归一到同一个键；解释器载荷（`python -c`）不解，按它自身归类。

审批面显示的就是要批准的内容：bash 命令按「留头尾、中间省略」完整呈现，CLI 提示、`/approvals`、
`/perms`、审批卡片共用同一份渲染（`src/approval_view.py`）；其余工具的参数仍限长，`write` 的整份
content 不会刷进控制台和审批历史。被截断的命令不提供「本会话允许同类」——审批人没看到全貌，
就不该顺手预授权一整类。命令里的不可见控制字符换成 `\\x1b` 这类可见写法，避免被审批的
字符串反过来改写审批提示（ANSI 清行、回车覆盖、BEL）。

⚠ 没有开启 OS 沙箱时，这一层只是策略级判定：它不隔离文件读取和网络，也不保证进程无法绕开。
出口审批只覆盖登记的网络客户端命令；解释器代码（`python -c`）、未登记的程序仍可直连网络——
这一项的作用是把常见出口变成一次明确的人工同意，不是网络隔离。

### OS 沙箱（可选，Windows）

`HARNESS_OS_SANDBOX` 控制是否用 Windows 强制完整性控制（MIC）把 shell 子进程降到
**低完整性级别（Low IL）**。它独立于执行/审批档位，默认 `off`：

| 取值 | 行为 |
| --- | --- |
| `off`（默认） | 不启用，shell 以普通权限运行 |
| `auto` | 尝试启用；本机不支持或初始化失败时退回普通执行，并在 stderr 提示一次 |
| `require` | 必须启用；失败时命令直接返回 `Error: OS 沙箱不可用：…`，不会降级执行 |

启用后：

- 工作区目录树被标记为 Low IL，临时目录重定向到 `<工作区>/.cache/sandbox-tmp`；
  子进程对 Medium IL 的工作区外文件写入、注册表写入会被内核拒绝（no-write-up）。
- **读取和网络不隔离**，命令仍能读取任意可读文件、访问网络；这是写入方向的最低成本兜底，
  不是 AppContainer 级别的完整沙箱。
- 覆盖范围是本项目启动的 shell 子进程：工具 `bash`（含 Teammate、后台 worker）和
  任务管理器的长命令；不覆盖第三方 MCP 工具及 Agent 进程自身的 Python 调用。
- 首次启用会对工作区执行一次 `icacls /setintegritylevel … L`。如果继承的 ACL 缺少
  `WRITE_OWNER`（在部分非系统盘上常见），会先为当前用户补一条 `(OI)(CI)(WO)` 授权；
  该操作只需要一次，结果按工作区缓存并提示一次。目录树较大时首次启用会明显变慢。
- **Git Bash / MSYS 工具（`ls`、`grep`、`cat`、`bash`、`sed` 等）在 Low IL 下会崩溃**
  （无法访问 `\BaseNamedObjects`，退出码 `3221225794`）。沙箱内请使用原生命令
  （`dir`、`findstr`、PowerShell、Python、Node、git、npm、curl 等），或保持沙箱关闭。
- 关闭沙箱不会撤销已经打上的 Low 标签；需要恢复时执行
  `icacls "<工作区>" /setintegritylevel M /T /C /Q`。

沙箱不可用（非 Windows、无法打标签、无法创建受限令牌等）时，`auto` 退回普通执行并提示，
`require` 拒绝执行——与 `never` 审批档的 fail-closed 语义一致，不会静默降级。

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

# 可选：工作区 AGENTS.md / CLAUDE.md 注入（on 默认 / off 关闭）
HARNESS_PROJECT_CONTEXT=on

# 可选：OS 沙箱 off（默认）/ auto / require；Windows Low IL，启用会标记工作区目录
HARNESS_OS_SANDBOX=off

# 可选：额外授权的工作区根，os.pathsep 分隔（Windows 为 ;，Linux/macOS 为 :）
# 与 HARNESS_WORKSPACE 一起构成服务端的全部授权根；登记表不参与授权。
HARNESS_WORKSPACE_ROOTS=D:\Projects;E:\Work

# 可选：工作区登记表位置；默认 SESSIONS_DIR 的父目录下 workspaces.json
HARNESS_WORKSPACE_FILE=.cache/workspaces.json

# 可选：启用分段计时，启动前设置
HARNESS_PERF=1
```

`HARNESS_WORKSPACE` / `HARNESS_WORKSPACE_ROOTS` 是授权配置，应当放在启动脚本、服务管理器
或用户级环境里；不要让它们来自工作区内 Agent 可写的 `.env`，否则等于把「能访问哪些目录」
交给被约束的一方。`HARNESS_WORKSPACE_FILE` 只影响发现列表，不扩大权限。

执行档位与审批档位不在这里配置：`.env`（以及任何 `load_dotenv` 读进来的文件）里的
`HARNESS_SANDBOX_MODE` / `HARNESS_APPROVAL_POLICY` / `HARNESS_APPROVAL_ALL` 会被忽略，
只认启动进程时环境里已有的值——`.env` 就在工作区里，Agent 写得动，把它当配置来源等于
让被约束的一方自己降档。要固定档位，请在启动脚本、服务管理器或用户级环境里设置，
运行中随时用 `/sandbox` 切换（只对当前进程生效）。

`local` 使用中英文关键词匹配，语义召回能力与 `llm` 不同；不设置时默认 `local`（每轮省一次 LLM 筛选往返，约 2s）。
旧开关 `HARNESS_LOAD_MEMORIES=0` 仍会关闭召回。
性能统计可通过 `perf.snapshot()` 读取当前执行上下文的数据；不同请求独立统计，
飞书网关与 API 服务也分别统计，尚未自动合并为端到端报告。
嵌套分段存在包含关系，不能直接相加。

常用交互命令还包括 `/diff`（只读查看当前工作区改动：文件状态 + 逐行统一 diff，未跟踪文件也会
展开）、`/compact`（保存完整记录后压缩当前上下文）、`/undo`（回到最近一次快照）、`/revoke`
（撤销本会话已批的同类预授权）和 `/approvals`（查看待审批操作）。`/cache gc` 默认只预览，
按文件名匹配会话事件里的引用，宁可少删也不误删；被清理的图片在恢复旧会话时降级为文本。
每个回合正常结束时，若本轮
改动了文件，终端会打印改动清单（`M/A/D/R` + 路径 + `/diff` 提示），同时写入 `files_changed`
事件；非 Git 工作区回退到 checkpoint 记录的 `write`/`edit`/`apply_patch` 文件。
飞书网关同步支持 `/model`、`/approvals`、`/perms`、`/revoke` 和 `/sandbox`；
文件查看、代码审阅（`/review`）与上下文压缩仍建议在 CLI 中执行。

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
这些工具调用默认都要审批（见上文执行策略），批一次可用「本次会话允许同类」在同会话内免审。

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

基础依赖只包含 Agent 运行本身，文档工具是可选能力：

- DOCX/PPTX/XLSX 的文本抽取与所有非文档工具：无需额外包（OOXML 由标准库解析）。
- PDF 文本层与页面渲染：`python -m pip install -r requirements-docs.txt`
  （`pypdf` / `pypdfium2` + `Pillow`）。
- DOCX/PPTX 页面渲染：在可选依赖之外还需要系统里可用的 LibreOffice（`soffice`）；
  只读文字不需要它。

缺少可选包时只会影响对应工具并返回安装提示，其他工具不受影响。`pytest`/`ruff`
属于开发工具（见 `requirements-dev.txt`），不参与运行时安装。LibreOffice 装在非标准
目录时，用 `HARNESS_SOFFICE` 指向 `soffice.exe`
（例如 `D:\Tools\LibreOffice\program\soffice.exe`）；该变量只认文件，不做 PATH 回退。

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

一次性（无头）执行：

```bash
python entrypoint.py -p "总结这个仓库的架构"
python entrypoint.py --json "找出未通过的测试" > events.jsonl
cat prompt.txt | python entrypoint.py -p --session daily-review
python entrypoint.py --workspace D:\work\project -i screenshot.png "复现这个报错"
```

- 触发方式：`-p/--print`、`--json`、`--session`、`--workspace`、`--image`，或直接给出位置参数 prompt。
  未提供 prompt 且 stdin 不是终端时，从 stdin 读取；`-` 也显式表示读取 stdin。
- stdout 只输出最终回答（普通模式）或 JSONL（`--json`）；工具日志、Hook 输出和 token 统计写入 stderr。
- `--session` 在会话已存在时恢复，不存在时按该 ID 新建；`--workspace` 为本次会话选择或切换工作区。
  `--resume` 只用于交互模式，无头执行恢复会话请用 `--session`。
- `-i/--image` 可重复，图片必须位于会话工作区内，并复用 `view_image` 的边界与敏感文件策略。
- `--json` 事件类型：`session`、`turn.started`、`tool.started`、`tool.completed`、`delta`、`message`、
  `turn.completed`（含 `usage` 与 `files_changed`）、`error`。
- 无头进程不启动 cron/队列线程，也不执行标题生成和记忆提炼；退出码 `0` 成功、`2` 用法/配置错误、
  `1` 运行错误、`130` 中断。

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
python -m pip install -r requirements-dev.txt
python -m pytest
```

测试文件位于 `tests/`，覆盖会话存储、上下文压缩、记忆、任务系统、Worktree、执行策略
（`test_exec_policy.py` 测判定矩阵，`test_execution_paths.py` 测四条执行入口的接线，
`test_sandbox.py` 测 OS 沙箱的启动、拒绝写与失败降级；Windows 集成用例在无法打标签时跳过）等模块。
全量回归还需要可选的文档依赖（`test_documents.py` / `test_document_render.py`）：
`python -m pip install -r requirements-docs.txt`；未安装时仅跳过 PDF 文本抽取与实际页面渲染用例，
Office 文本解析、转换命令及工具边界测试仍照常运行。
测试默认将 `HARNESS_OS_SANDBOX` 固定为 `off`，避免把 `tmp_path` 目录标记为 Low。
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
| `src/prompt_assembly.py` | system prompt 组装与上下文采集（工具/工作区/技能/项目说明） |
| `src/session_title.py` | 会话标题生成：取材、LLM 调用与本地兜底 |
| `src/headless.py` | 一次性执行与 JSONL 事件流：stdout 纯净输出、工具事件、退出码 |
| `src/project_context.py` | 工作区 AGENTS.md / CLAUDE.md 注入：路径边界、32 KiB 截断与缓存 |
| `src/review.py` | 只读 `/review`：未提交 diff 审阅、受限工具面与敏感文件过滤 |
| `src/workspaces.py` | 工作区登记表：命名、排序与原子持久化（只发现，不授权） |
| `src/tools.py` | 工具接线：路径边界、`run_*` 薄包装、`tool_registry` |
| `src/tool_schemas.py` | 37 个工具的 schema（顺序即契约，有快照测试锁定） |
| `src/tool_teams.py` | 团队协作：派生 teammate、消息收发、shutdown/plan 协议 |
| `src/tool_exec.py` | `bash` / 后台任务 / `web_fetch`：超时、审批门与抓取边界 |
| `src/tool_tasks.py` | 任务与 cron 工具：封装 task_system/cron_scheduler 并统一输出 |
| `src/tool_todos.py` | todo 清单：校验、会话分区、事件流持久化与格式化 |
| `src/tool_documents.py` | `view_image`/`read_document`/`render_document`：图片缓存与文档渲染引用 |
| `src/tool_search.py` | `glob`/`grep`/`ls`：只读边界、秘密文件过滤与 ripgrep 快路径 |
| `src/tool_files.py` | `read`/`write`/`edit` 实现：边界门、编码保留与二进制保护（依赖注入） |
| `src/tool_patch.py` | `apply_patch` 解析、规划与原子回滚；路径解析/审批/checkpoint 由 tools 注入 |
| `src/tool_text.py` | 文件文本 BOM/编码探测、二进制拒绝与换行符保留 |
| `src/tool_cache.py` | `/cache`：`.cache/tool_results` 统计与引用保护回收 |
| `src/exec_policy.py` | 执行策略层：档位、黑名单与审批分级（纯判定，不做 OS 强制） |
| `src/sandbox.py` | Windows Low IL 进程沙箱：工作区标记、受限令牌、Job Object 与超时清理 |
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
