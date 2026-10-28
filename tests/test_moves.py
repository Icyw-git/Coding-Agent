"""`move_paths` / `undo_moves`：安全的移动与重命名。

为什么需要它：`delete_paths` 走回收站、可撤销、逐次审批，而 `mv`/`move`/`ren`
全在 bash 里、**不可撤销**。更常见的操作（移动）保护比更危险的（删除）更弱——
这个倒挂是真的：一次 `mv` 手误会直接丢文件。
"""
from __future__ import annotations

import json
import shutil

import pytest

import moves
import tool_move


@pytest.fixture
def work(tmp_path):
    """一个含若干文件的临时工作目录。"""
    root = tmp_path / 'work'
    root.mkdir()
    for name in ('a.txt', 'b.txt', 'c.txt'):
        (root / name).write_text(f'content {name}', encoding='utf-8')
    (root / 'sub').mkdir()
    (root / 'sub' / 'nested.txt').write_text('nested', encoding='utf-8')
    return root


@pytest.fixture
def cache(tmp_path):
    path = tmp_path / 'cache'
    path.mkdir()
    return path


# ---------------------------------------------------------------- 校验：先全部检查

def test_validate_returns_absolute_paths(work):
    prepared = moves.validate_moves(
        [{'from': str(work / 'a.txt'), 'to': 'a.done.txt'}])
    assert prepared[0]['from'] == (work / 'a.txt').resolve()
    # 相对目标按**源目录**解析，不是 cwd
    assert prepared[0]['to'] == (work / 'a.done.txt').resolve()


def test_relative_target_resolves_against_source_dir(work):
    """相对路径按源文件所在目录解析——按 cwd 解析会让用户以为移到了别处。"""
    prepared = moves.validate_moves(
        [{'from': str(work / 'a.txt'), 'to': 'sub/a.txt'}])
    assert prepared[0]['to'] == (work / 'sub' / 'a.txt').resolve()


def test_missing_source_is_rejected(work):
    with pytest.raises(moves.MoveError, match='source does not exist'):
        moves.validate_moves([{'from': str(work / 'nope.txt'), 'to': 'x.txt'}])


def test_existing_target_is_rejected(work):
    """目标已存在一律拒绝：覆盖是数据丢失，不做隐式处理。"""
    with pytest.raises(moves.MoveError, match='target already exists'):
        moves.validate_moves([{'from': str(work / 'a.txt'),
                               'to': str(work / 'b.txt')}])


def test_move_into_itself_is_rejected(work):
    with pytest.raises(moves.MoveError, match='into itself'):
        moves.validate_moves([{'from': str(work / 'sub'),
                               'to': str(work / 'sub' / 'inner')}])


def test_same_source_and_target_is_rejected(work):
    with pytest.raises(moves.MoveError, match='same as source'):
        moves.validate_moves([{'from': str(work / 'a.txt'),
                               'to': str(work / 'a.txt')}])


def test_missing_fields_are_rejected(work):
    with pytest.raises(moves.MoveError, match='needs both from and to'):
        moves.validate_moves([{'from': str(work / 'a.txt')}])


def test_duplicate_sources_are_rejected(work):
    with pytest.raises(moves.MoveError, match='duplicate source'):
        moves.validate_moves([
            {'from': str(work / 'a.txt'), 'to': 'x.txt'},
            {'from': str(work / 'a.txt'), 'to': 'y.txt'}])


def test_duplicate_targets_are_rejected(work):
    with pytest.raises(moves.MoveError, match='duplicate target'):
        moves.validate_moves([
            {'from': str(work / 'a.txt'), 'to': 'same.txt'},
            {'from': str(work / 'b.txt'), 'to': 'same.txt'}])


def test_batch_size_is_capped(work):
    many = [{'from': str(work / 'a.txt'), 'to': f'x{i}.txt'} for i in range(51)]
    with pytest.raises(moves.MoveError, match='too many items'):
        moves.validate_moves(many)


def test_empty_batch_is_rejected():
    with pytest.raises(moves.MoveError, match='non-empty list'):
        moves.validate_moves([])


def test_single_dict_is_accepted_as_one_item(work):
    prepared = moves.validate_moves(
        {'from': str(work / 'a.txt'), 'to': 'a.done.txt'})
    assert len(prepared) == 1


