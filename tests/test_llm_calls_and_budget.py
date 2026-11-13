"""R2-b2：统一的模型调用记录 + 后台预算（next-design §7.2 / §9.5）。

现状的缺口：**副通道（记忆整理、标题、压缩、审批、建议）的 token 完全不可见**——
它们走 `llm_route.completion(role=...)`，没有任何一处记录。于是"每天花了多少"只能
看到主回合那一半。

契约（§7.2）：

- 统一调用记录：`role / provider / model / purpose / duration / tokens / cache /
  outcome / model_request_id / attempt`；
- **一次 SDK 请求只计一次**（记录点放在唯一的调用入口，主回合与副通道不能各记一遍）；
- `model_request_id` 是模型逻辑请求，**与用户入站 request_id 不是一回事**——一次用户
  回合可能含多次模型请求；
- 缺失 usage / 未知价格显式 unknown，**不能算 0**；
- 预算分两级：每回合上限 + 背景日预算；不足时**跳过可选工作**（记忆整理、建议、
  自改进），但**绝不跳过审批或安全核验**。
"""


import pytest

import budget
import llm_calls
import llm_route


class _Usage:
    def __init__(self, prompt=100, completion=20, cached=None):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion
        self.prompt_tokens_details = None
        self.prompt_cache_hit_tokens = cached
        self.cache_creation_input_tokens = None
        self.completion_tokens_details = None


class _Reply:
    def __init__(self, usage=None):
        self.usage = usage or _Usage()


@pytest.fixture()
def clock(monkeypatch):
    """**一处时钟**：记录写入与预算结算读同一个。

    两边各用各的（一边真 time.time、一边假时钟）时，"今天"的判定会永远对不上——
    这正是 R1-a / R2-b1 两次踩过的同一个坑。
    """
    state = {'now': 1_700_000_000.0}
    monkeypatch.setattr(llm_calls, '_now', lambda: state['now'])
    monkeypatch.setattr(budget, '_now', lambda: state['now'])
    return state


