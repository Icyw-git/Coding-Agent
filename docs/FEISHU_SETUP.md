# 飞书接入（3 步）与日常使用

目标：手机上**不用记命令**。第一次私聊机器人就自动建会话并弹出控制台卡片；
之后直接发消息，切换/停止/状态都点按钮。斜杠命令只保留给自动化和排障。

## 1. 飞书开放平台创建应用（一次性）

1. 打开 <https://open.feishu.cn> → 开发者后台 → 创建「企业自建应用」。
2. 添加「机器人」能力。
3. 事件与回调：
   - 订阅方式选 **长连接**（不需要公网 IP）；
   - 订阅事件 `im.message.receive_v1`；
   - 卡片回调选长连接（控制台按钮与审批按钮依赖它）。
4. 权限（按用到的功能勾选，详见下表）：
   - 必选：`im:message`（发消息、更新卡片）、`im:message.p2p_msg:readonly`（读单聊消息）
   - 收发图片/文件必选：`im:resource`（下载消息里的图片/文件资源）
   - 群聊里 @ 机器人才需要：`im:message.group_at_msg:readonly`
5. 在「凭证与基础信息」复制 `App ID` / `App Secret`，发布版本并让管理员审核通过。

### 权限与功能对照

| 功能 | 需要的权限 / 配置 |
|---|---|
| 私聊收发文字、流式更新回复卡、审批按钮 | `im:message` + `im:message.p2p_msg:readonly` |
| 发送/引用回复（`im.message.reply`） | `im:message` |
| 更新卡片（PATCH message） | `im:message` |
| 下载用户发来的图片/PDF/Office 文件；上传截图回传 | `im:resource` |
| 群聊 @ 机器人 | `im:message.group_at_msg:readonly`（网关默认只处理单聊） |
| 接收事件与按钮回调 | 事件订阅选「长连接」，订阅 `im.message.receive_v1`；回调订阅卡片回传交互 |

不需要文档（Drive）读写、通讯录、群管理权限：文件是通过消息资源下载到本地工作区，
再由 `read_document` / `view_image` 读取，不访问飞书云文档。

## 2. 配置（一次性）

把下面几项写进项目根的 `.env`（模板见 `feishu.env.example`）：

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
HARNESS_API_TOKEN=change-me          # API 与网关用同一个值
FEISHU_ALLOWED_USERS=ou_xxx          # 允许使用机器人的人
```

不知道自己的 `open_id`？先留空启动网关，给机器人发一条消息：
它会回你一行 `FEISHU_ALLOWED_USERS=ou_xxxx`，复制进 `.env` 后重启即可。
（首次启动前也可设 `FEISHU_ALLOW_ANY_USER=1`，仅限本机测试。）

离线检查配置是否齐全：

```bash
python src/feishu_gateway.py --check
```

## 3. 启动

开发/排障时仍是两条命令：

```bash
# 终端 A：Agent API（守护进程）
uvicorn api:create_app --factory --host 127.0.0.1 --port 8000

