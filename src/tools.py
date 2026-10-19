"""工具系统：TOOLS schema + 全部 run_* handler + tool_registry。
可变全局（WORKDIR 等）由 harness 持有，通过 _h() 在运行时读取（便于测试 patch）。
"""
import ast
import json
import os
import re
import subprocess
import tempfile
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Optional, List

import sandbox
import checkpoints
from process_utils import close_process, kill_process_tree

from exec_policy import is_secret_path, resolve_tool_path
from task_system import create_task, list_tasks, get_task, claim_task, complete_task
from cron_scheduler import schedule_job, cancel_job, cron_lock, scheduled_jobs
from agent_teams import spawn_teammate_thread, BUS
from team_protocols import new_request_id, ProtocolState, pending_requests, consume_lead_inbox
from worktree import create_worktree, remove_worktree, keep_worktree


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
        "description":"Execute shell commands",
        "parameters":{
            "type":"object",
            "properties":{
                "command":{
                    "type":"string",
                    "description":"The shell command to execute, e.g. 'ls'"
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
        "description":"Read a file. Absolute paths and paths outside the workspace are allowed; protected secret files are denied.",
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
        "description":"Write a file. Workspace paths are written directly; paths outside the workspace require approval.",
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
                    "description":"Root directory (or file). Relative to the workspace/cwd, or absolute; defaults to the workspace"
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
                    "description":"File or directory to search. Relative to the workspace/cwd, or absolute; defaults to the workspace"
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
                    "description":"Directory or file to list. Relative to the workspace/cwd, or absolute; defaults to the workspace"
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
        "description":"Update the current task list shown to the user. Use for tracking multi-step work.",
        "parameters":{
            "type":"object",
            "properties":{
                "todos":{
                    "type":"array",
                    "description":"List of tasks, each with 'content' and 'status' (pending/in_progress/completed)",
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
        "description":"Schedule a cron job: at matching times the prompt is injected into the conversation as a scheduled task.",
        "parameters":{
            "type":"object",
            "properties":{
                "cron":{
                    "type":"string",
                    "description":"5-field cron expression, e.g. '*/5 * * * *' (minute hour day-of-month month day-of-week)"
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

BASH_TIMEOUT = 120


def run_bash(command:str, cwd: str = None)->str:
    # 判定只有一处（策略层），第三份黑名单已删：teammate / 后台 worker / 任务 runner
    # 绕过 PreToolUse hook 直接走到这里，凭据缺失时按无头路径审批（远程队列，超时拒绝）。
    denied = _gate().enforce_headless('bash', {'command': command})
    if denied:
        return denied
    proc = None
    try:
        base = cwd or str(_h().get_workdir())
        # A descendant can keep a PIPE open after cmd.exe exits. A temporary
        # output file lets the timeout bound execution without waiting for EOF.
        with tempfile.TemporaryFile() as output:
            # cwd 同时是 OS 沙箱的可写根：teammate 在 worktree 里执行时，
            # 可写范围就是那个 worktree，而不是主工作区。
            proc = sandbox.spawn_shell(command, cwd=base, stdout=output,
                                       workspace=base)
            try:
                proc.wait(timeout=BASH_TIMEOUT)
            except subprocess.TimeoutExpired:
                kill_process_tree(proc)
                return f'Error: Command timeout({BASH_TIMEOUT}s); process termination requested.'
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
        close_process(proc)

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

def run_read(path: str, limit: Optional[int] = None, cwd: str = None) -> str:
    # 读默认放行，但秘密路径是硬拒；teammate 等无头路径不走 PreToolUse，
    # 边界上必须自己判一次。
    denied = _gate_file('read', path, cwd)
    if denied:
        return denied
    try:
        lines = safe_path(path, cwd).read_text(encoding='utf-8', errors='replace').splitlines()
        if limit is None and len(lines) > 500:
            # 大文件没传 limit：自动截断，防止整文件灌进上下文
            lines = lines[:200] + [f"... ({len(lines) - 200} more lines; use limit to read in chunks)"]
        elif limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str, cwd: str = None) -> str:
    # read-only 档 / 无头路径（teammate、后台）的写改同样要在边界拦一道：
    # 判定参数与 safe_path 一致，两处不会给出相反结论。
    denied = _gate_file('write', path, cwd)
    if denied:
        return denied
    try:
        file_path = safe_path(path, cwd)
        _capture_checkpoint(file_path)   # 写盘前保存 pre-image，供 /rewind 回滚
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding='utf-8')
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str, cwd: str = None) -> str:
    denied = _gate_file('edit', path, cwd)
    if denied:
        return denied
    try:
        file_path = safe_path(path, cwd)
        text = file_path.read_text(encoding='utf-8', errors='replace')
        if old_text not in text:
            return f"Error: text not found in {path}"
        _capture_checkpoint(file_path)   # 确认能改再抓 pre-image，避免记下无意义的快照
        file_path.write_text(text.replace(old_text, new_text, 1), encoding='utf-8')
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

MAX_SEARCH_BYTES = 2 * 1024 * 1024
_SEARCH_IGNORE = frozenset(checkpoints.DEFAULT_IGNORE) | {
    'node_modules', '.venv', 'venv', '.tox', '.mypy_cache'}


def _search_root(path: str, cwd: str | None) -> tuple[Path | None, str | None]:
    """只读搜索工具的根路径：显式秘密路径先拒，其余与 read 共用同一解析。"""
    denied = _gate_file('read', path, cwd)
    if denied:
        return None, denied
    try:
        return safe_path(path, cwd), None
    except Exception as exc:
        return None, f'Error: {exc}'


def _secret_file(path: Path) -> bool:
    """按最终目标判断：名为 notes.txt 的链接指向 .env 时，同样不能搜内容。"""
    if is_secret_path(path, checkpoints.DEFAULT_SECRETS):
        return True
    if path.is_symlink():
        try:
            return is_secret_path(path.resolve(), checkpoints.DEFAULT_SECRETS)
        except OSError:
            return True
    return False


def _search_files(root: Path):
    """遍历搜索根；跳过忽略目录、指向目录的链接与秘密文件。"""
    if root.is_file():
        if not _secret_file(root):
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in _SEARCH_IGNORE
            and not (Path(dirpath) / name).is_symlink())
        for name in sorted(filenames):
            candidate = Path(dirpath) / name
            if candidate.is_file() and not _secret_file(candidate):
                yield candidate


def _match_search_glob(path: Path, root: Path, pattern: str | None) -> bool:
    if not pattern:
        return True
    if '/' in pattern or '\\' in pattern:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        return fnmatchcase(rel, pattern.replace('\\', '/'))
    return fnmatchcase(path.name, pattern)


def run_glob(pattern: str, path: str = None, cwd: str = None) -> str:
    root, error = _search_root(path or '.', cwd)
    if error:
        return error
    try:
        if not root.exists():
            return f'Error: path not found: {path}'
        matches = [p for p in root.glob(pattern) if p.is_file()]
        results = sorted(str(p.relative_to(root)) for p in matches)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def run_grep(pattern: str, path: str = '.', glob: str = None,
             case_insensitive: bool = False, max_results: int = 100,
             cwd: str = None) -> str:
    """按正则搜索文件内容，返回 `path:line: text`。

    只读工具：根路径走 read 的分区判定；目录遍历跳过秘密文件与常见忽略目录。
    二进制与超过 MAX_SEARCH_BYTES 的文件不搜，避免把一次检索变成整盘扫描。
    """
    try:
        regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    except re.error as exc:
        return f'Error: invalid pattern: {exc}'
    root, error = _search_root(path, cwd)
    if error:
        return error
    if not root.exists():
        return f'Error: path not found: {path}'
    limit = max(1, min(int(max_results), 1000))
    hits: list[str] = []
    try:
        for candidate in _search_files(root):
            if not _match_search_glob(candidate, root, glob):
                continue
            try:
                if candidate.stat().st_size > MAX_SEARCH_BYTES:
                    continue
                data = candidate.read_bytes()
            except OSError:
                continue
            if b'\x00' in data[:4096]:
                continue
            text = data.decode('utf-8', errors='replace')
            try:
                display = str(candidate.relative_to(root)) \
                    if root.is_dir() else str(root)
            except ValueError:
                display = str(candidate)
            for lineno, line in enumerate(text.splitlines(), 1):
                if not regex.search(line):
                    continue
                if len(line) > 400:
                    line = line[:400] + '…'
                hits.append(f'{display}:{lineno}: {line}')
                if len(hits) >= limit:
                    return '\n'.join(hits) + f'\n(truncated at {limit} matches)'
    except Exception as e:
        return f'Error: {e}'
    return '\n'.join(hits) if hits else "(no matches)"


def run_ls(path: str = '.', all: bool = False, cwd: str = None) -> str:
    """列目录（只读）。隐藏项需要 all=True；路径可以在工作区外。"""
    root, error = _search_root(path, cwd)
    if error:
        return error
    try:
        if not root.exists():
            return f'Error: path not found: {path}'
        if root.is_file():
            return f'f  {root.name}  {root.stat().st_size} B'
        if not root.is_dir():
            return f'Error: not a file or directory: {path}'
        rows = []
        for item in sorted(root.iterdir(),
                           key=lambda p: (not p.is_dir(), p.name.lower())):
            if not all and item.name.startswith('.'):
                continue
            try:
                if item.is_dir():
                    rows.append(f'd  {item.name}/')
                else:
                    rows.append(f'f  {item.name}  {item.stat().st_size} B')
            except OSError:
                rows.append(f'?  {item.name}')
        return "\n".join(rows) if rows else "(empty)"
    except Exception as e:
        return f"Error: {e}"

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
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None

def run_todo_write(todos: list) -> str:
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"

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


tool_registry={'bash':run_bash,'read':run_read,'write':run_write,'edit':run_edit,'glob':run_glob,'grep':run_grep,'ls':run_ls,'todo_write':run_todo_write,
               'create_task':run_create_task,'list_tasks':run_list_tasks,'get_task':run_get_task,
               'claim_task':run_claim_task,'complete_task':run_complete_task,
               'schedule_cron':run_schedule_cron,'list_crons':run_list_crons,'cancel_cron':run_cancel_cron,
               'spawn_teammate':run_spawn_teammate,'send_message':run_send_message,'check_inbox':run_check_inbox,
               'request_shutdown':run_request_shutdown,'request_plan':run_request_plan,'review_plan':run_review_plan,
               'create_worktree':create_worktree,'remove_worktree':remove_worktree,'keep_worktree':keep_worktree}

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
        "description":"Load a registered skill's instructions by its name. When the system prompt lists skills (e.g. 'file-summarizer'), call this tool with that exact name to get the skill body instead of searching the filesystem manually.",
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
        "description":"Summarize the conversation history to free up context. Call this when the conversation is getting too long or you want to clear old tool outputs.",
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
