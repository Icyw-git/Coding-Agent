"""使用统计：从既有事件流聚合，不新增埋点。

这也是判断"记忆/主动性/自改进到底有没有用"的最小前提——没有它只能不断加机制、
永远无法判断。
"""
from __future__ import annotations

import json
import time

import pytest

import usage_stats

NOW = time.time()


def _session(directory, *, age_days=0, events=()):
    """造一个会话文件，mtime 拨到 age_days 天前。"""
    path = directory / f'session_{int(NOW)}_{abs(hash(str(events))) % 99999}.jsonl'
    lines = [json.dumps({'seq': i + 1, 'ts': NOW, 'type': event[0], **event[1]},
                        ensure_ascii=False)
             for i, event in enumerate(events)]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    if age_days:
        stamp = NOW - age_days * 86400
        import os
        os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def sessions(tmp_path):
    directory = tmp_path / 'sessions'
    directory.mkdir()
    return directory


def _usage(prompt=100, completion=20, cached=60, total=120, request_ms=500,
           model='test-model'):
    return {'prompt': prompt, 'completion': completion, 'cached': cached,
            'total': total, 'request_ms': request_ms, 'model': model}


def _tool_batch(*items, status='ok'):
    return {'tools': [{'name': name, 'duration_ms': duration}
                      for name, duration in items], 'status': status}


# ---------------------------------------------------------------- 聚合

def test_counts_requests_and_tokens(sessions):
    _session(sessions, events=[
        ('turn_started', {}),
        ('usage', _usage(prompt=100, completion=20, cached=60, total=120)),
        ('usage', _usage(prompt=200, completion=40, cached=100, total=240)),
        ('turn_completed', {}),
    ])
    stats = usage_stats.collect(sessions, days=7)
    assert stats['requests'] == 2
    assert stats['tokens']['prompt'] == 300
    assert stats['tokens']['completion'] == 60
    assert stats['tokens']['cached'] == 160
    assert stats['turns'] == 1
    assert stats['sessions'] == 1


def test_cache_hit_rate_uses_input_as_denominator(sessions):
    """口径：缓存命中率 = cached / prompt（输入），不是 / total。

    口径含糊的统计比没有统计更容易误导。
    """
    _session(sessions, events=[
        ('usage', _usage(prompt=100, cached=80, total=200)),
    ])
    assert usage_stats.collect(sessions, days=7)['cache_hit_rate'] == 0.8


def test_tool_calls_are_ranked(sessions):
    _session(sessions, events=[
        ('tool_profile', _tool_batch(('bash', 100), ('read', 10))),
        ('tool_profile', _tool_batch(('bash', 300))),
    ])
    stats = usage_stats.collect(sessions, days=7)
    assert stats['tools']['bash']['calls'] == 2
    assert stats['tools']['bash']['total_ms'] == 400
    assert stats['tools']['bash']['avg_ms'] == 200
    assert stats['tools']['read']['calls'] == 1
    # 按调用次数排序
    assert list(stats['tools'])[0] == 'bash'


def test_failed_tool_batch_is_counted(sessions):
    _session(sessions, events=[
        ('tool_profile', _tool_batch(('bash', 50), status='error')),
    ])
    stats = usage_stats.collect(sessions, days=7)
    assert stats['tools']['bash']['failed'] == 1


def test_approvals_and_denials(sessions):
    _session(sessions, events=[
        ('approval_auto', {'tool': 'bash', 'by': 'rule'}),
        ('approval_auto', {'tool': 'read', 'by': 'llm-judge'}),
        ('policy_denied', {'tool': 'bash', 'code': 'destructive'}),
    ])
    stats = usage_stats.collect(sessions, days=7)
    assert stats['approvals'] == {'auto': 2, 'denied': 1}


def test_memory_events(sessions):
    _session(sessions, events=[
        ('memory_written', {'name': 'a'}),
        ('memory_forgotten', {'name': 'b'}),
        ('memory_redacted', {'kinds': 'openai_key'}),
    ])
    stats = usage_stats.collect(sessions, days=7)
    assert stats['memory']['written'] == 1
    assert stats['memory']['forgotten'] == 1
    assert stats['memory']['redacted'] == 1


def test_latency_quantiles(sessions):
    _session(sessions, events=[
        ('usage', _usage(request_ms=100)), ('usage', _usage(request_ms=200)),
        ('usage', _usage(request_ms=300)), ('usage', _usage(request_ms=400)),
    ])
    stats = usage_stats.collect(sessions, days=7)
    assert stats['request_ms_p50'] is not None
    assert stats['request_ms_p90'] >= stats['request_ms_p50']


