"""桌面控制的工具层：文案、审计（不含输入内容）与动作后的变化摘要。"""
from __future__ import annotations

import hashlib

import desktop_control_windows


def _audit(audit, event: str, **payload) -> None:
    if audit is None:
        return
    try:
        audit(event, **payload)
    except Exception:
        # Audit failure must not turn an already completed input action into a
        # second failure, but the tool result remains intentionally minimal.
        pass


def _state_text(state) -> str:
    if not state.available:
        detail = f'（{state.error}）' if state.error else ''
        return f'当前没有可用的前台窗口{detail}'
    title = _clip(state.title, 60) or '(未命名窗口)'
    process = _clip(state.process, 40) or '未知进程'
    focused = (_clip(state.focused_name, 40)
               or _clip(state.focused_control_type, 40) or '未知控件')
    rect = ','.join(str(value) for value in state.rect) if state.rect else '未知'
    return (f'前台窗口：{title}（{process}，pid {state.pid}，'
            f'窗口 {state.window_id}）· 位置 {rect} · 焦点「{focused}」')


_ACTION_VERBS = {
    'move': '移动鼠标', 'click': '点击', 'type': '输入文字', 'key': '按键',
    'scroll': '滚动', 'sequence': '组合动作', 'focus': '切换窗口',
    'element': '元素操作',
}


def _change_text(before, after) -> str:
    """动作前后的差异：只报能确定看到的三个信号。

    这是「动作发出去了」和「界面变了」的区别。物理输入只看得到前台窗口与焦点，
    所以措辞必须留有余地：看不到变化不等于没生效。
    """
    if before is None or after is None:
        return ''
    if not before.available or not after.available:
        return ''
    if before.window_id and after.window_id and before.window_id != after.window_id:
        title = _clip(after.title, 40) or '另一个窗口'
        return f'前台窗口换成了「{title}」。'
    if (before.focused_name or '') != (after.focused_name or ''):
        old = _clip(before.focused_name, 30) or '无'
        new = _clip(after.focused_name, 30) or '无'
        return f'焦点从「{old}」变成「{new}」。'
    if (before.title or '') != (after.title or ''):
        title = _clip(after.title, 40)
        return f'窗口标题变成了「{title}」。'
    return ('焦点和前台窗口都没变；这不代表目标控件没响应，'
            '必要时用 desktop_inspect 确认。')


def snapshot(*, runner=None, audit=None) -> str:
    try:
        state = desktop_control_windows.snapshot(runner=runner)
    except desktop_control_windows.DesktopControlError as exc:
        _audit(audit, 'desktop_snapshot_failed', reason=str(exc)[:120])
        return f'读不到前台窗口：{exc}。'
    _audit(audit, 'desktop_snapshot', process=state.process, pid=state.pid,
           window_id=state.window_id,
           title_sha1=hashlib.sha1(state.title.encode('utf-8')).hexdigest(),
           sensitive_target=state.sensitive_target)
    return _state_text(state)


_ELEMENT_VERBS = {
    'invoke': '点击', 'setvalue': '写入', 'toggle': '切换', 'select': '选中',
    'expand': '展开', 'collapse': '收起', 'focus': '聚焦',
}
_MAX_NAME_CHARS = 60
_MAX_VALUE_CHARS = 60


#: 不可见字符：零宽空格、方向标记、软连字符。控件名里确实会出现（实测某个
#: 控件名带 U+200B），而控制台在 GBK 页下遇到它只能吐反斜杠转义串。
_INVISIBLE = dict.fromkeys(
    ord(ch) for ch in '\u200b\u200c\u200d\u200e\u200f'
    '\u202a\u202b\u202c\u202d\u202e\u2060\u2061\u2062\u2063\u2064'
    '\ufeff\u00ad')


def _clean(text) -> str:
    """把控件名 / 值收敛成控制台能安全显示的文本。"""
    text = str(text or '').translate(_INVISIBLE)
    kept = []
    for char in text:
        if char in '\t\r\n':
            kept.append(' ')
            continue
        if ord(char) < 32:
            continue
        try:
            char.encode('gbk')
        except UnicodeEncodeError:
            continue
        kept.append(char)
    return ''.join(kept)


def _clip(text, limit: int) -> str:
    text = ' '.join(_clean(text).split())
    if len(text) <= limit:
        return text
    return text[:limit - 1] + '~'


def _element_line(item) -> str:
    parts = [f'#{item.id}', item.label]
    name = _clip(item.name, _MAX_NAME_CHARS)
    if name:
        parts.append(f'"{name}"')
    elif item.value:
        parts.append(f'"{_clip(item.value, _MAX_NAME_CHARS)}"')
    marks = []
    if item.password:
        marks.append('已隐藏')
    elif item.value:
        value = _clip(item.value, _MAX_VALUE_CHARS)
        if value:
            marks.append(f'内容 "{value}"')
    if item.focused:
        marks.append('当前焦点')
    if not item.enabled:
        marks.append('不可用')
    line = ' '.join(parts)
    if marks:
        line += ' · ' + ' · '.join(marks)
    if item.bounds:
        left, top, width, height = item.bounds
        line += f' ({left},{top},{width},{height})'
    return '  ' + line


