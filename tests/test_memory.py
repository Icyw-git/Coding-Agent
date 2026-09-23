"""端到端测试记忆系统：提炼 → 落盘 → 召回 → 合并，覆盖 4 种记忆类型。

- test_memory_pipeline_all_types: LLM 提炼出 user/feedback/project/reference 四种记忆，
  验证落盘 frontmatter、索引重建、load_memories 召回、consolidate 合并。
- test_agent_loop_memory_injection: 走真实 agent_loop，验证记忆注入请求副本
  （不污染 messages）和会话结束时的提炼调用。

运行: python test_memory.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
import test_context_manage as tcm


def make_response(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_memory_pipeline_all_types():
    """提炼 4 种类型 → 落盘 → 按索引召回 → 合并去重。"""
    orig_dir, orig_index = harness.MEMORY_DIR, harness.MEMORY_INDEX
    orig_create = harness.client.chat.completions.create
    tmp = Path(tempfile.mkdtemp())
    harness.MEMORY_DIR = tmp
    harness.MEMORY_INDEX = tmp / 'MEMORY.md'
    try:
        # --- 1. extract_memories：LLM 返回 4 种类型各一条 ---
        extract_json = json.dumps([
            {"name": "prefer-chinese", "type": "user", "description": "user prefers Chinese replies",
             "body": "Always reply in Chinese."},
            {"name": "avoid-force-push", "type": "feedback", "description": "no force push to main",
             "body": "Never force-push to main/master."},
            {"name": "harness-is-core", "type": "project", "description": "core file of the project",
             "body": "harness.py is the core agent loop module."},
            {"name": "ref-openai-docs", "type": "reference", "description": "openai docs pointer",
             "body": "See https://platform.openai.com/docs for API reference."},
        ])
        harness.client.chat.completions.create = lambda **kw: make_response(extract_json)
        harness.extract_memories([{'role': 'user', 'content': '请用中文回复'},
                                  {'role': 'assistant', 'content': '好的，我会用中文。'}])

        files = harness.list_memory_files()
        types = sorted(f['type'] for f in files)
        assert types == ['feedback', 'project', 'reference', 'user'], f"记忆类型不完整: {types}"
        assert all(t in harness.MEMORY_TYPES for t in types), "类型必须在 MEMORY_TYPES 内"
        by_name = {f['name']: f for f in files}
        assert by_name['prefer-chinese']['body'].strip() == 'Always reply in Chinese.'
        assert by_name['harness-is-core']['type'] == 'project'
        assert 'prefer-chinese' in harness.read_memory_index(), "索引未重建"

        # --- 2. load_memories：LLM 挑相关索引 → 只召回选中的记忆 ---
        # 文件按文件名排序：avoid-force-push(0) harness-is-core(1) prefer-chinese(2) ref-openai-docs(3)
        harness.client.chat.completions.create = lambda **kw: make_response('[1, 2]')
        block = harness.load_memories([{'role': 'user', 'content': 'harness.py 项目，中文回复'}])
        assert block.startswith('<relevant_memories>'), block[:50]
        assert 'harness.py is the core' in block, "project 记忆应被召回"
        assert 'Always reply in Chinese.' in block, "user 记忆应被召回"
        assert 'force-push' not in block, "未选中的记忆不应被召回"

        # --- 3. consolidate_memories：达到阈值 → LLM 合并成 2 条 ---
        orig_thr = harness.CONSOLIDATE_THRESHOLD
        harness.CONSOLIDATE_THRESHOLD = 2
        merge_json = json.dumps([
            {"name": "merged-user", "type": "user", "description": "merged prefs",
             "body": "Chinese replies; never force-push."},
            {"name": "core-facts", "type": "project", "description": "project facts",
             "body": "harness.py core; openai docs."},
        ])
        harness.client.chat.completions.create = lambda **kw: make_response(merge_json)
        harness.consolidate_memories()
        harness.CONSOLIDATE_THRESHOLD = orig_thr

        merged = {f['name']: f['type'] for f in harness.list_memory_files()}
        assert merged == {'merged-user': 'user', 'core-facts': 'project'}, f"合并结果错误: {merged}"
    finally:
        harness.MEMORY_DIR = orig_dir
        harness.MEMORY_INDEX = orig_index
        harness.client.chat.completions.create = orig_create
        shutil.rmtree(tmp, ignore_errors=True)


def test_agent_loop_memory_injection():
    """走真实 agent_loop：记忆注入请求副本、不污染 messages、结束提炼。"""
    orig_dir, orig_index = harness.MEMORY_DIR, harness.MEMORY_INDEX
    orig_create = harness.client.chat.completions.create
    tmp = Path(tempfile.mkdtemp())
    harness.MEMORY_DIR = tmp
    harness.MEMORY_INDEX = tmp / 'MEMORY.md'
    try:
        harness.write_memory_file('e2e-mem', 'user', 'desc', 'Body text.')
        # 队列顺序：① select_relevant_memories(load) ② 主 API 调用 ③ extract_memories(结束)
        fake = tcm.FakeCompletions([
            tcm.make_text_response('[0]'),
            tcm.make_text_response('task done'),
            tcm.make_text_response('[]'),
        ])
        harness.client.chat.completions.create = fake.create

        result = harness.agent_loop([{'role': 'user', 'content': 'do it'}])
        assert result == 'task done', result

        # 主调用（calls[1]）应包含注入的记忆，且与原任务在同一 user 消息
        first_user = next(m for m in fake.calls[1] if m.get('role') == 'user')
        assert '<relevant_memories>' in first_user['content'], "记忆块未注入"
        assert 'Body text.' in first_user['content'], "记忆内容未注入"
        assert 'do it' in first_user['content'], "原任务被覆盖"
        # 注入不能污染 messages：calls[1] 的 tool/assistant 结构保持合法
        tcm.assert_valid_openai_msgs(fake.calls[1], "injected")

        # 结束时的提炼调用（calls[2]）是独立的单 user 消息
        extract_prompt = json.dumps(fake.calls[2], ensure_ascii=False)
        assert 'Extract user preferences' in extract_prompt, "结束时应触发记忆提炼"
    finally:
        harness.MEMORY_DIR = orig_dir
        harness.MEMORY_INDEX = orig_index
        harness.client.chat.completions.create = orig_create
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"[ERROR] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
