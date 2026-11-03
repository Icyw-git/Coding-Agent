"""桌面工具清单只能有一份。

同一个六元组原本在三个文件里各写一遍：

    exec_policy.DESKTOP_TOOLS      （策略：每次都 ASK）
    approval_view:635              （审批卡渲染）
    policy_bash:589                （不给「同类免审」的授权键）

`policy_bash` 那儿还留着一句「清单从 exec_policy 同步」——靠人同步。加第七个桌面
工具时漏掉任一处：审批卡会退化成原始参数（`_desktop_detail` 不认它），或者它拿到
一个可复用的授权键。前者是泄露面，后者是后门。
"""

from pathlib import Path

import exec_policy
import policy_bash
from approval_view import detail as _  # noqa: F401  （确认可导入，无循环）


def test_all_three_lists_are_the_same_object():
    assert exec_policy.DESKTOP_TOOLS is policy_bash.DESKTOP_TOOLS
    assert set(exec_policy.DESKTOP_TOOLS) == {
        'desktop_snapshot', 'desktop_inspect', 'desktop_wait',
        'desktop_displays', 'desktop_windows', 'desktop_action'}


def test_approval_view_uses_the_shared_list():
    """审批卡不能再自己写一份：漏了就等于把 arg 原样铺出来。"""
    import approval_view

    source = Path(approval_view.__file__).read_text(encoding='utf-8')
    # 单个工具名有用处（标签表、setvalue 特例）；禁的是**成串的清单**。
    for line in source.splitlines():
        joined = line.replace(' ', '')
        assert not ('desktop_inspect' in joined and 'desktop_wait' in joined), (
            f'approval_view 又抄了一份桌面工具清单：{line.strip()}')
