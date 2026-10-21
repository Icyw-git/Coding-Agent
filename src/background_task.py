import json
import threading
import time
from contextvars import ContextVar
from pathlib import Path

from process_utils import kill_process_tree
from workspace_context import current_workspace, use_workspace

_bg_counter=0
background_tasks:dict[str,dict]={}
background_results:dict[str,str]={}
# collect 之后结果从活跃表移除，但保留有限历史供 background_output 取全文；
# 否则模型只看到 200 字符通知，完整输出永远拿不回来。
background_finished:dict[str,dict]={}
background_procs:dict[str,object]={}
MAX_FINISHED=50
background_lock=threading.Lock() # 保护 background_tasks 和 background_results 访问
background_cond=threading.Condition(background_lock)
current_background_id:ContextVar[str|None]=ContextVar(
    'current_background_id', default=None)

def is_slow_operation(tool_name:str,tool_input:dict)->bool:
    if tool_name!='bash':
        return False
    cmd=tool_input.get('command','').lower()
    slow_keywords=["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(kw in cmd for kw in slow_keywords) # 检查命令是否包含慢操作关键词，判断是否为慢操作。

# 检查是否应该在后台运行任务
def should_run_background(tool_name:str,tool_input:dict)->bool:

    if tool_input.get('run_in_background'):
        return True
    if 'run_in_background' in tool_input:
        return False   # 显式 false 覆盖关键词启发式
    return is_slow_operation(tool_name,tool_input)

def execute_tool(tool_name:str,tool_input:dict,registry:dict)->str: #执行工具，返回结果
    handler=registry.get(tool_name)
    if handler:
        return handler(**tool_input)
    return f'Unknown tool: {tool_name}'

def register_process(proc) -> None:
    """run_bash spawn 后登记进程句柄；只有后台 worker 上下文才有 bg_id。"""
    bg_id=current_background_id.get()
    if not bg_id:
        return
    with background_lock:
        background_procs[bg_id]=proc

def unregister_process(proc) -> None:
    with background_lock:
        for bg_id, held in list(background_procs.items()):
            if held is proc:
                background_procs.pop(bg_id, None)

def start_background_task(tool_call,registry:dict)->str: #启动后台任务，返回任务 ID
    global _bg_counter
    name=tool_call.function.name
    args=json.loads(tool_call.function.arguments)
    cmd=args.get('command','')

    with background_lock:
        _bg_counter+=1
        bg_id=f'bg_{_bg_counter:04d}'
        background_tasks[bg_id]={
            'tool_use_id':tool_call.id,
            'status':'running',
            'command':cmd,
            'created_at':time.time(),
            'ended_at':None,
            'cancel_requested':False,
        }

    workspace = current_workspace(Path.cwd())
    import hooks  # 延迟导入：hooks 模块级依赖 tools，反向按需取
    grants = hooks.take_grants()   # 凭据转移给 worker，父上下文不再保留副本
    # 会话 id 同样要显式带走：ContextVar 不跨线程继承，丢了 worker 的审批白名单
    # 就落到进程级键上（一个会话批过的 rm 在所有会话的后台任务里都免审）。
    session_id = hooks.current_session_id()

    def worker(): #后台任务线程函数
        token=current_background_id.set(bg_id)
        result=''
        status='failed'
        try:
            with background_lock:
                cancelled=background_tasks[bg_id].get('cancel_requested')
            if cancelled:
                result='Error: cancelled before start'
                status='cancelled'
            else:
                try:
                    # 带上工作区、会话与「本次调用已批准」的凭据：工具边界据此放行，
                    # 否则 hook 批过的命令会在 worker 里被再问一次（或超时拒绝）。
                    with use_workspace(workspace), hooks.use_grants(grants), \
                            hooks.use_session(session_id):
                        result=str(execute_tool(name,args,registry))
                    status='completed'
                except Exception as exc:
                    result=f'Error: background {name} failed: {exc}'
                    status='failed'
        finally:
            current_background_id.reset(token)
        with background_cond:
            task=background_tasks.get(bg_id)
            if task is not None:
                if task.get('cancel_requested'):
                    status='cancelled'
                task['status']=status
                task['ended_at']=time.time()
                background_results[bg_id]=result
                background_cond.notify_all()

    thread=threading.Thread(target=worker,daemon=True) # 启动后台任务线程
    thread.start()
    print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
    return bg_id


def _remember_finished(bg_id:str, task:dict, output:str) -> None:
    background_finished[bg_id]={
        'status':task['status'],
        'command':task.get('command',''),
        'ended_at':task.get('ended_at'),
        'result':str(output),
    }
    while len(background_finished) > MAX_FINISHED:
        background_finished.pop(next(iter(background_finished)))


def list_background_tasks()->list[dict]:
    """活跃与最近完成任务的快照（含 id），按开始/结束时间倒序，供 /watch 展示。"""
    with background_lock:
        active=[dict(task, id=bg_id) for bg_id,task in background_tasks.items()]
        finished=[dict(task, id=bg_id) for bg_id,task in background_finished.items()]
    return sorted(active+finished,
                  key=lambda task: task.get('created_at')
                  or task.get('ended_at') or 0,
                  reverse=True)


def background_snapshot(bg_id:str)->dict|None:
    with background_lock:
        task=background_tasks.get(bg_id)
        if task is not None:
            return dict(task)
        task=background_finished.get(bg_id)
        return dict(task) if task is not None else None


def wait_background(bg_id:str, timeout:float)->dict|None:
    """等待到终态或超时；返回快照，未知 id 返回 None。"""
    deadline=time.monotonic()+max(0.0, timeout)
    with background_cond:
        while True:
            task=background_tasks.get(bg_id)
            if task is None:
                finished=background_finished.get(bg_id)
                return dict(finished) if finished is not None else None
            if task['status'] not in ('running','queued'):
                return dict(task)
            remaining=deadline-time.monotonic()
            if remaining <= 0:
                return dict(task)
            background_cond.wait(remaining)


def read_background_result(bg_id:str)->tuple[dict|None,str|None]:
    """取终态任务；活跃任务原样返回，终态任务移入历史并返回全文。"""
    with background_lock:
        task=background_tasks.get(bg_id)
        if task is None:
            finished=background_finished.get(bg_id)
            if finished is None:
                return None, None
            return dict(finished), str(finished.get('result',''))
        if task['status'] in ('running','queued'):
            return dict(task), None
        task=background_tasks.pop(bg_id)
        output=background_results.pop(bg_id,'')
        _remember_finished(bg_id, task, output)
        return dict(task), str(output)


def kill_background(bg_id:str)->dict|None:
    """请求终止后台任务；返回快照，未知 id 返回 None。"""
    with background_lock:
        task=background_tasks.get(bg_id)
        if task is None:
            finished=background_finished.get(bg_id)
            return dict(finished) if finished is not None else None
        if task['status'] in ('completed','failed','cancelled'):
            return dict(task)
        task['cancel_requested']=True
        proc=background_procs.get(bg_id)
        snapshot=dict(task)
    if proc is not None:
        kill_process_tree(proc)
    return snapshot


def collect_background_results()->list[dict]: #收集后台任务结果，返回通知列表

    with background_lock: # 加锁保护 background_tasks 和 background_results 访问
        ready_ids=[bid for bid,task in background_tasks.items()
                   if task['status'] in ('completed','failed','cancelled')]

    notifications=[]
    for bg_id in ready_ids:
        with background_lock:
            task=background_tasks.pop(bg_id)
            output=background_results.pop(bg_id,'')
            _remember_finished(bg_id, task, output)
        output=str(output)
        summary=output[:200] if len(output)>200 else output
        notifications.append(
            f'<task_notification>\n'
            f' <task_id>{bg_id}</task_id>\n'
            f'<status>{task["status"]}</status>\n'
            f'<command>{task["command"]}</command>\n'
            f'<summary>{summary}</summary>\n'
            f'</task_notification>'
        )
        print(f"  \033[32m[background done] {bg_id}: "
              f"{task['command'][:40]} ({len(output)} chars)\033[0m")
    return notifications
