"""子代理工具面：只广告可执行、且适合子代理的工具。"""
from types import SimpleNamespace

import harness
import subagent


def _fake_schema(name):
    return {
        'type': 'function',
        'function': {
            'name': name,
            'description': 'fake',
            'parameters': {'type': 'object', 'properties': {}},
        },
    }


def test_surface_uses_live_registry_and_excludes_parent_orchestration():
    schema = _fake_schema('fake_mcp_tool')
    harness.TOOLS.append(schema)
    harness.tool_registry['fake_mcp_tool'] = lambda **kwargs: 'ok'
    try:
        schemas, handlers = subagent._subagent_surface()
    finally:
        harness.TOOLS.remove(schema)
        harness.tool_registry.pop('fake_mcp_tool', None)

    names = {item['function']['name'] for item in schemas}
    assert 'fake_mcp_tool' in names
    assert names <= set(handlers)
    for excluded in ('task', 'compact', 'checkpoint', 'load_skill',
                     'spawn_teammate', 'schedule_cron', 'background_output',
                     'kill_background', 'rename_session', 'revoke_permissions',
                     'screen_capture', 'screen_ocr',
                     'clipboard_read', 'clipboard_write'):
        assert excluded not in names

    bash = next(item for item in schemas if item['function']['name'] == 'bash')
    properties = bash['function']['parameters']['properties']
    assert 'timeout' in properties and 'cwd' in properties
    assert 'run_in_background' not in properties


def test_surface_does_not_mutate_shared_bash_schema():
    subagent._subagent_surface()
    bash = next(item for item in harness.TOOLS
                if item['function']['name'] == 'bash')
    assert 'run_in_background' in bash['function']['parameters']['properties']


def test_spawn_subagent_passes_live_surface(monkeypatch):
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(
            content='done', tool_calls=None,
            model_dump=lambda: {'role': 'assistant', 'content': 'done'})
        return SimpleNamespace(choices=[
            SimpleNamespace(finish_reason='stop', message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(harness, 'client', client)
    monkeypatch.setattr(harness, 'build_sub_system', lambda: 'system')
    surface = ([_fake_schema('only_tool')], {'only_tool': lambda **kwargs: ''})
    monkeypatch.setattr(subagent, '_subagent_surface', lambda: surface)

    assert subagent.spawn_subagent('do it') == 'done'
    assert captured['tools'] is surface[0]
