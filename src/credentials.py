"""凭据存储：把 `.env` 里的明文密钥换成「机器绑定 + 用户绑定」的加密副本。

## 为什么需要这层

`.env` 是明文，任何能读这个文件的东西（另一个进程、误提交、备份同步、截图）
都能拿到飞书 secret 和 API token。项目已有 `checkpoints.DEFAULT_SECRETS` 把它
排除出工具读取范围，但那只是「模型不主动读」，不改变文件本身的形态。

## 取舍

- **加密而不是搬进系统凭据库**：Windows 有 DPAPI（`CryptProtectData`），
  加密绑定到当前用户账户，别的用户/别的机器拿到密文也解不开；不需要额外
  依赖（`win32crypt` 已随环境安装，缺失时降级并明确提示）。keyring 需要 pip
  包且在某些 Linux 上退化成明文文件，收益反而更小。
- **只做「密文副本」，不动 `.env`**：迁移是显式动作（`scripts/secure_credentials.py`）。
  自动改写用户的 `.env` 风险大于收益——它可能是符号链接、可能被别的工具管理。
- **加载顺序：密文优先，`.env` 兜底**。密文存在就覆盖同名环境变量，让「已迁移」
  真的生效；密文不存在或解不开时保持 `.env` 的行为，启动不会因为这一步失败。
- **启动时校验**：`--doctor` 与 `/status` 显示当前凭据来自哪里，避免「以为加密了
  其实还在用明文」。

## 边界（不要误读）

- 这是**静态数据保护**，不是运行时隔离。进程内存里仍然是明文，被调试器附加或
  被同用户权限的恶意进程读取时挡不住。
- DPAPI 绑定当前用户：换机器或换账户后密文不可用，需要用原 `.env` 重新加密。
- 磁盘上没有 `.env` 也能跑，但**前提是已经迁移过**；否则 `--doctor` 会明确报缺。
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import paths

# 需要保护的键：都是「拿到就能冒充或花钱」的东西。
# 刻意不包含 LLM_MODEL_ID / LLM_BASE_URL 这类非敏感配置——加密它们只会让
# 排障更麻烦。
SECRET_KEYS = (
    'LLM_API_KEY',
    'HARNESS_API_TOKEN',
    'FEISHU_APP_SECRET',
)

DUrable_NAME = 'credentials.bin'


def store_path() -> Path:
    return paths.cache_path(DUrable_NAME)


class CredentialError(RuntimeError):
    """加密存储不可用或内容损坏。"""


def _dpapi():
    try:
        import win32crypt
    except ImportError as exc:      # pragma: no cover - 取决于平台
        raise CredentialError(
            'DPAPI unavailable: install pywin32 (pip install pywin32) '
            'or keep using .env') from exc
    return win32crypt


def available() -> bool:
    """当前平台能否加密。非 Windows 一律不可用（不做自研加密）。"""
    if os.name != 'nt':
        return False
    try:
        _dpapi()
    except CredentialError:
        return False
    return True


def _entropy() -> bytes:
    # 额外熵把密文绑定到本应用：同一用户下别的程序即使调用 DPAPI 也没法
    # 直接解开这份数据（它仍能读到明文环境变量，见模块顶部的边界说明）。
    return b'harness-agent/credentials/v1'


def encrypt(values: dict) -> bytes:
    if not available():
        raise CredentialError('this platform cannot encrypt credentials')
    win32crypt = _dpapi()
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True).encode('utf-8')
    blob = win32crypt.CryptProtectData(payload, 'harness credentials',
                                       _entropy(), None, None, 0)
    return b'HARNESS1' + blob


def decrypt(blob: bytes) -> dict:
    if not blob.startswith(b'HARNESS1'):
        raise CredentialError('unrecognized credentials file format')
    win32crypt = _dpapi()
    try:
        _desc, data = win32crypt.CryptUnprotectData(
            blob[len(b'HARNESS1'):], _entropy(), None, None, 0)
    except Exception as exc:
        raise CredentialError(
            'cannot decrypt credentials (different Windows user or machine?): '
            f'{type(exc).__name__}') from exc
    try:
        values = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CredentialError(f'credentials payload is corrupt: {exc}') from exc
    if not isinstance(values, dict):
        raise CredentialError('credentials payload must be a mapping')
    return {str(key): str(value) for key, value in values.items()}


def save(values: dict) -> Path:
    """加密写入；只保留 ``SECRET_KEYS`` 里的键，避免把整份 .env 复制进来。"""
    if not available():
        raise CredentialError('this platform cannot encrypt credentials')
    picked = {key: str(values[key]) for key in SECRET_KEYS if values.get(key)}
    if not picked:
        raise CredentialError('no known secret keys to save')
    target = store_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    blob = encrypt(picked)
    tmp = target.with_suffix('.tmp')
    tmp.write_bytes(blob)
    tmp.replace(target)
    return target


def load() -> dict:
    """读取并解密；文件不存在返回空字典，损坏抛出 CredentialError。

    「不存在」与「读不了」必须区分：前者是正常的未迁移状态，后者要让用户
    知道（否则会以为自己在用加密凭据、实际在跑明文）。
    """
    target = store_path()
    if not target.exists():
        return {}
    return decrypt(target.read_bytes())


def apply(values: dict | None = None, *, environ=None) -> list[str]:
    """把解密出的值写进环境变量，返回实际覆盖的键。

    只覆盖 ``SECRET_KEYS``；调用方传 ``values`` 可跳过重复读盘。
    """
    env = os.environ if environ is None else environ
    if values is None:
        try:
            values = load()
        except CredentialError as exc:
            print(f'  \033[33m[credentials] {exc}；继续使用 .env 明文凭据\033[0m')
            return []
    applied = []
    for key in SECRET_KEYS:
        value = values.get(key)
        if value:
            env[key] = value
            applied.append(key)
    return applied


def status() -> dict:
    """给 /status 与 --doctor 的一行状态。"""
    target = store_path()
    result = {
        'available': available(),
        'path': str(target),
        'exists': target.exists(),
        'keys': [],
        'error': '',
    }
    if not target.exists():
        return result
    try:
        loaded = load()
    except CredentialError as exc:
        result['error'] = str(exc)
        return result
    result['keys'] = sorted(loaded)
    return result


def describe() -> str:
    state = status()
    if not state['available']:
        return '凭据：明文 .env（当前平台不支持 DPAPI 加密）'
    if not state['exists']:
        return '凭据：明文 .env（未迁移；运行 scripts/secure_credentials.py migrate）'
    if state['error']:
        return f"凭据：加密文件不可用（{state['error']}）"
    return (f"凭据：加密文件（{len(state['keys'])} 项："
            f"{', '.join(state['keys'])}）")


def fingerprint(values: dict) -> str:
    """密钥指纹（前 4 位 base64），用来确认「网关和 API 用的是同一份」。"""
    import hashlib

    digest = hashlib.sha256(
        ''.join(values.get(key, '') for key in SECRET_KEYS).encode('utf-8')
    ).digest()
    return base64.b32encode(digest).decode('ascii')[:8]
