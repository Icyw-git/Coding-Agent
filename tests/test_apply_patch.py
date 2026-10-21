"""apply_patch：Codex 风格补丁的解析、原子应用与策略接入。"""
import pytest

import hooks
import tools


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv('HARNESS_SANDBOX_MODE', 'danger-full-access')
    hooks.GATE.clear()
    hooks._GRANTS.set(frozenset())


def _patch(*lines):
    return '\n'.join(lines)


def test_apply_patch_add_update_delete_move(tmp_path):
    (tmp_path / 'greet.py').write_text("def greet():\n    return 'hi'\n", encoding='utf-8')
    (tmp_path / 'old.txt').write_text('obsolete\n', encoding='utf-8')
    (tmp_path / 'trash.txt').write_text('bye\n', encoding='utf-8')

    result = tools.run_apply_patch(_patch(
        '*** Begin Patch',
        '*** Add File: pkg/new.py',
        '+import os',
        '+',
        '+print(os.name)',
        '*** Update File: greet.py',
        '@@ def greet():',
        "-    return 'hi'",
        "+    return 'hello'",
        '*** Update File: old.txt',
        '*** Move to: archived/old.txt',
        '*** Delete File: trash.txt',
        '*** End Patch',
    ), cwd=str(tmp_path))

    assert result.startswith('Applied patch:')
    assert (tmp_path / 'greet.py').read_text(encoding='utf-8') == (
        "def greet():\n    return 'hello'\n")
    assert (tmp_path / 'pkg/new.py').read_text(encoding='utf-8') == (
        'import os\n\nprint(os.name)\n')
    assert not (tmp_path / 'old.txt').exists()
    assert (tmp_path / 'archived/old.txt').read_text(encoding='utf-8') == 'obsolete\n'
    assert not (tmp_path / 'trash.txt').exists()


def test_apply_patch_ambiguous_context_is_rejected(tmp_path):
    target = tmp_path / 'dup.txt'
    target.write_text('same\nsame\n', encoding='utf-8')

    result = tools.run_apply_patch(_patch(
        '*** Begin Patch',
        '*** Update File: dup.txt',
        '@@',
        '-same',
        '+changed',
        '*** End Patch',
    ), cwd=str(tmp_path))

    assert 'ambiguous' in result
    assert target.read_text(encoding='utf-8') == 'same\nsame\n'


def test_apply_patch_is_atomic_when_one_file_is_missing(tmp_path):
    target = tmp_path / 'a.txt'
    target.write_text('one\n', encoding='utf-8')

    result = tools.run_apply_patch(_patch(
        '*** Begin Patch',
        '*** Update File: a.txt',
        '@@',
        '-one',
        '+two',
        '*** Update File: missing.txt',
        '@@',
        '-x',
        '+y',
        '*** End Patch',
    ), cwd=str(tmp_path))

    assert 'not applied' in result
    assert target.read_text(encoding='utf-8') == 'one\n'


def test_apply_patch_move_rejects_existing_target(tmp_path):
    (tmp_path / 'src.txt').write_text('x\n', encoding='utf-8')
    (tmp_path / 'dst.txt').write_text('y\n', encoding='utf-8')

    result = tools.run_apply_patch(_patch(
        '*** Begin Patch',
        '*** Update File: src.txt',
        '*** Move to: dst.txt',
        '*** End Patch',
    ), cwd=str(tmp_path))

    assert 'already exists' in result
    assert (tmp_path / 'src.txt').read_text(encoding='utf-8') == 'x\n'
    assert (tmp_path / 'dst.txt').read_text(encoding='utf-8') == 'y\n'


def test_apply_patch_preserves_encoding_and_crlf(tmp_path):
    target = tmp_path / 'gbk.txt'
    target.write_bytes('第一行\r\n第二行\r\n'.encode('gbk'))

    result = tools.run_apply_patch(_patch(
        '*** Begin Patch',
        '*** Update File: gbk.txt',
        '@@',
        ' 第一行',
        '-第二行',
        '+第二行（改）',
        '*** End Patch',
    ), cwd=str(tmp_path))

    assert result.startswith('Applied patch:')
    assert target.read_bytes() == '第一行\r\n第二行（改）\r\n'.encode('gbk')


def test_apply_patch_refuses_add_over_existing_file(tmp_path):
    target = tmp_path / 'keep.txt'
    target.write_text('keep\n', encoding='utf-8')

    result = tools.run_apply_patch(_patch(
        '*** Begin Patch',
        '*** Add File: keep.txt',
        '+replace',
        '*** End Patch',
    ), cwd=str(tmp_path))

    assert 'already exists' in result
    assert target.read_text(encoding='utf-8') == 'keep\n'


def test_apply_patch_rejects_secret_paths(tmp_path):
    secret = tmp_path / '.env'
    secret.write_text('TOKEN=old\n', encoding='utf-8')

    result = tools.run_apply_patch(_patch(
        '*** Begin Patch',
        '*** Update File: .env',
        '@@',
        '-TOKEN=old',
        '+TOKEN=new',
        '*** End Patch',
    ), cwd=str(tmp_path))

    assert result.startswith('Error:')
    assert 'protected' in result
    assert secret.read_text(encoding='utf-8') == 'TOKEN=old\n'


@pytest.mark.parametrize('patch_text', [
    '',
    '*** Begin Patch\n*** Update File: a.txt\n',
    '*** Begin Patch\n*** Frobnicate File: a.txt\n*** End Patch',
    '*** Begin Patch\n*** Add File: new.txt\nnot prefixed\n*** End Patch',
])
def test_apply_patch_rejects_malformed_patches(tmp_path, patch_text):
    result = tools.run_apply_patch(patch_text, cwd=str(tmp_path))
    assert result.startswith('Error: invalid patch:')
    assert not (tmp_path / 'new.txt').exists()


def test_patch_paths_includes_move_target():
    paths = tools.patch_paths(_patch(
        '*** Begin Patch',
        '*** Update File: a.txt',
        '*** Move to: b.txt',
        '*** End Patch',
    ))
    assert paths == ['a.txt', 'b.txt']


def test_evaluate_call_denies_unparseable_patch():
    decision = hooks.evaluate_call('apply_patch', {'patch': 'not a patch'})
    assert decision.denied
    assert decision.code == 'invalid_patch'


def test_evaluate_call_uses_strictest_path_zone(tmp_path, monkeypatch):
    monkeypatch.setenv('HARNESS_SANDBOX_MODE', 'workspace-write')
    patch = _patch(
        '*** Begin Patch',
        '*** Update File: README.md',
        '@@',
        '-old',
        '+new',
        '*** Add File: {}',
        '+outside',
        '*** End Patch',
    ).format(tmp_path / 'outside.txt')

    decision = hooks.evaluate_call('apply_patch', {'patch': patch})
    assert decision.action == 'ask'
    assert decision.code == 'outside_workspace_write'
    assert hooks.approval_class('apply_patch', {'patch': patch}) == ''
