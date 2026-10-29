"""tool_activity：活动行摘要、截断与失败标记。"""
import pytest

import tool_activity


def test_summarize_prefers_intent_fields():
    assert tool_activity.summarize('bash', {'command': 'pytest -q'}) == 'pytest -q'
    assert tool_activity.summarize('read', {'path': 'src/app.py'}) == 'src/app.py'
    assert tool_activity.summarize('grep', {'pattern': 'TODO'}) == 'TODO'
    assert tool_activity.summarize('todo_write', {'todos': [1, 2]}) == '2 item(s)'


def test_summarize_truncates_and_collapses_whitespace():
    command = 'echo ' + 'x' * 200 + '\nnext line'
    summary = tool_activity.summarize('bash', {'command': command})

    assert len(summary) <= 61
    assert summary.endswith('…')
    assert '\n' not in summary


def test_summarize_falls_back_to_cwd_and_empty():
    assert tool_activity.summarize('ls', {'cwd': 'src'}) == 'cwd=src'
    assert tool_activity.summarize('compact', {}) == ''


def test_format_line_marks_status_and_background():
    ok = tool_activity.format_line('bash', {'command': 'pytest -q'}, 3.4)
    failed = tool_activity.format_line('read', {'path': 'a.py'}, 0.02, failed=True)
    background = tool_activity.format_line('bash', {'command': 'npm build'}, 0.1,
                                           background=True)

    # 工具行用中文名（用户在转录里读它，不是开发者日志）
    assert ok.startswith('  · 执行命令') and 'pytest -q' in ok and '3.40s √' in ok
    assert '0.02s ×' in failed
    assert '↗后台' in background


def test_is_failure_matches_tool_error_convention():
    assert tool_activity.is_failure('Error: nope')
    assert tool_activity.is_failure('  Permission denied by user')
    assert not tool_activity.is_failure('ok')


def test_result_hint_reports_multiline_without_content():
    assert tool_activity.result_hint('a' + chr(10) + 'b' + chr(10) + 'c') == '3 行'
    assert tool_activity.result_hint('single line') == ''
    assert tool_activity.result_hint('No output') == '无输出'
    assert tool_activity.result_hint('') == '空'


def test_format_line_appends_result_hint():
    ok = tool_activity.format_line('read', {'path': 'a.py'}, 0.1, hint='42 行')
    failed = tool_activity.format_line('read', {'path': 'a.py'}, 0.1,
                                       failed=True, hint='42 行')

    assert ok.endswith('→ 42 行')
    assert '→' not in failed


def test_preview_lines_folds_long_output():
    output = '\n'.join(f'line {i}' for i in range(20))

    lines, hidden = tool_activity.preview_lines(output)

    assert lines[0] == 'line 0' and lines[-1] == 'line 19'
    assert lines[4] == '…'
    assert hidden == 14


def test_preview_lines_keeps_short_output_intact():
    lines, hidden = tool_activity.preview_lines('a\nb')

    assert lines == ['a', 'b'] and hidden == 0


# ---------- 桌面工具的活动行 ----------

@pytest.mark.parametrize('args,fragment', [
    ({'action': 'click', 'x': 10, 'y': 20}, '点击 (10,20)'),
    ({'action': 'type', 'text': '你好世界'}, '输入 4 字'),
    ({'action': 'key', 'key': 'ctrl+v'}, '按键 ctrl+v'),
    ({'action': 'scroll', 'amount': -3}, '滚动 -3 格'),
    ({'action': 'focus'}, '切到前台'),
    ({'action': 'sequence', 'steps': [{}, {}]}, '组合 2 步'),
    ({'action': 'element', 'element': 3, 'operation': 'invoke',
      'expect': '提交按钮'}, '控件 #3「提交按钮」 点击'),
    ({'action': 'element', 'element': 7, 'operation': 'setvalue',
      'expect': '搜索框'}, '控件 #7「搜索框」 写入'),
])
def test_desktop_action_activity_says_what_it_did(args, fragment):
    """活动行每次调用都打印，只写 `操作键鼠` 等于没写。"""
    line = tool_activity.format_line('desktop_action', args, 0.42)
    assert fragment in line
    line.encode('gbk')


@pytest.mark.parametrize('name,args,fragment', [
    ('desktop_wait', {'text': '已保存'}, '等「已保存」出现'),
    ('desktop_wait', {'text': '加载中', 'state': 'absent'}, '等「加载中」消失'),
])
def test_desktop_wait_activity_shows_the_condition(name, args, fragment):
    assert fragment in tool_activity.format_line(name, args, 0.42)


@pytest.mark.parametrize('name', ['desktop_inspect', 'desktop_displays',
                                  'desktop_snapshot', 'desktop_windows'])
def test_read_only_desktop_tools_have_no_detail(name):
    """只读工具没有"做了什么"可言，多写反而是噪声。"""
    line = tool_activity.format_line(name, {}, 0.42)
    assert line.count('  ') >= 2
    assert 'action' not in line
