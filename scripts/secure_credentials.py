"""凭据加密管理：把 `.env` 里的密钥迁进 DPAPI 加密文件。

用法::

    python scripts/secure_credentials.py status      # 现在用的是哪儿
    python scripts/secure_credentials.py migrate     # 加密写入（不改 .env）
    python scripts/secure_credentials.py migrate --delete-env-secrets
    python scripts/secure_credentials.py rotate      # 重新加密当前有效值
    python scripts/secure_credentials.py clear       # 删除密文，回退到 .env

只处理 ``credentials.SECRET_KEYS`` 里的键；其余配置留在 `.env` 不动。

**默认不删 `.env` 里的行。** 删掉是不可逆动作，而且 `.env` 可能被别的工具
管理；由人显式加 `--delete-env-secrets` 才做，且只删这几行、保留注释与顺序。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import console  # noqa: E402

console.harden_stdio()

import credentials  # noqa: E402


def _env_path() -> Path:
    return Path(__file__).resolve().parents[1] / '.env'


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' not in stripped:
            continue
        key, _, value = stripped.partition('=')
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _delete_secret_lines(path: Path, keys) -> list[str]:
    """从 .env 里删掉指定键的行（保留注释与其它行），返回删掉的键。"""
    lines = path.read_text(encoding='utf-8-sig').splitlines(keepends=True)
    kept, removed = [], []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            key = stripped.partition('=')[0].strip()
            if key in keys:
                removed.append(key)
                continue
        kept.append(line)
    if removed:
        path.write_text(''.join(kept), encoding='utf-8')
    return removed


def cmd_status() -> int:
    print(f'  {credentials.describe()}')
    state = credentials.status()
    print(f"  文件：{state['path']}")
    if not state['available']:
        print('  本平台不支持 DPAPI；请继续使用 .env（注意它是否在备份/同步目录里）。')
        return 0
    if state['exists'] and state['keys']:
        print(f"  指纹：{credentials.fingerprint(credentials.load())}")
    env_values = _read_env(_env_path())
    present = [key for key in credentials.SECRET_KEYS if env_values.get(key)]
    if present:
        print(f'  .env 里仍有明文：{", ".join(present)}')
    return 0


def cmd_migrate(delete_env: bool) -> int:
    if not credentials.available():
        print('  [错误] 当前平台无法加密；未做任何改动。', file=sys.stderr)
        return 2
    env_values = _read_env(_env_path())
    present = {key: value for key, value in env_values.items()
               if key in credentials.SECRET_KEYS and value}
    if not present:
        print('  [错误] .env 里没有可迁移的密钥；未做任何改动。', file=sys.stderr)
        return 2

    # 已加密的旧值合并进来：只迁移了部分键时不该把上一份丢掉。
    merged = {}
    try:
        merged.update(credentials.load())
    except credentials.CredentialError as exc:
        print(f'  [提示] 旧密文不可用，将被覆盖：{exc}')
    merged.update(present)

    path = credentials.save(merged)
    print(f'  已加密 {len(present)} 项 → {path}')
    print(f'  指纹：{credentials.fingerprint(merged)}')

    if delete_env:
        removed = _delete_secret_lines(_env_path(), set(present))
        print(f'  已从 .env 删除明文行：{", ".join(removed)}')
        print('  提示：确认 API 与网关都能正常启动后，再清理备份/历史里的旧 .env。')
    else:
        print('  .env 未改动（加 --delete-env-secrets 才会删除明文行）。')
    return 0


def cmd_rotate() -> int:
    if not credentials.available():
        print('  [错误] 当前平台无法加密。', file=sys.stderr)
        return 2
    values = {}
    try:
        values.update(credentials.load())
    except credentials.CredentialError as exc:
        print(f'  [提示] 旧密文不可用：{exc}')
    values.update({key: value for key, value in _read_env(_env_path()).items()
                   if key in credentials.SECRET_KEYS and value})
    if not values:
        print('  [错误] 没有可加密的凭据。', file=sys.stderr)
        return 2
    path = credentials.save(values)
    print(f'  已重新加密 {len(values)} 项 → {path}')
    return 0


def cmd_clear() -> int:
    target = credentials.store_path()
    if not target.exists():
        print('  没有加密文件，无需清理。')
        return 0
    target.unlink()
    print(f'  已删除 {target}；之后回退到 .env 明文凭据。')
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='凭据加密管理')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('status', help='查看当前凭据来源')
    migrate = sub.add_parser('migrate', help='把 .env 里的密钥加密写入')
    migrate.add_argument('--delete-env-secrets', action='store_true',
                         help='同时删除 .env 里的明文行（不可逆，先确认能启动）')
    sub.add_parser('rotate', help='用当前有效值重新加密')
    sub.add_parser('clear', help='删除密文，回退到 .env')
    args = parser.parse_args(argv)

    if args.command == 'status':
        return cmd_status()
    if args.command == 'migrate':
        return cmd_migrate(args.delete_env_secrets)
    if args.command == 'rotate':
        return cmd_rotate()
    if args.command == 'clear':
        return cmd_clear()
    parser.print_help()
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
