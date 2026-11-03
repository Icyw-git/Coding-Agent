"""Windows Job Object 的 ctypes 结构体只能有一份定义。

`limits.py` 与 `sandbox.py` 各定义了一遍 `_IO_COUNTERS`、BasicLimit、ExtendedLimit，
字段顺序与类型逐字相同，只是类名不同（`_BASIC_LIMIT` vs
`_JOBOBJECT_BASIC_LIMIT_INFORMATION`）。

这类重复最危险：改动一份（补字段、改类型、改 ctypes 宽度）另一份不会跟着动，
而 `SetInformationJobObject` 只吃结构体的**字节布局**——布局错了不会报错，
只会把内存上限、CPU 时间等限制值当成别的东西生效。
"""

import os

import pytest

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Job Object 是 Windows API')


def test_extended_limit_is_one_object():
    import limits
    import sandbox

    assert (sandbox._JOBOBJECT_EXTENDED_LIMIT_INFORMATION
            is limits._EXTENDED_LIMIT)


def test_struct_layout_matches_winnt_order():
    """字段顺序是 ABI，不是风格问题。锁定它，顺便说明白为什么不能「各写一份」。"""
    import limits

    assert [name for name, _ in limits._BASIC_LIMIT._fields_] == [
        'PerProcessUserTimeLimit', 'PerJobUserTimeLimit', 'LimitFlags',
        'MinimumWorkingSetSize', 'MaximumWorkingSetSize', 'ActiveProcessLimit',
        'Affinity', 'PriorityClass', 'SchedulingClass']
    assert [name for name, _ in limits._EXTENDED_LIMIT._fields_] == [
        'BasicLimitInformation', 'IoInfo', 'ProcessMemoryLimit', 'JobMemoryLimit',
        'PeakProcessMemoryUsed', 'PeakJobMemoryUsed']