# 终端 B：飞书网关
python src/feishu_gateway.py
```

日常常驻建议用一个监督进程同时拉起两者：

```bash
python src/agent_service.py            # API + 网关；缺少 FEISHU_* 时只跑 API
python src/agent_service.py --no-feishu
```

子进程输出写到 `.cache/logs/api.log` / `feishu.log`（超过 10MB 时下次启动轮转）。
API 就绪后会弹一条本地 Toast；任一子进程退出会再弹一条失败通知并结束监督进程，
默认不自动重启，避免坏配置下无限拉起。

登录自启（当前用户、`ONLOGON`、`/RL LIMITED` 不提权）：

```bash
python scripts/install_autostart.py install --dry-run   # 只打印 schtasks 命令
python scripts/install_autostart.py install
python scripts/install_autostart.py status
python scripts/install_autostart.py remove
```

安装器只注册任务，不执行安装；`install --dry-run` 不会修改系统。任务运行
`src/agent_service.py`，优先使用同目录的 `pythonw.exe`，登录时不会闪控制台。
网关会常驻并自动：接收消息、流式更新同一张回复卡片、推送审批卡片、
在 TUI/API/cron 跑完回合后主动通知、每天同步 cron 任务。

飞书发不出去时（断网、凭证失效、机器人被移出会话），网关会在本机弹一条 error Toast
作为兜底：文字回复失败、回合终态/审批超时通知失败、心跳摘要与定时结果推送失败都会触发。
同一类事件 120 秒内只弹一次，避免远端故障时刷屏；`FEISHU_LOCAL_ALERTS=0` 可关闭。
这条降级只在 `src/feishu_gateway.py` 入口生效，直接用库构造网关不会弹通知。

## 日常使用（不需要输入命令）

| 想做什么 | 怎么做 |
|---|---|
| 开始对话 | 直接给机器人发消息；首次会自动建会话 |
| 新会话 | 点控制台卡片的「新会话」→ 选工作区 |
| 切换历史会话 | 点「会话列表」→ 按工作区分组（显示每个工作区的会话数与路径尾部），可「只看当前工作区」；再按「切换到此会话 / 查看详情」 |
| 停止当前回合 | 点「停止当前回合」 |
| 看当前状态 | 控制台卡片直接显示「待审批 N 条 · 定时任务 N 个 · 目录授权 N 个」；点「当前状态」看模型/工作区明细 |
| 审批 | 一张卡片列出**所有**待审批：每条都有「允许 / 拒绝 / 同类」，超过 4 条折叠成“另有 N 条”，底部可「全部允许 / 全部拒绝」；也能直接回 `y` / `n`（多条时 `y2` / `n2`） |
| 看后台任务 | 说“后台跑 xxx”，然后点「当前状态」→「看日志 / 停止 / 刷新」，命令 `/status`、`/logs` 仍可用 |
| 定时任务 | 点「定时任务」看列表、授权或删除；也可以直接说“每天早上 9 点提醒我……”即可，模型会创建任务并把结果推回本聊天（离线期间保留、恢复后补发）；说“我有哪些定时任务 / 取消那个提醒”也会走工具。默认只读；要给某个任务开写权限，点「定时任务」卡片里的「允许写操作」（每次写操作仍单独审批），也可以 `/cron add --writes ...`。想让它等你不用电脑时再跑，直接说“等我离开电脑再做”或“空闲 10 分钟后再跑”，会创建带空闲门控的任务（到点不空闲就延后，空闲/锁屏后补跑一次，超过 12 小时丢弃）。 |
| 查找旧会话 | 直接说“我们上次聊到哪了 / 帮我找关于 X 的会话”，模型用 `find_sessions` 搜索 |
| 改会话名 / 撤销授权 | 直接说“把这个会话改名叫 X”“撤销刚才的授权”，模型用 `rename_session` / `revoke_permissions`（同时收回本会话添加的目录写授权） |
| 目录写授权 | 控制台「目录授权」查看；新增/删除用 `/grants add "<绝对目录>" [until 2026-12-31] [note 备注]`、`/grants remove "<绝对目录>"`。卡片每条带「撤销授权」，10 分钟内有效 |
| 读写剪贴板 | 直接说“把我刚复制的发我”“把这段放进剪贴板”，模型调用 `clipboard_read` / `clipboard_write`；两者都会弹审批卡 |

## 回复卡片长什么样

每个回合一张可编辑卡片（schema 2.0，参考 dsh-im 的过程卡）：

```
⚙️ 任务过程
  <回答正文>
  📋 过程详情（折叠）
      🛠️ 工具摘要（3）
          bash × 2 · read × 1 · 累计耗时 1.2s
  _已完成 · 总用时 00:12_
