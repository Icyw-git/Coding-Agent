"""工具系统：TOOLS schema + 全部 run_* handler + tool_registry。
可变全局（WORKDIR 等）由 harness 持有，通过 _h() 在运行时读取（便于测试 patch）。
"""
import ast
import json
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import List, Optional

import sandbox
import web_access
from agent_teams import BUS, spawn_teammate_thread
from cron_scheduler import cancel_job, cron_lock, schedule_job, scheduled_jobs
from exec_policy import resolve_tool_path
from process_utils import close_process, kill_process_tree
from task_system import claim_task, complete_task, create_task, get_task, list_tasks
from team_protocols import ProtocolState, consume_lead_inbox, new_request_id, pending_requests

# harness / subagent / headless 从这里取多模态结果类型。
from tool_documents import (
    ImageResult,  # noqa: F401
    cache_image,
    read_document,
    render_document,
    view_image,
)
from tool_files import edit_file, read_file, write_file

# hooks / 策略层从这里取补丁解析入口，保持 tools 作为统一工具面。
from tool_patch import PatchError, parse_patch, patch_paths  # noqa: F401
from tool_patch import apply_patch as _apply_patch
from tool_search import _grep_with_ripgrep, _rg_binary, glob_files, grep_files, ls_dir
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


TOOLS=[{
    "type":"function",
    "function":{
        "name":"bash",
        "description":"Execute shell commands. Slow commands (install/build/test) run in the background "
                      "by default; use background_output to read their output and kill_background to stop them.",
        "parameters":{
            "type":"object",
            "properties":{
                "command":{
                    "type":"string",
                    "description":"The shell command to execute, e.g. 'ls'"
                },
                "cwd":{
                    "type":"string",
                    "description":"Optional working directory (relative to the workspace or absolute)"
                },
                "timeout":{
                    "type":"number",
                    "description":"Optional timeout in seconds (max 3600; background default 3600, foreground 120)"
                },
                "run_in_background":{
                    "type":"boolean",
                    "description":"Force background (true) or foreground (false) execution, "
                                  "overriding the slow-command heuristic"
                }
            },
            "required":[
                "command"
            ]

        }
    }

},{
    "type":"function",
    "function":{
        "name":"read",
        "description":"Read a file. Absolute paths and paths outside the workspace are allowed; protected secret "
                      "files are denied.",
        "parameters":{
            "type":"object",
            "properties":{
                "path":{
                    "type":"string",
                    "description":"File path: relative to the workspace (or cwd), or absolute"
                },
                "limit":{
                    "type":"integer",
                    "description":"Optional maximum number of lines to read"
                },
                "offset":{
                    "type":"integer",
                    "description":"Optional 0-based line offset to start reading from (default 0)"
                }
            },
            "required":[
                "path"
            ]

        }
    }

},{
    "type":"function",
    "function":{
        "name":"view_image",
        "description":"Read a local image (PNG/JPEG/GIF/WebP/BMP) and attach it to the conversation "
                      "for visual inspection. Requires a vision-capable model.",
        "parameters":{
            "type":"object",
            "properties":{
                "path":{
                    "type":"string",
                    "description":"Image path: relative to the workspace (or cwd), or absolute"
                },
                "cwd":{
                    "type":"string",
                    "description":"Optional directory to resolve a relative path against"
                }
            },
            "required":[
                "path"
            ]

        }
    }

},{
    "type":"function",
    "function":{
        "name":"read_document",
        "description":"Extract text from a local PDF, DOCX, PPTX, or XLSX file. PDF/PPTX accept a "
                      "1-based pages range such as '1-10,15'; XLSX accepts sheet names or 1-based "
                      "indexes. Scanned PDFs without a text layer are reported and need view_image "
                      "on rendered pages (no OCR).",
        "parameters":{
            "type":"object",
            "properties":{
                "path":{
                    "type":"string",
                    "description":"Document path: relative to the workspace (or cwd), or absolute"
                },
                "pages":{
                    "type":"string",
                    "description":"Optional PDF page or PPTX slide range, 1-based (e.g. '1-10,15'). "
                                  "Default: first 20."
                },
                "sheets":{
                    "type":"string",
                    "description":"Optional XLSX sheet names or 1-based indexes, comma-separated. "
                                  "Default: first 8 sheets."
                },
                "cwd":{
                    "type":"string",
                    "description":"Optional directory to resolve a relative path against"
                }
            },
            "required":[
                "path"
            ]

        }
    }

},{
    "type":"function",
    "function":{
        "name":"render_document",
        "description":"Render selected pages of a PDF, DOCX, or PPTX to PNG images for visual "
                      "inspection of layout, charts, tables, and scanned pages. Images are "
                      "attached in the next user message. Prefer read_document for plain text. "
                      "Both need optional extras: PDF needs pypdfium2, DOCX/PPTX conversion "
                      "needs LibreOffice; read_document keeps working without them.",
        "parameters":{
            "type":"object",
            "properties":{
                "path":{
                    "type":"string",
                    "description":"Document path: relative to the workspace (or cwd), or absolute"
                },
                "pages":{
                    "type":"string",
                    "description":"Optional 1-based page/slide range such as '1-3,5'. "
                                  "Default: first 3; max 8 per call."
                },
                "dpi":{
                    "type":"integer",
                    "description":"Render resolution, 72-200 (default 110). Higher costs more tokens."
                },
                "cwd":{
                    "type":"string",
                    "description":"Optional directory to resolve a relative path against"
                }
            },
            "required":[
                "path"
            ]

        }
    }

},{
    "type":"function",
    "function":{
        "name":"write",
        "description":"Write a file. Workspace paths are written directly; paths outside the workspace require "
                      "approval.",
        "parameters":{
            "type":"object",
            "properties":{
                "path":{
                    "type":"string",
                    "description":"File path: relative to the workspace (or cwd), or absolute"
                },
                "content":{
                    "type":"string",
                    "description":"Content to write to the file"
                }
            },
            "required":[
                "path",
                "content"
            ]

        }
    }

},{
    "type":"function",
    "function":{
        "name":"edit",
        "description":"Replace a unique text fragment in a file. Paths outside the workspace require approval.",
        "parameters":{
            "type":"object",
            "properties":{
                "path":{
                    "type":"string",
                    "description":"File path: relative to the workspace (or cwd), or absolute"
                },
                "old_text":{
                    "type":"string",
                    "description":"Exact text to replace"
                },
                "new_text":{
                    "type":"string",
                    "description":"Replacement text"
                },
                "replace_all":{
                    "type":"boolean",
                    "description":"Replace every occurrence (default false; otherwise old_text must be unique)"
                }
            },
            "required":[
                "path",
                "old_text",
                "new_text"
            ]

        }
    }

},{
    "type":"function",
    "function":{
        "name":"glob",
        "description":"List files matching a glob pattern. Read-only; the root may be outside the workspace.",
        "parameters":{
            "type":"object",
            "properties":{
                "pattern":{
                    "type":"string",
                    "description":"Glob pattern relative to the root, e.g. '*.py' or 'src/**/*.ts'"
                },
                "path":{
                    "type":"string",
                    "description":"Root directory (or file). Relative to the workspace/cwd, or absolute; defaults "
                                  "to the workspace"
                }
            },
            "required":[
                "pattern"
            ]

        }
    }

},{
    "type":"function",
    "function":{
        "name":"grep",
        "description":"Search file contents with a regular expression. Read-only; protected secret files are skipped.",
        "parameters":{
            "type":"object",
            "properties":{
                "pattern":{
                    "type":"string",
                    "description":"Python regular expression to search for"
                },
                "path":{
                    "type":"string",
                    "description":"File or directory to search. Relative to the workspace/cwd, or absolute; "
                                  "defaults to the workspace"
                },
                "glob":{
                    "type":"string",
                    "description":"Only search files matching this glob, e.g. '*.py' or 'src/**/*.ts'"
                },
                "case_insensitive":{
                    "type":"boolean",
                    "description":"Case-insensitive matching (default false)"
                },
                "max_results":{
                    "type":"integer",
                    "description":"Maximum matching lines to return (default 100, capped at 1000)"
                }
            },
            "required":[
                "pattern"
            ]

        }
    }

},{
    "type":"function",
    "function":{
        "name":"ls",
        "description":"List a directory. Read-only; the path may be outside the workspace.",
        "parameters":{
            "type":"object",
            "properties":{
                "path":{
                    "type":"string",
                    "description":"Directory or file to list. Relative to the workspace/cwd, or absolute; defaults "
                                  "to the workspace"
                },
                "all":{
                    "type":"boolean",
                    "description":"Include hidden entries (default false)"
                }
            }

        }
    }

},{
    "type":"function",
    "function":{
        "name":"todo_write",
        "description":"Replace the current task list. Use it to track multi-step work; "
                      "keep exactly one item in_progress, and mark items completed as you finish them.",
        "parameters":{
            "type":"object",
            "properties":{
                "todos":{
                    "type":"array",
                    "description":"Full task list (replaces the previous one), each with 'content' and "
                                  "'status' (pending/in_progress/completed)",
                    "items":{
                        "type":"object",
                        "properties":{
                            "content":{
                                "type":"string",
                                "description":"Task description"
                            },
                            "status":{
                                "type":"string",
                                "enum":["pending","in_progress","completed"],
                                "description":"Task status"
                            }
                        },
                        "required":[
                            "content",
                            "status"
                        ]
                    }
                }
            },
            "required":[
                "todos"
            ]

        }
    }

},{
    "type":"function",
    "function":{
        "name":"todo_read",
        "description":"Read the current task list for this session.",
        "parameters":{
            "type":"object",
            "properties":{}
        }
    }

},{
    "type":"function",
    "function":{
        "name":"create_task",
        "description":"Create a new task in the task system (persisted to .cache/tasks).",
        "parameters":{
            "type":"object",
            "properties":{
                "subject":{
                    "type":"string",
                    "description":"One-line task subject"
                },
                "description":{
                    "type":"string",
                    "description":"Detailed task description"
                },
                "blockedBy":{
                    "type":"array",
                    "description":"Task ids that must be completed before this task can start",
                    "items":{
                        "type":"string"
                    }
                }
            },
            "required":[
                "subject"
            ]
        }
    }

},{
    "type":"function",
    "function":{
        "name":"list_tasks",
        "description":"List all tasks with id, subject, status, owner and dependencies.",
        "parameters":{
            "type":"object",
            "properties":{}
        }
    }

},{
    "type":"function",
    "function":{
        "name":"get_task",
        "description":"Get a task's full detail by id.",
        "parameters":{
            "type":"object",
            "properties":{
                "task_id":{
                    "type":"string",
                    "description":"Task id, e.g. task_1700000000_abcd"
                }
            },
            "required":[
                "task_id"
            ]
        }
    }

},{
    "type":"function",
    "function":{
        "name":"claim_task",
        "description":"Claim a pending task (set to in_progress). Fails if its dependencies are not completed.",
        "parameters":{
            "type":"object",
            "properties":{
                "task_id":{
                    "type":"string",
                    "description":"Task id to claim"
                }
            },
            "required":[
                "task_id"
            ]
        }
    }

},{
    "type":"function",
    "function":{
        "name":"complete_task",
        "description":"Mark a task as completed and report newly unblocked pending tasks.",
        "parameters":{
            "type":"object",
            "properties":{
                "task_id":{
                    "type":"string",
                    "description":"Task id to complete"
                }
            },
            "required":[
                "task_id"
            ]
        }
    }

},{
    "type":"function",
    "function":{
        "name":"schedule_cron",
        "description":"Schedule a cron job: at matching times the prompt is injected into the conversation as a "
                      "scheduled task.",
        "parameters":{
            "type":"object",
            "properties":{
                "cron":{
                    "type":"string",
                    "description":"5-field cron expression, e.g. '*/5 * * * *' (minute hour day-of-month month "
                                  "day-of-week)"
                },
                "prompt":{
                    "type":"string",
                    "description":"Instruction injected when the job fires"
                },
                "recurring":{
                    "type":"boolean",
                    "description":"Whether the job repeats every matching time (default true)"
                },
                "durable":{
                    "type":"boolean",
                    "description":"Whether the job survives restarts (default true)"
                }
            },
            "required":[
                "cron",
                "prompt"
            ]
        }
    }

},{
    "type":"function",
    "function":{
        "name":"list_crons",
        "description":"List all scheduled cron jobs.",
        "parameters":{
            "type":"object",
            "properties":{}
        }
    }

},{
    "type":"function",
    "function":{
        "name":"cancel_cron",
        "description":"Cancel a scheduled cron job by id.",
        "parameters":{
            "type":"object",
            "properties":{
                "job_id":{
                    "type":"string",
                    "description":"Cron job id, e.g. cron_123456"
                }
            },
            "required":[
                "job_id"
            ]
        }
    }

},{
    "type":"function",
    "function":{
        "name":"spawn_teammate",
        "description":"Spawn a teammate agent in a background thread to work on a sub-task.",
        "parameters":{
            "type":"object",
            "properties":{
                "name":{
                    "type":"string",
                    "description":"Unique teammate name"
                },
                "role":{
                    "type":"string",
                    "description":"Role, e.g. 'researcher'"
                },
                "prompt":{
                    "type":"string",
                    "description":"Task instructions for the teammate"
                }
            },
            "required":[
                "name",
                "role",
                "prompt"
            ]
        }
    }

},{
    "type":"function",
    "function":{
        "name":"send_message",
        "description":"Send a message to a teammate via MessageBus.",
        "parameters":{
            "type":"object",
            "properties":{
                "to":{
                    "type":"string",
                    "description":"Recipient teammate name"
                },
                "content":{
                    "type":"string"
                }
            },
            "required":[
                "to",
                "content"
            ]
        }
    }

},{
    "type":"function",
    "function":{
        "name":"check_inbox",
        "description":"Check Lead's inbox for teammate messages.",
        "parameters":{
            "type":"object",
            "properties":{}
        }
    }

}]

