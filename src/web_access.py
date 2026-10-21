"""原生联网工具的网络层：URL 校验、私网阻断、重定向复检与 HTML 正文提取。

策略层的审批在 tools.run_web_fetch 里、请求发出之前完成；本模块只负责
「这个地址能不能访问」和「拿回来的字节怎么变成可读文本」。
"""
import codecs
import html
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
USER_AGENT = 'HarnessAgent/1.0'
_BLOCKED_SUFFIXES = ('.localhost', '.local', '.internal', '.home.arpa')
_BLOCKED_HOSTS = {'localhost', 'localhost.localdomain'}
_BINARY_TYPES = (
    'image/', 'audio/', 'video/', 'application/octet-stream',
    'application/pdf', 'application/zip', 'application/x-tar',
    'application/x-7z-compressed', 'application/vnd.rar',
)
_TEXTUAL_APPLICATION_TYPES = (
    'application/json', 'application/xml', 'application/xhtml+xml',
    'application/javascript', 'application/x-javascript',
    'application/yaml', 'application/x-yaml', 'application/toml',
)


class WebAccessError(ValueError):
    pass


def validate_url(url: str) -> urllib.parse.SplitResult:
    """语法校验：只允许 http(s)、无内嵌凭证、主机与端口合法。"""
    if not isinstance(url, str) or not url.strip():
        raise WebAccessError('url must be a non-empty string')
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.casefold() not in ('http', 'https'):
        raise WebAccessError('only http and https URLs are supported')
    if parsed.username is not None or parsed.password is not None:
        raise WebAccessError('URLs with embedded credentials are not allowed')
    host = parsed.hostname
    if not host:
        raise WebAccessError('URL has no hostname')
    try:
        _port = parsed.port
    except ValueError:
        raise WebAccessError('URL has an invalid port') from None
    return parsed


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, 'ipv4_mapped', None)
    if mapped is not None:
        ip = mapped
    return ip.is_global


def resolve_public_host(host: str, port: int) -> None:
    """解析主机并拒绝任何非公网地址；混合解析（公网+私网）整批拒绝。"""
    name = host.casefold().rstrip('.')
    if name in _BLOCKED_HOSTS or name.endswith(_BLOCKED_SUFFIXES):
        raise WebAccessError(f'refusing to access private host {host!r}')
    try:
        ascii_name = name.encode('idna').decode('ascii')
    except UnicodeError:
        raise WebAccessError(f'invalid hostname {host!r}') from None
    try:
        infos = socket.getaddrinfo(ascii_name, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WebAccessError(f'cannot resolve host {host!r}: {exc}') from None
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addresses:
        raise WebAccessError(f'cannot resolve host {host!r}')
    if not all(_is_public_ip(address) for address in addresses):
        raise WebAccessError(
            f'refusing to access non-public address for host {host!r}')


def check_url(url: str) -> urllib.parse.SplitResult:
    """语法 + DNS 校验；在审批之前调用，私网地址不会走到审批卡片。"""
    parsed = validate_url(url)
    port = parsed.port or (443 if parsed.scheme.casefold() == 'https' else 80)
    resolve_public_host(parsed.hostname, port)
    return parsed


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 自动跟随之前重新做一次完整校验：公网 URL 重定向到 127.0.0.1
        # 是最常见的 SSRF 绕过方式。
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(url: str, timeout: float):
    opener = urllib.request.build_opener(_SafeRedirectHandler)
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    return opener.open(request, timeout=timeout)


def _charset_of(headers, raw: bytes) -> str:
    charset = headers.get_content_charset()
    if not charset and b'<meta' in raw[:4096].lower():
        match = re.search(
            br'charset\s*=\s*["\']?\s*([a-zA-Z0-9._-]+)', raw[:4096])
        if match:
            charset = match.group(1).decode('ascii', 'ignore')
    return charset or 'utf-8'


def _decode_body(raw: bytes, charset: str) -> str:
    try:
        codecs.lookup(charset)
    except LookupError:
        charset = 'utf-8'
    try:
        return raw.decode(charset)
    except UnicodeDecodeError:
        pass
    if charset.casefold() not in ('utf-8', 'utf8', 'gb18030', 'gbk', 'gb2312'):
        return raw.decode('utf-8', errors='replace')
    try:
        return raw.decode('gb18030')
    except UnicodeDecodeError:
        return raw.decode('utf-8', errors='replace')


class _TextExtractor(HTMLParser):
    _SKIP = {'script', 'style', 'noscript', 'template', 'svg'}
    _BLOCK = {
        'p', 'div', 'br', 'li', 'ul', 'ol', 'tr', 'td', 'th', 'table',
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'section', 'article',
        'header', 'footer', 'pre', 'blockquote', 'dt', 'dd', 'hr',
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK and not self._skip_depth:
            self._parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in self._BLOCK and not self._skip_depth:
            self._parts.append('\n')

    def handle_data(self, data):
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return ''.join(self._parts)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    text = html.unescape(parser.text())
    text = re.sub(r'[ \t\f\v]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def fetch(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """抓取公网 URL，返回 {text, content_type, final_url, truncated}。"""
    check_url(url)
    try:
        response = _open_url(url, timeout)
    except urllib.error.HTTPError as exc:
        raise WebAccessError(f'HTTP {exc.code} from {url}') from None
    except urllib.error.URLError as exc:
        raise WebAccessError(f'cannot fetch {url}: {exc.reason}') from None
    except (TimeoutError, socket.timeout):
        raise WebAccessError(f'timeout while fetching {url}') from None
    except OSError as exc:
        raise WebAccessError(f'cannot fetch {url}: {exc}') from None
    with response:
        content_type = response.headers.get_content_type()
        if content_type.startswith(_BINARY_TYPES):
            raise WebAccessError(f'refusing binary response ({content_type})')
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        truncated = len(raw) > MAX_RESPONSE_BYTES
        raw = raw[:MAX_RESPONSE_BYTES]
        final_url = response.geturl()
    if not raw:
        text = ''
    else:
        decoded = _decode_body(raw, _charset_of(response.headers, raw))
        is_html = (content_type in ('text/html', 'application/xhtml+xml')
                   or decoded.lstrip()[:200].lower().startswith(('<!doctype html', '<html')))
        text = html_to_text(decoded) if is_html else decoded
    return {
        'text': text.strip(),
        'content_type': content_type,
        'final_url': final_url,
        'truncated': truncated,
    }
