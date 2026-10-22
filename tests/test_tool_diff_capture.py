"""写入类工具在 tool_end 里带 unified diff（供 UI 渲染）。"""
import json
from types import SimpleNamespace

import harness
import ui_events


def _tool_call(name, payload):
    return SimpleNamespace(
        id='call-1', type='function',
        function=SimpleNamespace(name=name, arguments=json.dumps(payload)))


def test_write_tool_emits_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    target = tmp_path / 'a.txt'
    target.write_text('old\n', encoding='utf-8')

    def fake_write(path, content):
        (tmp_path / path).write_text(content, encoding='utf-8')
        return 'Wrote'

    events = []
    ui_events.bus.subscribe(events.append)
    try:
        harness.exec_tool_call(
            _tool_call('write', {'path': 'a.txt', 'content': 'new\n'}),
            registry={'write': fake_write}, hooks=False)
    finally:
        ui_events.bus.unsubscribe(events.append)

    ended = next(event for event in events if event.type == 'tool_end')
    assert '-old' in ended.payload['diff']
    assert '+new' in ended.payload['diff']


def test_secret_file_is_not_diffed(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    (tmp_path / '.env').write_text('SECRET=1\n', encoding='utf-8')

    def fake_write(path, content):
        return 'ok'

    events = []
    ui_events.bus.subscribe(events.append)
    try:
        harness.exec_tool_call(
            _tool_call('write', {'path': '.env', 'content': 'SECRET=2\n'}),
            registry={'write': fake_write}, hooks=False)
    finally:
        ui_events.bus.unsubscribe(events.append)

    ended = next(event for event in events if event.type == 'tool_end')
    assert ended.payload['diff'] == ''
