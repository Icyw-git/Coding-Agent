"""R1-c 其二：模型调用期间发生的遗忘/改写，必须让旧候选作废（§6.2 / §9.4）。

场景：一轮抽取正在等模型返回（可能要几十秒）。这期间用户说"忘掉那条"。
模型回来后带着**改之前**的记忆清单，把刚被遗忘的内容原样写回去——用户会看到
"我明明让它忘了，它又记回来了"。

契约：抽取/合并开始时记下 `generation`（墓碑代际）与相关条目的 `revision`；
模型返回、写回之前比对，不一致就放弃这一条。

本测试用 `threading.Barrier` 把模型调用卡在中间，让主线程完成遗忘再放行——
不用 sleep 猜时序。
"""

import json
import threading

import pytest

import memory
import memory_tombstone


class _Msg:
    def __init__(self, content):
        self.content = content
        self.reasoning_content = ''


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Reply:
    def __init__(self, content):
        self.choices = [_Choice(content)]


@pytest.fixture()
def home(tmp_path, monkeypatch):
    import harness

    monkeypatch.setattr(harness, 'MEMORY_DIR', tmp_path / 'mem')
    monkeypatch.setattr(harness, 'MEMORY_INDEX', tmp_path / 'mem' / 'MEMORY.md')
    monkeypatch.setattr(memory_tombstone, 'STORE_PATH',
                        tmp_path / 'mem' / 'tombstones.json')
    return tmp_path / 'mem'


def _run_extraction_with(monkeypatch, payload, during_model):
    """在模型调用中间执行 `during_model()`，模拟"等回复期间用户动手了"。"""
    import llm_route

    released = threading.Event()

    def completion(*_args, **_kwargs):
        during_model()
        released.set()
        return _Reply(json.dumps(payload, ensure_ascii=False))

    monkeypatch.setattr(llm_route, 'completion', completion)
    memory.extract_memories([
        {'role': 'user', 'content': '部署用 make ship-prod'},
        {'role': 'assistant', 'content': '记住了'}])
    return released


def test_forget_during_extraction_blocks_the_stale_candidate(home, monkeypatch):
    """模型调用期间被遗忘的那条，回复回来也不能再写进去。"""
    memory.write_memory_file('deploy', 'fact', '部署走 make ship-prod', '正文',
                             source='sess-1')

    def forget_it():
        memory.delete_memory_file('deploy')

    _run_extraction_with(monkeypatch, [
        {'name': 'deploy', 'type': 'fact', 'description': '部署走 make ship-prod',
         'body': '正文'}], forget_it)

    assert memory.list_memory_files() == [], '被遗忘的记忆又被写回来了'


def test_plain_update_during_extraction_also_invalidates_the_candidate(home, monkeypatch):
    """没被忘、但被别的写入改过（revision 变了）：旧候选同样作废。"""
    memory.write_memory_file('deploy', 'fact', '第一版说明', '正文')
    stale = [item for item in memory.list_memory_files()
             if item['name'] == 'deploy'][0]

    def rewrite():
        memory.write_memory_file('deploy', 'fact', '第二版说明', '正文')

    _run_extraction_with(monkeypatch, [
        {'name': 'deploy', 'type': 'fact', 'description': '第一版说明',
         'body': '正文', 'supersedes': 'deploy'}], rewrite)

    entry = next(item for item in memory.list_memory_files()
                 if item['name'] == 'deploy')
    assert entry['description'] == '第二版说明', (
        f'旧候选覆盖了新内容：{entry}')
    assert entry['revision'] > stale['revision'], entry


def test_candidate_from_a_forgotten_source_is_rejected_under_a_new_name(
        home, monkeypatch):
    """换名字也绕不过：**同一段会话**里被忘掉的内容，新名字同样拒写。

    这是最真实的场景——用户在一个会话里说"忘掉那条"，而该会话自己的抽取还没跑完。
    """
    import hooks

    monkeypatch.setattr(hooks, 'current_session_id', lambda: 'sess-9')
    memory.write_memory_file('deploy', 'fact', 'x', '正文', source='sess-9')
    memory.delete_memory_file('deploy')

    _run_extraction_with(monkeypatch, [
        {'name': 'deploy-v2', 'type': 'fact', 'description': 'x', 'body': '正文'}],
        lambda: None)

    assert [item['name'] for item in memory.list_memory_files()] == []


def test_fresh_candidate_still_lands_normally(home, monkeypatch):
    """正常路径不受影响：没有并发的遗忘/改写时，抽取照常写入。"""
    _run_extraction_with(monkeypatch, [
        {'name': 'tea', 'type': 'preference', 'description': '下午想喝奶茶',
         'body': '正文'}], lambda: None)

    assert [item['name'] for item in memory.list_memory_files()] == ['tea']
