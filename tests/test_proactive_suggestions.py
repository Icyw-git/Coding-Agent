"""R2-a：建议要成为实体，接受/忽略回到生产入口（next-design §6.3 / §9.5）。

现状：`proactive.record_accepted` / `record_ignored` **只在测试里被调用**——没有任何
生产消费者。于是"忽略后会降频"这条承诺没人验证得起来，用户点了"忽略"也不会真的少被
打扰；"点开始才执行"也没有可绑定的对象。

契约（§6.3）：

- 建议实体：``id / workspace / evidence_refs / proposed_action / created_at / status``；
- 状态机：``proposed → accepted / dismissed / expired``；
- 接受是**幂等**的：同一 token 重复点击只受理一次；
- 接受要走**正常受理**（不是扩大权限）：点"开始"该照旧审批；
- **明确忽略**才记账；过期/未送达不算忽略（不能把沉默当拒绝）；
- busy 或明确未受理时建议仍可重试，**不能**标成 accepted。
"""

import pytest

import proactive
import proactive_suggestions as store
import request_dedup


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    base = tmp_path / 'cache'
    monkeypatch.setattr(store, 'STATE_PATH', base / 'suggestions.json')
    monkeypatch.setattr(request_dedup, 'STORE_PATH', base / 'dedup.jsonl')
    request_dedup.reset_cache()
    # 默认档是 off（`should_run` 会直接返回 False）。降频这条测的是"开了之后的行为"，
    # 所以显式开 observe——off 档另有测试守着（见 test_proactive.py）。
    monkeypatch.setenv('HARNESS_PROACTIVE_MODE', 'observe')
    return base


def _record(cache, *texts, workspace='/repo/a', recipient=''):
    return store.record(cache, list(texts), workspace=workspace,
                        evidence=('sessions/s1.jsonl',), recipient=recipient)


def test_proposals_are_recorded_as_entities(cache):
    rows = _record(cache, '补一下失败测试的日志', '把超时的用例标出来')

    assert [row['status'] for row in rows] == ['proposed', 'proposed']
    assert all(row['id'] for row in rows)
    assert rows[0]['workspace'] == '/repo/a'
    assert rows[0]['proposed_action'] == '补一下失败测试的日志'
    assert rows[0]['evidence_refs'] == ['sessions/s1.jsonl']
    assert len(store.open_items(cache)) == 2


def test_accept_is_idempotent_and_uses_a_stable_request_id(cache):
    row = _record(cache, '跑一遍测试')[0]
    calls = []

    def accept(request_id):
        calls.append(request_id)
        return {'status': 'accepted', 'turn_id': 'turn-1'}

    first = store.accept(cache, row['id'], accept=accept, recipient='chat-1')
    second = store.accept(cache, row['id'], accept=accept, recipient='chat-1')

    assert first['status'] == 'accepted' and first['turn_id'] == 'turn-1'
    assert second['status'] == 'accepted', '重复点击不该变成错误'
    assert calls == [first['request_id']], '第二次不该再受理一次'
    assert first['request_id'] == second['request_id']
    assert store.open_items(cache) == []


def test_accept_counts_as_positive_feedback(cache):
    row = _record(cache, '跑一遍测试')[0]
    proactive.record_ignored(cache)
    assert proactive._read_state(cache)['ignored_streak'] == 1

    store.accept(cache, row['id'],
                 accept=lambda request_id: {'status': 'accepted', 'turn_id': 't'},
                 recipient='chat-1')
    assert proactive._read_state(cache)['ignored_streak'] == 0, '采纳应当清零忽略计数'


def test_busy_keeps_the_suggestion_retryable(cache):
    """busy：建议必须还留在 proposed，下次还能点；不能标成 accepted。"""
    row = _record(cache, '跑一遍测试')[0]

    result = store.accept(cache, row['id'], accept=lambda rid: {'status': 'busy'},
                          recipient='chat-1')
    assert result['status'] == 'busy'
    assert [item['id'] for item in store.open_items(cache)] == [row['id']]
    assert store.accept(cache, row['id'],
                        accept=lambda rid: {'status': 'busy'},
                        recipient='chat-1')['status'] == 'busy'


def test_dismiss_counts_as_feedback_and_lowers_frequency(cache):
    """明确忽略 → 记负反馈 → 间隔变长。"""
    due, _ = proactive.should_run(cache)
    assert due is True

    for _ in range(proactive.IGNORE_LIMIT):
        row = _record(cache, '又一条')[0]
        assert store.dismiss(cache, row['id'], recipient='chat-1')['status'] == 'dismissed'
        proactive.record_run(cache, 1)      # 每次跑完都记一次时间

    _due, reason = proactive.should_run(cache)
    assert reason.startswith('next run in'), reason
    assert proactive._read_state(cache)['ignored_streak'] >= proactive.IGNORE_LIMIT


def test_expired_suggestion_is_not_counted_as_ignored(cache, monkeypatch):
    """过期 ≠ 忽略：用户根本没看到，不能拿它降频。"""
    row = _record(cache, '过期的那条')[0]
    real_now = store._now()
    monkeypatch.setattr(store, '_now', lambda: real_now + store.TTL_SECONDS + 1)

    result = store.dismiss(cache, row['id'], recipient='chat-1')
    assert result['status'] == 'expired'
    assert proactive._read_state(cache).get('ignored_streak', 0) == 0
    assert store.open_items(cache) == []


