"""自绘视口的真实鼠标命中、选区高亮与复制，不 mock 选区提取。"""
import asyncio

import pytest
from rich.cells import cell_len
from textual.geometry import Offset
from textual.selection import Selection

from ui.harness_app import HarnessApp


@pytest.mark.parametrize('reverse', [False, True])
def test_markdown_mouse_selection_copies_unicode_without_quitting(monkeypatch, reverse):
    async def scenario():
        copied = []
        app = HarnessApp([{'role': 'assistant', 'content': '**Hello 中文 🙂 world**'}])
        monkeypatch.setattr(app, 'copy_to_clipboard', copied.append)
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            app.view.scroll_home(animate=False)
            await pilot.pause()
            row = next(i for i in range(app.view.content_line_count)
                       if 'Hello' in str(app.doc_layout.line_at(i)))
            line = str(app.doc_layout.line_at(row))
            start = line.index('中')
            stop = line.index(' world')
            y = row - app.view.scroll_offset.y
            # 屏幕坐标按 cell 计数，提取和高亮按 Unicode 字符计数。
            first = (cell_len(line[:start]), y)
            last = (cell_len(line[:stop - 1]), y)
            if reverse:
                first, last = last, first
            await pilot.mouse_down(app.view, offset=first)
            await pilot.hover(app.view, offset=last)
            await pilot.mouse_up(app.view, offset=last)
            await pilot.pause()
            assert app.screen.get_selected_text() == '中文 🙂'
            assert copied == ['中文 🙂']  # 松开鼠标即复制，无需快捷键。
            assert app.model.snapshot()[-1].payload['text'] == 'Copied 4 chars to clipboard.'
            highlighted = app.view.render_line(y)
            assert any(segment.style and segment.style.bgcolor == app.view.selection_style.bgcolor
                       for segment in highlighted if '中' in segment.text)
            await pilot.press('ctrl+c')
            await pilot.pause()
            assert copied == ['中文 🙂', '中文 🙂']
            assert app.is_running
            app.screen.clear_selection()
            await pilot.pause()
            assert app.screen.get_selected_text() is None
            assert not any(segment.style and segment.style.bgcolor == app.view.selection_style.bgcolor
                           for segment in app.view.render_line(y) if '中' in segment.text)

    asyncio.run(scenario())


def test_scrolled_mouse_selection_copies_multiple_lines(monkeypatch):
    async def scenario():
        copied = []
        content = '\n'.join(f'row-{index:03d} 中文' for index in range(100))
        app = HarnessApp([{'role': 'assistant', 'content': f'```text\n{content}\n```'}])
        monkeypatch.setattr(app, 'copy_to_clipboard', copied.append)
        async with app.run_test(size=(60, 18)) as pilot:
            await pilot.pause()
            app.view.scroll_end(animate=False)
            await pilot.pause()
            top = app.view.scroll_offset.y
            assert top > 0
            y = 1
            lines = [str(app.doc_layout.line_at(top + y + i)).rstrip(' \t') for i in range(3)]
            assert all('row-' in line for line in lines)
            start = lines[0].index('row-')
            end = lines[-1].index(' 中文')
            await pilot.mouse_down(app.view, offset=(cell_len(lines[0][:start]), y))
            await pilot.hover(app.view, offset=(cell_len(lines[-1][:end - 1]), y + 2))
            await pilot.mouse_up(app.view, offset=(cell_len(lines[-1][:end - 1]), y + 2))
            await pilot.pause()
            expected = '\n'.join([lines[0][start:], lines[1], lines[-1][:end]])
            assert app.screen.get_selected_text() == expected
            assert copied == [expected]
            assert all(not line.endswith(' ') for line in copied[0].splitlines())
            await pilot.press('ctrl+shift+c')
            await pilot.pause()
            assert copied == [expected, expected]


def test_code_selection_removes_padding_and_preserves_indent(monkeypatch):
    async def scenario():
        copied = []
        app = HarnessApp([{'role': 'assistant', 'content': '```python\nx = 1\n    print(x)\n```'}])
        monkeypatch.setattr(app, 'copy_to_clipboard', copied.append)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            app.view.scroll_home(animate=False)
            await pilot.pause()
            row = next(i for i in range(app.view.content_line_count)
                       if 'x = 1' in str(app.doc_layout.line_at(i)))
            lines = [str(app.doc_layout.line_at(row + i)) for i in range(2)]
            assert len(lines[0]) > len(lines[0].rstrip(' '))  # 真实 Rich 面板填充。
            y = row - app.view.scroll_offset.y
            await pilot.mouse_down(app.view, offset=(0, y))
            await pilot.hover(app.view, offset=(80, y + 1))
            await pilot.mouse_up(app.view, offset=(80, y + 1))
            await pilot.pause()
            expected = '\n'.join(line.rstrip(' ') for line in lines)
            assert copied == [expected]
            assert '\n    ' in expected
            assert app.model.snapshot()[-1].payload['text'] == (
                f'Copied {len(expected)} chars to clipboard.')
            # 单击与只选择行尾填充，都不覆盖剪贴板。
            await pilot.click(app.view, offset=(0, y))
            await pilot.pause()
            assert copied == [expected]
            await pilot.mouse_down(app.view, offset=(70, y))
            await pilot.hover(app.view, offset=(80, y))
            await pilot.mouse_up(app.view, offset=(80, y))
            await pilot.pause()
            assert copied == [expected]

    asyncio.run(scenario())


def test_selection_bounds_and_resize():
    async def scenario():
        app = HarnessApp([{'role': 'assistant', 'content': '**hello**\n\nworld'}])
        async with app.run_test(size=(60, 18)) as pilot:
            await pilot.pause()
            height = app.view.content_line_count
            expected = '\n'.join(str(app.doc_layout.line_at(i)).rstrip(' \t')
                                 for i in range(height)).rstrip('\n')
            assert app.view.get_selection(Selection(None, None))[0] == expected
            assert app.view.get_selection(Selection(Offset(0, height + 1), None))[0] == ''
            app.screen.selections = {app.view: Selection(Offset(0, 0), Offset(3, 0))}
            await pilot.pause()
            await pilot.resize_terminal(40, 18)
            await pilot.pause()
            assert app.screen.get_selected_text() is None

    asyncio.run(scenario())
