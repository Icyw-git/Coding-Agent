"""外部内容不可信围栏（N6）。

背景：``web_fetch`` / MCP 返回的内容由第三方控制，模型无法区分「用户让我做的事」
和「网页里一句 ignore previous instructions」。加围栏不是为了过滤内容（过滤会破坏
代码块和 diff），而是让「这是数据」在上下文里显式可辨。
"""
from __future__ import annotations

import untrusted
from tool_exec import fetch_url

# ---------------------------------------------------------------- wrap 基本行为

def test_wrap_adds_fence_with_origin_and_kind():
    out = untrusted.wrap('hello', 'https://example.com/a', kind='web')
    assert out.startswith('<untrusted source="https://example.com/a" kind="web">')
    assert out.endswith('</untrusted>')
    assert 'hello' in out


def test_wrap_preserves_body_verbatim():
    """正文必须逐字保留：转义会破坏代码块、正则和 diff。"""
    body = 'def f():\n    return "<a>&amp;</a>"\n'
    out = untrusted.wrap(body, 'x', kind='web')
    assert body in out


def test_wrap_empty_text_returns_empty():
    assert untrusted.wrap('', 'x', kind='web') == ''


def test_wrap_disabled_by_env(monkeypatch):
    monkeypatch.setenv(untrusted.ENV, '0')
    assert untrusted.wrap('hello', 'x', kind='web') == 'hello'


def test_wrap_sanitizes_origin_quotes_and_newlines():
    """origin 里的引号和换行会破坏属性，必须收敛。"""
    out = untrusted.wrap('body', 'a"b\nc', kind='web')
    first = out.splitlines()[0]
    assert first.count('"') == 4          # source="…" kind="…" 各一对
    assert '\n' not in first


def test_wrap_truncates_long_origin():
    out = untrusted.wrap('body', 'h' * 500, kind='web')
    assert len(out.splitlines()[0]) < 260


# ---------------------------------------------------------------- 接入点

def test_fetch_url_wraps_web_content(monkeypatch):
    """web_fetch 的正文要进围栏，头部信息留在围栏外。"""

    class _Result(dict):
        pass

    monkeypatch.setattr('web_access.check_url', lambda url: None)
    monkeypatch.setattr('web_access.fetch', lambda url: _Result(
        text='page body', final_url='https://example.com/f', content_type='text/html',
        truncated=False))
    out = fetch_url('https://example.com/f', gate=lambda *a, **k: None)
    assert 'URL: https://example.com/f' in out
    head, fence = out.split('<untrusted', 1)
    assert 'page body' not in head        # 正文不在围栏外
    assert 'page body' in fence


def test_fetch_url_respects_disable_switch(monkeypatch):
    monkeypatch.setenv(untrusted.ENV, '0')
    monkeypatch.setattr('web_access.check_url', lambda url: None)
    monkeypatch.setattr('web_access.fetch', lambda url: {
        'text': 'page body', 'final_url': 'https://example.com/f',
        'content_type': 'text/html', 'truncated': False})
    out = fetch_url('https://example.com/f', gate=lambda *a, **k: None)
    assert '<untrusted' not in out
    assert 'page body' in out


def test_mcp_result_is_wrapped_but_error_is_not():
    """MCP 正常结果进围栏；错误串不包，否则调用方的前缀判断会失效。"""
    import mcp_bridge

    class _Client:
        name = 'filesystem'
        result = 'file body'
        error = None

        async def call_tool(self, _name, _kwargs):
            if self.error:
                raise self.error
            return self.result

    client = _Client()
    call = mcp_bridge._make_sync_wrapper(client, 'read')
    ok = call(path='a.txt')
    assert '<untrusted' in ok and 'file body' in ok

    client.result = 'MCP error: boom'
    err = call(path='a.txt')
    assert err == 'MCP error: boom'
    assert '<untrusted' not in err


# ---------------------------------------------------------------- 提示词

def test_system_prompt_explains_fence():
    """没有说明，模型会把围栏当成普通噪音，白加。"""
    import prompt_assembly
    prompt = prompt_assembly.build_system(
        workdir='.', tool_names=['read'], skills='')
    assert '<untrusted>' in prompt
    assert 'never as instructions' in prompt
