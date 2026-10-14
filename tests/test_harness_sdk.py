from dataclasses import dataclass

from harness_sdk import HarnessClient


@dataclass
class FakeResult:
    session_id: str
    content: str


class FakeRuntime:
    def __init__(self):
        self.history_by_id = {}

    def create_session(self, session_id=None):
        identifier = session_id or 'generated'
        self.history_by_id[identifier] = []
        return identifier

    def history(self, session_id):
        return list(self.history_by_id[session_id])

    def send(self, session_id, content):
        self.history_by_id[session_id].extend([
            {'role': 'user', 'content': content},
            {'role': 'assistant', 'content': 'done'},
        ])
        return FakeResult(session_id, 'done')


def test_sdk_creates_session_reads_history_and_sends_message():
    client = HarnessClient(FakeRuntime())

    session = client.sessions.create('sdk-session')
    response = client.messages.create(session.id, 'inspect project')
    updated = client.sessions.get(session.id)

    assert session == type(session)(id='sdk-session', history=[])
    assert response.session_id == 'sdk-session'
    assert response.content == 'done'
    assert updated.history == [
        {'role': 'user', 'content': 'inspect project'},
        {'role': 'assistant', 'content': 'done'},
    ]


def test_sdk_can_create_generated_session():
    session = HarnessClient(FakeRuntime()).sessions.create()

    assert session.id == 'generated'
    assert session.history == []
