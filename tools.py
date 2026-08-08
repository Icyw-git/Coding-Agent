"""工具系统：TOOLS schema + 全部 run_* handler + tool_registry。
可变全局（WORKDIR 等）由 harness 持有，通过 _h() 在运行时读取（便于测试 patch）。
"""
import ast
import json
import os
import subprocess
from pathlib import Path
from typing import Optional, List

from task_system import create_task, list_tasks, get_task, claim_task, complete_task
from cron_scheduler import schedule_job, cancel_job, cron_lock, scheduled_jobs
from agent_teams import spawn_teammate_thread, BUS
from team_protocols import new_request_id, ProtocolState, pending_requests, consume_lead_inbox


def _h():
    import harness
    return harness


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
        "description":"Read the contents of a file in the workspace",
        "parameters":{
            "type":"object",
            "properties":{
                "path":{
                    "type":"string",
                    "description":"Relative path to the file from the workspace root"
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
        "description":"Write content to a file in the workspace",
        "parameters":{
            "type":"object",
            "properties":{
                "path":{
                    "type":"string",
                    "description":"Relative path to the file from the workspace root"
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
        "description":"Replace a unique text fragment in a workspace file",
        "parameters":{
            "type":"object",
            "properties":{
                "path":{
                    "type":"string",
                    "description":"Relative path to the file from the workspace root"
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
        "description":"List files in the workspace matching a glob pattern",
        "parameters":{
            "type":"object",
            "properties":{
                "pattern":{
                    "type":"string",
                    "description":"Glob pattern, e.g. '*.py' or 'src/**/*.ts'"
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

def run_bash(command:str)->str:

    dangerous=["rm -rf /","sudo","shutdown","reboot","> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command, please do not execute"
    try:
        r=subprocess.run(command,shell=True,cwd=os.getcwd(),capture_output=True,timeout=120)
        raw=(r.stdout or b"")+(r.stderr or b"")
        try:
            out=raw.decode("utf-8")
        except UnicodeDecodeError:
            out=raw.decode("gbk",errors="replace")
        out=out.strip()
        out=out.replace('\ufffd','?')  # PS Get-Content 误读 UTF-8 产生的替换符，GBK 控制台打印会崩
        return out[:50000] if out else "No output"
    except subprocess.TimeoutExpired:
        return "Error: Command timeout(120s)"
    except (FileNotFoundError,OSError) as e:
        return f'Error:{e}'

def safe_path(p: str) -> Path:
    path = (_h().WORKDIR / p).resolve()
    if not path.is_relative_to(_h().WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_read(path: str, limit: Optional[int] = None) -> str:
    try:
        lines = safe_path(path).read_text(encoding='utf-8', errors='replace').splitlines()
        if limit is None and len(lines) > 500:
            # 大文件没传 limit：自动截断，防止整文件灌进上下文
            lines = lines[:200] + [f"... ({len(lines) - 200} more lines; use limit to read in chunks)"]
        elif limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding='utf-8')
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding='utf-8', errors='replace')
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding='utf-8')
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

def run_glob(pattern: str) -> str:
    try:
        base = Path(_h().WORKDIR)
        if '**' in pattern:
            matches = [p for p in base.rglob(pattern) if p.is_file() and p.resolve().is_relative_to(_h().WORKDIR)]
        else:
            matches = [p for p in base.glob(pattern) if p.is_file() and p.resolve().is_relative_to(_h().WORKDIR)]
        results = sorted(str(p.relative_to(_h().WORKDIR)) for p in matches)
        return "\n".join(results) if results else "(no matches)"
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


tool_registry={'bash':run_bash,'read':run_read,'write':run_write,'edit':run_edit,'glob':run_glob,'todo_write':run_todo_write,
               'create_task':run_create_task,'list_tasks':run_list_tasks,'get_task':run_get_task,
               'claim_task':run_claim_task,'complete_task':run_complete_task,
               'schedule_cron':run_schedule_cron,'list_crons':run_list_crons,'cancel_cron':run_cancel_cron,
               'spawn_teammate':run_spawn_teammate,'send_message':run_send_message,'check_inbox':run_check_inbox,
               'request_shutdown':run_request_shutdown,'request_plan':run_request_plan,'review_plan':run_review_plan}

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