TOOLS.append({
    "type":"function",
    "function":{
        "name":"background_output",
        "description":"Read a background task's output. Set wait=true to block until it finishes "
                      "(or the timeout elapses).",
        "parameters":{
            "type":"object",
            "properties":{
                "bg_id":{
                    "type":"string",
                    "description":"Background task id, e.g. 'bg_0001'"
                },
                "wait":{
                    "type":"boolean",
                    "description":"Block until the task reaches a terminal state (default false)"
                },
                "timeout":{
                    "type":"number",
                    "description":"Max seconds to wait when wait=true (default 30, max 600)"
                }
            },
            "required":[
                "bg_id"
            ]
        }
    }
})

TOOLS.append({
    "type":"function",
    "function":{
        "name":"kill_background",
        "description":"Request cancellation of a running background task and kill its process tree.",
        "parameters":{
            "type":"object",
            "properties":{
                "bg_id":{
                    "type":"string",
                    "description":"Background task id, e.g. 'bg_0001'"
                }
            },
            "required":[
                "bg_id"
            ]
        }
    }
})

BASH_TIMEOUT = 120
BASH_BACKGROUND_TIMEOUT = 3600
BASH_MAX_TIMEOUT = 3600


def _bash_timeout(timeout) -> float:
    import background_task
    if timeout is None:
        return (BASH_BACKGROUND_TIMEOUT
                if background_task.current_background_id.get()
                else BASH_TIMEOUT)
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        raise ValueError('timeout must be a number of seconds') from None
    if value <= 0 or value > BASH_MAX_TIMEOUT:
        raise ValueError(f'timeout must be between 0 and {BASH_MAX_TIMEOUT} seconds')
    return value


