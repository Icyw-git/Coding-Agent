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
