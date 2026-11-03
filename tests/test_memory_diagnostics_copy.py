"""记忆抽取的诊断输出：说人话、别把模型原文倒给用户、没事就别吭声。

这 12 条 `print` 都在**每回合**的路径上（`harness.py:831` 调 `extract_memories`）。
TUI 把 stdout 收进 StringIO 所以看不见；headless 重定向到 stderr；但 `entrypoint.py`
的默认交互模式（`run_repl`）就打在用户屏幕上。

两个问题：
1. 英文——`[Memory: extract returned empty (LLM judged nothing worth remembering)]`
   在"这轮没什么可记"的**常见**情况下每回合打一次；
2. 失败时 `{text[:200]!r}` 把模型回复的 Python repr 直接印出来。
"""

import memory


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


def _patch_llm(monkeypatch, content):
    import llm_route

    monkeypatch.setattr(llm_route, 'completion',
                        lambda *a, **k: _Reply(content))


DIALOGUE = [{'role': 'user', 'content': '我今天不想写代码'},
            {'role': 'assistant', 'content': '好的'}]


def test_nothing_extracted_prints_nothing(monkeypatch, capsys):
    """「没什么可记」是常态，不该每回合占一行。"""
    _patch_llm(monkeypatch, '[]')
    memory.extract_memories(DIALOGUE)
    assert capsys.readouterr().out.strip() == ''


def test_extracted_memories_report_in_chinese(monkeypatch, capsys):
    _patch_llm(monkeypatch, '[{"name": "t1", "type": "preference",'
                            ' "description": "周三不写代码", "body": "用户说的"}]')
    memory.extract_memories(DIALOGUE)
    out = capsys.readouterr().out
    assert '[Memory:' not in out, out
    assert '1' in out and ('记忆' in out or '记住' in out), out


def test_failure_does_not_dump_the_raw_reply(monkeypatch, capsys):
    """诊断里带上模型原文的 repr 既泄露又没人看得懂。"""
    secret = 'OPENAI_KEY_SENTINEL_xyz'
    _patch_llm(monkeypatch, f'sorry, I cannot help with {secret}')
    memory.extract_memories(DIALOGUE)
    out = capsys.readouterr().out
    assert '[Memory:' not in out, out
    assert secret not in out, out
