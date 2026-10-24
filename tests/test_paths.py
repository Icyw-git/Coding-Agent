"""agent home 与工作区解耦：换目录启动仍能看到同一批会话。"""
import os
import subprocess
import sys
from pathlib import Path

import harness
import paths

ROOT = Path(__file__).resolve().parents[1]


def _run(code: str, *, cwd: Path, home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env['HARNESS_HOME'] = str(home)
    env['PYTHONPATH'] = str(ROOT)
    env['PYTHONIOENCODING'] = 'utf-8'
    env.pop('HARNESS_WORKSPACE', None)
    return subprocess.run(
        [sys.executable, '-c', code],
        cwd=str(cwd), env=env, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=120)


def test_home_defaults_to_repo_root(monkeypatch):
    monkeypatch.delenv('HARNESS_HOME', raising=False)
    assert paths.home_dir() == ROOT
    assert paths.cache_dir() == ROOT / '.cache'


def test_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv('HARNESS_HOME', str(tmp_path))
    assert paths.home_dir() == tmp_path.resolve()
    assert paths.cache_path('sessions') == tmp_path.resolve() / '.cache' / 'sessions'


def test_harness_cache_dirs_follow_home_not_cwd():
    # harness 在导入时解析路径：默认 home 是仓库根，而不是进程 cwd。
    # 会话/快照目录会被 conftest 隔离到 tmp_path，这里断言 import 期常量本身。
    assert harness.HOME_DIR == paths.home_dir()
    assert harness.CACHE_DIR == paths.cache_dir()
    # MEMORY_DIR 也被 conftest 隔离，这里只断言 home 的解析结果。


def test_sessions_are_visible_from_another_directory(tmp_path):
    home = tmp_path / 'home'
    work_a = tmp_path / 'work-a'
    work_b = tmp_path / 'work-b'
    for directory in (home, work_a, work_b):
        directory.mkdir()

    create = _run(
        'import harness\n'
        'store = harness.start_new_session()\n'
        'print("SESSION", store.session_id)\n'
        'print("DIR", harness.SESSIONS_DIR)\n',
        cwd=work_a, home=home)
    assert create.returncode == 0, create.stderr
    lines = dict(line.split(' ', 1) for line in create.stdout.splitlines()
                 if line.startswith('SESSION ') or line.startswith('DIR '))
    session_id = lines['SESSION']
    assert Path(lines['DIR']) == home.resolve() / '.cache' / 'sessions'
    assert (home / '.cache' / 'sessions' / f'session_{session_id}.jsonl').is_file()
    assert not (work_a / '.cache' / 'sessions').exists()

    listing = _run(
        'import harness\n'
        'runtime = harness.create_runtime(initialize_tools=False)\n'
        'print("IDS", ",".join(row["id"] for row in runtime.list_sessions()))\n'
        'print("DIR", harness.SESSIONS_DIR)\n',
        cwd=work_b, home=home)
    assert listing.returncode == 0, listing.stderr
    assert session_id in listing.stdout
    assert str(home.resolve() / '.cache' / 'sessions') in listing.stdout
