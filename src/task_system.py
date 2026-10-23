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
DEFAULT_LEASE_SECONDS = 3600
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
    claimed_at:Optional[float] = None
    lease_expires:Optional[float] = None
    attempts:int = 0


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


def lease_expired(task:Task, now:Optional[float]=None)->bool:
    """in_progress 任务是否已超过租约；升级前没有租约的旧任务视为失效。"""
    if task.status != 'in_progress':
        return False
    if task.lease_expires is None:
        return True
    return task.lease_expires <= (time.time() if now is None else now)


def claimable_tasks(now:Optional[float]=None)->List[Task]:
    """可认领任务：pending 按创建顺序在前，过期租约按认领时间排在后面。"""
    current = time.time() if now is None else now
    pending, expired = [], []
    for task in list_tasks():
        if task.status == 'pending':
            if can_start(task.id):
                pending.append(task)
        elif task.status == 'in_progress' and lease_expired(task, current):
            expired.append(task)
    expired.sort(key=lambda task: (task.attempts or 0, task.claimed_at or 0, task.id))
    return pending + expired


def claim_task(task_id:str,owner:str='agent',
               lease_seconds:float=DEFAULT_LEASE_SECONDS)->str: #认领任务，返回认领结果
    with task_transaction():
        task=load_task(task_id)
        recovered = task.status == 'in_progress' and lease_expired(task)
        if not recovered and task.status != 'pending':
            return f'Task {task_id} is {task.status},cannot claim '
        if not can_start(task_id):
            deps=[]
            for dep_id in task.blockedBy:
                if not _task_path(dep_id).exists():
                    deps.append(f'{dep_id} (missing)')
                elif load_task(dep_id).status != 'completed':
                    deps.append(dep_id)
            return f'Blocked by:{deps}'
        now = time.time()
        task.owner=owner
        task.status='in_progress'
        task.claimed_at=now
        task.lease_expires=now + max(0.0, float(lease_seconds))
        task.attempts=(task.attempts or 0) + 1
        save_task(task)
        suffix = ' (recovered expired lease)' if recovered else ''
        return f'Claimed {task_id} ({task.subject}){suffix}'


def claim_next_task(owner:str='agent',
                    lease_seconds:float=DEFAULT_LEASE_SECONDS)->Optional[Task]:
    """认领队列中的下一个任务；候选在事务内重读，抢不到时继续尝试后续。"""
    with task_transaction():
        for task in claimable_tasks():
            result = claim_task(task.id, owner, lease_seconds)
            if 'Claimed' in result:
                return load_task(task.id)
        return None


def renew_lease(task_id:str,owner:str='agent',
                lease_seconds:float=DEFAULT_LEASE_SECONDS)->str: #续期任务租约
    """持有者每轮刷新租约，避免长任务超过 1 小时被其他 teammate 回收。"""
    with task_transaction():
        task=load_task(task_id)
        if task.status != 'in_progress':
            return f'Task {task_id} is {task.status}, nothing to renew'
        if owner and task.owner and owner != task.owner:
            return f'Task {task_id} is owned by {task.owner}, cannot renew'
        task.lease_expires = time.time() + max(0.0, float(lease_seconds))
        save_task(task)
        return f'Renewed lease for {task_id} ({task.subject})'


def complete_task(task_id:str, owner:Optional[str]=None)->str: #完成任务，返回完成结果
    with task_transaction():
        task=load_task(task_id)
        if task.status == 'completed':
            return f'Task {task_id} is already completed'
        if task.status != 'in_progress':
            return f'Task {task_id} is {task.status}, claim it before completing'
        if owner and task.owner and owner != task.owner:
            return f'Task {task_id} is owned by {task.owner}, cannot complete'
        task.status='completed'
        task.lease_expires=None
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
