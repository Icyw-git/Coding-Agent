"""行式 REPL 的斜杠命令层：解析、分发与本地确认。

组合根仍持有工作区、目录、快照配置和全部可 patch 的执行函数，所以这里统一经
``_h()`` 在调用期读取 ``harness`` 属性。测试对 ``harness.get_checkpoint_manager``、
``harness._confirm`` 等 patch 点因此继续生效，而不是复制一份状态。
"""
from __future__ import annotations

import os
import sys

import grants
import hooks
import llm_route
import rules as user_rules
from checkpoint_commands import format_checkpoint_list, format_restore_report, rewind_to
from checkpoints import CheckpointError
from cli_commands import command_error, format_command_help, parse_command


def _grant_audit(session_store, event: str, record: dict | None, by: str) -> None:
    if session_store is None or record is None:
        return
    try:
        session_store.append(event, path=record.get('path', ''),
                             scope=record.get('scope', 'write'),
                             expires=record.get('expires', ''), by=by,
                             note=record.get('note', ''),
                             session_id=session_store.session_id)
    except Exception as exc:
        print(f'  [grants] 审计写入失败: {type(exc).__name__}: {exc}')


def _format_grants() -> str:
    rows = grants.list_grants()
    if not rows:
        return ('  （没有目录写授权）\n'
                '  用法：/grants add "<目录>" [until YYYY-MM-DD] [note 备注]')
    lines = ['  目录写授权：']
    for row in rows:
        expiry = (f"有效至 {row['expires']}" if row.get('expires')
                  else '永久')
        if not row.get('active'):
            expiry = f"已过期（{row['expires']}）"
        note = f" · {row['note']}" if row.get('note') else ''
        lines.append(f"    {row['path']}  [{expiry}{note}]")
    lines.append('  撤销：/grants remove "<目录>"；秘密文件与系统目录不受授权影响。')
    return '\n'.join(lines)


def _format_rules() -> str:
    ruleset = user_rules.load()
    if ruleset.error:
        return ('  规则文件加载失败：' + str(ruleset.error) + chr(10)
                + '  修好之前所有规则都不生效（等同于没有规则文件）。')
    if not ruleset.rules:
        return ('  （没有用户规则）' + chr(10)
                + f'  文件：{user_rules.path_for_display()}' + chr(10)
                + '  可写 allow / ask / deny；只影响「问不问」，改不动硬拒与档位。')
    lines = [f'  用户规则：{len(ruleset.rules)} 条'
             f'（{user_rules.path_for_display()}）']
    for rule in ruleset.rules:
        tools = ','.join(rule.tools)
        scope = '' if rule.scope == user_rules.SCOPE_GLOBAL else f' [{rule.scope}]'
        note = f' · {rule.note}' if rule.note else ''
        lines.append(f'    {rule.decision:<5} [{tools}]{scope} '
                     f'{", ".join(rule.prefixes)}{note}')
    lines.append('  说明：只作用于 destructive / opaque_command / external_tool 三类询问；'
                 '档位、秘密文件、域名与桌面控制不受影响。')
    return chr(10).join(lines)


def _h():
    """延迟取 harness：模块导入期避免循环，调用期读到最新 patch 结果。"""
    import harness
    return harness


