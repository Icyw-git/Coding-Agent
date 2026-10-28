"""自我改进的台账、回滚与有界循环（B2/B3）。

核心约束来自 RSI 的三层定义：只做第二层——**系统可以变，尺子不能变**。

> The moment the system owns the ruler, it will get very good at *looking* improved.

所以这里测的重点不是"能不能改进"，而是**边界守不守得住**：
Ring 0（策略/审批/测试）不可改、指标不可自定义、改错能回滚。
"""
from __future__ import annotations

import json

import pytest

import self_improve as si
import self_improve_ledger as ledger

# ---------------------------------------------------------------- Ring 1 边界

@pytest.mark.parametrize('path', ['skills/x.md', 'skills/deep/y.md', 'AGENTS.md'])
def test_ring1_paths_are_editable(path):
    assert si.editable(path, workdir='.')


@pytest.mark.parametrize('path', [
    'tests/test_x.py',
    'src/memory.py',
    'src/hooks.py',
    'conftest.py',
    'pytest.ini',
    'ruff.toml',
    '.github/workflows/ci.yml',
    '../outside.md',
])
def test_ring0_paths_are_not_editable(path):
    """评估面与策略面一律不可自我改进。"""
    assert not si.editable(path, workdir='.')


# ---------------------------------------------------------------- 指标不可自定义

def test_metric_must_come_from_fixed_table(tmp_path):
    """不允许调用方塞新指标——那是"自己抬标准"的入口。"""
    with pytest.raises(ValueError, match='metric must be one of'):
        si.Session(cache_dir=tmp_path, workdir=tmp_path, goal='g',
                   metric='my_custom_metric')


def test_fixed_metrics_mark_hard_gates():
    """测试全绿与 lint 是硬门槛：任何一项变差就 discard。"""
    assert si.METRICS['pytest_pass']['hard'] is True
    assert si.METRICS['ruff_errors']['hard'] is True
    assert si.METRICS['pytest_pass']['direction'] == 'higher'
    assert si.METRICS['ruff_errors']['direction'] == 'lower'


# ---------------------------------------------------------------- 回归检测

def test_regression_in_tests_is_blocking():
    before = {'pytest_pass': {'pass': 1}, 'ruff_errors': {'errors': 0}}
    after = {'pytest_pass': {'pass': 0}, 'ruff_errors': {'errors': 0}}
    assert si.regressed(before, after)


def test_regression_in_lint_is_blocking():
    before = {'pytest_pass': {'pass': 1}, 'ruff_errors': {'errors': 0}}
    after = {'pytest_pass': {'pass': 1}, 'ruff_errors': {'errors': 3}}
    assert si.regressed(before, after)


def test_no_regression_when_equal():
    same = {'pytest_pass': {'pass': 1}, 'ruff_errors': {'errors': 0}}
    assert si.regressed(same, same) == []


def test_unmeasured_is_treated_as_regression():
    """测不出来不能当"没变差"——那是最容易藏问题的地方。"""
    before = {'pytest_pass': {'pass': 1}, 'ruff_errors': {'errors': 0}}
    after = {'pytest_pass': {}, 'ruff_errors': {'errors': 0}}
    assert si.regressed(before, after)


def test_improvement_threshold():
    before = {'ruff_errors': {'errors': 10}}
    assert si.improved(before, {'ruff_errors': {'errors': 5}})
    assert not si.improved(before, {'ruff_errors': {'errors': 10}})
    assert not si.improved(before, {'ruff_errors': {'errors': 9}})


# ---------------------------------------------------------------- 台账

def test_record_and_read(tmp_path):
    (tmp_path / 'skills').mkdir()
    (tmp_path / 'skills' / 'a.md').write_text('v1', encoding='utf-8')
    change_id = ledger.record(tmp_path, paths=['skills/a.md'], summary='改技能',
                              workdir=tmp_path)
    rows = ledger.entries(tmp_path)
    assert [row['change_id'] for row in rows] == [change_id]
    assert rows[0]['summary'] == '改技能'


def test_ledger_is_append_only(tmp_path):
    """记录本身可被改写的话，"改了什么"就不可追溯。"""
    (tmp_path / 'AGENTS.md').write_text('v1', encoding='utf-8')
    first = ledger.record(tmp_path, paths=['AGENTS.md'], summary='一',
                          workdir=tmp_path)
    ledger.update_decision(tmp_path, first, 'keep', 'ok')
    second = ledger.record(tmp_path, paths=['AGENTS.md'], summary='二',
                           workdir=tmp_path)
    path = tmp_path / ledger.LEDGER_NAME
    lines = [line for line in path.read_text(encoding='utf-8').splitlines() if line]
    assert len(lines) == 3          # 2 次记录 + 1 次更新
    rows = ledger.entries(tmp_path)
    assert [row['change_id'] for row in rows] == [first, second]
    assert rows[0]['decision'] == 'keep'


