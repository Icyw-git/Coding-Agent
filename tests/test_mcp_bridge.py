import json

import mcp_bridge


def test_load_server_configs_accepts_valid_servers(monkeypatch):
    monkeypatch.setenv('MCP_SERVERS', json.dumps([
        {'name': 'filesystem', 'command': 'python', 'args': ['server.py']},
        {'name': 'minimal', 'command': 'node'},
    ]))
    monkeypatch.delenv('MCP_SERVERS_FILE', raising=False)

    assert mcp_bridge._load_server_configs() == [
        {'name': 'filesystem', 'command': 'python', 'args': ['server.py']},
        {'name': 'minimal', 'command': 'node', 'args': []},
    ]


def test_load_server_configs_reports_malformed_json(monkeypatch, capsys):
    monkeypatch.setenv('MCP_SERVERS', '[invalid')

    assert mcp_bridge._load_server_configs() == []
    assert 'invalid MCP_SERVERS JSON' in capsys.readouterr().out


def test_load_server_configs_rejects_non_array(monkeypatch, capsys):
    monkeypatch.setenv('MCP_SERVERS', '{"name":"filesystem"}')

    assert mcp_bridge._load_server_configs() == []
    assert 'must be a JSON array' in capsys.readouterr().out


def test_load_server_configs_skips_invalid_entries_and_keeps_valid(monkeypatch, capsys):
    monkeypatch.setenv('MCP_SERVERS', json.dumps([
        {'name': 'missing-command'},
        {'name': 'bad-args', 'command': 'python', 'args': '--serve'},
        {'name': 'good', 'command': 'python', 'args': ['server.py']},
    ]))

    assert mcp_bridge._load_server_configs() == [
        {'name': 'good', 'command': 'python', 'args': ['server.py']},
    ]
    output = capsys.readouterr().out
    assert "config #1" in output
    assert "config #2" in output


def test_register_mcp_tools_does_not_connect_when_config_is_invalid(monkeypatch, capsys):
    monkeypatch.setenv('MCP_SERVERS', 'null')

    assert mcp_bridge.register_mcp_tools({}) == []
    assert 'must be a JSON array' in capsys.readouterr().out
