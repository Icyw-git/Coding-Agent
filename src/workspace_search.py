"""跨已登记工作区的联合搜索。

为什么不做"全盘扫描"
--------------------
用户常问「上次那份文档放哪了」「我记得有个文件提到 X，但不确定在哪个项目」。
但把它做成全盘扫描有两个不可接受的问题：

- **性能**：一次检索可能变成几分钟的磁盘遍历，而这是个要秒回的工具。
- **隐私**：全盘意味着把用户没让 agent 看过的目录纳进来。

所以只搜**已登记的工作区**（`/workspace add`），并且带明确预算。

三条约束
--------
1. **只搜登记的目录**。没登记过的地方不碰——这也是给用户一个可控边界：
   想让它搜，就登记。
2. **总量预算**：跨工作区共享一个结果上限和文件上限。命中够了就停，
   不为了让结果"完整"而扫到底。
3. **按工作区分组返回**，每组带命中数。用户看到的是"哪个项目里有"，
   而不是一串没有归属的路径。

复用单工作区搜索的全部分区与忽略规则（秘密文件、`.git`、`.cache`、
`node_modules`、符号链接目录）。
"""
from __future__ import annotations

from pathlib import Path

#: 一次联合搜索最多碰多少个文件。超过就停——这是"回忆定位"工具，不是索引器。
MAX_FILES = 4000
#: 默认最多返回多少条命中（跨所有工作区合计）。
DEFAULT_MAX_RESULTS = 60


def targets(*, file=None, sessions_dir=None, exclude=()) -> list[Path]:
    """要搜索的工作区根目录（去重、只保留存在的目录）。"""
    import workspaces

    seen: set[str] = set()
    out: list[Path] = []
    excluded = {str(Path(item).resolve()).casefold() for item in exclude if item}
    for entry in workspaces.list_workspaces(file=file, sessions_dir=sessions_dir):
        raw = str(entry.get('path') or '').strip()
        if not raw:
            continue
        path = Path(raw)
        try:
            resolved = path.resolve()
        except OSError:
            continue
        key = str(resolved).casefold()
        if key in seen or key in excluded:
            continue
        if not resolved.is_dir():
            continue
        seen.add(key)
        out.append(resolved)
    return out


def search(pattern: str, *, roots, glob: str | None = None,
           case_insensitive: bool = False,
           max_results: int = DEFAULT_MAX_RESULTS,
           max_files: int = MAX_FILES, search_workspace=None) -> dict:
    """在每个工作区里跑同一次搜索，按工作区分组。

    ``search_workspace(root, budget)`` 由调用方注入（通常包装
    ``tool_search.grep_files``）。返回 ``{groups, truncated, scanned}``。
    """
    if search_workspace is None:
        raise ValueError('search_workspace callback is required')

    remaining = max(1, int(max_results))
    file_budget = max(1, int(max_files))
    groups = []
    scanned = 0
    for root in roots:
        if remaining <= 0 or file_budget <= 0:
            break
        result = search_workspace(root, {'max_results': remaining,
                                        'max_files': file_budget})
        hits = result.get('hits') or []
        used = int(result.get('scanned') or 0)
        scanned += used
        file_budget -= used
        if hits:
            groups.append({'root': str(root), 'hits': hits})
            remaining -= len(hits)
    truncated = remaining <= 0 or file_budget <= 0
    return {'groups': groups, 'truncated': truncated, 'scanned': scanned}


def format_groups(result: dict, *, per_group: int = 12) -> str:
    """人读输出：按工作区分组，每组带命中数。

    分组是有意义的：用户问"在哪个项目里"时，一串没有归属的路径等于没回答。
    """
    groups = result.get('groups') or []
    if not groups:
        scanned = result.get('scanned') or 0
        return (f'No matches across the registered workspaces '
                f'(scanned {scanned} file(s)).\n'
                'If the file is outside them, add the directory with '
                '/workspace add <path> first.')

    total = sum(len(group['hits']) for group in groups)
    lines = [f'{total} match(es) across {len(groups)} workspace(s):']
    for group in groups:
        lines.append(f"  {group['root']}  ({len(group['hits'])})")
        for hit in group['hits'][:per_group]:
            lines.append(f'    {hit}')
        if len(group['hits']) > per_group:
            lines.append(f"    … 还有 {len(group['hits']) - per_group} 条")
    if result.get('truncated'):
        lines.append('(stopped at the search budget; narrow the pattern or glob '
                     'to see more)')
    return '\n'.join(lines)
