"""Ring 1 的守卫：agent 自己的指令（AGENTS.md / CLAUDE.md / skills）改动要审批。

为什么单独一个文件
------------------
RSI 的设计把可改范围分三环：Ring 0（测试/CI/lint，硬拒）、Ring 1（skills 与
AGENTS.md，需审批）、Ring 2（其余，自由）。实测发现 **Ring 1 只存在于文档和
`self_improve.evaluate()` 的事后判定里**——写入时不拦，事后 discard 那一轮，
而文件已经改了。

这几条测试钉住"写入时要审批、读取不受影响、普通文件不误伤"。
"""
from __future__ import annotations

import pytest

import harness
import hooks


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    """把工作区钉到 tmp：判定按 harness.get_workdir() 算分区。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    monkeypatch.delenv('HARNESS_SANDBOX_MODE', raising=False)
    monkeypatch.delenv('HARNESS_APPROVAL_POLICY', raising=False)
    return tmp_path


def _decide(tool, args):
    return hooks.evaluate_call(tool, args)


@pytest.mark.parametrize('relative', [
    'AGENTS.md', 'AGENTS.override.md', 'CLAUDE.md',
    'skills/demo.md', 'skills/nested/deep.md',
])
def test_self_modification_needs_approval(_workspace, relative):
    """改 agent 自己的指令/技能 = 改"它怎么做事的依据"，必须问。"""
    target = _workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)

    decision = _decide('write', {'path': str(target), 'content': 'x'})

    assert decision.action == 'ask', (relative, decision.action)
    assert decision.code == 'self_modification'


@pytest.mark.parametrize('relative', ['AGENTS.md', 'skills/demo.md'])
def test_reading_instructions_stays_allowed(_workspace, relative):
    """读必须放行：agent 要先看懂自己的指令，才谈得上遵守。"""
    target = _workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('x', encoding='utf-8')

    assert _decide('read', {'path': str(target)}).action == 'allow'


@pytest.mark.parametrize('relative', ['src/app.py', 'docs/note.md', 'notes.md'])
def test_ordinary_workspace_files_stay_allowed(_workspace, relative):
    """别误伤：普通源码与文档不因为这条规则开始要审批。"""
    target = _workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)

    assert _decide('write', {'path': str(target), 'content': 'x'}).action == 'allow'


def test_skills_outside_the_workspace_are_still_outside_writes(_workspace):
    """工作区外的 skills 目录按"越界写"处理，不该被这条规则盖住。"""
    outside = _workspace.parent / 'elsewhere' / 'skills' / 'x.md'
    outside.parent.mkdir(parents=True, exist_ok=True)

    decision = _decide('write', {'path': str(outside), 'content': 'x'})

    assert decision.code == 'outside_workspace_write'


def test_self_modification_still_applies_in_read_only_mode(_workspace, monkeypatch):
    """只读档本来就全要审批，但原因要说对：是"改自己的指令"，不是"档位只读"。"""
    monkeypatch.setenv('HARNESS_SANDBOX_MODE', 'read-only')
    target = _workspace / 'AGENTS.md'
    target.write_text('x', encoding='utf-8')

    decision = _decide('write', {'path': str(target), 'content': 'x'})

    assert decision.action == 'ask'
    assert decision.code == 'self_modification'


def test_self_modification_has_a_chinese_reason_for_the_card():
    """审批卡要给人看得懂的理由（code → 中文），和别的决策码一样。"""
    import approval_view

    text = approval_view.reason_zh('self_modification')
    assert '指令' in text or '技能' in text
    text.encode('gbk')
