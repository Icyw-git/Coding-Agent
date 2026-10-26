"""审批面渲染（approval_view）：命令看得全、不可见字符不反过来改写审批界面。

展示位有四个：CLI 提示（hooks._decide_console）、/approvals（harness）、审批卡片
（feishu_gateway）、/perms（gateway_sim）。它们共用 summarize/detail，所以这里的
用例锁的是「同一份可读内容」，而不是某个 print 的字面排版。
"""

from approval_view import detail, safe_text, summarize
from exec_policy import approval_class
from hooks import GATE as permission_gate

# ============================================================
# 登记摘要：命令留头尾，其余字段限长
# ============================================================

def test_long_command_keeps_both_ends():
    command = 'rm -rf ' + 'a' * 2000 + ' /root/.ssh/authorized_keys'
    text = summarize('bash', {'command': command})['command']
    assert text.startswith('rm -rf aaa')
    assert text.endswith('authorized_keys')
    assert '中间省略' in text
    # 截断意味着审批人没看到全貌：不提供「本次会话允许同类」。
    # 省略标记独占一行，按段归类时与头尾不一致，判定键自然为空。
    assert approval_class('bash', {'command': text}) == ''


def test_short_command_passes_through_and_other_fields_stay_bounded():
    assert summarize('bash', {'command': 'ls', 'cwd': None}) == {'command': 'ls',
                                                                 'cwd': None}
    content = summarize('write', {'path': 'a.txt', 'content': 'x' * 5000})['content']
    assert content.endswith('… (5000 chars)')
    assert len(content) < 250


def test_registered_request_carries_the_readable_command():
    command = 'git push ' + 'x' * 3000 + ' --force-with-lease'
    req = permission_gate.request('bash', {'command': command}, 'destructive')
    assert req.args['command'].endswith('--force-with-lease')
    assert '中间省略' in req.args['command']


# ============================================================
# 展示文本：多行可读 + 控制字符可见
# ============================================================

def test_lines_and_tabs_stay_visible_rest_of_c0_is_escaped():
    assert detail('bash', {'command': 'a\nb\tc'}) == 'a\nb\tc'
    shown = detail('bash', {'command': 'echo\x1b[2K\x07\x0ddone'})
    for ch in '\x1b\x07\x0d':
        assert ch not in shown
    assert '\\x1b' in shown and '\\x07' in shown and '\\x0d' in shown


def test_command_is_shown_on_its_own_with_the_other_args_below():
    shown = detail('bash', {'command': 'del x', 'cwd': 'D:/tmp'})
    assert shown.splitlines() == ['del x', 'cwd=D:/tmp']


def test_non_bash_tools_render_as_pairs():
    assert detail('remove_worktree', {'name': 'feat'}) == 'name=feat'
    assert detail('write', {'path': 'a.txt', 'content': None}) == 'path=a.txt'
    assert detail('bash', {}) == ''


def test_safe_text_accepts_non_strings():
    assert safe_text(42) == '42'


def test_screen_tools_render_as_readable_actions():
    assert detail('screen_capture', {'screen': 'all'}) == (
        'capture the local all screen (full screen)')
    assert detail('screen_capture', {'region': '0,0,100,50', 'delay_ms': 250}) == (
        'capture the local primary screen (0,0,100,50) after 250ms')
    assert detail('screen_ocr', {}) == (
        'run local OCR on the latest screenshot (lang=zh-Hans-CN)')
    assert detail('screen_ocr', {'path': 'C:/x/a.png', 'lang': 'en-US',
                                 'region': '1,2,3,4'}) == (
        'run local OCR on C:/x/a.png (lang=en-US, region 1,2,3,4)')


def test_clipboard_tools_render_without_dumping_the_whole_text():
    assert detail('clipboard_read', {}) == 'read the current local clipboard text'
    assert detail('clipboard_write', {'text': 'hello\nworld'}) == (
        'replace the local clipboard with 11 chars — starts with: hello world')
    assert detail('clipboard_write', {'text': ''}) == (
        'replace the local clipboard with 0 chars')


def test_desktop_tools_render_as_readable_actions():
    assert detail('desktop_snapshot', {}) == (
        'inspect foreground window metadata and focused UI control')
    assert detail('desktop_action', {'action': 'click', 'expected_pid': 7,
                                     'expected_window_id': 'AB', 'x': 1, 'y': 2}) == (
        'desktop input: action=click, foreground pid=7, window id=AB, '
        'coordinates=(1,2), button=left')
    assert detail('desktop_action', {
        'action': 'click', 'expected_pid': 7, 'expected_window_id': 'AB',
        'x': 1, 'y': 2, 'button': 'right', 'count': 2}).endswith(
            'coordinates=(1,2), button=right, count=2')
    assert detail('desktop_action', {
        'action': 'key', 'expected_pid': 7, 'expected_window_id': 'AB',
        'key': 'ctrl+v'}).endswith('key=ctrl+v')


def test_desktop_sequence_detail_lists_every_step():
    text = detail('desktop_action', {
        'action': 'sequence', 'expected_pid': 7, 'expected_window_id': 'AB',
        'steps': [{'action': 'click', 'x': 10, 'y': 20},
                  {'action': 'type', 'text': '你好 world'},
                  {'action': 'key', 'key': 'ctrl+enter'},
                  {'action': 'click', 'x': 3, 'y': 4, 'button': 'right'},
                  {'action': 'click', 'x': 5, 'y': 6, 'count': 2},
                  {'action': 'wait', 'ms': 300}]})
    assert 'action=sequence, foreground pid=7, window id=AB' in text
    assert 'steps (6)=1.click(10,20)' in text
    assert '2.type len=8 "你好 world"' in text
    assert '3.key=ctrl+enter' in text
    assert '4.right click(3,4)' in text
    assert '5.double click(5,6)' in text
    assert '6.wait 300ms' in text
