"""跨进程回合控制通道：owner / steer / cancel 文件的行为回归。"""
import time

import pytest

import turn_files
from turn_control import TurnToken
from turn_files import TurnChannel


def _channel(tmp_path, session_id='s1', turn_id='t1'):
    token = TurnToken(session_id=session_id, turn_id=turn_id, source='tui')
    return TurnChannel(tmp_path, session_id, token).start(), token


def test_owner_is_visible_and_removed_on_close(tmp_path):
    channel, _token = _channel(tmp_path)
    owner = turn_files.active_owner(tmp_path, 's1')
    assert owner and owner['turn_id'] == 't1' and owner['source'] == 'tui'
    channel.close()
    assert turn_files.active_owner(tmp_path, 's1') is None


def test_steer_file_is_drained_once(tmp_path):
    channel, _token = _channel(tmp_path)
    try:
        info = turn_files.send_steer(tmp_path, 's1', 'look at docs',
                                     request_id='r1')
        assert info['status'] == 'queued' and info['turn_id'] == 't1'
        assert channel.drain_steer() == ['look at docs']
        assert channel.drain_steer() == []
    finally:
        channel.close()


def test_duplicate_request_id_is_dropped_by_owner(tmp_path):
    channel, _token = _channel(tmp_path)
    try:
        for _ in range(2):
            # 模拟客户端重试：文件不同但 request_id 相同
            directory = channel._steer_dir
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f'{time.time_ns()}.json'
            path.write_text('{"content": "once", "request_id": "same"}',
                            encoding='utf-8')
        assert channel.drain_steer() == ['once']
    finally:
        channel.close()


def test_cancel_only_matches_own_turn(tmp_path):
    channel, token = _channel(tmp_path)
    try:
        turn_files.send_cancel(tmp_path, 's1')
        assert channel.cancel_requested(interval=0)
        assert token.cancelled
    finally:
        channel.close()

    other, other_token = _channel(tmp_path, turn_id='t2')
    try:
        # 伪造上一回合的取消标记：不能被新回合接受
        (other.directory / 'cancel').write_text('{"turn_id": "t1"}',
                                                encoding='utf-8')
        assert not other.cancel_requested(interval=0)
        assert not other_token.cancelled
    finally:
        other.close()


def test_old_cancel_marker_is_ignored_by_new_turn(tmp_path):
    old, _token = _channel(tmp_path, turn_id='t-old')
    turn_files.send_cancel(tmp_path, 's1')
    # 模拟上一回合异常退出：没有走 close()，cancel 文件留在磁盘上
    fresh, fresh_token = _channel(tmp_path, turn_id='t-new')
    try:
        # 旧标记仍在磁盘上（关闭时才会清），但 turn_id 不匹配，不能取消新回合
        assert (fresh.directory / 'cancel').exists()
        assert not fresh.cancel_requested(interval=0), '旧 turn_id 不能取消新回合'
        assert not fresh_token.cancelled
    finally:
        fresh.close()
        assert not (fresh.directory / 'cancel').exists()


def test_stale_owner_is_ignored(tmp_path):
    channel, _token = _channel(tmp_path)
    channel.close()
    (channel.directory / 'owner.json').write_text(
        '{"turn_id": "t1", "source": "tui"}', encoding='utf-8')
    # 把 mtime 拨到很久以前
    old = time.time() - turn_files.STALE_AFTER - 10
    import os
    os.utime(channel.directory / 'owner.json', (old, old))
    assert turn_files.active_owner(tmp_path, 's1') is None
    assert turn_files.send_steer(tmp_path, 's1', 'x') is None
    assert turn_files.send_cancel(tmp_path, 's1') is None


def test_steer_queue_is_bounded(tmp_path, monkeypatch):
    channel, _token = _channel(tmp_path)
    try:
        monkeypatch.setattr(turn_files, 'MAX_PENDING_STEER', 2)
        turn_files.send_steer(tmp_path, 's1', 'a')
        turn_files.send_steer(tmp_path, 's1', 'b')
        with pytest.raises(RuntimeError):
            turn_files.send_steer(tmp_path, 's1', 'c')
    finally:
        channel.close()


def test_runtime_uses_file_channel_for_other_processes(tmp_path):
    """本进程没有 token（回合在别处）时，runtime.steer/interrupt 走文件通道。"""
    from agent_runtime import AgentRuntime
    from session_store import SessionEventStore

    calls = []

    def run_turn(messages, session_store, **kwargs):
        calls.append(list(messages))
        return 'ok'

    runtime = AgentRuntime(run_turn=run_turn,
                           session_store_cls=SessionEventStore,
                           sessions_dir=tmp_path / 'sessions')
    session_id = runtime.create_session()
    channel, token = _channel(tmp_path, session_id)
    try:
        steered = runtime.steer(session_id, 'cross-process', request_id='x1')
        assert steered['status'] == 'queued' and steered['channel'] == 'file'
        assert channel.drain_steer() == ['cross-process']

        cancelled = runtime.interrupt(session_id)
        assert cancelled['status'] == 'cancelling'
        # 取消是“投递”：由回合持有者轮询后生效（本测试里模拟 owner 轮询）
        assert channel.cancel_requested(interval=0) is True
        assert token.cancelled
        assert calls == [], '跨进程 steer/interrupt 不能在本进程跑新回合'
    finally:
        channel.close()


def test_agent_loop_drains_file_steer_and_honours_file_cancel(tmp_path, monkeypatch):
    import harness

    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions')
    monkeypatch.setattr(harness, 'CHECKPOINT_DIR', tmp_path / 'checkpoints')
    store = harness.SessionEventStore(tmp_path / 'sessions', session_id='loop-s1')

    # 回合开始前投递：agent_loop 第一轮前会注入。
    channel, _token = _channel(tmp_path, 'loop-s1', turn_id='loop-t1')
    turn_files.send_steer(tmp_path, 'loop-s1', 'from another process')
    delivered = []

    def fake_request(state, request_messages, *args, **kwargs):
        delivered.append([m.get('content') for m in request_messages
                          if m.get('role') == 'user'])
        from types import SimpleNamespace
        message = SimpleNamespace(role='assistant', content='done',
                                  tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message,
                                                        finish_reason='stop')],
                               usage=None)

    monkeypatch.setattr(harness, '_run_llm_request', fake_request)
    try:
        result = harness.agent_loop([{'role': 'user', 'content': 'go'}],
                                    session_store=store, housekeeping=False)
    finally:
        channel.close()
    assert result == 'done'
    assert 'from another process' in delivered[0]
    kinds = [event['type'] for event in store.read_events()]
    assert 'turn_terminal' in kinds


def test_turn_cancelled_consults_file_channel(monkeypatch):
    import harness
    import turn_control

    turn_control.reset()

    class Channel:
        def cancel_requested(self):
            return True

    class State:
        file_channel = Channel()

    assert harness._turn_cancelled(State()) is True


def test_storage_profile_recognises_control_and_watch_state(tmp_path, monkeypatch):
    import storage_profile

    (tmp_path / 'control').mkdir()
    (tmp_path / 'turn_watch.json').write_text('{}', encoding='utf-8')
    profile = storage_profile.scan(tmp_path, sessions_dir=tmp_path / 'sessions',
                                   checkpoint_keep=20, deep=False)
    names = {entry['key'] for entry in profile['entries']}
    assert 'control' in names
    unmanaged = {entry['key'] for entry in profile['unmanaged']}
    assert 'turn_watch.json' not in unmanaged