def run_bash(command:str, cwd: str = None, timeout=None,
             run_in_background: bool = False)->str:
    # 判定只有一处（策略层），第三份黑名单已删：teammate / 后台 worker / 任务 runner
    # 绕过 PreToolUse hook 直接走到这里，凭据缺失时按无头路径审批（远程队列，超时拒绝）。
    workspace = _h().get_workdir()
    try:
        base_path = (resolve_tool_path(cwd, workspace=workspace)
                     if cwd else Path(workspace).resolve())
    except Exception as exc:
        return f'Error: {exc}'
    if not base_path.is_dir():
        return f'Error: cwd does not exist: {base_path}'
    base = str(base_path)
    denied = _gate().enforce_headless('bash', {'command': command, 'cwd': base})
    if denied:
        return denied
    try:
        limit = _bash_timeout(timeout)
    except ValueError as exc:
        return f'Error: {exc}'
    proc = None
    try:
        # A descendant can keep a PIPE open after cmd.exe exits. A temporary
        # output file lets the timeout bound execution without waiting for EOF.
        with tempfile.TemporaryFile() as output:
            # cwd 同时是 OS 沙箱的可写根：teammate 在 worktree 里执行时，
            # 可写范围就是那个 worktree，而不是主工作区。
            proc = sandbox.spawn_shell(command, cwd=base, stdout=output,
                                       workspace=base)
            import background_task
            background_task.register_process(proc)
            try:
                proc.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                kill_process_tree(proc)
                return (f'Error: Command timeout({limit:g}s); '
                        'process termination requested.')
            except BaseException:
                kill_process_tree(proc)
                raise
            output.seek(0)
            raw = output.read(200000)
        try:
            out=raw.decode("utf-8")
        except UnicodeDecodeError:
            out=raw.decode("gbk",errors="replace")
        out=out.strip()
        out=out.replace('\ufffd','?')  # PS Get-Content 误读 UTF-8 产生的替换符，GBK 控制台打印会崩
        return out[:50000] if out else "No output"
    except sandbox.SandboxUnavailable as e:
        return f'Error: {e}'
    except subprocess.TimeoutExpired:
        return 'Error: Timed out while terminating command; process state unknown.'
    except (FileNotFoundError,OSError) as e:
        return f'Error:{e}'
    finally:
        if proc is not None:
            try:
                import background_task
                background_task.unregister_process(proc)
            except Exception:
                pass
        close_process(proc)


