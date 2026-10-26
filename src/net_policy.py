"""网络域名策略：把「每个请求都问」变成可撤销的 allow / deny 名单。

三条规则，按优先级排列：

1. **deny 永远先判**：命中即硬拒，档位、审批策略、allow 名单都不能绕过。
   和秘密文件同性质——这是数据保护，不是工作区约束。
2. **allow 只免除「出口审批」**：请求仍然要过 `web_access.check_url` 的语法与
   私网校验，仍然受 `FEISHU`/策略层其余限制。allow 不会让非法 URL 变合法。
3. **没命中就维持默认**：`network_egress` 询问。默认永远是最保守的那一档。

主机匹配只有两种形态，刻意不支持正则：

- 精确主机：`api.example.com` 只匹配它自己，不匹配子域。
- 通配子域：`*.example.com` 匹配任意层子域，**不匹配** `example.com` 本身。
  想同时放行要写两条。这条与 Codex 的 `allowed_domains` 语义一致，避免
  `*.corp.example.com` 顺手把根域也放出去。

匹配前统一小写并去掉末尾的点；IDN 不做转换（策略层只比较调用方给出的
hostname，punycode 转换由网络层负责），遇到非 ASCII 直接不匹配，宁可多问一次。
"""
from __future__ import annotations

import re

ALLOW = 'allow'
DENY = 'deny'
SCOPES = (ALLOW, DENY)

MAX_HOST_CHARS = 253
_HOST_RE = re.compile(r'^(?:\*\.)?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
                      r'(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$')


class NetPolicyError(ValueError):
    """域名模式不合法。"""


def normalize_pattern(raw) -> str:
    """校验并归一化一条域名模式（可带 `*.` 前缀）。"""
    text = str(raw or '').strip().casefold().rstrip('.')
    if not text:
        raise NetPolicyError('host must not be empty')
    if len(text) > MAX_HOST_CHARS:
        raise NetPolicyError(f'host is longer than {MAX_HOST_CHARS} characters')
    if any(ch in text for ch in '/:@?#\\ '):
        raise NetPolicyError('host must not contain scheme, port, path or spaces')
    if text == '*':
        raise NetPolicyError("refusing to match every host; list domains explicitly")
    if '*' in text and not text.startswith('*.'):
        raise NetPolicyError("wildcard is only allowed as a leading '*.'")
    if not _HOST_RE.match(text):
        raise NetPolicyError('host must be a valid DNS name')
    if text.startswith('*.'):
        base = text[2:]
        if not _HOST_RE.match(base):
            raise NetPolicyError('wildcard host must be a valid DNS name')
    return text


def normalize_host(raw) -> str:
    """把请求里的 hostname 归一成比较用的形式；不可比较时返回空串。"""
    text = str(raw or '').strip().casefold().rstrip('.')
    if not text or any(ord(ch) > 127 for ch in text):
        return ''
    return text


def matches(pattern: str, host: str) -> bool:
    """`pattern` 是否命中 `host`。两边都应已归一化。"""
    if not pattern or not host:
        return False
    if pattern.startswith('*.'):
        suffix = pattern[1:]          # '.example.com'
        return host.endswith(suffix) and len(host) > len(suffix)
    return pattern == host


def classify(host, rules) -> str:
    """返回 ``'deny'`` / ``'allow'`` / ``''``。

    deny 先于 allow：同时命中两个名单时按拒绝处理。`rules` 是
    ``((pattern, scope), ...)``，顺序无关紧要。
    """
    target = normalize_host(host)
    if not target:
        return ''
    verdict = ''
    for pattern, scope in rules or ():
        if not matches(pattern, target):
            continue
        if scope == DENY:
            return DENY
        if scope == ALLOW:
            verdict = ALLOW
    return verdict


def describe(rules) -> str:
    """给 /status 与控制台用的单行摘要。"""
    rules = list(rules or ())
    if not rules:
        return '域名策略：无（未知域名逐个审批）'
    allowed = sum(1 for _, scope in rules if scope == ALLOW)
    denied = sum(1 for _, scope in rules if scope == DENY)
    return f'域名策略：允许 {allowed} · 拒绝 {denied}'
