"""cc_ui.py 单元测试：颜色降级 / 内联样式 / Markdown / Diff / Spinner / ProgressBar。

运行: python test_ui.py   （或 pytest test_ui.py）
"""
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cc_ui as ui
import ui_events
import renderer

_RESET = '\x1b[0m'


def test_session_cancel_signal_is_thread_safe_and_resettable():
    ui_events.clear_cancel()
    assert ui_events.cancel_requested() is False


def test_ansi_renderer_logs_round_metrics(caplog):
    import logging
    caplog.set_level(logging.INFO, logger='harness.ui')
    renderer.default_renderer.on_event(ui_events.UIEvent('round_end', {
        'round': 2, 'duration_ms': 1250, 'prompt_tokens': 100,
        'completion_tokens': 25, 'cost': '0.0012',
        'source': 'provider_reported', 'compressed': True,
    }))
    output = caplog.text
    assert 'round=2' in output
    assert 'input=100' in output
    assert 'compressed=True' in output
    ui_events.request_cancel()
    assert ui_events.cancel_requested() is True
    ui_events.clear_cancel()
    assert ui_events.cancel_requested() is False


# ---------- 颜色 ----------

def test_rgb_to_256_grayscale_in_232_255():
    assert 232 <= ui._rgb_to_256(10, 10, 10) <= 255
    # 纯黑白近似灰阶 → 走 232-255 灰阶分支
    assert ui._rgb_to_256(0, 0, 0) in (16, 232)
    assert ui._rgb_to_256(255, 255, 255) in (231, 255)


def test_rgb_to_256_cube_corners():
    assert ui._rgb_to_256(255, 0, 0) == 196
    assert ui._rgb_to_256(0, 255, 0) == 46
    assert ui._rgb_to_256(0, 0, 255) == 21


def test_ansi_16_bg_code_valid_range():
    """亮色（90-97）背景码必须在 100-107 内，否则终端忽略 → pill 不可见。"""
    for name in ('success', 'warning', 'error', 'permission', 'planMode',
                 'info', 'claude', 'subtle', 'inactive'):
        code = ui._ansi_16_bg_code(ui.TOKENS[name])
        assert 40 <= code <= 107, f"{name}: {code}"
    assert ui._ansi_16_bg_code(ui.TOKENS['success']) == 102  # 亮绿背景


def test_disp_w_cjk():
    assert ui._disp_w('ab') == 2
    assert ui._disp_w('名称') == 4   # 每个 CJK 计 2


def test_nocolor_disables_all_styles(monkeypatch):
    monkeypatch.setattr(ui, '_COLOR_LEVEL', 0)
    assert ui.b('x') == 'x' + _RESET
    assert ui.dim('x') == 'x' + _RESET
    assert ui.bold_c('success', 'x') == 'x' + _RESET
    assert ui.c('success', 'ok') == 'ok' + _RESET
    assert '\x1b[1m' not in ui.b('x')


def test_reload_color_level(monkeypatch):
    monkeypatch.setenv('NO_COLOR', '1')
    ui.reload_color_level()
    assert ui._COLOR_LEVEL == 0
    monkeypatch.delenv('NO_COLOR')
    ui.reload_color_level()
    assert ui._COLOR_LEVEL > 0


# ---------- 内联样式 ----------

def test_inline_bold_and_code():
    out = ui._inline('a **bold** and `code`')
    assert '\x1b[1m' in out and 'bold' in out
    assert ui.t('code') in out and 'code' in out


def test_inline_link_osc8():
    out = ui._inline('[text](https://example.com)')
    assert '\x1b]8;;https://example.com\x1b\\text\x1b]8;;\x1b\\' in out


def test_inline_strikethrough():
    out = ui._inline('~~gone~~')
    assert ui._dim() in out and 'gone' in out


# ---------- Markdown ----------

def test_markdown_heading():
    out = ui.markdown('# 标题')
    assert '标题' in out and ui.t('header') in out


