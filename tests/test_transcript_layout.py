"""Transcript 块模型与行布局：缓存、切片、命中测试。"""
from ui.transcript import Block, ConversationModel, TranscriptLayout


def _model():
    model = ConversationModel()
    model.append('user', {'text': 'hello'}, block_id='u1')
    model.append('assistant', {'text': 'world'}, block_id='a1')
    return model


def _render(block: Block, width: int):
    text = block.payload.get('text', '')
    return [line for line in str(text).splitlines()] or ['']


def test_append_delta_and_expand_bump_version():
    model = ConversationModel()
    block = model.append('assistant', {'text': 'a'})
    assert block.version == 0

    model.append_text(block.id, 'b')
    assert model.get(block.id).payload['text'] == 'ab'
    assert model.get(block.id).version == 1

    model.set_expanded(block.id, True)
    assert model.get(block.id).expanded
    assert model.get(block.id).version == 2
    model.set_expanded(block.id, True)   # 无变化不 bump
    assert model.get(block.id).version == 2
    assert len(model) == 1


def test_layout_reuses_unchanged_blocks():
    model = _model()
    layout = TranscriptLayout(_render)

    layout.update(model, 80)
    first_calls = layout.render_calls
    assert first_calls == 2
    assert [entry.block_id for entry in layout.entries()] == ['u1', 'a1']

    layout.update(model, 80)
    assert layout.render_calls == first_calls      # 无变化不重绘

    model.append_text('a1', '!')
    layout.update(model, 80)
    assert layout.render_calls == first_calls + 1  # 只有 a1 重绘


def test_layout_width_change_rerenders_everything():
    model = _model()
    layout = TranscriptLayout(_render)
    layout.update(model, 80)
    before = layout.render_calls

    layout.update(model, 40)

    assert layout.render_calls == before + 2
    assert layout.width == 40


def test_visible_slices_with_overscan_and_blanks():
    model = ConversationModel()
    for index in range(3):
        model.append('user', {'text': f'l{index}'}, block_id=f'b{index}')
    layout = TranscriptLayout(_render)
    layout.update(model, 80)

    assert layout.visible(0, 2) == ['l0', 'l1']
    assert layout.visible(1, 3) == ['l1', 'l2']
    assert layout.visible(2, 5, overscan=1) == ['l1', 'l2']
    assert layout.max_scroll(2) == 1


def test_block_at_maps_lines_to_blocks():
    model = ConversationModel()
    model.append('user', {'text': 'a\nb'}, block_id='first')
    model.append('user', {'text': 'c'}, block_id='second')
    layout = TranscriptLayout(_render)
    layout.update(model, 80)

    assert layout.block_at(0) == 'first'
    assert layout.block_at(1) == 'first'
    assert layout.block_at(2) == 'second'
    assert layout.block_at(3) is None
    assert layout.block_at(-1) is None


def test_virtual_transcript_renders_only_viewport():
    import asyncio

    from textual.app import App, ComposeResult

    from ui.transcript import ConversationModel, TranscriptLayout
    from ui.transcript.view import VirtualTranscript

    class Demo(App):
        def __init__(self):
            super().__init__()
            self.model = ConversationModel()
            for index in range(200):
                self.model.append('user', {'text': f'row-{index}'},
                                  block_id=f'b{index}')
            self.doc_layout = TranscriptLayout(
                lambda block, width: [block.payload['text']])
            self.view = VirtualTranscript(self.model, self.doc_layout,
                                          follow_end=False)

        def compose(self) -> ComposeResult:
            yield self.view

    async def scenario():
        app = Demo()
        async with app.run_test(size=(40, 10)) as pilot:
            await pilot.pause()
            view = app.view
            assert view.content_line_count == 200
            assert view.virtual_size.height == 200

            calls = app.doc_layout.render_calls
            line = view.render_line(0)
            assert 'row-0' in str(line.text)

            view.scroll_end(animate=False)
            await pilot.pause()
            await pilot.pause()
            assert app.doc_layout.render_calls == calls   # 滚动不重算布局
            assert view.scroll_offset.y > 0
            top = view.scroll_offset.y
            assert f'row-{top}' in str(view.render_line(0).text)
            app._exiting = True

    asyncio.run(scenario())


def test_line_cache_evicts_offscreen_blocks_but_keeps_heights():
    from ui.transcript import ConversationModel, TranscriptLayout

    model = ConversationModel()
    for index in range(5):
        model.append('user', {'text': f'block-{index}\nsecond'}, block_id=f'b{index}')
    layout = TranscriptLayout(
        lambda block, width: str(block.payload['text']).splitlines(),
        line_cache_limit=2)
    layout.update(model, 80)

    assert layout.total_height == 10
    assert layout.entries()[0].lines is None       # 只保留最近/有限的块
    assert layout.line_at(0) == 'block-0'
    assert layout.block_at(9) == 'b4'
    assert [line for line in layout.visible(0, 4)] == [
        'block-0', 'second', 'block-1', 'second']


def test_stream_update_keeps_starts_and_total_height():
    from ui.transcript import ConversationModel, TranscriptLayout

    model = ConversationModel()
    for index in range(3):
        model.append('assistant', {'text': 'a\nb\nc'}, block_id=f'b{index}')
    layout = TranscriptLayout(
        lambda block, width: str(block.payload['text']).splitlines())
    layout.update(model, 80)

    model.append_text('b2', 'x')
    layout.update(model, 80)

    assert layout.total_height == 9
    assert [entry.start for entry in layout.entries()] == [0, 3, 6]
