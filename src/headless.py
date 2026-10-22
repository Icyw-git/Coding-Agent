"""一次性无头执行，可选 JSONL 事件流。

传输契约刻意收窄：stdout 只放最终答案（plain 模式）或 JSON Lines（``--json``），
诊断、hook 日志和工具横幅全部改道 stderr。这里不启动 cron/队列线程，也不做
回合后 housekeeping，所以回合结束进程即可退出。

本模块发出的 JSONL 事件（每个对象都有 ``type``）：
    session         session_id / workspace / resumed
    turn.started
    tool.started    name / call_id / arguments
    tool.completed  name / call_id / status / output / images
    delta           text（assistant 流式片段）
    message         content（最终 assistant 回答）
    turn.completed  session_id / content / usage / files_changed
    error           code / message
"""

from __future__ import annotations

import contextlib
import json
import sys
import threading
from pathlib import Path

import harness
import tools
from workspace_context import use_workspace

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPT = 130


class HeadlessError(Exception):
    """用法/配置错误，按指定的退出码结束进程。"""

    def __init__(self, message: str, exit_code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class JsonlEmitter:
    """线程安全的 JSONL 写入器；工具批次可能从工作线程发事件。"""

    def __init__(self, stream, enabled: bool) -> None:
        self._stream = stream
        self.enabled = enabled
        self._lock = threading.Lock()

    def emit(self, event_type: str, payload: dict | None = None) -> None:
        if not self.enabled:
            return
        event = {'type': event_type}
        if payload:
            event.update(payload)
        line = json.dumps(event, ensure_ascii=False, separators=(',', ':'), default=str)
        with self._lock:
            self._stream.write(line + '\n')
            self._stream.flush()


def resolve_prompt(prompt: str | None, stdin=None) -> str:
    """从 argv、``-`` 或管道 stdin 取提示词；TTY 上绝不阻塞等待。"""
    source = stdin if stdin is not None else sys.stdin
    if prompt == '-':
        prompt = source.read() if source is not None else ''
    elif prompt is None:
        if source is None or getattr(source, 'isatty', lambda: False)():
            raise HeadlessError('a prompt is required: pass it as an argument, use -p, or pipe it on stdin')
        prompt = source.read()
    text = str(prompt or '').strip()
    if not text:
        raise HeadlessError('prompt is required and must not be empty')
    return text


def _prepare_session(runtime, session_id: str | None, workspace: str | None) -> tuple[str, bool]:
    """会话已存在则恢复，否则用该 ID 新建。"""
    workspace_path = None
    if workspace is not None:
        candidate = Path(workspace).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        workspace_path = str(candidate.resolve())
    if session_id is None:
        try:
            return runtime.create_session(workspace=workspace_path), False
        except ValueError as exc:
            raise HeadlessError(str(exc)) from exc
    try:
        runtime.open_session(session_id)
    except FileNotFoundError:
        try:
            return runtime.create_session(session_id, workspace=workspace_path), False
        except ValueError as exc:
            raise HeadlessError(str(exc)) from exc
    if workspace_path is not None:
        try:
            runtime.set_workspace(session_id, workspace_path)
        except ValueError as exc:
            raise HeadlessError(str(exc)) from exc
    return session_id, True


def _image_attachments(paths, workspace: Path) -> list[dict]:
    """显式 --image 文件走与 view_image 相同的闸门。

    只把缓存引用写进会话，稍后由 expand_image_parts 转成 provider 的 data URI。
    被拒或无效的图片在回合开始前就失败，避免模型悄悄跳过用户要求的输入。
    """
    attachments: list[dict] = []
    if not paths:
        return attachments
    workspace = workspace.resolve()
    with use_workspace(workspace):
        for raw in paths:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = workspace / candidate
            candidate = candidate.resolve()
            # 显式图片也走工作区边界：无头进程没有交互审批可等，越界请求
            # 若交给策略层排队只会挂到超时，这里直接快速失败。
            if not candidate.is_relative_to(workspace):
                raise HeadlessError(f'cannot attach image {raw}: outside the workspace {workspace}')
            result = tools.run_view_image(str(candidate))
            if isinstance(result, str):
                raise HeadlessError(f'cannot attach image {raw}: {result}')
            attachments.extend(result.images)
    return attachments


def _turn_metadata(store, prior_seq: int) -> tuple[dict, list[dict]]:
    """汇总本回合写入的 usage / files_changed 事件。"""
    usage = {'prompt': 0, 'completion': 0, 'cached': 0, 'total': 0, 'calls': 0, 'cached_seen': False}
    files: list[dict] = []
    model = ''
    for event in store.read_events():
        try:
            seq = int(event.get('seq', 0) or 0)
        except (TypeError, ValueError):
            continue
        if seq <= prior_seq:
            continue
        kind = event.get('type')
        if kind == 'usage':
            for key in ('prompt', 'completion', 'cached', 'total'):
                usage[key] += int(event.get(key, 0) or 0)
            usage['calls'] += 1
            usage['cached_seen'] = usage['cached_seen'] or bool(event.get('cached_seen'))
            model = str(event.get('model') or model)
        elif kind == 'files_changed':
            files = list(event.get('files') or [])
    if model:
        usage['model'] = model
    return usage, files


def _run(args, runtime, stdout, emitter: JsonlEmitter) -> int:
    prompt = resolve_prompt(args.prompt, stdin=sys.stdin)
    session_id, resumed = _prepare_session(runtime, args.session, args.workspace)
    workspace = Path(runtime.get_workspace(session_id))
    attachments = _image_attachments(args.image or (), workspace)
    emitter.emit('session', {'session_id': session_id, 'workspace': str(workspace), 'resumed': resumed})
    emitter.emit('turn.started', {'session_id': session_id})
    store = runtime.open_session(session_id)
    prior_seq = store.last_seq
    on_delta = (lambda text: emitter.emit('delta', {'text': text})) if args.json else None
    on_event = (lambda kind, payload: emitter.emit(kind, payload)) if args.json else None
    result = runtime.send(
        session_id,
        prompt,
        on_delta=on_delta,
        attachments=attachments or None,
        on_event=on_event,
        housekeeping=False,
    )
    usage, files = _turn_metadata(store, prior_seq)
    if args.json:
        emitter.emit('message', {'content': result.content})
        emitter.emit(
            'turn.completed',
            {
                'session_id': session_id,
                'content': result.content,
                'usage': usage,
                'files_changed': files,
            },
        )
    else:
        stdout.write(result.content or '')
        stdout.write('\n')
        stdout.flush()
    return EXIT_OK


def run(args, *, runtime=None, stdout=None, stderr=None) -> int:
    """执行一个无头回合，返回进程退出码。

    ``args`` 需要 ``prompt``、``session``、``workspace``、``image`` 与 ``json``
    属性（由入口解析器提供）。
    """
    real_stdout = stdout if stdout is not None else sys.stdout
    error_stream = stderr if stderr is not None else sys.stderr
    emitter = JsonlEmitter(real_stdout, bool(args.json))
    owns_runtime = runtime is None
    mcp_ready = False
    with contextlib.redirect_stdout(error_stream):
        try:
            if owns_runtime:
                try:
                    harness.validate_runtime_config()
                except RuntimeError as exc:
                    raise HeadlessError(f'[config] {exc}') from exc
                mcp_ready = True
                harness.initialize_mcp_tools()
                runtime = harness.create_runtime(initialize_tools=False)
            return _run(args, runtime, real_stdout, emitter)
        except KeyboardInterrupt:
            print('[headless] interrupted', file=error_stream)
            emitter.emit('error', {'code': EXIT_INTERRUPT, 'message': 'interrupted'})
            return EXIT_INTERRUPT
        except HeadlessError as exc:
            print(f'[headless] {exc}', file=error_stream)
            emitter.emit('error', {'code': exc.exit_code, 'message': str(exc)})
            return exc.exit_code
        except Exception as exc:
            print(f'[headless] {exc}', file=error_stream)
            emitter.emit('error', {'code': EXIT_ERROR, 'message': str(exc)})
            return EXIT_ERROR
        finally:
            if mcp_ready:
                with contextlib.suppress(Exception):
                    harness.shutdown_mcp()