def test_one_bad_item_rejects_the_whole_batch(work):
    """批量移动执行到一半失败会留下半完成状态，比干脆失败更糟。"""
    with pytest.raises(moves.MoveError):
        moves.validate_moves([
            {'from': str(work / 'a.txt'), 'to': 'ok.txt'},
            {'from': str(work / 'missing.txt'), 'to': 'x.txt'}])
    # 确认一项都没动
    assert (work / 'a.txt').is_file()
    assert not (work / 'ok.txt').exists()


# ---------------------------------------------------------------- 执行与撤销

def test_move_performs_and_journals(work, cache):
    result = moves.move([{'from': str(work / 'a.txt'), 'to': 'a.done.txt'}],
                        cache_dir=cache)
    assert (work / 'a.done.txt').is_file()
    assert not (work / 'a.txt').exists()
    assert result['moved']
    journal = cache / moves.UNDO_NAME
    assert journal.is_file()
    row = json.loads(journal.read_text(encoding='utf-8').strip())
    assert row['id'] == result['journal_id']


def test_move_creates_missing_parent_dirs(work, cache):
    moves.move([{'from': str(work / 'a.txt'), 'to': str(work / 'new' / 'deep' / 'a.txt')}],
               cache_dir=cache)
    assert (work / 'new' / 'deep' / 'a.txt').is_file()


def test_undo_restores_everything(work, cache):
    moves.move([{'from': str(work / 'a.txt'), 'to': 'a.done.txt'},
                {'from': str(work / 'b.txt'), 'to': 'b.done.txt'}],
               cache_dir=cache)
    result = moves.undo(cache)
    assert len(result['restored']) == 2
    assert result['skipped'] == []
    assert (work / 'a.txt').is_file() and (work / 'b.txt').is_file()
    assert not (work / 'a.done.txt').exists()


def test_undo_never_overwrites(work, cache):
    """撤销也不该造成数据丢失：原位被占用就跳过并报告。"""
    moves.move([{'from': str(work / 'a.txt'), 'to': 'a.done.txt'}],
               cache_dir=cache)
    (work / 'a.txt').write_text('someone else wrote this', encoding='utf-8')
    result = moves.undo(cache)
    assert result['restored'] == []
    assert result['skipped'][0]['reason'] == 'original location is occupied'
    assert (work / 'a.txt').read_text(encoding='utf-8') == 'someone else wrote this'


def test_undo_reports_missing_moved_file(work, cache):
    moves.move([{'from': str(work / 'a.txt'), 'to': 'a.done.txt'}],
               cache_dir=cache)
    (work / 'a.done.txt').unlink()
    result = moves.undo(cache)
    assert result['skipped'][0]['reason'] == 'moved file is gone'


def test_undo_specific_batch(work, cache):
    first = moves.move([{'from': str(work / 'a.txt'), 'to': 'a.1.txt'}],
                       cache_dir=cache)
    moves.move([{'from': str(work / 'b.txt'), 'to': 'b.1.txt'}], cache_dir=cache)
    result = moves.undo(cache, batch_id=first['journal_id'])
    assert result['batch'] == first['journal_id']
    assert (work / 'a.txt').is_file()
    # 第二批没被撤销
    assert (work / 'b.1.txt').is_file()


def test_undo_without_history_is_an_error(cache):
    with pytest.raises(moves.MoveError, match='no move batches'):
        moves.undo(cache)


def test_undo_unknown_batch_is_an_error(work, cache):
    moves.move([{'from': str(work / 'a.txt'), 'to': 'a.done.txt'}], cache_dir=cache)
    with pytest.raises(moves.MoveError, match='no move batch with id'):
        moves.undo(cache, batch_id='ghost')


def test_recent_batches_newest_first(work, cache):
    first = moves.move([{'from': str(work / 'a.txt'), 'to': 'a.1.txt'}],
                       cache_dir=cache)
    second = moves.move([{'from': str(work / 'b.txt'), 'to': 'b.1.txt'}],
                        cache_dir=cache)
    rows = moves.recent_batches(cache)
    assert [row['id'] for row in rows] == [second['journal_id'], first['journal_id']]


# ---------------------------------------------------------------- 工具层文案

def test_dry_run_is_default_and_changes_nothing(work, cache):
    out = tool_move.move([{'from': str(work / 'a.txt'), 'to': 'a.done.txt'}],
                         cache_dir=cache)
    assert 'Preview' in out
    assert 'nothing changed yet' in out
    assert 'dry_run=false' in out          # 给出下一步
    assert (work / 'a.txt').is_file()


def test_real_move_mentions_undo_id(work, cache):
    out = tool_move.move([{'from': str(work / 'a.txt'), 'to': 'a.done.txt'}],
                         cache_dir=cache, dry_run=False)
    assert 'Moved 1 item(s)' in out
    assert 'undo_moves' in out


