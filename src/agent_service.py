"""常驻服务监督进程：启动 API 与飞书网关，日志落 ``.cache/logs``。

只做“起不来要吼一声”：API 就绪、网关存活后发一条本地 Toast；任一子进程
退出就通知并结束自己。默认不自动重启 —— 配置错误时无限拉起只会刷屏。
由 ``scripts/install_autostart.py`` 注册为登录任务，也可以手动运行：:

    python src/agent_service.py [--host 127.0.0.1] [--port 8000] [--no-feishu]

API 用 ``python -m uvicorn api:create_app`` 启动，工作区固定为仓库根；
飞书网关是可选的，缺少 ``FEISHU_*`` 配置时只跑 API。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import notify
import paths

API_START_TIMEOUT = 30.0
STARTUP_GRACE = 5.0
POLL_INTERVAL = 2.0
LOG_DIR = 'logs'
LOG_ROTATE_BYTES = 10 * 1024 * 1024


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def build_api_command(host: str, port: int) -> list[str]:
    return [sys.executable, '-m', 'uvicorn', 'api:create_app', '--factory',
            '--host', host, '--port', str(port)]


def build_gateway_command() -> list[str]:
    return [sys.executable, str(_repo_root() / 'src' / 'feishu_gateway.py')]


def child_env(env=None) -> dict:
    merged = dict(os.environ if env is None else env)
    # 日志是 UTF-8 文件：避免子进程按 GBK 输出，也避免缓冲区让故障日志迟到
    merged['PYTHONIOENCODING'] = 'utf-8'
    merged['PYTHONUNBUFFERED'] = '1'
    return merged


def missing_feishu(env=None) -> list[str]:
    env = os.environ if env is None else env
    missing = [key for key in ('FEISHU_APP_ID', 'FEISHU_APP_SECRET')
               if not (env.get(key) or '').strip()]
    allowed = (env.get('FEISHU_ALLOWED_USERS') or '').strip()
    allow_any = (env.get('FEISHU_ALLOW_ANY_USER') or '').strip().casefold() \
        in ('1', 'true', 'yes')
    if not allowed and not allow_any:
        missing.append('FEISHU_ALLOWED_USERS')
    return missing


def _probe_host(host: str) -> str:
    return '127.0.0.1' if host in ('', '0.0.0.0', '::') else host


def _port_in_use(host: str, port: int) -> bool:
    try:
        with socket.create_connection((_probe_host(host), port), timeout=0.5):
            return True
    except OSError:
        return False


def probe_api(base_url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip('/') + '/openapi.json',
                                    timeout=timeout) as response:
            return 200 <= response.status < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


@dataclass
class Child:
    name: str
    proc: subprocess.Popen
    log_path: Path
    handle: IO

    def stop(self, timeout: float = 10.0) -> None:
        proc = self.proc
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                import process_utils
                process_utils.kill_process_tree(proc)
        try:
            self.handle.close()
        except OSError:
            pass


@dataclass
class Service:
    children: list[Child] = field(default_factory=list)
    base_url: str = ''

    def stop(self) -> None:
        for child in reversed(self.children):
            child.stop()
        self.children.clear()


def _open_log(log_dir: Path, name: str) -> tuple[Path, IO]:
    path = log_dir / f'{name}.log'
    try:
        if path.stat().st_size >= LOG_ROTATE_BYTES:
            path.replace(log_dir / f'{name}.1.log')
    except OSError:
        pass
    return path, open(path, 'ab')


def _start(name: str, command: list[str], log_dir: Path, env: dict, popen) -> Child:
    path, handle = _open_log(log_dir, name)
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    try:
        proc = popen(command, cwd=str(_repo_root()), env=env,
                     stdin=subprocess.DEVNULL, stdout=handle,
                     stderr=subprocess.STDOUT, creationflags=flags)
    except OSError:
        handle.close()
        raise
    return Child(name, proc, path, handle)


def _tail(path: Path, limit: int = 500) -> str:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    return ' '.join(text[-limit:].split())


def _fail(notifier, message: str) -> None:
    print(f'[service] {message}', file=sys.stderr)
    if notifier is not None:
        notifier('Harness Agent 启动失败', message, level='error')


def wait_for_api(base_url: str, child: Child, timeout: float,
                 *, probe=None, sleep=time.sleep) -> bool:
    checker = probe or probe_api
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.proc.poll() is not None:
            return False
        if checker(base_url):
            return True
        sleep(0.25)
    return False


def start_service(*, host: str = '127.0.0.1', port: int = 8000,
                  with_gateway: bool = True, env=None, log_dir=None,
                  notifier=None, popen=None, probe=None, port_check=None,
                  api_timeout: float = API_START_TIMEOUT,
                  grace: float = STARTUP_GRACE, sleep=time.sleep):
    """启动 API（和可选的飞书网关），成功返回 ``Service``，失败返回 ``None``。"""
    env = dict(os.environ if env is None else env)
    notifier = notifier if notifier is not None else notify.notify
    popen = popen or subprocess.Popen

    if not (env.get('HARNESS_API_TOKEN') or '').strip():
        _fail(notifier, '缺少 HARNESS_API_TOKEN，API 无法启动')
        return None
    busy = (port_check or _port_in_use)(host, port)
    if busy:
        _fail(notifier, f'端口 {port} 已被占用，API 无法启动')
        return None

    log_dir = Path(log_dir) if log_dir is not None else paths.cache_path(LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    base = f'http://{_probe_host(host)}:{port}'
    api_env = child_env(env)

    try:
        api = _start('api', build_api_command(host, port), log_dir, api_env, popen)
    except OSError as exc:
        _fail(notifier, f'API 启动失败：{exc}')
        return None
    if not wait_for_api(base, api, api_timeout, probe=probe, sleep=sleep):
        tail = _tail(api.log_path)
        api.stop()
        _fail(notifier, f'API 未在 {api_timeout:g} 秒内就绪'
                        + (f'：{tail}' if tail else ''))
        return None

    children = [api]
    missing = missing_feishu(env)
    gateway_status = '未配置飞书网关'
    if with_gateway and not missing:
        gateway_env = child_env(env)
        gateway_env['HARNESS_API_URL'] = base
        try:
            gateway = _start('feishu', build_gateway_command(), log_dir,
                             gateway_env, popen)
        except OSError as exc:
            service = Service(children, base)
            service.stop()
            _fail(notifier, f'飞书网关启动失败：{exc}')
            return None
        children.append(gateway)
        if grace > 0:
            sleep(grace)
        if gateway.proc.poll() is not None:
            code = gateway.proc.returncode
            tail = _tail(gateway.log_path)
            service = Service(children, base)
            service.stop()
            _fail(notifier, f'飞书网关启动后退出（code {code}）'
                            + (f'：{tail}' if tail else ''))
            return None
        gateway_status = '飞书网关在线'
    elif with_gateway:
        print(f'[service] 跳过飞书网关：缺少 {", ".join(missing)}')
    else:
        gateway_status = '已跳过飞书网关'

    print(f'[service] ready: {base} · {gateway_status}')
    if notifier is not None:
        notifier('Harness Agent 已就绪', f'API {base} · {gateway_status}',
                 level='info')
    return Service(children, base)


def monitor(service: Service, *, notifier=None, interval: float = POLL_INTERVAL,
            sleep=time.sleep) -> int:
    """等到某个子进程退出；返回 0（中断）或 4（子进程退出）。"""
    notifier = notifier if notifier is not None else notify.notify
    while True:
        for child in service.children:
            code = child.proc.poll()
            if code is None:
                continue
            message = f'{child.name} 已退出（code {code}）；日志：{child.log_path}'
            print(f'[service] {message}', file=sys.stderr)
            if notifier is not None:
                notifier('Harness Agent 子进程退出', message, level='error')
            return 4
        try:
            sleep(interval)
        except KeyboardInterrupt:
            return 0


def _redirect_stdio_if_needed():
    """pythonw / 计划任务下 stdout/stderr 可能为 None，print 会直接炸。"""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    log_path = paths.cache_path(LOG_DIR, 'service.log')
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(log_path, 'a', encoding='utf-8',
                  errors='backslashreplace', buffering=1)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return stream


def main(argv=None) -> int:
    import argparse

    _redirect_stdio_if_needed()
    import console
    console.harden_stdio()

    parser = argparse.ArgumentParser(description='Harness Agent 常驻服务')
    parser.add_argument('--host', default=os.getenv('HARNESS_API_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int,
                        default=int(os.getenv('HARNESS_API_PORT', '8000')))
    parser.add_argument('--no-feishu', action='store_true', help='只启动 API')
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv()
        import credentials
        credentials.apply()   # 密文凭据优先；失败时继续用 .env
    except ImportError:
        pass
    except Exception:
        pass

    service = start_service(host=args.host, port=args.port,
                            with_gateway=not args.no_feishu)
    if service is None:
        return 1
    try:
        return monitor(service)
    except KeyboardInterrupt:
        print('\n[service] 已停止')
        return 0
    finally:
        service.stop()


if __name__ == '__main__':
    raise SystemExit(main())
