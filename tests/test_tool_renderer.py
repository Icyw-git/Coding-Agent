"""工具块渲染：diff 着色、预览行数、展开。"""
from ui.renderers.tools import tool_view


def test_tool_view_renders_state_summary_and_diff():
    payload = {
        'name': 'edit', 'args': {'path': 'src/app.py'},
        'state': 'complete', 'duration_ms': 120,
        'diff': '--- src/app.py\n+++ src/app.py\n@@ -1 +1 @@\n-old\n+new',
        'output': 'Edited src/app.py',
    }

    text = tool_view(payload).plain

    assert '▸ edit' in text
    assert 'src/app.py' in text
    assert 'complete' in text and '0.1s' in text
    assert '-old' in text and '+new' in text
    assert 'Edited' in text


def test_tool_view_collapses_output_until_expanded():
    output = '\n'.join(f'line {i}' for i in range(20))
    collapsed = tool_view({'name': 'bash', 'args': {'command': 'pytest'},
                           'state': 'complete', 'output': output}).plain
    expanded = tool_view({'name': 'bash', 'args': {'command': 'pytest'},
                          'state': 'complete', 'output': output,
                          'expanded': True}).plain

    assert 'line 9' in collapsed and 'line 10' not in collapsed
    assert 'more lines' in collapsed
    assert 'line 19' in expanded and 'more lines' not in expanded


def test_tool_output_ansi_is_stripped():
    from ui.renderers.tools import tool_view

    rendered = str(tool_view({
        'name': 'bash', 'args': {'command': 'x'},
        'state': 'complete', 'output': '\x1b[31mred\x1b[0m done',
    }))
    assert 'red done' in rendered
    assert '\x1b' not in rendered