def run_background_output(bg_id: str, wait: bool = False,
                          timeout: float = 30) -> str:
    """查看后台任务输出；wait=True 时最多阻塞 timeout 秒。"""
    import background_task
    if not isinstance(bg_id, str) or not bg_id.strip():
        return 'Error: bg_id must be a non-empty string'
    try:
        wait_seconds = float(timeout if timeout is not None else 30)
    except (TypeError, ValueError):
        return 'Error: timeout must be a number of seconds'
    if wait_seconds < 0:
        return 'Error: timeout must be non-negative'
    wait_seconds = min(wait_seconds, 600)
    task = (background_task.wait_background(bg_id, wait_seconds)
            if wait else background_task.background_snapshot(bg_id))
    if task is None:
        return f'Error: unknown background task {bg_id}'
    status = task['status']
    if status in ('running', 'queued'):
        return (f'[{bg_id}] {status}: {task.get("command", "")}\n'
                'call again with wait=true to block for completion')
    task, output = background_task.read_background_result(bg_id)
    if task is None:
        return f'Error: unknown background task {bg_id}'
    text = str(output if output is not None else task.get('result', ''))
    if len(text) > 50000:
        text = text[:50000] + '\n... (truncated)'
    return (f'[{bg_id}] {status}\nCommand: {task.get("command", "")}\n'
            f'Output:\n{text or "(no output)"}')


