import os 
from dotenv import load_dotenv
import subprocess
import openai
import json
import time
import ast
import pathlib
from pathlib import Path
from typing import Optional
import yaml



load_dotenv()
WORKDIR = Path(os.getcwd()).resolve()
SKILLS_DIR=WORKDIR/"skills"

client=openai.OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL"),
    
)

SYSTEM=(
    'You are a coding agent working on Windows at {workdir}. '
    'Use the bash tool to execute shell commands (Windows cmd syntax works, e.g. dir instead of ls). '
    'Rules: '
    '(1) If the user denies or rejects an operation, STOP permanently. Do NOT retry it and do NOT find alternative '
    'commands or workarounds (del/rm/Remove-Item/powershell/etc.) to achieve the same result. '
    '(2) Prefer safe, non-destructive commands for inspection (dir, type, findstr) and only modify files when asked. '
    'Skills available:\n{catalog}\n'
     'Use load_skill to load a skill. E.g. load_skill("file-summarizer")'
)

SUB_SYSTEM=(
    'You are a focused sub-agent working at {workdir} on behalf of a parent agent. '
    'Complete the task you are given and return a concise final answer to the parent agent. '
    'Rules: '
    '(1) Stay strictly within the workspace; use only relative paths under {workdir}. '
    '(2) Prefer safe commands (dir, type, findstr) and the read/glob tools for inspection. '
    '(3) If the parent task asks you to write/edit/delete files, still ask yourself whether it is '
    'destructive; if it is, do NOT execute it silently — report back that user confirmation is required. '
    '(4) If you hit an error (tool fails, path missing), do not loop on the same command. Try one '
    'reasonable alternative, then report the situation honestly to the parent. '
    '(5) Keep the final answer short and structured: what you did, what you found, any issues. '
    'Do not re-plan the parent task and do not spawn further sub-agents. '
     'Skills available:\n{catalog}\n'
     'Use load_skill to load a skill. E.g. load_skill("file-summarizer")'
)

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

}]

SKILL_REGISTRY:dict[str,dict]={}


def _parse_frontmatter(text:str)->tuple[dict,str]:

    if not text.startswith('---'):

        return {},text
    parts=text.split('---',2)
    if len(parts)<3:
        return {},text
    try:
        meta=yaml.safe_load(parts[1]) or {}

    except yaml.YAMLError:
        meta={}
    return meta,parts[2].strip()

def _scan_skills():
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest=d / "skill.md"
        if manifest.exists():
            raw=manifest.read_text(encoding='utf-8', errors='replace')
            meta,body=_parse_frontmatter(raw)
            name=meta.get('name',d.name)
            desc=meta.get('description',raw.split('\n')[0].lstrip('#').strip())
            SKILL_REGISTRY[name]={'name':name,
                'description':desc,
                'body':body,
            }

_scan_skills()

def list_skills()->str:
    if not SKILL_REGISTRY:
        return "No skills registered"
    return '\n'.join(f'- **{skill["name"]}**: {skill["description"]}' for skill in SKILL_REGISTRY.values())

def build_system()->str:
    catalog=list_skills()
    return SYSTEM.format(workdir=WORKDIR, catalog=catalog, skills_dir=SKILLS_DIR)

def build_sub_system()->str:
    catalog=list_skills()
    return SUB_SYSTEM.format(workdir=WORKDIR, catalog=catalog, skills_dir=SKILLS_DIR)



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
        return out[:50000] if out else "No output"
    except subprocess.TimeoutExpired:
        return "Error: Command timeout(120s)"
    except (FileNotFoundError,OSError) as e:
        return f'Error:{e}'

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_read(path: str, limit: Optional[int] = None) -> str:
    try:
        lines = safe_path(path).read_text(encoding='utf-8', errors='replace').splitlines()
        if limit and limit < len(lines):
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
        base = Path(WORKDIR)
        if '**' in pattern:
            matches = [p for p in base.rglob(pattern) if p.is_file() and p.resolve().is_relative_to(WORKDIR)]
        else:
            matches = [p for p in base.glob(pattern) if p.is_file() and p.resolve().is_relative_to(WORKDIR)]
        results = sorted(str(p.relative_to(WORKDIR)) for p in matches)
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


tool_registry={'bash':run_bash,'read':run_read,'write':run_write,'edit':run_edit,'glob':run_glob,'todo_write':run_todo_write}

DENY_LIST=['rm -rf /','sudo','shutdown','reboot','> /dev/']
DESTRUCTIVE = ["rm ", "del ", "rmdir ", "rd ", "Remove-Item ", "erase ", "> /etc/", "chmod 777"]

HOOKS={"UserPromptSubmit":[],"PreToolUse":[],"PostToolUse":[],"Stop":[]}

def register_hook(event:str,callback):
    HOOKS[event].append(callback)

def trigger_hooks(event:str,*args):
    for callback in HOOKS[event]:
        result=callback(*args)
        if result is not None:
            return result #阻断工具调用
    return None

