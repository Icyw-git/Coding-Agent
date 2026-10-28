"""异步 LLM-as-judge 记忆评测（memory_eval）。

为什么要异步：判一次要一次模型调用（1~3 秒）。记忆是背景设施，不能占用
主路径——所有评测在回合结束后由后台线程跑，结果只落盘、只展示，
**不改记忆、不改召回、不触发任何自动动作**（否则评测会变成一条自我强化的路径）。
"""
from __future__ import annotations

import json
import time

import pytest

import memory_eval as me


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(me.ENV, raising=False)
    monkeypatch.delenv(me.ENV_EVERY, raising=False)


RECALLED = [
    {'name': 'deploy-prod', 'type': 'fact',
     'description': 'production deploys go through make ship-prod'},
    {'name': 'pref-tabs', 'type': 'preference',
     'description': 'prefers tabs over spaces in code'},
]
LIBRARY = RECALLED + [
    {'name': 'trip-note', 'type': 'context',
     'description': 'this trip: avoid checked bags'},
]


def _fake(*, recall='{"relevant":[0],"missing":""}',
          quality='{"keep":[0],"drop":[1]}'):
    """返回一个按提示词分派的假完成函数。"""
    def complete(prompt: str) -> str:
        if 'auditing a memory system' in prompt:
            return recall
        if 'auditing stored assistant memories' in prompt:
            return quality
        raise AssertionError(f'unexpected prompt: {prompt[:60]}')
    return complete


# ---------------------------------------------------------------- 开关与采样

def test_disabled_by_default():
    """多数人不该为评测付费。"""
    assert not me.enabled()
    assert not me.should_sample(10)


def test_sampling_by_turn(monkeypatch):
    monkeypatch.setenv(me.ENV, '1')
    assert me.should_sample(10)
    assert not me.should_sample(7)
    assert not me.should_sample(0)


def test_sampling_interval_configurable(monkeypatch):
    monkeypatch.setenv(me.ENV, '1')
    monkeypatch.setenv(me.ENV_EVERY, '3')
    assert me.every() == 3
    assert me.should_sample(3) and me.should_sample(6)
    assert not me.should_sample(4)


def test_bad_interval_falls_back(monkeypatch):
    monkeypatch.setenv(me.ENV_EVERY, 'oops')
    assert me.every() == me.DEFAULT_EVERY
    monkeypatch.setenv(me.ENV_EVERY, '0')
    assert me.every() == 1


# ---------------------------------------------------------------- 判召回

def test_judge_recall_parses_indices(tmp_path):
    result = me.judge_recall('部署怎么发', RECALLED, complete=_fake())
    assert result['relevant'] == [0]
    assert result['judged'] == 2


def test_judge_recall_reports_missing():
    result = me.judge_recall('部署怎么发', RECALLED,
                             complete=_fake(recall='{"relevant":[],"missing":"rollback"}'))
    assert result['relevant'] == []
    assert result['missing'] == 'rollback'


def test_judge_recall_tolerates_code_fences():
    reply = '```json\n{"relevant":[1]}\n```'
    assert me.judge_recall('x', RECALLED, complete=_fake(recall=reply))['relevant'] == [1]


def test_judge_recall_drops_invalid_indices():
    """越界/非整数下标一律丢弃：模型的输出不可信到可以直接打索引。"""
    reply = '{"relevant":[0, 99, -1, "0", 1.5, true, 1]}'
    assert me.judge_recall('x', RECALLED, complete=_fake(recall=reply))['relevant'] == [0, 1]


def test_judge_recall_without_notes_is_noop():
    assert me.judge_recall('x', [], complete=_fake()) == {
        'relevant': [], 'irrelevant': [], 'abstain': [],
        'missing': '', 'confidence': 'low', 'judged': 0}


def test_judge_recall_without_message_is_noop():
    assert me.judge_recall('   ', RECALLED, complete=_fake())['judged'] == 0


def test_judge_recall_caps_note_count():
    many = [{'name': f'n{i}', 'type': 'fact', 'description': 'd'} for i in range(50)]
    assert me.judge_recall('x', many, complete=_fake())['judged'] == me.MAX_JUDGED


# ---------------------------------------------------------------- 判沉淀质量

def test_judge_quality_splits_keep_and_drop():
    result = me.judge_quality(LIBRARY, complete=_fake(quality='{"keep":[0,1],"drop":[2]}'))
    assert result['keep'] == [0, 1]
    assert result['drop'] == [2]


