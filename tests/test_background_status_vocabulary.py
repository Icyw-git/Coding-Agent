"""后台任务的状态词表只能有一份。

`('completed','failed','cancelled')` 原本写了三遍：`background_task.py` 两遍（
`kill_background` 与 `collect_background_results`）+ `tool_exec.py` 一遍（`cancel`）。

这是**状态机词表**，不是普通常量：将来加一个终态（比如 `timeout`）漏掉任一处，
那个任务的结局就永远不会被 `collect_background_results` 收走——结果通知不来，
`/status` 里它一直显示在跑。
"""

import background_task
import tool_exec


def test_terminal_statuses_come_from_one_place():
    assert background_task.TERMINAL_STATUSES == ('completed', 'failed', 'cancelled')


def test_tool_exec_uses_the_shared_vocabulary():
    from pathlib import Path

    source = Path(tool_exec.__file__).read_text(encoding='utf-8')
    for line in source.splitlines():
        assert not (')' in line and "('completed', 'failed', 'cancelled')" in line), (
            f'tool_exec 又抄了一份终态表：{line.strip()}')
