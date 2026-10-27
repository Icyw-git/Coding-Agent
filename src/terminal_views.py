"""`/domains` 与 `/audit` 的共享实现。

CLI、TUI、飞书三处都要展示「域名策略」和「最近审计」。飞书有自己的卡片
（`feishu_domain_card` / `feishu_audit_card`）；终端侧只需要纯文本，但 **CLI 和
TUI 不该各写一份** —— 两份实现的字段顺序、有效期措辞迟早会分叉，用户在不同
界面看到的东西就对不上了。

所以这里只做「数据 → 文本」的纯函数，两个终端前端各自拿结果去 print / notice。
"""
from __future__ import annotations

import time

USAGE = '\n'.join([
    '  用法：/domains                      查看',
    '        /domains allow <主机> [until YYYY-MM-DD] [note 备注]',
    '        /domains deny  <主机> [until YYYY-MM-DD] [note 备注]',
    '        /domains remove <主机>',
    '  主机写精确名或通配子域（*.example.com，不含根域本身）。',
])

AUDIT_USAGE = '\n'.join([
    '  用法：/audit            最近 50 条',
    '        /audit <类型>    只看某类',
    '        /audit all       列出所有可过滤类型',
])


def _expiry_label(row: dict) -> str:
    expires = str(row.get('expires') or '')
    if not expires:
        return '永久'
    if row.get('active', True):
        return f'有效至 {expires}'
    return f'已过期（{expires}）'


def domains(argument: str) -> str:
    """`/domains` 的完整处理：返回要展示的文本。

    参数解析与落盘都在这里，前端只负责把回来的字符串显示出去。
    """
    import grants
    import net_grants

    parts = (argument or '').split(maxsplit=2)
    action = parts[0].casefold() if parts and parts[0] else ''
    rest = ' '.join(parts[1:]) if len(parts) > 1 else ''

    if not action:
        rows = net_grants.list_rules()
        if not rows:
            return '  （没有域名规则；联网请求仍逐个审批）\n' + USAGE
        lines = [f'  域名策略：{len(rows)} 条']
        for row in rows:
            mark = '拒绝' if row.get('scope') == 'deny' else '允许'
            note = f" · {row['note']}" if row.get('note') else ''
            lines.append(f'    {mark} {row["host"]}  [{_expiry_label(row)}{note}]')
        lines.append('  删除：/domains remove <主机>')
        return '\n'.join(lines)

    if action not in ('allow', 'deny', 'remove') or not rest.strip():
        return USAGE

    try:
        host, options = grants.split_path_argument(rest)
    except ValueError as exc:
        return f'  [domains] {exc}'

    if action == 'remove':
        try:
            record = net_grants.remove(host)
        except Exception as exc:
            return f'  [domains] {exc}'
        if record is None:
            return f'  没有这条规则：{host}'
        return f'  已删除：{record["scope"]} {record["host"]}'

    try:
        expires, note = grants.parse_options(options)
        record = net_grants.add(host, scope=action, expires=expires, note=note,
                                created_by='cli')
    except Exception as exc:
        return f'  [domains] {exc}'
    effect = ('访问会直接拒绝（硬拒，任何档位都绕不过）'
              if record['scope'] == 'deny'
              else '访问不再逐次审批（仍受私网拦截与 URL 校验）')
    return f'  已添加：{record["scope"]} {record["host"]}——{effect}'


def _audit_line(event: dict) -> str:
    stamp = time.strftime('%m-%d %H:%M:%S',
                          time.localtime(event.get('ts') or 0))
    bits = [f'{stamp} {event["type"]}']
    for key in ('tool', 'by', 'code', 'blocked_by', 'path', 'host',
                'action', 'process'):
        value = event.get(key)
        if value:
            bits.append(f'{key}={value}')
    # policy_denied 的原因最有信息量，单独放末尾（前面已有 code）
    reason = str(event.get('reason') or '').strip()
    if event.get('type') == 'policy_denied' and reason:
        if reason.startswith('Error: '):
            reason = reason[7:]
        bits.append(reason[:70])
    return '    ' + '  '.join(bits)


def audit(argument: str, *, limit: int = 50) -> str:
    """`/audit` 的纯文本渲染。"""
    import audit as audit_mod
    import harness

    argument = (argument or '').strip()
    if argument.casefold() == 'all':
        lines = ['  可过滤的审计类型：']
        lines.extend(f'    {name}' for name in sorted(audit_mod.AUDIT_EVENTS))
        return '\n'.join(lines)

    types = None
    if argument:
        if argument not in audit_mod.AUDIT_EVENTS:
            return AUDIT_USAGE
        types = [argument]

    events = audit_mod.collect(getattr(harness, 'SESSIONS_DIR', None),
                               limit=limit, types=types)
    if not events:
        return '  （最近没有安全相关事件）'
    lines = [f'  最近 {len(events)} 条：']
    lines.extend(_audit_line(event) for event in events)
    return '\n'.join(lines)
