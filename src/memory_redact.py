"""记忆落盘前的秘密脱敏。

为什么必须在记忆这一层做
------------------------
记忆是**长期状态**：一条 `.env` 内容被记下来，之后每次会话都会被召回并塞进请求。
泄露面比会话本身大得多——会话可以删，记忆会一直跟着。Codex 把这条做进管线
（"redacts secrets before anything hits disk"），我们在 M1 之前完全没有。

两处都要拦
----------
1. **落盘侧**（``redact``）：写入/合并时替换。挡住"以后某天被召回"。
2. **注入侧**（``contains_secret``）：召回进请求前再查一次。挡住在脱敏上线前
   写进去的历史数据——那些文件已经躺在磁盘上了。

命名与形状
----------
命中处替换成 ``[redacted:kind]``，保留"这里原本有个东西"的位置信息，否则
"数据库密码是 XXX"会变成"数据库密码是"，读起来像记忆被截断了。
"""
from __future__ import annotations

import re

import settings

#: 关闭开关（不建议：关掉等于把密钥写进长期记忆）。
ENV = 'HARNESS_REDACT_MEMORY_SECRETS'

#: 已知凭据形态。顺序有意义：更具体的（带前缀的 token）排在通用 kv 之前，
#: 否则 ``api_key = sk-xxx`` 会先被 kv 规则吃掉，标签退化成 ``kv``。
_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ('private_key', re.compile(
        r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----',
        re.DOTALL)),
    ('openai_key', re.compile(r'\bsk-[A-Za-z0-9_\-]{16,}')),
    ('anthropic_key', re.compile(r'\bsk-ant-[A-Za-z0-9_\-]{16,}')),
    ('github_token', re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}')),
    ('aws_access_key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('slack_token', re.compile(r'\bxox[baprs]-[A-Za-z0-9\-]{10,}')),
    ('google_api_key', re.compile(r'\bAIza[0-9A-Za-z_\-]{35}\b')),
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}')),
    ('bearer', re.compile(r'\bBearer\s+[A-Za-z0-9._\-]{16,}', re.IGNORECASE)),
    # 通用 key=value / key: value / key is value：只在键名像凭据时才替换，避免把
    # "token 预算是 8000"（自然语言）也吃掉。中文键名一并覆盖：本项目以中文
    # 交互为主，"密码是 xxx" 和 "password: xxx" 同样危险。
    ('kv', re.compile(
        r'(?i)\b(api[_\-]?key|access[_\-]?key|secret[_\-]?key|client[_\-]?secret|'
        r'password|passwd|pwd|token|auth[_\-]?token|refresh[_\-]?token)\b'
        r'\s*(?:[:=]|\bis\b|是|为)\s*["\']?([^\s"\',;]{6,})["\']?')),
    ('kv_cjk', re.compile(
        r'(密码|口令|密钥|令牌|凭证)\s*(?:是|为|[:=])\s*["\']?([^\s"\',;]{6,})["\']?')),
)

#: 额外的精确值黑名单（来自 ``credentials.py`` 的真实凭据），在 ``redact`` 时注入。
_REDACTION = '[redacted:{}]'


def enabled() -> bool:
    return settings.flag(ENV, True)


def redact(text: str, *, extra_values=()) -> tuple[str, list[str]]:
    """把疑似凭据替换成 ``[redacted:kind]``，返回 (新文本, 命中的 kind 列表)。

    ``extra_values`` 是已知的真实凭据值（如已解密的 API key），命中即替换——
    这比形态匹配可靠得多：真实值一定对，形态匹配只是猜测。
    """
    if not text or not enabled():
        return text, []
    found: list[str] = []
    result = text

    # 先按真实值替换（最可靠），再按形态。
    for value in extra_values:
        value = str(value or '').strip()
        if len(value) < 8 or value not in result:
            continue
        result = result.replace(value, _REDACTION.format('known_credential'))
        found.append('known_credential')

    for kind, pattern in _PATTERNS:
        if kind in ('kv', 'kv_cjk'):
            def _kv(match):
                return f'{match.group(1)}=[redacted:kv]'
            new = pattern.sub(_kv, result)
        else:
            new = pattern.sub(_REDACTION.format(kind), result)
        if new != result:
            found.append(kind)
            result = new
    return result, found


def contains_secret(text: str, *, extra_values=()) -> bool:
    """注入前的最后一道检查：这段文本里还有没有凭据形态。"""
    if not text or not enabled():
        return False
    _cleaned, found = redact(text, extra_values=extra_values)
    return bool(found)
