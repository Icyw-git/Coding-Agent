"""计时定位 housekeeping 首步卡顿：saved_session_title 耗时 + 堆栈转储。"""
import faulthandler
import sys, os, json, tempfile, shutil, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
dump_path = ROOT / '.cache' / 'hk_dump.txt'
dump_file = open(dump_path, 'w', buffering=1)
faulthandler.dump_traceback_later(8, repeat=True, file=dump_file)

sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT))

import harness
import test_context_manage as tcm

tmp = Path(tempfile.mkdtemp())
orig_dir, orig_index = harness.MEMORY_DIR, harness.MEMORY_INDEX
orig_create = harness.client.chat.completions.create
harness.MEMORY_DIR = tmp
harness.MEMORY_INDEX = tmp / 'MEMORY.md'

try:
    harness.write_memory_file('e2e-mem', 'user', 'desc', 'Body text.')

    orig_saved = harness.SessionEventStore.saved_session_title
    def spy_saved(self):
        t0 = time.time()
        r = orig_saved(self)
        print(f'[timed] saved_session_title took {time.time()-t0:.1f}s -> {r!r}', flush=True)
        return r
    harness.SessionEventStore.saved_session_title = spy_saved

    orig_gen = harness.generate_session_title
    def spy_gen(messages):
        t0 = time.time()
        r = orig_gen(messages)
        print(f'[timed] generate_session_title took {time.time()-t0:.1f}s -> {str(r)[:30]!r}', flush=True)
        return r
    harness.generate_session_title = spy_gen

    orig_extract = harness.extract_memories
    def spy_extract(messages):
        t0 = time.time()
        print('[timed] extract ENTER', flush=True)
        r = orig_extract(messages)
        print(f'[timed] extract took {time.time()-t0:.1f}s', flush=True)
        return r
    harness.extract_memories = spy_extract

    fake = tcm.FakeCompletions([
        tcm.make_text_response('[0]'),
        tcm.make_text_response('task done'),
        tcm.make_text_response('[]'),
    ])
    harness.client.chat.completions.create = fake.create

    result = harness.agent_loop([{'role': 'user', 'content': 'do it'}])
    print(f'[main] result={result!r}', flush=True)
    harness.wait_for_housekeeping(90)
    print(f'[main] housekeeping done; calls={len(fake.calls)}', flush=True)
    for i, c in enumerate(fake.calls):
        print(f'  call#{i}: {json.dumps(c, ensure_ascii=False)[:80]}', flush=True)
finally:
    harness.MEMORY_DIR = orig_dir
    harness.MEMORY_INDEX = orig_index
    harness.client.chat.completions.create = orig_create
    shutil.rmtree(tmp, ignore_errors=True)
    faulthandler.cancel_dump_traceback_later()
    dump_file.close()
