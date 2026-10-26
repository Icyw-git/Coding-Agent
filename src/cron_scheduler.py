import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime

import paths


@dataclass
class CronJob:
    id:str
    cron:str
    prompt:str
    recurring:bool
    durable:bool
    # 本分钟标记（"YYYY-MM-DD HH:MM"）。随 durable job 一起落盘：进程在同一分钟内
    # 重启不会把同一个 occurrence 再触发一次。
    last_fired:str=''
    # 默认 False = 定时回合只读：写文件、联网、第三方工具、子代理一律拒绝。
    # 创建时显式开启才允许副作用，届时仍走正常审批（远程队列，不读 stdin）。
    allow_side_effects:bool=False
    # 结果推送到哪个聊天（飞书 chat_id）；空 = 只在会话日志里，不推送。
    deliver_to:str=''
    # 空闲门控：>0 时要求机器已空闲这么多秒（锁屏也算）才触发。到点不空闲就
    # 延后，等真正空闲再补跑一次；0 = 不门控，到点即跑。
    idle_after_s:int=0
    # 被延后的那次 occurrence 从何时开始等待（epoch 秒）。随 durable 记录落盘，
    # 重启后仍能在上限内补跑；触发或超时后清空。
    deferred_since:float|None=None

scheduled_jobs:dict[str,CronJob]={} # id -> job
cron_queue:list[CronJob]=[] #任务队列
cron_lock=threading.RLock() # 任务队列锁，用于保护任务队列
agent_lock=threading.RLock() # 代理锁；允许同一线程在 REPL 入口与降级路径重复获取
_last_fired:dict[str,str]={} # 兼容旧调用：id -> last fired（真实值在 job.last_fired）
DURABLE_PATH=paths.cache_path('crons.json')

# 空闲门控：到了预定时间但用户还在用机器时，延后到空闲再跑。等待超过上限就
# 丢弃这次 occurrence——不能第二天早上再执行昨晚的“夜間任务”。
IDLE_DEFER_MAX=12*3600.0
MAX_IDLE_AFTER_S=24*3600
# 探测要起 PowerShell（见 idle.py），不能每秒一次：30 秒内复用同一份快照。
IDLE_PROBE_INTERVAL=30.0
_idle_cache:tuple[float,dict]|None=None

def _cron_field_matches(field:str,value:int)->bool: #判断cron表达式字段是否匹配当前时间，支持通配符、范围、步长
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
    # 与常见 crontab 一致：当日期和星期都指定时，任意一个匹配即可触发。
    return dom_ok or dow_ok

def _validate_cron_field(field:str,lo:int,hi:int): #验证cron表达式字段是否有效，返回错误信息或None
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

