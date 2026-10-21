"""view_image 与多模态消息通道：引用落盘、请求前展开、附件消息顺序。"""
import base64
import json
from pathlib import Path

import pytest

import agent_core
import harness
import test_context_manage as tcm
import tools
from session_store import SessionEventStore

PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8'
    'z8BQDwAFgwJ/lK3Z2wAAAABJRU5ErkJggg==')


def _png_size(width: int, height: int) -> bytes:
    return (b'\x89PNG\r\n\x1a\n' + (13).to_bytes(4, 'big') + b'IHDR'
            + width.to_bytes(4, 'big') + height.to_bytes(4, 'big')
            + b'\x08\x06\x00\x00\x00')


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    return tmp_path


def test_view_image_returns_reference_and_caches_file(tmp_path):
    image = tmp_path / 'pic.png'
    image.write_bytes(PNG)
    result = tools.run_view_image('pic.png')
    assert isinstance(result, tools.ImageResult)
    assert 'pic.png' in result.text
    assert len(result.images) == 1
    ref = result.images[0]
    assert ref['type'] == 'harness_image'
    assert ref['mime'] == 'image/png'
    assert ref['bytes'] == len(PNG)
    assert ref['tokens'] >= 1
    cached = Path(ref['path'])
    assert cached.is_file()
    assert cached.read_bytes() == PNG


def test_view_image_rejects_missing_and_invalid_files(tmp_path):
    missing = tools.run_view_image('nope.png')
    assert isinstance(missing, str) and 'not found' in missing

    text = tmp_path / 'notes.txt'
    text.write_text('not an image', encoding='utf-8')
    invalid = tools.run_view_image('notes.txt')
    assert isinstance(invalid, str) and 'unsupported image format' in invalid


def test_view_image_enforces_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, '_IMAGE_MAX_BYTES', 16)
    image = tmp_path / 'big.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'x' * 32)
    result = tools.run_view_image('big.png')
    assert isinstance(result, str) and 'too large' in result


def test_view_image_denies_secret_path(tmp_path):
    secret = tmp_path / '.env'
    secret.write_bytes(PNG)
    result = tools.run_view_image('.env')
    assert isinstance(result, str)
    assert 'Error' in result
    assert 'harness_image' not in result


def test_expand_image_parts_builds_data_uri_without_mutating_history(tmp_path):
    image = tmp_path / 'pic.png'
    image.write_bytes(PNG)
    result = tools.run_view_image('pic.png')
    attachment = agent_core.image_attachment_message(result.images)
    history = [{'role': 'user', 'content': '看看'},
               attachment,
               {'role': 'assistant', 'content': '好的'}]

    request = agent_core.expand_image_parts(history)

    assert request[0] is history[0] and request[2] is history[2]
    parts = request[1]['content']
    assert parts[0]['type'] == 'text' and '[image attachment]' in parts[0]['text']
    assert parts[1]['type'] == 'image_url'
    assert parts[1]['image_url']['url'].startswith('data:image/png;base64,')
    assert history[1]['content'][1]['type'] == 'harness_image'   # 历史只留引用


def test_expand_image_parts_degrades_when_cache_is_gone(tmp_path):
    attachment = agent_core.image_attachment_message([{
        'type': 'harness_image', 'path': str(tmp_path / 'gone.png'),
        'name': 'gone.png', 'mime': 'image/png'}])
    expanded = agent_core.expand_image_parts([attachment])
    assert expanded[0]['content'][1] == {
        'type': 'text', 'text': '[image unavailable: gone.png]'}


def test_image_dimensions_read_headers_without_pillow():
    assert agent_core.image_dimensions(_png_size(640, 480)) == (640, 480)
    gif = b'GIF89a' + (320).to_bytes(2, 'little') + (200).to_bytes(2, 'little')
    assert agent_core.image_dimensions(gif) == (320, 200)
    assert agent_core.image_dimensions(b'not an image') is None


def test_estimate_image_tokens_uses_model_family(monkeypatch):
    monkeypatch.setenv('HARNESS_IMAGE_TOKEN_ESTIMATE', '1')
    large = _png_size(1024, 1024)
    # GPT-4o 高细节：缩到 768² → 4 个 512 瓦片 → 85 + 680
    assert agent_core.estimate_image_tokens(large, 'gpt-4o') == 765
    # Claude：长边 1568 内，ceil(1024²/750)
    assert agent_core.estimate_image_tokens(large, 'claude-sonnet-4') == 1399
    # Gemini：2×2 个 768 瓦片
    assert agent_core.estimate_image_tokens(large, 'gemini-2.5-pro') == 1032
    # 已知尺寸走模型公式；固定值只托底，不能把估算压得更低
    small = _png_size(64, 64)
    assert agent_core.estimate_image_tokens(small, 'gpt-4o') == 255
    monkeypatch.setenv('HARNESS_IMAGE_TOKEN_ESTIMATE', '2000')
    assert agent_core.estimate_image_tokens(small, 'gpt-4o') == 2000
    # DeepSeek 官方只给每图上限，没有公开瓦片公式：按 1024 保守计
    monkeypatch.setenv('HARNESS_IMAGE_TOKEN_ESTIMATE', '1')
    assert agent_core.estimate_image_tokens(small, 'deepseek-flash') == 1024
    assert agent_core.estimate_image_tokens(
        large, 'deepseek-v4-flash-vision-exp') == 1024


