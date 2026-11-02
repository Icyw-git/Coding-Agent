"""工具 schema 的顺序与内容指纹：拆分前后必须逐字一致。

`TOOLS` 的顺序进入 system prompt 与 `_tools_fingerprint`，直接影响请求前缀缓存和
usage 事件的归因。schema 搬到独立模块后，这份快照是第一道回归护栏。

注意 `tools.TOOLS` 是**按能力开关裁剪后**的表（桌面控制默认关，那 6 个工具的 schema
不发），所以顺序/指纹这两条断言针对**规范表** `TOOL_SCHEMAS`：它们要守的是"schema
有没有被误改"，不是"当前环境发了哪些"。发不发是 `tests/test_tool_surface.py` 的事。
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

SCHEMA_SHA1 = 'debcfb293546133f992cb151001afa8d261f8942'


def test_tool_spec_order_is_stable():
    assert [item['function']['name'] for item in tool_schemas.TOOL_SCHEMAS] == ORDER


def test_runtime_tool_order_follows_the_canonical_order():
    """运行时表是规范表的**保序子集**：裁剪只删，不重排。"""
    names = [item['function']['name'] for item in tools.TOOLS]
    assert names == [name for name in ORDER if name in set(names)]


def test_tool_schema_content_is_stable():
    blob = json.dumps(tool_schemas.TOOL_SCHEMAS, ensure_ascii=False,
                      sort_keys=False)

    assert hashlib.sha1(blob.encode()).hexdigest() == SCHEMA_SHA1


def test_every_schema_has_a_handler():
    missing = [item['function']['name'] for item in tools.TOOLS
               if item['function']['name'] not in tools.tool_registry]

    # checkpoint / compact 由主循环特判，没有注册 handler。
    assert missing == ['checkpoint', 'compact']


def test_tools_is_a_mutable_copy_of_the_schema_list():
    # MCP 运行期会 append 到 TOOLS；不能污染模块级的规范列表。
    assert tools.TOOLS is not tool_schemas.TOOL_SCHEMAS
    # 是子集（按能力开关裁剪），不是同一份
    assert all(item in tool_schemas.TOOL_SCHEMAS for item in tools.TOOLS)
    tools.TOOLS.append({'function': {'name': 'probe_only'}})
    try:
        assert all(item['function']['name'] != 'probe_only'
                   for item in tool_schemas.TOOL_SCHEMAS)
    finally:
        tools.TOOLS.pop()


def test_no_schema_restates_the_feature_switch():
    """开关名不该写进工具描述。

    "Requires HARNESS_DESKTOP_CONTROL=1 and explicit approval" 描述的是本项目的配置项，
    不是工具语义：要不要审批由策略层决定、调用时 prompt 会说。5 个 desktop 工具各写
    一遍，等于每次请求为同一句话付 5 次钱（实测省 322 字符 ≈ 80 tokens）。

    这条守的是"别再把安装细节抄回 schema 里"。
    """
    import json

    blob = json.dumps(tool_schemas.TOOL_SCHEMAS, ensure_ascii=False)
    for env_name in ('HARNESS_DESKTOP_CONTROL', 'HARNESS_SCREEN_TOOLS',
                     'HARNESS_CLIPBOARD_TOOLS'):
        assert env_name not in blob, f'{env_name} 又出现在工具描述里了'
