"""Single-frame reuse must preserve Rich text and styles without prefix splitting."""
import pytest

from ui import harness_app as module
from ui.transcript.model import Block


def test_labeled_code_uses_plain_styles_while_streaming_and_rich_styles_at_end():
    app = module.HarnessApp([])
    source = '```python\ndef answer(value):\n    return value + 1\n```'
    block = Block('code-stream', 'assistant', {'text': source, 'streaming': True})

    provisional = app._render_block(block, 60)
    expected_final = module._render_markdown(source, 60)
    assert [line.plain for line in provisional] == [line.plain for line in expected_final]
    assert any(line.spans for line in expected_final)
    assert not app._stream_render_cache

    block.payload['streaming'] = False
    final = app._render_block(block, 60)
    assert [(line.plain, line.spans) for line in final] == [
        (line.plain, line.spans) for line in expected_final]
    assert any(line.spans for line in final)
    assert app._stream_render_cache is None

    block.payload['streaming'] = True
    resized = app._render_block(block, 24)
    assert [line.plain for line in resized] == [
        line.plain for line in module._render_markdown(source, 24)]
    block.payload['streaming'] = False
    assert [(line.plain, line.spans) for line in app._render_block(block, 24)] == [
        (line.plain, line.spans) for line in module._render_markdown(source, 24)]


@pytest.mark.parametrize('source', [
    '~~~javascript\nconst answer = 42;\n~~~',
    '```python linenos\nprint(1)\n```',
    '```\nplain text\n```',
    'paragraph\n\n```python\nprint(1)',
    '```python`invalid\nprint(1)\n```',
])
def test_streaming_code_fence_transform_preserves_plain_layout(source):
    provisional_source, changed = module._streaming_markdown_source(source)
    assert changed == (('python' in source or 'javascript' in source)
                       and '`invalid' not in source)
    for width in (24, 60, 100):
        actual = module._render_markdown(provisional_source, width)
        expected = module._render_markdown(source, width)
        assert [line.plain for line in actual] == [line.plain for line in expected]


@pytest.mark.parametrize('source', [
    '**bold** 中文 🙂',
    '- first\n  - nested\n\nnext',
    '```python\nprint(1)\n```',
    '```python\nprint(1)',
    '| A | B |\n|---|---|\n| 1 | 2 |',
    '[label][ref]\n\n[ref]: https://example.org',
    '> quotation\n\nnext',
])
def test_end_reuses_exact_last_frame(monkeypatch, source):
    app = module.HarnessApp([])
    block = Block('stream', 'assistant', {'text': source, 'streaming': True})
    expected = module._render_markdown(source, 60)
    calls = []
    original = module._render_markdown

    def counted(text, width):
        calls.append((text, width))
        return original(text, width)

    monkeypatch.setattr(module, '_render_markdown', counted)
    app._render_block(block, 60)
    block.payload['streaming'] = False
    final = app._render_block(block, 60)
    assert len(calls) == (2 if module._streaming_markdown_source(source)[1] else 1)
    assert [(x.plain, x.spans) for x in final] == [(x.plain, x.spans) for x in expected]
    assert app._stream_render_cache is None


def test_changed_text_width_or_block_rerenders(monkeypatch):
    app = module.HarnessApp([])
    calls = []
    original = module._render_markdown

    def counted(text, width):
        calls.append((text, width))
        return original(text, width)

    monkeypatch.setattr(module, '_render_markdown', counted)
    block = Block('stream', 'assistant', {'text': '**hello', 'streaming': True})
    app._render_block(block, 60)
    block.payload['text'] += '**'
    app._render_block(block, 60)
    app._render_block(block, 40)
    block.id = 'other'
    app._render_block(block, 40)
    assert len(calls) == 4
    block.payload['text'] += '!'
    block.payload['streaming'] = False
    final = app._render_block(block, 40)
    assert final == original('**hello**!', 40)
    assert len(calls) == 5
    assert app._stream_render_cache is None


def test_oversized_frame_is_not_cached():
    app = module.HarnessApp([])
    block = Block('stream', 'assistant', {'text': 'a' * 100_001, 'streaming': True})
    app._render_block(block, 100)
    assert app._stream_render_cache is None


def test_history_reload_releases_last_frame():
    app = module.HarnessApp([])
    app._render_block(Block('s', 'assistant', {'text': 'old', 'streaming': True}), 60)
    assert app._stream_render_cache is not None
    app._load_history([])
    assert app._stream_render_cache is None