def test_estimate_image_tokens_env_fallback(monkeypatch):
    monkeypatch.setenv('HARNESS_IMAGE_TOKEN_ESTIMATE', '500')
    assert agent_core.estimate_image_tokens(b'unknown', 'gpt-4o') == 500
    assert agent_core.estimate_image_tokens(_png_size(0, 0), 'gpt-4o') == 500
    monkeypatch.setenv('HARNESS_IMAGE_TOKEN_ESTIMATE', 'bad')
    assert agent_core.estimate_image_tokens(b'unknown') == (
        agent_core.IMAGE_TOKEN_ESTIMATE)


def test_image_token_cost_prefers_reference_estimate(monkeypatch):
    ref = {'type': 'harness_image', 'path': 'x', 'tokens': 1234}
    assert agent_core._image_token_cost([{'role': 'user', 'content': [ref]}]) == (
        1234, 1)
    monkeypatch.setenv('HARNESS_IMAGE_TOKEN_ESTIMATE', '300')
    legacy = {'type': 'harness_image', 'path': 'x'}
    assert agent_core._image_token_cost([{'role': 'user', 'content': [legacy]}]) == (
        300, 1)
    assert agent_core._image_token_cost([{'role': 'user', 'content': 'hi'}]) == (0, 0)


def test_estimate_size_does_not_count_base64_payload(tmp_path):
    attachment = agent_core.image_attachment_message([{
        'type': 'harness_image', 'path': str(tmp_path / 'x.png'),
        'name': 'x.png'}])
    attachment['content'].append({
        'type': 'image_url',
        'image_url': {'url': 'data:image/png;base64,' + 'A' * 400_000}})
    # 40 万字符的 base64 若按字符估算会超过 10 万 token；这里只算图片定额
    assert agent_core.estimate_size([attachment]) < 5_000


def test_session_summary_ignores_image_attachment(tmp_path):
    store = SessionEventStore(tmp_path, session_id='img')
    store.append_message({'role': 'user', 'content': '真正的问题'})
    attachment = agent_core.image_attachment_message([{
        'type': 'harness_image', 'path': 'x', 'name': 'x.png'}])
    store.append_message(attachment)
    assert store.session_summary()['last_user'] == '真正的问题'
    from session_store import is_injected_user_content
    assert is_injected_user_content(attachment['content'])


def test_agent_loop_attaches_image_after_tool_result(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'load_memories', lambda messages: '')
    monkeypatch.setattr(harness, 'extract_memories', lambda pre: None)
    monkeypatch.setattr(harness, 'consolidate_memories', lambda: None)
    monkeypatch.setattr(harness, 'generate_session_title', lambda messages: 't')
    monkeypatch.setattr(harness, '_post_turn_housekeeping', lambda *a, **k: None)

    image = tmp_path / 'pic.png'
    image.write_bytes(PNG)
    store = SessionEventStore(tmp_path / 'sessions', session_id='img')
    fake = tcm.FakeCompletions([
        tcm.make_tool_response([
            ('c1', 'view_image', json.dumps({'path': 'pic.png'}))]),
        tcm.make_text_response('seen'),
    ])
    monkeypatch.setattr(harness.client.chat.completions, 'create', fake.create)

    result = harness.agent_loop(
        [{'role': 'user', 'content': '看看这张图'}], session_store=store)

    assert result == 'seen'
    request = fake.calls[1]
    tool_index = next(i for i, m in enumerate(request)
                      if m.get('role') == 'tool')
    attachment = request[tool_index + 1]
    assert attachment['role'] == 'user'
    assert attachment['content'][1]['type'] == 'image_url'
    assert attachment['content'][1]['image_url']['url'].startswith(
        'data:image/png;base64,')
    # 事件流/历史只存引用，不存 base64（转录与恢复不会被图片撑爆）
    events = store.read_events()
    serialized = json.dumps(events, ensure_ascii=False)
    assert 'harness_image' in serialized
    assert 'base64' not in serialized
