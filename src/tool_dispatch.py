"""Tool invocation path: single-call execution, batches, and terminal activity lines.

The module owns no harness state: registry, hooks, UI bus, metrics and the active
ticker are read back from ``harness`` at call time so tests keep patching the
composition root.
"""

import concurrent.futures
import contextvars
import difflib
import json
import sys
import time
from contextlib import nullcontext
from pathlib import Path

import checkpoints
from agent_core import _bound_tool_output, image_attachment_message
from background_task import should_run_background, start_background_task
from exec_policy import is_readonly_command, is_secret_path
from tools import ImageResult, patch_paths, safe_path


def _h():
    """延迟取 harness：模块导入期避免循环，调用期读到最新 patch 结果。"""
    import harness
    return harness


_DIFF_TOOLS = frozenset({'write', 'edit', 'apply_patch'})
_DIFF_MAX_FILE_BYTES = 1_000_000
_DIFF_MAX_CHARS = 20_000


def _diff_targets(name, args):
    """要比较的文件（写入类工具）；路径解析失败/秘密文件直接跳过。"""
    try:
        if name in ('write', 'edit'):
            raw = str(args.get('path') or '')
            targets = [safe_path(raw, args.get('cwd'))] if raw else []
        elif name == 'apply_patch' and args.get('patch'):
            targets = [safe_path(path, args.get('cwd'))
                       for path in patch_paths(str(args['patch']))]
        else:
            return []
    except Exception:
        return []
    return [path for path in targets
            if not is_secret_path(path, checkpoints.DEFAULT_SECRETS)]


def _file_bytes(path):
    try:
        if path.is_file() and path.stat().st_size <= _DIFF_MAX_FILE_BYTES:
            return path.read_bytes()
    except OSError:
        pass
    return None


def _capture_tool_files(name, args):
    return {str(path): _file_bytes(path) for path in _diff_targets(name, args)}


def _tool_diff(before):
    chunks = []
    for raw_path, old in before.items():
        path = Path(raw_path)
        new = _file_bytes(path)
        if old == new:
            continue
        try:
            old_text = (old or b'').decode('utf-8')
            new_text = (new or b'').decode('utf-8')
        except UnicodeDecodeError:
            continue    # 二进制/编码不明：不做行 diff
        diff = list(difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(),
            fromfile=raw_path, tofile=raw_path, lineterm=''))
        if diff:
            chunks.append(chr(10).join(diff))
    return chr(10).join(chunks)[:_DIFF_MAX_CHARS]


