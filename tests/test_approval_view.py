"""审批面渲染（approval_view）：命令看得全、不可见字符不反过来改写审批界面。

展示位有四个：CLI 提示（hooks._decide_console）、/approvals（harness）、审批卡片
（feishu_gateway）、/perms（gateway_sim）。它们共用 summarize/detail，所以这里的
用例锁的是「同一份可读内容」，而不是某个 print 的字面排版。
"""

import approval_view
from approval_view import detail, safe_text, summarize, tool_label
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
    # 写入内容现在留头尾（而不是只留头 200 字）：审批人要能看出文件末尾
    # 写了什么。上限放宽到 600 字，仍远小于飞书 30KB 卡片上限。
    content = summarize('write', {'path': 'a.txt', 'content': 'x' * 5000})['content']
    assert '省略' in content
    assert len(content) < 700


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
    assert shown.splitlines() == ['del x', '工作目录：D:/tmp']


def test_non_bash_tools_render_as_labelled_pairs():
    # 通用兜底也要给人话标签：`name=feat` 对用户等于没写。
    assert detail('remove_worktree', {'name': 'feat'}) == '名称：feat'
    assert detail('write', {'path': 'a.txt', 'content': None}) == 'a.txt（无内容摘要）'
    assert detail('bash', {}) == ''


def test_safe_text_accepts_non_strings():
    assert safe_text(42) == '42'


def test_screen_tools_render_as_readable_actions():
    assert detail('screen_capture', {'screen': 'all'}) == '截取本机全部屏幕，整屏'
    assert detail('screen_capture', {'region': '0,0,100,50', 'delay_ms': 250}) == (
        '截取本机主屏，区域 0,0,100,50，等 250 毫秒后截')
    assert detail('screen_ocr', {}).startswith('识别截图文字：最近一张截图')
    assert detail('screen_ocr', {'path': 'C:/x/a.png', 'lang': 'en-US',
                                 'region': '1,2,3,4'}).startswith(
        '识别截图文字：C:/x/a.png（语言 en-US，区域 1,2,3,4）')


def test_clipboard_tools_render_without_dumping_the_whole_text():
    assert detail('clipboard_read', {}) == '读取本机剪贴板当前内容'
    assert detail('clipboard_write', {'text': 'hello' + chr(10) + 'world'}) == (
        '用 11 个字符覆盖本机剪贴板（会冲掉你现在复制的内容），开头是：hello world')
    assert detail('clipboard_write', {'text': ''}).startswith(
        '用 0 个字符覆盖本机剪贴板')


def test_desktop_tools_render_as_readable_actions():
    assert detail('desktop_snapshot', {}) == '查看前台窗口信息与当前焦点控件（只读）'
    assert detail('desktop_action', {'action': 'click', 'expected_pid': 7,
                                     'expected_window_id': 'AB', 'x': 1, 'y': 2}) == (
        '桌面操作：点击，目标进程 7，目标窗口 AB，坐标 (1,2)，左键单击')
    assert detail('desktop_action', {
        'action': 'click', 'expected_pid': 7, 'expected_window_id': 'AB',
        'x': 1, 'y': 2, 'button': 'right', 'count': 2}).endswith(
            '坐标 (1,2)，右键双击')
    assert detail('desktop_action', {
        'action': 'key', 'expected_pid': 7, 'expected_window_id': 'AB',
        'key': 'ctrl+v'}).endswith('按键 ctrl+v')


def test_desktop_sequence_detail_lists_every_step():
    text = detail('desktop_action', {
        'action': 'sequence', 'expected_pid': 7, 'expected_window_id': 'AB',
        'steps': [{'action': 'click', 'x': 10, 'y': 20},
                  {'action': 'type', 'text': '你好 world'},
                  {'action': 'key', 'key': 'ctrl+enter'},
                  {'action': 'click', 'x': 3, 'y': 4, 'button': 'right'},
                  {'action': 'click', 'x': 5, 'y': 6, 'count': 2},
                  {'action': 'wait', 'ms': 300}]})
    assert '桌面操作：组合动作，目标进程 7，目标窗口 AB' in text
    assert '依次执行 6 步：' in text
    assert '1. 左键单击 (10,20)' in text
    assert '2. 输入 8 个字符：你好 world' in text
    assert '3. 按键 ctrl+enter' in text
    assert '4. 右键单击 (3,4)' in text
    assert '5. 左键双击 (5,6)' in text
    assert '6. 等 300 毫秒' in text


