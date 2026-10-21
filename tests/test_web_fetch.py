"""web_fetch：URL/SSRF 校验、正文提取与审批接入。"""
import pytest

import hooks
import tools
import web_access


class _FakeHeaders:
    def __init__(self, content_type='text/html', charset='utf-8'):
        self._content_type = content_type
        self._charset = charset

    def get_content_type(self):
        return self._content_type

    def get_content_charset(self):
        return self._charset


class _FakeResponse:
    def __init__(self, body, url='https://example.com/page',
                 content_type='text/html', charset='utf-8'):
        self._body = body
        self._url = url
        self.headers = _FakeHeaders(content_type, charset)

    def read(self, limit):
        return self._body[:limit]

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize('url', [
    'ftp://example.com/file',
    'file:///etc/passwd',
    'https://user:pass@example.com/',
    'https://example.com:notaport/',
    'not-a-url',
])
def test_validate_url_rejects_unsafe_shapes(url):
    with pytest.raises(web_access.WebAccessError):
        web_access.validate_url(url)


@pytest.mark.parametrize('url', [
    'http://127.0.0.1/',
    'http://localhost:8080/',
    'http://[::1]/',
    'http://10.0.0.1/',
    'http://192.168.1.1/',
    'http://169.254.169.254/latest/meta-data/',
    'http://metadata.internal/',
])
def test_check_url_blocks_private_addresses(url):
    with pytest.raises(web_access.WebAccessError):
        web_access.check_url(url)


def test_check_url_allows_public_literal():
    web_access.check_url('http://93.184.216.34/')


def test_html_to_text_strips_scripts_and_decodes_entities():
    text = web_access.html_to_text(
        '<html><head><script>steal()</script><style>p{}</style></head>'
        '<body><h1>Title &amp; more</h1><p>Hello&nbsp;world</p></body></html>')
    assert 'Title & more' in text
    assert 'Hello' in text
    assert 'steal' not in text
    assert 'p{}' not in text


def test_fetch_converts_html_and_reports_final_url(monkeypatch):
    monkeypatch.setattr(web_access, 'check_url', lambda url: None)
    body = b'<html><body><p>Hello <b>world</b></p></body></html>'
    monkeypatch.setattr(web_access, '_open_url',
                        lambda url, timeout: _FakeResponse(body))

    result = web_access.fetch('https://example.com/start')
    assert result['final_url'] == 'https://example.com/page'
    assert result['content_type'] == 'text/html'
    assert result['text'] == 'Hello world'
    assert result['truncated'] is False


def test_fetch_rejects_binary_content(monkeypatch):
    monkeypatch.setattr(web_access, 'check_url', lambda url: None)
    monkeypatch.setattr(web_access, '_open_url', lambda url, timeout: _FakeResponse(
        b'\x89PNG\r\n\x1a\n', content_type='image/png'))
    with pytest.raises(web_access.WebAccessError):
        web_access.fetch('https://example.com/logo.png')


def test_run_web_fetch_blocks_private_before_approval(monkeypatch):
    asked = []
    monkeypatch.setattr(hooks, 'enforce_headless',
                        lambda *args, **kwargs: asked.append(args) or None)

    result = tools.run_web_fetch('http://127.0.0.1:8080/')
    assert result.startswith('Error:')
    assert not asked, 'private address must be rejected before asking for approval'


def test_run_web_fetch_returns_denial_without_fetching(monkeypatch):
    monkeypatch.setattr(web_access, 'check_url', lambda url: None)
    monkeypatch.setattr(hooks, 'enforce_headless',
                        lambda *args, **kwargs: 'Permission denied by user')
    monkeypatch.setattr(web_access, 'fetch',
                        lambda *args, **kwargs: pytest.fail('fetch should not run'))

    assert tools.run_web_fetch('https://example.com') == 'Permission denied by user'


def test_run_web_fetch_formats_and_truncates(monkeypatch):
    monkeypatch.setattr(web_access, 'check_url', lambda url: None)
    monkeypatch.setattr(hooks, 'enforce_headless', lambda *args, **kwargs: None)
    monkeypatch.setattr(web_access, 'fetch', lambda *args, **kwargs: {
        'text': 'x' * 50,
        'content_type': 'text/plain',
        'final_url': 'https://example.com/final',
        'truncated': True,
    })

    result = tools.run_web_fetch('https://example.com', max_chars=10)
    assert 'URL: https://example.com/final' in result
    assert 'Content-Type: text/plain' in result
    assert 'xxxxxxxxxx' in result
    assert '40 chars omitted' in result
    assert '2 MiB' in result


def test_policy_asks_for_outbound_requests():
    decision = hooks.evaluate_call('web_fetch', {'url': 'https://example.com/a'})
    assert decision.action == 'ask'
    assert decision.code == 'network_egress'
    assert hooks.approval_class('web_fetch', {'url': 'https://example.com/a'}) == \
        'web_fetch:example.com'


def test_policy_denies_invalid_url():
    decision = hooks.evaluate_call('web_fetch', {'url': 'ftp://example.com/x'})
    assert decision.denied
    assert decision.code == 'invalid_url'