def is_tool_hook(tool:str,args:dict):
    if tool not in tool_registry.keys():
        return f'Error: unknown tool {tool}'
    return None

def permission_hook(tool:str,args:dict):
    if tool=='bash':
        for pattern in DENY_LIST:
            if pattern in args.get("command", ""):
                print(f'\n\033[31m⛔ Blocked: {pattern}\033[0m')
                return f"Error: Dangerous command {pattern}, please do not execute"
        for pattern in DESTRUCTIVE:
            if pattern in args.get("command", ""):
                print(f"\n\033[33m⚠  Potentially destructive command\033[0m")
                print(f"   Tool: {tool}({args})")
                choice=input("Allow? (y/n) ").strip().lower()
                if choice not in ('y','yes'):
                    return "Permission denied by user"
    elif tool in ["read", "write", "edit"]:
        path=args.get('path','')
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m⚠  Access outside workspace\033[0m")
            print(f"   Tool: {tool}({path})")
            choice=input("Allow? (y/n) ").strip().lower()
            if choice not in ('y','yes'):
                return "Permission denied by user"

    return None

def log_hook(tool:str,args:dict,output:str):
    print(f"\033[90m[HOOK] {tool}({args}) -> {output[:100]}\033[0m")
    return None

def large_output_hook(tool:str,args:dict,output:str):
    if len(str(output))>100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {tool}: {len(str(output))} chars\033[0m")
    return None

def context_injection_hook(query:str):
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

def summary_hook(messages:list):
    tool_count=sum(1 for m in messages if m.get('role')=='tool')
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

register_hook("UserPromptSubmit",context_injection_hook)
register_hook("Stop",summary_hook)
register_hook("PreToolUse",is_tool_hook)
register_hook("PreToolUse",permission_hook)
register_hook("PostToolUse",log_hook)
register_hook("PostToolUse",large_output_hook)

SUB_TOOLS=TOOLS.copy()

SUB_HANDLERS=tool_registry.copy()

def _extract_text(text:str)->str:
    return text.strip()


def spawn_subagent(description:str)->str:
    print(f'\n\033[35m[Subagent spawned]\033[0m')
    messages=[{'role':'system','content':build_sub_system()},{'role':'user','content':description}]


    for _ in range(30):

        response=client.chat.completions.create(
            model=os.getenv("LLM_MODEL_ID"),
            messages=messages,
            tools=SUB_TOOLS,
            temperature=0.7,
            max_tokens=8000,
        )
        message=response.choices[0].message
        messages.append(message.model_dump())

        if response.choices[0].finish_reason != "tool_calls":
            force=trigger_hooks('Stop',messages)
            if force:
                messages.append({'role':'user','content':force})
                continue
            if message.content:
                print(message.content)
            return message.content or "No output"

        tool_messages = []
        for tool_call in message.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)

            blocked = trigger_hooks("PreToolUse", name, args)
            if blocked:
                output = blocked
            elif name not in SUB_HANDLERS:
                output = f"Error: unknown tool {name}"
            else:
                output = SUB_HANDLERS[name](**args)
                print(output[:200])

            trigger_hooks("PostToolUse", name, args, output)

            tool_messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })

        messages.extend(tool_messages)

    result=_extract_text(messages[-1]['content']) if isinstance(messages[-1].get('content'), str) else ""
    if not result:
        for m in reversed(messages):
            if m['role']=='assistant' and isinstance(m.get('content'), str):
                result=_extract_text(m['content'])
                if result:
                    break
        if not result:
            result="Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result


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

tool_registry['task']=spawn_subagent

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

def load_skill(name:str)->str:
    skill=SKILL_REGISTRY.get(name)
    if not skill:
        return f"Error: skill {name} not found"
    return skill['body']

tool_registry['load_skill']=load_skill

CONTEXT_LIMIT=50000
KEEP_RECENT=3
PERSIST_THERSHOLD=30000
TOOL_RESULTS_DIR=WORKDIR/'.cache'/'tool_results'
TRANSCRIPT_DIR=WORKDIR/'.cache'/'transcripts'

def estimate_size(msgs):
    return len(str(msgs))

def _block_type(message)->str:
    if isinstance(message,dict):
        if message.get('tool_calls'):
            return 'tool_calls'
        if message.get('content'):
            return 'text'
        return 'empty'
    if getattr(message,'tool_calls',None):
        return 'tool_calls'
    if getattr(message,'content',None):
        return 'text'
    return 'empty'

# OpenAI 格式：assistant 的 tool_calls 是顶层字段（content 为 null/字符串），
# 工具结果是独立的 role='tool' 消息（tool_call_id + content 字符串）
def _message_has_tool_use(msg):
    return msg.get('role')=='assistant' and bool(msg.get('tool_calls'))

def _is_tool_result_message(msg):
    return msg.get('role')=='tool'

