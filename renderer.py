"""渲染后端：BaseRenderer 接口 + AnsiRenderer（默认后端，普通 print）。

业务代码只 emit 事件，renderer 决定怎么渲染。默认 AnsiRenderer 与
无 UI 版本的显示效果一致（普通打印 + hooks 现有输出）；未来可加
TextualRenderer 而业务代码零改动。import 本模块即触发默认后端 attach。
"""
import logging
import time

import cc_ui
from ui_events import UIEvent, bus

logger = logging.getLogger('harness.ui')


class BaseRenderer:
    """渲染后端接口：订阅 bus，把 UIEvent 转成可见输出。"""

    def on_event(self, e: UIEvent) -> None:
        raise NotImplementedError


class AnsiRenderer(BaseRenderer):
    """默认渲染后端：普通 print，保持与无 UI 版本一致的显示效果。

    所有可见业务状态均从事件总线渲染，避免 hooks 直接 print 与 UI 状态脱节。
    """

    def on_event(self, e: UIEvent) -> None:
        if e.type == 'user_message':
            cc_ui.safe_print('> ' + str(e.payload.get('text', '')))
        elif e.type == 'activity':
            logger.debug('[%s] %s', e.payload.get('state', 'ready'), e.payload.get('detail', ''))
        elif e.type == 'session_start':
            cc_ui.safe_print(cc_ui.status('Session started'))
        elif e.type == 'assistant_text':
            print(e.payload.get('text', ''))
        elif e.type == 'assistant_start':
            pass
        elif e.type == 'assistant_delta':
            # 真流式：增量打印（不换行），assistant_end 收尾换行
            print(e.payload.get('delta', ''), end='', flush=True)
        elif e.type == 'assistant_end':
            print('')
        elif e.type == 'debug':
            logger.debug(e.payload.get('msg', ''))
        elif e.type == 'tool_start':
            name = e.payload.get('name', 'tool')
            args = e.payload.get('args', {})
            cc_ui.safe_print(cc_ui.tool_line(name, args))
        elif e.type == 'tool_end':
            name = e.payload.get('name', 'tool')
            output = str(e.payload.get('output_preview', ''))
            duration = e.payload.get('duration_ms')
            outcome = e.payload.get('outcome', 'completed')
            ok = outcome in ('completed', 'warning')
            if ok:
                cc_ui.safe_print(cc_ui.bash_box(name, output))
            else:
                label = 'Permission denied' if outcome == 'denied' else 'Tool failed'
                cc_ui.say('error', '{0}: {1}'.format(label, name))
                cc_ui.safe_print(output)
            if duration is not None:
                cc_ui.safe_print(cc_ui.status('completed in {0:.1f}s'.format(float(duration) / 1000)))
        elif e.type == 'error':
            cc_ui.say('error', str(e.payload.get('msg', 'Unknown error')))
        elif e.type == 'diff':
            if e.payload.get('path'):
                cc_ui.safe_print(cc_ui.status(str(e.payload['path'])))
            cc_ui.safe_print(cc_ui.diff(e.payload.get('diff_text', '')))
        elif e.type == 'permission_request':
            cc_ui.safe_print(cc_ui.permission_banner(
                e.payload.get('title', 'Permission needed'),
                [e.payload.get('tool', ''), str(e.payload.get('args', {}))]))
        elif e.type == 'background_start':
            logger.info('background task started: %s', e.payload.get('task_id', ''))
        elif e.type == 'background_end':
            state = 'completed' if e.payload.get('ok') else 'failed'
            logger.info('background task %s: %s', state, e.payload.get('task_id', ''))
        elif e.type == 'status':
            # 思考中提示：单行覆盖（\r 回行首 + idle 清行），避免等待时无反馈
            if e.payload.get('phase') == 'thinking':
                print('  \033[90m[thinking] 模型思考中...\033[0m', end='', flush=True)
            elif e.payload.get('phase') == 'idle':
                print('\x1b[2K\r', end='', flush=True)
            elif 'tokens' in e.payload or 'cost' in e.payload:
                now = time.monotonic()
                last = getattr(self, '_last_metric_status', 0.0)
                if (now - last < 0.2 and
                        e.payload.get('cost_source') != 'confirmed' and
                        e.payload.get('ctx_source') != 'confirmed'):
                    return
                self._last_metric_status = now
                line = cc_ui.statusline(
                    model=e.payload.get('model', ''),
                    workdir=e.payload.get('workdir', ''),
                    tools=e.payload.get('tools', 0),
                    ctx_pct=e.payload.get('ctx_pct'),
                    ctx_source=e.payload.get('ctx_source', 'estimated'),
                    ctx_tokens=e.payload.get('ctx_tokens'),
                    ctx_window=e.payload.get('ctx_window'),
                    tokens=e.payload.get('tokens', ''),
                    prompt_tokens=e.payload.get('prompt_tokens'),
                    completion_tokens=e.payload.get('completion_tokens'),
                    cached_tokens=e.payload.get('cached_tokens'),
                    cache_creation_tokens=e.payload.get('cache_creation_tokens'),
                    reasoning_tokens=e.payload.get('reasoning_tokens'),
                    cost=e.payload.get('cost'),
                    cost_source=e.payload.get('cost_source', 'estimated'))
                if line:
                    cc_ui.safe_print(line)
        elif e.type == 'round_end':
            metric = e.payload
            logger.info('round=%s duration_ms=%s input=%s output=%s cost=%s compressed=%s',
                        metric.get('round', 0), metric.get('duration_ms', 0),
                        metric.get('prompt_tokens', 0), metric.get('completion_tokens', 0),
                        metric.get('cost', 0), bool(metric.get('compressed')))
        elif e.type == 'session_end':
            summary = e.payload.get('summary', {})
            outcomes = summary.get('tool_outcomes', {})
            failed = outcomes.get('failed', 0) + outcomes.get('denied', 0)
            source = summary.get('cost_source', 'estimated')
            prefix = '' if source == 'confirmed' else '~'
            cc_ui.safe_print(cc_ui.status(
                'Session complete: {0} rounds, {1} tools ({2} failed/denied), {3} tokens, {4}¥{5:.4f}'.format(
                    summary.get('rounds', 0), summary.get('tools', 0), failed,
                    summary.get('tokens', 0), prefix, float(summary.get('cost', 0)))))


class ReplRenderer(AnsiRenderer):
    """行式 REPL 专用后端：流式/工具/指标已由 REPL 自己打印，这里只补遗漏通道。

    REPL 有更细的进度行、工具折叠与回合末 tokens 行；同一批事件再走
    AnsiRenderer 会重复输出。统一到事件总线的方式是：订阅同一个 bus，
    但把这些通道让给 REPL，剩下的 error/diff/background 仍由渲染器负责。
    """

    _REPL_HANDLED = frozenset({
        'user_message', 'assistant_start', 'assistant_delta', 'assistant_end',
        'assistant_text', 'tool_start', 'tool_end', 'status', 'round_end',
        'session_start', 'session_end',
    })

    def on_event(self, e: UIEvent) -> None:
        if e.type in self._REPL_HANDLED:
            return
        super().on_event(e)


# 默认 attach：import renderer 即生效；业务代码无需感知渲染后端。
# Textual 模式切换时（ui_textual.run）会 unsubscribe default_renderer。
default_renderer = AnsiRenderer()
bus.subscribe(default_renderer.on_event)
