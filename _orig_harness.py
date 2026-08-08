import os 
from dotenv import load_dotenv
import subprocess
import openai
import json
import time
import ast
import pathlib
import re
import threading
from pathlib import Path
from typing import Optional, List
import yaml
from background_task import should_run_background, start_background_task, collect_background_results
import recovery
from task_system import create_task, list_tasks, get_task, claim_task, complete_task
from cron_scheduler import (schedule_job, cancel_job, consume_cron_queue,
                            cron_lock, scheduled_jobs, cron_scheduler_loop,
                            load_durable_jobs, agent_lock,
                            queue_processor_loop, wait_for_cron_idle)



load_dotenv()
WORKDIR = Path(os.getcwd()).resolve()
SKILLS_DIR=WORKDIR/"skills"



client=openai.OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL"),
    
)

PROMPT_SECTIONS={
    'identity_main':(
        'You are a coding agent working on Windows at {workdir}. '
        'Use the bash tool to execute shell commands (Windows cmd syntax works, e.g. dir instead of ls). '
    ),
    'identity_sub':(
        'You are a focused sub-agent working at {workdir} on behalf of a parent agent. '
        'Complete the task you are given and return a concise final answer to the parent agent. '
    ),
    'rules_main':(
        'Rules: '
        '(1) If the user denies or rejects an operation, STOP permanently. Do NOT retry it and do NOT find alternative '
        'commands or workarounds (del/rm/Remove-Item/powershell/etc.) to achieve the same result. '
        '(2) Prefer safe, non-destructive commands for inspection (dir, type, findstr). '
        'To read file contents prefer the read tool (supports limit); avoid PowerShell Get-Content, '
        'which mis-decodes UTF-8 files on this system. Only modify files when asked. '
    ),
    'rules_sub':(
        'Rules: '
        '(1) Stay strictly within the workspace; use only relative paths under {workdir}. '
        '(2) Prefer safe commands (dir, type, findstr) and the read/glob tools for inspection. '
        '(3) If the parent task asks you to write/edit/delete files, still ask yourself whether it is '
        'destructive; if it is, do NOT execute it silently — report back that user confirmation is required. '
        '(4) If you hit an error (tool fails, path missing), do not loop on the same command. Try one '
        'reasonable alternative, then report the situation honestly to the parent. '
        '(5) Keep the final answer short and structured: what you did, what you found, any issues. '
        'Do not re-plan the parent task and do not spawn further sub-agents. '
    ),
    'skills_usage':(
        'Skills available:\n{catalog}\n'
        'Use load_skill to load a skill. E.g. load_skill("file-summarizer")'
    ),
}

def assemble_system_prompt(context:dict)->str: # 组装系统提示，包含身份、工具、工作目录、记忆、技能、规则等
    sections=[]
    sections.append(PROMPT_SECTIONS['identity_main'].format(
        workdir=context.get('workspace',WORKDIR)
    ))
    tools=','.join(context.get('enabled_tools',[]))
    if tools:
        sections.append(f'Available tools:{tools}.')
    sections.append(f'Working directory:{context.get("workspace",WORKDIR)}.')

    memories=context.get('memories','')
    if memories:
        sections.append(f'Relevant memories:\n{memories}')
    skills=context.get('skills','')
    if skills:
        sections.append(PROMPT_SECTIONS['skills_usage'].format(catalog=skills))
    sections.append(PROMPT_SECTIONS['rules_main'])
    return '\n\n'.join(sections)

_last_context_key=None # 缓存键，用于判断是否需要重新组装系统提示
_last_prompt=None # 缓存系统提示，用于判断是否需要重新组装系统提示

# ============================================================
# 全局配置集中区
# ============================================================

# ---------- 记忆系统配置 ----------
MEMORY_TYPES=['user','feedback','project','reference']
MEMORY_DIR=WORKDIR/'.cache'/'memories'
MEMORY_INDEX=MEMORY_DIR/'MEMORY.md'
CONSOLIDATE_THRESHOLD=10

# ---------- 安全策略 ----------
DENY_LIST=['rm -rf /','sudo','shutdown','reboot','> /dev/']
DESTRUCTIVE=["rm ","del ","rmdir ","rd ","Remove-Item ","erase ","> /etc/","chmod 777"]

# ---------- Hook 注册表 ----------
HOOKS={"UserPromptSubmit":[],"PreToolUse":[],"PostToolUse":[],"Stop":[]}

