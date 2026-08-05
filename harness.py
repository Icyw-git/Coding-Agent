import os 
from dotenv import load_dotenv
import subprocess
import openai
import json
import time
import ast
import pathlib
import re
from pathlib import Path
from typing import Optional
import yaml



load_dotenv()
WORKDIR = Path(os.getcwd()).resolve()
SKILLS_DIR=WORKDIR/"skills"
MEMORY_TYPES=['user','feedback','project','reference']
MEMORY_DIR=WORKDIR/'.cache'/'memories'
MEMORY_INDEX=MEMORY_DIR/'MEMORY.md'



client=openai.OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL"),
    
)

SYSTEM=(
    'You are a coding agent working on Windows at {workdir}. '
    'Use the bash tool to execute shell commands (Windows cmd syntax works, e.g. dir instead of ls). '
    '{memories_section}'
    'Rules: '
    '(1) If the user denies or rejects an operation, STOP permanently. Do NOT retry it and do NOT find alternative '
    'commands or workarounds (del/rm/Remove-Item/powershell/etc.) to achieve the same result. '
    '(2) Prefer safe, non-destructive commands for inspection (dir, type, findstr). '
    'To read file contents prefer the read tool (supports limit); avoid PowerShell Get-Content, '
    'which mis-decodes UTF-8 files on this system. Only modify files when asked. '
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

def write_memory_file(name:str,mem_type:str,description:str,body:str):
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    slug=name.lower().replace(' ','-').replace('/','-')

    filename=f'{slug}.md'
    filepath=MEMORY_DIR/filename
    filepath.write_text(
        f'---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n'

    )
    _rebuild_index()

    return filepath

def _rebuild_index():
    lines=[]
    for f in sorted(MEMORY_DIR.glob('*.md')):
        if f.name=='MEMORY.md':
            continue
        raw=f.read_text()
        meta,body=_parse_frontmatter(raw)
        name=meta.get('name',f.name)
        desc=meta.get('description',raw.split('\n')[0][:80])
        lines.append(f'- [{name}({f.name}) - {desc}]')
    MEMORY_INDEX.write_text('\n'.join(lines)+'\n' if lines else '')

def read_memory_index()->str:
    if not MEMORY_INDEX.exists():
        return ''

    text=MEMORY_INDEX.read_text().strip()
    return text if text else ''

def read_memory_file(filename:str):
    path=MEMORY_DIR /filename
    if not path.exists():
        return None

    return path.read_text()

def list_memory_files()->list[dict]:
    results=[]
    for f in sorted(MEMORY_DIR.glob('*.md')):
        if f.name=='MEMORY.md':
            continue
        raw=f.read_text()
        meta,body=_parse_frontmatter(raw)
        results.append({
            'filename':f.name,
            'name':meta.get('name',f.stem),
            'description':meta.get('description',''),
            'type':meta.get('type','user'),
            'body':body
        })
    return results

def select_relevant_memories(messages:list,max_items:int=5)->list[str]:
    files=list_memory_files()
    if not files:
        return []
    recent_texts=[]
    for msg in reversed(messages):
        if msg.get('role')=='user':
            content=msg.get('content','')
            if isinstance(content,list):
                content = " ".join(
                    str(getattr(b, "text", "")) for b in content
                    if getattr(b, "type", None) == "text"
                )
            if isinstance(content, str):
                recent_texts.append(content)
            if len(recent_texts) >= 3:
                break
    recent=' '.join(reversed(recent_texts))[:2000]

    if not recent.strip():
        return []

    catalog_lines=[]
    for i ,f in enumerate(files):
        catalog_lines.append(f"{i}:{f['name']} - {f['description']}")
    catalog='\n'.join(catalog_lines)

    prompt = (
        "Given the recent conversation and the memory catalog below, "
        "select the indices of memories that are clearly relevant. "
        "Return ONLY a JSON array of integers, e.g. [0, 3]. "
        "If none are relevant, return [].\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"Memory catalog:\n{catalog}"
    )

    try:
        response=client.chat.completions.create(
            model=os.getenv('LLM_MODEL_ID'),
            messages=[{'role':'user','content':prompt}],
            temperature=0.7,
            max_tokens=200,

        )
        text=_extract_text(response.choices[0].message.content).strip()
        match=re.search(r'\[.*?\]', text, re.DOTALL)
        if match:
            indices=json.loads(match.group())
            selected=[]
            for idx in indices:
                if isinstance(idx,int) and 0<=idx<len(files):
                    selected.append(files[idx]['filename'])
                    if len(selected)>=max_items:
                        break
            return selected
    except Exception:
        pass

    keywords=[w.lower() for w in recent.split() if len(w)>=3]
    selected=[]
    for f in files:
        text=(f['name']+' '+f['description']).lower()
        if any(kw in text for kw in keywords):
            selected.append(f['filename'])
            if len(selected)>=max_items:
                break
    return selected

def load_memories(messages:list)->str:
    selected_files=select_relevant_memories(messages)
    if not selected_files:
        return ''
    
    parts=["<relevant_memories>"]
    for f in selected_files:
        content=read_memory_file(f)
        if content:
            parts.append(content)
    parts.append('</relevant_memories>')
    return '\n\n'.join(parts)

def extract_memories(messages:list):
    dialogue_parts=[]
    for msg in messages[-10:]:
        role=msg.get('role','?')
        content=msg.get('content','')
        if isinstance(content,list):
            content = " ".join(
                str(getattr(b, "text", "")) for b in content
                if getattr(b, "type", None) == "text"
            )
        if isinstance(content, str) and content.strip():
            dialogue_parts.append(f"{role}: {content}")
    dialogue = "\n".join(dialogue_parts)

    if not dialogue.strip():
        return 
    
    existing=list_memory_files()
    existing_desc='\n'.join(f"- {m['name']}: {m['description']}" for m in existing) if existing else "(none)"

    prompt=(
        "Extract user preferences, constraints, or project facts from this dialogue.\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n"
        "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
        "- type: one of 'user' (user preference), 'feedback' (guidance), "
        "'project' (project fact), 'reference' (external pointer)\n"
        "- description: one-line summary for index lookup\n"
        "- body: full detail in markdown\n"
        "If nothing new or already covered by existing memories, return [].\n\n"
        f"Existing memories:\n{existing_desc}\n\n"
        f"Dialogue:\n{dialogue[:4000]}"
    )

    try:
        response=client.chat.completions.create(
            model=os.getenv('LLM_MODEL_ID'),
            messages=[{'role':'user','content':prompt}],
            temperature=0.7,
            max_tokens=800,
        )
        text=_extract_text(response.choices[0].message.content).strip()
        match=re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return 
        items=json.loads(match.group())
        if not items:
            return
        count=0
        for mem in items:
            name=mem.get('name',f'memory_{int(time.time())}')
            mem_type=mem.get('type','user')
            desc=mem.get('description','')
            body=mem.get('body','')
            if desc and body:
                write_memory_file(name,mem_type,desc,body)
                count+=1
        if count:
            print(f"\n\033[33m[Memory: extracted {count} new memories]\033[0m")
    except Exception:
        pass

CONSOLIDATE_THRESHOLD = 10

def consolidate_memories():
    """Merge duplicate/stale memories. Triggered when file count ≥ threshold."""
    files = list_memory_files()
    if len(files) < CONSOLIDATE_THRESHOLD:
        return

    catalog = "\n\n".join(
        f"## {f['filename']}\nname: {f['name']}\ndescription: {f['description']}\n{f['body']}"
        for f in files
    )

    prompt = (
        "Consolidate the following memory files. Rules:\n"
        "1. Merge duplicates into one\n"
        "2. Remove outdated/contradicted memories\n"
        "3. Keep the total under 30 memories\n"
        "4. Preserve important user preferences above all\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n\n"
        f"{catalog[:16000]}"
    )

    try:
        response = client.chat.completions.create(
            model=os.getenv('LLM_MODEL_ID'),
            messages=[{'role':'user','content':prompt}], max_tokens=3000
        )
        text = _extract_text(response.choices[0].message.content).strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())

        # Remove old memory files (keep MEMORY.md)
        for f in MEMORY_DIR.glob("*.md"):
            if f.name != "MEMORY.md":
                f.unlink()

        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)

        print(f"\n\033[33m[Memory: consolidated {len(files)} → {len(items)} memories]\033[0m")
    except Exception:
        pass



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
    if not isinstance(meta, dict):
        meta={}   # frontmatter 非映射（如缺空格被当纯标量）时按空处理
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
    index=read_memory_index()
    memories_section = f"\n\nMemories available:\n{index}" if index else ""
    catalog=list_skills()
    return SYSTEM.format(workdir=WORKDIR, catalog=catalog, skills_dir=SKILLS_DIR, memories_section=memories_section)

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
        out=out.replace('\ufffd','?')  # PS Get-Content 误读 UTF-8 产生的替换符，GBK 控制台打印会崩
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

