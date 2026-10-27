"""`delete_paths` 工具层：可撤销删除的格式化与审计。

只负责「怎么说清楚」，删除动作本身在 `trash.py`（能力层），该不该删在
`exec_policy`（策略层）。
"""
from __future__ import annotations


def _notify(audit, event: str, **payload) -> None:
    if audit is None:
        return
    try:
        audit(event, **payload)
    except Exception:
        pass


def delete(paths, *, runner=None, audit=None) -> str:
    """把一个或多个路径挪进回收站。

    ``runner`` 仅供测试注入；生产走真实回收站。
    """
    import trash

    if isinstance(paths, str):
        paths = [paths]
    try:
        items = list(paths)
    except TypeError:
        return 'Error: paths must be a list of paths'
    if not items:
        return 'Error: paths must not be empty'

    try:
        result = (runner(items) if runner is not None else trash.trash(items))
    except trash.TrashError as exc:
        _notify(audit, 'delete_failed', count=len(items), reason=str(exc)[:200])
        return f'Error: {exc}'
    except Exception as exc:
        _notify(audit, 'delete_failed', count=len(items),
                reason=f'{type(exc).__name__}: {exc}'[:200])
        return f'Error: delete failed: {exc}'

    method = result[0].method if result else 'unknown'
    _notify(audit, 'delete_trashed', count=len(result), method=method,
            paths=[str(item.path) for item in result][:20])
    lines = [f'Moved {len(result)} item(s) to the '
             f'{"Recycle Bin" if method == "recycle-bin" else "system trash"} '
             '(restorable):']
    lines.extend(f'  {item.path}' for item in result)
    lines.append('Tell the user they can restore these from the recycle bin if '
                 'this was a mistake.')
    return chr(10).join(lines)