# ---------- 模型上下文配置 ----------
MODEL_WINDOWS={
    'deepseek-v4-flash':131072,
    'deepseek-chat':65536,
    'deepseek-reasoner':65536,
}
OUTPUT_RESERVE=8192        # 给 max_tokens=8000 输出预留
CTX_SAFETY=0.8             # 留余量，防 prompt_too_long

def _default_context_limit()->int:
    window=int(os.getenv('LLM_CONTEXT_WINDOW') or
               MODEL_WINDOWS.get(os.getenv('LLM_MODEL_ID',''),65536))
    return max(4000,int((window-OUTPUT_RESERVE)*CTX_SAFETY))

CONTEXT_LIMIT=_default_context_limit()
KEEP_RECENT=6
PERSIST_THERSHOLD=10000

# ---------- 缓存目录 ----------
TOOL_RESULTS_DIR=WORKDIR/'.cache'/'tool_results'
TRANSCRIPT_DIR=WORKDIR/'.cache'/'transcripts'

# ---------- 重试配置 ----------
MAX_REACTIVE_RETRIES=1


def get_system_prompt(context:dict)->str: #获取上下文，如果上下文没有改变，直接返回缓存的系统提示，否则重新组装系统提示
    global _last_context_key,_last_prompt
    key=json.dumps(context,sort_keys=True,ensure_ascii=False,default=str)
    if key==_last_context_key and _last_prompt:
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
    _last_context_key=key
    _last_prompt=assemble_system_prompt(context)

    loaded=['identity','tools','workspace','rules']
    if context.get('memories'):
        loaded.append('memory')
    if context.get('skills'):
        loaded.append('skills')
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return _last_prompt

def update_context(context:dict,messages:list)->dict: # 更新上下文，包含工具、工作目录、记忆、技能等
    memories=''
    if MEMORY_INDEX.exists():
        content=MEMORY_INDEX.read_text(encoding='utf-8',errors='replace').strip()
        if content:
            memories=content
    skills=list_skills()
    return {
        'enabled_tools':list(tool_registry.keys()),
        'workspace':str(WORKDIR),
        'memories':memories,
        'skills':skills,
    }


def build_system()->str:
    return assemble_system_prompt(update_context({},[]))


def build_sub_system()->str:
    return (
        PROMPT_SECTIONS['identity_sub'].format(workdir=WORKDIR) +
        '\n\n' +
        PROMPT_SECTIONS['rules_sub'].format(workdir=WORKDIR) +
        '\n\n' +
        PROMPT_SECTIONS['skills_usage'].format(catalog=list_skills())
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

}]

def write_memory_file(name:str,mem_type:str,description:str,body:str): #保存记忆文件
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    slug=name.lower().replace(' ','-').replace('/','-')

    filename=f'{slug}.md'
    filepath=MEMORY_DIR/filename
    filepath.write_text(
        f'---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n',
        encoding='utf-8',
    )
    _rebuild_index() #重建记忆索引

    return filepath

def _rebuild_index():
    lines=[]
    for f in sorted(MEMORY_DIR.glob('*.md')):
        if f.name=='MEMORY.md':
            continue
        raw=f.read_text(encoding='utf-8', errors='replace')
        meta,body=_parse_frontmatter(raw)
        name=meta.get('name',f.name)
        desc=meta.get('description',raw.split('\n')[0][:80])
        lines.append(f'- [{name}({f.name}) - {desc}]') #索引的结构是：[名称](文件名) - �描述
    MEMORY_INDEX.write_text('\n'.join(lines)+'\n' if lines else '', encoding='utf-8')

def read_memory_index()->str: #读取记忆索引
    if not MEMORY_INDEX.exists():
        return ''

    text=MEMORY_INDEX.read_text(encoding='utf-8', errors='replace').strip()
    return text if text else ''

def read_memory_file(filename:str): #读取记忆文件
    path=MEMORY_DIR /filename
    if not path.exists():
        return None

    return path.read_text(encoding='utf-8', errors='replace')