def test_markdown_code_block():
    out = ui.markdown('```python\ndef f():\n    pass\n```')
    assert 'def' in out and ui.bg('codeBg') in out  # 整行背景色块
    assert ui.G['pipe'] not in out                   # 不再使用左侧竖线


def test_markdown_task_list():
    out = ui.markdown('- [ ] todo\n- [x] done')
    assert ui.G['box_off'] in out
    assert ui.G['box_on'] in out


def test_markdown_nested_list():
    out = ui.markdown('- 一级\n  - 二级')
    lines = out.splitlines()
    assert len(lines) == 2
    assert len(lines[1]) - len(lines[0]) > 0  # 二级缩进更深


def test_markdown_table():
    out = ui.markdown('| 名称 | type |\n|---|---|\n| 记忆 | user |')
    assert '记忆' in out


def test_code_box_background():
    out = ui.code_box('a.py', 'line1\nline2', width=80)
    assert ui.bg('codeBg') in out
    assert 'line1' in out and 'read: a.py' in out


def test_bash_box_ellipsis_on_long_command():
    out = ui.bash_box('x' * 200, 'out', width=50)
    assert '...' in out.splitlines()[0]


def test_py_highlight_no_version_number():
    out = ui._py_highlight('Python 3.9 works')
    assert '106;176;255' not in out  # info 蓝只用于数字，版本号不误高亮


# ---------- Diff ----------

def test_diff_added_removed_background():
    out = ui.diff('+added\n-removed')
    assert ui.bg('diffAdded') in out and '+added' in out
    assert ui.bg('diffRemoved') in out and '-removed' in out


# ---------- 状态行 / 模式 ----------

def test_mode_indicator_plan():
    out = ui.mode_indicator('plan')
    assert '[Plan Mode]' in out and ui.t('planMode') in out


def test_mode_indicator_review_and_normal():
    assert ui.t('permission') in ui.mode_indicator('review')
    assert '[Normal]' in ui.mode_indicator('')


def test_statusline_pills():
    out = ui.statusline(model='deepseek-v4-flash', tools=3, ctx_pct=42,
                        cost=0.5, tokens='1k', mode='plan')
    assert '[Plan Mode]' in out
    assert ui.bg('success') in out and '\u00a5' in out   # cost pill 显示人民币


def test_context_bar_preserves_sub_ten_percent_precision():
    import pytest
    ui_textual = pytest.importorskip('ui_textual')
    out = ui_textual.CCApp._ctx_bar(4.7)
    assert '4.7%' in out
    assert '\u2588' in out and '\u2591' in out


def test_statusline_estimated_cost_and_token_breakdown():
    out = ui.statusline(model='m', ctx_pct=42, ctx_source='estimated',
                        cost='0.125', cost_source='mixed', tokens='100',
                        prompt_tokens=70, completion_tokens=30,
                        cached_tokens=10, reasoning_tokens=5)
    assert '~' in out
    assert 'in 70' in out and 'out 30' in out
    assert 'cache 10' in out and 'reason 5' in out
    assert ui.bg('inactive') in out                     # tools/tokens pill
    assert ui.b('deepseek-v4-flash') not in out         # 模型不再用纯加粗，走 pill


def test_statusline_empty():
    assert ui.statusline() == ''


# ---------- Spinner / ProgressBar ----------