def snip_compact(messages,max_messages=50):
    if len(messages)<=max_messages:
        return messages
    keep_head,keep_tail=3,max_messages-3
    head_end,tail_start=keep_head,len(messages)-keep_tail
    if head_end >0 and _message_has_tool_use(messages[head_end-1]):
        while head_end <len(messages) and _is_tool_result_message(messages[head_end]):
            head_end+=1
    if (tail_start >0 and tail_start <len(messages)) and _is_tool_result_message(messages[tail_start]) and _message_has_tool_use(messages[tail_start-1]):
        tail_start-=1
    if head_end>=tail_start:
        return messages
    snipped=tail_start-head_end
    return messages[:head_end]+[{'role':'user','content':f"[snipped {snipped} messages]"}]+messages[tail_start:]

def collect_tool_results(messages):
    return [msg for msg in messages if msg.get('role')=='tool']

def micro_compact(messages):
    tool_results=collect_tool_results(messages)
    if len(tool_results)<KEEP_RECENT:
        return messages
    for msg in tool_results[:-KEEP_RECENT]:
        if len(str(msg.get('content','')))>120:
            msg['content']="[Earlier tool result compacted. Re-run if needed.]"
    return messages

def persist_large_output(tool_use_id,output):
    if len(output)<=PERSIST_THERSHOLD:
        return output
    TOOL_RESULTS_DIR.mkdir(parents=True,exist_ok=True)
    path=TOOL_RESULTS_DIR / f'{tool_use_id}.txt'
    if not path.exists():
        path.write_text(output)
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"

def tool_result_budget(messages,max_bytes=200000):
    tool_msgs=[m for m in messages if m.get('role')=='tool']
    total=sum(len(str(m.get('content',''))) for m in tool_msgs)
    if total<=max_bytes:
        return messages
    ranked=sorted(tool_msgs,key=lambda m:len(str(m.get('content',''))),reverse=True)
    for m in ranked:
        if total<=max_bytes:
            break
        content=str(m.get('content',''))
        if len(content)<=PERSIST_THERSHOLD:
            continue
        tid=m.get('tool_call_id','unknown')
        m['content']=persist_large_output(tid,content)
        total=sum(len(str(x.get('content',''))) for x in tool_msgs)
    return messages

def write_transcript(messages):
    TRANSCRIPT_DIR.mkdir(parents=True,exist_ok=True)

    path=TRANSCRIPT_DIR /f'transcript_{int(time.time())}.jsonl'

    with open(path,'w',encoding='utf-8') as f:
        for msg in messages:
            f.write(json.dumps(msg,default=str)+'\n')
    return path

def summarize_history(messages):
    conversation=json.dumps(messages,default=str)[:80000]
    prompt= ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n" + conversation)
    response=client.chat.completions.create(
        model=os.getenv("LLM_MODEL_ID"),
        messages=[{'role':'user','content':prompt}],
        temperature=0.7,
        max_tokens=2000,
    )
    return (response.choices[0].message.content or "(empty summary)").strip()

def compact_history(messages):
    transcript_path=write_transcript(messages)
    print(f'[transcript saved:{transcript_path}]')
    summary=summarize_history(messages)
    return [{'role':'user','content':f'[Compacted]\n\n{summary}'}]

def reactive_compact(messages):
    transcript=write_transcript(messages)
    tail_start=max(0,len(messages)-5)
    if (tail_start>0 and tail_start<len(messages)) and _is_tool_result_message(messages[tail_start]) and _message_has_tool_use(messages[tail_start-1]):
        tail_start-=1
    summary=summarize_history(messages[:tail_start])
    return [{'role':'user','content':f'[Reactive compact]\n\n{summary}'}]
    


def agent_loop(messages:list):
    if messages:
        trigger_hooks('UserPromptSubmit',messages[-1]['content'])
    messages.append({'role':'system','content':build_system()})

    while True:
        response=client.chat.completions.create(

            model=os.getenv("LLM_MODEL_ID"),
            messages=messages,
            tools=TOOLS,
            temperature=0.7,
            max_tokens=8000,
        )

        message=response.choices[0].message
        messages.append(message.model_dump())

        if response.choices[0].finish_reason != "tool_calls":
            force=trigger_hooks('Stop',messages)
            if force:
                messages.append({'role':'user','content':force})
                continue
            if message.content:
                print(message.content)
            return message.content or "No output"
            

        tool_messages = []
        for tool_call in message.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)

            blocked = trigger_hooks("PreToolUse", name, args)
            if blocked:
                output = blocked
            elif name not in tool_registry:
                output = f"Error: unknown tool {name}"
            else:
                output = tool_registry[name](**args)
                print(output[:200])

            trigger_hooks("PostToolUse", name, args, output)

            tool_messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })

        messages.extend(tool_messages)

if __name__ == '__main__':
    messages = [{'role': 'user', 'content': '当前工作区有 file-summarizer 这个 skill，请用它来总结 harness.py 这个文件的代码结构。'}]
    agent_loop(messages)