def list_memory_files()->list[dict]: #列出所有记忆文件
    results=[]
    for f in sorted(MEMORY_DIR.glob('*.md')):
        if f.name=='MEMORY.md':
            continue
        raw=f.read_text(encoding='utf-8', errors='replace')
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
        if msg.get('role')=='user': #只考虑用户消息，因为只有用户消息才会包含记忆
            content=msg.get('content','')
            if isinstance(content,list): #处理包含多个文本的消息，示例：[{'type':'text','text':'你好'},{'type':'text','text':'我是张三'}]
                content = " ".join(
                    str(getattr(b, "text", "")) for b in content
                    if getattr(b, "type", None) == "text"
                )
            if isinstance(content, str):
                recent_texts.append(content)
            if len(recent_texts) >= 3: #最多考虑最近3条消息
                break
    recent=' '.join(reversed(recent_texts))[:2000] #最近3条消息，最多2000个字符

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
        match=re.search(r'\[.*?\]', text, re.DOTALL) #解析JSON数组，如[0, 3]
        if match:
            indices=json.loads(match.group())
            selected=[]
            for idx in indices:
                if isinstance(idx,int) and 0<=idx<len(files):
                    selected.append(files[idx]['filename'])
                    if len(selected)>=max_items: #最多选择max_items个记忆文件
                        break
            return selected
    except Exception:
        pass

    keywords=[w.lower() for w in recent.split() if len(w)>=3] #fallback，根据最近消息中的关键词筛选记忆文件,关键词长度至少为3
    selected=[]
    for f in files:
        text=(f['name']+' '+f['description']).lower()
        if any(kw in text for kw in keywords): #如果记忆文件的名称或描述包含关键词
            selected.append(f['filename'])
            if len(selected)>=max_items:
                break
    return selected

def load_memories(messages:list)->str: #加载相关记忆文件内容
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

def extract_memories(messages:list): #提取用户偏好、约束、项目事实等记忆
    # 记忆窗口：会话开头前 10 条（用户偏好通常最先说）+ 结尾最后 10 条
    if len(messages) <= 20:
        window=messages
    else:
        window=messages[:10]+messages[-10:]
        
    dialogue_parts=[]
    for msg in window:
        role=msg.get('role','?')
        content=msg.get('content','')
        if isinstance(content,list):
            content = " ".join(
                str(getattr(b, "text", "")) for b in content
                if getattr(b, "type", None) == "text"
            )
        if isinstance(content, str) and content.strip():
            dialogue_parts.append(f"{role}: {content}")
    dialogue = "\n".join(dialogue_parts) #合并所有消息，最多4000个字符，生成对话记录

    if not dialogue.strip():
        print("[Memory: extract skipped - no text content in recent messages]")
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
        "If nothing new or already covered by existing memories, return [].\n"
        "OUTPUT FORMAT (STRICT): reply with ONLY a plain JSON array. "
        "Do NOT think out loud, do NOT explain, do NOT wrap it in markdown code "
        "fences (```json), do NOT add any text before or after the array.\n\n"
        f"Existing memories:\n{existing_desc}\n\n"
        f"Dialogue:\n{dialogue[:4000]}"
    )

    try:
        response=client.chat.completions.create(
            model=os.getenv('LLM_MODEL_ID'),
            messages=[{'role':'user','content':prompt}],
            temperature=0.7,
            max_tokens=4000,
        )
        msg=response.choices[0].message
        text=_extract_text(msg.content or '').strip()
        if not text:
            # 推理模型可能把 max_tokens 预算全花在 reasoning_content 上导致 content 为空，
            # 退而从 reasoning_content 里找 JSON 数组
            text=_extract_text(getattr(msg,'reasoning_content','') or '').strip()
        match=re.search(r'\[.*\]', text, re.DOTALL)
        # 解析 JSON 数组：LLM 回复里可能混有自然语言方括号或多个数组段（Extra data），
        # 先试贪婪匹配，失败再逐个尝试最短数组段，取第一个能解析成 list 的
        items=None
        if match:
            try:
                items=json.loads(match.group())
            except Exception:
                items=None
        if not isinstance(items,list):
            for m in re.finditer(r'\[.*?\]', text, re.DOTALL):
                try:
                    cand=json.loads(m.group())
                    if isinstance(cand,list):
                        items=cand
                        break
                except Exception:
                    continue
        if not isinstance(items,list):
            print(f"[Memory: extract failed - no valid JSON array in LLM reply: {text[:200]!r}]")
            return
        if not items:
            print("[Memory: extract returned empty (LLM judged nothing worth remembering)]")
            return
        count=0
        for mem in items:
            name=mem.get('name',f'memory_{int(time.time())}')
            mem_type=mem.get('type','user')
            desc=mem.get('description','')
            body=mem.get('body','')
            if desc and body:
                write_memory_file(name,mem_type,desc,body) #写入新记忆文件，格式为name.md，内容为description\nbody
                count+=1
        if count:
            print(f"\n\033[33m[Memory: extracted {count} new memories]\033[0m")
        else:
            print(f"[Memory: extract returned {len(items)} items but none had desc+body]")
    except Exception as e:
        print(f"[Memory: extract error: {e}]")

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
            print(f"[Memory: consolidate failed - no JSON array in LLM reply: {text[:200]!r}]")
            return
        items = json.loads(match.group())
        if not items:
            # 必须先于删除逻辑返回：否则会把所有记忆文件删光再写 0 条
            print("[Memory: consolidate returned empty - aborting (would delete all memories)]")
            return

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
                write_memory_file(name, mem_type, desc, body) #边写边更新索引


        print(f"\n\033[33m[Memory: consolidated {len(files)} → {len(items)} memories]\033[0m")
    except Exception as e:
        print(f"[Memory: consolidate error: {e}]")



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

