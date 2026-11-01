"""`/suggest`：一个只读回合，给出几条基于当前状态的建议。

为什么是"一个回合"而不是"一次模型调用"
--------------------------------------
建议要有依据（工作区里有什么、待办还剩几条、最近在忙什么）。让它能调只读工具，
比在提示里塞一堆上下文更准，也更省 token。

两条腿一起用才算只读
--------------------
`TurnRequest.tools` 让模型**看不到**写工具（不必先撞墙），`hooks.proactive_scope`
在钩子里**拒绝**白名单之外的工具（真拦截）。只做前者是提示，只做后者是浪费回合。
"""
from __future__ import annotations

from ui.widgets import TurnRequest


class ProactiveActionsMixin:
    """把 `/suggest` 变成一次只读回合；不拥有回合本身。"""

    def start_suggestions(self) -> None:
        import harness
        import proactive

        if self._busy:
            self._notice('等这一回合结束再要建议——建议本身也是一轮。')
            return
        try:
            material = proactive.scan_sources(
                workdir=harness.get_workdir(),
                sessions_dir=harness.SESSIONS_DIR)
            text = proactive.prompt(material, limit=proactive.max_suggestions())
            tools = tuple(proactive.read_only_schemas())
        except Exception as exc:
            self._notice(f'要建议失败：{type(exc).__name__}: {exc}', level='error')
            return
        request = TurnRequest(text=text, tools=tools, proactive=True)
        if not self._begin_turn(request, echo_text=False):
            return
        self._notice('只读建议回合：这次只给读工具，不会改动任何文件。')

    def record_proactive_run(self, answer) -> None:
        """建议回合结束后记一次运行：自动档的间隔判断靠它，手动跑同样算跑过。"""
        import harness
        import proactive

        try:
            proactive.record_answer(harness.CACHE_DIR, answer)
        except Exception:
            pass        # 记账失败不该影响已经给出的建议
