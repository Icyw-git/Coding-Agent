"""端到端：真实 agent_loop → ui_bridge → Textual 渲染。

只替换模型调用；审批、事件、状态与渲染都走真实路径，用来守住
「main 的功能确实进了 UI」这条链路。
"""
import asyncio
from types import SimpleNamespace

import pytest

import harness
import ui_textual


def _usage(prompt=120, completion=6):
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0))


def _text(chat, text):
    parts = [str(chat.render())]
    for child in chat.children:
        parts.append(_text(child, ''))
    return '\n'.join(parts)


@pytest.mark.skipif(not pytest.importorskip('textual', reason='textual missing'),
                    reason='textual missing')
def test_real_agent_loop_streams_into_textual(monkeypatch):
    response = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(role='assistant', content='hello-ui',
                                    tool_calls=None),
            finish_reason='stop')],
        usage=_usage())

    def fake_stream(kwargs, hook):
        hook('hello-')
        hook('ui')
        return response

    monkeypatch.setattr(harness, '_stream_completion', fake_stream)
    monkeypatch.setattr(harness, 'MEMORY_DIR', harness.SESSIONS_DIR / 'mem')
    monkeypatch.setattr(harness, 'MEMORY_INDEX',
                        harness.SESSIONS_DIR / 'mem' / 'MEMORY.md')

    async def scenario():
        app = ui_textual.CCApp([])
        async with app.run_test() as pilot:
            app._user_input_q.put('hi')
            seen_markdown = ''
            for _ in range(120):
                await pilot.pause()
                await asyncio.sleep(0.02)
                text = _text(app.query_one('#chat'), '')
                if 'hello-ui' in text:
                    seen_markdown = text
                    break
            assert 'hello-ui' in seen_markdown, seen_markdown
            app._user_input_q.put(None)   # 结束 agent 线程
            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if not app.is_running:
                    break

    asyncio.run(scenario())


@pytest.mark.skipif(not pytest.importorskip('textual', reason='textual missing'),
                    reason='textual missing')
def test_real_tool_execution_appears_in_textual(monkeypatch):
    tool_call = SimpleNamespace(
        id='c1', type='function',
        function=SimpleNamespace(name='read', arguments='{"path":"a.txt"}'))
    tool_response = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(role='assistant', content=None,
                                    tool_calls=[tool_call]),
            finish_reason='tool_calls')],
        usage=_usage())
    final_response = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(role='assistant', content='done-ui',
                                    tool_calls=None),
            finish_reason='stop')],
        usage=_usage())
    responses = [tool_response, final_response]

    def fake_stream(kwargs, hook):
        return responses.pop(0)

    monkeypatch.setattr(harness, '_stream_completion', fake_stream)
    monkeypatch.setitem(harness.tool_registry, 'read',
                        lambda path: 'file-content')
    monkeypatch.setattr(harness, 'MEMORY_DIR', harness.SESSIONS_DIR / 'mem')
    monkeypatch.setattr(harness, 'MEMORY_INDEX',
                        harness.SESSIONS_DIR / 'mem' / 'MEMORY.md')

    async def scenario():
        app = ui_textual.CCApp([])
        async with app.run_test() as pilot:
            app._user_input_q.put('read it')
            text = ''
            for _ in range(160):
                await pilot.pause()
                await asyncio.sleep(0.02)
                text = _text(app.query_one('#chat'), '')
                if 'done-ui' in text:
                    break
            assert 'done-ui' in text, text
            assert 'read' in text or 'file-content' in text, text
            app._user_input_q.put(None)
            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if not app.is_running:
                    break

    asyncio.run(scenario())