def inspect(*, runner=None, audit=None) -> str:
    """列出前台窗口的可交互控件。只读，不改状态。"""
    try:
        tree = desktop_control_windows.inspect(runner=runner)
    except desktop_control_windows.DesktopControlError as exc:
        _audit(audit, 'desktop_inspect_failed', reason=str(exc)[:120])
        return (f'读不到这个窗口的界面结构：{exc}。'
                '可以先让用户切到目标窗口，或改用 desktop_snapshot 看前台窗口。')
    state = tree.state
    _audit(audit, 'desktop_inspect', process=state.process, pid=state.pid,
           window_id=state.window_id,
           title_sha1=hashlib.sha1(state.title.encode('utf-8')).hexdigest(),
           sensitive_target=state.sensitive_target,
           elements=len(tree.items), truncated=tree.truncated,
           duration_ms=tree.duration_ms)
    title = _clip(state.title, 80) or '(未命名窗口)'
    process = _clip(state.process, 40) or '未知进程'
    header = f'前台窗口：{title}（{process}）'
    if not tree.items:
        return (header + chr(10)
                + '这个窗口没有可读的控件（可能是自绘界面或游戏）。'
                '可以先截屏看一眼，再用坐标点击。')
    lines = [header, f'可交互控件 {len(tree.items)} 个'
             + ('（已按上限截断）' if tree.truncated else '')]
    if state.dpi and state.dpi != 96:
        percent = int(round(state.dpi / 96.0 * 100))
        lines.append(f'这块屏是 {percent}% 缩放；下面的坐标是物理像素，'
                     f'desktop_action 用同一套。')
    lines.extend(_element_line(item) for item in tree.items)
    lines.append(f'结构指纹：{tree.fingerprint}')
    lines.append('要操作其中某个控件，用 desktop_action：action 填 element，'
                 'element 填上面的编号，tree 原样回传这个指纹。'
                 '编号在界面变化后会失效，届时重新 desktop_inspect。')
    if len(tree.items) <= 2:
        # 实测：走旧式无障碍桥的应用（部分 WinForms / 老程序 / 部分 Explorer
        # 视图）只暴露一两个节点。总得告诉模型“不是你没看懂，是它就这么多”。
        lines.append('这个窗口能读到的控件很少，可能不支持无障碍接口。'
                     '目标不在上面的话，改用截屏加坐标点击。')
    return chr(10).join(lines)


_WAIT_SECONDS = 1000.0


def _wait_elapsed(result) -> str:
    seconds = result.elapsed_ms / _WAIT_SECONDS
    return f'{seconds:.1f} 秒'


def wait(*, target='control', text=None, state='present', timeout_ms=None,
         expected_pid=None, expected_window_id=None, runner=None,
         audit=None) -> str:
    """轮询等待一个条件成立。只读，但每次轮询都要读界面结构。"""
    needle = ' '.join(str(text or '').split())
    desired = str(state or 'present').strip().casefold()
    try:
        result = desktop_control_windows.wait(
            target, needle, desired, timeout_ms, expected_pid=expected_pid,
            expected_window_id=expected_window_id, runner=runner)
    except desktop_control_windows.DesktopControlError as exc:
        _audit(audit, 'desktop_wait_failed', reason=str(exc)[:120])
        return f'等待失败：{exc}'
    _audit(audit, 'desktop_wait', target=result.target, desired=result.desired,
           matched=result.matched, elapsed_ms=result.elapsed_ms,
           polls=result.polls, process=result.state.process,
           pid=result.state.pid, window_id=result.state.window_id)
    verb = '出现' if desired == 'present' else '消失'
    if result.target in ('window', 'foreground'):
        return _wait_window_text(result, needle, verb)
    return _wait_control_text(result, needle, verb)