# compact 工具：让模型在上下文过长时主动触发历史摘要
# （不注册进 tool_registry，由 agent_loop 特殊处理）
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

# 模型上下文窗口（token），可用环境变量 LLM_CONTEXT_WINDOW 覆盖
MODEL_WINDOWS = {
    'deepseek-v4-flash': 131072,
    'deepseek-chat': 65536,
    'deepseek-reasoner': 65536,
}
OUTPUT_RESERVE = 8192        # 给 max_tokens=8000 输出预留
CTX_SAFETY = 0.8             # 留余量，防 prompt_too_long

def _default_context_limit() -> int:
    window = int(os.getenv('LLM_CONTEXT_WINDOW') or
                 MODEL_WINDOWS.get(os.getenv('LLM_MODEL_ID', ''), 65536))
    return max(4000, int((window - OUTPUT_RESERVE) * CTX_SAFETY))

CONTEXT_LIMIT=_default_context_limit()
KEEP_RECENT=6
PERSIST_THERSHOLD=10000
TOOL_RESULTS_DIR=WORKDIR/'.cache'/'tool_results'
TRANSCRIPT_DIR=WORKDIR/'.cache'/'transcripts'

def estimate_size(msgs):
    # token 启发式：英文约 3.5 字符/token，中文约 1.3 字符/token（与 CONTEXT_LIMIT 同单位）
    text=str(msgs)
    ascii_chars=sum(1 for ch in text if ord(ch) < 128)
    cjk=len(text)-ascii_chars
    return max(1, int(ascii_chars/3.5 + cjk*1.3))