def test_update_does_not_resurrect_missing_record(tmp_path):
    assert not ledger.update_decision(tmp_path, 'ghost', 'keep')


def test_rollback_restores_file(tmp_path):
    (tmp_path / 'skills').mkdir()
    target = tmp_path / 'skills' / 'a.md'
    target.write_text('before', encoding='utf-8')
    change_id = ledger.record(tmp_path, paths=['skills/a.md'], summary='改',
                              workdir=tmp_path)
    target.write_text('after', encoding='utf-8')

    restored = ledger.rollback(tmp_path, change_id)
    assert restored == ['skills/a.md']
    assert target.read_text(encoding='utf-8') == 'before'


def test_rollback_reports_created_files_without_deleting(tmp_path):
    """新增文件没有快照，自动删有风险（可能是别的进程刚写的）。"""
    (tmp_path / 'skills').mkdir()
    new_file = tmp_path / 'skills' / 'new.md'
    change_id = ledger.record(tmp_path, paths=['skills/new.md'], summary='新增',
                              workdir=tmp_path)
    new_file.write_text('fresh', encoding='utf-8')

    assert ledger.rollback(tmp_path, change_id) == []
    assert new_file.is_file()
    assert ledger.created_paths(tmp_path, change_id) == ['skills/new.md']


def test_rollback_unknown_change_is_noop(tmp_path):
    assert ledger.rollback(tmp_path, 'nope') == []


def test_ledger_skips_broken_lines(tmp_path):
    path = tmp_path / ledger.LEDGER_NAME
    path.write_text('{"change_id":"a","ts":1,"paths":[],"summary":"x"}\nnot json\n',
                    encoding='utf-8')
    assert [row['change_id'] for row in ledger.entries(tmp_path)] == ['a']


def test_format_ledger_empty(tmp_path):
    assert '还没有自我改进记录' in ledger.format_ledger(tmp_path)


def test_format_ledger_shows_decision_and_rollback_hint(tmp_path):
    (tmp_path / 'AGENTS.md').write_text('v1', encoding='utf-8')
    change_id = ledger.record(tmp_path, paths=['AGENTS.md'], summary='改约定',
                              workdir=tmp_path)
    ledger.update_decision(tmp_path, change_id, 'keep', '指标变好')
    text = ledger.format_ledger(tmp_path)
    assert '改约定' in text
    assert '✓' in text
    assert change_id in text


def test_report_is_jsonl(tmp_path):
    (tmp_path / 'AGENTS.md').write_text('v1', encoding='utf-8')
    ledger.record(tmp_path, paths=['AGENTS.md'], summary='中文', workdir=tmp_path)
    raw = (tmp_path / ledger.LEDGER_NAME).read_text(encoding='utf-8')
    json.loads(raw.strip().splitlines()[0])


# ---------------------------------------------------------------- 会话与收敛

def test_session_rejects_out_of_scope_paths(tmp_path):
    """判定层再拦一道：测试与策略改动不该进台账。"""
    session = si.Session(cache_dir=tmp_path, workdir='.', goal='g')
    result = session.evaluate(['tests/test_x.py'], '试图改测试')
    assert result['decision'] == 'discard'
    assert 'outside editable scope' in result['reason']
    assert ledger.entries(tmp_path) == []


def test_session_converges_after_patience(tmp_path):
    """连续 N 轮无新最优就停，不无限跑。"""
    session = si.Session(cache_dir=tmp_path, workdir=tmp_path, goal='g',
                         max_rounds=50)
    session.without_best = si.CONVERGENCE_ROUNDS
    assert session.exhausted()


def test_session_respects_max_rounds(tmp_path):
    session = si.Session(cache_dir=tmp_path, workdir=tmp_path, goal='g',
                         max_rounds=2)
    session.rounds = 2
    assert session.exhausted()


def test_session_rounds_are_capped(tmp_path):
    """max_rounds 有硬上限，防止一个循环跑一整夜。"""
    session = si.Session(cache_dir=tmp_path, workdir=tmp_path, goal='g',
                         max_rounds=999)
    assert session.max_rounds <= 20


def test_session_report(tmp_path):
    session = si.Session(cache_dir=tmp_path, workdir=tmp_path, goal='降 lint 错误')
    text = session.report()
    assert '降 lint 错误' in text
    assert '轮数' in text
