"""tool_activity：活动行摘要、截断与失败标记。"""
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

    assert ok.startswith('  • bash') and 'pytest -q' in ok and '3.40s ✓' in ok
    assert '0.02s ✗' in failed
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
