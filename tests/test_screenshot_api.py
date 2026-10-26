"""回合截图元数据回传与受控下载路由。"""

import importlib.util
import json
from contextlib import ExitStack

import pytest

from harness_api import create_app

fastapi_available = importlib.util.find_spec('fastapi') is not None

pytestmark = pytest.mark.skipif(not fastapi_available,
                                reason='FastAPI is an optional dependency')

PNG = b'\x89PNG\r\n\x1a\n' + b'pixels'
SHA = 'a' * 40


@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient

    with ExitStack() as stack:
        yield lambda app: stack.enter_context(TestClient(app))


def _runtime(tmp_path, *, shots=(SHA,)):
    from agent_runtime import AgentRuntime
    from session_store import SessionEventStore

    def run_turn(messages, session_store=None, **kwargs):
        for sha1 in shots:
            session_store.append(
                'screen_captured', path=f'C:/shots/{sha1}.png', sha1=sha1,
                bytes=len(PNG), screen='primary', width=1024, height=768,
                duration_ms=12)
        return '已截屏'

    return AgentRuntime(run_turn=run_turn,
                        session_store_cls=SessionEventStore,
                        sessions_dir=tmp_path / 'sessions')


def _app(tmp_path, runtime=None):
    return create_app(runtime or _runtime(tmp_path), api_token='t',
                      task_root=tmp_path / 'tasks', workspace=tmp_path,
                      rate_limit=10000)


HEADERS = {'Authorization': 'Bearer t'}


def test_stream_done_carries_screenshots_without_absolute_paths(tmp_path, api_client):
    client = api_client(_app(tmp_path))
    client.post('/v1/sessions', json={'id': 'shot-stream'}, headers=HEADERS)
    response = client.post('/v1/sessions/shot-stream/messages/stream',
                           json={'content': '截图'}, headers=HEADERS)
    assert response.status_code == 200

    done = None
    event = None
    for line in response.text.splitlines():
        if line.startswith('event:'):
            event = line[6:].strip()
        elif line.startswith('data:') and event == 'done':
            done = json.loads(line[5:].strip())
    assert done is not None
    assert done['content'] == '已截屏'
    assert done['screenshots'] == [{
        'name': f'{SHA}.png', 'sha1': SHA, 'bytes': len(PNG),
        'width': 1024, 'height': 768,
    }]
    assert f'/shots/{SHA}' not in json.dumps(done, ensure_ascii=False)


def test_non_stream_response_carries_screenshots(tmp_path, api_client):
    client = api_client(_app(tmp_path))
    client.post('/v1/sessions', json={'id': 'shot-plain'}, headers=HEADERS)
    response = client.post('/v1/sessions/shot-plain/messages',
                           json={'content': '截图'}, headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body['content'] == '已截屏'
    assert [shot['sha1'] for shot in body['screenshots']] == [SHA]


def test_response_omits_screenshots_when_turn_captured_none(tmp_path, api_client):
    client = api_client(_app(tmp_path, _runtime(tmp_path, shots=())))
    client.post('/v1/sessions', json={'id': 'shot-none'}, headers=HEADERS)
    body = client.post('/v1/sessions/shot-none/messages',
                       json={'content': 'hi'}, headers=HEADERS).json()
    assert body['screenshots'] == []


def _seed_screenshot(tmp_path, monkeypatch, name=SHA + '.png'):
    home = tmp_path / 'home'
    monkeypatch.setenv('HARNESS_HOME', str(home))
    directory = home / '.cache' / 'screenshots'
    directory.mkdir(parents=True)
    target = directory / name
    target.write_bytes(PNG)
    return target


def test_download_returns_png_for_existing_session(tmp_path, monkeypatch, api_client):
    _seed_screenshot(tmp_path, monkeypatch, SHA + '.png')
    client = api_client(_app(tmp_path))
    client.post('/v1/sessions', json={'id': 'shot-dl'}, headers=HEADERS)
    response = client.get(f'/v1/sessions/shot-dl/screenshots/{SHA}.png',
                          headers=HEADERS)
    assert response.status_code == 200
    assert response.headers['content-type'] == 'image/png'
    assert response.content == PNG


def test_download_rejects_bad_name_and_missing_files(tmp_path, monkeypatch, api_client):
    _seed_screenshot(tmp_path, monkeypatch, SHA + '.png')
    client = api_client(_app(tmp_path))
    client.post('/v1/sessions', json={'id': 'shot-dl'}, headers=HEADERS)

    assert client.get('/v1/sessions/shot-dl/screenshots/notes.txt',
                      headers=HEADERS).status_code == 400
    assert client.get('/v1/sessions/shot-dl/screenshots/..%2Fsecret.png',
                      headers=HEADERS).status_code in (400, 404)
    missing = 'b' * 40
    assert client.get(f'/v1/sessions/shot-dl/screenshots/{missing}.png',
                      headers=HEADERS).status_code == 404


def test_download_requires_existing_session(tmp_path, monkeypatch, api_client):
    _seed_screenshot(tmp_path, monkeypatch, SHA + '.png')
    client = api_client(_app(tmp_path))
    response = client.get(f'/v1/sessions/nope/screenshots/{SHA}.png',
                          headers=HEADERS)
    assert response.status_code == 404


def test_download_rejects_symlinked_screenshot(tmp_path, monkeypatch, api_client):
    target = _seed_screenshot(tmp_path, monkeypatch, SHA + '.png')
    link = target.with_name('c' * 40 + '.png')
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip('symlink creation is not permitted on this host')
    client = api_client(_app(tmp_path))
    client.post('/v1/sessions', json={'id': 'shot-dl'}, headers=HEADERS)
    response = client.get(f'/v1/sessions/shot-dl/screenshots/{link.name}',
                          headers=HEADERS)
    assert response.status_code == 404
