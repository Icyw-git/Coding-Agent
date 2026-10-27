"""可用性回归：审批面信息量、路径错误提示、VCS 目录保护。"""
import pytest

import approval_view as av
import checkpoints
from exec_policy import PATH_SECRET, PATH_WORKSPACE, classify_path

# ---------- 补丁审批：先看影响面 ----------

def test_patch_detail_shows_touched_paths_and_stats():
    """审阅补丁第一眼要看影响面：动了几处、哪些新增哪些删除。"""
    patch = '\n'.join([
        '*** Begin Patch',
        '*** Add File: a.txt',
        '+hello',
        '+world',
        '*** Update File: b.py',
        '-x = 1',
        '+x = 2',
        '*** Delete File: c.md',
        '*** End Patch',
    ])
    text = av.detail('apply_patch', {'patch': patch})
    assert 'patch touches 3 path(s)' in text
    assert 'add a.txt' in text
    assert 'update b.py' in text
    assert 'delete c.md' in text
    # +hello、+world、+x = 2 三行新增，-x = 1 一行删除
    assert '+3/-1' in text


def test_patch_detail_handles_move():
    patch = '\n'.join(['*** Begin Patch', '*** Update File: old.py',
                       '*** Move to: new.py', '+x', '*** End Patch'])
    text = av.detail('apply_patch', {'patch': patch})
    assert 'rename to new.py' in text


def test_patch_detail_without_recognisable_headers_falls_back():
    """解析不出文件头时退回内容预览，而不是显示空白。"""
    text = av.detail('apply_patch', {'patch': 'just some text'})
    assert 'just some text' in text


# ---------- VCS 目录：不能当普通工作区 ----------

@pytest.mark.parametrize('path', [
    '.git/config',
    '.git/hooks/pre-commit',
    '.git/x',
    '.hg/hgrc',
    '.svn/entries',
])
def test_vcs_internals_are_secret(tmp_path, path):
    """`.git` 里改一处就能改 remote 或植入 hook，必须按秘密区硬拒。

    它和工作区里普通文件的区别是「改完立即生效、不需要额外动作」：
    `.github/workflows` 要 push 才生效，`setup.py` 要安装才执行，
    而 `.git/hooks/pre-commit` 下一次 commit 就跑。
    """
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / '.git' / 'hooks').mkdir(parents=True)
    zone = classify_path(path, workspace=workspace,
                         secrets=checkpoints.DEFAULT_SECRETS)
    assert zone == PATH_SECRET


@pytest.mark.parametrize('path', [
    'git-notes/a.txt',        # 只是名字里带 git
    'src/.github-readme.md',
    'conf/hgrc',              # 不带点的普通文件
])
def test_lookalike_paths_stay_workspace(tmp_path, path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'git-notes').mkdir()
    (workspace / 'src').mkdir()
    (workspace / 'conf').mkdir()
    assert classify_path(path, workspace=workspace,
                         secrets=checkpoints.DEFAULT_SECRETS) == PATH_WORKSPACE


def test_real_tool_refuses_git_internals(tmp_path, monkeypatch):
    """走真实工具链路：write 到 .git 应被硬拒。"""
    import harness
    import hooks
    import tools

    workspace = tmp_path / 'ws'
    (workspace / '.git' / 'hooks').mkdir(parents=True)

    class FakeHarness:
        DENY_LIST = []
        DESTRUCTIVE = []
        SESSIONS_DIR = tmp_path / 'sessions'
        SessionEventStore = None

        @staticmethod
        def get_workdir():
            return workspace

        @staticmethod
        def active_checkpoint_manager():
            return None

    monkeypatch.setattr(hooks, '_h', lambda: FakeHarness)
    monkeypatch.setattr(harness, 'get_workdir', lambda: workspace)
    monkeypatch.setattr(harness, 'active_checkpoint_manager', lambda: None)

    message = tools.run_write('.git/hooks/pre-commit', '#!/bin/sh')
    assert 'protected path' in message
    assert not (workspace / '.git' / 'hooks' / 'pre-commit').exists()


# ---------- 路径类错误的下一步 ----------

def test_reading_a_directory_says_what_to_do(tmp_path):
    import tools

    message = tools.run_read(str(tmp_path))
    assert 'is a directory' in message
    assert 'ls' in message


def test_reading_a_document_suffix_redirects(tmp_path):
    import tools

    pdf = tmp_path / 'a.pdf'
    pdf.write_bytes(b'%PDF-1.4')
    message = tools.run_read(str(pdf))
    assert 'read_document' in message
