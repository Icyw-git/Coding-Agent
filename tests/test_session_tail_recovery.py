"""损坏尾部：读取可以容忍，**写入必须拒绝**（next-design §9.3 R0-f 第三条）。

原来 `append()` 完全不看尾部。崩溃留下的半行会被新事件接在后面，变成**文件中部的**
损坏——下次 `read_events()` 直接抛 `SessionReplayError`，而那时已经分不清哪一行是
用户的数据、哪一行是拼接出来的。

契约：

- 普通查询（`read_events`）仍容忍尾部不完整，**但不自动截断**、不删任何字节；
- `append()` 看到尾部不完整 → 抛 `SessionRecoveryRequired`（`code='recovery_required'`），
  **原文件字节不变**；
- 显式恢复 `recover_tail()` 持会话写锁、先把原件另存、然后区分两种情况：
  「完整但没有换行」→ 只补一个换行；「半行/坏行」→ 截到最后一条完整事件。
  中间损坏不按尾部问题处理，直接拒绝。
"""

import json

import pytest

import session_store
from session_store import SessionEventStore


def _store(tmp_path, session_id='tail'):
    store = SessionEventStore(tmp_path / 'sessions', session_id=session_id)
    store.append('message', message={'role': 'user', 'content': 'one'})
    store.append('message', message={'role': 'user', 'content': 'two'})
    return store


def test_partial_tail_can_still_be_read(tmp_path):
    """读容忍：半行不影响前面的事件（这是既有契约，别被守卫误伤）。"""
    store = _store(tmp_path)
    with store.path.open('a', encoding='utf-8', newline='\n') as handle:
        handle.write('{"v":1,"seq":3,"type":"mess')       # 崩溃留下的半行

    events = store.read_events()
    assert [event['seq'] for event in events] == [1, 2]


def test_append_refuses_and_leaves_the_file_untouched(tmp_path):
    store = _store(tmp_path)
    with store.path.open('a', encoding='utf-8', newline='\n') as handle:
        handle.write('{"v":1,"seq":3,"type":"mess')
    before = store.path.read_bytes()

    with pytest.raises(session_store.SessionRecoveryRequired) as excinfo:
        store.append('message', message={'role': 'user', 'content': 'three'})

    assert excinfo.value.code == 'recovery_required'
    assert excinfo.value.kind == 'partial_line', excinfo.value.kind
    assert store.path.read_bytes() == before, '拒绝追加时不能改动原文件'


def test_recover_truncates_a_partial_line_and_keeps_a_copy(tmp_path):
    store = _store(tmp_path)
    with store.path.open('a', encoding='utf-8', newline='\n') as handle:
        handle.write('{"v":1,"seq":3,"type":"mess')

    result = session_store.recover_tail(store.path)

    assert result['kind'] == 'partial_line'
    assert result['action'] == 'truncated'
    assert result['saved_to'] and __import__('pathlib').Path(result['saved_to']).is_file()
    # 原件里的半行必须留在备份里，供诊断
    assert 'mess' in __import__('pathlib').Path(result['saved_to']).read_text(encoding='utf-8')

    events = store.read_events()
    assert [event['seq'] for event in events] == [1, 2]
    store.append('message', message={'role': 'user', 'content': 'three'})
    assert [event['seq'] for event in store.read_events()] == [1, 2, 3]


def test_recover_only_adds_a_newline_for_a_complete_last_line(tmp_path):
    """完整但缺换行：内容不丢，只补一个换行。"""
    store = _store(tmp_path)
    raw = store.path.read_bytes()
    store.path.write_bytes(raw.rstrip(b'\n'))            # 去掉末尾换行，内容仍完整

    result = session_store.recover_tail(store.path)
    assert result['kind'] == 'complete_no_newline'
    assert result['action'] == 'newline_added'
    assert [event['seq'] for event in store.read_events()] == [1, 2]

    store.append('message', message={'role': 'user', 'content': 'three'})
    assert [event['seq'] for event in store.read_events()] == [1, 2, 3]


def test_midfile_corruption_is_not_treated_as_a_tail_problem(tmp_path):
    """中间坏行 + 尾部也坏：**拒绝恢复**，不能拿截断把中间那段一起抹掉。

    设计文档：「中间损坏不得按尾部问题静默跳过」。截断会让这个会话「能读了」，
    但同时丢掉中间之后的所有事件——比报错危险得多。
    """
    store = _store(tmp_path)
    raw = store.path.read_text(encoding='utf-8').splitlines()
    broken = raw[:1] + ['{"v":1,"seq":2,broken'] + raw[1:] + ['{"v":1,"seq":4,"ty']
    store.path.write_text('\n'.join(broken), encoding='utf-8')   # 末行也没换行

    assert session_store.tail_state(store.path)['kind'] == 'midfile'
    before = store.path.read_bytes()
    with pytest.raises(session_store.SessionRecoveryRequired) as excinfo:
        session_store.recover_tail(store.path)
    assert excinfo.value.kind == 'midfile', excinfo.value.kind
    assert store.path.read_bytes() == before, '拒绝恢复时不能动文件'


def test_valid_tail_with_a_broken_middle_is_left_to_the_reader(tmp_path):
    """中间坏、尾部好：没有「尾部」可恢复，recover_tail 什么都不做。

    这种情况由读取侧报 `SessionReplayError`——需要人判断，不是截断能解决的。
    """
    store = _store(tmp_path)
    raw = store.path.read_text(encoding='utf-8').splitlines()
    broken = raw[:1] + ['{"v":1,"seq":2,broken'] + raw[1:]
    store.path.write_text('\n'.join(broken) + '\n', encoding='utf-8')

    assert session_store.tail_state(store.path)['kind'] == 'ok'
    assert session_store.recover_tail(store.path)['action'] == 'none'


def test_healthy_store_appends_without_any_recovery(tmp_path):
    store = _store(tmp_path)
    store.append('message', message={'role': 'user', 'content': 'three'})
    assert len(store.read_events()) == 3
    assert json.loads(store.path.read_text(encoding='utf-8').splitlines()[-1])['seq'] == 3


def test_doctor_reports_and_repair_unblocks_the_session(tmp_path, monkeypatch, capsys):
    """doctor 报出来，repair 修完能继续写——否则拒绝追加等于把会话锁死。"""
    import harness
    import status_report

    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions', raising=False)
    monkeypatch.setattr(harness, 'MEMORY_DIR', tmp_path / 'memories', raising=False)
    monkeypatch.setattr(harness, 'MEMORY_INDEX', tmp_path / 'memories' / 'MEMORY.md',
                        raising=False)
    store = _store(tmp_path)
    with store.path.open('a', encoding='utf-8', newline='\n') as handle:
        handle.write('{"v":1,"seq":3,"ty')

    capsys.readouterr()
    status_report.run_doctor()
    out = capsys.readouterr().out
    assert 'sessions-damaged' in out, out[-800:]
    assert '--repair-session tail' in out, out[-800:]

    assert status_report.repair_session('tail') == 0
    store.append('message', message={'role': 'user', 'content': 'three'})
    assert [event['seq'] for event in store.read_events()] == [1, 2, 3]
