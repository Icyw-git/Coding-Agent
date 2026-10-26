"""飞书截图回传：图片上传/发送、开关、上限与失败隔离。"""

import json
from types import SimpleNamespace

import pytest

import feishu_gateway
import screen_capture
from feishu_gateway import FeishuGateway, LarkTransport

PNG = screen_capture.PNG_MAGIC + b'pixels'
SHA = 'a' * 40


class FakeResponse:
    def __init__(self, ok=True, *, data=None, code=0, msg=''):
        self._ok = ok
        self.data = data
        self.code = code
        self.msg = msg

    def success(self):
        return self._ok


class FakeImageApi:
    def __init__(self, response=None):
        self.response = response or FakeResponse(
            data=SimpleNamespace(image_key='img_1'))
        self.requests = []

    def create(self, request):
        self.requests.append(request)
        return self.response


class FakeMessageApi:
    def __init__(self, response=None):
        self.response = response or FakeResponse(
            data=SimpleNamespace(message_id='om_out'))
        self.sent = []
        self.replied = []

    def create(self, request):
        self.sent.append(request)
        return self.response

    def reply(self, request):
        self.replied.append(request)
        return self.response


def _transport(monkeypatch, *, image=None, message=None):
    import lark_oapi as lark

    client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
        image=image or FakeImageApi(),
        message=message or FakeMessageApi(),
    )))
    monkeypatch.setattr(
        lark.Client, 'builder',
        lambda: SimpleNamespace(
            app_id=lambda aid: SimpleNamespace(
                app_secret=lambda sec: SimpleNamespace(build=lambda: client))))
    return LarkTransport('cli_x', 'secret')


def test_send_image_uploads_and_replies(monkeypatch):
    image = FakeImageApi()
    message = FakeMessageApi()
    transport = _transport(monkeypatch, image=image, message=message)

    assert transport.send_image('oc_chat', PNG, reply_to='om_1') == 'om_out'

    upload = image.requests[0]
    assert upload.request_body.image_type == 'message'
    assert upload.request_body.image.getvalue() == PNG
    reply = message.replied[0]
    assert reply.message_id == 'om_1'
    assert reply.request_body.msg_type == 'image'
    assert json.loads(reply.request_body.content) == {'image_key': 'img_1'}
    assert message.sent == []


def test_send_image_can_create_without_reply(monkeypatch):
    image = FakeImageApi()
    message = FakeMessageApi()
    transport = _transport(monkeypatch, image=image, message=message)

    assert transport.send_image('oc_chat', PNG) == 'om_out'

    sent = message.sent[0]
    assert sent.request_body.receive_id == 'oc_chat'
    assert sent.request_body.msg_type == 'image'
    assert json.loads(sent.request_body.content) == {'image_key': 'img_1'}
    assert message.replied == []


def test_send_image_rejects_non_png_and_oversize(monkeypatch):
    image = FakeImageApi()
    transport = _transport(monkeypatch, image=image)

    with pytest.raises(ValueError, match='PNG'):
        transport.send_image('oc_chat', b'not-a-png')
    assert image.requests == []

    monkeypatch.setattr(feishu_gateway, 'FEISHU_IMAGE_MAX_BYTES', 8)
    with pytest.raises(ValueError, match='too large'):
        transport.send_image('oc_chat', PNG)
    assert image.requests == []


def test_send_image_surfaces_upload_and_message_failures(monkeypatch):
    transport = _transport(
        monkeypatch,
        image=FakeImageApi(FakeResponse(False, code=42, msg='denied')))
    with pytest.raises(RuntimeError, match='image upload failed: 42 denied'):
        transport.send_image('oc_chat', PNG)

    transport = _transport(
        monkeypatch,
        message=FakeMessageApi(FakeResponse(False, code=43, msg='bad chat')))
    with pytest.raises(RuntimeError, match='image reply failed: 43 bad chat'):
        transport.send_image('oc_chat', PNG, reply_to='om_1')


class FakeTransport:
    def __init__(self, *, fail_image=False):
        self.sent: list[tuple[str, str]] = []
        self.images: list[tuple[str, bytes, str | None]] = []
        self.fail_image = fail_image

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))

    def send_image(self, chat_id, image, *, reply_to=None):
        if self.fail_image:
            raise RuntimeError('upload down')
        self.images.append((chat_id, image, reply_to))
        return f'om_img_{len(self.images)}'

    def all_text(self):
        return '\n'.join(text for _, text in self.sent)


class FakeApi:
    last_duration = None

    def __init__(self, shots, *, fail_download=False, without_download=False):
        self.shots = list(shots)
        self.fail_download = fail_download
        if without_download:
            self.download_screenshot = None

    def send_stream(self, content, session_id=None, on_delta=None,
                    on_progress=None, origin_chat=None, on_screenshots=None):
        if on_delta:
            on_delta('已截屏')
        if on_screenshots:
            on_screenshots(self.shots)
        return '已截屏'

    def download_screenshot(self, session_id, name):
        if self.fail_download:
            raise RuntimeError('api down')
        return PNG


