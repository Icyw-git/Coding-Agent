"""工具系统：handler 薄包装、路径边界与 tool_registry。

schema 定义在 tool_schemas.py（顺序是契约）；各域实现见 tool_*.py。
可变全局（WORKDIR 等）由 harness 持有，通过 _h() 在运行时读取（便于测试 patch）。
"""
from pathlib import Path
from typing import Optional

from exec_policy import resolve_tool_path
from tool_clipboard import read as _clipboard_read_tool
from tool_clipboard import write as _clipboard_write_tool
from tool_delete import delete as _delete_tool
from tool_desktop import action as _desktop_action_tool
from tool_desktop import list_windows as _desktop_list_tool
from tool_desktop import snapshot as _desktop_snapshot_tool
from tool_documents import (
    ImageResult,  # noqa: F401
    read_document,
    render_document,
    view_image,
)

# harness / subagent / headless 从这里取多模态结果类型。
from tool_exec import bash, fetch_url, run_background_output, run_kill_background
from tool_files import edit_file, read_file, write_file

# hooks / 策略层从这里取补丁解析入口，保持 tools 作为统一工具面。
from tool_patch import PatchError, parse_patch, patch_paths  # noqa: F401
from tool_patch import apply_patch as _apply_patch
from tool_schemas import TOOL_SCHEMAS
from tool_screen import capture as _screen_capture_tool
from tool_screen import ocr as _screen_ocr_tool
from tool_search import _grep_with_ripgrep, _rg_binary, glob_files, grep_files, ls_dir
from tool_sessions import (
    run_find_sessions,
    run_rename_session,
    run_revoke_permissions,
)
from tool_tasks import (  # noqa: F401  注册表与 Teammate 从这里取 handler
    run_cancel_cron,
    run_claim_task,
    run_complete_task,
    run_create_task,
    run_get_task,
    run_list_crons,
    run_list_tasks,
    run_schedule_cron,
)

# 测试夹具直接清空内存缓存；harness 从 tools 取 restore_todos。
from tool_teams import (  # noqa: F401  注册表从这里取 handler
    run_check_inbox,
    run_request_plan,
    run_request_shutdown,
    run_review_plan,
    run_send_message,
    run_spawn_teammate,
)
from tool_todos import _TODOS, _TODOS_LOCK, read_todos, restore, write_todos  # noqa: F401
from worktree import create_worktree, keep_worktree, remove_worktree


def _h():
    import harness
    return harness


def _gate():
    """延迟导入：hooks 在模块级依赖 tools，反向只能按需取（与 _h() 同一手法）。"""
    import hooks
    return hooks


def _gate_file(tool: str, path: str, cwd: str | None) -> str | None:
    """文件工具的边界判定：参数形状与 safe_path 保持一致（cwd 只在显式传入时参与）。"""
    args = {'path': path}
    if cwd:
        args['cwd'] = cwd
    return _gate().enforce_headless(tool, args)


TOOLS = list(TOOL_SCHEMAS)   # MCP 运行期会 append，必须是独立可变列表


BASH_TIMEOUT = 120
BASH_BACKGROUND_TIMEOUT = 3600
BASH_MAX_TIMEOUT = 3600


def run_bash(command: str, cwd: str = None, timeout=None,
             run_in_background: bool = False) -> str:
    return bash(command, cwd=cwd, timeout=timeout,
                run_in_background=run_in_background,
                workdir=_h().get_workdir(),
                gate=_gate().enforce_headless,
                default_timeout=BASH_TIMEOUT,
                background_timeout=BASH_BACKGROUND_TIMEOUT,
                max_timeout=BASH_MAX_TIMEOUT)


def run_web_fetch(url: str, max_chars: int = 20000) -> str:
    return fetch_url(url, max_chars=max_chars,
                     gate=_gate().enforce_headless)


def safe_path(p: str, cwd: str = None) -> Path:
    """解析工具路径。

    能不能读/写由 _gate_file（无头路径）和 PreToolUse hook 判定，这里不再按
    工作区做二次拦截：读可以越界；越界写在审批通过后也必须落到卡片上显示的
    那个路径，否则按钮仍然不起作用。路径解析与 hooks._path_zone 共用同一份。
    """
    return resolve_tool_path(p, cwd=cwd, workspace=_h().get_workdir())


