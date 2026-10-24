# 多供应商与角色模型配置

Harness 的模型选择分两层：**provider**（服务地址 + 凭据）与 **model**（模型 id +
能力/窗口），再由 **role** 决定某个调用走哪个模型。默认所有角色继承 `main`，
因此旧的 `.env` 写法不需要任何改动。

## 1. 旧写法（仍然完整可用）

```env
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL_ID=your-model-id
FALLBACK_MODEL_ID=optional-backup-model   # 可选
```

等价于：

- provider `default`：`LLM_BASE_URL` + `LLM_API_KEY`；
- model `your-model-id`（未注册，使用 OpenAI-compatible 默认能力）；
- 所有角色继承 `main`；
- `FALLBACK_MODEL_ID` 作为 `main` 的同 provider 回退。

`validate_runtime_config` 的 **报错文案和启动行为保持不变**：缺少
`LLM_API_KEY` / `LLM_MODEL_ID` 时仍提示这两个变量。

## 2. 新写法

把 `HARNESS_LLM_CONFIG` 指向一个 YAML/JSON 文件，或直接写内联 YAML：

```env
HARNESS_LLM_CONFIG=./llm.yaml
# 或
# HARNESS_LLM_CONFIG=providers: {default: {base_url: "https://api.deepseek.com", api_key_env: DEEPSEEK_API_KEY}}
```

```yaml
providers:
  default:
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    timeout: 120                 # 可选，秒
  openai:
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY

models:
  deepseek-flash:
    provider: default
    context_window: 1048576
    capabilities:
      reasoning: true
  deepseek-chat:
    provider: default
    model: deepseek-chat         # 可选：注册名与真实模型 id 不同时使用
  gpt-5-mini:
    provider: openai
    context_window: 400000

roles:
  main: deepseek-flash
  summary: deepseek-chat         # 未写的角色自动继承 main
  memory: deepseek-chat
  title: deepseek-chat
  approval: deepseek-chat

fallbacks:
  main: [deepseek-chat, openai/gpt-5-mini]

fallback_cross_provider: false   # 跨 provider 回退默认关闭
```

### provider

| 字段 | 说明 |
| --- | --- |
| `base_url` | OpenAI-compatible 端点；省略时使用 SDK 默认地址 |
| `api_key_env` | **环境变量名**，不是密钥本身；默认 `LLM_API_KEY` |
| `timeout` | 单请求超时（秒），默认 120 |

配置里出现字面量 `api_key` 会直接报错：密钥只应从环境变量读取，避免误提交。
本地无鉴权端点（如 Ollama）可写 `api_key_env: ''`，客户端会用占位 key。

### model

| 字段 | 说明 |
| --- | --- |
| `provider` | 必须是已声明的 provider；只有一个 provider 时可省略 |
| `model` | 发给供应商的真实模型 id；默认与注册名相同 |
| `context_window` | 上下文窗口；状态行与压缩阈值会使用它 |
| `capabilities` | 见下表，未写的项取默认值 |

能力默认值：`stream=true`、`tools=true`、`usage=true`、`reasoning=false`、
`images=false`。`main` 模型 `tools=false` 会在启动校验时失败；`stream=false`
时主循环自动退回非流式请求。声明 `reasoning/images` 目前用于配置可见性，
不会改变请求体。

### role

支持 `main`、`summary`、`memory`、`title`、`approval`：

| 角色 | 调用方 |
| --- | --- |
| `main` | 主循环、子代理、teammate、`/review` |
| `summary` | 历史压缩摘要 |
| `memory` | 记忆召回、提取、合并 |
| `title` | 会话标题 |
| `approval` | LLM 审批审查官 |

### 优先级

从高到低：

1. `HARNESS_MODEL_<ROLE>` 环境变量（如 `HARNESS_MODEL_TITLE`）；
2. 配置文件的 `roles.<role>`；
3. `HARNESS_JUDGE_MODEL`（仅 `approval`，兼容旧变量）；
4. 继承 `main`。

`main` 自身的回退链：`HARNESS_MODEL_MAIN` → `roles.main` → `LLM_MODEL_ID`。
因此新配置可以在保留旧变量的机器上平滑切换。

模型引用有两种写法：

- 注册名：`deepseek-chat`；
- `provider/model`：`openai/gpt-5-mini`。只有前缀是已声明的 provider 时才按
  provider 解析；否则整串仍视为模型 id（兼容 OpenRouter 的
  `openrouter/anthropic/claude` 这类名字）。

## 3. 回退与错误恢复

请求失败先归一成固定类别，再决定动作：

| 类别 | 行为 |
| --- | --- |
| `context_length` | 不重试；主循环做响应式压缩 |
| `rate_limit` / `overloaded` | 退避重试（读取 Retry-After，上限 60s） |
| `server` / `timeout` / `network` | 退避重试 |
| `auth` / `invalid_request` / `unknown` | 立即上抛 |

回退目标按 `fallbacks.<role>` 顺序尝试，每个目标最多获得 3 次重试预算：

- 同 provider 回退始终允许（旧 `FALLBACK_MODEL_ID` 也走这里）；
- 跨 provider 必须显式开启：配置 `fallback_cross_provider: true`，或设置
  `HARNESS_ALLOW_CROSS_PROVIDER_FALLBACK=1`；
- 连续 overload 达到 3 次会提前切换；其余可重试错误在重试耗尽后切换；
- **流式响应一旦已向用户输出文本，就停止重试并上抛**，避免把同一段回答输出两遍。

## 4. 排障

- `/model`、`/status` 与 `run_doctor` 显示的是解析后的 `provider/model`；
- `/model` 还会列出与 main 不同的角色模型；
- `HARNESS_LLM_CONFIG` 的相对路径按**进程当前目录**解析；
- 配置文件改动会在下一次 `get_settings()` 时自动生效（按 mtime/size 判断），
  无需重启；主角色客户端检测到 provider/base_url/key 变化时会重新构造。
