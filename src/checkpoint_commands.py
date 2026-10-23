"""Checkpoint 命令层：列表、GC 预览/执行、rewind 报告。

这里只放不依赖 `harness` 全局状态的命令与格式化逻辑；目录、保留上限和
标签策略由调用方注入，行式 REPL 与 Textual 共用同一份输出。
"""
from __future__ import annotations

import checkpoints
import tool_cache
from turn_progress import relative_time

DEFAULT_KEEP = 20


def format_checkpoint_list(manager) -> str:
    rows = manager.list()
    if not rows:
        return '  暂无快照。用 /checkpoint 创建，或在执行任务时自动生成。'
    head = manager.head()
    lines = []
    for row in rows:
        mark = '*' if row['cp_id'] == head else ' '
        label = row.get('label') or ('手动快照' if row['trigger'] == 'manual' else '任务快照')
        status = ' · 尚未完成' if row['status'] == 'open' else ''
        current = ' · 当前' if mark == '*' else ''
        turn = ' · 回合起点' if row.get('turn_start') else ''
        # 时间锚点：没有它无法分辨快照是「刚才」还是「两小时前」的
        when = f" · {relative_time(row.get('created_at'))}" if row.get('created_at') else ''
        lines.append(f"  {label}{current}{turn}{status} · {len(row['files'])} 个文件{when}\n"
                     f"    /rewind {row['cp_id']}")
    return '\n'.join(lines)


def parse_checkpoint_gc(argument: str, *, default_keep: int = DEFAULT_KEEP) -> tuple[bool, int]:
    """解析 `/checkpoint-gc [--all] [--keep N]`，返回 (all, keep)。"""
    all_sessions = False
    keep = default_keep
    tokens = (argument or '').split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == '--all':
            all_sessions = True
        elif token == '--keep':
            index += 1
            if index >= len(tokens):
                raise ValueError('--keep 需要一个正整数')
            token = tokens[index]
            if not token.isdigit() or not 1 <= int(token) <= 10_000:
                raise ValueError('--keep 需要 1～10000 之间的整数')
            keep = int(token)
        elif token.startswith('--keep='):
            value = token.split('=', 1)[1]
            if not value.isdigit() or not 1 <= int(value) <= 10_000:
                raise ValueError('--keep 需要 1～10000 之间的整数')
            keep = int(value)
        else:
            raise ValueError('用法：/checkpoint-gc [--all] [--keep N]')
        index += 1
    return all_sessions, keep


def checkpoint_gc_preview(manager, argument: str, *, base_dir,
                          default_keep: int = DEFAULT_KEEP,
                          keep_labeled: bool = True) -> dict:
    """只读预览；TUI 先弹确认，行式 REPL 打印后再执行。"""
    all_sessions, keep = parse_checkpoint_gc(argument, default_keep=default_keep)
    counts = checkpoints.checkpoint_counts(base_dir)
    current_id = manager.session_id
    current_nodes = counts.get(current_id, 0)
    other_nodes = sum(count for session, count in counts.items()
                      if session != current_id)
    if all_sessions:
        summary = checkpoints.gc_all_checkpoints(
            base_dir, max_nodes=keep, keep_labeled=keep_labeled, dry_run=True)
        summary.update({'mode': 'all', 'keep': keep,
                        'current_nodes': current_nodes,
                        'other_nodes': other_nodes,
                        'sessions_total': len(counts)})
        return summary

    plan = manager.gc_plan(max_nodes=keep)
    size = sum(item['bytes'] for item in plan)
    return {
        'mode': 'current', 'keep': keep, 'dry_run': True,
        'removed': len(plan), 'bytes': size,
        'details': [{'session_id': current_id, 'removed': len(plan),
                     'bytes': size}] if plan else [],
        'current_nodes': current_nodes, 'other_nodes': other_nodes,
        'sessions_total': len(counts),
    }


def checkpoint_gc_execute(manager, argument: str, *, base_dir,
                          default_keep: int = DEFAULT_KEEP,
                          keep_labeled: bool = True) -> dict:
    """执行已确认的回收；返回与预览同形的统计。调用方负责先预览。"""
    all_sessions, keep = parse_checkpoint_gc(argument, default_keep=default_keep)
    if all_sessions:
        summary = checkpoints.gc_all_checkpoints(
            base_dir, max_nodes=keep, keep_labeled=keep_labeled, dry_run=False)
        summary.update({'mode': 'all', 'keep': keep,
                        'sessions_total': summary.get('sessions', 0)})
        return summary

    current_id = manager.session_id
    plan = manager.gc_plan(max_nodes=keep)
    removed = manager.gc(max_nodes=keep)
    counts = checkpoints.checkpoint_counts(base_dir)
    other_nodes = sum(count for session, count in counts.items()
                      if session != current_id)
    size = sum(item['bytes'] for item in plan)
    return {
        'mode': 'current', 'keep': keep, 'dry_run': False,
        'removed': len(removed), 'bytes': size,
        'details': [{'session_id': current_id,
                     'removed': len(removed), 'bytes': size}] if removed else [],
        'current_nodes': counts.get(current_id, 0),
        'other_nodes': other_nodes, 'sessions_total': len(counts),
    }


