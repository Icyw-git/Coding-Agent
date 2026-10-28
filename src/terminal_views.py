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
    # 与飞书卡片共用同一个实现，避免两边对「active 缺失」给出相反结论。
    import net_grants
    return net_grants.expiry_label(row)


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


#: 审计事件的中文名。用原名会让用户看到 ``desktop_snapshot_failed`` 这种
#: 标识符——那是给代码读的，不是给人读的。
_AUDIT_LABELS = {
    'approval_timeout': '审批超时',
    'approval_auto': '自动放行',
    'policy_denied': '策略拒绝',
    'permission_revoked': '撤销授权',
    'screen_captured': '截屏',
    'screen_ocr': 'OCR',
    'clipboard_read': '读剪贴板',
    'clipboard_written': '写剪贴板',
    'desktop_snapshot': '桌面快照',
    'desktop_snapshot_failed': '桌面快照失败',
    'desktop_list_windows': '列出窗口',
    'desktop_focus': '切换窗口',
    'desktop_focus_failed': '切换窗口失败',
    'desktop_action': '桌面操作',
    'desktop_action_failed': '桌面操作失败',
    'grant_added': '目录授权',
    'grant_removed': '撤销目录授权',
    'domain_added': '域名规则',
    'domain_removed': '撤销域名规则',
    'memory_written': '记忆写入',
    'memory_forgotten': '记忆删除',
    'memory_redacted': '记忆脱敏',
    'memory_archived': '记忆归档',
    'memory_scope': '记忆开关',
    'proactive_suggested': '主动建议',
    'proactive_accepted': '采纳建议',
    'delete_trashed': '删除到回收站',
    'delete_failed': '删除失败',
    'moved': '移动文件',
    'move_preview': '移动预览',
    'move_rejected': '移动被拒',
    'move_failed': '移动失败',
    'moves_undone': '撤销移动',
    'move_undo_failed': '撤销移动失败',
    'remind_scheduled': '设置提醒',
    'remind_rejected': '提醒被拒',
    'remind_failed': '提醒失败',
}

#: 这些字段已经用中文名或单独行表达，不再重复成 ``key=value``。
_SKIP_KEYS = ('type',)


def _displayable(text: str) -> str:
    """把不能显示的字符换成可显示的。

    会话日志是历史累积的：早期用错编码写入的记录会带 U+FFFD（解码替换符），
    而 GBK 控制台连它都编不了——**一条乱码记录能让整个 /audit 崩掉**。
    显示层负责兜住，而不是让用户看到报错。
    """
    if not text:
        return ''
    cleaned = str(text).replace('�', '?')
    try:
        cleaned.encode('gbk')
    except UnicodeEncodeError:
        # **只替换编不了的那个字**，不要整行退化——整体 ascii 会把中文全变成 ?，
        # 比乱码还糟（用户连带能读的部分一起丢了）。
        cleaned = cleaned.encode('gbk', 'replace').decode('gbk', 'replace')
    # 历史脏数据的 reason 整段都是替换符，40 个 ? 只是噪音。折叠成 …，
    # 让用户能读出结构（哪条事件、后面的字段），而不是被问号淹没。
    import re as _re
    return _re.sub(r'\?{4,}', '[无法显示]', cleaned)


def _audit_line(event: dict) -> str:
    stamp = time.strftime('%m-%d %H:%M:%S',
                          time.localtime(event.get('ts') or 0))
    kind = str(event.get('type') or '')
    bits = [f'{stamp} {_AUDIT_LABELS.get(kind, kind)}']
    for key in ('tool', 'by', 'code', 'blocked_by', 'path', 'host',
                'action', 'process', 'name', 'mode', 'count'):
        if key in _SKIP_KEYS:
            continue
        value = event.get(key)
        if value not in (None, '', 0):
            bits.append(f'{key}={value}')
    # reason **任何**有它的事件都要显示：``desktop_snapshot_failed`` 的全部信息
    # 就在 reason 里，原来只有 policy_denied 显示它，于是这类记录只剩一个事件名。
    reason = str(event.get('reason') or '').strip()
    if reason:
        if reason.startswith('Error: '):
            reason = reason[7:]
        bits.append(reason[:70])
    return '    ' + _displayable('  '.join(bits))


def audit(argument: str, *, limit: int = 50) -> str:
    """`/audit` 的纯文本渲染。"""
    import audit as audit_mod
    import harness

    argument = (argument or '').strip()
    if argument.casefold() == 'all':
        # 列表视图用中文名（给用户看），但排查时需要原始标识符，
        # 所以这里给对照：两边的名字都能查到。
        lines = ['  可过滤的审计类型（原名 — 含义）：']
        for name in sorted(audit_mod.AUDIT_EVENTS):
            label = _AUDIT_LABELS.get(name)
            lines.append(f'    {name}  —  {label}' if label else f'    {name}')
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