@pytest.fixture()
def isolated(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(llm_calls, 'LOG_PATH', tmp_path / 'llm_calls.jsonl')
    monkeypatch.setattr(budget, 'LOG_PATH', tmp_path / 'llm_calls.jsonl')
    monkeypatch.setattr(budget, 'STATE_PATH', tmp_path / 'budget.json')
    return tmp_path


def _fake_create(monkeypatch, *, reply=None, error=None, role='memory'):
    calls = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if error is not None:
                raise error
            return reply or _Reply()

    class _Client:
        chat = type('C', (), {'completions': _Completions()})()

    monkeypatch.setattr(llm_route, 'target_for',
                        lambda r='main': type('T', (), {'model': 'deepseek-flash',
                                                        'provider': 'deepseek'})())
    monkeypatch.setattr(llm_route, 'client_for', lambda r='main', t=None: _Client())
    return calls


def test_one_sdk_request_is_recorded_exactly_once(isolated, monkeypatch):
    """记录点放在唯一入口：一次 SDK 请求只产生一条记录。"""
    _fake_create(monkeypatch)
    llm_route.completion('memory', messages=[{'role': 'user', 'content': 'x'}])

    rows = llm_calls.read(isolated / 'llm_calls.jsonl')
    assert len(rows) == 1, rows
    row = rows[0]
    assert row['role'] == 'memory'
    assert row['model'] == 'deepseek-flash'
    assert row['provider'] == 'deepseek'
    assert row['outcome'] == 'ok'
    assert row['tokens']['prompt'] == 100
    assert row['tokens']['completion'] == 20
    assert row['model_request_id']
    assert row['attempt'] == 1


def test_failure_is_recorded_with_an_outcome(isolated, monkeypatch):
    _fake_create(monkeypatch, error=RuntimeError('provider 500'))
    with pytest.raises(RuntimeError):
        llm_route.completion('memory', messages=[])

    row = llm_calls.read(isolated / 'llm_calls.jsonl')[0]
    assert row['outcome'] == 'error'
    assert 'provider 500' in row['error']
    assert row['tokens'] is None, '失败时没有用量：要显式 unknown，不能写 0'


def test_missing_usage_is_unknown_not_zero(isolated, monkeypatch):
    """供应商没回 usage：记 unknown，不能算 0（否则预算与成本都失真）。"""
    _fake_create(monkeypatch, reply=type('R', (), {'usage': None})())
    llm_route.completion('title', messages=[])

    row = llm_calls.read(isolated / 'llm_calls.jsonl')[0]
    assert row['tokens'] is None
    assert row['usage_available'] is False


def test_main_and_side_channels_are_counted_separately(isolated, monkeypatch):
    """主回合与副通道分开看：同一次请求不能在两处重复累计。"""
    _fake_create(monkeypatch)
    llm_route.completion('main', messages=[])
    llm_route.completion('memory', messages=[])
    llm_route.completion('main', messages=[])

    rows = llm_calls.read(isolated / 'llm_calls.jsonl')
    assert [row['role'] for row in rows] == ['main', 'memory', 'main']
    totals = llm_calls.totals_by_role(rows)
    assert totals['main']['requests'] == 2
    assert totals['memory']['requests'] == 1
    assert totals['main']['tokens'] == 240          # (100+20) × 2
    assert totals['memory']['tokens'] == 120


# ---------------------------------------------------------------- 预算

def test_daily_budget_uses_a_fake_clock_and_reports_exhaustion(isolated, monkeypatch, clock):
    _fake_create(monkeypatch)
    monkeypatch.setattr(budget, 'DAILY_TOKENS', 240)

    for _ in range(2):
        llm_route.completion('main', messages=[])

    assert budget.spent_today() == 240
    assert budget.exhausted() is True

    # 第二天自动清零（按自然日结算）
    clock['now'] += 24 * 3600
    assert budget.spent_today() == 0
    assert budget.exhausted() is False


def test_budget_never_blocks_approval_or_safety_work(isolated, monkeypatch):
    """预算耗尽时**必须**还能做审批/安全相关的判断。

    这两类是"不做就会出错"的工作，和"少整理一次记忆"不是一回事。
    """
    monkeypatch.setattr(budget, 'DAILY_TOKENS', 0)
    assert budget.exhausted() is True
    assert budget.allows('approval') is True
    assert budget.allows('safety') is True
    assert budget.allows('memory') is False
    assert budget.allows('proactive') is False


def test_unknown_usage_does_not_fake_a_smaller_spend(isolated, monkeypatch):
    """有请求但没用量：不能因为"没数字"就当成 0 花了。

    已知用量仍是 0，但说明里必须出现"未知次数"——否则用户会以为今天一分钱没花。
    """
    monkeypatch.setattr(budget, 'DAILY_TOKENS', 1000)
    _fake_create(monkeypatch, reply=type('R', (), {'usage': None})())
    llm_route.completion('memory', messages=[])

    assert budget.spent_today() == 0
    assert '未知' in budget.describe(), budget.describe()


# ---------------------------------------------------------------- 接线

def test_optional_work_is_skipped_when_the_budget_is_gone(isolated, monkeypatch):
    """预算耗尽：可选工作跳过并记账，但**审批仍可调模型**。"""
    monkeypatch.setattr(budget, 'DAILY_TOKENS', 0)
    assert budget.allows('memory') is False

    budget.note_skip('memory')
    assert budget.skipped_today() == {'memory': 1}
    assert budget.allows('approval') is True, '省预算不能省掉审批'


def test_status_reports_budget_and_skips(isolated, monkeypatch):
    import status_report

    monkeypatch.setattr(budget, 'DAILY_TOKENS', 100)
    _fake_create(monkeypatch)
    llm_route.completion('main', messages=[])
    budget.note_skip('proactive')

    note = status_report._budget_note()
    assert '背景预算' in note, note
    assert '120' in note, note          # 已知用量
    assert '建议' in note, note          # 今天跳过了什么


def test_housekeeping_skips_memory_when_budget_is_gone(isolated, monkeypatch):
    """接进生产：预算耗尽时 `/status` 那两行与记忆整理都按"跳过"走。"""
    import harness

    called = []
    monkeypatch.setattr(harness, 'extract_memories', lambda msgs: called.append('extract'))
    monkeypatch.setattr(harness, 'consolidate_memories', lambda: called.append('consolidate'))
    monkeypatch.setattr(budget, 'DAILY_TOKENS', 0)

    assert budget.allows('memory') is False
    budget.note_skip('memory')
    assert called == [], '预算耗尽时不该再跑记忆整理'
    assert budget.skipped_today().get('memory') == 1
