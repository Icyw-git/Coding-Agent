"""请求前缀指纹：定位缓存跌破时第一条被改写的消息。"""

from prompt_cache_debug import enabled, fingerprints, first_divergence, message_fingerprint


def test_fingerprint_is_stable_and_sensitive():
    first = {'role': 'user', 'content': 'hello'}
    assert message_fingerprint(first) == message_fingerprint(dict(first))
    assert message_fingerprint(first) != message_fingerprint(
        {'role': 'user', 'content': 'hello!'})


def test_tool_calls_are_part_of_the_fingerprint():
    base = {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'c1', 'type': 'function',
                            'function': {'name': 'bash', 'arguments': '{}'}}]}
    same = {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'c1', 'type': 'function',
                            'function': {'name': 'bash', 'arguments': '{}'}}]}
    other = {'role': 'assistant', 'content': None,
             'tool_calls': [{'id': 'c1', 'type': 'function',
                             'function': {'name': 'bash',
                                          'arguments': '{"command": "ls"}'}}]}
    assert message_fingerprint(base) == message_fingerprint(same)
    assert message_fingerprint(base) != message_fingerprint(other)


def test_pure_append_keeps_the_prefix():
    previous = fingerprints([{'role': 'user', 'content': 'a'}])
    current = fingerprints([{'role': 'user', 'content': 'a'},
                            {'role': 'assistant', 'content': 'b'}])
    assert first_divergence(previous, current) == len(previous)


def test_rewritten_middle_reports_the_first_changed_message():
    previous = fingerprints([{'role': 'user', 'content': 'a'},
                             {'role': 'tool', 'content': 'old'},
                             {'role': 'user', 'content': 'b'}])
    current = fingerprints([{'role': 'user', 'content': 'a'},
                            {'role': 'tool', 'content': 'new'},
                            {'role': 'user', 'content': 'b'}])
    assert first_divergence(previous, current) == 1


def test_shorter_prefix_reports_its_length():
    previous = fingerprints([{'role': 'user', 'content': 'a'},
                             {'role': 'user', 'content': 'b'}])
    current = fingerprints([{'role': 'user', 'content': 'a'}])
    assert first_divergence(previous, current) == 1


def test_enabled_flag(monkeypatch):
    monkeypatch.delenv('HARNESS_DEBUG_CACHE', raising=False)
    assert not enabled()
    monkeypatch.setenv('HARNESS_DEBUG_CACHE', '1')
    assert enabled()
    monkeypatch.setenv('HARNESS_DEBUG_CACHE', 'off')
    assert not enabled()


def test_report_writes_a_jsonl_record_only_when_the_prefix_is_rewritten(
        monkeypatch, capsys, tmp_path):
    import json

    import harness

    monkeypatch.setenv('HARNESS_DEBUG_CACHE', '1')
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions')
    harness._LAST_REQUESTS.clear()
    first = [{'role': 'user', 'content': 'a'}]
    harness._report_cache_prefix('s1', first)
    harness._report_cache_prefix(
        's1', first + [{'role': 'assistant', 'content': 'b'}])
    assert 'prefix changed' not in capsys.readouterr().err
    assert not (tmp_path / 'cache_debug.jsonl').exists()

    harness._report_cache_prefix(
        's1', [{'role': 'user', 'content': 'a'},
               {'role': 'assistant', 'content': 'B'}])
    assert 'prefix changed at msg 1' in capsys.readouterr().err

    record = json.loads(
        (tmp_path / 'cache_debug.jsonl').read_text(encoding='utf-8').splitlines()[-1])
    assert record['session_id'] == 's1' and record['index'] == 1
    assert record['before']['head'] == 'b'
    assert record['after']['head'] == 'B'
    assert record['truncated'] is False
    assert record['system_fingerprint']


def test_shorter_request_records_truncation_instead_of_crashing(
        monkeypatch, capsys, tmp_path):
    import json

    import harness

    monkeypatch.setenv('HARNESS_DEBUG_CACHE', '1')
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions')
    harness._LAST_REQUESTS.clear()
    previous = [{'role': 'user', 'content': 'a'},
                {'role': 'assistant', 'content': 'b'},
                {'role': 'user', 'content': 'c'},
                {'role': 'assistant', 'content': 'd'}]
    harness._report_cache_prefix('s1', previous)
    harness._report_cache_prefix('s1', previous[:2])
    assert 'prefix changed at msg 2' in capsys.readouterr().err

    record = json.loads(
        (tmp_path / 'cache_debug.jsonl').read_text(encoding='utf-8').splitlines()[-1])
    assert record['index'] == 2 and record['truncated'] is True
    assert record['after'] is None
    assert record['before'] == {'role': 'user', 'chars': 1, 'head': 'c'}


def test_system_text_is_fingerprinted_without_storing_the_whole_request(
        monkeypatch, tmp_path):
    import json

    import harness

    monkeypatch.setenv('HARNESS_DEBUG_CACHE', '1')
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions')
    harness._LAST_REQUESTS.clear()
    messages = [{'role': 'user', 'content': 'a'}]
    harness._report_cache_prefix('s1', messages, system='old prompt')
    harness._report_cache_prefix('s1', messages, system='new prompt')

    record = json.loads(
        (tmp_path / 'cache_debug.jsonl').read_text(encoding='utf-8').splitlines()[-1])
    assert record['index'] == 0
    assert record['system_chars'] == len('new prompt')
    assert record['before']['head'] == 'old prompt'
    assert record['after']['head'] == 'new prompt'
    assert harness._LAST_REQUESTS['s1'][1][0]['head'] == 'new prompt'


def test_recent_requests_are_bounded(monkeypatch, tmp_path):
    import harness

    monkeypatch.setenv('HARNESS_DEBUG_CACHE', '1')
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions')
    harness._LAST_REQUESTS.clear()
    total = harness._CACHE_DEBUG_SESSIONS + 5
    for index in range(total):
        harness._report_cache_prefix(
            f's{index}', [{'role': 'user', 'content': str(index)}])

    assert len(harness._LAST_REQUESTS) == harness._CACHE_DEBUG_SESSIONS
    assert 's0' not in harness._LAST_REQUESTS
    assert f's{total - 1}' in harness._LAST_REQUESTS
