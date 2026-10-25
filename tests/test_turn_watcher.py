"""阶段 1 watcher：增量游标、终态通知、至少一次投递与去重。"""
import time

from turn_watcher import TurnWatcher, WatchState


def _terminal(seq, turn_id, status='completed', source='tui', **extra):
    payload = {'seq': seq, 'type': 'turn_terminal', 'turn_id': turn_id,
               'status': status, 'source': source, 'ts': time.time()}
    payload.update(extra)
    return payload


def _started(seq, turn_id, ts):
    return {'seq': seq, 'type': 'turn_started', 'turn_id': turn_id,
            'source': 'tui', 'ts': ts}


class Scripted:
    """按轮次返回事件的假 fetch；记录调用次数。"""

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    def __call__(self, session_id, after):
        self.calls.append((session_id, after))
        batch = self.batches.pop(0) if self.batches else []
        return batch, max([int(event.get('seq') or 0) for event in batch],
                          default=after)


def _watcher(tmp_path, batches, sent, *, clock=time.time, stalled_after=300.0):
    state = WatchState(tmp_path / 'watch.json')
    fetch = Scripted(batches)

    def notify(session_id, kind, payload):
        sent.append((session_id, kind, dict(payload)))

    return TurnWatcher(state, fetch=fetch, notify=notify, sessions=lambda: ['s1'],
                       stalled_after=stalled_after, clock=clock), state, fetch


def test_terminal_notifies_once_even_when_events_replay(tmp_path):
    sent = []
    events = [_started(1, 't1', time.time()), _terminal(2, 't1')]
    watcher, state, fetch = _watcher(tmp_path, [events, events], sent)

    first = watcher.poll_once('s1')
    assert first['notified'] == 1
    assert sent[0][1] == 'terminal' and sent[0][2]['status'] == 'completed'
    assert state.cursor('s1') == 2

    second = watcher.poll_once('s1')
    assert second['notified'] == 0
    assert len(sent) == 1, 'replayed turn must not notify twice'
    assert fetch.calls[1] == ('s1', 2)


def test_feishu_sourced_turn_is_not_pushed_again(tmp_path):
    sent = []
    events = [_started(1, 't1', time.time()),
              _terminal(2, 't1', source='feishu')]
    watcher, state, _fetch = _watcher(tmp_path, [events], sent)
    watcher.poll_once('s1')
    assert sent == []
    assert state.cursor('s1') == 2


def test_failed_delivery_retries_without_advancing_cursor(tmp_path):
    sent = []
    state = WatchState(tmp_path / 'watch.json')
    events = [_started(1, 't1', time.time()), _terminal(2, 't1')]
    attempts = {'fail': True}

    def fetch(session_id, after):
        return events, 2

    def notify(session_id, kind, payload):
        if attempts['fail']:
            attempts['fail'] = False
            raise RuntimeError('feishu down')
        sent.append(payload)

    watcher = TurnWatcher(state, fetch=fetch, notify=notify,
                          sessions=lambda: ['s1'])
    result = watcher.poll_once('s1')
    assert result['error'] == 'notify_failed'
    assert sent == []
    assert state.cursor('s1') == 0
    assert state.attempts('s1', 's1:t1:completed') == 1

    result = watcher.poll_once('s1')
    assert result['notified'] == 1
    assert len(sent) == 1
    assert state.attempts('s1', 's1:t1:completed') == 2


def test_stalled_hint_is_sent_once_and_never_called_a_failure(tmp_path):
    now = {'value': 1000.0}
    sent = []
    events = [_started(1, 't1', now['value'])]
    watcher, state, _fetch = _watcher(
        tmp_path, [events], sent, clock=lambda: now['value'], stalled_after=60)
    watcher.poll_once('s1')
    assert sent == []
    now['value'] += 61
    assert watcher.check_stalled() == 1
    assert sent and sent[0][1] == 'stalled'
    assert 'status' not in sent[0][2], 'stalled 不是终态'
    assert watcher.check_stalled() == 0


def test_approval_timeout_notifies_once_per_request(tmp_path):
    """超时事件与回合终态独立：即使回合随后跑完，也只推一次『无人应答』。"""
    sent = []
    events = [{'seq': 1, 'type': 'approval_timeout', 'request_id': 'perm_1',
               'tool': 'bash', 'reason': 'destructive', 'timeout_s': 300.0,
               'ts': time.time()},
              _started(2, 't1', time.time()), _terminal(3, 't1')]
    watcher, state, _fetch = _watcher(tmp_path, [events, events], sent)

    first = watcher.poll_once('s1')
    assert first['notified'] == 2
    assert [item[1] for item in sent] == ['approval_timeout', 'terminal']
    assert sent[0][2]['request_id'] == 'perm_1'
    assert state.cursor('s1') == 3

    assert watcher.poll_once('s1')['notified'] == 0
    assert len(sent) == 2


def test_approval_timeout_delivery_failure_retries(tmp_path):
    sent = []
    state = WatchState(tmp_path / 'watch.json')
    event = {'seq': 1, 'type': 'approval_timeout', 'request_id': 'perm_2',
             'tool': 'write', 'reason': 'sensitive path', 'timeout_s': 60.0}
    attempts = {'fail': True}

    def fetch(session_id, after):
        return [event], 1

    def notify(session_id, kind, payload):
        if attempts['fail']:
            attempts['fail'] = False
            raise RuntimeError('feishu down')
        sent.append((session_id, kind, dict(payload)))

    watcher = TurnWatcher(state, fetch=fetch, notify=notify,
                          sessions=lambda: ['s1'])
    assert watcher.poll_once('s1')['error'] == 'notify_failed'
    assert state.cursor('s1') == 0
    assert watcher.poll_once('s1')['notified'] == 1
    assert sent and sent[0][1] == 'approval_timeout'
    assert state.cursor('s1') == 1


def test_state_survives_restart(tmp_path):
    sent = []
    events = [_started(1, 't1', time.time()), _terminal(2, 't1')]
    watcher, _state, _fetch = _watcher(tmp_path, [events], sent)
    watcher.poll_once('s1')

    reopened = WatchState(tmp_path / 'watch.json')
    assert reopened.cursor('s1') == 2
    assert reopened.notified('s1', 's1:t1:completed')

    watcher2 = TurnWatcher(reopened, fetch=Scripted([events]),
                           notify=lambda *a: sent.append(a),
                           sessions=lambda: ['s1'])
    assert watcher2.poll_once('s1')['notified'] == 0
    assert len(sent) == 1


def test_notified_history_is_bounded(tmp_path):
    state = WatchState(tmp_path / 'watch.json')
    for index in range(260):
        state.commit('s1', cursor=index, notified_key=f'k{index}')
    data = state._sessions['s1']
    assert len(data['notified']) <= 200
    assert 'k259' in data['notified']
    assert 'k0' not in data['notified']


def test_missing_session_is_reported_once_and_skipped(tmp_path):
    missing = []

    def fetch(session_id, after):
        raise FileNotFoundError(session_id)

    state = WatchState(tmp_path / 'watch.json')
    watcher = TurnWatcher(state, fetch=fetch, notify=lambda *a: None,
                          sessions=lambda: ['gone'],
                          on_missing=missing.append)
    assert watcher.poll_once('gone')['error'] == 'missing'
    assert missing == ['gone']
    assert watcher.poll_once('gone')['error'] == 'missing'
    assert missing == ['gone'], '同一会话只报告一次'
    assert watcher.poll_all() == [], '不再轮询已缺失会话'
