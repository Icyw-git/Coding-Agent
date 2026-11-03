"""工具失败判定只能有一处实现。

同一个判断（「这个工具输出算失败吗」）原本抄了三份，其中两份规则不同：

    tool_activity.is_failure      lstrip() + ('Error', 'Permission denied')
    tool_dispatch:150             lstrip() + ('Error', 'Permission denied')
    tool_dispatch:173             startswith + ('Error:', 'Permission denied')
    ui/background_actions:51      lstrip() + ('Error', 'Permission denied')

三份一致的输入结论一致，所以真实数据里看不出来；但规则一旦分叉，同一次工具调用
可能在 ``tool_end`` 里算失败、在 ``tool.completed`` 里算成功。
"""

from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / 'src'
_CANONICAL = 'is_failure'


def test_is_failure_is_the_only_implementation():
    """别的模块要这个判断，就 import `tool_activity.is_failure`，别再抄。"""
    offenders = []
    for path in _SRC.rglob('*.py'):
        if path.name == 'tool_activity.py':
            continue
        text = path.read_text(encoding='utf-8')
        for number, line in enumerate(text.splitlines(), 1):
            if 'Permission denied' not in line:
                continue
            # 产生这条消息的地方（hooks / background_task）合法，判定才算违规
            if 'startswith' in line:
                offenders.append(f'{path.name}:{number}: {line.strip()}')
    assert not offenders, '失败判定被抄了：' + '; '.join(offenders)
