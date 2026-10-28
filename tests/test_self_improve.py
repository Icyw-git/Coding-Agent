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


# ---------------------------------------------------------------- 软指标（SICA 三目标）

def test_soft_metrics_are_registered():
    """SICA 的效用是 性能 .5 / 成本 .25 / 时间 .25。只有正确性没有成本，
    「测试全绿但 token 翻三倍」在本判据里会是「无变化」。"""
    soft = {name for name, spec in si.METRICS.items() if not spec['hard']}
    assert soft == {'tokens', 'wall_time'}


def test_soft_drift_is_reported_but_not_blocking():
    hard = {'pytest_pass': {'pass': 1}, 'ruff_errors': {'errors': 0}}
    before = {**hard, 'tokens': {'tokens_per_turn': 100.0},
              'wall_time': {'wall_time_s': 10.0}}
    after = {**hard, 'tokens': {'tokens_per_turn': 300.0},
             'wall_time': {'wall_time_s': 10.0}}
    drift = si.soft_drift(before, after)
    assert any('tokens' in item for item in drift)
    # 不进硬门槛：软指标不否决改动
    assert si.regressed(before, after) == []


def test_small_soft_drift_is_ignored():
    before = {'tokens': {'tokens_per_turn': 100.0}}
    after = {'tokens': {'tokens_per_turn': 105.0}}
    assert si.soft_drift(before, after) == []


def test_soft_metric_without_data_is_skipped():
    """测不出来就不比较——不填默认值，也不当恶化。"""
    assert si.soft_drift({}, {'tokens': {}}) == []


# ---------------------------------------------------------------- 耐心窗口（Gödel 92%）

def test_regression_enters_observation_not_discard():
    """Gödel Agent 实测 92% 的优化在变好之前必然先变差。

    「一变差就丢弃」会掐死绝大多数正确方向。
    """
    assert si.PATIENCE_ROUNDS >= 1


@pytest.fixture
def stubbed(tmp_path, monkeypatch):
    """把指标测量替换成可控序列，避免真的跑 pytest。"""
    def build(sequence):
        state = {'i': 0}

        def make(name):
            def measure(*, workdir):
                step = sequence[min(state['i'], len(sequence) - 1)]
                return step.get(name, {})
            return measure

        for name, spec in si.METRICS.items():
            monkeypatch.setitem(spec, 'measure', make(name))
        return state

    # workdir 用仓库根：被测路径是 skills/a.md，越界检查按 workdir 解析。
    session = si.Session(cache_dir=tmp_path, workdir='.',
                         goal='g', max_rounds=10)
    return session, build


def _hard(pass_=1, errors=0):
    return {'pytest_pass': {'pass': pass_}, 'ruff_errors': {'errors': errors}}


def test_observe_then_discard_when_never_recovers(stubbed):
    session, build = stubbed
    state = build([
        _hard(errors=5), _hard(errors=8), _hard(errors=9), _hard(errors=9),
    ])
    state['i'] = 0
    session.best = session.baseline()

    decisions = []
    for index in (1, 2, 3):
        state['i'] = index
        out = session.evaluate(['skills/a.md'], summary='s', mechanism='调整措辞')
        decisions.append(out['decision'])
    assert decisions == ['observe', 'observe', 'discard']


def test_recovering_regression_is_kept(stubbed):
    """先退后进：若一变差就丢弃，第 2 轮那个真正的改进永远看不到。"""
    session, build = stubbed
    state = build([_hard(errors=5), _hard(errors=8), _hard(errors=2)])
    state['i'] = 0
    session.best = session.baseline()

    state['i'] = 1
    assert session.evaluate(['skills/a.md'], summary='s',
                            mechanism='合并重复段落')['decision'] == 'observe'
    state['i'] = 2
    assert session.evaluate(['skills/a.md'], summary='s',
                            mechanism='合并重复段落')['decision'] == 'keep'


# ---------------------------------------------------------------- 可解释性门槛

def test_mechanism_is_required():
    assert not si.explainable('')
    assert not si.explainable('   ')


def test_mechanism_must_be_short():
    """能一句话讲清的多是真正的改进；需要一段话辩护的往往是漂移。"""
    assert si.explainable('缩短提示词以减少重复前缀')
    assert not si.explainable('很长的理由' * 40)


def test_evaluate_rejects_change_without_mechanism(tmp_path):
    session = si.Session(cache_dir=tmp_path, workdir='.', goal='g')
    result = session.evaluate(['skills/a.md'], summary='改了', mechanism='')
    assert result['decision'] == 'discard'
    assert 'mechanism' in result['reason']
    assert ledger.entries(tmp_path) == []      # 说不清就不该进台账


# ---------------------------------------------------------------- 台账防污染

def test_mechanism_is_stored_separately_from_metrics(tmp_path):
    """OWASP ASI06：agent 自己写的总结若能反向影响决策，就是正反馈回路。

    metrics/decision 由代码算；mechanism/summary 只是人类可读文本。
    """
    (tmp_path / 'AGENTS.md').write_text('v1', encoding='utf-8')
    change_id = ledger.record(tmp_path, paths=['AGENTS.md'], summary='改了',
                              metric={'round': 1}, workdir=tmp_path)
    ledger.update_decision(tmp_path, change_id, 'keep', '指标变好',
                           mechanism='减少重复段落')
    row = ledger.entries(tmp_path)[0]
    assert row['mechanism'] == '减少重复段落'
    assert row['metric'] == {'round': 1}       # 代码给的
    assert row['decision'] == 'keep'           # 来自判定，不来自 summary


# ---------------------------------------------------------------- editable 的 workdir 语义