def _wait_control_text(result, needle: str, verb: str) -> str:
    if not result.matched:
        state = result.state
        title = _clip(state.title, 60) or '(未定)'
        text = (f'{_wait_elapsed(result)}内没等到「{needle}」{verb}。'
                f'当前前台窗口是「{title}」，可读控件 {result.elements} 个。')
        if result.truncated:
            # 遍历被节点/时间预算截断时，「没看到」不等于「不存在」。
            # 不说清楚，模型会得出错误结论然后改方案。
            text += ('注意这次遍历被截断了：目标可能在没列出来的那部分控件里。'
                     '可以先用 desktop_inspect 确认，再决定要不要继续等。')
        else:
            text += '可以把 timeout_ms 调大，或先 desktop_inspect 看看现在有什么。'
        return text
    if result.desired == 'absent':
        return (f'等到了：「{needle}」已消失，{_wait_elapsed(result)}'
                f'（轮询 {result.polls} 次）。')
    item = result.element
    label = f'（{item.label}）' if item else ''
    head = (f'等到了：控件「{_clip(needle, 60)}」{label}出现了，'
            f'{_wait_elapsed(result)}（轮询 {result.polls} 次）。')
    if not item:
        return head
    lines = [head, _element_line(item).strip()]
    if result.fingerprint:
        lines.append(f'结构指纹：{result.fingerprint}')
        lines.append('要操作这个控件，用 desktop_action 的 element 模式，'
                     'element 填上面的编号、tree 回传这个指纹。')
    return chr(10).join(lines)


def _wait_window_text(result, needle: str, verb: str) -> str:
    if not result.matched:
        return (f'{_wait_elapsed(result)}内没等到标题含「{needle}」的窗口{verb}。'
                f'当前可见窗口 {result.windows} 个；'
                '可以 desktop_windows 看看都有什么。')
    if result.desired == 'absent':
        return (f'等到了：标题含「{needle}」的窗口已消失，'
                f'{_wait_elapsed(result)}（轮询 {result.polls} 次）。')
    window = result.window or {}
    title = _clip(window.get('title'), 60) or needle
    process = _clip(window.get('process'), 40) or '未知进程'
    pid = window.get('pid')
    window_id = window.get('window_id') or ''
    lines = [f'等到了：窗口「{title}」（{process}）出现了，'
             f'{_wait_elapsed(result)}（轮询 {result.polls} 次）。']
    if pid and window_id:
        lines.append(f'要操作它：先用 desktop_action 的 focus（pid {pid}、'
                     f'window_id {window_id}），再 desktop_inspect。')
    return chr(10).join(lines)


def element_action(*, operation, element, tree, expected_pid, expected_window_id,
                   text=None, runner=None, audit=None) -> str:
    """按编号操作控件。走 UIA 的元素动作，不移动鼠标。"""
    import never_do

    try:
        result = desktop_control_windows.element_action(
            operation, element, tree, expected_pid=expected_pid,
            expected_window_id=expected_window_id, text=text,
            forbidden=never_do.ui_forbidden_patterns(), runner=runner)
    except desktop_control_windows.DesktopControlError as exc:
        _audit(audit, 'desktop_action_failed', action='element',
               reason=str(exc)[:120])
        return f'{exc}'
    state = result.state
    target = result.target
    label = (target.name or target.label) if target else ''
    verb = _ELEMENT_VERBS.get(operation, operation)
    _audit(audit, 'desktop_action', action='element', operation=operation,
           element=element, process=state.process, pid=state.pid,
           window_id=state.window_id, duration_ms=result.duration_ms,
           title_sha1=hashlib.sha1(state.title.encode('utf-8')).hexdigest(),
           sensitive_target=state.sensitive_target)
    if label:
        return f'已完成{verb}「{_clip(label, _MAX_NAME_CHARS)}」。{_state_text(state)}'
    return f'已完成{verb}。{_state_text(state)}'


def action(action_name: str, *, x=None, y=None, text=None, key=None,
           amount=None, button=None, count=None, steps=None,
           expected_pid=None, expected_window_id=None,
           runner=None, audit=None) -> str:
    try:
        result = desktop_control_windows.action(
            action_name, x=x, y=y, text=text, key=key, amount=amount,
            button=button, count=count, steps=steps,
            expected_pid=expected_pid,
            expected_window_id=expected_window_id,
            runner=runner)
    except desktop_control_windows.DesktopControlError as exc:
        _audit(audit, 'desktop_action_failed', action=str(action_name)[:30],
               reason=str(exc)[:120])
        return f'Error: desktop action failed: {exc}'
    state = result.state
    step_records = getattr(result, 'steps', ()) or ()
    audit_payload = {
        'action': result.action, 'process': state.process, 'pid': state.pid,
        'window_id': state.window_id, 'duration_ms': result.duration_ms,
        'title_sha1': hashlib.sha1(state.title.encode('utf-8')).hexdigest(),
        'sensitive_target': state.sensitive_target,
    }
    if result.action == 'sequence':
        audit_payload['steps'] = len(step_records)
    _audit(audit, 'desktop_action', **audit_payload)
    if result.action == 'sequence':
        lines = [f'已执行 {len(step_records)} 个步骤：']
        for index, step in enumerate(step_records, 1):
            verb = _ACTION_VERBS.get(str(step.get('action')), '操作')
            lines.append(f'  {index}. {verb} 完成')
        lines.append(_state_text(state))
        change = _change_text(getattr(result, 'before', None), state)
        if change:
            lines.append(change)
        return '\n'.join(lines)
    verb = _ACTION_VERBS.get(result.action, result.action)
    tail = _state_text(state)
    change = _change_text(getattr(result, 'before', None), state)
    if change:
        tail += '\n' + change
    return f'已完成{verb}。{tail}'


