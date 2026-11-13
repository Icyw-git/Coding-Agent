"""R1-0：工具执行核心可以被假依赖直接驱动（next-design §9.4）。

验收要求：

- 用假 deps 直接调用核心，**无需导入 harness / 加载用户配置 / 构造 UI**；
- 原门面（`exec_tool_call`）的 patch 点（`harness.tool_registry` /
  `harness.trigger_hooks` / `harness.ui_emit` / `harness.METRICS`）仍命中实际调用；
- 正常 / 拒绝 / 未知工具 / 参数错误 / 图片 / 后台 queued 的返回值契约不变。

这条用「把 `tool_dispatch._h` 换成会抛异常的函数」来证明核心真的不再回读组合根：
只要核心路径上还剩一处 `_h()`，调用就会炸。
"""

import json
from types import SimpleNamespace

import pytest

import tool_dispatch


class _Call:
    def __init__(self, call_id='c1', name='read', arguments='{"path": "a.txt"}'):
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=arguments)


class _Deps:
    """假依赖：记录每次 hook / emit / 计数，不碰 harness。"""

    def __init__(self, registry=None, blocked=None):
        self.registry = registry or {}
        self.blocked = blocked
        self.hook_calls = []
        self.events = []
        self.recorded = 0

    def trigger_hooks(self, phase, name, args, *extra):
        self.hook_calls.append((phase, name))
        return self.blocked if phase == 'PreToolUse' else None

    def emit(self, kind, **payload):
        self.events.append((kind, payload))

    def record_tool(self):
        self.recorded += 1

    def deps(self):
        return tool_dispatch.ToolExecutionDeps(
            registry=self.registry,
            trigger_hooks=self.trigger_hooks,
            emit=self.emit,
            record_tool=self.record_tool)


@pytest.fixture()
def no_harness(monkeypatch):
    """核心路径上任何一处 `_h()` 都会立刻失败。"""
    def boom():
        raise AssertionError('核心回读了组合根（_h）')

    monkeypatch.setattr(tool_dispatch, '_h', boom)


def test_core_runs_with_fake_deps_and_no_harness(no_harness):
    deps = _Deps(registry={'read': lambda path: f'contents of {path}'})
    name, args, message, images = tool_dispatch.execute_tool_core(
        _Call(), deps.deps())

    assert name == 'read' and args == {'path': 'a.txt'}
    assert message['content'] == 'contents of a.txt' and images == []
    assert deps.hook_calls == [('PreToolUse', 'read'), ('PostToolUse', 'read')]
    assert [kind for kind, _ in deps.events] == ['tool_start', 'tool_end']
    assert deps.recorded == 1


def test_core_reports_a_blocked_call_without_running_it(no_harness):
    ran = []
    deps = _Deps(registry={'read': lambda path: ran.append(path) or 'x'},
                 blocked='Permission denied by user')
    _n, _a, message, _i = tool_dispatch.execute_tool_core(_Call(), deps.deps())

    assert ran == [], '被拒的调用不该执行工具'
    assert message['content'] == 'Permission denied by user'
    _, payload = deps.events[1]
    assert payload['outcome'] == 'denied' and payload['ok'] is False


def test_core_reports_an_unknown_tool(no_harness):
    deps = _Deps(registry={})
    _n, _a, message, _i = tool_dispatch.execute_tool_core(_Call(), deps.deps())
    assert message['content'] == 'Error: unknown tool read'
    assert deps.events[1][1]['outcome'] == 'failed'


def test_core_reports_invalid_arguments(no_harness):
    deps = _Deps(registry={'read': lambda path: 'x'})
    call = _Call(arguments='{not json')
    seen = []
    _n, _a, message, _i = tool_dispatch.execute_tool_core(
        call, deps.deps(), on_event=lambda kind, payload: seen.append(kind))

    assert message['content'].startswith('Error: invalid tool arguments')
    assert seen == ['tool.started', 'tool.completed'], seen


def test_core_queues_a_background_task(no_harness, monkeypatch):
    """后台被接受时不算完成：outcome 必须是 queued。"""
    monkeypatch.setattr(tool_dispatch, 'should_run_background',
                        lambda name, args: True)
    monkeypatch.setattr(tool_dispatch, 'start_background_task',
                        lambda call, registry: 'bg_0001')
    deps = _Deps(registry={'bash': lambda command: 'ran'})
    call = _Call(name='bash', arguments=json.dumps({'command': 'npm install'}))

    _n, _a, message, _i = tool_dispatch.execute_tool_core(call, deps.deps())

    assert 'bg_0001' in message['content']
    _, payload = deps.events[1]
    assert payload['outcome'] == 'queued' and payload['background'] is True


def test_core_unwraps_image_results(no_harness):
    from tools import ImageResult

    deps = _Deps(registry={
        'view_image': lambda path: ImageResult(text='[图片] a.png',
                                               images=[{'name': 'a.png'}]),
    })
    _n, _a, message, images = tool_dispatch.execute_tool_core(
        _Call(name='view_image', arguments='{"path": "a.png"}'), deps.deps())

    assert message['content'] == '[图片] a.png'
    assert images == [{'name': 'a.png'}]


def test_facade_still_honours_harness_patch_points(monkeypatch):
    """门面必须继续在调用期读 harness 的四样东西，否则既有测试/插件会静默失效。"""
    import harness

    seen = {}

    class _Metrics:
        def record_tool(self):
            seen['metrics'] = True

    monkeypatch.setattr(harness, 'tool_registry', {'read': lambda path: 'patched'})
    # 契约：返回非 None = 拦截语。这里只记录相位、返回 None（放行）。
    phases = []
    monkeypatch.setattr(harness, 'trigger_hooks',
                        lambda phase, name, args, *rest: phases.append(phase))
    monkeypatch.setattr(harness, 'ui_emit',
                        lambda kind, **payload: seen.setdefault('emit', kind))
    monkeypatch.setattr(harness, 'METRICS', _Metrics())

    _n, _a, message, _i = harness.exec_tool_call(_Call())

    assert message['content'] == 'patched', message
    assert phases == ['PreToolUse', 'PostToolUse'], phases
    assert seen['emit'] == 'tool_start'
    assert seen['metrics'] is True


def test_facade_with_hooks_disabled_skips_pre_tool_use(monkeypatch):
    """teammate 路径（hooks=False）仍然跳过策略 hook，其余不变。"""
    import harness

    called = []
    monkeypatch.setattr(harness, 'tool_registry', {'read': lambda path: 'x'})
    monkeypatch.setattr(harness, 'trigger_hooks',
                        lambda phase, name, args, *rest: called.append(phase))

    harness.exec_tool_call(_Call(), hooks=False)
    assert called == []


def test_core_function_does_not_reference_the_composition_root():
    """静态确认：`execute_tool_core` 内部没有 `_h()`/`import harness`。

    上面那条用「把 _h 换成会抛异常的函数」动态验证；这条从源码层面钉住，
    免得将来有人在核心函数里加一行 `_h()` 而测试恰好没走到那分支。
    """
    import ast
    import inspect

    source = inspect.getsource(tool_dispatch.execute_tool_core)
    tree = ast.parse(source.lstrip())
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == '_h':
            raise AssertionError('核心函数里出现了 `_h`')
        if isinstance(node, ast.ImportFrom) and node.module == 'harness':
            raise AssertionError('核心函数里 import 了 harness')
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != 'harness', '核心函数里 import 了 harness'
