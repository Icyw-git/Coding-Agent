import json
import random
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

@dataclass
class CronJob:
    id:str
    cron:str
    prompt:str
    recurring:bool
    durable:bool

scheduled_jobs:dict[str,CronJob]={} # id -> job
cron_queue:list[CronJob]=[] #任务队列
cron_lock=threading.Lock() # 任务队列锁，用于保护任务队列
agent_lock=threading.Lock() # 代理锁，用于保护代理状态
_last_fired:dict[str,str]={} # id -> last fired time
DURABLE_PATH=Path(__file__).resolve().parent/'.cache'/'crons.json'

def _cron_field_matches(field:str,value:int)->bool: #判断cron表达式字段是否匹配当前时间
    if field=="*":
        return True
    if field.startswith("*/"):
        step=int(field[2:])
        return step>0 and value%step==0
    if ',' in field:
        return any(_cron_field_matches(f.strip(),value) for f in field.split(','))
    if '-' in field:
        lo,hi=field.split('-',1)
        return int(lo)<=value<=int(hi)
    return value==int(field)

def cron_matches(cron_expr:str,dt:datetime)->bool: #对外接口，判断cron表达式是否匹配当前时间
    fields=cron_expr.strip().split()
    if len(fields)!=5:
        return False
    minute,hour,dom,month,dow=fields
    dow_val=(dt.weekday()+1)%7

    m=_cron_field_matches(minute,dt.minute)
    h=_cron_field_matches(hour,dt.hour)
    dom_ok=_cron_field_matches(dom,dt.day)
    month_ok=_cron_field_matches(month,dt.month)
    dow_ok=_cron_field_matches(dow,dow_val)

    if not (m and h and month_ok):
        return False
    
    dom_unconstrained=dom=="*"
    dow_unconstrained=dow=="*"
    if dom_unconstrained and dow_unconstrained:
        return True
    if dom_unconstrained:
        return dow_ok   # dom 通配 → 只看 dow
    if dow_unconstrained:
        return dom_ok   # dow 通配 → 只看 dom
    return dom_ok and dow_ok

def _validate_cron_field(field:str,lo:int,hi:int):
    if field=='*':
        return None
    if field.startswith("*/"):
        step_str=field[2:]
        if not step_str.isdigit():
            return f'Invalid step:{field}'
        step=int(step_str)
        if step<=0:
            return f'Step must be > 0:{field}'

        return None
    if ',' in field:
        for part in field.split(','):
            err=_validate_cron_field(part.strip(),lo,hi)
            if err:
                return err
        return None
    if '-' in field:
        parts=field.split('-',1)
        if not parts[0].isdigit() or not parts[1].isdigit():
            return f'Invalid range:{field}'
        a,b=int(parts[0]),int (parts[1])
        if a<lo or a>hi or b<lo or b>hi:
            return f'Range {field} out of bounds [{lo}-{hi}]'
        if a>b:
            return f'Range start > end:{field}'
        return None

    if not field.isdigit():
        return f'Invalid field:{field}'
    val=int(field)
    if val<lo or val>hi:
        return f'Value {val} out of bounds [{lo}-{hi}]'
    return None

