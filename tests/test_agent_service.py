"""常驻服务监督：启动顺序、就绪通知、失败收敛与子进程退出。"""
import socket

import pytest

import agent_service


class FakeProc:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


class FakePopen:
    def __init__(self, procs):
        self.procs = list(procs)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return self.procs.pop(0)


class Notes:
    def __init__(self):
        self.calls = []

    def __call__(self, title, body='', **kwargs):
        self.calls.append((title, body, kwargs))


@pytest.fixture
def env():
    return {
        'HARNESS_API_TOKEN': 'token',
        'FEISHU_APP_ID': 'cli_x',
        'FEISHU_APP_SECRET': 'secret',
        'FEISHU_ALLOWED_USERS': 'ou_x',
    }


def _start(tmp_path, env, procs, *, notifier=None, probe=None, **kwargs):
    popen = FakePopen(procs)
    kwargs.setdefault('port_check', lambda host, port: False)
    service = agent_service.start_service(
        env=env, log_dir=tmp_path / 'logs', popen=popen,
        notifier=notifier or Notes(),
        probe=probe or (lambda url: True),
        sleep=kwargs.pop('sleep', lambda seconds: None),
        grace=kwargs.pop('grace', 0), **kwargs)
    return service, popen


def test_start_service_launches_api_then_gateway(tmp_path, env):
    api, gateway = FakeProc(), FakeProc()
    notes = Notes()
    service, popen = _start(tmp_path, env, [api, gateway], notifier=notes)

    assert service is not None
    assert service.base_url == 'http://127.0.0.1:8000'
    assert [child.name for child in service.children] == ['api', 'feishu']
    api_command, api_kwargs = popen.calls[0]
    assert api_command[1:4] == ['-m', 'uvicorn', 'api:create_app']
    assert '--port' in api_command
    gateway_command, gateway_kwargs = popen.calls[1]
    assert gateway_command[-1].endswith('feishu_gateway.py')
    assert gateway_kwargs['env']['HARNESS_API_URL'] == service.base_url
    assert api_kwargs['cwd'] == str(agent_service._repo_root())
    assert notes.calls[0][0] == 'Harness Agent 已就绪'
    assert '飞书网关在线' in notes.calls[0][1]

    service.stop()
    assert api.terminated and gateway.terminated


def test_gateway_is_skipped_without_feishu_config(tmp_path):
    env = {'HARNESS_API_TOKEN': 'token'}
    notes = Notes()
    service, popen = _start(tmp_path, env, [FakeProc()], notifier=notes)
    assert service is not None
    assert [child.name for child in service.children] == ['api']
    assert len(popen.calls) == 1
    assert '未配置飞书网关' in notes.calls[0][1]
    service.stop()


def test_missing_token_fails_without_spawning(tmp_path, env):
    env.pop('HARNESS_API_TOKEN')
    notes = Notes()
    service, popen = _start(tmp_path, env, [], notifier=notes)
    assert service is None
    assert popen.calls == []
    assert notes.calls[0][0] == 'Harness Agent 启动失败'
    assert 'HARNESS_API_TOKEN' in notes.calls[0][1]


def test_busy_port_fails_before_spawning(tmp_path, env):
    notes = Notes()
    service, popen = _start(tmp_path, env, [], notifier=notes,
                            port_check=lambda host, port: True)
    assert service is None
    assert popen.calls == []
    assert '已被占用' in notes.calls[0][1]


def test_api_exit_during_startup_is_reported(tmp_path, env):
    notes = Notes()
    service, popen = _start(tmp_path, env, [FakeProc(returncode=7)],
                            notifier=notes, probe=lambda url: False)
    assert service is None
    assert len(popen.calls) == 1
    assert 'API 未在' in notes.calls[0][1]


def test_gateway_exit_during_grace_stops_everything(tmp_path, env):
    api, gateway = FakeProc(), FakeProc(returncode=3)
    notes = Notes()
    service, popen = _start(tmp_path, env, [api, gateway], notifier=notes)
    assert service is None
    assert api.terminated
    assert notes.calls[0][0] == 'Harness Agent 启动失败'
    assert '飞书网关启动后退出' in notes.calls[0][1]


def test_monitor_reports_child_exit(tmp_path):
    handle = open(tmp_path / 'feishu.log', 'ab')
    child = agent_service.Child('feishu', FakeProc(returncode=9),
                                tmp_path / 'feishu.log', handle)
    service = agent_service.Service([child], 'http://127.0.0.1:8000')
    notes = Notes()
    rc = agent_service.monitor(service, notifier=notes,
                               sleep=lambda seconds: None)
    assert rc == 4
    assert notes.calls[0][0] == 'Harness Agent 子进程退出'
    assert 'feishu 已退出' in notes.calls[0][1]
    handle.close()


def test_monitor_returns_zero_on_interrupt(tmp_path):
    handle = open(tmp_path / 'api.log', 'ab')
    child = agent_service.Child('api', FakeProc(), tmp_path / 'api.log', handle)
    notes = Notes()

    def interrupted(seconds):
        raise KeyboardInterrupt

    assert agent_service.monitor(agent_service.Service([child]), notifier=notes,
                                 sleep=interrupted) == 0
    assert notes.calls == []
    handle.close()


def test_build_commands_use_current_interpreter():
    api = agent_service.build_api_command('127.0.0.1', 8123)
    assert api[0] == agent_service.sys.executable
    assert api[-4:] == ['--host', '127.0.0.1', '--port', '8123']
    gateway = agent_service.build_gateway_command()
    assert gateway[0] == agent_service.sys.executable
    assert gateway[1].endswith('feishu_gateway.py')


def test_missing_feishu_variants():
    assert agent_service.missing_feishu({}) == [
        'FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'FEISHU_ALLOWED_USERS']
    assert agent_service.missing_feishu({
        'FEISHU_APP_ID': 'a', 'FEISHU_APP_SECRET': 'b',
        'FEISHU_ALLOWED_USERS': 'ou_x'}) == []
    assert agent_service.missing_feishu({
        'FEISHU_APP_ID': 'a', 'FEISHU_APP_SECRET': 'b',
        'FEISHU_ALLOW_ANY_USER': 'TRUE'}) == []


def test_child_env_forces_utf8_logs():
    merged = agent_service.child_env({'X': '1'})
    assert merged['X'] == '1'
    assert merged['PYTHONIOENCODING'] == 'utf-8'
    assert merged['PYTHONUNBUFFERED'] == '1'


def test_port_in_use_detects_listener():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert agent_service._port_in_use('127.0.0.1', port) is True
    assert agent_service._port_in_use('127.0.0.1', port) is False


def test_log_rotates_when_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_service, 'LOG_ROTATE_BYTES', 8)
    log = tmp_path / 'api.log'
    log.write_bytes(b'x' * 9)
    path, handle = agent_service._open_log(tmp_path, 'api')
    handle.close()
    assert path == log
    assert log.read_bytes() == b''
    assert (tmp_path / 'api.1.log').read_bytes() == b'x' * 9
