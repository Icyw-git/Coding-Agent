import dataclasses
from dataclasses import dataclass, asdict, field
from re import S
from typing import Optional, List,Literal
from pathlib import Path

import json

# 任务持久化目录：与仓库 .cache 约定一致，保存时按需自动创建
TASK_DIR = Path(__file__).resolve().parent / '.cache' / 'tasks'
TASKS_DIR = TASK_DIR



@dataclass 
class Task:
    id:str
    subject:str
    description:str
    status:Literal['pending','in_progress','completed']
    owner:Optional[str] = None
    blockedBy:List[str] = field(default_factory=list)

import time
import random
def random_hex(n:int):
    return ''.join(random.choices('0123456789abcdef',k=n))

def create_task(subject:str,description:str='',blockedBy:Optional[List[str]] = None):
    task=Task(
        id=f'task_{int(time.time())}_{random_hex(4)}',
        subject=subject,
        description=description,
        status='pending',

        owner=None,
        blockedBy=blockedBy or []
    )
    save_task(task)   # 与 claim/complete 一致：状态变更即落盘，创建后立即可查
    return task

def can_start(task_id:str):
    task=load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        dep=load_task(dep_id)
        if dep.status!='completed':
            return False
    return True


def claim_task(task_id:str,owner:str='agent')->str:
    task=load_task(task_id)
    if task.status !='pending':
        return f'Task {task_id} is {task.status},cannot claim '
    if not can_start(task_id):
        deps=[d for d in task.blockedBy if load_task(d).status!='completed']
        return f'Blocked by:{deps}'
    task.owner=owner
    task.status='in_progress'
    save_task(task)
    return f'Claimed {task_id} ({task.subject})'


def complete_task(task_id:str)->str:
    task=load_task(task_id)

    task.status='completed'
    save_task(task)
    unblocked=[t.subject for t in list_tasks() if t.status=='pending' and t.blockedBy and can_start(t.id)]
    msg=f'Completed {task_id} ({task.subject})'
    if unblocked:
        msg+=f"\nUnblocked:{','.join(unblocked)}"
    return msg

def get_task(task_id:str)->str:
    task=load_task(task_id)
    return json.dumps(asdict(task),indent=2)

def _task_path(task_id:str)->Path:
    return TASK_DIR/f'{task_id}.json'

def save_task(task:Task):
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    _task_path(task.id).write_text(json.dumps(asdict(task),indent=2))

def load_task(task_id:str)->Task:
    return Task(**json.loads(_task_path(task_id).read_text()))

def list_tasks()->List[Task]:
    if not TASKS_DIR.exists():
        return []
    return [Task(**json.loads(p.read_text())) for p in sorted(TASKS_DIR.glob('task_*.json'))]