def test_judge_quality_never_both_lists():
    """模型自相矛盾时以 keep 为准，drop 里不重复。"""
    result = me.judge_quality(LIBRARY, complete=_fake(quality='{"keep":[0],"drop":[0,1]}'))
    assert result['keep'] == [0]
    assert result['drop'] == [1]


def test_judge_quality_empty_library():
    assert me.judge_quality([], complete=_fake())['judged'] == 0


# ---------------------------------------------------------------- 异步与落盘

def test_run_async_records_both_kinds(tmp_path, monkeypatch):
    monkeypatch.setenv(me.ENV, '1')
    thread = me.run_async(cache_dir=tmp_path, turn_no=10, message='部署怎么发',
                          recalled=RECALLED, library=LIBRARY,
                          complete=_fake(quality='{"keep":[0,1],"drop":[2]}'))
    assert thread is not None and thread.daemon
    me.wait_for_eval(10)
    kinds = sorted(row['kind'] for row in me.load_records(tmp_path))
    assert kinds == ['quality', 'recall']


def test_run_async_skips_when_not_sampled(tmp_path, monkeypatch):
    monkeypatch.setenv(me.ENV, '1')
    assert me.run_async(cache_dir=tmp_path, turn_no=7, message='x',
                        recalled=RECALLED, complete=_fake()) is None
    assert me.load_records(tmp_path) == []


def test_run_async_does_not_block_caller(tmp_path, monkeypatch):
    """关键性质：判得慢也不能拖住调用方。"""
    monkeypatch.setenv(me.ENV, '1')

    def slow(prompt: str) -> str:
        time.sleep(0.4)
        return _fake()(prompt)

    started = time.perf_counter()
    me.run_async(cache_dir=tmp_path, turn_no=10, message='x',
                 recalled=RECALLED, complete=slow)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.2, f'run_async 阻塞了 {elapsed:.2f}s'
    me.wait_for_eval(10)


def test_judge_failure_is_contained(tmp_path, monkeypatch):
    """判失败只丢这一轮的数据，不影响落盘的其他部分。"""
    monkeypatch.setenv(me.ENV, '1')

    def broken(prompt: str) -> str:
        if 'auditing a memory system' in prompt:
            raise RuntimeError('model down')
        return '{"keep":[0],"drop":[]}'

    me.run_async(cache_dir=tmp_path, turn_no=10, message='x',
                 recalled=RECALLED, library=LIBRARY, complete=broken)
    me.wait_for_eval(10)
    kinds = [row['kind'] for row in me.load_records(tmp_path)]
    assert kinds == ['quality']


def test_unparsable_reply_records_empty(tmp_path, monkeypatch):
    monkeypatch.setenv(me.ENV, '1')
    me.run_async(cache_dir=tmp_path, turn_no=10, message='x', recalled=RECALLED,
                 complete=lambda prompt: 'I cannot answer that.')
    me.wait_for_eval(10)
    rows = me.load_records(tmp_path)
    assert rows and rows[0]['relevant'] == []


# ---------------------------------------------------------------- 汇总

def test_summary_empty(tmp_path):
    assert me.summarize(tmp_path) == {'samples': 0}
    assert '还没有评测数据' in me.format_summary(tmp_path)


def test_summary_computes_rates(tmp_path, monkeypatch):
    monkeypatch.setenv(me.ENV, '1')
    me.record(tmp_path, {'kind': 'recall', 'judged': 2, 'relevant': [0], 'missing': ''})
    me.record(tmp_path, {'kind': 'recall', 'judged': 2, 'relevant': [0, 1], 'missing': 'x'})
    me.record(tmp_path, {'kind': 'quality', 'judged': 4, 'keep': [0, 1], 'drop': [2, 3]})
    stats = me.summarize(tmp_path)
    assert stats['recall_precision'] == 0.75      # 3 / 4
    assert stats['missing_rate'] == 0.5           # 1 / 2
    assert stats['keep_rate'] == 0.5              # 2 / 4


def test_summary_skips_broken_lines(tmp_path):
    path = tmp_path / me.REPORT_NAME
    path.write_text('{"kind":"recall","judged":1,"relevant":[0]}\nnot json\n',
                    encoding='utf-8')
    assert me.summarize(tmp_path)['samples'] == 1


def test_format_summary_reports_state_when_disabled(tmp_path):
    assert '关' in me.format_summary(tmp_path)


def test_record_appends_jsonl(tmp_path):
    me.record(tmp_path, {'kind': 'recall', 'judged': 1, 'relevant': [0]})
    me.record(tmp_path, {'kind': 'quality', 'judged': 1, 'keep': [0]})
    rows = me.load_records(tmp_path)
    assert len(rows) == 2
    assert all('ts' in row for row in rows)
    raw = (tmp_path / me.REPORT_NAME).read_text(encoding='utf-8')
    assert len([line for line in raw.splitlines() if line.strip()]) == 2


