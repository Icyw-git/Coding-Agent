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

    def _suggestion_request(self, *, silent: bool = False) -> TurnRequest | None:
        """组一个只读建议回合；素材收集失败时返回 None（并提示）。"""
        import harness
        import proactive

        try:
            material = proactive.scan_sources(
                workdir=harness.get_workdir(),
                sessions_dir=harness.SESSIONS_DIR)
            text = proactive.prompt(material, limit=proactive.max_suggestions())
            tools = tuple(proactive.read_only_schemas())
        except Exception as exc:
            self._notice(f'要建议失败：{type(exc).__name__}: {exc}', level='error')
            return None
        return TurnRequest(text=text, tools=tools, proactive=True, silent=silent)

    def start_suggestions(self) -> None:
        # 手动触发不看档位：`HARNESS_PROACTIVE_MODE` 管的是"它自己会不会来"。
        if self._busy:
            self._notice('等这一回合结束再要建议——建议本身也是一轮。')
            return
        request = self._suggestion_request()
        if request is None:
            return
        if not self._begin_turn(request, echo_text=False):
            return
        self._notice('只读建议回合：这次只给读工具，不会改动任何文件。')

    def maybe_auto_suggest(self) -> None:
        """启动时按档位决定要不要自己来一次（默认 off，什么都不做）。

        - ``observe``：跑一个**静默回合**，建议只写观察台账——事后能看见它本来
          会说什么，但不打扰人。
        - ``auto``：走 `/suggest` 的同一条路径，建议直接摆出来。

        频率由 ``proactive.should_run`` 决定（连续忽略会降频）。
        """
        import harness
        import proactive

        mode = proactive.mode()
        if mode == proactive.MODE_OFF:
            return
        due, _reason = proactive.should_run(harness.CACHE_DIR)
        if not due:
            return
        if mode == proactive.MODE_AUTO:
            self.start_suggestions()
            return
        request = self._suggestion_request(silent=True)
        if request is None:
            return
        if self._begin_turn(request, echo_text=False):
            self._notice('今天的建议已按 observe 档记录；输入 /suggest 可以直接看。')

    def record_proactive_run(self, answer, *, silent: bool = False) -> None:
        """建议回合结束后记账：节奏状态 + （observe 档）观察台账。

        手动跑同样算"今天建议过"——否则自动档会在同一天再跳出来一次。
        """
        import harness
        import proactive

        try:
            count = proactive.record_answer(harness.CACHE_DIR, answer)
            if silent:
                suggestions = proactive.parse_suggestions(
                    answer or '', limit=max(1, count))
                proactive.record_observation(
                    harness.CACHE_DIR, suggestions,
                    workspace=harness.get_workdir(),
                    limit=proactive.max_suggestions())
        except Exception:
            pass        # 记账失败不该影响已经给出的建议