```

- 运行时面板展开、显示“正在执行 bash · 00:03”，卡片底部有「⏹ 停止这个回合」，
  长回合不用回控制台找入口；完成后自动折叠，只显示答案 + 「⚙️ 控制台」。
- 工具调用与失败项来自会话事件（`tool_started` / `tool_profile`），不额外请求模型。
- 机器人会**引用你的原消息**回复（`im.message.reply`），手机上一眼能对上上下文。
- 网关启动时会给已绑定聊天发一次控制台；每张完成卡底部也有「⚙️ 控制台」按钮，
  入口常驻，不需要记命令。
- 主动通知只显示结论（`✅ 回合完成` / `⚠️ 回合失败（原因）`），不带会话/turn ID。
- 答案超过 18000 字会截断，并在卡片里提示“可以让它写入文件再发给你”。
- 失败时先给一句可执行的原因（余额不足 402 / 鉴权 401 / 频繁 429 / 超时 /
  上一轮还在跑），技术细节放在下一行，不把原始异常直接糊到手机上。
- 失败卡自带「🔄 重试这一次」：一键重发刚才那句话，不用重新打字（30 分钟内有效）。
- 若你的飞书客户端不支持 2.0 卡片，设置 `FEISHU_STEP_CARD=0` 回到 Markdown 卡片。

## 发图片 / 文件

- 直接发**图片**：网关经消息资源接口（`messages/{id}/resources`）下载到会话工作区 `.cache/feishu-inbox/`，然后让 agent 用
  `view_image` 查看（支持 PNG/JPG/GIF/WEBP）。
- 直接发**文件**：同样落到 `.cache/feishu-inbox/`，agent 用 `read_document` 读取
  PDF / Word / Excel / PPT（DOCX/PPTX 转换需要服务端装 LibreOffice），文本文件也可用
  `read`。
- 单文件默认上限 20MB，可用 `FEISHU_MAX_ATTACHMENT_MB` 调整；超限直接拒绝。
- **富文本（post）消息**：保留文字与链接，内嵌图片同样落到 `.cache/feishu-inbox/`
  后交给 `view_image`（一次最多 6 张）。
- **音频**：会下载并保存为工作区附件，但当前不自动转写（需要另接 ASR）。
- **视频/media**：会下载并保存为工作区附件，但当前不自动做视频理解。
- 语音、表情、转发卡片、位置、联系人等暂不自动解析，会回一句提示。

## 看屏幕 / 读屏

- 直接说“看看我现在屏幕上的报错”“把截图里的表格读出来”，模型会调用 `screen_capture` /
  `screen_ocr`。截图保存到**运行 API 的那台机器**的 `.cache/screenshots/`（不是聊天工作区），
  OCR 用 Windows 自带的本地引擎，不联网、不经过视觉模型。
- 屏幕工具**每次调用都会弹审批卡**，且不提供“本会话允许同类”；拒绝就看不到。审批档位设为
  `never` 时按 fail-closed 直接拒绝（`danger-full-access` 下的 `never` 除外）。
  后台任务、定时任务和子代理不能读屏，会直接报“需要交互式回合”。
- 截图保留 7 天，进程启动时的后台维护会清理过期文件；控制台面板显示
  “屏幕工具 开/关 · 截图 N 张（大小）”。`HARNESS_SCREEN_TOOLS=0` 可在启动前整体关闭。
- **截图原图会回传到本聊天**：成功回合结束后，网关下载本轮截图并以图片消息回复原消息；
  默认最多 4 张、单张不超过飞书 10MB 限制。被截断或超限时会附一句说明；上传失败只影响
  图片，文字答复照常。设置 `FEISHU_SEND_SCREENSHOTS=0` 可只保留文字结论。
  失败/取消的回合不会发送部分截图，图片也不会暴露 API 机器上的本地路径。
- 只截当前用户会话能看到的内容；锁屏或 UAC 安全桌面截不到，会返回明确的失败原因。
- OCR 默认中文（`zh-Hans-CN`），可要求 `lang=auto` 跟随系统；缺语言包时会列出本机可用语言。

## 剪贴板

- 直接说“把我刚复制的发我”“读一下剪贴板”，模型调用 `clipboard_read`，读取的是**运行 API
  那台机器**的剪贴板文本（超过 200 KiB 会截断并提示）。说“把这段放进剪贴板”，模型调用
  `clipboard_write`，会覆盖你当前复制的内容。
- 两者**每次调用都会弹审批卡**，不提供“本会话允许同类”；`never` 档位按 fail-closed 拒绝
  （`danger-full-access` 下的 `never` 除外）。后台任务、定时任务和子代理不能访问剪贴板。
- 只处理纯文本，图片/文件格式不解析。审计只记长度、是否截断与耗时，不记录内容；
  `HARNESS_CLIPBOARD_TOOLS=0` 可在启动前整体关闭。

## 目录写授权

- 控制台「目录授权」或 `/grants` 打开卡片：列出持久授权、到期时间和添加人，每条一个
  「撤销授权」按钮；超过 4 条就地翻页。按钮带 10 分钟 token，过期卡片只提示，需重新打开。
- 新增只能由人操作：`/grants add "D:\notes" [until 2026-12-31] [note 备注]`；
  路径必须是已存在的绝对目录，含空格时加引号。删除用 `/grants remove "D:\notes"`
  或卡片按钮。模型没有新增授权的工具。
- 只放宽**工作区外写入**的审批，且只在 `workspace-write` 档生效；秘密文件与系统目录
  仍硬拒，`read-only` 档照旧逐次审批。`apply_patch` 中每个工作区外路径都要被覆盖。
- `/revoke` 会撤销当前会话添加的目录授权；人从飞书或 CLI 加的全局授权不受模型调用
  `revoke_permissions` 影响，需要人自己点撤销。
- 账本在 API 机器的 `<agent home>/cache/grants.json`，损坏时按“无授权”继续询问，
  不会静默放行。

## 域名策略

联网请求默认**每次都问**。域名规则只是在“问不问”这一层做取舍，不会让非法地址变合法。

- 查看：`/domains`（卡片每条带「撤销规则」；超过 6 条就地翻页，按钮 10 分钟有效）。
- 允许：`/domains allow "*.example.com" [until 2026-12-31] [note 备注]`——命中的域名
  不再逐次审批，但仍受 URL 语法与私网拦截（`127.0.0.1`、内网段、云元数据地址照样拒）。
- 拒绝：`/domains deny ads.example.com`——**硬拒**，任何执行档位都绕不过，`danger-full-access`
  也不行；与秘密文件同性质。
- 删除：`/domains remove <主机>` 或卡片按钮。
- 匹配规则只有两种：精确主机（`api.example.com` 只匹配自己）和通配子域
  （`*.example.com` 匹配任意层子域，**不包含** `example.com` 本身）；不支持正则，
  也不接受 `*` 这种全匹配。
- 同时命中 allow 与 deny 时按**拒绝**处理。
- 只能由人添加：模型没有新增域名规则的入口。`/revoke` 可收回本会话添加的规则，
  人手动加的全局规则保留。
- 账本在 `<agent home>/cache/net_grants.json`，损坏或版本未知时按“无规则”处理，
  等于回到逐个审批。

注意：这里管的是 `web_fetch` 与原生联网工具。`bash` 里的 `curl` 仍然按命令文本走
出口审批，不受域名名单影响——那一条属于命令分析层。

## 用户规则文件

命令审批的噪音主要来自三类「静态判不准」的情况：破坏性词误伤（`npm test` 撞上
`npm`）、解释器与编码载荷、MCP 工具的未知副作用。规则文件让这些情况可以按前缀
提前决定，而**不需要**改档位或放开边界。

文件位置：`<agent home>/.cache/rules.yaml`

```yaml
version: 1
rules:
  - decision: allow
    tools: [bash]
    prefixes: ["git status", "git diff", "npm test"]
    note: 只读 git 与测试
  - decision: ask
    prefixes: ["git push"]
    note: push 前总是确认
  - decision: deny
    prefixes: ["npm publish", "docker push"]
    note: 绝不自动发布
