"""终端审批提示要和飞书卡片说同一件事。

同一个 `PermissionRequest`，两处渲染：

    飞书（feishu_approval_card）  reason_zh(code, reason) + tool_label(tool)
    终端（approval._decide_console） 直接打印 req.reason + req.tool

而 `req.reason` 是**写给模型的英文**
（"desktop_action inspects or controls the local foreground desktop; each
operation requires explicit approval"），`req.tool` 是原始标识符。
`PermissionRequest.code` 的注释本来就是这么说的：「策略决策码：审批面按它给中文
解释（reason 是英文）」——终端这条路没照做。
"""

import approval
import approval_view


def _render(monkeypatch, capsys, request):
    gate = approval.PermissionGate()
    gate.resolve = lambda *a, **k: None
    monkeypatch.setattr('builtins.input', lambda *a, **k: 'n')
    gate._decide_console(request)
    return capsys.readouterr().out


def _prompt_text(monkeypatch, request) -> str:
    """input() 的提示语不经过 stdout，要直接记它被怎么调用。"""
    gate = approval.PermissionGate()
    gate.resolve = lambda *a, **k: None
    seen = []

    def fake_input(prompt=''):
        seen.append(prompt)
        return 'n'

    monkeypatch.setattr('builtins.input', fake_input)
    gate._decide_console(request)
    return ' '.join(seen)


def _request(**extra):
    base = dict(
        req_id='r1', tool='desktop_action',
        reason='desktop_action inspects or controls the local foreground '
               'desktop; each operation requires explicit approval',
        created_at=0.0, args={'action': 'click', 'x': 10, 'y': 20},
        code='desktop_control')
    base.update(extra)
    return approval.PermissionRequest(**base)


def test_reason_is_chinese(monkeypatch, capsys):
    out = _render(monkeypatch, capsys, _request())
    assert approval_view.reason_zh('desktop_control', '') in out, out
    assert 'inspects or controls the local foreground desktop' not in out, out


def test_tool_name_is_chinese(monkeypatch, capsys):
    out = _render(monkeypatch, capsys, _request())
    assert '桌面操作' in out or '操作键鼠' in out, out
    assert '操作: desktop_action' not in out, out


def test_prompt_is_chinese(monkeypatch):
    text = _prompt_text(monkeypatch, _request())
    assert 'Allow?' not in text, text
    assert '允许吗' in text, text
    assert 'y=' in text and 'n=' in text, text


def test_unknown_code_falls_back_to_the_english_reason(monkeypatch, capsys):
    """没有中文解释时宁可显示英文，也不能什么都不说。"""
    out = _render(monkeypatch, capsys, _request(code='', reason='some policy said no'))
    assert 'some policy said no' in out, out