def run_kill_background(bg_id: str) -> str:
    import background_task
    if not isinstance(bg_id, str) or not bg_id.strip():
        return 'Error: bg_id must be a non-empty string'
    task = background_task.kill_background(bg_id)
    if task is None:
        return f'Error: unknown background task {bg_id}'
    status = task['status']
    if status in ('completed', 'failed', 'cancelled'):
        return f'[{bg_id}] already {status}'
    return f'[{bg_id}] cancel requested; use background_output to confirm'


def run_web_fetch(url: str, max_chars: int = 20000) -> str:
    # 私网/本机地址在审批之前就拒掉：不给「批准一次 localhost 探测」的机会。
    try:
        web_access.check_url(url)
    except web_access.WebAccessError as exc:
        return f'Error: {exc}'
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        return 'Error: max_chars must be an integer'
    max_chars = max(1, min(max_chars, 100000))
    denied = _gate().enforce_headless('web_fetch', {'url': url})
    if denied:
        return denied
    try:
        result = web_access.fetch(url)
    except web_access.WebAccessError as exc:
        return f'Error: {exc}'
    text = result['text']
    header = (f'URL: {result["final_url"]}\n'
              f'Content-Type: {result["content_type"]}\n\n')
    if not text:
        return header + '(empty response)'
    if len(text) > max_chars:
        omitted = len(text) - max_chars
        text = text[:max_chars] + f'\n... (truncated; {omitted} chars omitted)'
    if result['truncated']:
        text += '\n... (response exceeded 2 MiB and was truncated)'
    return header + text

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
    return getattr(_h(), 'TOOL_RESULTS_DIR', None)


def _cache_image(data: bytes, suffix: str) -> Path:
    return cache_image(data, suffix, _tool_results_dir())


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


_TODO_MAX_ITEMS = 100
_TODO_MAX_CHARS = 500
_TODOS: dict[str, list[dict]] = {}
_TODOS_LOCK = threading.Lock()


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