def test_editable_resolves_relative_paths_against_workdir(tmp_path):
    """相对路径必须按 workdir 解析，不是 cwd。

    实测踩到的 bug：workdir 指向临时目录时 `editable('skills/a.md')` 返回 False，
    于是每一轮都被当成越界丢弃，而且因为早退连状态都不落盘。
    """
    (tmp_path / 'skills').mkdir()
    (tmp_path / 'skills' / 'a.md').write_text('v1', encoding='utf-8')
    assert si.editable('skills/a.md', workdir=str(tmp_path))


def test_editable_accepts_absolute_path_under_workdir(tmp_path):
    (tmp_path / 'skills').mkdir()
    target = tmp_path / 'skills' / 'a.md'
    target.write_text('v1', encoding='utf-8')
    assert si.editable(str(target), workdir=str(tmp_path))


def test_editable_still_blocks_outside_workdir(tmp_path):
    (tmp_path / 'skills').mkdir()
    assert not si.editable('tests/a.py', workdir=str(tmp_path))
    assert not si.editable('src/memory.py', workdir=str(tmp_path))
    assert not si.editable('../escape.md', workdir=str(tmp_path))


# ---------------------------------------------------------------- 状态持久化

def _stub_metrics(monkeypatch, sequence, state):
    def make(name):
        def measure(*, workdir):
            return sequence[min(state['i'], len(sequence) - 1)].get(name, {})
        return measure
    for name, spec in si.METRICS.items():
        monkeypatch.setitem(spec, 'measure', make(name))


def test_patience_window_survives_restart(tmp_path, monkeypatch):
    """耐心窗口的意义是跨多轮，而多轮可能跨天。状态只在内存里的话，
    一次重启就把观察期清零——本来在等的回归永远走不到 discard。"""
    state = {'i': 0}
    _stub_metrics(monkeypatch, [_hard(errors=5), _hard(errors=8), _hard(errors=9)],
                  state)
    (tmp_path / 'skills').mkdir()
    (tmp_path / 'skills' / 'a.md').write_text('v1', encoding='utf-8')

    first = si.Session(cache_dir=tmp_path, workdir=str(tmp_path), goal='降 lint')
    first.baseline()
    state['i'] = 1
    assert first.evaluate(['skills/a.md'], summary='r1',
                          mechanism='调整措辞')['decision'] == 'observe'

    # 模拟重启
    second = si.Session(cache_dir=tmp_path, workdir=str(tmp_path), goal='降 lint')
    assert second.rounds == 1
    assert second.pending_regression == 1
    state['i'] = 2
    assert second.evaluate(['skills/a.md'], summary='r2',
                           mechanism='调整措辞')['decision'] == 'observe'

    third = si.Session(cache_dir=tmp_path, workdir=str(tmp_path), goal='降 lint')
    assert third.pending_regression == 2
    assert third.evaluate(['skills/a.md'], summary='r3',
                          mechanism='调整措辞')['decision'] == 'discard'


def test_state_is_not_restored_across_different_goals(tmp_path, monkeypatch):
    """换目标等于换了一件事，旧进度没有参照意义。"""
    state = {'i': 0}
    _stub_metrics(monkeypatch, [_hard(errors=5)], state)
    (tmp_path / 'skills').mkdir()
    (tmp_path / 'skills' / 'a.md').write_text('v1', encoding='utf-8')

    first = si.Session(cache_dir=tmp_path, workdir=str(tmp_path), goal='目标 A')
    first.baseline()
    second = si.Session(cache_dir=tmp_path, workdir=str(tmp_path), goal='目标 B')
    assert second.rounds == 0
    assert second.best is None


def test_clear_state_resets_session(tmp_path, monkeypatch):
    state = {'i': 0}
    _stub_metrics(monkeypatch, [_hard(errors=5)], state)
    session = si.Session(cache_dir=tmp_path, workdir=str(tmp_path), goal='g')
    session.baseline()
    assert si.load_state(tmp_path) is not None
    si.clear_state(tmp_path)
    assert si.load_state(tmp_path) is None


def test_corrupt_state_file_is_ignored(tmp_path):
    (tmp_path / si.SESSION_NAME).write_text('{not json', encoding='utf-8')
    assert si.load_state(tmp_path) is None
    # 构造不应因此报错
    assert si.Session(cache_dir=tmp_path, workdir='.', goal='g').rounds == 0


def test_observe_does_not_count_toward_convergence(stubbed):
    """observe 的语义是「还在等，不算失败」，不该推进收敛。

    实测踩到的 bug：observe 走进 `else` 分支被计入 without_best，于是收敛在
    耐心窗口用完之前就触发——**耐心窗口对收敛路径是空操作**。
    消融实验里表现为「开关两种情况下结论相同」，才发现这条。
    """
    session, build = stubbed
    state = build([_hard(errors=5), _hard(errors=8), _hard(errors=8),
                   _hard(errors=8), _hard(errors=2)])
    state['i'] = 0
    session.best = session.baseline()

    # 前两轮回归：observe，且不推进 without_best
    for index in (1, 2):
        state['i'] = index
        assert session.evaluate(['skills/a.md'], summary='s',
                                mechanism='合并重复段落')['decision'] == 'observe'
    assert session.without_best == 0

    # 第三轮超出耐心窗口 → discard，这一次才计入收敛
    state['i'] = 3
    assert session.evaluate(['skills/a.md'], summary='s',
                            mechanism='合并重复段落')['decision'] == 'discard'
    assert session.without_best == 1

    # 仍能在收敛前跑到真正的改进
    assert not session.exhausted()
    state['i'] = 4
    assert session.evaluate(['skills/a.md'], summary='s',
                            mechanism='合并重复段落')['decision'] == 'keep'
