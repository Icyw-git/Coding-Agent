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
4. 权限：`im:message`（以应用身份发消息/读单聊消息）；
   如需在群里 @ 机器人，再加 `im:message.group_at_msg`。
5. 在「凭证与基础信息」复制 `App ID` / `App Secret`，发布版本并让管理员审核通过。

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

## 3. 启动（两条命令）

```bash
# 终端 A：Agent API（守护进程）
uvicorn api:create_app --factory --host 127.0.0.1 --port 8000

# 终端 B：飞书网关
python src/feishu_gateway.py
```

网关会常驻并自动：接收消息、流式更新同一张回复卡片、推送审批卡片、
在 TUI/API/cron 跑完回合后主动通知、每天同步 cron 任务。

## 日常使用（不需要输入命令）

| 想做什么 | 怎么做 |
|---|---|
| 开始对话 | 直接给机器人发消息；首次会自动建会话 |
| 新会话 | 点控制台卡片的「新会话」→ 选工作区 |
| 切换历史会话 | 点「会话列表」→ 卡片里按工作区浏览、切换、看详情 |
| 停止当前回合 | 点「停止当前回合」 |
| 看当前状态 | 点「当前状态」 |
| 审批 | 审批卡片上的「允许 / 拒绝 / 本会话同类允许」按钮，或直接回 `y` / `n` |
| 定时任务 | 控制台卡片之外仍可用 `/cron`；自动化脚本建议走 API |

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

- 运行时面板展开、显示“正在执行 bash · 00:03”；完成后自动折叠，只显示答案。
- 工具调用与失败项来自会话事件（`tool_started` / `tool_profile`），不额外请求模型。
- 机器人会**引用你的原消息**回复（`im.message.reply`），手机上一眼能对上上下文。
- 网关启动时会给已绑定聊天发一次控制台；每张完成卡底部也有「⚙️ 控制台」按钮，
  入口常驻，不需要记命令。
- 主动通知只显示结论（`✅ 回合完成` / `⚠️ 回合失败（原因）`），不带会话/turn ID。
- 若你的飞书客户端不支持 2.0 卡片，设置 `FEISHU_STEP_CARD=0` 回到 Markdown 卡片。

## 发图片 / 文件

- 直接发**图片**：网关下载到会话工作区 `.cache/feishu-inbox/`，然后让 agent 用
  `view_image` 查看（支持 PNG/JPG/GIF/WEBP）。
- 直接发**文件**：同样落到 `.cache/feishu-inbox/`，agent 用 `read_document` 读取
  PDF / Word / Excel / PPT（DOCX/PPTX 转换需要服务端装 LibreOffice），文本文件也可用
  `read`。
- 单文件默认上限 20MB，可用 `FEISHU_MAX_ATTACHMENT_MB` 调整；超限直接拒绝。
- 语音、表情等暂不支持，会回一句提示。

斜杠命令（`/sessions`、`/archive`、`/cron` 等）仍可用，适合批处理、排障和脚本；
日常操作不需要记忆它们。

## 常见问题

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
