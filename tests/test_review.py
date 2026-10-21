"""只读 /review：工具面、工作区路径限制、敏感文件过滤与轮次预算。"""
import copy
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import harness
import review
import test_context_manage as tcm

pytestmark = pytest.mark.skipif(
    shutil.which('git') is None, reason='git is not installed')


def _git(repo, *args):
    return subprocess.run(['git', *args], cwd=str(repo), check=True,
                          capture_output=True, text=True, encoding='utf-8')


class RecordingCompletions:
    """记录完整请求参数（FakeCompletions 只保留 messages）。"""

    def __init__(self, items):
        self.items = list(items)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(items):
    completions = RecordingCompletions(items)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


@pytest.fixture()
def repo(tmp_path):
    path = tmp_path / 'repo'
    path.mkdir()
    _git(path, 'init', '-q')
    _git(path, 'config', 'user.email', 'test@example.com')
    _git(path, 'config', 'user.name', 'Test')
    (path / 'app.py').write_text('def add(a, b):\n    return a + b\n',
                                 encoding='utf-8')
    _git(path, 'add', '.')
    _git(path, 'commit', '-qm', 'init')
    return path


def test_review_clean_workspace_does_not_call_model(repo):
    client, fake = _client([])
    output = review.run_review(repo, client=client)
    assert '工作区干净' in output
    assert fake.calls == []


def test_review_non_git_workspace(tmp_path):
    workspace = tmp_path / 'plain'
    workspace.mkdir()
    client, fake = _client([])
    output = review.run_review(workspace, client=client)
    assert '不是 Git 仓库' in output
    assert fake.calls == []


def test_review_uses_read_only_surface_and_reads_workspace(repo):
    (repo / 'app.py').write_text('def add(a, b):\n    return a - b\n',
                                 encoding='utf-8')
    client, fake = _client([
        tcm.make_tool_response([('c1', 'read', '{"path": "app.py"}')]),
        tcm.make_text_response('结论：可以提交。\n测试建议：无'),
    ])

    output = review.run_review(repo, client=client, max_rounds=3)

    assert '结论：可以提交。' in output
    assert '[review] 已审阅 1 个未提交文件' in output
    first = fake.calls[0]
    names = {tool['function']['name'] for tool in first['tools']}
    assert names == {'read', 'glob', 'grep', 'ls'}
    assert 'return a - b' in first['messages'][-1]['content']

    second = fake.calls[1]
    tool_messages = [m for m in second['messages'] if m['role'] == 'tool']
    assert any('return a - b' in m['content'] for m in tool_messages)


def test_review_rejects_paths_outside_workspace(repo, tmp_path):
    (tmp_path / 'secret.txt').write_text('TOP-SECRET\n', encoding='utf-8')
    (repo / 'app.py').write_text('changed\n', encoding='utf-8')
    client, fake = _client([
        tcm.make_tool_response([('c1', 'read', '{"path": "../secret.txt"}')]),
        tcm.make_text_response('结论：未读取外部文件。'),
    ])

    output = review.run_review(repo, client=client, max_rounds=3)

    assert '未读取外部文件' in output
    second = fake.calls[1]
    tool_messages = [m for m in second['messages'] if m['role'] == 'tool']
    assert any('only reads inside the workspace' in m['content']
               for m in tool_messages)
    assert 'TOP-SECRET' not in str(second['messages'])


def test_review_skips_secret_files(repo):
    (repo / '.env').write_text('API_KEY=SUPER_SECRET\n', encoding='utf-8')
    (repo / 'app.py').write_text('changed\n', encoding='utf-8')
    client, fake = _client([
        tcm.make_text_response('结论：可以提交。'),
    ])

    output = review.run_review(repo, client=client)

    assert '跳过 1 个敏感文件' in output
    prompt = fake.calls[0]['messages'][-1]['content']
    assert 'app.py' in prompt
    assert 'SUPER_SECRET' not in prompt
    assert '.env' not in prompt


def test_review_only_secret_changes_do_not_call_model(repo):
    (repo / '.env').write_text('API_KEY=SUPER_SECRET\n', encoding='utf-8')
    client, fake = _client([])
    output = review.run_review(repo, client=client)
    assert '只涉及敏感文件' in output
    assert fake.calls == []


def test_review_round_budget_forces_final_answer(repo):
    (repo / 'app.py').write_text('changed\n', encoding='utf-8')
    client, fake = _client([
        tcm.make_tool_response([('c1', 'ls', '{"path": "."}')]),
        tcm.make_text_response('结论：工具预算用尽后的最终结论。'),
    ])

    output = review.run_review(repo, client=client, max_rounds=1)

    assert '最终结论' in output
    assert 'tools' in fake.calls[0]
    assert 'tools' not in fake.calls[1]


def test_handle_command_review_dispatches(monkeypatch, capsys):
    monkeypatch.setattr(review, 'run_review',
                        lambda **kwargs: 'REVIEW-RESULT')
    harness.handle_command('/review', [], None)
    assert 'REVIEW-RESULT' in capsys.readouterr().out


def test_review_command_has_no_arguments():
    from cli_commands import command_error, format_command_help
    assert '/review' in format_command_help()
    assert command_error('/review', 'extra')
