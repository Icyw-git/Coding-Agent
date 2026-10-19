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
    # 「本次会话允许同类」的键取命令首词：摘要后仍然是 rm
    assert approval_class('bash', {'command': text}) == 'rm'


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