def _scale_text(scale: float) -> str:
    if scale <= 0:
        return '缩放未知'
    percent = int(round(scale * 100))
    return f'缩放 {percent}%（{int(round(scale * 96))} dpi）'


def displays(*, runner=None, audit=None) -> str:
    """列出显示器的物理矩形、工作区与 DPI。只读。"""
    try:
        result = desktop_control_windows.displays(runner=runner)
    except desktop_control_windows.DesktopControlError as exc:
        _audit(audit, 'desktop_displays_failed', reason=str(exc)[:120])
        return f'读不到显示器信息：{exc}。'
    _audit(audit, 'desktop_displays', count=len(result.displays),
           foreground_index=result.foreground_index,
           foreground_dpi=result.foreground_dpi)
    if not result.displays:
        return '没有读到任何显示器信息。'
    virtual = result.virtual_screen
    head = f'显示器 {len(result.displays)} 块'
    if virtual:
        head += f' · 虚拟桌面 {virtual[0]},{virtual[1]} {virtual[2]}x{virtual[3]}'
    lines = [head, '坐标一律是物理像素，和截图、desktop_action 同一套。']
    for index, item in enumerate(result.displays, 1):
        left, top, width, height = item.bounds
        work_left, work_top, work_width, work_height = item.work
        mark = '（主屏）' if item.primary else ''
        lines.append(
            f'  #{index} {_clip(item.name, 24)}{mark} {left},{top} {width}x{height}'
            f' · 工作区 {work_left},{work_top},{work_width},{work_height}'
            f' · {_scale_text(item.scale)}')
    foreground = result.foreground_index
    target = None
    if foreground and foreground <= len(result.displays):
        target = result.displays[foreground - 1]
        title = _clip(result.state.title, 40) or '(未命名窗口)'
        lines.append(f'前台窗口「{title}」在 #{foreground} 上。')
    elif result.state.available:
        lines.append('前台窗口不在这些显示器上（可能在已断开的显示器）。')
    else:
        # 没有前台窗口时按主屏给提示：读数照样有用，别因为读不到焦点就不说。
        target = next((item for item in result.displays if item.primary),
                      result.displays[0])
    if target is not None and target.scale > 1.0:
        lines.append(
            f'{"这块屏" if foreground else "主屏"}是 {int(round(target.scale * 100))}% '
            f'缩放：网页/设计稿给的逻辑坐标要乘 {target.scale:g} '
            f'才是这里用的物理像素。')
    return chr(10).join(lines)


def list_windows(*, limit: int = 20, runner=None, audit=None) -> str:
    """列出可见窗口。只读，不需要先知道前台是谁。"""
    try:
        result = desktop_control_windows.list_windows(limit=limit, runner=runner)
    except desktop_control_windows.DesktopControlError as exc:
        return f'列窗口失败：{exc}。'
    windows = result['windows']
    _audit(audit, 'desktop_list_windows', count=len(windows),
           total=result['total'])
    if not windows:
        return '当前没有可见的顶层窗口。'
    lines = [f"可见窗口 {len(windows)} 个（共 {result['total']} 个）："]
    for index, item in enumerate(windows, 1):
        marks = []
        if item['foreground']:
            marks.append('当前前台')
        if item['minimized']:
            marks.append('已最小化')
        suffix = f" · {'、'.join(marks)}" if marks else ''
        title = _clip(item['title'], 70)
        process = _clip(item['process'], 30) or '未知进程'
        lines.append(f"  {index}. {process}：{title}{suffix}"
                     f"（pid {item['pid']}，窗口 {item['window_id']}）")
    lines.append('要操作某个窗口：先用 desktop_action 的 focus（带上它的 pid 和'
                 '窗口号）切到前台，再 desktop_inspect 看控件。')
    return chr(10).join(lines)


def focus(action_args: dict, *, runner=None, audit=None) -> str:
    """把窗口切到前台；仍要 pid + window_id 双因子。"""
    try:
        state = desktop_control_windows.focus_window(
            expected_pid=action_args.get('expected_pid'),
            expected_window_id=action_args.get('expected_window_id'),
            runner=runner)
    except desktop_control_windows.DesktopControlError as exc:
        _audit(audit, 'desktop_focus_failed', reason=str(exc)[:120])
        return f'切不到那个窗口：{exc}'
    _audit(audit, 'desktop_focus', process=state.process, pid=state.pid,
           window_id=state.window_id,
           title_sha1=hashlib.sha1(state.title.encode('utf-8')).hexdigest())
    return f'已切到那个窗口。{_state_text(state)}'
