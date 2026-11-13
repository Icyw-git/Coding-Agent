"""R2-b1：统一的投递契约（next-design §5.2 第二步 / §9.5）。

三个消费者（cron 结果、心跳摘要、回合通知）现在是三份**形状相似但字段不同**的
待发队列：`pending` 列表 + 按 id ack + 一个条数上限。共同的缺口：

- 失败只是"留在 pending"，没有退避、没有死信——一个永久失效的接收人会每轮被重试；
- 上限只有一个条数，字节与保留时长没人管，也没有"为什么被裁"的记录；
- 逐个接收人确认靠调用方自己写 for 循环，容易漏。

契约（§5.2）：同一种记录字段 + 重试分类 + 逐接收人确认 + 失败退避 + 死信可查可重发；
上限要**条数/字节/期限三条各自可见且有界**。发送与 ack 之间崩溃仍允许重复——
`delivery_id` 只能减少重复，不能承诺外部恰好一次。
"""

import pytest

import delivery


@pytest.fixture()
def store(tmp_path):
    return tmp_path / 'deliveries.json'


def _clock(start=1_000_000.0):
    state = {'now': start}
    return state, (lambda: state['now'])


def test_records_carry_the_contract_fields(store):
    state, clock = _clock()
    row = delivery.enqueue(store, recipient='chat-1', text='结果一',
                           source_ref='turn-42', kind='result', now=clock())

    assert row['delivery_id']
    assert row['recipient'] == 'chat-1'
    assert row['source_ref'] == 'turn-42'
    assert row['kind'] == 'result'
    assert row['state'] == 'pending'
    assert row['attempts'] == 0
    assert row['next_retry_at'] <= clock()
    assert row['bytes'] == len('结果一'.encode('utf-8'))


def test_per_recipient_ack_only_drops_that_recipient(store):
    """两个接收人：A 成功、B 失败 → 下一轮只该重试 B。"""
    state, clock = _clock()
    a = delivery.enqueue(store, recipient='chat-a', text='结果', source_ref='r1',
                         now=clock())
    b = delivery.enqueue(store, recipient='chat-b', text='结果', source_ref='r1',
                         now=clock())

    assert delivery.ack(store, a['delivery_id'])
    failed = delivery.fail(store, b['delivery_id'], error='network', now=clock())

    # 失败的 B 进入退避：立刻查是空的，等到 next_retry_at 才重新出现。
    assert delivery.due(store, now=clock()) == []
    due = delivery.due(store, now=failed['next_retry_at'])
    assert [row['delivery_id'] for row in due] == [b['delivery_id']]
    assert due[0]['recipient'] == 'chat-b', '只该重试失败的那个接收人'
    assert delivery.due(store, recipient='chat-a',
                        now=failed['next_retry_at']) == []


def test_failure_backs_off_then_becomes_a_dead_letter(store):
    """退避：下一次重试时间往后推；次数用完进死信（不再每轮重试）。"""
    state, clock = _clock()
    row = delivery.enqueue(store, recipient='chat-a', text='结果', source_ref='r1')

    first = delivery.fail(store, row['delivery_id'], error='boom', now=clock())
    assert first['state'] == 'retrying'
    assert first['next_retry_at'] > clock(), '失败后要等到退避时间再试'
    assert delivery.due(store, now=clock()) == [], '还没到下一次重试时间'

    for _ in range(delivery.MAX_ATTEMPTS):
        state['now'] = max(state['now'], first['next_retry_at'])
        result = delivery.fail(store, row['delivery_id'], error='boom',
                               now=state['now'])
        if result['state'] == 'dead':
            break
        state['now'] = result['next_retry_at']

    assert result['state'] == 'dead', result
    assert delivery.due(store, now=state['now'] + 10 ** 6) == []
    dead = delivery.dead_letters(store)
    assert [item['delivery_id'] for item in dead] == [row['delivery_id']]
    assert 'boom' in dead[0].get('last_error', '')


