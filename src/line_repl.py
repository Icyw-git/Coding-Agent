"""行式 REPL：Textual 不可用或显式选择纯文本时的回退入口。

输入循环只做解析与分发；工作区、会话存储、流式回调与事件总线仍归 ``harness``
所有，所以统一经 ``_h()`` 在调用期读取。这样测试对 ``harness.agent_loop``、
``harness.run_bash``、``harness.set_workspace`` 等 patch 点继续生效，跨模块的
``ACTIVE_TICKER`` 也仍是 harness 上的同一个属性。
"""
from __future__ import annotations

import sys
from contextlib import nullcontext
from pathlib import Path

from cli_commands import command_error, parse_command
from session_store import SessionReplayError
from workspace_choices import find_workspace


def _h():
    """延迟取 harness：模块导入期避免循环，调用期读到最新 patch 结果。"""
    import harness
    return harness


def prompt_text() -> str:
    """提示符带上工作区名、分支与档位：多会话/多目录/多分支时不容易发错地方。"""
    h = _h()
    workspace = h.get_workdir()
    mode = h.load_policy().sandbox_mode
    mark = {'read-only': 'ro', 'workspace-write': 'rw',
            'danger-full-access': '!!'}.get(mode, mode)
    branch = h.current_branch(workspace)
    label = workspace.name or str(workspace)
    if branch:
        label = f'{label} ({branch})'
    return f'\n{label} [{mark}] > '


_RENDERER_ATTACHED = False


def _attach_renderer() -> None:
    """TTY 行式 REPL 订阅事件总线；非 TTY（测试/管道）保持纯文本。

    只 attach 一次：同一进程多次 run_repl 时不重复订阅。
    """
    global _RENDERER_ATTACHED
    if _RENDERER_ATTACHED or not sys.stdout.isatty():
        return
    from renderer import ReplRenderer
    _h().bus.subscribe(ReplRenderer().on_event)
    _RENDERER_ATTACHED = True


def _resume(argument: str, session_store):
    """`/r`、`/resume`、`/sessions`：返回 (history, store)，失败/取消返回 None。"""
    h = _h()
    from cli_input import choose_session

    try:
        if argument == 'last':
            rows = h.session_rows(limit=1, include_empty=False)
            if not rows:
                print('  [session] 没有可恢复的会话。')
                return None
            if rows[0]['id'] == session_store.session_id:
                print(f"  [session] 最近会话就是当前会话：{rows[0]['title']}")
                return None
            path = h.resolve_session_path(rows[0]['id'])
        elif not argument:
            identifier = choose_session(h.session_rows(limit=None, include_empty=True),
                                        session_store.session_id, h.get_workdir())
            if identifier is None:
                return None
            path = h.resolve_session_path(identifier)
        else:
            rows = h.session_rows(include_empty=False, query=argument)
            matches = [h.SESSIONS_DIR / f"session_{row['id']}.jsonl" for row in rows]
            if len(matches) == 1:
                path = matches[0]
            elif len(matches) > 1:
                selected = choose_session(rows, session_store.session_id)
                if selected is None:
                    return None
                path = h.resolve_session_path(selected)
            else:
                path = h.resolve_session_path(argument)
        candidate = h.SessionEventStore.open(path)
        history = candidate.rebuild_openai_history(recover_pending=True)
        summary = candidate.session_summary()
        if summary['workspace']:
            h.set_workspace(summary['workspace'])
        else:
            print('  [session] 工作目录未记录，保留当前目录；请用 /cd 确认。')
        print(f"  已恢复：{summary['title']}\n  工作区：{h.get_workdir()}")
        return history, candidate
    except (FileNotFoundError, ValueError, SessionReplayError) as exc:
        print(f'  [session] {exc}')
        return None


def _new(requested: str, session_store):
    """`/n`、`/new [--ws <序号|名称|路径>|会话ID]`：返回 ([], store) 或 None。"""
    h = _h()
    target = h.get_workdir()
    session_id = None
    lowered = requested.lower() if requested else ''
    if lowered.startswith('--'):
        if lowered != '--ws' and not lowered.startswith('--ws '):
            print('  [workspace] 未知参数；用法：'
                  '/new [--ws <序号|名称|路径>|会话ID]')
            return None
        selector = requested[4:].strip()
        if not selector:
            print('  [workspace] 用法：/new --ws <序号|名称|路径>')
            return None
        try:
            target = find_workspace(h.registered_workspaces(), selector)['path']
        except ValueError as exc:
            # 直接给路径也允许（未登记时不必先 /workspace add）
            candidate = Path(selector).expanduser()
            if not candidate.is_absolute():
                candidate = h.get_workdir() / candidate
            if candidate.is_dir():
                target = str(candidate.resolve())
            else:
                print(f'  [workspace] {exc}')
                return None
    elif requested:
        if not requested.replace('-', '').replace('_', '').isalnum():
            print('  [session] invalid session id')
            return None
        session_id = requested
    else:
        target = h._workspace_for_new_session()
        if target is None:
            print('  [session] 已取消新建会话。')
            return None
    try:
        store = h.start_new_session(workspace=target, session_id=session_id)
    except (ValueError, FileExistsError) as exc:
        print(f'  [session] {exc}')
        return None
    print(f'  已开始新会话，工作区：{h.get_workdir()}')
    return [], store


