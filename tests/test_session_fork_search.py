"""会话管理增强测试：fork 分叉、跨会话搜索、/resume 直达、API 端点、飞书入口。

运行: python test_session_fork_search.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pytest

import gateway_sim
import harness
import feishu_gateway
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


def test_last_user_skips_noise_input(tmp_path):
    store = SessionEventStore(tmp_path, session_id='noise')
    store.append_message({'role': 'user', 'content': 'x'})
    store.append_message({'role': 'assistant', 'content': 'ok'})
    store.append_message({'role': 'user', 'content': '检查会话标题过滤'})
    assert store.session_summary()['last_user'] == '检查会话标题过滤'
    # 全部是噪声时退回原文，不凭空消失
    only = SessionEventStore(tmp_path, session_id='onlynoise')
    only.append_message({'role': 'user', 'content': 'x'})
    assert only.session_summary()['last_user'] == 'x'


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


def test_relative_time():
    now = time.time()
    assert relative_time(None) == '时间未知'
    assert relative_time(now - 30, now) == '刚刚'
    assert relative_time(now - 300, now) == '5 分钟前'
    assert relative_time(now - 7200, now) == '2 小时前'
    assert relative_time(now - 3 * 86400, now) == '3 天前'


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

        # /resume 无参 → 直达最近会话
        fake.sent.clear()
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