# OpenAI 格式：assistant 的 tool_calls 是顶层字段（content 为 null/字符串），
# 工具结果是独立的 role='tool' 消息（tool_call_id + content 字符串）
def _message_has_tool_use(msg): #判断是否工具调用
    return msg.get('role')=='assistant' and bool(msg.get('tool_calls'))

def _is_tool_result_message(msg): #判断是否工具结果
    return msg.get('role')=='tool'

def snip_compact(messages,max_messages=50): #保留头 3 条，尾部47 条，中间按 tool_calls 分组
    if len(messages)<=max_messages:
        return messages
    # 头部：保留前 3 条；不能切断"assistant 带多个工具"的分组（否则 dangling）
    head_end=3
    if head_end>0 and head_end<len(messages):
        # 情况 A：头部以 assistant-with-tool_calls 结尾 → 并入其工具结果
        if _message_has_tool_use(messages[head_end-1]):
            while head_end<len(messages) and _is_tool_result_message(messages[head_end]):
                head_end+=1
        # 情况 B：头部切断在分组中间（前后都是工具结果）→ 并入剩余工具结果
        while head_end<len(messages) and _is_tool_result_message(messages[head_end-1]) and _is_tool_result_message(messages[head_end]):
            head_end+=1
    # 尾部预算：head + 1 条 marker + tail <= max_messages
    keep_tail=max_messages-head_end-1
    if keep_tail<=1:
        return messages[:head_end]
    tail_start=len(messages)-keep_tail
    # 裁剪点不能落在工具结果上（其 assistant 已被裁掉=孤儿），推进到非工具消息
    while tail_start<len(messages) and _is_tool_result_message(messages[tail_start]):
        tail_start+=1
    if head_end>=tail_start:
        return messages
    result=messages[:head_end]+[{'role':'user','content':f"[snipped {tail_start-head_end} messages]"}]+messages[tail_start:]
    # 若配对调整导致超出预算，从尾部再裁掉超出的条数，并避免以"孤儿"工具结果开头
    over=len(result)-max_messages
    while over>0 and tail_start<len(messages):
        tail_start+=1
        over-=1
        while (tail_start<len(messages) and _is_tool_result_message(messages[tail_start])):
            tail_start+=1
            over-=1
    return messages[:head_end]+[{'role':'user','content':f"[snipped {tail_start-head_end} messages]"}]+messages[tail_start:]