```

- `decision`：`allow` / `ask` / `deny`。
- `tools`：默认 `["bash"]`；`["*"]` 表示所有工具。
- `prefixes`：按**命令词前缀**匹配。`git status` 命中 `git status -s`，不命中
  `git statusx`，也不命中 `echo git status`。多条规则按拒优先生效。
- `note`：出现在 `/rules` 与审批拒绝提示里，写清楚「为什么」。
- `scope`：`session` 的规则只在当前会话生效，默认 `global`。

查看：`/rules`（CLI 与 TUI）；`/status` 会显示「规则文件：允许 N · 询问 N · 拒绝 N」。

**规则能改什么，不能改什么** —— 这是刻意设计，不是遗漏：

| 能改 | 改不动 |
|---|---|
| `destructive`（破坏性词误伤） | `sandbox_read_only`、`untrusted` 档位 |
| `opaque_command`（解释器/编码载荷） | `network_egress`（由域名账本管） |
| `external_tool`（MCP 工具） | `secret_egress`、`secret_path`（秘密数据） |
| | `outside_workspace_write`（由目录授权账本管） |
| | `screen_*`、`clipboard_*`、`desktop_control_*`（每次都问是能力定义） |
| | 黑名单、根删除、系统路径、参数错误 |

实现上是**白名单**（`rules.RULEABLE_CODES`）：以后新增任何决策码默认受保护，
不会因为忘了加进保护名单而被规则悄悄放宽。

另外两条边界：

- `allow` 在 `read-only` 与 `untrusted` 档下**不生效**——档位是用户的显式选择，
  规则文件不能替他改回去（`rm -rf` 在 read-only 下会先命中破坏性询问，放行它就
  等于让只读档形同虚设）。`deny` 不受此限，收窄永远是允许的方向。
- 多段命令必须**每一段**都命中 `allow`：`ls && rm -rf ~` 只有第一段命中时整条
  仍然要问。

文件损坏或格式错误时，启动不受影响：打印一行提示后按「没有规则」处理，所有规则
一起失效，而不是启用半份。需要改规则时直接编辑该文件，下一次工具调用即生效（每次
判定都重新读盘，便于手工调整；文件很小，不是热点）。

## 审计

- `/audit` 打开审计卡：最近 100 条安全相关事件（审批超时、截屏/OCR、剪贴板、
  桌面操作、目录与域名授权变更），8 条/页，按钮翻页。
- `/audit screen_captured` 只看某一类；`/audit all` 列出所有可过滤类型。
- 服务端在投影时就裁剪字段：桌面动作不记录键入文本，OCR 不记录识别出的文字，
  剪贴板只记录长度与是否截断。所以这里看到的是「做过什么」，不是「内容是什么」。
- `approval_auto`（🤖）是**不经人点头就执行**的记录：`by=llm-judge` 是审查官放行，
  `by=rule` 是用户规则文件放行。只记工具、策略码、理由与命令摘要，用于事后核对。
- 也可以用 `GET /v1/audit?limit=&types=&since=&session_id=` 直接取 JSON；
  未知类型会返回 400，不会静默返回空列表。

审批卡片会**合并**：多条请求共用一张卡，处理完一条就原地刷新，不会一条一张卡刷屏；
超过 4 条时只展开前 4 条，其余提示用 `y序号` / `n序号` 或底部「全部允许 / 全部拒绝」。

定时任务卡片（`/cron` 或控制台「定时任务」按钮）：按**最近触发**排序，每页 3 个，
底部「← 上一页 / 下一页 →」**就地翻页**（PATCH 同一张卡，不在聊天里堆消息）。
每个任务带「允许写操作 / 收回写权限 / 删除」，删除需要再点一次确认。授权只能由
人点按——模型自己创建的定时任务永远是只读的。按钮 10 分钟后失效，重开即可；
点了过期卡片只提示，不会连带作废最新那张。

斜杠命令（`/sessions`、`/archive`、`/cron` 等）仍可用，适合批处理、排障和脚本；
日常操作不需要记忆它们。工作区登记/移除、归档/删除、切换执行档位、解除绑定这些
配置或破坏性操作仍保留为显式命令，不交给模型自动触发。

## 验收

- 本地预检（配置、API、8 类卡片结构）：`python scripts/feishu_acceptance.py --api`
- 真机看卡片渲染：`python scripts/feishu_acceptance.py --send-cards bound`
  （发给已绑定的聊天，只读 bindings，不发别的消息）
- 逐项操作清单与记录表：[FEISHU_ACCEPTANCE.md](FEISHU_ACCEPTANCE.md)

## 常见问题

- **回复里出现 `处理未完成：'gbk' codec can't encode character ...`**：这是旧版本的
  Windows 控制台编码问题（回合里的日志打印了 emoji）。已修复：stdout 遇到无法编码
  的字符会写成 `😊` 而不是抛异常；重启 API 即可生效。
