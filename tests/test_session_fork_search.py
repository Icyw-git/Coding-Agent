"""会话管理增强测试：fork 分叉、跨会话搜索、/resume 直达、API 端点、飞书入口。

运行: python test_session_fork_search.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pytest

import cli_input
import gateway_sim
import harness
from agent_runtime import AgentRuntime
from feishu_gateway import FeishuGateway
from harness_api import create_app
from session_store import SessionEventStore
from test_feishu_gateway import FakeTransport, _sync_asgi_transport
from turn_progress import relative_time

# ---------- SessionEventStore.fork ----------

def test_fork_copies_events_and_leaves_source_intact(tmp_path):
    src = SessionEventStore(tmp_path, session_id='src')
    src.append_message({'role': 'user', 'content': 'hello'})
    src.append_message({'role': 'assistant', 'content': 'hi there'})
    src.append('session_title', title='会话管理重构讨论记录方案')
    src.record_workspace(tmp_path)
    before = len(src.read_events())

    forked = SessionEventStore.fork(src)
    assert len(src.read_events()) == before          # 原会话不动
    assert forked.session_id != 'src'
    history = forked.rebuild_openai_history()
    assert [m['content'] for m in history] == ['hello', 'hi there']
    summary = forked.session_summary()
    assert summary['title'] == '会话管理重构讨论记录方案'
    assert summary['workspace'] == str(tmp_path.resolve())


def test_fork_upto_seq_repairs_orphan_tool_call(tmp_path):
    src = SessionEventStore(tmp_path, session_id='src2')
    src.append_message({'role': 'user', 'content': 'q'})
    src.append_message({'role': 'assistant', 'content': None,
                        'tool_calls': [{'id': 'c1', 'type': 'function',
                                        'function': {'name': 'read',
                                                     'arguments': '{"path":"a"}'}}]})
    cut = src.read_events()[-1]['seq']   # 截断在 assistant(tool_calls) 之后
    forked = SessionEventStore.fork(src, upto_seq=cut)
    history = forked.rebuild_openai_history()   # 不抛 = 孤儿已闭合且历史合法
    assert history[0]['content'] == 'q'


def test_fork_rejects_existing_target(tmp_path):
    src = SessionEventStore(tmp_path, session_id='src')
    src.append_message({'role': 'user', 'content': 'hello'})   # 让目标文件存在
    with pytest.raises(FileExistsError):
        SessionEventStore.fork(src, session_id='src')


def test_read_events_cache_stays_coherent(tmp_path):
    store = SessionEventStore(tmp_path, session_id='cache')
    store.append_message({'role': 'user', 'content': 'one'})
    assert len(store.read_events()) == 1   # 首读并缓存
    store.append_message({'role': 'assistant', 'content': 'two'})
    second = store.read_events()
    assert [e['seq'] for e in second] == [1, 2]   # append 扩展缓存，不丢事件
    second.append({'seq': 99})   # 返回列表是快照：调用方改动不得污染缓存
    assert len(store.read_events()) == 2
    with open(store.path, 'a', encoding='utf-8') as fh:   # 外部写入 → size 失效重读
        fh.write(json.dumps({'v': 1, 'seq': 3, 'session_id': 'cache', 'type': 'session_title',
                             'ts': time.time(), 'title': '外部写入'}, ensure_ascii=False) + '\n')
    third = store.read_events()
    assert len(third) == 3 and third[-1]['title'] == '外部写入'


def test_agent_loop_streams_without_duplicate_print(tmp_path, monkeypatch, capsys):
    import test_context_manage as tcm

    store = SessionEventStore(tmp_path, session_id='stream')
    mem = tmp_path / 'mem'
    monkeypatch.setattr(harness, 'MEMORY_DIR', mem)
    monkeypatch.setattr(harness, 'MEMORY_INDEX', mem / 'MEMORY.md')
    harness.write_memory_file('e2e-mem', 'user', 'desc', 'Body text.')
    fake = tcm.FakeCompletions([
        tcm.make_text_response('[0]'),
        tcm.make_text_response('task done'),
        tcm.make_text_response('[]'),
    ])
    monkeypatch.setattr(harness.client.chat.completions, 'create', fake.create)
    seen = []
    harness.agent_loop([{'role': 'user', 'content': 'do it'}],
                       session_store=store,
                       on_delta=lambda part: seen.append(part))
    harness.wait_for_housekeeping(30)
    assert seen == ['task done']      # 流式增量把回复交付一次（非流式客户端降级）
    out = capsys.readouterr().out
    assert 'task done' not in out     # 最终打印被抑制，无双份


def test_stream_completion_captures_usage(monkeypatch):
    from types import SimpleNamespace as NS

    chunks = [
        NS(choices=[NS(delta=NS(content='你好', tool_calls=None), finish_reason=None)],
           usage=None),
        NS(choices=[], usage=NS(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
    ]
    fake_client = NS(chat=NS(completions=NS(create=lambda **kw: iter(chunks))))
    monkeypatch.setattr(harness, 'client', fake_client)
    parts = []
    resp = harness._stream_completion({'model': 'x'}, parts.append)
    assert parts == ['你好']   # usage-only 收尾块被跳过，不进增量
    assert resp.usage.total_tokens == 15


def test_agent_loop_reports_tokens(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace as NS

    store = SessionEventStore(tmp_path, session_id='usage')
    mem = tmp_path / 'mem'
    monkeypatch.setattr(harness, 'MEMORY_DIR', mem)
    monkeypatch.setattr(harness, 'MEMORY_INDEX', mem / 'MEMORY.md')
    usage = NS(prompt_tokens=100, completion_tokens=20, total_tokens=120,
               prompt_tokens_details=NS(cached_tokens=96))

    def fake_create(**kwargs):
        return NS(choices=[NS(message=NS(role='assistant', content='done', tool_calls=None),
                                finish_reason='stop')], usage=usage)

    monkeypatch.setattr(harness.client.chat.completions, 'create', fake_create)
    harness.agent_loop([{'role': 'user', 'content': '统计 token'}], session_store=store)
    out = capsys.readouterr().out
    assert '[tokens] 本轮 120 · 会话累计 120 · 缓存命中 96%' in out
    assert 'tokens：120' in harness.format_status(store)   # /status 显示会话累计
    assert '缓存命中 96%' in harness.format_status(store)
    # 埋点持久化：usage 事件写入事件流（prompt/completion/cached/cached_seen 明细）
    events = [e for e in store.read_events() if e.get('type') == 'usage']
    assert events and events[0]['prompt'] == 100 and events[0]['completion'] == 20
    assert events[0]['cached'] == 96 and events[0]['cached_seen']
    # 跨重启：新 store 实例从事件聚合，累计与命中率不变
    reopened = SessionEventStore.open(store.path)
    totals = reopened.usage_totals()
    assert totals['total'] == 120 and totals['cached'] == 96 and totals['cached_seen']
    assert 'tokens：120' in harness.format_status(reopened)
    assert '缓存命中 96%' in harness.format_status(reopened)


def test_parse_usage_collects_cached_tokens():
    from types import SimpleNamespace as NS

    # OpenAI 嵌套字段
    openai_usage = NS(prompt_tokens=100, completion_tokens=20, total_tokens=120,
                      prompt_tokens_details=NS(cached_tokens=80))
    detail = harness._parse_usage(openai_usage)
    assert detail == {'prompt': 100, 'completion': 20, 'total': 120,
                      'cached': 80, 'cached_seen': True}
    # DeepSeek 扁平字段
    ds = harness._parse_usage(NS(prompt_tokens=100, completion_tokens=20, total_tokens=120,
                                 prompt_cache_hit_tokens=60))
    assert ds['cached'] == 60 and ds['cached_seen']
    # 聚合口径：cached ⊆ prompt，命中率 = cached / prompt
    bucket = {'prompt': 200, 'cached': 140, 'cached_seen': True}
    assert '70%' in harness._cache_hit_rate_line(bucket)   # 140 / 200
    # 供应商未回传 cached 字段 → 不显示（未知 ≠ 0）
    plain = harness._parse_usage(NS(prompt_tokens=50, completion_tokens=10, total_tokens=60))
    assert plain['cached'] == 0 and not plain['cached_seen']
    assert harness._cache_hit_rate_line(plain) == ''
    # 缺失 usage / 全零 → None（不产生垃圾事件）
    assert harness._parse_usage(None) is None
    assert harness._parse_usage(NS(prompt_tokens=0, completion_tokens=0, total_tokens=0)) is None


def test_usage_totals_aggregates_events(tmp_path):
    store = SessionEventStore(tmp_path, session_id='agg')
    store.append('usage', prompt=100, completion=20, cached=80, total=120, cached_seen=True)
    store.append('message', message={'role': 'user', 'content': 'x'})   # 干扰事件
    store.append('usage', prompt=50, completion=10, total=60)           # 无 cached 字段
    totals = store.usage_totals()
    assert totals == {'prompt': 150, 'completion': 30, 'cached': 80,
                      'total': 180, 'calls': 2, 'cached_seen': True}
    # 空会话 / 无 usage 事件 → 全零且不可观测
    empty = SessionEventStore(tmp_path, session_id='none')
    assert empty.usage_totals() == {'prompt': 0, 'completion': 0, 'cached': 0,
                                    'total': 0, 'calls': 0, 'cached_seen': False}


def test_compaction_tail_survives_replay(tmp_path):
    store = SessionEventStore(tmp_path, session_id='tail')
    store.append_message({'role': 'user', 'content': '早期问题'})
    store.append_compaction('早期摘要', 'manual', tail=[
        {'role': 'user', 'content': '最近的问题'},
        {'role': 'assistant', 'content': '最近的回答'},
    ])
    history = store.rebuild_openai_history()
    assert history[0]['content'] == '[Compacted]\n\n早期摘要'
    assert [m['content'] for m in history[1:]] == ['最近的问题', '最近的回答']
    # 旧事件（无 tail 字段）回放不受影响
    store.append('compaction', summary='纯摘要', strategy='auto',
                 replaces_through_seq=store.last_seq)
    history2 = store.rebuild_openai_history()
    assert history2 == [{'role': 'user', 'content': '[Compacted]\n\n纯摘要'}]


def test_record_compaction_writes_tail(tmp_path):
    store = SessionEventStore(tmp_path, session_id='rec')
    harness._record_compaction(store, [
        {'role': 'user', 'content': '[Reactive compact]\n\n摘要内容'},
        {'role': 'user', 'content': '最近的问题'},
        {'role': 'assistant', 'content': 'tail 回复'},
    ], 'reactive')
    event = store.read_events()[-1]
    assert event['type'] == 'compaction' and event['summary'] == '摘要内容'
    assert event['tail'] == [{'role': 'user', 'content': '最近的问题'},
                             {'role': 'assistant', 'content': 'tail 回复'}]


def test_inject_memories_appends_tail_system_message():
    from agent_core import inject_memories
    messages = [{'role': 'user', 'content': '问题'}]
    out = inject_memories(messages, '<relevant_memories>记忆</relevant_memories>',
                          '系统提示')
    assert out[0] == {'role': 'system', 'content': '系统提示'}
    assert out[1] == {'role': 'user', 'content': '问题'}   # 历史不被改写
    assert out[-1]['role'] == 'system' and '记忆' in out[-1]['content']
    assert messages[0]['content'] == '问题'                # 不污染原列表


def test_last_user_skips_noise_input(tmp_path):
    store = SessionEventStore(tmp_path, session_id='noise')
    store.append_message({'role': 'user', 'content': 'x'})
    store.append_message({'role': 'assistant', 'content': 'ok'})
    store.append_message({'role': 'user', 'content': '检查会话标题过滤'})
    summary = store.session_summary()
    assert summary['last_user'] == '检查会话标题过滤'
    assert summary['preview'] == '检查会话标题过滤'   # preview 与 last_user 同口径
    # 全部是噪声时退回原文，不凭空消失
    only = SessionEventStore(tmp_path, session_id='onlynoise')
    only.append_message({'role': 'user', 'content': 'x'})
    assert only.session_summary()['last_user'] == 'x'


def test_preview_skips_injected_and_falls_back_to_assistant(tmp_path):
    """todo 提醒/后台通知/压缩备注等系统注入不是「最近动态」；整段都是工具
    活动时 preview 兜底为最后一条 assistant 文本（实测旧版 preview 全是
    '<reminder>Update your todos.</reminder>'）。"""
    store = SessionEventStore(tmp_path, session_id='inj')
    store.append_message({'role': 'user', 'content': '继续任务'})
    store.append_message({'role': 'user', 'content': '<reminder>Update your todos.</reminder>'})
    store.append_message({'role': 'user', 'content': '<task_notification>\n <task_id>bg_1</task_id></task_notification>'})
    store.append_message({'role': 'user', 'content': '[Compacted] Conversation history has been summarized.'})
    summary = store.session_summary()
    assert summary['last_user'] == '继续任务'
    assert summary['preview'] == '继续任务'
    # 整段无有效用户输入 → 兜底 assistant 文本
    only_tool = SessionEventStore(tmp_path, session_id='tooly')
    only_tool.append_message({'role': 'user', 'content': '<reminder>Update your todos.</reminder>'})
    only_tool.append_message({'role': 'assistant', 'content': '结论：缓存命中率已修复至 92%。'})
    s2 = only_tool.session_summary()
    assert s2['last_user'] == ''
    assert s2['preview'] == '结论：缓存命中率已修复至 92%。'


# ---------- 跨会话搜索 ----------

def test_search_all_matches_and_snippets(tmp_path):
    a = SessionEventStore(tmp_path, session_id='aaa')
    a.append_message({'role': 'user', 'content': '独特词汇量子猫'})
    b = SessionEventStore(tmp_path, session_id='bbb')
    b.append_message({'role': 'user', 'content': '无关内容'})

    hits = SessionEventStore.search_all(tmp_path, '量子猫')
    assert [row['id'] for row in hits] == ['aaa']
    assert '量子猫' in hits[0]['matched']


def test_search_all_empty_query_returns_empty(tmp_path):
    assert SessionEventStore.search_all(tmp_path, '   ') == []


def test_search_matches_title_only_sessions(tmp_path):
    a = SessionEventStore(tmp_path, session_id='titled')
    a.append_message({'role': 'user', 'content': '聊聊别的话题'})
    a.append_session_title('猫爪杯专题研究')
    hits = SessionEventStore.search_all(tmp_path, '猫爪杯')
    assert [row['id'] for row in hits] == ['titled']
    assert hits[0]['matched'].startswith('主题:')


def test_relative_time():
    now = time.time()
    assert relative_time(None) == '时间未知'
    assert relative_time(now - 30, now) == '刚刚'
    assert relative_time(now - 300, now) == '5 分钟前'
    assert relative_time(now - 7200, now) == '2 小时前'
    assert relative_time(now - 3 * 86400, now) == '3 天前'
    # 跨年：7 天外补年份，避免去年会话被误读成今年
    cross = time.mktime((2025, 12, 1, 10, 0, 0, 0, 0, 0))
    assert relative_time(cross, cross + 45 * 86400).startswith('2025-12-01')


# ---------- CLI ----------

def test_find_command_lists_matches(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    store = SessionEventStore(tmp_path, session_id='findme')
    store.append_message({'role': 'user', 'content': '独一无二的关键词XYZ组合'})

    harness.handle_command('/find 独一无二', [], None)
    out = capsys.readouterr().out
    assert 'findme' in out and 'XYZ' in out


def test_find_without_query_prints_usage(capsys):
    harness.handle_command('/find', [], None)
    assert '用法' in capsys.readouterr().out


def test_rename_command_sets_title(tmp_path, capsys):
    store = SessionEventStore(tmp_path, session_id='rename-me')
    harness.handle_command('/rename 会话主题重命名验证', [], store)
    assert store.session_summary()['title'] == '会话主题重命名验证'
    assert '会话主题已设置' in capsys.readouterr().out


def test_rename_without_title_prints_usage(capsys):
    harness.handle_command('/rename', [], None)
    assert '用法' in capsys.readouterr().out


def test_resume_last_on_current_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)
    monkeypatch.setattr(harness, 'SKILLS_DIR', tmp_path / 'skills')
    current = SessionEventStore(tmp_path, session_id='current')
    current.append_message({'role': 'user', 'content': '当前会话的有效消息内容'})
    commands = iter(['/resume last', '/quit'])
    monkeypatch.setattr(cli_input, 'create_reader', lambda *args: lambda _: next(commands))
    harness.run_repl([], current)
    assert '最近会话就是当前会话' in capsys.readouterr().out


def test_fork_command_validation():
    from cli_commands import command_error
    assert command_error('/fork', 'abc')
    assert command_error('/fork', '') is None
    assert command_error('/find', '')   # 缺关键词
    assert command_error('/rename', '')   # 缺主题


# ---------- API 端点 ----------

@pytest.fixture()
def api_app(tmp_path):
    runtime = AgentRuntime(
        run_turn=lambda messages, **kwargs: 'agent-reply-ok',
        session_store_cls=SessionEventStore,
        sessions_dir=tmp_path / 'sessions',
    )
    return create_app(runtime, api_token='t',
                      task_root=tmp_path / 'tasks', workspace=tmp_path,
                      rate_limit=10000)


def test_session_search_and_fork_endpoints(api_app):
    from fastapi.testclient import TestClient

    client = TestClient(api_app)
    headers = {'Authorization': 'Bearer t'}
    created = client.post('/v1/sessions', json={}, headers=headers)
    assert created.status_code == 201
    sid = created.json()['id']

    sent = client.post(f'/v1/sessions/{sid}/messages',
                       json={'content': '独特词汇量子猫'}, headers=headers)
    assert sent.status_code == 200

    hits = client.get('/v1/sessions/search', params={'q': '量子猫'},
                      headers=headers).json()['sessions']
    assert any(row['id'] == sid for row in hits)

    forked = client.post(f'/v1/sessions/{sid}/fork', json={}, headers=headers)
    assert forked.status_code == 200
    new_id = forked.json()['session_id']
    assert new_id != sid

    history = client.post(f'/v1/sessions/{new_id}/messages',
                          json={'content': '分叉会话继续'}, headers=headers)
    assert history.status_code == 200   # 分叉会话可直接继续对话


def test_session_rename_endpoint(api_app):
    from fastapi.testclient import TestClient

    client = TestClient(api_app)
    headers = {'Authorization': 'Bearer t'}
    sid = client.post('/v1/sessions', json={}, headers=headers).json()['id']
    renamed = client.post(f'/v1/sessions/{sid}/title',
                          json={'title': '自定义主题'}, headers=headers)
    assert renamed.status_code == 200
    assert renamed.json()['title'] == '自定义主题'
    rows = client.get('/v1/sessions', headers=headers).json()['sessions']
    assert any(row['id'] == sid and row['title'] == '自定义主题' for row in rows)
    assert client.post(f'/v1/sessions/{sid}/title', json={'title': ' '},
                       headers=headers).status_code == 400   # 空标题
    assert client.post('/v1/sessions/missing-session/title', json={'title': '不存在会话的主题'},
                       headers=headers).status_code == 404


# ---------- 飞书入口 ----------

def test_feishu_resume_last_fork_and_find(api_app, tmp_path):
    api = gateway_sim.GatewayClient(
        'http://test', 't', transport=_sync_asgi_transport(api_app))
    fake = FakeTransport()
    gw = FeishuGateway(api, transport=fake,
                       bindings_path=tmp_path / 'bindings.json')
    try:
        sid = api.create_session()
        gw.bindings['c1'] = sid
        gw.on_message('c1', '关于猫爪杯的历史')   # 内容落盘到会话

        # /resume 无参：最近会话就是当前绑定 → 如实提示；解绑后再直达 → 真切换
        fake.sent.clear()
        gw.on_message('c1', '/resume')
        assert any('已绑定该会话' in text for _, text in fake.sent)
        gw.on_message('c1', '/quit')
        gw.on_message('c1', '/resume')
        assert any('已切换到会话' in text for _, text in fake.sent)

        # /fork → 切换到分叉出的新会话
        gw.on_message('c1', '/fork')
        forked_id = gw.bindings['c1']
        assert forked_id != sid
        assert any(f'已分叉出新会话 {forked_id}' in text for _, text in fake.sent)

        # /find → 跨会话搜索命中内容
        fake.sent.clear()
        gw.on_message('c1', '/find 猫爪杯')
        assert any('猫爪杯' in text for _, text in fake.sent)

        # /rename → 设置绑定会话的主题
        gw.on_message('c1', '/rename 猫爪杯研究')
        assert any('会话主题已设置为：猫爪杯研究' in text for _, text in fake.sent)
    finally:
        api.close()