def _fork(argument: str, session_store):
    """`/fork [事件序号]`：返回 (history, store) 或 None。"""
    h = _h()
    upto = None
    if argument:
        if not argument.isdigit():
            print('  [fork] 用法：/fork [事件序号]，缺省复制全部历史。')
            return None
        upto = int(argument)
    try:
        forked = h.SessionEventStore.fork(session_store, upto_seq=upto)
    except (FileExistsError, OSError, SessionReplayError) as exc:
        print(f'  [fork] 分叉失败：{exc}')
        return None
    print(f'  已分叉出新会话 {forked.session_id}（原会话保留），后续对话在新会话进行。')
    return forked.rebuild_openai_history(), forked


def _run_turn(line: str, messages: list, session_store) -> None:
    """把一条用户输入交给 agent_loop，并把流式文本直接写到终端。"""
    h = _h()
    h.ui_emit('user_message', text=line)
    h.turn_control.begin_turn()
    ticker = None
    if sys.stdout.isatty():
        from turn_progress import ProgressTicker, TurnProgress
        progress = TurnProgress()
        try:
            progress.follow(session_store, session_store.path.stat().st_size)
        except OSError:
            progress.follow(session_store, 0)
        ticker = ProgressTicker(progress).start()
        h.ACTIVE_TICKER = ticker

    def on_delta(part):
        nonlocal ticker
        if h.turn_control.cancel_requested():
            raise KeyboardInterrupt
        if ticker is not None:
            ticker.stop()   # 正文开始上屏前清掉进度行
            ticker = None
        print(part, end='', flush=True)

    try:
        # CLI 直连流式：模型文本逐段上屏（栈内 gateway 早已用同一条路径）
        h.agent_loop(messages, session_store=session_store, on_delta=on_delta)
    except KeyboardInterrupt:
        h.ui_emit('activity', state='cancelled', detail='Request cancelled')
        print('\n[会话已中断]')
    except Exception as exc:   # 让 UI 看到失败，同时保留终端可读性
        h.ui_emit('error', msg=f'{type(exc).__name__}: {exc}')
        print(f'\n[error] {type(exc).__name__}: {exc}')
    finally:
        h.turn_control.end_turn()
        if ticker is not None:
            ticker.stop()
            h.ACTIVE_TICKER = None
    messages[:] = session_store.rebuild_openai_history(recover_pending=True)


def run_line_repl(messages: list, session_store) -> None:
    """最小交互循环；普通输入进 agent_loop，`/` 开头的命令不进模型。"""
    h = _h()
    _attach_renderer()
    policy = h.load_policy()
    print(f"  会话 {session_store.session_id[:8]} · "
          f"{session_store.session_summary()['title']}")
    print(f"  模型 {h.format_main_target()} · 工作区 {h.get_workdir()}")
    print(f'  档位 {policy.sandbox_mode} / 审批 {policy.approval_policy} · '
          f'沙箱 {h.sandbox.configured_mode()}')
    print('  输入消息开始；/ 命令 · /resume 恢复 · Ctrl-C 中断 · Ctrl-D 退出')

    from cli_input import create_reader

    def session_choices():
        for row in h.session_rows(limit=None):
            from turn_progress import relative_time
            yield row['id'], (f"{row['title']} · {relative_time(row.get('updated'))}"
                              f" · {row['workspace'] or '工作目录未记录'} · {row['preview']}")

    read_line = create_reader(lambda: h.get_workdir(), session_choices)

    from prompt_toolkit.patch_stdout import patch_stdout

    # TTY 下输入线程持有提示符：回合内与命令输出都打印到提示符上方。
    stdout_guard = patch_stdout(raw=True) if sys.stdout.isatty() else nullcontext()
    with stdout_guard:
        interrupted = 0
        while True:
            try:
                line = read_line(prompt_text()).strip()
                interrupted = 0
            except EOFError:
                print('\n[会话已结束]')
                break
            except KeyboardInterrupt:
                # Codex 习惯：第一次 Ctrl-C 只提示，第二次才退出，避免误触丢会话。
                if interrupted:
                    print('\n[会话已结束]')
                    break
                interrupted += 1
                print('\n  再按一次 Ctrl-C 退出（Ctrl-D 也可退出）。')
                continue
            if not line:
                continue
            if line.startswith('!') and line[1:].strip():
                # `!cmd`：直接执行 shell，输出只显示在终端，不进入模型上下文。
                command = line[1:].strip()
                try:
                    print(h.run_bash(command))
                except Exception as exc:
                    print(f'Error: {exc}')
                continue
            if line.startswith('/') or line == 'cd' or line.startswith('cd '):
                if line == 'cd' or line.startswith('cd '):
                    line = '/' + line
                command, argument = parse_command(line)
                error = command_error(command, argument)
                if error:
                    print(f'  {error}')
                    continue
                line = f'{command} {argument}'.rstrip()
                if command in ('/exit', '/quit'):
                    break
                if command in ('/r', '/resume', '/sessions'):
                    selected = _resume(argument, session_store)
                    if selected is not None:
                        messages, session_store = selected
                    continue
                if command in ('/n', '/new'):
                    created = _new(argument, session_store)
                    if created is not None:
                        messages, session_store = created
                    continue
                if command == '/fork':
                    forked = _fork(argument, session_store)
                    if forked is not None:
                        messages, session_store = forked
                    continue
                h.handle_command(line, messages, session_store)
                continue
            messages[:] = session_store.rebuild_openai_history(recover_pending=True)
            messages.append({'role': 'user', 'content': line})
            session_store.append_message(messages[-1])
            _run_turn(line, messages, session_store)
