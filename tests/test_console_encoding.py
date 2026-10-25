"""GBK 控制台/日志遇到 emoji 不能把回合打死（2026-10-25 真实故障）。"""
import io
import sys

import console


def _gbk_stdout():
    return io.TextIOWrapper(io.BytesIO(), encoding='gbk', newline='')


def test_harden_stream_makes_gbk_print_survive_emoji():
    stream = _gbk_stdout()
    with_exc = None
    try:
        stream.write('emoji 😊\n')
    except UnicodeEncodeError as exc:   # 未加固前就是这个异常打到用户界面
        with_exc = exc
    assert with_exc is not None

    console.harden_stream(stream)
    stream.write('emoji 😊\n')          # 不再抛
    stream.flush()
    value = stream.buffer.getvalue().decode('gbk')
    assert 'emoji' in value
    assert r'\U0001f60a' in value       # 转义写法，日志里还认得出是什么


def test_harden_stdio_covers_stdout_and_stderr(monkeypatch):
    out, err = _gbk_stdout(), _gbk_stdout()
    monkeypatch.setattr(sys, 'stdout', out)
    monkeypatch.setattr(sys, 'stderr', err)

    console.harden_stdio()

    print('ok 😊')
    print('err 😊', file=sys.stderr)
    out.flush()
    err.flush()
    assert r'\U0001f60a' in out.buffer.getvalue().decode('gbk')
    assert r'\U0001f60a' in err.buffer.getvalue().decode('gbk')


def test_harden_stream_tolerates_streams_without_reconfigure():
    class Plain:
        def __init__(self):
            self.written = []

        def write(self, text):
            self.written.append(text)

    stream = Plain()
    assert console.harden_stream(stream) is stream
    stream.write('😊')
    assert stream.written == ['😊']


def test_hook_debug_print_survives_emoji_output(monkeypatch):
    """hooks 的 [HOOK] 调试行会打印工具输出，曾经把 402 之后的回合再打死一次。"""
    import hooks

    out = _gbk_stdout()
    monkeypatch.setattr(sys, 'stdout', out)
    console.harden_stdio()

    hooks.log_hook('read', {}, 'file content with emoji 😊')
    out.flush()
    assert 'file content with emoji' in out.buffer.getvalue().decode('gbk')


def test_turn_still_succeeds_when_answer_has_emoji(tmp_path, monkeypatch):
    """端到端：stdout 是 GBK 时，回合里打印 emoji 不能变成失败。

    真实故障：hooks 的 `[HOOK] ... -> {output[:100]}` 打印了含 😊 的工具输出，
    在 API 进程（stdout 重定向到日志，GBK）抛 UnicodeEncodeError，回合被判失败。
    """
    from fastapi.testclient import TestClient

    from agent_runtime import AgentRuntime
    from harness_api import create_app
    from session_store import SessionEventStore

    out = _gbk_stdout()
    monkeypatch.setattr(sys, 'stdout', out)

    def run_turn(messages, **kwargs):
        print('[HOOK] read({}) -> file content 😊')
        return '回答里也有 emoji 😊'

    runtime = AgentRuntime(run_turn=run_turn,
                           session_store_cls=SessionEventStore,
                           sessions_dir=tmp_path / 'sessions')
    app = create_app(runtime, api_token='t', task_root=tmp_path / 'tasks',
                     workspace=tmp_path, rate_limit=1000)
    client = TestClient(app)
    headers = {'Authorization': 'Bearer t'}
    session_id = client.post('/v1/sessions', json={}, headers=headers).json()['id']
    response = client.post(f'/v1/sessions/{session_id}/messages',
                           json={'content': 'hi'}, headers=headers)
    assert response.status_code == 200, response.text
    assert 'emoji 😊' in response.json()['content']
