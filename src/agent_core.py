"""兼容门面：Hook 注册表 + 记忆/图片 token/压缩原语的统一导入面。

实现已按领域拆到 ``memory`` / ``tokens`` / ``compaction``，harness 与测试继续
从这里导入，避免大范围改调用点。本模块仍不持有可变状态：子模块通过
``llm_route.harness()`` 在运行时读取 harness 的当前全局状态，测试
``harness.MEMORY_DIR = tmp`` / patch ``harness.client`` 依旧直接生效。
"""
from compaction import (
    _bound_tool_output,
    _is_tool_result_message,
    _message_has_tool_use,
    _summary_input,
    _tail_within_budget,
    collect_tool_results,
    compact_history,
    content_text,
    micro_compact,
    persist_large_output,
    reactive_compact,
    snip_compact,
    summarize_history,
    tool_result_budget,
    write_transcript,
)
from memory import (
    _parse_frontmatter,
    _rebuild_index,
    _recent_user_text,
    consolidate_memories,
    extract_memories,
    inject_memories,
    list_memory_files,
    load_memories,
    local_relevant_memories,
    read_memory_file,
    read_memory_index,
    select_relevant_memories,
    write_memory_file,
)
from tokens import (
    DEEPSEEK_IMAGE_TOKEN_LIMIT,
    IMAGE_TOKEN_ESTIMATE,
    estimate_image_tokens,
    estimate_size,
    expand_image_parts,
    image_attachment_message,
    image_dimensions,
)
from tokens import image_token_cost as _image_token_cost

__all__ = [
    # Hook 注册表
    'HOOKS',
    'register_hook',
    'trigger_hooks',
    # 记忆
    '_parse_frontmatter',
    '_rebuild_index',
    '_recent_user_text',
    'consolidate_memories',
    'extract_memories',
    'inject_memories',
    'list_memory_files',
    'load_memories',
    'local_relevant_memories',
    'read_memory_file',
    'read_memory_index',
    'select_relevant_memories',
    'write_memory_file',
    # Token / 多模态
    'DEEPSEEK_IMAGE_TOKEN_LIMIT',
    'IMAGE_TOKEN_ESTIMATE',
    '_image_token_cost',
    'estimate_image_tokens',
    'estimate_size',
    'expand_image_parts',
    'image_attachment_message',
    'image_dimensions',
    # 压缩
    '_bound_tool_output',
    '_is_tool_result_message',
    '_message_has_tool_use',
    '_summary_input',
    '_tail_within_budget',
    'collect_tool_results',
    'compact_history',
    'content_text',
    'micro_compact',
    'persist_large_output',
    'reactive_compact',
    'snip_compact',
    'summarize_history',
    'tool_result_budget',
    'write_transcript',
]


HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result  # 阻断工具调用
    return None


def _extract_text(text: str) -> str:
    return text.strip()
