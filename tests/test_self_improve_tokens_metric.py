"""`tokens` 指标必须读对 usage 事件形状（next-design §9.3 R0-b2）。

两个缺陷叠在一起，结果是**永远是 0**：

```python
usage = event.get('usage') or {}                              # ① 事件里没有嵌套 usage
total_in += int(usage.get('prompt_tokens') or 0)              # ② 字段名也不对
```

真实事件是 `{"type": "usage", "prompt": 100, "completion": 20, "cached": 30, …}`
（`harness.py` 用 `store.append('usage', **detail)` 写顶层字段）。所以两次请求本该
算出 180 token / 90 每条，旧代码一律得 0——于是「token 翻了三倍」被判成「无变化」。

验收数字（设计文档给的）：
- 样本 (prompt=100, completion=20) 与 (prompt=50, completion=10) → **总 180、每条 90**；
- `cached` 是 `prompt` 的子集，**不能再加一次**；
- 没有 usage 的事件不增加样本分母。
"""

import json

import self_improve as si


def _session(workdir, events):
    cache = workdir / '.cache' / 'sessions'
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / 'session_probe.jsonl'
    with path.open('w', encoding='utf-8') as handle:
        for item in events:
            handle.write(json.dumps(item, ensure_ascii=False) + '\n')
    return path


def test_reads_top_level_prompt_and_completion(tmp_path):
    _session(tmp_path, [
        {'type': 'usage', 'prompt': 100, 'completion': 20, 'cached': 30,
         'total': 120},
        {'type': 'usage', 'prompt': 50, 'completion': 10, 'cached': 5,
         'total': 60},
    ])
    result = si.measure_tokens(workdir=tmp_path)
    assert result['total_tokens'] == 180, result
    assert result['requests'] == 2, result
    assert result['tokens_per_request'] == 90, result


def test_cached_is_not_added_again(tmp_path):
    """`cached` 是 `prompt` 的子集：加进去会把成本算高。"""
    _session(tmp_path, [{'type': 'usage', 'prompt': 100, 'completion': 20,
                         'cached': 80}])
    assert si.measure_tokens(workdir=tmp_path)['total_tokens'] == 120


def test_events_without_usage_do_not_count(tmp_path):
    """没有 usage 的事件不进分母：否则「多了一批空事件」会显得 token 变少了。"""
    _session(tmp_path, [
        {'type': 'usage', 'prompt': 100, 'completion': 20},
        {'type': 'message', 'message': {'role': 'user', 'content': 'hi'}},
        {'type': 'usage', 'request_ms': 900},          # 空 usage，无量可算
    ])
    result = si.measure_tokens(workdir=tmp_path)
    assert result['requests'] == 1, result
    assert result['tokens_per_request'] == 120, result


def test_no_usage_at_all_is_unknown_not_zero(tmp_path):
    """一条 usage 都没有 → 返回 {}（指标不可测量），不是 0。

    0 会被当成「改完 token 降到 0」，直接进入「变好了」的结论。
    """
    _session(tmp_path, [{'type': 'message', 'message': {'role': 'user',
                                                        'content': 'hi'}}])
    assert si.measure_tokens(workdir=tmp_path) == {}


def test_turn_correlation_is_reported_as_unknown(tmp_path):
    """usage 事件没有 turn 关联 → 只能报「每条请求」，不能冒充「每回合」。"""
    _session(tmp_path, [{'type': 'usage', 'prompt': 100, 'completion': 20}])
    result = si.measure_tokens(workdir=tmp_path)
    assert 'turn_tokens' not in result, result
    assert 'tokens_per_turn' not in result, result


# ============================================================
# R0-b2：改前快照（prepare）—— record 在改完之后跑，快照拍的是"改后"
# ============================================================


def test_prepare_captures_the_pre_image_before_the_edit(tmp_path):
    """`prepare` 在改动**之前**调用：回滚后文件必须回到改动前的内容。

    原来的 `record()` 在 `evaluate()` 里执行，而 evaluate 是改完之后跑的——
    "快照"拍的是改后文件，回滚就等于空操作。这条记录的是改前字节。
    """
    import self_improve_ledger as ledger

    target = tmp_path / 'sample.py'
    target.write_text('original\n', encoding='utf-8')

    prepared = ledger.prepare(tmp_path, ['sample.py'], summary='试改一行',
                              workdir=tmp_path)
    assert prepared['snapshotted'] == ['sample.py'], prepared
    assert prepared['missing'] == [] and prepared['failed'] == []

    target.write_text('changed\n', encoding='utf-8')       # 模拟 agent 改文件

    entry = ledger.finish(tmp_path, prepared, summary='试改一行',
                          workdir=tmp_path)
    assert entry['snapshot_stage'] == 'pre', entry

    restored = ledger.rollback(tmp_path, prepared['change_id'])
    assert restored == ['sample.py'], restored
    assert target.read_text(encoding='utf-8') == 'original\n'


def test_prepare_reports_files_that_did_not_exist(tmp_path):
    """新建的文件没有 pre-image：要如实报出来，而不是算作已快照。"""
    import self_improve_ledger as ledger

    prepared = ledger.prepare(tmp_path, ['brand_new.py'], summary='新增',
                              workdir=tmp_path)
    assert prepared['snapshotted'] == []
    assert prepared['missing'] == ['brand_new.py']


def test_finish_without_prepare_is_marked_post_and_says_so(tmp_path):
    """没用 prepare 就 finish：快照拍的是改后文件，必须**标明**，不能假装能回滚。"""
    import self_improve_ledger as ledger

    target = tmp_path / 'sample.py'
    target.write_text('changed\n', encoding='utf-8')
    entry = ledger.finish(tmp_path, None, summary='没准备', workdir=tmp_path,
                          paths=['sample.py'])
    assert entry['snapshot_stage'] == 'post', entry
    assert 'pre-image' in entry.get('rollback_note', ''), entry


def test_rollback_refuses_to_pretend_for_post_snapshots(tmp_path):
    """改后才拍的快照不能拿来"回滚"——那不是原件，恢复它等于什么都没做。

    返回空列表（而不是"恢复了 1 个"），让界面能说出"这次没有可靠回滚点"。
    """
    import self_improve_ledger as ledger

    target = tmp_path / 'sample.py'
    target.write_text('changed\n', encoding='utf-8')
    entry = ledger.finish(tmp_path, None, summary='没准备', workdir=tmp_path,
                          paths=['sample.py'])
    assert ledger.rollback(tmp_path, entry['change_id']) == []


def test_missing_mechanism_never_runs_the_measurers(tmp_path, monkeypatch):
    """缺机制说明时连测量器都不能跑（§9.3 R0-b2 验收）。

    测量会跑 pytest / ruff（真实子进程、几十秒）。一个本该立即拒绝的调用不应该
    先花两分钟去测量——原来 mechanism 校验确实在测量之前，这条把它钉住。
    """
    import self_improve as si

    calls = []

    def spy(**kwargs):
        calls.append(kwargs)
        return {}

    for name, spec in si.METRICS.items():
        monkeypatch.setitem(si.METRICS, name, {**spec, 'measure': spy})

    session = si.Session(cache_dir=tmp_path, workdir=str(tmp_path), goal='g')
    result = session.evaluate(['skills/a.md'], summary='改了', mechanism='')
    assert result['decision'] == 'discard'
    assert calls == [], f'测量器被调用了 {len(calls)} 次'