def test_validation_error_is_reported_not_raised(work, cache):
    out = tool_move.move([{'from': str(work / 'nope'), 'to': 'x'}], cache_dir=cache)
    assert out.startswith('Error:')
    assert 'source does not exist' in out


# ---------------------------------------------------------------- 策略层

def test_dry_run_is_allowed_without_approval():
    """预览不改磁盘，不该逼用户先批一次。"""
    from exec_policy import ALLOW, Policy
    policy = Policy()
    decision = policy.evaluate('move_paths', {'moves': [], 'dry_run': True})
    assert decision.action == ALLOW


def test_dry_run_default_value_is_allowed():
    from exec_policy import ALLOW, Policy
    decision = Policy().evaluate('move_paths', {'moves': []})
    assert decision.action == ALLOW


def test_real_move_requires_approval():
    from exec_policy import ASK, Policy
    decision = Policy().evaluate('move_paths',
                                 {'moves': [], 'dry_run': False})
    assert decision.action == ASK


def test_undo_requires_approval():
    from exec_policy import ASK, Policy
    assert Policy().evaluate('undo_moves', {'batch_id': ''}).action == ASK


# ---------------------------------------------------------------- 不覆盖

def test_move_does_not_overwrite_existing_target(work, cache):
    """即使绕过校验直接调 move，也不该覆盖已有文件。"""
    (work / 'target.txt').write_text('keep me', encoding='utf-8')
    with pytest.raises(moves.MoveError, match='target already exists'):
        moves.move([{'from': str(work / 'a.txt'), 'to': str(work / 'target.txt')}],
                   cache_dir=cache)
    assert (work / 'target.txt').read_text(encoding='utf-8') == 'keep me'
    assert (work / 'a.txt').is_file()


def test_move_directory_works(work, cache):
    moves.move([{'from': str(work / 'sub'), 'to': str(work / 'renamed')}],
               cache_dir=cache)
    assert (work / 'renamed' / 'nested.txt').is_file()
    moves.undo(cache)
    assert (work / 'sub' / 'nested.txt').is_file()


def test_library_is_importable_without_side_effects():
    """moves 是纯逻辑模块：导入不该碰文件系统。"""
    assert callable(moves.validate_moves)
    assert callable(shutil.move)


# ---------------------------------------------------------------- 预览可读性

def test_preview_shortens_paths_to_common_base(work, cache):
    """满屏绝对路径前缀会把预览变成不可核对——真实文件名被挤到行尾。"""
    out = tool_move.move([{'from': str(work / 'a.txt'), 'to': str(work / 'arch' / 'a.txt')},
                          {'from': str(work / 'b.txt'), 'to': str(work / 'arch' / 'b.txt')}],
                         cache_dir=cache)
    # 公共目录单独一行，条目只显示相对部分
    assert f'in {work}' in out
    assert 'a.txt -> arch/a.txt' in out
    assert 'b.txt -> arch/b.txt' in out


def test_preview_falls_back_to_absolute_without_common_base(tmp_path, cache):
    """跨盘符/无公共目录时退回绝对路径，不能报错。"""
    left = tmp_path / 'left'
    left.mkdir()
    (left / 'a.txt').write_text('x', encoding='utf-8')
    other = tmp_path / 'right'
    other.mkdir()
    prepared = [{'from': left / 'a.txt', 'to': other / 'a.txt'}]
    text = moves.describe(prepared)
    assert 'left' in text and 'right' in text


def test_error_says_nothing_was_moved(work, cache):
    """用户看到错误第一反应是"移了一半没有"——必须直接回答。"""
    out = tool_move.move([{'from': str(work / 'a.txt'), 'to': 'ok.txt'},
                          {'from': str(work / 'missing.txt'), 'to': 'x.txt'}],
                         cache_dir=cache, dry_run=False)
    assert 'Nothing was moved' in out
    assert 'whole batch is checked' in out
    assert (work / 'a.txt').is_file()


def test_undo_without_history_gives_next_step(cache):
    out = tool_move.undo(cache)
    assert 'nothing to undo' in out
    assert 'move_paths' in out


def test_undo_unknown_batch_lists_recent(work, cache):
    batch = moves.move([{'from': str(work / 'a.txt'), 'to': 'a.done.txt'}],
                       cache_dir=cache)
    out = tool_move.undo(cache, batch_id='ghost')
    assert 'no move batch with id ghost' in out
    assert batch['journal_id'] in out      # 列出最近的，便于抄对 id
