"""请求去重存储（R1-a 其一）。容量结论见 PERFORMANCE_PROFILE.md。

调研结论决定了形状：**append-only JSONL + 内存索引**。单文件 JSON 在 1 万条时一次
读-改-写要 50ms，而它要在每条入站请求的热路径上（与 R1-b 修掉的"每次判定都读授权
账本"同一个错误形状）。

语义契约（§9.4 R1-a）：

- 同 id 同内容 → 返回**原关联**（`duplicate`），不追加用户消息、不再执行；
- 同 id 异内容 → `conflict`，不追加；
- 过保留期 → 视为新请求（**不承诺永久去重**）；
- 满容量 → 拒绝新的带 id 受理并说明原因；**未过期的记录不能为腾位提前淘汰**；
- 无 id 的旧客户端 → 不参与去重。
"""

import threading

import pytest

import request_dedup


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(request_dedup, 'STORE_PATH', tmp_path / 'requests.jsonl')
    request_dedup.reset_cache()
    return request_dedup


def _fp(text='hello'):
    return request_dedup.fingerprint(text=text, workspace='/tmp/ws', mode='followup')


def test_same_id_same_content_returns_the_original_binding(store):
    first = store.claim('r1', fingerprint=_fp(), session_id='s1', origin='feishu')
    assert first['status'] == 'accepted'
    assert first['turn_id'] == ''      # 还没绑定回合

    store.bind('r1', turn_id='turn-1')
    again = store.claim('r1', fingerprint=_fp(), session_id='s1', origin='feishu')

    assert again['status'] == 'duplicate'
    assert again['turn_id'] == 'turn-1', '重复请求要能查到原回合'


def test_same_id_different_content_is_a_conflict(store):
    store.claim('r1', fingerprint=_fp('first'), session_id='s1', origin='feishu')
    other = store.claim('r1', fingerprint=_fp('second'), session_id='s1',
                        origin='feishu')
    assert other['status'] == 'conflict'
    assert other['reason'], other


def test_requests_without_an_id_are_not_deduplicated(store):
    assert store.claim('', fingerprint=_fp(), session_id='s1',
                       origin='feishu')['status'] == 'passthrough'
    assert store.claim('', fingerprint=_fp(), session_id='s1',
                       origin='feishu')['status'] == 'passthrough'


def test_expired_records_are_treated_as_new(store, monkeypatch):
    store.claim('r1', fingerprint=_fp(), session_id='s1', origin='feishu')
    # 用**确定的时间偏移**判断过期，而不是把保留期设成 0：
    # Windows 上 time.time() 的精度约 15ms，两条记录的 created_at 可能完全相等，
    # `created_at < now - 0` 就不成立——那种写法会随机器不同而飘。
    real_now = store._now()
    monkeypatch.setattr(store, '_now',
                        lambda: real_now + store.RETENTION_SECONDS + 1)
    store.reset_cache()
    again = store.claim('r1', fingerprint=_fp(), session_id='s1', origin='feishu')
    assert again['status'] == 'accepted', '过期的 id 不承诺永久去重'


def test_capacity_rejects_new_ids_but_keeps_old_ones(store, monkeypatch):
    monkeypatch.setattr(store, 'MAX_RECORDS', 3)
    for index in range(3):
        assert store.claim(f'r{index}', fingerprint=_fp(),
                           session_id='s1', origin='feishu')['status'] == 'accepted'

    rejected = store.claim('r-new', fingerprint=_fp(), session_id='s1',
                           origin='feishu')
    assert rejected['status'] == 'full'
    assert 'capacity' in rejected['reason'] or '满' in rejected['reason']

    # 未过期的记录不能为腾位被淘汰：已受理的仍能查到原关联
    assert store.claim('r0', fingerprint=_fp(), session_id='s1',
                       origin='feishu')['status'] == 'duplicate'


def test_state_survives_a_restart(store):
    store.claim('r1', fingerprint=_fp(), session_id='s1', origin='feishu')
    store.bind('r1', turn_id='turn-9')

    store.reset_cache()             # 模拟进程重启：丢掉内存索引，只留文件
    again = store.claim('r1', fingerprint=_fp(), session_id='s1', origin='feishu')
    assert again['status'] == 'duplicate'
    assert again['turn_id'] == 'turn-9'


def test_concurrent_claims_of_the_same_id_only_accept_once(store):
    """并发：同 id 同内容同时到达，只能有一个 accepted。"""
    results = []
    barrier = threading.Barrier(4, timeout=10)

    def worker():
        barrier.wait()
        results.append(store.claim('race', fingerprint=_fp(), session_id='s1',
                                   origin='feishu')['status'])

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results).count('accepted') == 1, results


def test_compaction_drops_expired_rows_only(store):
    store.claim('old', fingerprint=_fp(), session_id='s1', origin='feishu')
    store.claim('new', fingerprint=_fp(), session_id='s1', origin='feishu')

    live = store.compact(now=store._now() + 1)     # 什么都没过期
    assert live == 2
    lines = store.STORE_PATH.read_text(encoding='utf-8').splitlines()
    assert len([line for line in lines if line.strip()]) == 2

    assert store.compact(now=store._now() + store.RETENTION_SECONDS + 1) == 0
    assert store.STORE_PATH.read_text(encoding='utf-8').strip() == ''