def _normalize_todos(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    if len(todos) > _TODO_MAX_ITEMS:
        return None, f"Error: at most {_TODO_MAX_ITEMS} todos are supported"
    normalized = []
    seen = set()
    active = []
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
        content = str(t["content"]).strip()
        if not content:
            return None, f"Error: todos[{i}] content must not be empty"
        if len(content) > _TODO_MAX_CHARS:
            return None, (f"Error: todos[{i}] content is too long "
                          f"(max {_TODO_MAX_CHARS} chars)")
        if content in seen:
            return None, f"Error: duplicate todo content: {content}"
        seen.add(content)
        if t["status"] == "in_progress":
            active.append(i)
        normalized.append({"content": content, "status": t["status"]})
    if len(active) > 1:
        return None, (f"Error: only one todo may be in_progress (got indexes "
                      f"{', '.join(str(i) for i in active)})")
    return normalized, None


def _todo_stats(todos: list[dict]) -> str:
    counts = {"completed": 0, "in_progress": 0, "pending": 0}
    for t in todos:
        counts[t["status"]] += 1
    return (f"{counts['completed']}/{len(todos)} completed · "
            f"{counts['in_progress']} in progress · {counts['pending']} pending")


def _format_todos(todos: list[dict]) -> str:
    icons = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
    return '\n'.join(f'{icons[t["status"]]} {t["content"]}' for t in todos)


def _print_todos(todos: list[dict]) -> None:
    print("\n\033[33m## Current Tasks\033[0m")
    icons = {"pending": " ", "in_progress": "\033[36m▸\033[0m",
             "completed": "\033[32m✓\033[0m"}
    for t in todos:
        print(f"  [{icons[t['status']]}] {t['content']}")
    print(f"  \033[90m{_todo_stats(todos)}\033[0m")


def restore_todos(session_store, scope: str | None = None) -> list[dict]:
    """从事件流恢复最后一次 todo_write 的状态（会话重启后仍可读）。"""
    if session_store is None:
        return []
    target = scope or session_store.session_id
    try:
        events = session_store.read_events()
    except Exception:
        return []
    last = next((event for event in reversed(events)
                 if event.get('type') == 'todos'
                 and isinstance(event.get('todos'), list)), None)
    if last is None:
        return []
    normalized, error = _normalize_todos(last['todos'])
    if error:
        return []
    with _TODOS_LOCK:
        _TODOS[target] = normalized
    return normalized


def run_todo_write(todos: list) -> str:
    normalized, error = _normalize_todos(todos)
    if error:
        return error
    scope = _todo_scope()
    with _TODOS_LOCK:
        _TODOS[scope] = normalized
    store = _todo_store()
    if store is not None:
        try:
            store.append('todos', todos=normalized)
        except Exception:
            pass   # 事件落盘失败不回滚内存里的计划
    _print_todos(normalized)
    if not normalized:
        return "Task list cleared."
    return (f"Updated {len(normalized)} tasks ({_todo_stats(normalized)}).\n"
            + _format_todos(normalized))


def run_todo_read() -> str:
    scope = _todo_scope()
    with _TODOS_LOCK:
        todos = list(_TODOS.get(scope, []))
    if not todos:
        store = _todo_store()
        if store is not None:
            todos = restore_todos(store, scope)
    if not todos:
        return "No tasks. Use todo_write to create a task list."
    return f"{_todo_stats(todos)}.\n{_format_todos(todos)}"

def run_create_task(subject: str, description: str = "",
                    blockedBy: Optional[List[str]] = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"

def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "●",
                "completed": "✓"}.get(t.status, "?")
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(f"  {icon} {t.id}: {t.subject} "
                     f"[{t.status}]{owner}{deps}")
    return "\n".join(lines)

def run_get_task(task_id: str) -> str:
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"

def run_claim_task(task_id: str) -> str:
    return claim_task(task_id, owner="agent")

def run_complete_task(task_id: str) -> str:
    return complete_task(task_id)

def run_schedule_cron(cron: str, prompt: str,
                      recurring: bool = True, durable: bool = True) -> str:
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"Error: {result}"
    return f"Scheduled {result.id}: '{cron}' → {prompt}"

def run_list_crons() -> str:
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return "No cron jobs. Use schedule_cron to add one."
    lines = []
    for j in jobs:
        tag = "recurring" if j.recurring else "one-shot"
        dur = "durable" if j.durable else "session"
        lines.append(f"  {j.id}: '{j.cron}' → {j.prompt[:40]} "
                     f"[{tag}, {dur}]")
    return "\n".join(lines)

def run_cancel_cron(job_id: str) -> str:
    return cancel_job(job_id)

def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    return spawn_teammate_thread(name, role, prompt)

def run_send_message(to: str, content: str) -> str:
    BUS.send('lead', to, content, 'message')
    return f"Sent to {to}"

def run_check_inbox() -> str:
    # 走协议路由：shutdown/plan 响应会更新 pending_requests 状态，其余消息返回注入对话
    msgs = consume_lead_inbox(route_protocol=True)
    if not msgs:
        return "No messages in inbox."
    return "\n".join(f"[from {m['from']} ({m.get('type', 'message')})] {m['content']}"
                     for m in msgs)


# ── Lead Protocol Tools (s16 new) ──

def run_request_shutdown(teammate: str) -> str:
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="shutdown",
        sender="lead", target=teammate,
        status="pending", payload="")
    BUS.send("lead", teammate, "Please shut down gracefully.",
             "shutdown_request",
             {"request_id": req_id})
    print(f"  \033[35m[protocol] shutdown_request → {teammate} "
          f"({req_id})\033[0m")
    return f"Shutdown request sent to {teammate} (req: {req_id})"


