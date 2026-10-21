"""One-shot headless execution with an optional JSONL event stream.

The transport contract is intentionally narrow: stdout carries only the final
answer (plain mode) or JSON Lines (``--json``); every diagnostic, hook log and
tool banner is redirected to stderr. No cron/queue threads or post-turn
housekeeping are started, so the process exits as soon as the turn finishes.

JSONL events emitted by this module (all objects have ``type``):
    session         session_id / workspace / resumed
    turn.started
    tool.started    name / call_id / arguments
    tool.completed  name / call_id / status / output / images
    delta           text  (assistant streaming chunks)
    message         content  (the final assistant answer)
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
    """A usage/config problem that should exit with a specific status."""

    def __init__(self, message: str, exit_code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class JsonlEmitter:
    """Thread-safe JSONL writer; tool batches may emit from worker threads."""

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
        line = json.dumps(event, ensure_ascii=False, separators=(',', ':'),
                          default=str)
        with self._lock:
            self._stream.write(line + '\n')
            self._stream.flush()


def resolve_prompt(prompt: str | None, stdin=None) -> str:
    """Take the prompt from argv, ``-``, or piped stdin; never block on a TTY."""
    source = stdin if stdin is not None else sys.stdin
    if prompt == '-':
        prompt = source.read() if source is not None else ''
    elif prompt is None:
        if source is None or getattr(source, 'isatty', lambda: False)():
            raise HeadlessError(
                'a prompt is required: pass it as an argument, use -p, '
                'or pipe it on stdin')
        prompt = source.read()
    text = str(prompt or '').strip()
    if not text:
        raise HeadlessError('prompt is required and must not be empty')
    return text


def _prepare_session(runtime, session_id: str | None,
                     workspace: str | None) -> tuple[str, bool]:
    """Resume when the session exists, otherwise create it with that ID."""
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
    """Load explicit --image files through the same gate as view_image.

    Only cached references enter the session; expand_image_parts later turns
    them into data URIs for the provider. A denied/invalid image aborts before
    the turn so the model cannot silently answer without the requested input.
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
                raise HeadlessError(
                    f'cannot attach image {raw}: outside the workspace {workspace}')
            result = tools.run_view_image(str(candidate))
            if isinstance(result, str):
                raise HeadlessError(f'cannot attach image {raw}: {result}')
            attachments.extend(result.images)
    return attachments


def _turn_metadata(store, prior_seq: int) -> tuple[dict, list[dict]]:
    """Aggregate usage/files_changed events written by this turn."""
    usage = {'prompt': 0, 'completion': 0, 'cached': 0, 'total': 0,
             'calls': 0, 'cached_seen': False}
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
    emitter.emit('session', {'session_id': session_id,
                             'workspace': str(workspace),
                             'resumed': resumed})
    emitter.emit('turn.started', {'session_id': session_id})
    store = runtime.open_session(session_id)
    prior_seq = store.last_seq
    on_delta = (lambda text: emitter.emit('delta', {'text': text})) if args.json else None
    on_event = (lambda kind, payload: emitter.emit(kind, payload)) if args.json else None
    result = runtime.send(
        session_id, prompt,
        on_delta=on_delta,
        attachments=attachments or None,
        on_event=on_event,
        housekeeping=False,
    )
    usage, files = _turn_metadata(store, prior_seq)
    if args.json:
        emitter.emit('message', {'content': result.content})
        emitter.emit('turn.completed', {
            'session_id': session_id,
            'content': result.content,
            'usage': usage,
            'files_changed': files,
        })
    else:
        stdout.write(result.content or '')
        stdout.write('\n')
        stdout.flush()
    return EXIT_OK


def run(args, *, runtime=None, stdout=None, stderr=None) -> int:
    """Execute one headless turn; returns a process exit code.

    ``args`` needs ``prompt``, ``session``, ``workspace``, ``image`` and
    ``json`` attributes (the entrypoint parser supplies them).
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