class OldApi:
    last_duration = None

    def send_stream(self, content, session_id=None, on_delta=None,
                    on_progress=None, origin_chat=None):
        if on_delta:
            on_delta('旧客户端')
        return '旧客户端'


def _gateway(api, transport, tmp_path):
    return FeishuGateway(api, transport=transport,
                         bindings_path=tmp_path / 'bindings.json')


def _shot(sha1=SHA):
    return {'name': f'{sha1}.png', 'sha1': sha1, 'bytes': len(PNG),
            'width': 10, 'height': 10}


def _reply(gateway, tmp_path):
    gateway._send_agent_reply('oc_chat', 's1', '截图完成', reply_to='om_1')


def test_gateway_uploads_screenshots_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv(feishu_gateway.SCREENSHOT_REPLY_ENV, raising=False)
    api = FakeApi([_shot()])
    transport = FakeTransport()
    _reply(_gateway(api, transport, tmp_path), tmp_path)

    assert transport.images == [('oc_chat', PNG, 'om_1')]
    assert transport.all_text() == '已截屏'


def test_gateway_env_flag_disables_upload(tmp_path, monkeypatch):
    monkeypatch.setenv(feishu_gateway.SCREENSHOT_REPLY_ENV, '0')
    transport = FakeTransport()
    _reply(_gateway(FakeApi([_shot()]), transport, tmp_path), tmp_path)

    assert transport.images == []
    assert transport.all_text() == '已截屏'


def test_gateway_caps_images_and_notes_truncation(tmp_path, monkeypatch):
    monkeypatch.delenv(feishu_gateway.SCREENSHOT_REPLY_ENV, raising=False)
    shots = [_shot(f'{index:040x}') for index in range(1, 7)]
    transport = FakeTransport()
    _reply(_gateway(FakeApi(shots), transport, tmp_path), tmp_path)

    assert len(transport.images) == feishu_gateway.SCREENSHOT_REPLY_LIMIT == 4
    assert '只回传前 4 张' in transport.all_text()


def test_gateway_skips_oversize_image_with_note(tmp_path, monkeypatch):
    monkeypatch.delenv(feishu_gateway.SCREENSHOT_REPLY_ENV, raising=False)
    monkeypatch.setattr(feishu_gateway, 'FEISHU_IMAGE_MAX_BYTES', 8)
    transport = FakeTransport()
    _reply(_gateway(FakeApi([_shot()]), transport, tmp_path), tmp_path)

    assert transport.images == []
    assert '超过飞书 10MB 限制' in transport.all_text()


def test_gateway_image_failures_do_not_break_text_reply(tmp_path, monkeypatch):
    monkeypatch.delenv(feishu_gateway.SCREENSHOT_REPLY_ENV, raising=False)
    transport = FakeTransport(fail_image=True)
    _reply(_gateway(FakeApi([_shot()]), transport, tmp_path), tmp_path)
    assert transport.images == []
    assert transport.all_text() == '已截屏'

    transport = FakeTransport()
    _reply(_gateway(FakeApi([_shot()], fail_download=True), transport, tmp_path),
           tmp_path)
    assert transport.images == []
    assert transport.all_text() == '已截屏'


def test_gateway_ignores_invalid_and_duplicate_names(tmp_path, monkeypatch):
    monkeypatch.delenv(feishu_gateway.SCREENSHOT_REPLY_ENV, raising=False)
    shots = [{'name': '../secret.png'},
             _shot(), _shot(),
             {'name': f'{"B" * 40}.png'}]
    transport = FakeTransport()
    _reply(_gateway(FakeApi(shots), transport, tmp_path), tmp_path)
    assert [shot[1] for shot in transport.images] == [PNG]


def test_gateway_skips_upload_when_transport_has_no_image_support(tmp_path, monkeypatch):
    monkeypatch.delenv(feishu_gateway.SCREENSHOT_REPLY_ENV, raising=False)

    class TextOnlyTransport(FakeTransport):
        send_image = None

    transport = TextOnlyTransport()
    _reply(_gateway(FakeApi([_shot()]), transport, tmp_path), tmp_path)
    assert transport.images == []
    assert transport.all_text() == '已截屏'


def test_gateway_skips_upload_when_client_cannot_download(tmp_path, monkeypatch):
    monkeypatch.delenv(feishu_gateway.SCREENSHOT_REPLY_ENV, raising=False)
    transport = FakeTransport()
    api = FakeApi([_shot()], without_download=True)
    _reply(_gateway(api, transport, tmp_path), tmp_path)
    assert transport.images == []
    assert transport.all_text() == '已截屏'


def test_gateway_does_not_pass_callback_to_old_streamer(tmp_path, monkeypatch):
    monkeypatch.delenv(feishu_gateway.SCREENSHOT_REPLY_ENV, raising=False)
    transport = FakeTransport()
    _reply(_gateway(OldApi(), transport, tmp_path), tmp_path)
    assert transport.images == []
    assert transport.all_text() == '旧客户端'
