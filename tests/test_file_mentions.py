"""file_mentions：工作区索引、匹配顺序与 `@` 光标解析。"""
from file_mentions import (
    format_mention,
    match_mentions,
    mention_token,
    workspace_files,
)


def test_workspace_files_skips_hidden_and_noise(tmp_path):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src' / 'app.py').write_text('', encoding='utf-8')
    (tmp_path / 'node_modules').mkdir()
    (tmp_path / 'node_modules' / 'x.js').write_text('', encoding='utf-8')
    (tmp_path / '.env').write_text('', encoding='utf-8')
    (tmp_path / 'README.md').write_text('', encoding='utf-8')

    files = workspace_files(tmp_path)

    assert 'src/app.py' in files and 'README.md' in files
    assert not any('node_modules' in path or path.startswith('.')
                   for path in files)


def test_match_mentions_prefers_basename_prefix():
    files = ['src/app.py', 'lib/app_test.py', 'docs/app.md', 'src/other.py']

    assert match_mentions(files, 'app')[:2] == ['src/app.py', 'lib/app_test.py']
    assert match_mentions(files, 'other') == ['src/other.py']
    assert match_mentions(files, '')[:2] == files[:2]


def test_mention_token_boundaries():
    assert mention_token('check @src/ap') == (6, 'src/ap')
    assert mention_token('@a') == (0, 'a')
    assert mention_token('see (@x') == (5, 'x')
    assert mention_token('a@b') is None        # 邮箱不是提及
    assert mention_token('@src/a b') is None   # 空格结束提及
    assert mention_token('plain text') is None


def test_format_mention_quotes_spaces():
    assert format_mention('src/app.py') == '@src/app.py'
    assert format_mention('my docs/a.md') == '@"my docs/a.md"'