- **机器人连着但发不出消息 / 附件下载失败**：多半是本机系统代理（Clash/VPN）关了，
  而飞书请求被一起代理走。网关默认让 `*.feishu.cn` 绕过代理（`NO_PROXY`），
  若你的网络必须经代理访问飞书，设 `FEISHU_USE_PROXY=1` 恢复原行为。
- **提示“已经有一个网关实例在运行”**：你（或开机自启脚本）已经启动过一个网关。
  两个进程会把同一条消息回两次、把同一批推送发两遍，所以第二个会直接退出。
  先关掉旧进程再启动；确实要并行（不同应用凭证）才设置 `FEISHU_ALLOW_MULTI=1`。

- **机器人不回**：先跑 `--check`；再看网关日志里的 `[feishu]` 行。长连接需要
  「事件订阅」选了长连接方式，且应用版本已发布。
- **提示没有权限**：机器人会回你的 `open_id`，加入 `FEISHU_ALLOWED_USERS` 后重启网关。
- **群聊没反应**：默认只处理单聊（p2p）；群聊需要额外权限并显式放开，
  避免群里任何人触发本机 shell。
- **按钮点了没反应**：卡片回调也走长连接；确认开放平台里启用了卡片回调，
  且卡片未超过有效期（会话/工作区列表 10 分钟）。