def format_checkpoint_gc(summary: dict, *, default_keep: int = DEFAULT_KEEP,
                         done: bool = False) -> str:
    """把预览/执行结果格式化成 TUI 通知和行式 REPL 共用的一段文字。"""
    keep = summary.get('keep', default_keep)
    removed = summary.get('removed', 0)
    size = tool_cache.human_size(summary.get('bytes', 0))
    mode = summary.get('mode', 'current')
    if mode == 'all':
        sessions_total = summary.get('sessions_total', summary.get('sessions', 0))
        scope = (f"所有 {sessions_total} 个会话" if sessions_total
                 else '所有会话')
    else:
        scope = '当前会话'
    if removed:
        prefix = '已清理' if done else '可清理'
        lines = [f'  {prefix} {removed} 个快照（约 {size}）。']
        if mode == 'all':
            sessions = summary.get('sessions', 0)
            affected = len(summary.get('details', [])) or sessions
            total = summary.get('sessions_total', sessions)
            lines.append(f'  涉及 {affected}/{total} 个会话；'
                         f'每个会话保留最新 {keep} 个、head 与带标签快照。')
        else:
            current_nodes = summary.get('current_nodes', 0)
            lines.append(f'  当前会话共 {current_nodes} 个快照，保留上限 {keep}。')
        if not done:
            lines.append('  删除后旧快照不可恢复。')
    else:
        lines = [f'  {scope}无需清理：没有超过保留上限 {keep} 的可回收快照。']
    if mode == 'current' and summary.get('other_nodes'):
        other_nodes = summary['other_nodes']
        lines.append(f'  其他会话还有 {other_nodes} 个快照；'
                     '/checkpoint-gc --all 可统一清理。')
    return '\n'.join(lines)


def format_restore_report(plan: dict) -> str:
    scope = '仅会话' if plan.get('scope') == 'session' else '会话与文件'
    lines = [f"  回溯预览：{plan.get('label') or plan['cp_id']} · {scope}"]
    for entry in plan['actions']:
        lines.append(f"    {entry['action']:<9} {entry['path']}  ({entry['reason']})")
    for title, items in (('存在冲突', plan['conflicts']),
                         ('快照尚未完成，无法校验', plan['unverified']),
                         ('命令或外部改动，无法自动恢复', plan['drift'])):
        if items:
            remaining = f'（另有 {len(items) - 10} 项）' if len(items) > 10 else ''
            lines.append(f"    \033[33m{title}: {', '.join(items[:10])}{remaining}\033[0m")
    lines.append('    注意：记忆、定时任务、任务进程和 worktree 不随回溯撤销。')
    if not plan['ok']:
        lines.append(f"    \033[31m{plan['reason']}\033[0m")
    return '\n'.join(lines)


def rewind_to(messages: list, session_store, manager, ref: str, *,
              force: bool = False, scope: str = 'all', report: bool = True) -> bool:
    """把 messages 就地替换成回溯后的历史，并把 rewind 事件写进 session。

    report=True 时打印完整计划+结果（直接调用时用）；命令层已经打印过计划，传
    report=False，这里只输出「实际做了什么」，避免同一份报告刷两遍。
    """
    try:
        cp_id = manager.resolve(ref)
    except checkpoints.CheckpointError as exc:
        print(f"  \033[31m[rewind] {exc}\033[0m")
        return False
    snapshot = manager.snapshot_path(cp_id)
    if not snapshot.exists():
        print(f"  \033[31m[rewind] snapshot missing for {cp_id}\033[0m")
        return False

    try:
        plan = manager.restore(cp_id, force=force, scope=scope)
    except checkpoints.CheckpointError as exc:
        print(f'  [rewind] {exc}')
        return False
    if report:
        print(format_restore_report(plan))
    if not plan['ok']:
        print(f"  \033[33m[rewind] 未执行任何改动；确认可覆盖后重试 "
              f"/rewind {cp_id} --force\033[0m")
        return False

    session_store.append_rewind(cp_id, scope=scope,
                                snapshot_path=str(snapshot),
                                restored_files=plan['applied'])
    messages[:] = session_store.rebuild_openai_history()
    applied = ', '.join(plan['applied']) if plan['applied'] else '无文件改动'
    print(f"  已恢复会话 · {applied}")
    return True
