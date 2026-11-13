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

    def start_suggestions(self, argument: str = '') -> None:
        """`/suggest` 跑一轮；`--last` 看台账；`list/start/skip` 处理已有建议。

        手动触发不看档位：`HARNESS_PROACTIVE_MODE` 管的是"它自己会不会来"。
        """
        arg = str(argument or '').strip()
        if arg == '--last':
            self._show_observations()
            return
        if arg == 'list':
            self._list_suggestions()
            return
        if arg.startswith('start') or arg.startswith('skip'):
            self._act_on_suggestion(arg)
            return
        if self._busy:
            self._notice('等这一回合结束再要建议——建议本身也是一轮。')
            return
        request = self._suggestion_request()
        if request is None:
            return
        if not self._begin_turn(request, echo_text=False):
            return
        self._notice('只读建议回合：这次只给读工具，不会改动任何文件。')

    def _list_suggestions(self) -> None:
        """列出还没处理的建议（编号按最近排序，供 start/skip 用）。"""
        import harness
        import proactive_suggestions as store

        rows = store.open_items(harness.CACHE_DIR)
        if not rows:
            self._notice('没有待处理的建议。说「/suggest」要几条。')
            return
        lines = ['待处理的建议：']
        for index, row in enumerate(rows, 1):
            lines.append(f"  {index}. {row.get('proposed_action')}")
        lines.append('开始：/suggest start <编号>　不用了：/suggest skip <编号>')
        self._notice(chr(10).join(lines))

    def _act_on_suggestion(self, argument: str) -> None:
        """`start`/`skip`。**采纳才执行**，忽略只记账——这是同一条建议的两种结局。"""
        import harness
        import proactive_suggestions as store

        verb, _, raw = argument.partition(' ')
        number = raw.strip()
        rows = store.open_items(harness.CACHE_DIR)
        try:
            index = int(number)
        except (TypeError, ValueError):
            self._notice(f'用法：/suggest {verb} <编号>（先 /suggest list 看编号）')
            return
        if not 1 <= index <= len(rows):
            self._notice(f'没有编号 {index} 的建议（当前 {len(rows)} 条）。')
            return
        row = rows[index - 1]

        if verb == 'skip':
            result = store.dismiss(harness.CACHE_DIR, row['id'])
            if result['status'] == 'dismissed':
                self._notice('这条不再提醒（连续忽略会降低建议频率）。')
            elif result['status'] == 'expired':
                self._notice('这条已经过期了。')
            else:
                self._notice(f'没处理成功（{result["status"]}）。', level='error')
            return

        if self._busy:
            # busy 时**不**标成已采纳：建议还留在列表里，等这回合结束再点。
            self._notice('回合进行中，等结束了再开始这条建议。')
            return

        def accept(request_id):
            # 采纳 = 走正常受理：用建议文字当提示词跑一个普通回合（照旧审批）。
            # request_id 由建议 id 派生，所以重复点击天然幂等。
            if not self._begin_turn(TurnRequest(text=row['proposed_action']),
                                    echo_text=True):
                return {'status': 'busy'}
            return {'status': 'accepted', 'turn_id': ''}

        result = store.accept(harness.CACHE_DIR, row['id'], accept=accept)
        if result['status'] == 'accepted':
            self._notice('开始执行这条建议（该问的照旧会问）。')
        elif result['status'] == 'busy':
            self._notice('回合进行中，这条建议还留着。')
        else:
            self._notice(f'没受理成功（{result["status"]}）。', level='error')

    def _show_observations(self) -> None:
        """把 observe 档最近的记录打出来。

        observe 的意义是"先看几天它本来会说什么"，所以这些记录必须能看到——
        藏在 .cache 的 jsonl 里等于没有。
        """
        import harness
        import proactive

        rows = proactive.recent_observations(harness.CACHE_DIR, limit=3)
        if not rows:
            self._notice('还没有观察记录。开启方式：HARNESS_PROACTIVE_MODE=observe'
                         + chr(10) + f'台账位置：{proactive.observation_path(harness.CACHE_DIR)}')
            return
        lines = []
        for row in rows:
            lines.append(f"{row.get('at') or '?'} · {len(row.get('suggestions') or [])} 条")
            for item in row.get('suggestions') or []:
                lines.append(f'  · {item}')
        self._notice(chr(10).join(lines) + chr(10)
                     + f"（台账：{proactive.observation_path(harness.CACHE_DIR)}）")

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
            suggestions = proactive.parse_suggestions(
                answer or '', limit=max(1, count))
            if silent:
                proactive.record_observation(
                    harness.CACHE_DIR, suggestions,
                    workspace=harness.get_workdir(),
                    limit=proactive.max_suggestions())
            else:
                # 手动/自动档：把建议**落成实体**，这样它才有 id 可以被采纳或忽略。
                # 原来这里只打了一段文字，`record_accepted/ignored` 因此永远没人调用。
                import proactive_suggestions as store

                store.record(harness.CACHE_DIR, suggestions,
                             workspace=str(harness.get_workdir()),
                             evidence=('recent sessions',))
        except Exception:
            pass        # 记账失败不该影响已经给出的建议