def test_report_is_valid_jsonl(tmp_path):
    me.record(tmp_path, {'kind': 'recall', 'judged': 1, 'relevant': [0],
                         'note': '中文测试'})
    raw = (tmp_path / me.REPORT_NAME).read_text(encoding='utf-8')
    json.loads(raw.strip().splitlines()[0])


# ---------------------------------------------------------------- 判据与弃权

def test_recall_judge_has_explicit_criteria():
    """判据必须是可核对的条件，不是"judge this"。"""
    assert '(1)' in me.RECALL_PROMPT and '(2)' in me.RECALL_PROMPT
    assert 'NOT relevant if' in me.RECALL_PROMPT
    assert 'abstain' in me.RECALL_PROMPT


def test_quality_judge_has_explicit_criteria():
    assert '(1)' in me.QUALITY_PROMPT and '(2)' in me.QUALITY_PROMPT
    assert 'KEEP examples' in me.QUALITY_PROMPT
    assert 'DROP examples' in me.QUALITY_PROMPT


def test_abstain_is_separate_from_irrelevant():
    """弃权不能混进"不相关"：前者是判不了，后者是否定。"""
    reply = '{"relevant":[0],"irrelevant":[1],"abstain":[2]}'
    result = me.judge_recall('x', RECALLED + [{'description': 'vague'}],
                             complete=_fake(recall=reply))
    assert result['relevant'] == [0]
    assert result['irrelevant'] == [1]
    assert result['abstain'] == [2]


def test_partition_is_mutually_exclusive():
    """模型可能把同一索引写进多个列表，重复计数会让精确率虚高。"""
    reply = '{"relevant":[0],"irrelevant":[0,1],"abstain":[0,1]}'
    result = me.judge_recall('x', RECALLED, complete=_fake(recall=reply))
    assert result['relevant'] == [0]
    assert result['irrelevant'] == [1]
    assert result['abstain'] == []


def test_confidence_is_parsed():
    assert me.judge_recall('x', RECALLED, complete=_fake(
        recall='{"relevant":[0],"confidence":"high"}'))['confidence'] == 'high'
    assert me.judge_recall('x', RECALLED, complete=_fake(
        recall='{"relevant":[0]}'))['confidence'] == 'low'


def test_abstain_excluded_from_precision(tmp_path):
    """弃权从分母剔除：把"判不了"算成判错会惩罚正确的谨慎。"""
    me.record(tmp_path, {'kind': 'recall', 'judged': 3,
                         'relevant': [0], 'irrelevant': [1], 'abstain': [2]})
    stats = me.summarize(tmp_path)
    assert stats['recall_precision'] == 0.5      # 1 相关 / (3 - 1 弃权)
    assert stats['recall_abstain_rate'] == 0.333


# ---------------------------------------------------------------- κ 校准

def test_kappa_perfect_agreement():
    assert me.cohen_kappa([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0


def test_kappa_catches_always_positive_judge():
    """关键用例：原始一致率 50%，但判官其实什么都没判（κ=0）。"""
    report = me.validate_against_labels([1, 0, 1, 0], [1, 1, 1, 1])
    assert report['raw_agreement'] == 0.5
    assert report['kappa'] == 0.0
    assert not report['usable']


def test_kappa_chance_level_is_zero():
    assert me.cohen_kappa([1, 1, 0, 0], [1, 0, 1, 0]) == 0.0


def test_kappa_single_label_is_undefined():
    """两边都只用一个类别时 κ 无定义（分母为 0），返回 None 而不是 1.0。"""
    assert me.cohen_kappa([1, 1, 1], [1, 1, 1]) is None


def test_kappa_length_mismatch():
    assert me.cohen_kappa([1, 0], [1]) is None
    assert me.cohen_kappa([], []) is None


def test_kappa_bands():
    assert '不可用' in me.kappa_band(0.1)
    assert '趋势' in me.kappa_band(0.3)
    assert '可参考' in me.kappa_band(0.5)
    assert '验收' in me.kappa_band(0.7)
    assert '无法计算' in me.kappa_band(None)


def test_usable_threshold_is_substantial_agreement():
    """0.41 是"中等一致"的下界：低于它不能当结论。"""
    assert me.validate_against_labels([1, 0, 1, 0], [1, 0, 1, 1])['usable'] is True
    assert me.validate_against_labels([1, 0, 1, 0], [1, 1, 0, 0])['usable'] is False
