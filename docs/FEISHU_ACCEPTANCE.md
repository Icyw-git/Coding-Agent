# 飞书端到端验收记录

状态更新：2026-10-25。自动化能覆盖的部分（配置、卡片结构、飞书对卡片的受理、
API 可达性、网关单实例）已在本机跑过并留证；**手机上的交互项只能由人点**，
下表里未执行的项目保持“待验收”，不填推测结果。

配套工具：

```powershell
& 'D:/Anaconda_envs/envs/aitest01_py310/python.exe' scripts/feishu_acceptance.py            # 本地预检
& 'D:/Anaconda_envs/envs/aitest01_py310/python.exe' scripts/feishu_acceptance.py --api       # 额外查 API/token
& 'D:/Anaconda_envs/envs/aitest01_py310/python.exe' scripts/feishu_acceptance.py --send-cards bound
#   ↑ 把 8 类卡片各发一份到已绑定聊天，用真机看渲染（真实发消息）
```

脚本只读配置与本地状态；只有 `--send-cards` 会发消息，且一次只发给指定聊天。

## 1. 环境与版本

| 项 | 值 |
|---|---|
| 提交 | 本次工作区（`src/feishu_cron_card.py`、`src/api_crons.py`、`src/cron_scheduler.py`、`src/feishu_gateway.py` 等改动） |
| 平台 | Windows，Python 3.10（`D:/Anaconda_envs/envs/aitest01_py310`） |
| API | `uvicorn api:create_app --factory --port 8000` |
| 网关 | `python src/feishu_gateway.py`（长连接，单实例锁 `locks/feishu-gateway.lock`） |
| 飞书应用权限 | `im:message`、`im:message.p2p_msg:readonly`、`im:resource`（见 [FEISHU_SETUP.md](FEISHU_SETUP.md)） |

验收前必须重启 API 与网关：旧进程不会加载新代码。本次验收就踩到了这一点——
8000 端口上的旧 API 没有 `/v1/heartbeat`（404），旧网关也没有单实例锁。

## 2. 自动化已验证（2026-10-25，本机）

| 项目 | 结果 | 证据 |
|---|---|---|
| 配置齐全（app id/secret/token/白名单） | 通过 | `--check` 输出 `[ok] 配置齐全` |
| 8 类卡片结构校验（元素、按钮 value、30KB 上限） | 通过 | 控制台 1151B、回复卡 718–1298B、定时任务 1261B、删除确认 693B、会话列表 1490B、工作区 897B |
| 飞书对卡片的受理 | 通过 | `--send-cards bound` 8/8 返回 message_id（`om_x100b62ead1…`），无 230xxx 错误 |
| API 可达 + token 有效（会话/定时任务/心跳） | 通过 | `/v1/sessions`、`/v1/crons`、`/v1/heartbeat` 全部 200 |
| 网关长连接 | 通过 | 日志 `connected to wss://msg-frontier.feishu.cn/ws/v2` |
| 网关单实例锁 | 通过 | 网关运行时锁被持有；`test_gateway_refuses_a_second_instance` 覆盖拒绝与 `FEISHU_ALLOW_MULTI=1` |
| durable 任务重启后可加载 | 通过（本轮修复） | 旧代码用 locale 解码 UTF-8 文件，中文提示词报 `'gbk' codec can't decode`，整表丢失；现固定 UTF-8 并单条隔离坏记录 |

## 3. 手机逐项验收（待执行）

按顺序做，每条记录“通过 / 失败 + 现象”。失败项保留截图、时间点与网关日志片段。

| # | 操作 | 期望 | 结果 |
|---|---|---|---|
| 1 | 私聊发“你好” | 先出过程卡（运行中→完成），且引用你的原消息 | 待验收 |
| 2 | 点完成卡「⚙️ 控制台」 | 打开控制台，会话/工作区/模型正确 | 待验收 |
| 3 | 点「定时任务」 | 列出任务；只读任务显示「允许写操作」，可写任务显示「收回写权限」 | 待验收 |
| 4 | 点「允许写操作」再「收回写权限」 | 两条提示 + 列表刷新，授权确实变化 | 待验收 |
| 5 | 点「删除」→「取消」 | 只出现确认卡，任务仍在 | 待验收 |
| 6 | 发一张图片 | 回复提到 `view_image`，答案描述图片内容 | 待验收 |
| 7 | 发 PDF / DOCX | `read_document` 读出内容；扫描件提示改用 `render_document` | 待验收 |
| 8 | 发带标题+图片的富文本 | 文字与图片都被处理，不出现“暂不支持 post” | 待验收 |
| 9 | 发一段语音 | 明确“已保存、不自动转写”，不假装听懂 | 待验收 |
| 10 | 说“每天早上 9 点提醒我喝水” | 创建只读任务，说明结果推回本聊天 | 待验收 |
| 11 | 把某任务改成 1 分钟后触发 | 结果推回本聊天，且只推一次 | 待验收 |
| 12 | 触发需审批的工具 | 收到审批卡；「允许」执行、「拒绝」不执行 | 待验收 |
| 13 | 审批卡放置到超时 | 收到“审批超时、已按拒绝处理” | 待验收 |
| 14 | TUI 跑长回合时手机发消息 | 手机收到忙碌反馈，消息不丢 | 待验收 |
| 15 | 手机点「停止当前回合」 | 回合取消，TUI 与手机状态一致 | 待验收 |
| 16 | 再开一个网关进程 | 第二个进程打印“已经有一个网关实例在运行”并退出 | 待验收 |

## 4. 已知限制（不算失败）

- 扫描 PDF 走视觉模型（`render_document`）而非 OCR；默认只渲染少量页面，需要按页指定。
- 语音/视频只落盘保存，不自动转写/理解。
- `sticker`、`interactive`、`share_chat`、`share_user`、`location` 明确回复“暂不支持”。
- 模型 provider 余额不足（402）时回合会失败，这属于外部状态，不作为验收结论。

## 5. 复测记录

| 日期 | 提交 | 修复/变更 | 复测结果 |
|---|---|---|---|
| 2026-10-25 | 工作区 | `cron_scheduler.load_durable_jobs` 固定 UTF-8 + 单条容错 | 中文 durable 任务重启后可加载（新增 3 条回归） |
| 2026-10-25 | 工作区 | 网关单实例锁 | 第二次启动被拒绝，返回码 2 |