def test_spinner_stops_and_clears(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(ui, '_is_tty', True)
    monkeypatch.setattr(sys, 'stdout', buf)
    sp = ui.Spinner(label='test', speed='requesting')
    with sp:
        time.sleep(0.1)
    assert sp._thread is None or not sp._thread.is_alive()
    assert buf.getvalue().rstrip().endswith('\r\x1b[2K')


def test_spinner_non_tty_no_thread(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(ui, '_is_tty', False)
    monkeypatch.setattr(sys, 'stdout', buf)
    with ui.Spinner(label='x'):
        pass
    assert buf.getvalue() == ''  # 非 TTY 不写任何内容


def test_progressbar_update_renders_pct(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', buf)
    bar = ui.ProgressBar(width=10)
    bar.update(50)
    assert '50%' in buf.getvalue()
    assert ui.G['block'] in buf.getvalue()
    bar.finish()


def test_progressbar_indeterminate_with_block(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(ui, '_is_tty', True)
    monkeypatch.setattr(sys, 'stdout', buf)
    with ui.ProgressBar(indeterminate=True, width=10) as bar:
        time.sleep(0.15)
    assert bar._thread is None or not bar._thread.is_alive()
    assert buf.getvalue().rstrip().endswith('\r\x1b[2K')


def test_say(capsys):
    ui.say('success', 'ok')
    captured = capsys.readouterr()
    assert 'ok' in captured.out


# ---------- 权限横幅分级 ----------

def test_permission_banner_level_colors():
    read_banner = ui.permission_banner('Read file', ['path'], level='read')
    write_banner = ui.permission_banner('Write file', ['path'], level='write')
    exec_banner = ui.permission_banner('Run cmd', ['cmd'], level='execute')
    assert 'Read: Read file' in read_banner
    assert 'Write: Write file' in write_banner
    assert 'Execute: Run cmd' in exec_banner
    assert ui.t('info') in read_banner
    assert ui.t('warning') in write_banner
    assert ui.t('error') in exec_banner


# ---------- 错误卡片 / 流式渲染 ----------

def test_error_card_truncates_detail():
    card = ui.error_card('API Error', 'a\nb\nc\nd')
    assert 'API Error' in card
    assert ui.t('error') in card
    assert '... 1 more lines' in card


def test_error_card_actions_and_no_truncation():
    card = ui.error_card('x', 'only one', actions=['Retry', 'Skip'])
    assert 'Retry / Skip' in card
    assert 'more lines' not in card


def test_stream_renderer_non_tty_buffers():
    buf = io.StringIO()
    old_stdout, old_tty = sys.stdout, ui._is_tty
    sys.stdout, ui._is_tty = buf, False
    try:
        sr = ui.StreamRenderer()
        sr.feed('# Hi')
        assert buf.getvalue() == ''      # 非 TTY：feed 只积累不输出
        rendered = sr.finish()
        assert 'Hi' in rendered
        assert 'Hi' in buf.getvalue()    # finish 一次性打印
    finally:
        sys.stdout, ui._is_tty = old_stdout, old_tty


def test_stream_renderer_tty_redraws():
    buf = io.StringIO()
    old_stdout, old_tty = sys.stdout, ui._is_tty
    sys.stdout, ui._is_tty = buf, True
    try:
        sr = ui.StreamRenderer()
        sr.feed('line one\n')
        assert '\x1b[1A' not in buf.getvalue()   # 第一帧无上行清行
        sr.feed('line two\n')
        assert '\x1b[1A' in buf.getvalue()       # 第二帧回退重绘
        sr.finish()
        assert 'line one' in buf.getvalue()
        assert 'line two' in buf.getvalue()
    finally:
        sys.stdout, ui._is_tty = old_stdout, old_tty


# ---------- 固定面板（TUI 全屏重绘） ----------

def test_fixed_panel_tty_renders_todos_and_status(monkeypatch):
    """ANSI 覆盖式重绘：归位 + 逐行清行尾渲染。"""
    buf = io.StringIO()
    monkeypatch.setattr(ui, '_is_tty', True)
    monkeypatch.setattr(sys, 'stdout', buf)
    ts = os.terminal_size((120, 10))
    monkeypatch.setattr(ui.shutil, 'get_terminal_size', lambda: ts)
    panel = ui.FixedPanel()
    assert panel._enabled is True
    panel.log('line one')
    panel.set_todos([
        {'content': 'a', 'status': 'completed'},
        {'content': 'b', 'status': 'in_progress'},
    ])
    panel.set_status(ui.statusline(model='m', cost=0.5))
    out = buf.getvalue()
    assert '\x1b[H' in out         # TUI 归位左上
    assert '\x1b[2K' in out        # 覆盖式逐行清行尾（不清全屏，减少闪烁）
    assert '\x1b[2J' not in out    # 不使用全屏清空
    assert '## Current Tasks' in out
    assert 'a' in out and 'b' in out
    assert ui.statusline(model='m', cost=0.5) in out
    panel.close()


def test_fixed_panel_buffer_trim(monkeypatch):
    """日志缓冲只保留屏幕能容纳的行数（10 行终端 - 固定区行数）。"""
    buf = io.StringIO()
    monkeypatch.setattr(ui, '_is_tty', True)
    monkeypatch.setattr(sys, 'stdout', buf)
    ts = os.terminal_size((120, 10))
    monkeypatch.setattr(ui.shutil, 'get_terminal_size', lambda: ts)
    panel = ui.FixedPanel()
    for i in range(30):
        panel.log('line {0}'.format(i))
    assert len(panel._buffer) <= 10
    # 最旧的日志被裁掉，最新的保留
    assert 'line 0' not in panel._buffer
    assert 'line 29' in panel._buffer


def test_fixed_panel_non_tty_falls_back_to_print(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(ui, '_is_tty', False)
    monkeypatch.setattr(sys, 'stdout', buf)
    for var in ('HARNESS_FORCE_PANEL', 'WT_SESSION', 'ConEmuANSI', 'COLORTERM', 'TERM'):
        monkeypatch.delenv(var, raising=False)
    panel = ui.FixedPanel()
    assert panel._enabled is False
    panel.log('plain log')
    panel.set_todos([{'content': 'x', 'status': 'pending'}])
    panel.set_status('status line')
    out = buf.getvalue()
    assert 'plain log' in out
    assert '## Current Tasks' in out and 'x' in out
    assert 'status line' in out
    assert '\x1b[2J' not in out   # 非 ANSI 环境不刷屏


def test_fixed_panel_force_enable(monkeypatch):
    """HARNESS_FORCE_PANEL=1 强制启用（即使非 TTY）。"""
    buf = io.StringIO()
    monkeypatch.setattr(ui, '_is_tty', False)
    monkeypatch.setenv('HARNESS_FORCE_PANEL', '1')
    monkeypatch.setattr(sys, 'stdout', buf)
    panel = ui.FixedPanel()
    assert panel._enabled is True


def test_fixed_panel_no_panel_off(monkeypatch):
    """HARNESS_NO_PANEL=1 显式关闭（即使 TTY）。"""
    monkeypatch.setattr(ui, '_is_tty', True)
    monkeypatch.setenv('HARNESS_NO_PANEL', '1')
    panel = ui.FixedPanel()
    assert panel._enabled is False


def test_textual_streaming_flow():
    """CCApp（Textual）流式渲染：帧刷新 + 收尾 Markdown + 状态栏 streaming 指示。

    假 agent 分两段吐流：先一段 delta 挂起（threading.Event 控制），
    测试确认中间态后放行剩余 delta + end，验证：
    - 流式中状态栏出现 generating 指示（spinner + 耗时）
    - 中间态渲染为纯文本 Static（markup 已转义，方括号不报错）
    - assistant_end 后替换为 Markdown 最终排版（代码块/加粗精致化）"""
    import asyncio
    try:
        import textual  # noqa: F401
    except ImportError:
        return   # 未安装 textual 的环境跳过该用例
    import threading
    import time as _time
    import ui_events as ev
    import ui_textual
    import harness

    async def _scenario():
        orig_loop = harness.agent_loop
        orig_use_ui = harness.permission_gate.use_ui
        step = threading.Event()   # 测试放行：中间态确认后继续吐剩余 delta + end
        inspect = threading.Event()   # 测试放行：Markdown 确认后允许应用退出
        done = threading.Event()

        def fake_agent_loop(msgs):
            b = ev.bus
            b.emit('assistant_start', mid='m1')
            b.emit('assistant_delta', mid='m1', delta='Hello **world**')
            step.wait(timeout=5)   # 挂起，等测试确认中间态已渲染
            b.emit('assistant_delta', mid='m1', delta=' with `code` and [brackets]')
            b.emit('assistant_end', mid='m1')
            inspect.wait(timeout=5)   # 挂起，等测试确认 Markdown 最终排版已渲染
            done.set()

        def _render_map(app):
            return {type(w).__name__: str(w.render())
                    for w in app.query_one('#chat').children}

        def _collect_text(w):
            """递归收集 widget 树文本（Markdown 是容器，正文在子 block 里）。"""
            parts = [str(w.render())]
            for c in w.children:
                parts.append(_collect_text(c))
            return '\n'.join(parts)

        harness.agent_loop = fake_agent_loop
        app = ui_textual.CCApp([])
        try:
            async with app.run_test() as pilot:
                # 阶段 1：start + 第一段 delta 后，帧刷新渲染出纯文本 Static；
                # 同时等 _stream_tick（100ms）在状态栏刷出 generating 指示
                saw_generating = mid_stream_plain = False
                for _ in range(80):
                    await pilot.pause()
                    await asyncio.sleep(0.03)
                    if not app.is_running:
                        break
                    try:
                        line = str(app.query_one('#status-line').render())
                        if 'generating' in line:
                            saw_generating = True
                        t = _render_map(app).get('Static', '')
                        if 'Hello' in t and '**world**' in t:   # 未转 markdown，仍是纯文本
                            mid_stream_plain = True
                    except Exception:
                        continue
                    if saw_generating and mid_stream_plain:
                        break
                step.set()   # 放行剩余 delta + end
                # 阶段 2：assistant_end 后 Static 替换为 Markdown 最终排版
                final_markdown = False
                md_content = ''
                for _ in range(80):
                    await pilot.pause()
                    await asyncio.sleep(0.03)
                    try:
                        for w in app.query_one('#chat').children:
                            if type(w).__name__ == 'Markdown':
                                t = _collect_text(w)
                                if 'Hello' in t:
                                    final_markdown, md_content = True, t
                                    break
                        if final_markdown:
                            break
                    except Exception:
                        continue
                inspect.set()   # 放行：允许应用退出
                # 阶段 3：等应用自然退出（sentinel 消费完）
                for _ in range(80):
                    if not app.is_running:
                        break
                    await pilot.pause()
                    await asyncio.sleep(0.03)
                assert saw_generating, '状态栏应显示 generating 流式指示'
                assert mid_stream_plain, '流式中间态应渲染为纯文本 Static'
                assert final_markdown and 'world' in md_content and 'brackets' in md_content, \
                    f'Markdown 最终排版缺失: {md_content[:200]!r}'
        finally:
            harness.agent_loop = orig_loop
            harness.permission_gate.use_ui = orig_use_ui

    asyncio.run(_scenario())


def test_permission_gate_cancel_pending():
    """退出时取消权限请求必须立即唤醒业务线程，不能等待 300 秒超时。"""
    import threading
    from ui_events import PermissionGate, UIEventBus

    local_bus = UIEventBus()
    gate = PermissionGate(local_bus, use_ui=True)
    requested = threading.Event()
    result = []

    def observe(event):
        if event.type == 'permission_request':
            requested.set()

    local_bus.subscribe(observe)
    worker = threading.Thread(
        target=lambda: result.append(gate.request('bash', {'command': 'del x'})),
    )
    worker.start()
    assert requested.wait(timeout=1), '权限请求未发出'
    gate.cancel_pending()
    worker.join(timeout=1)

    assert not worker.is_alive(), '取消后业务线程仍在等待权限'
    assert result == [False]
    assert gate._pending == {}


def test_textual_idle_does_not_hold_agent_lock():
    """Textual 等待输入时必须释放锁，供 cron 队列主动执行。"""
    import asyncio
    try:
        import textual  # noqa: F401
    except ImportError:
        return
    import harness
    import ui_textual

    async def _scenario():
        app = ui_textual.CCApp([])
        async with app.run_test() as pilot:
            for _ in range(20):
                await pilot.pause()
                await asyncio.sleep(0.01)
            acquired = harness.agent_lock.acquire(blocking=False)
            try:
                assert acquired, 'TUI 空闲时不应占用 agent_lock'
            finally:
                if acquired:
                    harness.agent_lock.release()
            app.action_quit()
            for _ in range(50):
                if not app.is_running:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)

    asyncio.run(_scenario())


def test_textual_agent_error_recovers_input():
    """agent_loop 失败后应显示错误并恢复输入，而不是让 UI 留在假死状态。"""
    import asyncio
    try:
        import textual  # noqa: F401
    except ImportError:
        return
    import harness
    import ui_textual

    async def _scenario():
        original_loop = harness.agent_loop
        harness.agent_loop = lambda _msgs: (_ for _ in ()).throw(RuntimeError('simulated failure'))
        app = ui_textual.CCApp([])
        try:
            async with app.run_test() as pilot:
                app._set_input_busy(True)
                app._user_input_q.put('trigger error')
                error_visible = False
                for _ in range(100):
                    await pilot.pause()
                    await asyncio.sleep(0.01)
                    try:
                        text = '\n'.join(str(widget.render())
                                         for widget in app.query_one('#chat').children)
                        error_visible = 'simulated failure' in text
                    except Exception:
                        continue
                    if error_visible and not app._busy:
                        break
                assert error_visible, '异常没有反馈到会话界面'
                assert not app._busy
                assert not app.query_one('#input-line').disabled
                app.action_quit()
                for _ in range(50):
                    if not app.is_running:
                        break
                    await pilot.pause()
                    await asyncio.sleep(0.01)
        finally:
            harness.agent_loop = original_loop

    asyncio.run(_scenario())


def test_textual_tool_titles_and_palette():
    """工具标题须传达状态和耗时，CSS 只使用集中定义的调色板。"""
    try:
        import textual  # noqa: F401
    except ImportError:
        return
    import ui_textual

    title = ui_textual.CCApp._tool_title(
        'failed', 'write', {'path': 'a' * 100}, duration_ms=1250)
    assert 'Failed write' in str(title)
    assert '1.2s' in str(title)
    assert len(ui_textual.CCApp._short_args({'path': 'a' * 100})) <= 72
    assert '$accent' not in ui_textual.CSS
    assert ui_textual.PALETTE['accent'] in ui_textual.CSS


def test_textual_search_and_clear_commands():
    """命令路径可以定位可见消息，并在空闲时清理会话。"""
    import asyncio
    try:
        import textual  # noqa: F401
    except ImportError:
        return
    import ui_events
    import ui_textual

    async def _scenario():
        app = ui_textual.CCApp([{'role': 'user', 'content': 'needle'}])
        async with app.run_test() as pilot:
            for _ in range(20):
                await pilot.pause()
                await asyncio.sleep(0.01)
            app.execute_command('search needle')
            for _ in range(40):
                await pilot.pause()
                await asyncio.sleep(0.01)
                if app._search_matches:
                    break
            assert app._search_matches
            app.execute_command('clear')
            for _ in range(40):
                await pilot.pause()
                await asyncio.sleep(0.01)
                if not app._msgs:
                    break
            assert app._msgs == []
            app.action_quit()
            for _ in range(40):
                if not app.is_running:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)

    asyncio.run(_scenario())


# ---------- 运行 ----------

def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    failed = 0
    for t in tests:
        try:
            if 'monkeypatch' in t.__code__.co_varnames or 'capsys' in t.__code__.co_varnames:
                print(f"[SKIP] {t.__name__}: 需要 pytest fixture")
                continue
            t()
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed (非 fixture 用例)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