def run_request_plan(teammate: str, task: str) -> str:
    """Lead asks a teammate to submit a plan for a task."""
    BUS.send("lead", teammate, f"Please submit a plan for: {task}",
             "message")
    return f"Asked {teammate} to submit a plan"


def run_review_plan(request_id: str, approve: bool, feedback: str = "") -> str:
    state = pending_requests.get(request_id)
    if not state:
        return f"Request {request_id} not found"
    if state.status != "pending":
        return f"Request {request_id} already {state.status}"
    state.status = "approved" if approve else "rejected"
    BUS.send("lead", state.sender, feedback or ("Approved" if approve else "Rejected"),
             "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    icon = "✓" if approve else "✗"
    print(f"  \033[32m[protocol] plan {icon} ({request_id})\033[0m")
    return f"Plan {'approved' if approve else 'rejected'} ({request_id})"


tool_registry={'bash':run_bash,'background_output':run_background_output,
               'kill_background':run_kill_background,
               'read':run_read,'write':run_write,'edit':run_edit,
               'view_image':run_view_image,'read_document':run_read_document,
               'render_document':run_render_document,
               'apply_patch':run_apply_patch,
               'web_fetch':run_web_fetch,
               'glob':run_glob,'grep':run_grep,'ls':run_ls,'todo_write':run_todo_write,
               'todo_read':run_todo_read,
               'create_task':run_create_task,'list_tasks':run_list_tasks,'get_task':run_get_task,
               'claim_task':run_claim_task,'complete_task':run_complete_task,
               'schedule_cron':run_schedule_cron,'list_crons':run_list_crons,'cancel_cron':run_cancel_cron,
               'spawn_teammate':run_spawn_teammate,'send_message':run_send_message,'check_inbox':run_check_inbox,
               'request_shutdown':run_request_shutdown,'request_plan':run_request_plan,'review_plan':run_review_plan,
               'create_worktree':create_worktree,'remove_worktree':remove_worktree,'keep_worktree':keep_worktree}

TOOLS.append({
    "type":"function",
    "function":{
        "name":"web_fetch",
        "description":"Fetch a public http(s) URL and return readable text (HTML is converted). "
                      "Requires the same outbound approval as curl; private/loopback addresses "
                      "and redirects to them are blocked. Prefer this over curl for reading pages.",
        "parameters":{
            "type":"object",
            "properties":{
                "url":{
                    "type":"string",
                    "description":"Absolute http(s) URL to fetch"
                },
                "max_chars":{
                    "type":"integer",
                    "description":"Maximum characters of extracted text to return (default 20000, max 100000)"
                }
            },
            "required":[
                "url"
            ]
        }
    }
})

TOOLS.append({
    "type":"function",
    "function":{
        "name":"apply_patch",
        "description":"Apply a structured patch to one or more files. Supports add/update/delete/move "
                      "and applies all changes atomically (rolls back on failure). Use this instead of "
                      "multiple edit calls for multi-file changes.",
        "parameters":{
            "type":"object",
            "properties":{
                "patch":{
                    "type":"string",
                    "description":"Patch text framed by '*** Begin Patch' and '*** End Patch', using "
                                  "'*** Add File:', '*** Update File:', '*** Delete File:' headers and "
                                  "'*** Move to:' for renames. Hunk lines start with ' ', '+' or '-'."
                },
                "cwd":{
                    "type":"string",
                    "description":"Optional base directory for relative paths (default: workspace)"
                }
            },
            "required":[
                "patch"
            ]
        }
    }
})

TOOLS.append({
    "type":"function",
    "function":{
        "name":"checkpoint",
        "description":("Create a rewindable checkpoint of the current conversation and workspace. "
                       "Call it before risky multi-file refactors so the user can rewind with /rewind. "
                       "Only the user can trigger a rewind."),
        "parameters":{
            "type":"object",
            "properties":{
                "label":{
                    "type":"string",
                    "description":"Short label describing the safe point, e.g. 'before refactor'"
                }
            },
            "required":[]
        }
    }
})

TOOLS.append({
    "type":"function",
    "function":{
        "name":"task",
        "description":"Spawn a subagent to complete a task",
        "parameters":{
            "type":"object",
            "properties":{
                "description":{
                    "type":"string",
                    "description":"The task description"
                }
            },
            "required":[
                "description"
            ]
        }
    }


})

TOOLS.append({
    "type":"function",
    "function":{
        "name":"load_skill",
        "description":"Load a registered skill's instructions by its name. When the system prompt lists skills "
                      "(e.g. 'file-summarizer'), call this tool with that exact name to get the skill body instead "
                      "of searching the filesystem manually.",
        "parameters":{
            "type":"object",
            "properties":{
                "name":{
                    "type":"string",
                    "description":"The exact skill name shown in the Skills available list, e.g. 'file-summarizer'"
                }
            },
            "required":[
                "name"
            ]
        }
    }

})

TOOLS.append({
    "type":"function",
    "function":{
        "name":"compact",
        "description":"Summarize the conversation history to free up context. Call this when the conversation is "
                      "getting too long or you want to clear old tool outputs.",
        "parameters":{
            "type":"object",
            "properties":{}
        }
    }
})

TOOLS.append({
    "type":"function",
    "function":{
        "name":"request_shutdown",
        "description":"Ask a teammate agent to shut down gracefully.",
        "parameters":{
            "type":"object",
            "properties":{
                "teammate":{
                    "type":"string",
                    "description":"Teammate name to shut down"
                }
            },
            "required":["teammate"]
        }
    }
})

TOOLS.append({
    "type":"function",
    "function":{
        "name":"request_plan",
        "description":"Ask a teammate to submit a plan for a task.",
        "parameters":{
            "type":"object",
            "properties":{
                "teammate":{
                    "type":"string",
                    "description":"Teammate name"
                },
                "task":{
                    "type":"string",
                    "description":"The task to plan for"
                }
            },
            "required":["teammate", "task"]
        }
    }
})

TOOLS.append({
    "type":"function",
    "function":{
        "name":"review_plan",
        "description":"Approve or reject a teammate's submitted plan (by request_id).",
        "parameters":{
            "type":"object",
            "properties":{
                "request_id":{
                    "type":"string",
                    "description":"Plan request id from check_inbox"
                },
                "approve":{
                    "type":"boolean",
                    "description":"Whether to approve the plan"
                },
                "feedback":{
                    "type":"string",
                    "description":"Optional feedback when rejecting"
                }
            },
            "required":["request_id", "approve"]
        }
    }
})

TOOLS.append({
    "type":"function",
    "function":{
        "name":"create_worktree",
        "description":"Create a git worktree (isolated branch) for a task; optionally bind it to task_id.",
        "parameters":{
            "type":"object",
            "properties":{
                "name":{
                    "type":"string",
                    "description":"Worktree name (letters/digits/._-, 1-64 chars)"
                },
                "task_id":{
                    "type":"string",
                    "description":"Optional task id to bind to this worktree"
                }
            },
            "required":["name"]
        }
    }
})

TOOLS.append({
    "type":"function",
    "function":{
        "name":"remove_worktree",
        "description":"Remove a worktree; refuses if it has uncommitted/unpushed changes unless discard_changes=true.",
        "parameters":{
            "type":"object",
            "properties":{
                "name":{
                    "type":"string"
                },
                "discard_changes":{
                    "type":"boolean",
                    "description":"Force removal, discarding changes"
                }
            },
            "required":["name"]
        }
    }
})

TOOLS.append({
    "type":"function",
    "function":{
        "name":"keep_worktree",
        "description":"Keep a worktree for review (logs the event).",
        "parameters":{
            "type":"object",
            "properties":{
                "name":{
                    "type":"string"
                }
            },
            "required":["name"]
        }
    }
})
