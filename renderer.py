"""渲染后端：BaseRenderer 接口 + AnsiRenderer（默认后端，普通 print）。

业务代码只 emit 事件，renderer 决定怎么渲染。默认 AnsiRenderer 与
无 UI 版本的显示效果一致（普通打印 + hooks 现有输出）；未来可加
TextualRenderer 而业务代码零改动。import 本模块即触发默认后端 attach。
"""
from ui_events import UIEvent, bus


class BaseRenderer:
    """渲染后端接口：订阅 bus，把 UIEvent 转成可见输出。"""

    def on_event(self, e: UIEvent) -> None:
        raise NotImplementedError


class AnsiRenderer(BaseRenderer):
    """默认渲染后端：普通 print，保持与无 UI 版本一致的显示效果。

    tool_start/tool_end 暂不渲染：工具输出的显示仍由 hooks.log_hook
    （直接 print）负责，后续 P0 收尾时把工具渲染也收敛进本后端。
    """

    def on_event(self, e: UIEvent) -> None:
        if e.type == 'assistant_text':
            print(e.payload.get('text', ''))
        elif e.type == 'debug':
            print(e.payload.get('msg', ''))
        # status / tool_* / session_* 当前无需渲染（状态由 hooks 与日志承担）


# 默认 attach：import renderer 即生效；业务代码无需感知渲染后端
bus.subscribe(AnsiRenderer().on_event)