def test_write_detail_shows_content_preview():
    """越界写的审批要能看出「写了什么」——只给路径没法判断。"""
    text = detail('write', {'path': 'D:/out.txt', 'content': 'hello world'})
    assert 'D:/out.txt' in text
    assert 'hello world' in text
    assert '11 chars' in text


def test_write_detail_truncates_large_content_keeping_both_ends():
    """长内容要留头尾：只看开头会把「文件末尾写了什么」整段切掉。"""
    content = 'A' * 300 + 'B' * 3000 + 'C' * 300
    text = detail('write', {'path': 'D:/big.txt', 'content': content})
    assert '省略' in text
    assert 'A' * 20 in text
    assert 'C' * 20 in text
    assert len(text) < 1000          # 不能把整段内容塞进卡片


def test_edit_detail_shows_a_diff():
    text = detail('edit', {'path': 'D:/a.py',
                           'old_text': 'a = 1', 'new_text': 'a = 2'})
    assert 'D:/a.py' in text
    assert '- a = 1' in text
    assert '+ a = 2' in text


def test_edit_detail_marks_replace_all():
    text = detail('edit', {'path': 'D:/a.py', 'old_text': 'x',
                           'new_text': 'y', 'replace_all': True})
    assert '全部替换' in text


def test_summarize_keeps_both_ends_for_writes():
    """登记时的摘要也不能只留头部——detail 收到的就是它。"""
    summary = summarize('write', {'path': 'D:/o.txt',
                                  'content': 'A' * 300 + 'B' * 3000 + 'C' * 300})
    stored = summary['content']
    assert '省略' in stored
    assert stored.startswith('A')
    assert stored.endswith('C')


def test_summarize_leaves_short_content_alone():
    summary = summarize('write', {'path': 'a.txt', 'content': 'short'})
    assert summary['content'] == 'short'


def test_tool_label_pairs_chinese_with_the_raw_name():
    """卡片写 `desktop_action` 对非技术用户等于没写；只写中文又不好对照日志。"""
    assert tool_label('desktop_action') == '操作键鼠 (desktop_action)'
    assert tool_label('delete_paths') == '删除（进回收站） (delete_paths)'
    assert tool_label('bash') == '执行命令 (bash)'


def test_tool_label_falls_back_to_the_raw_name():
    """没登记的工具原样显示，不能编一个中文名。"""
    assert tool_label('some_mcp_tool') == 'some_mcp_tool'
    assert tool_label('') == '工具'
    assert tool_label(None) == '工具'


def test_approval_card_shows_the_chinese_label():
    import feishu_approval_card as fc

    card = fc.approval_card([{'req_id': 'r', 'tool': 'desktop_action',
                              'args': {}, 'reason': 'why'}])
    assert '操作键鼠' in card['elements'][0]['content']


# ---------------------------------------------------------------- 新工具的审批面

def test_move_paths_has_chinese_label():
    """审批面上写 move_paths 等于没写——用户要在这判断要不要让它动文件。"""
    label = approval_view.tool_label('move_paths')
    assert '移动' in label
    assert 'move_paths' in label          # 原名保留，便于对照日志


def test_move_paths_detail_is_not_raw_dict():
    """绝不能把 args 打成 moves=[{'from': ...}]：用户无法核对。"""
    text = approval_view.detail('move_paths', {'moves': [
        {'from': 'a.pdf', 'to': 'arch/a.pdf'},
        {'from': 'b.pdf', 'to': 'arch/b.pdf'}]})
    assert '移动 2 项' in text
    assert 'a.pdf -> arch/a.pdf' in text
    assert "{'from'" not in text