def exec_tool_call(tool_call, registry=None, *, hooks=True, background=True,
                   on_event=None, on_activity=None):
    """执行单个 tool_call：PreToolUse 权限 hook → 后台/直接执行 → PostToolUse → 异常兜底。
    返回 (name, args, tool 消息 dict, images)。images 是 view_image 的附件引用，
    由调用方作为独立 user 消息追加（tool 角色里放图片各家支持不一致）。
    主循环与 teammate 共用同一条安全/兜底路径。
    hooks=False：teammate 线程无法交互询问，跳过 hook（由工具集内层安全检查兜底）。
    on_event：无头 JSONL 的事件出口，按 ``(类型, 载荷)`` 回调。
    on_activity：交互终端的工具活动行出口，参数为
    ``(name, args, elapsed, failed, background)``。"""
    h = _h()
    name = tool_call.function.name
    try:
        args = json.loads(tool_call.function.arguments)
        if not isinstance(args, dict):
            raise ValueError('tool arguments must be a JSON object')
    except (TypeError, ValueError) as exc:
        # 无头 JSONL 的消费方按 started/completed 配对跟踪工具调用；参数解析失败
        # 也要配对上报，否则这次调用在事件流里是静默丢失的。
        message = f'Error: invalid tool arguments: {exc}'
        if on_event:
            on_event('tool.started', {'name': name, 'call_id': tool_call.id,
                                      'arguments': {}})
            on_event('tool.completed', {'name': name, 'call_id': tool_call.id,
                                        'status': 'error', 'output': message,
                                        'images': []})
        return name, {}, {'role': 'tool', 'tool_call_id': tool_call.id,
                          'content': message}, []
    if on_event:
        on_event('tool.started', {'name': name, 'call_id': tool_call.id,
                                  'arguments': args})
    if registry is None:
        registry = h.tool_registry
    if hooks:
        blocked = h.trigger_hooks("PreToolUse", name, args)
    else:
        blocked = None
    images = []
    background_started = False
    diff_before = (_capture_tool_files(name, args)
                   if name in _DIFF_TOOLS else {})
    started = time.perf_counter()
    h.ui_emit('tool_start', name=name, args=args, call_id=tool_call.id)
    if blocked:
        output = blocked
    elif name not in registry:
        output = f"Error: unknown tool {name}"
    else: #判断是否为后台任务，若为后台任务则启动后台任务，否则直接执行工具
        try:
            if background and should_run_background(name, args):
                bg_id = start_background_task(tool_call, registry)
                background_started = True
                output = (f"[Background task {bg_id} started] "
                          f"Command: {args.get('command', '')}. "
                          f"Result will be available when complete.")
            else:
                output = registry[name](**args)
                if isinstance(output, ImageResult):
                    images = output.images
                    output = output.text
        except Exception as e:
            # 模型可能传 schema 之外的参数（如 offset），**args 展开时在函数体外抛错
            output = f"Error: {e}"
    elapsed = time.perf_counter() - started
    text_output = str(output)
    failed = text_output.lstrip().startswith(('Error', 'Permission denied'))
    h.ui_emit('tool_end', name=name, args=args, call_id=tool_call.id,
              output_preview=text_output[:2000], output_len=len(text_output),
              diff=_tool_diff(diff_before) if diff_before else '',
              ok=not (blocked or failed),
              outcome=('denied' if blocked else
                       'queued' if background_started else
                       'failed' if failed else 'completed'),
              background=background_started,
              duration_ms=round(elapsed * 1000))
    h.METRICS.record_tool()
    if on_activity is not None:
        from tool_activity import is_failure, result_hint
        on_activity(name, args, elapsed, is_failure(output),
                    background_started, result_hint(output), str(output))
    if hooks:
        h.trigger_hooks("PostToolUse", name, args, output)
    # 入口 middle-out 截断：历史里从此不存在超大工具结果（事件流存的即最终
    # 形态，前缀天然稳定；全量已落盘可恢复）。hooks 拿到的仍是全量输出。
    content = _bound_tool_output(tool_call.id, output)
    if on_event:
        text = str(content)
        status = ('blocked' if blocked else
                  'error' if text.startswith(('Error:', 'Permission denied')) else 'ok')
        on_event('tool.completed', {
            'name': name, 'call_id': tool_call.id, 'status': status,
            'output': text,
            'images': [str(image.get('name') or 'image') for image in images],
        })
    return name, args, {"role": "tool", "tool_call_id": tool_call.id,
                        "content": content}, images


# 无副作用工具白名单：模型一轮返回多个 tool_call 时这些可以并行执行（Codex 行为）。
# 可变工具（bash/write/edit/任务写入/cron 变更/子代理等）与 loop 行为默认串行；
# 唯一例外见 _run_tool_batch 的只读 bash 批（exec_policy.is_readonly_command）。
PARALLEL_SAFE_TOOLS = frozenset({
    'read', 'view_image', 'read_document', 'glob', 'grep', 'ls', 'list_tasks', 'get_task',
    'list_crons', 'check_inbox', 'load_skill', 'background_output',
})
PARALLEL_MAX_WORKERS = 4


TOOL_OUTPUTS = {}      # 折叠输出的环形缓存：编号 -> (工具名, 全文)
TOOL_OUTPUT_LIMIT = 20
_tool_output_seq = 0


def _remember_tool_output(name, output) -> int:
    """保存被折叠的输出，返回 /expand 用的编号；只保留最近 20 条。"""
    global _tool_output_seq
    _tool_output_seq += 1
    while len(TOOL_OUTPUTS) >= TOOL_OUTPUT_LIMIT:
        TOOL_OUTPUTS.pop(next(iter(TOOL_OUTPUTS)))
    TOOL_OUTPUTS[_tool_output_seq] = (name, str(output))
    return _tool_output_seq


def format_tool_output(argument: str) -> str:
    """/expand <编号>：打印被折叠的工具全文。"""
    try:
        number = int(argument.strip())
    except (TypeError, ValueError):
        return '  用法：/expand <编号>（编号见工具行末尾的折叠提示）'
    entry = TOOL_OUTPUTS.get(number)
    if entry is None:
        return f'  没有编号为 {number} 的工具输出（只保留最近 {TOOL_OUTPUT_LIMIT} 条）。'
    return entry[1]


