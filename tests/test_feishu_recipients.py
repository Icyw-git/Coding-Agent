import json
from types import SimpleNamespace

import pytest

from feishu_gateway import FeishuGateway, LarkTransport


def test_real_transport_ignores_test_bindings_without_rewriting_file(tmp_path):
    path = tmp_path / 'bindings.json'
    saved = {'probe-chat': 'test-session', 'oc_real_chat': 'real-session'}
    path.write_text(json.dumps(saved), encoding='utf-8')
    gateway = FeishuGateway(None, transport=LarkTransport.__new__(LarkTransport),
                            bindings_path=path)
    assert gateway.bindings == {'oc_real_chat': 'real-session'}
    assert json.loads(path.read_text(encoding='utf-8')) == saved


@pytest.mark.parametrize('recipient', ['probe-chat', 'ou_user', '', None])
def test_invalid_recipient_is_rejected_before_sdk_call(recipient):
    transport = LarkTransport.__new__(LarkTransport)
    with pytest.raises(ValueError, match='chat_id'):
        transport.send_text(recipient, 'hello')
    with pytest.raises(ValueError, match='chat_id'):
        transport.send_markdown(recipient, 'hello')
    with pytest.raises(ValueError, match='chat_id'):
        transport.send_approval_card(recipient, 'hello', 'request')


def test_notification_failure_does_not_block_other_chats(tmp_path):
    delivered = []

    class Transport:
        def send_text(self, chat_id, text):
            if chat_id == 'unavailable':
                raise RuntimeError('invalid receive_id')
            delivered.append(chat_id)

    api = SimpleNamespace(pending_permissions=lambda: [
        {'req_id': 'request', 'tool': 'shell', 'args': 'echo hello'}])
    gateway = FeishuGateway(api, transport=Transport(), bindings_path=tmp_path / 'bindings.json')
    gateway.bindings = {'unavailable': 'one', 'available': 'two'}
    gateway.poll_permissions()
    assert delivered == ['available']
    gateway.poll_permissions()
    assert delivered == ['available']