def validate_cron(cron_expr:str): #验证cron表达式是否有效，返回错误信息或None
    fields=cron_expr.strip().split()
    if len(fields)!=5:
        return f'Expected 5 fields,got {len(fields)}'
    bounds=[(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names=["minute", "hour", "day-of-month", "month", "day-of-week"]
    for i,(field,(lo,hi),name) in enumerate(zip(fields,bounds,names)):
        err=_validate_cron_field(field,lo,hi)
        if err:
            return f'{name}:{err}'
    return None


def save_durable_jobs(): #保存持久化任务
    DURABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    durable=[asdict(j) for j in scheduled_jobs.values() if j.durable]
    DURABLE_PATH.write_text(json.dumps(durable,ensure_ascii=False,indent=2))

def load_durable_jobs(): #加载持久化任务，将有效任务添加到scheduled_jobs中
    if not DURABLE_PATH.exists():

        return 
    try:
        jobs=json.loads(DURABLE_PATH.read_text())
        for j in jobs:
            job=CronJob(**j)
            err=validate_cron(job.cron)
            if err:
                print(f"  \033[31m[cron] skipping invalid job {job.id}: {err}\033[0m")
                continue
            scheduled_jobs[job.id]=job
        valid=[j for j in jobs if j['id'] in scheduled_jobs]
        if valid:
            print(f"  \033[35m[cron] loaded {len(valid)} durable job(s)\033[0m")
    except Exception:
        pass

def schedule_job(cron:str,prompt:str,recurring:bool=True,durable:bool=True): #注册一个cron任务
    """
    Register a cron job with the scheduler.
    """
    
    err=validate_cron(cron)
    if err:
        return err
    job=CronJob(
        id=f'cron_{random.randint(0,999999):06d}',
        cron=cron,
        prompt=prompt,
        recurring=recurring,
        durable=durable,
    )

    with cron_lock:
        scheduled_jobs[job.id]=job
    if durable:
        save_durable_jobs()
    print(f"  \033[35m[cron register] {job.id} '{cron}' → {prompt[:40]}\033[0m")
    return job

def cancel_job(job_id:str)->str: #取消一个cron任务
    """
    Cancel a cron job with the scheduler.
    """
    with cron_lock:
        job=scheduled_jobs.pop(job_id,None)
    if not job:
        return f'Job {job_id} not found'
    if job.durable:
        save_durable_jobs()
    print(f"  \033[31m[cron cancel] {job_id}\033[0m")
    return f'Cancelled {job_id}'

def cron_scheduler_loop(): #cron任务调度循环，每秒检查一次是否有任务到点
    while True:
        time.sleep(1)
        now=datetime.now()
        minute_marker=now.strftime("%Y-%m-%d %H:%M")
        with cron_lock:
            for job in list(scheduled_jobs.values()):
                try:
                    if cron_matches(job.cron,now):
                        if _last_fired.get(job.id)!=minute_marker:
                            cron_queue.append(job)
                            _last_fired[job.id]=minute_marker
                            print(f"  \033[35m[cron fire] {job.id} → "
                                  f"{job.prompt[:40]}\033[0m")
                        if not job.recurring:
                            scheduled_jobs.pop(job.id,None)
                            if job.durable:
                                save_durable_jobs()
                except Exception as e:
                    print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")

def consume_cron_queue()->list[CronJob]: #消费cron队列中的任务，返回已触发的任务列表，
    """
    Consume cron jobs from the queue.
    """
    with cron_lock:
        fired=list(cron_queue)
        cron_queue.clear()
    return fired

def _has_cron_queue()->bool: #检查cron队列是否为空
    with cron_lock:
        return bool(cron_queue)

def queue_processor_loop(run_turn, poll_interval: float = 0.2) -> None:
    """后台推送循环：cron 到点且有工作、且 agent 空闲（拿到 agent_lock）时，调用 run_turn 执行一轮。

    - run_turn：持锁状态下被调用，应消费 cron 队列并执行一轮对话（如 agent_loop）；
    - 非阻塞抢锁：agent 正在忙（主会话或上一轮 cron 任务）时跳过，绝不排队；
    - 双重检查：拿到锁后队列可能已被取走，避免跑空轮。
    """
    while True:
        time.sleep(poll_interval)
        if not _has_cron_queue():
            continue
        if not agent_lock.acquire(blocking=False):   # 尝试拿锁：agent 忙则跳过这轮
            continue
        try:
            if not _has_cron_queue():                # 双重检查：拿锁后队列可能已被取走
                continue
            run_turn()
        finally:
            agent_lock.release()


def wait_for_cron_idle(poll_interval: float = 0.2) -> None:
    """阻塞直到定时任务全部处理完且处理器空闲（无待触发任务、队列为空、锁可拿）。

    供 __main__ 在主会话后保持进程存活，让 queue_processor_loop 有机会推送 cron 任务；
    有 recurring 任务时会一直等待（进程常驻），Ctrl+C 由调用方处理。
    """
    while True:
        with cron_lock:
            pending = bool(scheduled_jobs) or bool(cron_queue)
        if not pending and agent_lock.acquire(blocking=False):
            agent_lock.release()
            return
        time.sleep(poll_interval)