def _print_tool_activity(name, args, elapsed, failed, background=False,
                         hint='', output=''):
    """交互终端的工具活动行；长输出折叠为头尾 + /expand 提示。"""
    if not sys.stdout.isatty():
        return
    h = _h()
    if getattr(h.permission_gate, 'use_ui', False):
        # Textual 占用终端：工具块由事件流渲染，行式输出只会刷坏 TUI。
        return
    from tool_activity import format_line, preview_lines
    line = format_line(name, args, elapsed, failed=failed,
                       background=background, hint=hint)
    lines, hidden = preview_lines(output)
    if hidden:
        number = _remember_tool_output(name, output)
        block = '\n'.join(
            [line]
            + [f'    │ {text}' for text in lines]
            + [f'    └ … {hidden} 行已折叠 · /expand {number}'])
    else:
        block = line
    if h.ACTIVE_TICKER is not None:
        h.ACTIVE_TICKER.emit_line(block)
        return
    try:
        print(block, flush=True)
    except UnicodeEncodeError:
        # 控制台编码（如 GBK）放不下 '•'/'→'：替换后重试，别让展示问题打断回合。
        encoding = getattr(sys.stdout, 'encoding', 'utf-8') or 'utf-8'
        print(block.encode(encoding, 'replace').decode(encoding, 'replace'),
              flush=True)


def _batch_bash_commands(tool_calls):
    """整批 bash 的命令文本；混入其他工具或 JSON 解析失败返回 None（串行兜底）。"""
    commands = []
    for tool_call in tool_calls:
        if tool_call.function.name != 'bash':
            return None
        try:
            args = json.loads(tool_call.function.arguments or '{}')
        except (ValueError, TypeError):
            return None
        if not isinstance(args, dict):
            return None
        commands.append(str(args.get('command', '') or ''))
    return commands


def _mcp_telemetry_scope(session_store):
    """MCP 调用遥测只在桥已加载时开启，避免为未使用 MCP 的进程导入 SDK。"""
    bridge = sys.modules.get('mcp_bridge')
    if bridge is None:
        return nullcontext()
    return bridge.telemetry_sink(
        lambda event: session_store.append('mcp_call', **event))


def _run_tool_batch(tool_calls, session_store, tool_messages, on_event=None):
    """执行工具批次，并记录不含工具内容的批次与单工具耗时。"""
    profile_started = time.perf_counter()
    tool_timings = {}
    parallel = (len(tool_calls) > 1
                and all(tc.function.name in PARALLEL_SAFE_TOOLS for tc in tool_calls))
    if not parallel and len(tool_calls) > 1:
        commands = _batch_bash_commands(tool_calls)
        parallel = (commands is not None
                    and all(is_readonly_command(command) for command in commands))

    def record_profile(status, error_type=''):
        session_store.append('tool_profile', duration_ms=round(
            (time.perf_counter() - profile_started) * 1000, 3),
            parallel=parallel, status=status, error_type=error_type,
            tool_count=len(tool_calls),
            tools=[{'name': tc.function.name,
                    'duration_ms': tool_timings.get(tc.id)} for tc in tool_calls])

    try:
        if parallel:
            activities = {}
            for tool_call in tool_calls:
                session_store.append_tool_started(tool_call)   # 原序登记全部 started

            def run_parallel(tc):
                started = time.perf_counter()
                try:
                    with _mcp_telemetry_scope(session_store):
                        return contextvars.copy_context().run(
                            _h().exec_tool_call, tc, on_event=on_event,
                            on_activity=lambda *activity: activities.__setitem__(tc.id, activity))
                finally:
                    tool_timings[tc.id] = round(
                        (time.perf_counter() - started) * 1000, 3)

            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(PARALLEL_MAX_WORKERS, len(tool_calls))) as pool:
                executed = list(pool.map(run_parallel, tool_calls))
            for tool_call in tool_calls:   # 并行完成顺序不定，按原序补打活动行
                activity = activities.get(tool_call.id)
                if activity is not None:
                    _print_tool_activity(*activity)
        else:
            executed = []
            for tool_call in tool_calls:
                session_store.append_tool_started(tool_call)
                started = time.perf_counter()
                try:
                    with _mcp_telemetry_scope(session_store):
                        executed.append(_h().exec_tool_call(
                            tool_call, on_event=on_event,
                            on_activity=_print_tool_activity))
                finally:
                    tool_timings[tool_call.id] = round(
                        (time.perf_counter() - started) * 1000, 3)
    except BaseException as exc:
        record_profile('error', type(exc).__name__)
        raise
    for _, _, tool_message, _images in executed:   # 原序写回结果
        tool_messages.append(tool_message)
        session_store.append_message(tool_message)
    # 图片附件排在整批 tool 结果之后：并行 tool_calls 要求工具结果连续，
    # 中间插 user 消息会被部分 API 判成孤儿。附件本身也是 user 消息。
    for _, _, _tool_message, images in executed:
        if images:
            attachment = image_attachment_message(images)
            tool_messages.append(attachment)
            session_store.append_message(attachment)
    record_profile('ok')