def test_models_are_counted(sessions):
    _session(sessions, events=[
        ('usage', _usage(model='a')), ('usage', _usage(model='a')),
        ('usage', _usage(model='b')),
    ])
    stats = usage_stats.collect(sessions, days=7)
    assert stats['models'] == {'a': 2, 'b': 1}


# ---------------------------------------------------------------- 时间窗口与容错

def test_old_sessions_are_excluded(sessions):
    _session(sessions, age_days=30, events=[
        ('usage', _usage()), ('turn_completed', {})])
    assert usage_stats.collect(sessions, days=7)['requests'] == 0
    assert usage_stats.collect(sessions, days=60)['requests'] == 1


def test_broken_lines_are_skipped(sessions):
    path = sessions / 'session_broken.jsonl'
    path.write_text('not json\n' + json.dumps(
        {'type': 'usage', **_usage()}) + '\n', encoding='utf-8')
    assert usage_stats.collect(sessions, days=7)['requests'] == 1


def test_missing_directory_is_safe(tmp_path):
    stats = usage_stats.collect(tmp_path / 'nope', days=7)
    assert stats['requests'] == 0
    assert stats['sessions'] == 0


def test_file_limit_is_respected(sessions):
    for index in range(5):
        _session(sessions, events=[('usage', _usage(request_ms=100 + index))])
    stats = usage_stats.collect(sessions, days=7, limit=2)
    assert stats['requests'] == 2


# ---------------------------------------------------------------- 输出

def test_report_lists_key_numbers(sessions):
    _session(sessions, events=[
        ('turn_started', {}),
        ('usage', _usage(prompt=1000, cached=800, completion=200, total=1200,
                         request_ms=1500)),
        ('tool_profile', _tool_batch(('bash', 100))),
        ('turn_completed', {}),
    ])
    text = usage_stats.format_report(usage_stats.collect(sessions, days=7))
    assert 'token' in text
    assert '缓存命中率' in text
    assert 'bash' in text
    assert '1500' in text or 'p50' in text


def test_report_on_empty_window(generic_sessions=None):
    assert '没有可统计的会话' in usage_stats.format_report(
        {'window_days': 7, 'requests': 0, 'turns': 0})


def test_report_warns_when_no_model_requests(sessions):
    """只有本地操作（无模型请求）时要说明，避免看起来像统计坏了。"""
    text = usage_stats.format_report(
        usage_stats.collect(sessions, days=7))
    assert '没有可统计的会话' in text


# ---------------------------------------------------------------- 性能：字节预筛

def test_only_interesting_lines_are_parsed(sessions):
    """会话里绝大多数是 message 行，全解码会慢一个数量级。

    实测 22 个会话：全解码 1095ms，字节预筛后 83ms。
    """
    bulk = [('message', {'message': {'role': 'user', 'content': 'x' * 200}})
            for _ in range(200)]
    _session(sessions, events=bulk + [('usage', _usage())])
    started = time.perf_counter()
    stats = usage_stats.collect(sessions, days=7)
    elapsed = time.perf_counter() - started
    assert stats['requests'] == 1
    assert elapsed < 1.0, f'扫描 {elapsed:.2f}s，预筛可能失效'


def test_local_only_activity_is_not_reported_as_empty(sessions):
    """只有工具调用、没有模型请求时不能说「没有数据」——那会让人以为统计坏了。"""
    _session(sessions, events=[('tool_profile', _tool_batch(('bash', 10)))])
    stats = usage_stats.collect(sessions, days=7)
    assert stats['sessions'] == 1
    assert stats['tools']
    text = usage_stats.format_report(stats)
    assert '没有可统计的会话' not in text
    assert 'bash' in text


def test_answers_where_the_time_went(sessions):
    """报告要能回答「时间花在哪」：工具按耗时也排得出来。"""
    _session(sessions, events=[
        ('tool_profile', _tool_batch(('desktop_snapshot', 9000))),
        ('tool_profile', _tool_batch(('read', 5))),
    ])
    stats = usage_stats.collect(sessions, days=7)
    assert stats['tools']['desktop_snapshot']['total_ms'] == 9000
    text = usage_stats.format_report(stats)
    assert 'desktop_snapshot' in text