# Task tools

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


# ── Cron Tools ──

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


tool_registry={'bash':run_bash,'read':run_read,'write':run_write,'edit':run_edit,'glob':run_glob,'todo_write':run_todo_write,
               'create_task':run_create_task,'list_tasks':run_list_tasks,'get_task':run_get_task,
               'claim_task':run_claim_task,'complete_task':run_complete_task,
               'schedule_cron':run_schedule_cron,'list_crons':run_list_crons,'cancel_cron':run_cancel_cron}

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

def agent_loop(messages:list):
    context=update_context({},messages)
    recovery_state=recovery.RecoveryState()
    if messages:
        trigger_hooks('UserPromptSubmit',messages[-1]['content'])
    system=get_system_prompt(context)
    reactive_retries=0 #响应重试次数
    skip_micro_rounds=0 #跳过 micro_compact 轮数
    rounds_since_todo=0 #距离上次 todo 提醒的轮数
    # 记忆加载：整个 Loop 只在此处加载一次（LLM 从 .cache/memories 目录筛选相关记忆）。
    # ⚠️ 提醒：循环中途不会再次刷新 —— 本轮新产生的信息不会进入 memories_content；
    #    要等会话结束 extract_memories 落盘后，下一次会话的 load_memories 才能召回。
    memories_content=load_memories(messages)   # 相关记忆块（空串 = 无相关记忆）

    while True:
        pre_compress=[m if isinstance(m,dict) else {'role':m.get('role',''),'content':str(m.get('content',''))} for m in messages] #压缩前的快照：纯净对话（不含 cron/提醒），供会话结束提炼记忆
        

        messages[:]=tool_result_budget(messages) #具体压缩方式：大的工具结果优先压缩
        messages[:]=snip_compact(messages) #保留头尾消息，切除中间部分
        if skip_micro_rounds>0:
            skip_micro_rounds-=1  # 压缩后前几轮跳过 micro_compact，保留刚拿到的工具结果
        else:
            messages[:]=micro_compact(messages)

        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:]=compact_history(messages) #调用Llm进行压缩
            skip_micro_rounds=3

        if rounds_since_todo >=8 and messages:
            messages.append({
                'role':'user',
                'content':"<reminder>Update your todos.</reminder>",
            })
            rounds_since_todo=0

        fired=consume_cron_queue()
        for job in fired:
            messages.append({
                'role':'user',
                'content':f'[Scheduled] {job.prompt}'
            })
            print(f"  \033[35m[inject cron] {job.prompt[:50]}\033[0m")

        




        request_messages = messages
        if memories_content:
            # 记忆注入：只往「第一条用户消息」追加（break 后即停），不是每条消息都追加；
            # 注入的是 request_messages 副本，不污染 messages，压缩/转录/tool 配对保持干净。
            request_messages = messages.copy()
            for i, m in enumerate(request_messages):
                if m.get('role') == 'user' and isinstance(m.get('content'), str):
                    request_messages[i] = {**m, 'content': memories_content + '\n\n' + m['content']}
                    break

        # system 提示通过 messages 传入（当前 SDK 不支持 system= 关键字参数）
        request_messages=[{'role':'system','content':system}]+request_messages

        def _create():
            max_tokens=(recovery.ESCALATED_MAX_TOKENS
                        if recovery_state.has_escalated
                        else recovery.DEFAULT_MAX_TOKENS)
            return client.chat.completions.create(
                model=recovery_state.current_model or os.getenv("LLM_MODEL_ID"),
                messages=request_messages,
                tools=TOOLS,
                temperature=0.7,
                max_tokens=max_tokens,
            )

        try:
            response=recovery.with_retry(_create,recovery_state)
            reactive_retries=0
        except Exception as e:
            if recovery.is_prompt_too_long_error(e) and reactive_retries<MAX_REACTIVE_RETRIES:
                reactive_retries+=1
                print('[reactive compact]')
                messages[:]=reactive_compact(messages) #保存完整对话记录，保留最近五条消息，返回压缩后的消息
                skip_micro_rounds=3
                continue
            raise


        message=response.choices[0].message
        messages.append(message.model_dump()) #添加模型回复到上下文

        # 输出被截断：升级到加大 max_tokens 并提示续写
        if response.choices[0].finish_reason=='length' and not recovery_state.has_escalated:
            recovery_state.has_escalated=True
            print('  [length] escalating max_tokens and requesting continuation')
            messages.append({'role':'user','content':recovery.CONTINUATION_PROMPT})
            continue


        if response.choices[0].finish_reason != "tool_calls":
            force=trigger_hooks('Stop',messages)
            if force:
                messages.append({'role':'user','content':force})
                continue
            if message.content:
                print(message.content)
            write_transcript(messages)   # 正常结束也落盘，便于复盘
            # extract_memories(pre_compress)：从「压缩前快照」提炼记忆（压缩会丢细节，快照保证不漏）。
            # 提炼对象：user(用户偏好) / feedback(引导性反馈) / project(项目事实) / reference(外部参考)。
            # ⚠️ 提醒：只负责落盘到 .cache/memories/*.md，不会反馈回当前 Loop ——
            #    本会话的新记忆要等下一次会话 load_memories 才会被召回。
            extract_memories(pre_compress)   # 从压缩前快照提炼记忆，压缩不丢信息
            # consolidate_memories()：仅当记忆文件数 ≥ CONSOLIDATE_THRESHOLD(10) 时触发，
            #    合并重复/过期记忆；LLM 返回空数组时会中止，防止删光所有记忆。
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
                    if should_run_background(name, args):
                        bg_id = start_background_task(tool_call, tool_registry)
                        output = (f"[Background task {bg_id} started] "
                                  f"Command: {args.get('command', '')}. "
                                  f"Result will be available when complete.")
                    else:
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

        notifications=collect_background_results()
        if notifications:
            for notification in notifications:
                # 后台任务完成通知：作为 user 消息注入（孤儿 role=tool 会触发 API 400）
                tool_messages.append(
                    {
                        'role':'user',
                        'content':notification,
                        
                    }
                )
            print(f"  \033[32m[inject] {len(notifications)} background "
                  f"notification(s)\033[0m")

        messages.extend(tool_messages)
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        context=update_context(context,messages) # 更新上下文，包含工具、工作目录、记忆、技能等
        system=get_system_prompt(context) #更新系统提示，包含身份、工具、工作目录、记忆、技能、规则等


