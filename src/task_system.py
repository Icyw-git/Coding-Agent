import json
import random
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Literal, Optional

from file_lock import exclusive_file_lock

# 任务持久化目录：与仓库 .cache 约定一致，保存时按需自动创建
TASK_DIR = Path(__file__).resolve().parents[1] / '.cache' / 'tasks'
TASKS_DIR = TASK_DIR
task_lock = threading.RLock()
_transaction_state = threading.local()


@contextmanager
def task_transaction():
    """Serialize task read-modify-write across threads and processes.

    Nested calls reuse the lock held by the outer transaction; ``save_task``
    is called from create/claim/complete and must not try to re-lock.
    """
    with task_lock:
        depth = getattr(_transaction_state, 'depth', 0)
        if depth:
            _transaction_state.depth = depth + 1
            try:
                yield
            finally:
                _transaction_state.depth = depth
            return
        with exclusive_file_lock(TASK_DIR / '.tasks.lock'):
            _transaction_state.depth = 1
            try:
                yield
            finally:
                _transaction_state.depth = 0



@dataclass
class Task:
    id:str
    subject:str
    description:str
    status:Literal['pending','in_progress','completed']
    owner:Optional[str] = None
    blockedBy:List[str] = field(default_factory=list)
    worktree:str = ''   # 关联的 git worktree 名（空 = 无隔离目录）


def random_hex(n:int):
    return ''.join(random.choices('0123456789abcdef',k=n))

def create_task(subject:str,description:str='',blockedBy:Optional[List[str]] = None): #创建任务
    with task_transaction():
        task=Task(
            id=f'task_{int(time.time())}_{uuid.uuid4().hex[:8]}',
            subject=subject,
            description=description,
            status='pending',
            owner=None,
            blockedBy=blockedBy or []
        )
        save_task(task)   # 与 claim/complete 一致：状态变更即落盘，创建后立即可查
        return task

def can_start(task_id:str): #判断任务是否可以开始
    with task_lock:
        task=load_task(task_id)
        for dep_id in task.blockedBy:
            if not _task_path(dep_id).exists():
                return False
            dep=load_task(dep_id)
            if dep.status!='completed':
                return False
        return True


def claim_task(task_id:str,owner:str='agent')->str: #认领任务，返回认领结果
    with task_transaction():
        task=load_task(task_id)
        if task.status !='pending':
            return f'Task {task_id} is {task.status},cannot claim '
        if not can_start(task_id):
            deps=[]
            for dep_id in task.blockedBy:
                if not _task_path(dep_id).exists():
                    deps.append(f'{dep_id} (missing)')
                elif load_task(dep_id).status != 'completed':
                    deps.append(dep_id)
            return f'Blocked by:{deps}'
        task.owner=owner
        task.status='in_progress'
        save_task(task)
        return f'Claimed {task_id} ({task.subject})'


def complete_task(task_id:str)->str: #完成任务，返回完成结果
    with task_transaction():
        task=load_task(task_id)
        if task.status == 'completed':
            return f'Task {task_id} is already completed'
        if task.status != 'in_progress':
            return f'Task {task_id} is {task.status}, claim it before completing'
        task.status='completed'
        save_task(task)
        unblocked=[t.subject for t in list_tasks() if t.status=='pending' and t.blockedBy and can_start(t.id)]
        msg=f'Completed {task_id} ({task.subject})'
        if unblocked:
            msg+=f"\nUnblocked:{','.join(unblocked)}"
        return msg

def get_task(task_id:str)->str: #获取任务详情，返回 JSON 字符串
    task=load_task(task_id)
    return json.dumps(asdict(task),indent=2)

def _task_path(task_id:str)->Path: #获取任务文件路径
    return TASK_DIR/f'{task_id}.json'

def save_task(task:Task): #保存任务到文件
    with task_transaction():
        TASK_DIR.mkdir(parents=True, exist_ok=True)
        path = _task_path(task.id)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(asdict(task),indent=2), encoding='utf-8')
        tmp.replace(path)

def load_task(task_id:str)->Task: #从文件加载任务
    with task_lock:
        return Task(**json.loads(_task_path(task_id).read_text(encoding='utf-8')))

def list_tasks()->List[Task]: #列出所有任务
    with task_lock:
        if not TASKS_DIR.exists():
            return []
        return [Task(**json.loads(p.read_text(encoding='utf-8'))) for p in sorted(TASKS_DIR.glob('task_*.json'))]
