"""tool_teams：消息总线、收件箱与 lead 协议的输出与状态。"""
import pytest

import tool_teams
from team_protocols import ProtocolState


class _Bus:
    def __init__(self):
        self.sent = []

    def send(self, *args):
        self.sent.append(args)


@pytest.fixture(autouse=True)
def _clean_pending():
    tool_teams.pending_requests.clear()
    yield
    tool_teams.pending_requests.clear()


def test_send_message_uses_lead_channel(monkeypatch):
    bus = _Bus()
    monkeypatch.setattr(tool_teams, 'BUS', bus)

    assert tool_teams.run_send_message('worker', 'hi') == 'Sent to worker'
    assert bus.sent == [('lead', 'worker', 'hi', 'message')]


def test_check_inbox_formats_messages_and_empty(monkeypatch):
    monkeypatch.setattr(tool_teams, 'consume_lead_inbox',
                        lambda route_protocol: [{'from': 'w1', 'type': 'plan',
                                                 'content': 'ok'}])
    assert tool_teams.run_check_inbox() == '[from w1 (plan)] ok'

    monkeypatch.setattr(tool_teams, 'consume_lead_inbox',
                        lambda route_protocol: [])
    assert tool_teams.run_check_inbox() == 'No messages in inbox.'


def test_request_shutdown_records_pending_and_sends(monkeypatch):
    bus = _Bus()
    monkeypatch.setattr(tool_teams, 'BUS', bus)
    monkeypatch.setattr(tool_teams, 'new_request_id', lambda: 'req-1')

    output = tool_teams.run_request_shutdown('w1')

    assert 'req-1' in output
    state = tool_teams.pending_requests['req-1']
    assert state.type == 'shutdown' and state.status == 'pending'
    assert bus.sent[0][1:3] == ('w1', 'Please shut down gracefully.')


def test_request_plan_sends_message(monkeypatch):
    bus = _Bus()
    monkeypatch.setattr(tool_teams, 'BUS', bus)

    assert 'submit a plan' in tool_teams.run_request_plan('w1', 'refactor')
    assert bus.sent[0][1] == 'w1'


def test_review_plan_handles_missing_already_done_and_approval(monkeypatch):
    bus = _Bus()
    monkeypatch.setattr(tool_teams, 'BUS', bus)

    assert 'not found' in tool_teams.run_review_plan('missing', True)

    tool_teams.pending_requests['req-1'] = ProtocolState(
        request_id='req-1', type='plan', sender='w1', target='lead',
        status='pending', payload='')
    assert 'approved' in tool_teams.run_review_plan('req-1', True, 'go')
    assert tool_teams.pending_requests['req-1'].status == 'approved'
    assert bus.sent[-1][2] == 'go'

    assert 'already approved' in tool_teams.run_review_plan('req-1', False)


def test_spawn_teammate_delegates_to_agent_teams(monkeypatch):
    calls = []
    monkeypatch.setattr(tool_teams, 'spawn_teammate_thread',
                        lambda *args: calls.append(args) or 'started')

    assert tool_teams.run_spawn_teammate('w1', 'tester', 'inspect') == 'started'
    assert calls == [('w1', 'tester', 'inspect')]