def collect_tool_results(messages): #收集所有工具结果
    return [msg for msg in messages if msg.get('role')=='tool']

def micro_compact(messages): #旧工具结果占位，只保留最近 KEEP_RECENT 条工具结果
    tool_results=collect_tool_results(messages)
    if len(tool_results)<KEEP_RECENT:
        return messages
    for msg in tool_results[:-KEEP_RECENT]:
        if len(str(msg.get('content','')))>120:
            msg['content']="[Earlier tool result compacted. Re-run if needed.]"
    return messages

def persist_large_output(tool_use_id,output): #持久化大输出
    if len(output)<=PERSIST_THERSHOLD:
        return output
    TOOL_RESULTS_DIR.mkdir(parents=True,exist_ok=True)
    path=TOOL_RESULTS_DIR / f'{tool_use_id}.txt'
    if not path.exists():
        path.write_text(output,encoding='utf-8')
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"

def tool_result_budget(messages,max_bytes=None):  #将工具结果压缩到 max_bytes 内存
    if max_bytes is None:
        # 默认预算取 CONTEXT_LIMIT 的 80%，保证 budget 先于自动压缩(LLM)触发
        max_bytes=int(CONTEXT_LIMIT*0.8)
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
        m['content']=persist_large_output(tid,content) #持久化大输出，返回引用路径和预览内容
        total=sum(len(str(x.get('content',''))) for x in tool_msgs)
    return messages

def write_transcript(messages): #写入对话记录，返回路径
    TRANSCRIPT_DIR.mkdir(parents=True,exist_ok=True)

    path=TRANSCRIPT_DIR /f'transcript_{int(time.time())}.jsonl'

    with open(path,'w',encoding='utf-8') as f:
        for msg in messages:
            f.write(json.dumps(msg,default=str)+'\n')
    return path

def summarize_history(messages): #总结对话记录，返回总结内容
    if not messages:
        return "(empty summary)"
    conversation=json.dumps(messages,default=str)[-80000:]
    prompt= ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete, and MUST complete "
              "all five sections - if tight on space, drop details rather than truncating.\n\n" + conversation)
    response=client.chat.completions.create(
        model=os.getenv("LLM_MODEL_ID"),
        messages=[{'role':'user','content':prompt}],
        temperature=0.7,
        max_tokens=4000,
    )
    return (response.choices[0].message.content or "(empty summary)").strip()

def compact_history(messages): #使用llm进行对话记录压缩，返回压缩后的消息
    transcript_path=write_transcript(messages)
    print(f'[transcript saved:{transcript_path}]')
    summary=summarize_history(messages)
    return [{'role':'system','content':build_system()},
            {'role':'user','content':f'[Compacted]\n\n{summary}'}]

def reactive_compact(messages): #保存完整对话记录，保留最近五条消息，返回压缩后的消息
    transcript=write_transcript(messages)
    tail_start=max(0,len(messages)-5)
    # 尾部起点不能是孤儿工具结果（其 assistant 已被裁掉）
    while tail_start<len(messages) and _is_tool_result_message(messages[tail_start]):
        tail_start+=1
    summary=summarize_history(messages[:tail_start])
    result=[{'role':'system','content':build_system()},
            {'role':'user','content':f'[Reactive compact]\n\n{summary}'}]
    result.extend(messages[tail_start:])   # 保留最近工作现场
    return result
    
MAX_REACTIVE_RETRIES=1