def test_move_paths_detail_marks_real_move():
    text = approval_view.detail('move_paths', {
        'moves': [{'from': 'a', 'to': 'b'}], 'dry_run': False})
    assert '/undo-move' in text            # 告诉用户怎么撤销


def test_move_paths_detail_caps_long_batches():
    moves = [{'from': f'f{i}', 'to': f'g{i}'} for i in range(30)]
    text = approval_view.detail('move_paths', {'moves': moves})
    assert '还有 10 项' in text


def test_undo_moves_detail():
    assert '17931' in approval_view.detail('undo_moves', {'batch_id': '17931'})
    assert '最近一批' in approval_view.detail('undo_moves', {})


def test_remind_detail_shows_time_and_text():
    text = approval_view.detail('remind', {'text': '交周报', 'when': '明天下午3点'})
    assert '明天下午3点' in text
    assert '交周报' in text


def test_remember_detail_translates_type():
    """type=preference 不如「偏好」直观。"""
    text = approval_view.detail('remember', {'text': '周三不看代码',
                                             'type': 'preference'})
    assert '偏好' in text
    assert '周三不看代码' in text
    assert 'preference' not in text


def test_forget_detail_says_what_is_deleted():
    assert 'deploy' in approval_view.detail('forget', {'name': 'deploy'})


def test_recall_detail():
    assert '部署' in approval_view.detail('recall', {'query': '部署'})
    assert '全部' in approval_view.detail('recall', {})


def test_new_tools_do_not_leak_raw_kwargs():
    """这些工具在审批面上都不能出现 ``key=value`` 形式的原始 kwargs。"""
    cases = {
        'move_paths': {'moves': [{'from': 'a', 'to': 'b'}]},
        'undo_moves': {'batch_id': 'x'},
        'remind': {'text': 't', 'when': 'w'},
        'remember': {'text': 't', 'type': 'fact'},
    }
    for tool, args in cases.items():
        text = approval_view.detail(tool, args)
        assert 'moves=' not in text, tool
        assert 'batch_id=' not in text, tool


def test_element_write_shows_what_will_be_written():
    """写入控件的审批要给出内容，不能只有「写入 N 个字符」。

    和 action=type 用同一套说法：字数 + 内容开头。只给数字等于让人闭着眼睛批。
    """
    text = approval_view.detail('desktop_action', {
        'action': 'element', 'element': 2, 'operation': 'setvalue',
        'expect': '文件名', 'text': '季度报告 2026Q3.docx'})
    assert '「文件名」' in text
    assert '写入 16 个字符' in text
    assert '内容：季度报告 2026Q3.docx' in text
    assert '写入，写入' not in text
    text.encode('gbk')


def test_element_non_write_operations_keep_the_short_verb():
    """没写内容的操作不该被拼成「点击 0 个字符」这种话。"""
    for operation, verb in (('invoke', '点击该控件'), ('toggle', '切换开关')):
        text = approval_view.detail('desktop_action', {
            'action': 'element', 'element': 3, 'operation': operation,
            'expect': '记住我'})
        assert verb in text
        assert '个字符' not in text


def test_detail_accepts_the_display_label_too():
    """`detail()` 按原始工具名分支渲染，但传中文标签也得到同样的结果。

    实测踩到两次：为了显示中文，把 `tool_label()` 的结果传进 detail()，
    于是审批面又变成 ``moves=[{'from': 'a', 'to': 'b'}]``——第二次是飞书审批卡。
    现在入口处把「中文名」「中文名 (原名)」都还原成原名，这个坑从根上堵住，
    不用再靠调用方记得。
    """
    label = approval_view.tool_label('move_paths')      # 移动/重命名文件 (move_paths)
    bare = approval_view._TOOL_LABELS['move_paths']     # 移动/重命名文件
    args = {'moves': [{'from': 'a', 'to': 'b'}]}

    expected = approval_view.detail('move_paths', args)
    assert '移动 1 项' in expected
    assert approval_view.detail(label, args) == expected
    assert approval_view.detail(bare, args) == expected