def handle_command(line: str, messages: list, session_store) -> None:
    """REPL 里的 /命令；回溯需要调用方已持有 agent_lock。"""
    h = _h()
    command, argument = parse_command(line)
    if command == '/undo':
        command, argument = '/rewind', 'turn --yes'
    error = command_error(command, argument)
    if error:
        print(f'  {error}')
        return
    parts = [command, *argument.split()]
    if command == '/help':
        print(format_command_help(argument))
        return
    manager = (h.get_checkpoint_manager(session_store)
               if command in ('/checkpoints', '/checkpoint', '/rewind', '/checkpoint-gc')
               else None)

    if command == '/checkpoints':
        print(format_checkpoint_list(manager))

    elif command == '/status':
        print(h.format_status(session_store))

    elif command == '/expand':
        print(h.format_tool_output(argument))

    elif command == '/watch':
        print(h.watch_command(argument))

    elif command == '/diff':
        print(h.format_workspace_diff(color=sys.stdout.isatty()))

    elif command == '/cache':
        if argument.strip().lower() == 'profile':
            print(h.format_storage_profile())
        else:
            print(h.tool_cache.handle_cache_command(
                h.TOOL_RESULTS_DIR, h.SESSIONS_DIR, argument))

    elif command == '/review':
        from review import run_review
        try:
            print(run_review(progress=lambda text: print(text, flush=True)))
        except Exception as exc:
            print(f'  [review] 审阅失败：{exc}')

    elif command == '/model':
        main = llm_route.target_for('main')
        if main is not None:
            print(f'  当前模型：{main.ref}')
            print(f'  服务地址：{main.base_url or "默认地址"}')
            for _role in ('summary', 'memory', 'title', 'approval'):
                role_target = llm_route.target_for(_role)
                if role_target is None:
                    continue
                if (role_target.provider, role_target.model) != (main.provider, main.model):
                    print(f'  {_role}：{role_target.ref}')
        else:
            print(f'  当前模型：{os.getenv("LLM_MODEL_ID", "未配置")}')
            print(f'  服务地址：{os.getenv("LLM_BASE_URL", "默认地址")}')

    elif command == '/sandbox':
        print(h.apply_sandbox_tier(argument) if argument else h.format_sandbox_tier())

    elif command == '/approvals':
        tokens = argument.split()
        if tokens:
            index, choice = int(tokens[0]) - 1, tokens[1]
            pending = hooks.GATE.pending()
            if not 0 <= index < len(pending):
                print('  没有该序号的待审批操作；先运行 /approvals 查看列表。')
                return
            req = pending[index]
            record = hooks.GATE.resolve(req.req_id, choice == 'y', by='cli')
            print(f'  已决议：{record.status} · {req.tool}')
        else:
            print(h.format_pending_approvals())

    elif command == '/revoke':
        session_id = hooks.current_session_id() or ''
        count = hooks.GATE.revoke_session(session_id or None)
        removed = grants.revoke_for_session(session_id) if session_id else []
        for record in removed:
            _grant_audit(session_store, 'grant_removed', record,
                         'revoke_permissions')
        extra = f'，{len(removed)} 条目录写授权' if removed else ''
        print(f'  已撤销 {count} 条当前会话预授权{extra}。')

    elif command == '/rules':
        print(_format_rules())

    elif command == '/grants':
        parts = argument.split(maxsplit=1)
        action = parts[0].casefold() if parts else ''
        rest = parts[1] if len(parts) > 1 else ''
        if not action:
            print(_format_grants())
            return
        if action not in ('add', 'remove'):
            print('  用法：/grants [add "<目录>" [until YYYY-MM-DD] [note 备注]'
                  '|remove "<目录>"]')
            return
        try:
            path, options = grants.split_path_argument(rest)
        except ValueError as exc:
            print(f'  [grants] {exc}')
            return
        if not path:
            print('  用法：/grants add|remove "<目录>"')
            return
        session_id = hooks.current_session_id() or ''
        try:
            if action == 'remove':
                if options:
                    print('  用法：/grants remove "<目录>"（路径含空格时加引号）')
                    return
                record = grants.remove_path(path)
                if record is None:
                    print(f'  没有找到该目录的授权：{path}')
                    return
            else:
                expires, note = grants.parse_options(options)
                record = grants.add_path(path, expires=expires, note=note,
                                         created_by='cli', session_id=session_id)
        except (grants.GrantError, OSError) as exc:
            print(f'  [grants] {exc}')
            return
        _grant_audit(session_store,
                     'grant_removed' if action == 'remove' else 'grant_added',
                     record, 'cli')
        if action == 'remove':
            print(f"  已撤销目录写授权：{record['path']}")
        else:
            expiry = (f"有效至 {record['expires']}" if record.get('expires')
                      else '永久')
            print(f"  已授权目录写操作：{record['path']}（{expiry}）")

    elif command == '/compact':
        if not messages:
            print('  当前会话没有可压缩的内容。')
            return
        before = len(messages)
        try:
            compacted = h.compact_history(messages)
        except Exception as exc:
            print(f'  [compact] 压缩失败，原会话未改变：{exc}')
            return
        messages[:] = compacted
        h._record_compaction(session_store, messages, 'manual')
        # compaction 事件回放时会重建 marker 消息本身，不能再追加同一条
        print(f'  已压缩会话上下文：{before} 条消息 → {len(messages)} 条。完整记录已保存。')

    elif command == '/find':
        if not argument:
            print('  用法：/find <关键词>（跨会话搜索历史内容）')
            return
        rows = h.SessionEventStore.search_all(h.SESSIONS_DIR, argument, limit=8)
        if not rows:
            print(f'  没有包含「{argument}」的会话。')
            return
        from turn_progress import relative_time
        for row in rows:
            print(f"  {row['id'][:12]}  {relative_time(row.get('updated'))}  {row['title']}")
            print(f"    {row.get('matched', '')}")

    elif command == '/rename':
        if session_store is None:
            print('  [rename] 当前没有会话上下文。')
            return
        title = session_store.append_session_title(argument.strip()[:80], source='user')
        print(f"  会话主题已设置为：{title['title']}")

    elif command == '/sessions':
        print(h.format_session_list())

    elif command in ('/archive', '/delete'):
        from cli_input import choose_cleanup_sessions

        # 先等本进程的后台写入落盘，再移动会话日志。
        h.wait_for_housekeeping()
        if h.housekeeping_busy():
            print('  会话后台整理尚未结束，请稍后再清理。')
            return
        deleting = command == '/delete'
        restoring = argument == 'restore'
        action_key = 'delete' if deleting else 'restore' if restoring else 'archive'
        archive, rows = h.session_cleanup_plan(command.lstrip('/'), argument,
                                               session_store.session_id)
        selected = choose_cleanup_sessions(rows, restore=restoring, delete=deleting)
        selected_rows = [row for row in rows if row['id'] in selected]
        if not selected_rows:
            return
        action = '永久删除' if deleting else '恢复' if restoring else '归档'
        print(f'  将{action}以下会话（含关联快照）：')
        for row in selected_rows:
            print(f"    {row['title']} · {row['workspace'] or '目录未记录'}")
        print('  请确认其他 CLI/API 服务没有正在使用这些会话。')
        if deleting:
            try:
                confirmed = input('  不可恢复。输入“删除”确认，其他输入取消：').strip() == '删除'
            except (EOFError, KeyboardInterrupt):
                confirmed = False
        else:
            confirmed = h._confirm(f'确认{action} {len(selected_rows)} 个会话？')
        if not confirmed:
            print('  已取消，未执行改动。')
            return
        completed, failures = h.apply_session_cleanup(
            archive, selected_rows, action_key, session_store.session_id)
        for row, error in failures:
            print(f"  未处理 {row['title']}：{error}")
        print(f'  已{action} {completed} 个会话。' + (
            '记录及关联快照不可恢复，项目文件未改动。' if deleting else
            '用 /archive restore 可恢复；项目文件未改动。' if not restoring else '可用 /resume 继续会话。'))

    elif command in ('/workspaces', '/workspace'):
        h.handle_workspace_command(command, argument)

    elif command == '/pwd':
        print(f'  工作区：{h.get_workdir()}')

    elif command == '/cd':
        if len(parts) < 2:
            print(f'  工作区：{h.get_workdir()}')
            return
        try:
            path = argument
            if len(path) >= 2 and path[0] == path[-1] and path[0] in ('"', "'"):
                path = path[1:-1]
            target = h.set_workspace(path)
            print(f'  工作区：{target}')
            if session_store is not None:
                session_store.record_workspace(target)
        except ValueError as exc:
            print(f'  [workspace] {exc}')

    elif command == '/checkpoint':
        label = ' '.join(parts[1:])
        cp_id = h._safe_checkpoint(manager.begin_turn, messages, session_store,
                                   label=label, trigger='manual')
        if cp_id:
            h._safe_checkpoint(manager.seal)
            print(f"  已创建快照{('：' + label) if label else ''}。用 /rewind head 回溯。")

    elif command == '/rewind':
        ref = next((p for p in parts[1:] if not p.startswith('--')), 'turn')
        force = '--force' in parts
        scope = 'session' if '--session' in parts else 'all'
        try:
            cp_id = manager.resolve(ref)
        except CheckpointError as e:
            print(f"  \033[31m[rewind] {e}\033[0m")
            return
        if not manager.snapshot_path(cp_id).exists():
            print(f"  \033[31m[rewind] snapshot missing for {cp_id}\033[0m")
            return

        # 先预览（plan_restore 不落盘），再确认执行
        preview = h._safe_checkpoint(manager.plan_restore, cp_id, scope=scope)
        if preview is None:
            return
        preview['scope'] = scope
        print(format_restore_report(preview))
        if preview['conflicts'] and not force:
            print(f"  \033[33m[rewind] 未执行任何改动；确认可覆盖后重试 "
                  f"/rewind {cp_id} --force\033[0m")
            return
        if '--yes' not in parts and not h._confirm('确认回溯？'):
            print('  已取消回溯，未执行改动。')
            return
        h._safe_checkpoint(rewind_to, messages, session_store, manager, cp_id,
                           force=force, scope=scope, report=False)

    elif command == '/checkpoint-gc':
        try:
            preview = h.checkpoint_gc_preview(manager, argument)
        except ValueError as exc:
            print(f'  {exc}')
            return
        print(h.format_checkpoint_gc(preview))
        if not preview['removed']:
            return
        if preview['mode'] == 'all' and not h._confirm('确认清理这些旧快照？'):
            print('  已取消，未删除任何快照。')
            return
        result = h.checkpoint_gc_execute(manager, argument)
        print(h.format_checkpoint_gc(result, done=True))

    else:
        print(f"  unknown command: {command}")
