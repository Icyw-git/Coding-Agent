"""`move_paths` 与 `undo_moves` 的工具层：怎么说清楚。

能力在 `moves.py`（校验/执行/撤销），该不该做在 `exec_policy`。这里只负责把
结果讲成人能核对的话——尤其 **dry-run 的预览**，它是这个工具存在的主要理由。
"""
from __future__ import annotations


def _notify(audit, event: str, **payload) -> None:
    if audit is None:
        return
    try:
        audit(event, **payload)
    except Exception:
        pass


def move(moves, *, cache_dir, dry_run: bool = True, runner=None,
         audit=None) -> str:
    """移动或重命名文件/目录；``dry_run=True``（默认）只预览不动磁盘。

    默认 dry-run 是刻意的：批量移动手误的代价是文件丢失，而"先看一遍清单"的
    成本只是多一次调用。
    """
    import moves as engine

    try:
        prepared = engine.validate_moves(moves)
    except engine.MoveError as exc:
        _notify(audit, 'move_rejected', reason=str(exc)[:200])
        # 必须说清“一项都没动”：用户看到错误第一反应是“移了一半没有”？
        # 验证是整批先做的，所以答案总是“没动”，而这句话省掉一次恐慌。
        return (f'Error: {exc}\n\n'
                'Nothing was moved — the whole batch is checked before anything '
                'touches the disk. Fix that item and try again.')

    listing = engine.describe(prepared)
    if dry_run:
        _notify(audit, 'move_preview', count=len(prepared),
                moves=[{'from': str(i['from']), 'to': str(i['to'])}
                       for i in prepared][:20])
        return (
            f'Preview: {len(prepared)} item(s) would move (nothing changed yet):\n'
            f'{listing}\n\n'
            'Call move_paths again with dry_run=false to perform it. '
            'The move is reversible with undo_moves.')

    try:
        result = engine.move(prepared, cache_dir=cache_dir, runner=runner)
    except engine.MoveError as exc:
        _notify(audit, 'move_failed', count=len(prepared), reason=str(exc)[:200])
        return f'Error: {exc}'
    except Exception as exc:
        _notify(audit, 'move_failed', count=len(prepared),
                reason=f'{type(exc).__name__}: {exc}'[:200])
        return f'Error: move failed: {exc}'

    moved = result['moved']
    _notify(audit, 'moved', count=len(moved), batch=result['journal_id'],
            moves=moved[:20])
    return (
        f'Moved {len(moved)} item(s):\n{listing}\n\n'
        f'Undo with /undo-move {result["journal_id"]} (or undo_moves) if this was '
        'a mistake.')


def undo(cache_dir, *, batch_id: str = '', runner=None, audit=None) -> str:
    """撤销一批移动；默认最近一批。"""
    import moves as engine

    try:
        result = engine.undo(cache_dir, batch_id=batch_id, runner=runner)
    except engine.MoveError as exc:
        _notify(audit, 'move_undo_failed', reason=str(exc)[:200])
        return f'Error: {exc}'
    except Exception as exc:
        _notify(audit, 'move_undo_failed',
                reason=f'{type(exc).__name__}: {exc}'[:200])
        return f'Error: undo failed: {exc}'

    restored = result['restored']
    skipped = result['skipped']
    _notify(audit, 'moves_undone', count=len(restored),
            batch=result.get('batch') or '', skipped=len(skipped))
    lines = [f'Restored {len(restored)} item(s) (batch {result.get("batch")}).']
    lines.extend(f"  {item['from']} -> {item['to']}" for item in restored[:20])
    if skipped:
        lines.append(f'Skipped {len(skipped)} item(s) — not overwriting anything:')
        lines.extend(f"  {item['path']}: {item['reason']}" for item in skipped[:10])
    return '\n'.join(lines)