def agent_loop(messages:list):
    if messages:
        trigger_hooks('UserPromptSubmit',messages[-1]['content'])
    messages.append({'role':'system','content':build_system()})
    reactive_retries=0
    skip_micro_rounds=0
    rounds_since_todo=0
    memories_content=load_memories(messages)   # 相关记忆块（空串 = 无相关记忆）

    while True:
        pre_compress=[m if isinstance(m,dict) else {'role':m.get('role',''),'content':str(m.get('content',''))} for m in messages]
        

        messages[:]=tool_result_budget(messages)
        messages[:]=snip_compact(messages)
        if skip_micro_rounds>0:
            skip_micro_rounds-=1  # 压缩后前几轮跳过 micro_compact，保留刚拿到的工具结果
        else:
            messages[:]=micro_compact(messages)

        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:]=compact_history(messages)
            skip_micro_rounds=3

        if rounds_since_todo >=8 and messages:
            messages.append({
                'role':'user',
                'content':"<reminder>Update your todos.</reminder>",
            })
            rounds_since_todo=0
        




        request_messages = messages
        if memories_content:
            # 记忆只注入请求副本，不污染 messages（压缩/转录/tool 配对保持干净）
            request_messages = messages.copy()
            for i, m in enumerate(request_messages):
                if m.get('role') == 'user' and isinstance(m.get('content'), str):
                    request_messages[i] = {**m, 'content': memories_content + '\n\n' + m['content']}
                    break

        try:
            response=client.chat.completions.create(
                model=os.getenv("LLM_MODEL_ID"),
                messages=request_messages,
                tools=TOOLS,
                temperature=0.7,
                max_tokens=8000,
            )
            reactive_retries=0
        except Exception as e:
            if ('prompt_too_long' in str(e).lower() or "too many tokens" in str(e).lower()) and reactive_retries<MAX_REACTIVE_RETRIES:

                reactive_retries+=1
                print('[reactive compact]')
                messages[:]=reactive_compact(messages)
                skip_micro_rounds=3
                continue
            raise


        message=response.choices[0].message
        messages.append(message.model_dump())

        if response.choices[0].finish_reason != "tool_calls":
            force=trigger_hooks('Stop',messages)
            if force:
                messages.append({'role':'user','content':force})
                continue
            if message.content:
                print(message.content)
            write_transcript(messages)   # 正常结束也落盘，便于复盘
            extract_memories(pre_compress)   # 从压缩前快照提炼记忆，压缩不丢信息
            consolidate_memories()
            return message.content or "No output"
            

        # compact 工具：在 for 循环内按顺序处理——compact 之前的 tool_call 先正常执行，
        # 遇到 compact 时压缩历史并重开一轮。
        tool_messages = []
        compacted = False
        used_todo = False
        for tool_call in message.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)
            if name=='todo_write':
                used_todo = True
            if name == 'compact':
                # compact_history 会替换整个上下文，原 assistant 的 tool_calls 被删除，
                # 之前已执行的 tool 结果不能以 role=tool 保留（会变成 orphan），
                # 改为文本附到新上下文里，避免执行结果丢失。
                messages[:] = compact_history(messages)
                skip_micro_rounds=3
                note = '[Compacted] Conversation history has been summarized.'
                if tool_messages:
                    done = '\n'.join(f"- {str(tm['content'])[:1000]}" for tm in tool_messages)
                    note += '\n\nTool results executed before compaction:\n' + done
                messages.append({'role': 'user', 'content': note})
                compacted = True
                break

            blocked = trigger_hooks("PreToolUse", name, args)
            if blocked:
                output = blocked
            elif name not in tool_registry:
                output = f"Error: unknown tool {name}"
            else:
                try:
                    output = tool_registry[name](**args)
                except Exception as e:
                    # 模型可能传 schema 之外的参数（如 offset），**args 展开时在函数体外抛错
                    output = f"Error: {e}"

            trigger_hooks("PostToolUse", name, args, output)

            tool_messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })

        if compacted: #压缩后工具丢失，需要重新执行
            continue

        messages.extend(tool_messages)
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1

if __name__ == '__main__':
    messages = [{'role': 'user', 'content': '请用中文总结当前工作区所有 .py 文件的代码结构。'
    '注意：以后输出报告一律用中文；'
    '并且永远不要用 PowerShell Get-Content 读文件（会乱码），改用 read 工具。'}]
    agent_loop(messages)