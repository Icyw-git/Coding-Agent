"""浏览器 MCP 配置助手：生成/检查 Playwright MCP 的 server 配置片段。

不启动浏览器、不改 `.env`——只把「怎么接」变成一条可复制的命令，避免手写
JSON 时把 profile 指到日常 Chrome 目录（那会与应用已开着的窗口冲突，也会让
登录态和日常生活混在一起）。

用法::

    python scripts/browser_setup.py snippet     # 打印 server 配置片段
    python scripts/browser_setup.py check       # 检查当前配置与 profile
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import console  # noqa: E402

console.harden_stdio()

import browser_profile  # noqa: E402

# Playwright 官方 MCP server 的常见两种启动方式。这里只生成配置，不安装包。
SERVERS = {
    'playwright': {
        'command': 'npx',
        'args': ['-y', '@playwright/mcp@latest', '--isolated'],
        'note': '--isolated 每次用临时 profile；想复用登录态就去掉它并加 --user-data-dir',
    },
}


def _servers_path() -> Path:
    import os

    raw = (os.getenv('MCP_SERVERS_FILE') or '').strip()
    return Path(raw) if raw else ROOT / '.cache' / 'mcp_servers.json'


def snippet(*, keep_login: bool) -> dict:
    cfg = {key: (list(value) if isinstance(value, list) else value)
           for key, value in SERVERS['playwright'].items()}
    cfg['name'] = 'playwright'
    if keep_login:
        cfg['args'] = [arg for arg in cfg['args'] if arg != '--isolated']
        cfg['args'].append(f'--user-data-dir={browser_profile.profile_dir()}')
    return cfg


def cmd_snippet(args) -> int:
    cfg = snippet(keep_login=args.keep_login)
    if args.keep_login:
        profile = browser_profile.ensure()
        print(f'  已准备 profile 目录：{profile}', file=sys.stderr)
        note = ('登录态会保存在这个目录里；在其中登录一次即可复用。'
                '不要把它复制进工作区。')
    else:
        note = SERVERS['playwright']['note']
    print(json.dumps([cfg], ensure_ascii=False, indent=2))
    print(f'\n  # {note}', file=sys.stderr)
    return 0


def _looks_like_browser(cfg: dict) -> bool:
    """按 name/command/args 里是否出现浏览器标记来判断，而不是只看 name。

    用户常把 server 命名成 `pw` 或 `web`；只看 name 会漏判，进而让「profile 指到
    日常目录」这类警告失效。
    """
    fields = [cfg.get('name'), cfg.get('command'), *(cfg.get('args') or [])]
    for item in fields:
        text = str(item or '').casefold()
        if 'playwright' in text or 'puppeteer' in text or 'browser' in text:
            return True
    return False


def cmd_check(args) -> int:
    state = browser_profile.status()
    print(f'  {browser_profile.describe()}')
    path = _servers_path()
    print(f'  MCP 配置：{path}')
    if not path.is_file():
        print('  （配置文件不存在；先运行 snippet 并把输出写进去）')
        return 0
    try:
        configs = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        print(f'  [错误] 配置读不了：{exc}', file=sys.stderr)
        return 2
    if not isinstance(configs, list):
        print('  [错误] 配置必须是 JSON 数组', file=sys.stderr)
        return 2
    browsers = [cfg for cfg in configs
                if isinstance(cfg, dict) and _looks_like_browser(cfg)]
    if not browsers:
        print('  （没有浏览器相关 server）')
        return 0
    for cfg in browsers:
        joined = ' '.join(str(item) for item in cfg.get('args') or [])
        print(f"  浏览器 server：{cfg.get('name')}")
        if 'user-data-dir' not in joined and '--isolated' not in joined:
            print('  ⚠ 既没有 --isolated 也没有 --user-data-dir：'
                  '可能默认打开日常 profile，与已开着的浏览器冲突。')
        elif '--isolated' in joined:
            print('  · 使用临时 profile：不保留登录态。')
        else:
            print('  · 使用指定 profile：登录态会被复用。')
            if state['inside_daily_profile']:
                print('  ⚠ profile 指向日常浏览器目录。')
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='浏览器 MCP 配置助手')
    sub = parser.add_subparsers(dest='command')
    snippet_cmd = sub.add_parser('snippet', help='打印 server 配置 JSON')
    snippet_cmd.add_argument('--keep-login', action='store_true',
                             help='使用独立 profile 复用登录态（而不是 --isolated）')
    sub.add_parser('check', help='检查配置与 profile 状态')
    args = parser.parse_args(argv)
    if args.command == 'snippet':
        return cmd_snippet(args)
    if args.command == 'check':
        return cmd_check(args)
    parser.print_help()
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
