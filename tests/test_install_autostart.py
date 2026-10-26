"""自启任务安装器：只验证命令构造与 dry-run，不触碰真实计划任务。"""
from types import SimpleNamespace

from scripts import install_autostart as auto


def _recorder(*, returncode=0, stdout='', stderr=''):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return runner, calls


def test_create_command_shape_and_quoting():
    command = auto.build_create_command(
        'HarnessAgent', python=r'C:\Program Files\Py\pythonw.exe',
        script=r'D:\repo\src\agent_service.py')
    assert command == [
        'schtasks', '/Create', '/TN', 'HarnessAgent',
        '/TR', '"C:\\Program Files\\Py\\pythonw.exe" "D:\\repo\\src\\agent_service.py"',
        '/SC', 'ONLOGON', '/RL', 'LIMITED', '/F',
    ]


def test_delete_and_query_commands():
    assert auto.build_delete_command() == ['schtasks', '/Delete', '/TN',
                                           'HarnessAgent', '/F']
    assert auto.build_query_command() == ['schtasks', '/Query', '/TN',
                                          'HarnessAgent']


def test_default_python_prefers_pythonw(tmp_path, monkeypatch):
    python = tmp_path / 'python.exe'
    pythonw = tmp_path / 'pythonw.exe'
    python.write_text('')
    pythonw.write_text('')
    monkeypatch.setattr(auto.sys, 'executable', str(python))
    assert auto.default_python('win32') == str(pythonw)
    assert auto.default_python('linux') == str(python)


def test_dry_run_prints_command_without_running(capsys):
    runner, calls = _recorder()
    rc = auto.main(['install', '--dry-run', '--python', r'C:\Py\pythonw.exe'],
                   runner=runner)
    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert 'schtasks' in out
    assert '/Create' in out
    assert 'ONLOGON' in out


def test_install_runs_schtasks_and_reports(capsys):
    runner, calls = _recorder(stdout='SUCCESS: The scheduled task was created.')
    rc = auto.main(['install', '--python', r'C:\Py\pythonw.exe'], runner=runner)
    assert rc == 0
    command, kwargs = calls[0]
    assert command == auto.build_create_command(
        python=r'C:\Py\pythonw.exe')
    assert kwargs['capture_output'] is True
    out = capsys.readouterr().out
    assert '已注册 HarnessAgent' in out


def test_remove_runs_schtasks(capsys):
    runner, calls = _recorder()
    assert auto.main(['remove'], runner=runner) == 0
    assert calls[0][0] == auto.build_delete_command()
    assert '已删除 HarnessAgent' in capsys.readouterr().out


def test_status_reports_installed_and_missing(capsys):
    runner, _ = _recorder(stdout='TaskName                                 Next Run Time')
    assert auto.main(['status'], runner=runner) == 0
    assert '已安装' in capsys.readouterr().out

    runner, _ = _recorder(returncode=1, stderr='ERROR: The system cannot find')
    assert auto.main(['status'], runner=runner) == 1
    captured = capsys.readouterr()
    assert '未安装' in captured.out
    assert 'cannot find' in captured.err


def test_non_windows_refuses_real_actions():
    runner, calls = _recorder()
    assert auto.main(['install'], runner=runner, platform='linux') == 2
    assert calls == []


def test_service_script_exists():
    assert auto.service_script().is_file()
    assert auto.service_script().name == 'agent_service.py'