- **网关和 API 不在同一台机器**：设置 `HARNESS_API_URL` 指向 API 地址，
  `HARNESS_API_TOKEN` 两端一致；API 只监听本机时需要用 SSH 隧道或反代。
- **数据在哪**：会话/记忆/快照在 agent home（默认仓库根，可用 `HARNESS_HOME` 覆盖），
  `python entrypoint.py --doctor` 会打印实际路径。

## 凭据保护（DPAPI 加密）

`.env` 是明文：任何能读这个文件的东西都能拿到飞书 secret 与 API token。
本机可以把这些密钥加密存到 `.cache/credentials.bin`，加密绑定**当前 Windows
用户**（DPAPI），换个用户或换台机器都解不开。

```bash
python scripts/secure_credentials.py status    # 现在用的是哪儿
python scripts/secure_credentials.py migrate   # 加密写入（不改 .env）
python scripts/secure_credentials.py migrate --delete-env-secrets
python scripts/secure_credentials.py clear     # 删密文，回退 .env
```

- 默认**不删** `.env` 里的行。确认 API 与网关都能正常启动后，再加
  `--delete-env-secrets` 删掉那几行（只删这几个键，保留注释与其它配置）。
- 加密覆盖 `LLM_API_KEY`、`HARNESS_API_TOKEN`、`FEISHU_APP_SECRET` 三个键。
  其余配置（base URL、模型名、白名单）留在 `.env`。
- 加载顺序：**密文优先，`.env` 兜底**。密文不存在或解不开时自动降级，启动不受影响。
- `/status` 与 `--doctor` 会显示「凭据：加密文件（N 项）」或「凭据：明文 .env
  （未迁移）」，避免以为加密了其实还在用明文。
- 排障：`HARNESS_DISABLE_SECURE_CREDENTIALS=1` 可临时忽略密文。
- 非 Windows 平台不支持（不做自研加密），`status` 会明确说明。

**这不是运行时隔离**：进程内存里仍是明文，被同用户权限的调试器或恶意进程读取时
挡不住。它保护的是磁盘上的静态副本（备份、同步盘、误提交、截图）。
