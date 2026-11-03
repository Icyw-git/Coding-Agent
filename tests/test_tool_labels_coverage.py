"""每个下发给模型的工具都要有中文短名。

`approval_view.tool_name_zh()` 用在两处用户可见的地方：

    活动行   `· 执行命令  pytest -q  3.40s √`
    进度行   `⏳ 2.3s · 正在执行命令`

没有中文名时它回退到原名，于是这两处会出现 `· kill_background`、
`⏳ 正在kill_background`——「正在」直接接一个英文动词。

另外 `_RAW_TOOL_BY_LABEL` 是 `{中文名: 原名}` 的反查表（由 `_TOOL_LABELS` 反向生成）。
两个工具用同一个中文名，反查就会把其中一个还原成错的原名——所以标签还必须唯一。
"""

import collections

import approval_view
import tool_schemas


def _schema_tools() -> list[str]:
    return [item['function']['name'] for item in tool_schemas.TOOL_SCHEMAS]


def test_every_schema_tool_has_a_chinese_label():
    missing = [name for name in _schema_tools()
               if name not in approval_view._TOOL_LABELS]
    assert not missing, f'这些工具在活动/进度行里会显示英文原名：{missing}'


def test_labels_are_unique():
    counts = collections.Counter(approval_view._TOOL_LABELS.values())
    duplicate = {label: n for label, n in counts.items() if n > 1}
    assert not duplicate, f'中文名重复，反查表会还原错工具：{duplicate}'


def test_labels_are_gbk_safe():
    """活动行每次工具调用都打印，中文 Windows 控制台是 GBK。"""
    for label in approval_view._TOOL_LABELS.values():
        label.encode('gbk')


def test_progress_line_reads_naturally():
    """「正在」+ 标签要成句：不能出现「正在」接英文。"""
    import turn_progress

    line = turn_progress.format_progress_line(
        {'phase': 'tool', 'tool': 'kill_background', 'elapsed': 1.0})
    assert 'kill_background' not in line, line
    assert '正在' in line and '停止后台任务' in line, line
