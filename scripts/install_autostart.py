"""安装/卸载 Windows 登录自启任务（schtasks），不会自动执行安装。

用法::

    python scripts/install_autostart.py install --dry-run   # 只看要执行的命令
    python scripts/install_autostart.py install             # 真正注册
    python scripts/install_autostart.py status
    python scripts/install_autostart.py remove

任务以当前用户、``/RL LIMITED``（不提权）、``ONLOGON`` 触发，运行
``src/agent_service.py``：优先用同目录的 ``pythonw.exe``，避免登录时闪控制台。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

TASK_NAME = 'HarnessAgent'


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def service_script() -> Path:
    return repo_root() / 'src' / 'agent_service.py'


def default_python(platform: str | None = None) -> str:
    platform = sys.platform if platform is None else platform
    exe = Path(sys.executable)
    if platform.startswith('win'):
        pythonw = exe.with_name('pythonw.exe')
        if pythonw.exists():
            return str(pythonw)
    return str(exe)


def task_action(python: str | None = None, script: str | Path | None = None) -> str:
    return f'"{Path(python or default_python())}" "{Path(script or service_script())}"'


def build_create_command(task_name: str = TASK_NAME, *, python: str | None = None,
                         script: str | Path | None = None) -> list[str]:
    return ['schtasks', '/Create', '/TN', task_name,
            '/TR', task_action(python, script),
            '/SC', 'ONLOGON', '/RL', 'LIMITED', '/F']


def build_delete_command(task_name: str = TASK_NAME) -> list[str]:
    return ['schtasks', '/Delete', '/TN', task_name, '/F']


def build_query_command(task_name: str = TASK_NAME) -> list[str]:
    return ['schtasks', '/Query', '/TN', task_name]


def _run(command: list[str], runner=None):
    runner = runner or subprocess.run
    return runner(command, capture_output=True, text=True, check=False)


def _emit(result) -> None:
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)


def main(argv=None, *, runner=None, platform: str | None = None) -> int:
    platform = sys.platform if platform is None else platform
    parser = argparse.ArgumentParser(description='Harness Agent 登录自启任务')
    parser.add_argument('action', choices=('install', 'remove', 'status'))
    parser.add_argument('--task-name', default=TASK_NAME)
    parser.add_argument('--python', default=None,
                        help='运行服务用的解释器（默认同目录 pythonw.exe）')
    parser.add_argument('--dry-run', action='store_true', help='只打印命令，不执行')
    args = parser.parse_args(argv)

    if args.action == 'install':
        command = build_create_command(args.task_name, python=args.python)
    elif args.action == 'remove':
        command = build_delete_command(args.task_name)
    else:
        command = build_query_command(args.task_name)

    if args.dry_run:
        print(subprocess.list2cmdline(command))
        return 0
    if not platform.startswith('win'):
        print('[autostart] 仅支持 Windows 计划任务', file=sys.stderr)
        return 2

    result = _run(command, runner)
    if args.action == 'status':
        if result.returncode == 0:
            print(f'[autostart] 计划任务 {args.task_name} 已安装')
        else:
            print(f'[autostart] 计划任务 {args.task_name} 未安装')
    _emit(result)
    if args.action == 'install' and result.returncode == 0:
        print(f'[autostart] 已注册 {args.task_name}（当前用户、登录时启动、不自动提权）')
    elif args.action == 'remove' and result.returncode == 0:
        print(f'[autostart] 已删除 {args.task_name}')
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
