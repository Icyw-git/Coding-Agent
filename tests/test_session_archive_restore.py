"""归档 → 恢复的往返：内容必须一字不差地回来。

`SessionArchive.archive()` 会把会话日志、快照目录、子代理日志一起挪进归档目录，
`restore()` 再挪回来。这是**动真实数据**的操作，但之前只测了删除和几条拒绝路径
（`test_session_delete.py`），没有一条断言「恢复之后内容还在」。

搬文件最典型的失败不是抛异常，而是**少搬一部分**：日志回来了、快照没回来，
或者子代理日志丢了——不报错，只是 `/rewind` 或 `/resume` 时才发现少了东西。
"""

from session_cleanup import SessionArchive
from session_store import SessionEventStore


def _archived(tmp_path, session_id='old'):
    store = SessionEventStore(tmp_path / 'sessions', session_id=session_id)
    store.append_message({'role': 'user', 'content': '旧任务'})
    store.append_message({'role': 'assistant', 'content': '做完了'})
    checkpoints = tmp_path / 'checkpoints' / session_id
    checkpoints.mkdir(parents=True)
    (checkpoints / 'snapshot').write_text('snapshot-bytes')
    child = store.directory / f'session_{session_id}.subagent'
    child.mkdir()
    (child / 'child.jsonl').write_text('{"type":"message"}\n')
    archive = SessionArchive(store.directory, checkpoints.parent)
    return archive, store, store.session_summary()


def test_restore_brings_back_the_log_checkpoints_and_child_logs(tmp_path):
    archive, store, row = _archived(tmp_path)

    archive.archive(row, 'current')
    assert not store.path.exists(), '归档后原位置不该还有日志'
    assert archive.list(), '归档列表里应该看得到'

    archive.restore(row['id'])

    assert store.path.exists(), '恢复后日志要回到原位'
    events = store.read_events()
    contents = [str(event.get('message', {}).get('content', ''))
                for event in events if event.get('type') == 'message']
    assert contents == ['旧任务', '做完了'], contents

    restored = tmp_path / 'checkpoints' / 'old' / 'snapshot'
    assert restored.read_text() == 'snapshot-bytes', '快照没跟着回来'

    child = store.directory / 'session_old.subagent' / 'child.jsonl'
    assert child.is_file(), '子代理日志没跟着回来'