def validate_cron(cron_expr:str): #验证cron表达式是否有效，返回错误信息或None，对外接口
    fields=cron_expr.strip().split()
    if len(fields)!=5:
        return f'Expected 5 fields,got {len(fields)}'
    bounds=[(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names=["minute", "hour", "day-of-month", "month", "day-of-week"]
    for field,(lo,hi),name in zip(fields,bounds,names,strict=True):
        err=_validate_cron_field(field,lo,hi)
        if err:
            return f'{name}:{err}'
    return None


def save_durable_jobs(): #保存持久化任务
    with cron_lock:
        DURABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
        durable=[asdict(j) for j in scheduled_jobs.values() if j.durable]
        tmp=DURABLE_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(durable,ensure_ascii=False,indent=2),encoding='utf-8')
        tmp.replace(DURABLE_PATH)

def _read_durable_text(path) -> str: #读取持久化文件；编码固定 UTF-8
    """持久化文件一律按 UTF-8 读。

    直接用 ``Path.read_text()`` 会跟着系统 locale 走（Windows 上是 GBK），中文提示词
    的 UTF-8 字节会解码失败，结果是**整批 durable 任务在重启后消失**。旧版本可能
    按本地编码写过，所以回退一次 locale 解码，再不行才放弃并明确报错。
    """
    raw=path.read_bytes()
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        pass
    import locale
    fallback=locale.getpreferredencoding(False) or 'utf-8'
    try:
        return raw.decode(fallback)
    except UnicodeDecodeError as exc:
        raise ValueError(f'cannot decode {path} as UTF-8 or {fallback}') from exc

def load_durable_jobs(): #加载持久化任务，将有效任务添加到scheduled_jobs中
    if not DURABLE_PATH.exists():

        return
    try:
        jobs=json.loads(_read_durable_text(DURABLE_PATH))
    except Exception as exc:
        print(f"  \033[31m[cron] failed to load durable jobs: {exc}\033[0m")
        return
    # 单条记录坏了只跳过它，不能让一个坏任务清空整张表。
    for j in jobs:
        try:
            job=CronJob(**j)
        except (TypeError, ValueError) as exc:
            print(f"  \033[31m[cron] skipping malformed job record: {exc}\033[0m")
            continue
        err=validate_cron(job.cron)
        if err:
            print(f"  \033[31m[cron] skipping invalid job {job.id}: {err}\033[0m")
            continue
        scheduled_jobs[job.id]=job
    if scheduled_jobs:
        print(f"  \033[35m[cron] loaded {len(scheduled_jobs)} durable job(s)\033[0m")

def schedule_job(cron:str,prompt:str,recurring:bool=True,durable:bool=True,
                 job_id:str|None=None,allow_side_effects:bool=False,
                 deliver_to:str='',idle_after_s:int=0): #注册一个cron任务
    """
    Register a cron job with the scheduler.

    job_id 可指定稳定 ID（如 heartbeat）：已存在时返回错误字符串，由调用方转成 409。
    allow_side_effects 默认 False：定时回合按只读策略执行，写操作/联网/第三方工具
    会被拒绝；只有在创建时显式开启的任务里才走正常审批。
    空 deliver_to = 不推送结果；飞书创建的默认目标是当前聊天。
    idle_after_s > 0 时任务只在机器空闲足够久（或锁屏）后触发，到点不空闲就延后。
    """

    err=validate_cron(cron)
    if err:
        return err
    try:
        idle_after_s=int(idle_after_s or 0)
    except (TypeError,ValueError):
        return 'idle_after_s must be an integer'
    if idle_after_s<0 or idle_after_s>MAX_IDLE_AFTER_S:
        return f'idle_after_s must be between 0 and {MAX_IDLE_AFTER_S}'
    with cron_lock: #确保写入scheduled_jobs时线程安全，避免重复注册
        if job_id and job_id in scheduled_jobs:
            return f'Job {job_id} already exists'
        job_id = job_id or f'cron_{uuid.uuid4().hex[:12]}'
        job=CronJob(
            id=job_id,
            cron=cron,
            prompt=prompt,
            recurring=recurring, # 是否重复执行
            durable=durable, # 是否持久化存储
            allow_side_effects=bool(allow_side_effects),
            deliver_to=(deliver_to or '').strip(),
            idle_after_s=idle_after_s,
        )
        scheduled_jobs[job.id]=job
    if durable: # 如果任务持久化存储，保存持久化存储
        save_durable_jobs()
    print(f"  \033[35m[cron register] {job.id} '{cron}' → {prompt[:40]}\033[0m")
    return job

def cancel_job(job_id:str)->str: #取消一个cron任务
    """
    Cancel a cron job with the scheduler.
    """
    with cron_lock: #确保取消scheduled_jobs时线程安全，避免重复取消
        job=scheduled_jobs.pop(job_id,None)
    if not job:
        return f'Job {job_id} not found'
    if job.durable: # 如果任务持久化存储，保存持久化存储
        save_durable_jobs()
    print(f"  \033[31m[cron cancel] {job_id}\033[0m")
    return f'Cancelled {job_id}'

def update_job(job_id:str,*,allow_side_effects:bool|None=None,
               deliver_to:str|None=None,idle_after_s:int|None=None): #调整已有任务的授权/投递目标
    """
    修改任务的副作用授权、投递目标或空闲门控；返回更新后的任务，未知任务返回错误字符串。

    授权只能由人通过按钮/命令/API 显式给出：模型侧的 schedule_cron 工具不暴露
    这个开关，避免模型给自己授权。
    """
    if idle_after_s is not None:
        try:
            idle_after_s=int(idle_after_s)
        except (TypeError,ValueError):
            return 'idle_after_s must be an integer'
        if idle_after_s<0 or idle_after_s>MAX_IDLE_AFTER_S:
            return f'idle_after_s must be between 0 and {MAX_IDLE_AFTER_S}'
    with cron_lock:
        job=scheduled_jobs.get(job_id)
        if job is None:
            return f'Job {job_id} not found'
        if allow_side_effects is not None:
            job.allow_side_effects=bool(allow_side_effects)
        if deliver_to is not None:
            job.deliver_to=(deliver_to or '').strip()
        if idle_after_s is not None:
            job.idle_after_s=idle_after_s
            # 改门控等于作废旧 occurrence：不清掉会拿旧阈值立刻补跑
            job.deferred_since=None
        durable=job.durable
    if durable:
        save_durable_jobs()
    mode='可写' if job.allow_side_effects else '只读'
    print(f"  [35m[cron update] {job.id} → {mode}[0m")
    return job

def _probe_idle(probe=None) -> dict:
    """读一次空闲快照；探测异常收敛成 available=False，不让调度线程死掉。"""
    if probe is None:
        import idle
        probe = idle.state
    try:
        snapshot = probe() or {}
    except Exception as exc:
        return {'available': False, 'error': f'{type(exc).__name__}: {exc}'}
    return snapshot if isinstance(snapshot, dict) else {'available': False}


def idle_snapshot(probe=None) -> dict:
    """带缓存地读空闲状态：生产路径 30 秒内只探测一次。

    显式传 probe（测试/诊断）时绕过缓存，避免用例之间互相污染。
    """
    global _idle_cache
    if probe is not None:
        return _probe_idle(probe)
    now = time.monotonic()
    if _idle_cache is not None and now - _idle_cache[0] < IDLE_PROBE_INTERVAL:
        return _idle_cache[1]
    snapshot = _probe_idle()
    _idle_cache = (now, snapshot)
    return snapshot


def reset_idle_cache() -> None:
    global _idle_cache
    _idle_cache = None


def idle_ready(job:CronJob,snapshot:dict|None) -> bool:
    """门控是否放行：锁屏视为离开；探测不可用一律按“不空闲”处理。"""
    threshold=int(getattr(job,'idle_after_s',0) or 0)
    if threshold<=0:
        return True
    if not snapshot:
        return False
    if snapshot.get('locked'):
        return True
    if not snapshot.get('available',True):
        return False
    try:
        seconds=float(snapshot.get('idle_seconds') or 0)
    except (TypeError,ValueError):
        return False
    return seconds>=threshold


def _has_due_idle_job(now:datetime) -> bool:
    """是否有门控任务“现在就需要”空闲快照：已延后，或本分钟刚匹配。"""
    with cron_lock:
        for job in scheduled_jobs.values():
            if int(getattr(job,'idle_after_s',0) or 0)<=0:
                continue
            if job.deferred_since is not None or cron_matches(job.cron, now):
                return True
    return False


def tick(now: datetime | None = None, idle_probe=None) -> list[CronJob]: #扫描一次；返回本次新入队的任务
    """按当前时间扫描所有任务：到点且本分钟未触发 → 入队。

    触发标记写在任务本身上并随 durable job 落盘，因此 restart 不会在一分钟内重复触发。

    带 idle_after_s 的任务到点但机器不空闲时先记 deferred_since、不入队；此后
    即使 cron 已不再匹配，也会在空闲时补跑一次。等待超过 IDLE_DEFER_MAX 就丢弃
    这次 occurrence（一次性任务直接移除），避免执行隔夜任务。
    """
    now = now or datetime.now()
    minute_marker = now.strftime("%Y-%m-%d %H:%M")
    now_ts = now.timestamp()
    snapshot = idle_snapshot(idle_probe) if _has_due_idle_job(now) else None
    fired: list[CronJob] = []
    deferred: list[CronJob] = []
    dropped: list[CronJob] = []
    with cron_lock: #确保遍历scheduled_jobs时线程安全，避免并发修改
        for job in list(scheduled_jobs.values()):
            try:
                gated = int(getattr(job, 'idle_after_s', 0) or 0) > 0
                if gated and job.deferred_since is not None:
                    if now_ts - job.deferred_since > IDLE_DEFER_MAX:
                        job.deferred_since = None
                        dropped.append(job)
                        if not job.recurring:
                            scheduled_jobs.pop(job.id, None)
                        continue
                    if job.last_fired == minute_marker:
                        continue
                    if not idle_ready(job, snapshot):
                        continue
                else:
                    if not cron_matches(job.cron, now):
                        continue
                    if job.last_fired == minute_marker:
                        continue
                    if gated and not idle_ready(job, snapshot):
                        job.deferred_since = now_ts
                        deferred.append(job)
                        continue
                cron_queue.append(job)
                job.last_fired = minute_marker
                job.deferred_since = None
                _last_fired[job.id] = minute_marker
                fired.append(job)
                print(f"  [35m[cron fire] {job.id} → "
                      f"{job.prompt[:40]}[0m")
                if not job.recurring: # 如果任务不重复执行
                    scheduled_jobs.pop(job.id, None)
            except Exception as e:
                print(f"  [31m[cron error] {job.id}: {e}[0m")
        if any(job.durable for job in fired + deferred + dropped):
            save_durable_jobs()
    for job in deferred:
        minutes = max(1, int(getattr(job, 'idle_after_s', 0) or 0) // 60)
        print(f"  \033[35m[cron defer] {job.id}: 等待空闲 ≥{minutes} 分钟\033[0m")
    for job in dropped:
        print(f"  \033[31m[cron skip] {job.id}: 等待空闲超过 "
              f"{IDLE_DEFER_MAX / 3600:g} 小时，丢弃本次\033[0m")
    return fired


def cron_scheduler_loop(stop_event=None): #cron任务调度循环，每秒检查一次是否有任务到点
    stop_event = stop_event or threading.Event()
    while not stop_event.wait(1.0): # 每秒检查一次；停止信号可立即打断等待
        tick()


def consume_cron_queue(predicate=None)->list[CronJob]: #消费cron队列中的任务，返回已触发的任务列表，
    """取走队列里的任务；predicate 为空时全部取走。

    带 predicate 时只取匹配的任务，其余留在队列里（不丢弃）：交互回合执行期间
    只应消费「心跳」与显式允许副作用的任务，只读任务留给专用 cron 回合，避免
    借当前会话的写权限执行。
    """
    with cron_lock:
        if predicate is None:
            fired=list(cron_queue) # 复制队列，避免并发修改
            cron_queue.clear() # 清空队列
        else:
            fired=[job for job in cron_queue if predicate(job)]
            if fired:
                cron_queue[:]=[job for job in cron_queue if not predicate(job)]
    return fired

def _has_cron_queue()->bool: #检查cron队列是否为空
    with cron_lock:
        return bool(cron_queue)

def queue_processor_loop(run_turn, poll_interval: float = 0.2, stop_event=None) -> None:
    """后台推送循环：cron 到点且有工作、且 agent 空闲（拿到 agent_lock）时，调用 run_turn 执行一轮。

    - run_turn：持锁状态下被调用，应消费 cron 队列并执行一轮对话（如 agent_loop）；
    - 非阻塞抢锁：agent 正在忙（主会话或上一轮 cron 任务）时跳过，绝不排队；
    - 双重检查：拿到锁后队列可能已被取走，避免跑空轮。
    - stop_event：置位后循环在当前 poll 结束前退出（lifespan 关闭时需要）。
    """
    stop_event = stop_event or threading.Event()
    while not stop_event.wait(poll_interval): # 每 poll_interval 秒检查一次是否有任务到点
        if not _has_cron_queue(): # 如果队列为空
            continue
        if not agent_lock.acquire(blocking=False):   # 尝试拿锁：agent 忙则跳过这轮，例如主会话中任务正在执行
            continue
        try:
            if not _has_cron_queue():                # 双重检查：拿锁后队列可能已被取走，例如上一轮 cron 任务已执行
                continue
            try:
                run_turn()
            except Exception as exc:
                # 兜底：run_turn 的实现（agent_loop）可能抛出模型/网络错误。
                # 这里吞掉并继续下一轮，不能让 cron-worker 线程死掉——线程一死，
                # 之后所有定时任务都会静默失效。
                print(f"  \033[31m[cron] turn failed: "
                      f"{type(exc).__name__}: {exc}\033[0m")
        finally:
            agent_lock.release() # 释放锁，允许其他任务执行


def wait_for_cron_idle(poll_interval: float = 0.2) -> None:
    """阻塞直到定时任务全部处理完且处理器空闲（无待触发任务、队列为空、锁可拿）。

    供 __main__ 在主会话后保持进程存活，让 queue_processor_loop 有机会推送 cron 任务；
    有 recurring 任务时会一直等待（进程常驻），Ctrl+C 由调用方处理。
    """
    while True:
        with cron_lock:
            pending = bool(scheduled_jobs) or bool(cron_queue)
        if not pending and agent_lock.acquire(blocking=False):
            agent_lock.release() # 释放锁，允许其他任务执行
            return
        time.sleep(poll_interval)


