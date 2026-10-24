"""LLM 请求错误分类、重试与回退。

分类必须 provider-neutral：OpenAI-compatible 网关返回的错误形状各不相同
（异常类名、HTTP 状态、消息文本），按单一供应商的字符串匹配会把 429 误判成
过载、把 5xx/超时直接抛出。这里先归一成有限的几类，再决定重试/回退/上抛：

- context_length：不重试，交给主循环的响应式压缩；
- rate_limit / overloaded / server / timeout / network：退避重试；
- auth / invalid_request / unknown：立即上抛，重试只会浪费时间和配额。

回退目标来自 llm_config（旧 ``FALLBACK_MODEL_ID`` 会作为 main 的同 provider
回退）。跨 provider 回退必须显式开启（配置 ``fallback_cross_provider`` 或
``HARNESS_ALLOW_CROSS_PROVIDER_FALLBACK=1``）。流式响应一旦向用户交付过文本，
就不再做任何重试——重放会把同一段回答输出两遍。
"""
import os
import random
import time

import llm_config

ESCALATED_MAX_TOKENS=64000
DEFAULT_MAX_TOKENS=8000
MAX_RECOVERY_RETRIES=3
MAX_RETRIES=10
BASE_DELAY_MS=500
MAX_CONSECUTIVE_529=3
FALLBACK_ATTEMPTS=3
CONTINUATION_PROMPT=(
    "Output token limit hit. Resume directly — "
    "no apology, no recap. Pick up mid-thought."
)

RETRYABLE_KINDS=frozenset({'rate_limit','overloaded','server','timeout','network'})
ERROR_KINDS=('context_length','rate_limit','overloaded','auth','invalid_request',
             'timeout','network','server','unknown')

_RETRY_LABELS={
    'rate_limit':'429 rate limit',
    'overloaded':'529 overloaded',
    'server':'5xx server',
    'timeout':'timeout',
    'network':'network',
}


def _status_code(error: Exception):
    response=getattr(error,'response',None)
    return getattr(error,'status_code',None) or getattr(response,'status_code',None)


def is_prompt_too_long_error(e:Exception)->bool: # 判断是否是提示过长错误
    msg=str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "prompt_is_too_long" in msg
            or "context_length_exceeded" in msg
            or "maximum context length" in msg
            or "max_context_window" in msg)


def classify_error(e:Exception)->str:
    """把供应商各异的异常归一为固定类别；判断顺序即优先级。"""
    status=_status_code(e)
    name=type(e).__name__.lower()
    msg=str(e).lower()
    if is_prompt_too_long_error(e):
        return 'context_length'
    if status in (401,403) or 'authentication' in msg or 'invalid api key' in msg \
            or 'unauthorized' in msg:
        return 'auth'
    if status==429 or 'ratelimit' in name or 'rate limit' in msg \
            or 'too many requests' in msg:
        return 'rate_limit'
    if status==529 or 'overloaded' in name or 'overloaded' in msg:
        return 'overloaded'
    if status in (400,404,422) or 'invalid_request' in name or 'bad request' in msg:
        return 'invalid_request'
    if status==408 or 'timeout' in name or 'timed out' in msg or 'timeout' in msg:
        return 'timeout'
    if isinstance(status,int) and status>=500:
        return 'server'
    if ('connection' in name or 'apiconnectionerror' in name or 'connect' in msg
            or 'network' in msg or 'proxy' in msg or 'connection reset' in msg
            or 'remote end closed' in msg):
        return 'network'
    return 'unknown'


def _retry_after(e:Exception):
    """读取 Retry-After（秒），上限 60s；缺失/非数字返回 None。"""
    response=getattr(e,'response',None)
    headers=getattr(response,'headers',None)
    if not headers:
        return None
    try:
        value=headers.get('retry-after') or headers.get('Retry-After')
    except AttributeError:
        return None
    try:
        seconds=float(value)
    except (TypeError,ValueError):
        return None
    return min(max(seconds,0),60)


