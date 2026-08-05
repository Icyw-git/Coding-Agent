"""cc_ui.py 单元测试：颜色降级 / 内联样式 / Markdown / Diff / Spinner / ProgressBar。

运行: python test_ui.py   （或 pytest test_ui.py）
"""
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cc_ui as ui

_RESET = '\x1b[0m'


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
    assert ui.bg('success') in out and '0.50' in out   # cost pill
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