def run_agent_turn_locked():
    """持锁状态下执行一轮对话（供 queue_processor_loop 调用）：
    消费 cron 队列 → 构造 user 消息 → agent_loop。"""
    fired = consume_cron_queue()
    if not fired:
        return
    prompts = [f'[Scheduled] {job.prompt}' for job in fired]
    print(f"  \033[35m[cron turn] {len(fired)} scheduled job(s)\033[0m")
    try:
        agent_loop([{'role': 'user', 'content': '\n'.join(prompts)}])
    except KeyboardInterrupt:
        print('\n[会话已中断]')


if __name__ == '__main__':
    # 启动 cron 调度线程（生产队列）+ 队列处理器（推送执行）；测试直接调 agent_loop 不受影响
    load_durable_jobs()
    threading.Thread(target=cron_scheduler_loop, daemon=True).start()
    threading.Thread(target=queue_processor_loop,
                     args=(run_agent_turn_locked,), daemon=True).start()

    def _run_session(msgs):
        try:
            agent_loop(msgs)
        except KeyboardInterrupt:
            print('\n[会话已中断]')

    # 主会话持 agent_lock：队列处理器只会在主会话结束后才推送 cron 任务
    with agent_lock:
        _run_session([{'role': 'user', 'content': 'Create a one-shot reminder in 1 minute to check the build status'}])

    # 保持进程存活直到 cron 任务全部处理完（无任务立即退出）；Ctrl+C 退出
    try:
        wait_for_cron_idle()
    except KeyboardInterrupt:
        print('\n[会话已中断]')