def retry_delay(attempt,retry_after=None): # 计算重试延迟时间，计算方式为2^attempt * BASE_DELAY_MS + random jitter
    if retry_after:
        return retry_after
    base=min(BASE_DELAY_MS*(2**attempt),32000)/1000
    jitter=random.uniform(0,base*0.25)
    return base+jitter


class RecoveryState:
    def __init__(self):
        self.has_escalated=False # 是否升级到升级模式
        self.recovery_count=0 # 恢复次数
        self.consecutive_529=0 # 连续529错误次数
        self.has_attempted_reactive_compact=False # 是否尝试过反应式紧凑模型
        self.stream_started=False # 本次尝试是否已向用户交付文本（拒绝重放）
        self.last_error=None
        self.current_target=None  # 当前 ModelTarget（回退可能换 provider）
        self.fallback_targets=[]  # 已按配置与跨 provider 开关过滤
        self.fallback_index=0
        self.fallback_notified=False
        self.current_model=os.getenv('LLM_MODEL_ID') # 当前模型（旧字段，兼容状态行）
        try:
            settings=llm_config.get_settings()
            target=settings.resolve_for('main')
            self.current_target=target
            self.current_model=target.model
            self.fallback_targets=settings.fallback_targets('main',target)
        except llm_config.ConfigError:
            pass


def switch_fallback(state:RecoveryState, reason:str='')->bool:
    """切到下一个回退目标；已耗尽返回 False。"""
    while state.fallback_index < len(state.fallback_targets):
        target=state.fallback_targets[state.fallback_index]
        state.fallback_index+=1
        if state.current_target is not None \
                and (target.provider,target.model)==(state.current_target.provider,
                                                     state.current_target.model):
            continue
        previous=state.current_model
        state.current_target=target
        state.current_model=target.model
        state.consecutive_529=0
        state.fallback_notified=False
        suffix=f' ({reason})' if reason else ''
        print(f"  \033[31m[fallback] {previous} -> {target.ref}{suffix}\033[0m")
        return True
    if state.fallback_targets and not state.fallback_notified:
        state.fallback_notified=True
        print("  \033[31m[fallback] all configured fallback models exhausted\033[0m")
    elif not state.fallback_targets and not state.fallback_notified and reason:
        state.fallback_notified=True
        print("  \033[31m[fallback] no fallback model configured, continuing retry\033[0m")
    return False


def with_retry(fn,state:RecoveryState):
    last_error=None
    attempts=0
    while attempts<MAX_RETRIES:
        try:
            result=fn()
            state.consecutive_529=0
            return result
        except Exception as e:
            # except 块结束时 Python 会删除 e，重试耗尽那条路只能靠这份引用取到原因
            last_error=e
            state.last_error=e
            kind=classify_error(e)
            if kind not in RETRYABLE_KINDS:
                raise
            if state.stream_started:
                raise RuntimeError(
                    '流式输出已开始，为避免重复内容不再重试：'
                    f'{type(e).__name__}: {e}') from e

            if kind=='overloaded':
                state.consecutive_529+=1
            else:
                state.consecutive_529=0

            if kind=='overloaded' and state.consecutive_529>=MAX_CONSECUTIVE_529:
                switch_fallback(state,f'{MAX_CONSECUTIVE_529} consecutive overloads')

            attempts+=1
            if attempts>=MAX_RETRIES:
                budget=min(FALLBACK_ATTEMPTS,MAX_RETRIES)
                if switch_fallback(state,'retries exhausted'):
                    attempts=max(0,MAX_RETRIES-budget)
                else:
                    break
            delay=retry_delay(attempts-1,_retry_after(e))
            label=_RETRY_LABELS.get(kind,kind)
            print(f"  \033[33m[{label}] retry {max(1, attempts)}/{MAX_RETRIES},"
                  f" wait {delay:.1f}s\033[0m")
            time.sleep(delay)
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded. {last_error}") # 最大重试次数超过，抛出异常