def _capture_checkpoint(path: Path) -> None:
    """把「写入前」的内容交给当前活跃 checkpoint 节点；无活跃 manager 时静默跳过。"""
    manager = _h().active_checkpoint_manager()
    if manager is not None:
        manager.capture_pre_image(path)


_IMAGE_MAX_BYTES = 8 * 1024 * 1024   # 实现读取这个值，测试按模块名打补丁


def _tool_results_dir():
    """缓存根目录在调用时读取：跟随 conftest 的隔离与 /cache 的统计口径。"""
    return getattr(_h(), 'TOOL_RESULTS_DIR', None)


def _screenshot_dir():
    return getattr(_h(), 'SCREENSHOT_DIR', None)


def _screen_audit(event: str, **payload) -> None:
    """截图/OCR 审计进当前会话事件流；没有会话上下文（测试/单次）就跳过。"""
    try:
        manager = _h().active_checkpoint_manager()
    except Exception:
        return
    store = getattr(manager, 'session_store', None)
    if store is None:
        return
    store.append(event, **payload)


def run_screen_capture(screen: str = 'primary', region: str = None,
                       delay_ms: float = 0):
    return _screen_capture_tool(
        screen, region, delay_ms,
        directory=_screenshot_dir(), audit=_screen_audit)


def run_screen_ocr(path: str = None, lang: str = None, region: str = None) -> str:
    return _screen_ocr_tool(
        path, lang=lang, region=region,
        directory=_screenshot_dir(), audit=_screen_audit)


def run_clipboard_read() -> str:
    return _clipboard_read_tool(audit=_screen_audit)


def run_clipboard_write(text: str) -> str:
    return _clipboard_write_tool(text, audit=_screen_audit)


def run_delete_paths(paths: list) -> str:
    return _delete_tool(paths, audit=_screen_audit)


def run_desktop_snapshot() -> str:
    return _desktop_snapshot_tool(audit=_screen_audit)


def run_desktop_windows(limit: int = 20) -> str:
    return _desktop_list_tool(limit=limit, audit=_screen_audit)


def run_desktop_action(action: str, x: int | None = None, y: int | None = None,
                       text: str | None = None, key: str | None = None,
                       amount: int | None = None, button: str | None = None,
                       count: int | None = None, expected_pid: int | None = None,
                       expected_window_id: str | None = None,
                       steps: list | None = None) -> str:
    return _desktop_action_tool(
        action, x=x, y=y, text=text, key=key, amount=amount, button=button,
        count=count, steps=steps,
        expected_pid=expected_pid, expected_window_id=expected_window_id,
        audit=_screen_audit)


def run_view_image(path: str, cwd: str = None):
    return view_image(path, cwd, gate=_gate_file, resolve=safe_path,
                      max_bytes=_IMAGE_MAX_BYTES,
                      cache_dir=_tool_results_dir())


def run_read_document(path: str, pages: str = None, sheets: str = None,
                      cwd: str = None) -> str:
    return read_document(path, cwd, gate=_gate_file, resolve=safe_path,
                         pages=pages, sheets=sheets)


def run_render_document(path: str, pages: str = None, dpi: int = 110,
                        cwd: str = None):
    return render_document(path, cwd, gate=_gate_file, resolve=safe_path,
                           pages=pages, dpi=dpi,
                           cache_dir=_tool_results_dir())


def run_read(path: str, limit: Optional[int] = None,
             offset: int = 0, cwd: str = None) -> str:
    return read_file(path, limit=limit, offset=offset, cwd=cwd,
                     resolve=safe_path, gate=_gate_file)


def run_write(path: str, content: str, cwd: str = None) -> str:
    return write_file(path, content, cwd=cwd, resolve=safe_path,
                      gate=_gate_file, capture=_capture_checkpoint)


def run_edit(path: str, old_text: str, new_text: str,
             replace_all: bool = False, cwd: str = None) -> str:
    return edit_file(path, old_text, new_text, replace_all=replace_all,
                     cwd=cwd, resolve=safe_path, gate=_gate_file,
                     capture=_capture_checkpoint)