def test_unknown_token_and_wrong_recipient_are_rejected(cache):
    row = _record(cache, '发给 chat-1 的建议', recipient='chat-1')[0]

    assert store.accept(cache, 'no-such-id', accept=lambda rid: None)['status'] == 'unknown'
    assert store.check_token(cache, row['id'], recipient='chat-2') is False
    assert store.check_token(cache, row['id'], recipient='chat-1') is True

    wrong = store.accept(cache, row['id'], accept=lambda rid: {'status': 'accepted'},
                         recipient='chat-2')
    assert wrong['status'] == 'forbidden'
    assert len(store.open_items(cache)) == 1, '错误接收人不能把建议消费掉'


def test_accept_that_never_lands_keeps_the_suggestion_open(cache):
    """受理回包丢了 / 明确未受理：建议不标 accepted，仍可重试。"""
    row = _record(cache, '跑一遍测试')[0]

    result = store.accept(cache, row['id'],
                          accept=lambda rid: {'status': 'pending'},
                          recipient='chat-1')
    assert result['status'] == 'pending'
    assert [item['id'] for item in store.open_items(cache)] == [row['id']]


def test_expired_items_are_pruned_from_the_store(cache, monkeypatch):
    _record(cache, '会过期的一条')
    real_now = store._now()
    monkeypatch.setattr(store, '_now', lambda: real_now + store.TTL_SECONDS + 1)

    assert store.open_items(cache) == []
    # 状态落成 expired，而不是被悄悄删掉（事后能看见"它过期了"）
    rows = store.all_items(cache)
    assert [row['status'] for row in rows] == ['expired']


# ============================================================
# 生产入口：/suggest list / start / skip
# ============================================================


def test_tui_lists_starts_and_skips_suggestions(cache, monkeypatch):
    """TUI 的 /suggest list|start|skip 是这套闭环的第一个生产消费者。"""

    import harness
    from ui.proactive_actions import ProactiveActionsMixin

    monkeypatch.setattr(harness, 'CACHE_DIR', cache, raising=False)
    monkeypatch.setattr(harness, 'get_workdir', lambda: '/repo/a', raising=False)

    notices = []
    started = []

    class _App(ProactiveActionsMixin):
        _busy = False

        def _notice(self, text, level='info'):
            notices.append(str(text))

        def _begin_turn(self, request, *, echo_text=True):
            started.append((request.text, echo_text))
            return True

    _record(cache, '第一条建议', '第二条建议')
    app = _App()

    app.start_suggestions('list')
    assert '第一条建议' in notices[-1] and '1.' in notices[-1]

    app.start_suggestions('start 1')
    assert started and started[-1][0] in ('第一条建议', '第二条建议'), started
    assert '开始执行' in notices[-1], notices[-1]
    assert len(store.open_items(cache)) == 1, '采纳后应当少一条'

    app.start_suggestions('skip 1')
    assert '不再提醒' in notices[-1], notices[-1]
    assert store.open_items(cache) == []


def test_tui_start_while_busy_keeps_the_suggestion(cache, monkeypatch):
    import harness

    monkeypatch.setattr(harness, 'CACHE_DIR', cache, raising=False)
    monkeypatch.setattr(harness, 'get_workdir', lambda: '/repo/a', raising=False)

    from ui.proactive_actions import ProactiveActionsMixin

    notices = []

    class _App(ProactiveActionsMixin):
        _busy = True

        def _notice(self, text, level='info'):
            notices.append(str(text))

        def _begin_turn(self, request, *, echo_text=True):   # pragma: no cover
            raise AssertionError('忙的时候不该开回合')

    _record(cache, '唯一一条建议')
    _App().start_suggestions('start 1')

    assert '回合进行中' in notices[-1], notices[-1]
    assert len(store.open_items(cache)) == 1, '忙的时候建议必须还在'


# ============================================================
# 只读回合：`proactive=True` 时工具面被裁到白名单
# ============================================================


def test_proactive_turn_is_read_only_and_wrapped(tmp_path):
    """飞书建议入口走的就是这条：裁 schema + 开钩子白名单，两道一起。"""
    import hooks
    from agent_runtime import AgentRuntime
    from session_store import SessionEventStore

    seen = {}

    def run_turn(messages, session_store, **kwargs):
        import proactive

        seen['tools'] = kwargs.get('tools')
        seen['scope'] = hooks.proactive_turn()
        seen['allowed'] = {item['function']['name']
                           for item in proactive.read_only_schemas()}
        return '建议一二三'

    runtime = AgentRuntime(run_turn=run_turn,
                           session_store_cls=SessionEventStore,
                           sessions_dir=tmp_path / 'sessions')
    session_id = runtime.create_session('sess')
    runtime.send(session_id, '给点建议', proactive=True)

    assert seen['scope'] is True, '没有开钩子白名单，模型仍可能调到写工具'
    names = {item['function']['name'] for item in seen['tools']}
    assert names == seen['allowed'], names
    assert 'write' not in names and 'bash' not in names, names