def test_dead_letter_can_be_requeued(store):
    state, clock = _clock()
    row = delivery.enqueue(store, recipient='chat-a', text='结果', source_ref='r1')
    for _ in range(delivery.MAX_ATTEMPTS + 1):
        state['now'] += 10 ** 6
        result = delivery.fail(store, row['delivery_id'], now=state['now'])
        if result['state'] == 'dead':
            break

    assert delivery.retry_dead(store, row['delivery_id'], now=state['now'])
    due = delivery.due(store, now=state['now'])
    assert [item['delivery_id'] for item in due] == [row['delivery_id']]
    assert due[0]['attempts'] == 0, '重发应当重新计数，而不是继续累加'


def test_crash_between_send_and_ack_keeps_the_item(store):
    """发送成功但 ack 前崩：下一轮还会发一次（允许重复），但**不重跑任务**。"""
    state, clock = _clock()
    row = delivery.enqueue(store, recipient='chat-a', text='结果',
                           source_ref='turn-9', kind='result', now=clock())
    # 这里不调 ack（模拟崩溃）
    due = delivery.due(store, now=clock() + 1)
    assert [item['delivery_id'] for item in due] == [row['delivery_id']]
    assert due[0]['source_ref'] == 'turn-9', '契约要求能凭 source_ref 找回原结果'


def test_item_limit_is_visible(tmp_path):
    """条数超限：队列有界，且能看见"因为条数被裁了几条"。"""
    store = tmp_path / 'a.json'
    _, clock = _clock()
    for index in range(delivery.MAX_ITEMS + 5):
        delivery.enqueue(store, recipient='chat-a', text=f'结果{index}',
                         source_ref=f'r{index}', now=clock() + index)

    stats = delivery.stats(store, now=clock() + delivery.MAX_ITEMS + 5)
    assert stats['count'] == delivery.MAX_ITEMS
    assert stats['overflow']['items'] == 5, stats['overflow']


def test_byte_limit_is_visible(tmp_path, monkeypatch):
    """字节超限：记录的是 `bytes` 而不是 `items`——两个上限要能分开看。"""
    store = tmp_path / 'b.json'
    _, clock = _clock()
    monkeypatch.setattr(delivery, 'MAX_BYTES', 64)
    for index in range(10):
        delivery.enqueue(store, recipient='chat-a', text='y' * 40,
                         source_ref=f'r{index}', now=clock() + index)

    stats = delivery.stats(store, now=clock() + 10)
    assert stats['bytes'] <= 64, stats
    assert stats['overflow']['bytes'] >= 1, stats['overflow']
    assert stats['overflow']['items'] == 0, (
        f'超字节被记成了条数：{stats["overflow"]}')


def test_ttl_limit_is_visible(tmp_path):
    """超过保留期：不再待发，并计入 `ttl`。"""
    store = tmp_path / 'c.json'
    state, clock = _clock()
    delivery.enqueue(store, recipient='chat-a', text='老结果', source_ref='r1',
                     now=clock())

    later = clock() + delivery.TTL_SECONDS + 1
    stats = delivery.stats(store, now=later)
    assert stats['count'] == 0
    assert stats['overflow']['ttl'] == 1, stats['overflow']
    assert delivery.due(store, now=later) == []


def test_expired_items_are_not_delivered(store):
    state, clock = _clock()
    delivery.enqueue(store, recipient='chat-a', text='老结果', source_ref='r1')
    assert delivery.due(store, now=state['now'] + delivery.TTL_SECONDS + 1) == []


def test_stats_reports_states_and_oldest(store):
    state, clock = _clock()
    delivery.enqueue(store, recipient='chat-a', text='一', source_ref='r1')
    resolved = delivery.enqueue(store, recipient='chat-a', text='二', source_ref='r2')
    delivery.ack(store, resolved['delivery_id'])

    stats = delivery.stats(store, now=clock())
    assert stats['count'] == 1, 'acked 不应留在待发队列里'
    assert stats['by_state'].get('pending') == 1
    assert stats['oldest_at'] > 0
