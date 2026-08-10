import os
import random
import time

ESCALATED_MAX_TOKENS=64000
DEFAULT_MAX_TOKENS=8000
MAX_RECOVERY_RETRIES=3
MAX_RETRIES=10
BASE_DELAY_MS=500
MAX_CONSECUTIVE_529=3
FALLBACK_MODEL=os.getenv('FALLBACK_MODEL_ID')
CONTINUATION_PROMPT=(
    "Output token limit hit. Resume directly — "
    "no apology, no recap. Pick up mid-thought."
)

class RecoveryState:
    def __init__(self):
        self.has_escalated=False # 是否升级到升级模式
        self.recovery_count=0 # 恢复次数
        self.consecutive_529=0 # 连续529错误次数
        self.has_attempted_reactive_compact=False # 是否尝试过反应式紧凑模型
        self.current_model=os.getenv('LLM_MODEL_ID') # 当前模型
        
def retry_delay(attempt,retry_after=None): # 计算重试延迟时间，计算方式为2^attempt * BASE_DELAY_MS + random jitter
    if retry_after:
        return retry_after
    base=min(BASE_DELAY_MS*(2**attempt),32000)/1000
    jitter=random.uniform(0,base*0.25)
    return base+jitter

def with_retry(fn,state:RecoveryState):
    for attempt in range(MAX_RETRIES):
        try:
            result=fn()
            state.consecutive_529=0
            return result
        except Exception as e:
            name=type(e).__name__
            msg=str(e).lower()

            if 'overloaded' in name.lower() or '429' in msg: #529错误，需要切换模型
                state.consecutive_529+=1
                if state.consecutive_529>=MAX_CONSECUTIVE_529:
                    if FALLBACK_MODEL:
                        state.current_model=FALLBACK_MODEL # 切换到FALLBACK_MODEL

                        state.consecutive_529=0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" switching to {FALLBACK_MODEL}\033[0m")
                    else:
                        state.consecutive_529=0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" no FALLBACK_MODEL_ID configured, continuing retry\033[0m")
                delay=retry_delay(attempt)
                print(f"  \033[33m[529 overloaded] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay) # 等待重试延迟时间
                continue
                
            if 'ratelimit' in name.lower() or '429' in msg: #429错误，需要重试
                delay=retry_delay(attempt)
                print(f"  \033[33m[429 rate limit] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            raise
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded. {e}") # 最大重试次数超过，抛出异常

def is_prompt_too_long_error(e:Exception)->bool: # 判断是否是提示过长错误
    msg=str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "prompt_is_too_long" in msg
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)


def reactive_compact(message:list)->list: # 反应式紧凑模型
    if not message:
        return []
    tail=message[-5:]
    return [{'role':'user','content':"[Reactive compact] Earlier conversation trimmed. "
                        "Continue from where you left off."}, *tail]
    
    