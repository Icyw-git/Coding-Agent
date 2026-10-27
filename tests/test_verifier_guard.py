"""评估器保护（B1）：tests / CI / lint 配置只能人自己改。

为什么这条比其他安全规则更本质：我们判断"改进了没有"的唯一外部信号是
`pytest 全绿`。一旦 agent 能改测试，这个信号就不再是证据——它可以删失败用例、
放宽断言，然后"证明"自己变好了。这是 RSI 领域公认的第一条红线：

    The moment the system owns the ruler, it will get very good at *looking*
    improved.
"""
from __future__ import annotations

import pytest

import verifier_guard as vg

# ---------------------------------------------------------------- 路径判定

@pytest.mark.parametrize('path', [
    'tests/test_memory.py',
    'tests/anything.py',              # 目录内任何文件都算
    'C:\\repo\\tests\\a.py',          # Windows 盘符 + 反斜杠
    'D:/harness-agent/tests/x.py',
    '.github/workflows/ci.yml',
    'conftest.py',
    'sub/conftest.py',                # 任意层级的 conftest
    'pytest.ini',
    'ruff.toml',
    'tox.ini',
    'test_top.py',                    # 仓库根的 test_*.py
])
def test_protected_paths(path):
    assert vg.is_protected(path), path


@pytest.mark.parametrize('path', [
    'src/memory.py',
    'README.md',
    'docs/tests.md',                  # 只是文档里提到 tests
    'skills/deploy.md',
    'src/test_helpers.py',            # 深层同名但不在测试目录
    'src/pytest_like.py',
    '',
])
def test_allowed_paths(path):
    assert not vg.is_protected(path), path


def test_all_test_directories_count():
    """任何 tests/ 目录都算评估面，不限于 Python。"""
    assert vg.is_protected('visualizer/tests/app.spec.js')


# ---------------------------------------------------------------- 只拦写

@pytest.mark.parametrize('tool', ['write', 'edit', 'apply_patch', 'delete_paths'])
def test_write_tools_are_checked(tool):
    args = {'path': 'tests/test_x.py', 'paths': ['tests/test_x.py']}
    assert vg.check(tool, args)


@pytest.mark.parametrize('tool', ['read', 'grep', 'ls', 'glob', 'bash'])
def test_read_tools_are_not_checked(tool):
    """读测试是正常需求：agent 需要看断言才能理解期望行为。"""
    assert vg.check(tool, {'path': 'tests/test_memory.py'}) == ''


def test_apply_patch_paths_are_parsed():
    patch = ('*** Begin Patch\n'
             '*** Update File: tests/test_x.py\n'
             '@@\n-old\n+new\n'
             '*** End Patch\n')
    assert vg.check('apply_patch', {'patch': patch})


def test_apply_patch_on_source_is_allowed():
    patch = '*** Update File: src/memory.py\n@@\n-old\n+new\n'
    assert vg.check('apply_patch', {'patch': patch}) == ''


def test_delete_paths_list_is_checked():
    assert vg.check('delete_paths', {'paths': ['tests/a.py', 'src/b.py']})
    assert vg.check('delete_paths', {'paths': ['src/b.py']}) == ''


def test_clean_args_pass():
    assert vg.check('write', {'path': 'src/new_module.py', 'content': 'x'}) == ''
    assert vg.check('write', {}) == ''
    assert vg.check('write', None) == ''


# ---------------------------------------------------------------- 拒绝语文案

def test_refusal_explains_the_reason():
    """必须说清「不是审批问题」，否则模型会换个工具重试。"""
    text = vg.check('write', {'path': 'tests/a.py'})
    assert 'not an approval question' in text
    assert 'evidence' in text
    assert 'changed by the user personally' in text


def test_incident_switch(monkeypatch):
    monkeypatch.setenv(vg.ENV, '0')
    assert vg.check('write', {'path': 'tests/a.py'}) == ''


def test_enabled_by_default():
    assert vg.enabled()


# ---------------------------------------------------------------- 接入 enforce

class _PermissiveGate:
    """最宽松的组合：全放行档位 + 白名单命中 + 审批都同意。"""

    timeout = 1

    def __init__(self):
        self.asked = 0

    def decide(self, *_a, **_k):
        self.asked += 1
        return True, 'approved'

    def decide_queued(self, *_a, **_k):
        self.asked += 1
        return True, 'approved'

    def session_allows(self, *_a, **_k):
        return True


@pytest.fixture
def permissive(monkeypatch):
    import hooks
    monkeypatch.setenv('HARNESS_SANDBOX', 'danger-full-access')
    monkeypatch.setenv('HARNESS_APPROVAL', 'never')
    gate = _PermissiveGate()
    monkeypatch.setattr(hooks, 'GATE', gate)
    return hooks, gate


def test_enforce_blocks_test_edits_even_when_everything_is_permissive(permissive):
    """核心断言：评估器保护不看档位、不问人。"""
    hooks, gate = permissive
    out = hooks.enforce('write', {'path': 'tests/test_x.py', 'content': 'x'},
                        interactive=True)
    assert out and 'Refused' in out
    assert gate.asked == 0


def test_enforce_blocks_ci_edits(permissive):
    hooks, _gate = permissive
    out = hooks.enforce('edit', {'path': '.github/workflows/ci.yml',
                                 'oldText': 'a', 'newText': 'b'},
                        interactive=True)
    assert out and 'Refused' in out


def test_enforce_allows_reading_tests(permissive):
    hooks, _gate = permissive
    assert hooks.enforce('read', {'path': 'tests/test_memory.py'},
                         interactive=True) is None


def test_enforce_allows_source_edits(permissive):
    hooks, _gate = permissive
    assert hooks.enforce('write', {'path': 'src/memory.py', 'content': 'x'},
                         interactive=True) is None


def test_background_turn_is_also_blocked(permissive):
    """后台回合不能因为没人看着就放宽。"""
    hooks, _gate = permissive
    out = hooks.enforce('write', {'path': 'tests/a.py', 'content': 'x'},
                        interactive=False)
    assert out and 'Refused' in out
