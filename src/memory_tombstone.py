"""记忆墓碑：遗忘是写入协议，不是 unlink。

为什么需要单独的墓碑
--------------------
`delete_memory_file()` 原来只做 `unlink`。于是三件事都会把删掉的记忆弄回来：

1. 正在跑的抽取/合并拿着**改之前**的记忆快照，回写时原样写回；
2. 换一个名字重新生成同一条，绕开"同名才拦"的检查；
3. 重建索引 / 清缓存之后，谁也不记得它被遗忘过。

墓碑要做的只有一件事：**记住"这个名字、这个来源谱系已经被遗忘"，直到人明确说重新记住。**
它刻意只保留阻止复活所需的最小标识——不保存被遗忘的正文，也不保存可还原的内容。

代际（generation）
------------------
每次遗忘递增一次。候选写回前比较自己算出的代际：不一致就放弃，需要时基于新快照重算。
这比"逐条比对内容"便宜，也比"只拦同名"准确——换名字但同来源的复活同样被拦。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import paths

#: 墓碑存哪。放在记忆目录旁边而不是里面：`list_memory_files()` 扫的是
#: `MEMORY_DIR/*.md`，墓碑不该成为一条"记忆"。
STORE_PATH = paths.cache_path('memories_tombstones.json')

#: 保留多少条墓碑明细。计数不封顶，明细有界——防一个长期使用的库把文件撑大。
MAX_ENTRIES = 200


class TombstoneError(RuntimeError):
    """墓碑落盘失败。调用方**不能**继续删除，也不能宣称遗忘成功。"""


def _empty() -> dict:
    return {'version': 1, 'generation': 0, 'entries': [], 'last_revive': {}}


def _read_store() -> dict:
    try:
        data = json.loads(Path(STORE_PATH).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    entries = [item for item in (data.get('entries') or [])
               if isinstance(item, dict) and item.get('name')]
    try:
        generation = int(data.get('generation') or 0)
    except (TypeError, ValueError):
        generation = 0
    last_revive = data.get('last_revive')
    return {'version': 1,
            'generation': max(0, generation),
            'entries': entries[-MAX_ENTRIES:],
            'last_revive': last_revive if isinstance(last_revive, dict) else {}}


def _write_store(data: dict) -> None:
    """原子写。失败抛 `TombstoneError`——外面据此中止遗忘。"""
    path = Path(STORE_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding='utf-8')
        tmp.replace(path)
    except OSError as exc:
        raise TombstoneError(f'cannot persist memory tombstone: {exc}') from exc


def load() -> dict:
    return _read_store()


def generation() -> int:
    return _read_store()['generation']


def block(name: str, *, sources=(), reason: str = '') -> dict:
    """记一条墓碑并递增代际，返回新的记录。

    **只存最小标识**：名字、来源引用、代际、时间、原因。不存正文——被遗忘的内容
    不该在另一个文件里继续躺着。
    """
    data = _read_store()
    clean = ' '.join(str(name or '').split())
    if not clean:
        raise TombstoneError('memory name is required')
    data['generation'] = int(data.get('generation') or 0) + 1
    data['entries'] = (data['entries'] + [{
        'name': clean,
        'sources': [str(item) for item in (sources or []) if str(item).strip()][:8],
        'generation': data['generation'],
        'ts': time.time(),
        'reason': ' '.join(str(reason or '').split())[:200],
    }])[-MAX_ENTRIES:]
    _write_store(data)
    return data


def revive(name: str, *, reason: str = '') -> dict:
    """显式解除：只有人明确要求"重新记住"时才调用（§6.2 第 4 条）。

    记下新依据（``reason``），这样将来问"这条为什么又出现了"时能回答。
    """
    data = _read_store()
    clean = ' '.join(str(name or '').split())
    data['entries'] = [item for item in data['entries']
                       if item.get('name') != clean]
    data['generation'] = int(data.get('generation') or 0) + 1
    data['last_revive'] = {'name': clean, 'reason': ' '.join(str(reason or '').split())[:200],
                           'ts': time.time()}
    _write_store(data)
    return data


def may_revive(name: str, source: str = '') -> bool:
    """这个名字/来源现在还能不能被写入或召回。

    判据有两条，任何一条命中就拒绝：

    - 同名被遗忘过；
    - 同一来源（``source_session``）下的内容被遗忘过——换名字不能绕过。
      来源为空时不按来源拦（旧数据没有来源，无从判断），只按名字。
    """
    data = _read_store()
    clean = ' '.join(str(name or '').split())
    source = str(source or '').strip()
    for item in data['entries']:
        if item.get('name') == clean:
            return False
        if source and source in (item.get('sources') or []):
            return False
    return True


def blocked_names() -> set[str]:
    """全部被遗忘的名字（给 `list_memory_files()` 做一次集合查询，避免逐条读文件）。"""
    return {str(item.get('name') or '') for item in _read_store()['entries']}


def is_blocked_filename(filename: str) -> bool:
    """按文件名（``name.md``）判断——`list_memory_files()` 用的是这个形状。"""
    stem = str(filename or '').rsplit('.md', 1)[0]
    return not may_revive(stem)