def run_apply_patch(patch: str, cwd: str = None) -> str:
    """审批门、路径解析与 checkpoint 捕获由 tools 注入，实现见 tool_patch。"""
    return _apply_patch(
        patch, cwd,
        resolve=safe_path,
        capture=_capture_checkpoint,
        gate=lambda name, args: _gate().enforce_headless(name, args),
    )


def run_glob(pattern: str, path: str = None, cwd: str = None) -> str:
    return glob_files(pattern, path, cwd, gate=_gate_file, resolve=safe_path)


def _grep_backend(pattern, root, glob, case_insensitive, limit):
    """保持后端 5 参数签名；rg 查找函数由 tools 注入，测试可替换。"""
    return _grep_with_ripgrep(pattern, root, glob, case_insensitive, limit,
                              rg_binary=_rg_binary)


def run_grep(pattern: str, path: str = '.', glob: str = None,
             case_insensitive: bool = False, max_results: int = 100,
             cwd: str = None) -> str:
    return grep_files(pattern, path, glob, case_insensitive=case_insensitive,
                      max_results=max_results, cwd=cwd, gate=_gate_file,
                      resolve=safe_path, backend=_grep_backend)


def run_ls(path: str = '.', all: bool = False, cwd: str = None) -> str:
    return ls_dir(path, all, cwd, gate=_gate_file, resolve=safe_path)


def _todo_scope() -> str:
    """todo 按会话分区；没有会话上下文时退回进程级（测试/单次调用）。"""
    try:
        return _gate().current_session_id() or '__global__'
    except Exception:
        return '__global__'


def _todo_store():
    """当前会话的事件流；manager 由 agent_loop 持有，拿不到就返回 None。"""
    try:
        manager = _h().active_checkpoint_manager()
    except Exception:
        return None
    return getattr(manager, 'session_store', None)


def restore_todos(session_store, scope: str | None = None) -> list[dict]:
    """会话恢复后从事件流重建 todo；harness 在打开会话时调用。"""
    return restore(session_store, scope)


def run_todo_write(todos: list) -> str:
    return write_todos(todos, scope=_todo_scope(),
                       session_store=_todo_store())


def run_todo_read() -> str:
    return read_todos(scope=_todo_scope(), session_store=_todo_store())


tool_registry = {
    'bash': run_bash,
    'background_output': run_background_output,
    'kill_background': run_kill_background,
    'read': run_read,
    'write': run_write,
    'edit': run_edit,
    'view_image': run_view_image,
    'screen_capture': run_screen_capture,
    'screen_ocr': run_screen_ocr,
    'clipboard_read': run_clipboard_read,
    'clipboard_write': run_clipboard_write,
    'delete_paths': run_delete_paths,
    'desktop_snapshot': run_desktop_snapshot,
    'desktop_windows': run_desktop_windows,
    'desktop_action': run_desktop_action,
    'read_document': run_read_document,
    'render_document': run_render_document,
    'apply_patch': run_apply_patch,
    'web_fetch': run_web_fetch,
    'glob': run_glob,
    'grep': run_grep,
    'ls': run_ls,
    'todo_write': run_todo_write,
    'todo_read': run_todo_read,
    'create_task': run_create_task,
    'list_tasks': run_list_tasks,
    'get_task': run_get_task,
    'claim_task': run_claim_task,
    'complete_task': run_complete_task,
    'schedule_cron': run_schedule_cron,
    'list_crons': run_list_crons,
    'cancel_cron': run_cancel_cron,
    'find_sessions': run_find_sessions,
    'rename_session': run_rename_session,
    'revoke_permissions': run_revoke_permissions,
    'spawn_teammate': run_spawn_teammate,
    'send_message': run_send_message,
    'check_inbox': run_check_inbox,
    'request_shutdown': run_request_shutdown,
    'request_plan': run_request_plan,
    'review_plan': run_review_plan,
    'create_worktree': create_worktree,
    'remove_worktree': remove_worktree,
    'keep_worktree': keep_worktree,
}
