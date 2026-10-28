"""工具 schema 的顺序与内容指纹：拆分前后必须逐字一致。

`TOOLS` 的顺序进入 system prompt 与 `_tools_fingerprint`，直接影响请求前缀缓存和
usage 事件的归因。schema 搬到独立模块后，这份快照是第一道回归护栏。
"""
import hashlib
import json

import tool_schemas
import tools

ORDER = [
    'bash', 'read', 'view_image', 'read_document', 'render_document',
    'write', 'edit', 'glob', 'grep', 'ls', 'todo_write', 'todo_read',
    'create_task', 'list_tasks', 'get_task', 'claim_task', 'complete_task',
    'schedule_cron', 'list_crons', 'cancel_cron',
    'find_sessions', 'rename_session', 'revoke_permissions',
    'spawn_teammate', 'send_message', 'check_inbox', 'background_output',
    'kill_background',
    'web_fetch', 'apply_patch', 'checkpoint', 'task', 'load_skill', 'compact',
    'request_shutdown', 'request_plan', 'review_plan', 'create_worktree',
    'remove_worktree', 'keep_worktree', 'screen_capture', 'screen_ocr',
    'clipboard_read', 'clipboard_write',
    'desktop_snapshot', 'desktop_inspect', 'desktop_displays', 'desktop_wait',
    'desktop_windows', 'desktop_action',
    'delete_paths', 'remind', 'move_paths', 'undo_moves',
    'remember', 'forget', 'recall',
]

SCHEMA_SHA1 = '76e47d6dd2817ab30ab0b4feed9463d92baf4923'


def test_tool_order_is_stable():
    assert [item['function']['name'] for item in tools.TOOLS] == ORDER


def test_tool_schema_content_is_stable():
    blob = json.dumps(tools.TOOLS, ensure_ascii=False, sort_keys=False)

    assert hashlib.sha1(blob.encode()).hexdigest() == SCHEMA_SHA1


def test_every_schema_has_a_handler():
    missing = [item['function']['name'] for item in tools.TOOLS
               if item['function']['name'] not in tools.tool_registry]

    # checkpoint / compact 由主循环特判，没有注册 handler。
    assert missing == ['checkpoint', 'compact']


def test_tools_is_a_mutable_copy_of_the_schema_list():
    # MCP 运行期会 append 到 TOOLS；不能污染模块级的规范列表。
    assert tools.TOOLS is not tool_schemas.TOOL_SCHEMAS
    assert tools.TOOLS == tool_schemas.TOOL_SCHEMAS
