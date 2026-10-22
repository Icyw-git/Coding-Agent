"""Single-frame reuse must preserve Rich text and styles without prefix splitting."""
import pytest

from ui import harness_app as module
from ui.transcript.model import Block


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
    first = app._render_block(block, 60)
    block.payload['streaming'] = False
    final = app._render_block(block, 60)
    assert len(calls) == 1
    assert final is first
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
