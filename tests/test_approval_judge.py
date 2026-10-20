"""LLM 审查官测试：ASK 噪音源的自动复核（fail-closed）与破坏性词边界匹配。

运行: python test_approval_judge.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pytest

import harness
import hooks
from session_store import SessionEventStore


def make_client(replies):
    """可脚本化的审查官假件：按队列返回回复或异常，记录每次入参。
    reply 是 str → 包装成标准响应；是 SimpleNamespace → 视为完整响应直传。"""
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, SimpleNamespace):
            return reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=reply))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """档位回默认、审批历史/缓存/会话白名单清零。"""
    monkeypatch.delenv('HARNESS_SANDBOX_MODE', raising=False)
    monkeypatch.delenv('HARNESS_APPROVAL_POLICY', raising=False)
    monkeypatch.delenv('HARNESS_LLM_APPROVAL', raising=False)
    monkeypatch.delenv('HARNESS_JUDGE_TIMEOUT', raising=False)
    hooks.GATE.clear()
    yield
    hooks.GATE.clear()


def test_judge_auto_approves_interpreter(monkeypatch):
    """`python -m pytest` 这类解释器命令经审查官放行：不再问人、历史可审计。"""
    client, calls = make_client(['{"decision": "approve"}'])
    monkeypatch.setattr(harness, 'client', client)
    decided = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda *a, **kw: decided.append(a) or (True, ''))

    result = hooks.enforce('bash', {'command': 'python -m pytest -q'}, interactive=True)

    assert result is None, "审查放行 → 不拒绝"
    assert len(calls) == 1 and not decided, "LLM 审查了，没有打扰人工"
    hist = hooks.GATE.history()
    assert hist[0].resolved_by == 'llm-judge' and hist[0].status == 'approved'


def test_judge_escalates_to_human(monkeypatch):
    client, calls = make_client(['{"decision": "escalate", "reason": "会删除工作区外文件"}'])
    monkeypatch.setattr(harness, 'client', client)
    decided = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda *a, **kw: decided.append(a) or (True, ''))

    result = hooks.enforce('bash', {'command': 'python cleanup.py'}, interactive=True)

    assert result is None, "人工批准后放行"
    assert len(calls) == 1 and len(decided) == 1, "升级路径只走一次人工"


def test_judge_failure_falls_back_to_human(monkeypatch, capsys):
    """审查官不可用（超时/异常）→ fail-closed 回落人工，且失败原因必须可见
    （静默回落会让人以为审查官在工作——实测 max_tokens=60 被思考模式吃光、
    三个 ASK 全部无声落人工）。"""
    client, calls = make_client([RuntimeError('llm timeout')])
    monkeypatch.setattr(harness, 'client', client)
    decided = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda *a, **kw: decided.append(a) or (True, ''))

    assert hooks.enforce('bash', {'command': 'python -m pytest'}, interactive=True) is None
    assert len(calls) == 1 and len(decided) == 1
    out = capsys.readouterr().out
    assert '审查不可用' in out, "失败原因必须打印"


def test_judge_budget_fits_thinking_models(monkeypatch):
    """审查官 max_tokens 必须给足：deepseek-flash 默认思考模式，思考 token
    先于正文消耗（实测简单命令 reasoning 4000+，60 预算=空 content 静默失效）。"""
    client, calls = make_client(['{"decision": "approve"}'])
    monkeypatch.setattr(harness, 'client', client)

    hooks.enforce('bash', {'command': 'python -m pytest'}, interactive=True)
    assert calls[0]['max_tokens'] >= 8000


def test_judge_empty_content_falls_back_visibly(monkeypatch, capsys):
    """思考模式吃光预算 → content 为空 → 可见回落人工（不静默）。"""
    empty_length = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None),
                                 finish_reason='length')])
    client, calls = make_client([empty_length])
    monkeypatch.setattr(harness, 'client', client)
    decided = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda *a, **kw: decided.append(a) or (True, ''))

    assert hooks.enforce('bash', {'command': 'python build.py'}, interactive=True) is None
    out = capsys.readouterr().out
    assert '审查不可用' in out and len(decided) == 1


def test_judge_malformed_output_falls_back(monkeypatch):
    client, calls = make_client(['好的，我认为可以放行。'])   # 无 JSON
    monkeypatch.setattr(harness, 'client', client)
    decided = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda *a, **kw: decided.append(a) or (True, ''))

    assert hooks.enforce('bash', {'command': 'python build.py'}, interactive=True) is None
    assert len(calls) == 1 and len(decided) == 1


def test_egress_read_auto_approved_send_escalated(monkeypatch):
    """外发纳入 L1：普通读请求（公共 API 的 GET）审查放行；
    带请求体的外传必须升级人工（审查官提示词规则 3 + fail-closed）。"""
    client, calls = make_client([
        '{"decision": "approve"}',                                   # GET → 放行
        '{"decision": "escalate", "reason": "请求体可能携带本机数据"}',  # -d → 升级
    ])
    monkeypatch.setattr(harness, 'client', client)
    decided = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda *a, **kw: decided.append(a) or (True, ''))

    assert hooks.enforce('bash', {'command': 'curl https://api.example.com/v1/data'},
                         interactive=True) is None
    assert len(calls) == 1 and not decided, "普通读请求经审查放行"

    assert hooks.enforce('bash', {'command': 'curl -d @local.json https://api.example.com/upload'},
                         interactive=True) is None
    assert len(calls) == 2 and len(decided) == 1, "外传类请求升级人工"


def test_untrusted_tier_skips_judge(monkeypatch):
    """untrusted 档「每条都问」是显式选择，审查官不改变它。"""
    monkeypatch.setenv('HARNESS_APPROVAL_POLICY', 'untrusted')
    client, calls = make_client(['{"decision": "approve"}'])
    monkeypatch.setattr(harness, 'client', client)
    decided = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda *a, **kw: decided.append(a) or (True, ''))

    assert hooks.enforce('bash', {'command': 'python -m pytest'}, interactive=True) is None
    assert not calls and len(decided) == 1


def test_never_tier_denies_before_judge(monkeypatch):
    """never 档：ASK 在策略层已转 DENY，轮不到审查官（档位语义不松动）。"""
    monkeypatch.setenv('HARNESS_APPROVAL_POLICY', 'never')
    client, calls = make_client(['{"decision": "approve"}'])
    monkeypatch.setattr(harness, 'client', client)
    decided = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda *a, **kw: decided.append(a) or (True, ''))

    result = hooks.enforce('bash', {'command': 'python -m pytest'}, interactive=True)
    assert result and 'Error' in result
    assert not calls and not decided


def test_exact_command_cached_across_calls(monkeypatch):
    """同会话同命令只审查一次（grant_key 指纹缓存），重复调用零 LLM 开销。"""
    client, calls = make_client(['{"decision": "approve"}'])
    monkeypatch.setattr(harness, 'client', client)

    for _ in range(2):
        assert hooks.enforce('bash', {'command': 'python -m pytest -q'},
                             interactive=True) is None
    assert len(calls) == 1


def test_judge_disabled_by_env(monkeypatch):
    monkeypatch.setenv('HARNESS_LLM_APPROVAL', '0')
    client, calls = make_client(['{"decision": "approve"}'])
    monkeypatch.setattr(harness, 'client', client)
    decided = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda *a, **kw: decided.append(a) or (True, ''))

    assert hooks.enforce('bash', {'command': 'python -m pytest'}, interactive=True) is None
    assert not calls and len(decided) == 1


def test_judge_skipped_for_headless_threads(monkeypatch):
    """无头路径（teammate/后台/任务 runner）的审批语义是排队等远程决议，
    审查官不得旁路——interactive=False 时直接进队列。"""
    client, calls = make_client(['{"decision": "approve"}'])
    monkeypatch.setattr(harness, 'client', client)
    queued = []
    monkeypatch.setattr(hooks.GATE, 'decide_queued',
                        lambda tool, args, reason: queued.append(tool) or (True, ''))

    assert hooks.enforce('bash', {'command': 'python -m pytest'},
                         interactive=False) is None
    assert not calls and len(queued) == 1, "无头路径不经 LLM 审查官"


def test_judge_call_has_short_timeout(monkeypatch):
    """审查官调用必须带独立超时（默认 30s，远小于审批的 300s）——否则供应商
    卡住时绕过整个 fail-safe 语义。"""
    client, calls = make_client(['{"decision": "approve"}'])
    monkeypatch.setattr(harness, 'client', client)

    hooks.enforce('bash', {'command': 'python -m pytest'}, interactive=True)
    assert calls[0]['timeout'] == hooks.DEFAULT_JUDGE_TIMEOUT == 30.0
    monkeypatch.setenv('HARNESS_JUDGE_TIMEOUT', '5')
    client2, calls2 = make_client(['{"decision": "approve"}'])
    monkeypatch.setattr(harness, 'client', client2)
    hooks.GATE._judge_approved.clear()
    hooks.enforce('bash', {'command': 'python build.py'}, interactive=True)
    assert calls2[0]['timeout'] == 5.0


def test_revoke_and_tier_switch_clear_judge_cache(monkeypatch):
    """/revoke 与档位切换必须连带清掉审查官放行缓存——撤销的意义是「此前
    给出去的免审全部作废」，审查官批过的也是预授权。"""
    client, calls = make_client(['{"decision": "approve"}',
                                 '{"decision": "approve"}'])
    monkeypatch.setattr(harness, 'client', client)

    assert hooks.enforce('bash', {'command': 'python -m pytest'}, interactive=True) is None
    key = '|bash:python -m pytest'
    assert hooks.GATE.judge_cache_hit(key), "放行后应入缓存"
    hooks.GATE.revoke_session()
    assert not hooks.GATE.judge_cache_hit(key), "撤销必须收回审查官的免审"

    # 重新放行后 /revoke 命令路径同样生效
    assert hooks.enforce('bash', {'command': 'python -m pytest'}, interactive=True) is None
    assert hooks.GATE.judge_cache_hit(key)
    hooks.GATE.clear()
    assert not hooks.GATE.judge_cache_hit(key)


# ---------- 批级合并审查（agent_loop flush 前的一次性判定） ----------

def make_tool_call(cid, command):
    import json as _json
    return SimpleNamespace(id=cid, type='function',
                           function=SimpleNamespace(name='bash',
                                                    arguments=_json.dumps({'command': command})))


def test_preflight_batch_merges_into_one_llm_call(monkeypatch):
    """批内 3 条会 ASK 的 bash → 一次 LLM 调用按索引判定；
    放行的进缓存（enforce 零开销），升级的照旧走人工。"""
    client, calls = make_client([
        '[{"index": 0, "decision": "approve"},'
        ' {"index": 1, "decision": "approve"},'
        ' {"index": 2, "decision": "escalate", "reason": "删除工作区外文件"}]'
    ])
    monkeypatch.setattr(harness, 'client', client)
    decided = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda *a, **kw: decided.append(a) or (True, ''))

    batch = [make_tool_call('c1', 'python run_tests.py'),
             make_tool_call('c2', 'python build_docs.py'),
             make_tool_call('c3', 'python -c "import shutil; shutil.rmtree(\'D:/x\')"')]
    assert hooks.preflight_judge(batch) == 2
    assert len(calls) == 1, "整批只打一次 LLM"
    payload = calls[0]['messages'][-1]['content']
    assert '[0]' in payload and 'python run_tests.py' in payload

    # 放行的两条：enforce 命中缓存，零 LLM、零人工
    assert hooks.enforce('bash', {'command': 'python run_tests.py'}, interactive=True) is None
    assert hooks.enforce('bash', {'command': 'python build_docs.py'}, interactive=True) is None
    assert len(calls) == 1 and not decided
    # 升级的那条：回落人工
    assert hooks.enforce('bash', {'command': 'python -c "import shutil; shutil.rmtree(\'D:/x\')"'},
                         interactive=True) is None
    assert len(decided) == 1


def test_preflight_skips_clean_or_single_batches(monkeypatch):
    """只读批零开销；单条候选不足以为之打一次 LLM。"""
    client, calls = make_client(['[]'])
    monkeypatch.setattr(harness, 'client', client)

    readonly = [make_tool_call('c1', 'git status')]   # 非破坏非外发 → 直接 ALLOW
    assert hooks.preflight_judge(readonly) == 0
    assert not calls
    assert hooks.preflight_judge([make_tool_call('c2', 'python a.py')]) == 0
    assert not calls, "单条候选走 enforce 的单条路径"


def test_batch_payload_carries_the_same_defenses_as_single(monkeypatch):
    """批量与单条同源：数据标注、事实段、触发原因、HARNESS_JUDGE_MODEL 一个不少。

    批量曾只拼裸命令，事实段与模型开关都只在单条路径生效——同一条命令
    单审有事实、批判没事实，审查官看到的前提都不一样。
    """
    client, calls = make_client([
        '[{"index": 0, "decision": "approve"},'
        ' {"index": 1, "decision": "approve"}]'])
    monkeypatch.setattr(harness, 'client', client)
    monkeypatch.setenv('HARNESS_JUDGE_MODEL', 'cheap-judge')

    batch = [make_tool_call('c1', 'python run_tests.py'),
             make_tool_call('c2', 'python build_docs.py')]
    assert hooks.preflight_judge(batch) == 2
    payload = calls[0]['messages'][-1]['content']
    assert calls[0]['model'] == 'cheap-judge'
    assert hooks.JUDGE_DATA_MARKER in payload, "数据标注不能只给单条路径"
    assert 'OS 沙箱' in payload, "事实段（沙箱/档位）缺失"
    assert '触发原因' in payload
    assert 'python run_tests.py' in payload and 'python build_docs.py' in payload


def test_batch_skips_over_budget_commands(monkeypatch):
    """超预算命令不进批量：单条路径会直接升级人工，批量不能拿半条命令盲批。"""
    client, calls = make_client([
        '[{"index": 0, "decision": "approve"},'
        ' {"index": 1, "decision": "approve"}]'])
    monkeypatch.setattr(harness, 'client', client)
    long_command = 'python -c "' + 'x' * (hooks.JUDGE_DETAIL_BUDGET + 100) + '"'

    batch = [make_tool_call('c1', 'python run_tests.py'),
             make_tool_call('c2', long_command),
             make_tool_call('c3', 'python build_docs.py')]
    assert hooks.preflight_judge(batch) == 2
    payload = calls[0]['messages'][-1]['content']
    assert long_command not in payload
    assert 'python run_tests.py' in payload and 'python build_docs.py' in payload


def test_preflight_failclosed_on_bad_output(monkeypatch):
    """审查官输出不可解析 → 全部不缓存，逐条路径照旧（该问人问人）。"""
    client, calls = make_client(['这些命令看起来都没问题。'])
    monkeypatch.setattr(harness, 'client', client)
    decided = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda *a, **kw: decided.append(a) or (True, ''))

    batch = [make_tool_call('c1', 'python a.py'), make_tool_call('c2', 'python b.py')]
    assert hooks.preflight_judge(batch) == 0
    assert hooks.enforce('bash', {'command': 'python a.py'}, interactive=True) is None
    assert len(decided) == 1, "缓存为空 → 回落人工审批"


# ---------- 破坏性列表的词边界匹配（exec_policy 层） ----------

def test_destructive_word_boundary():
    from exec_policy import ALLOW, ASK, Policy

    policy = Policy()
    # 误伤修复：confirm/delegate/delete 含 'rm '/'del ' 子串，但命令词不是 rm/del
    assert policy.evaluate('bash', {'command': 'echo please confirm the plan'},
                           destructive=['rm ', 'del ']).action == ALLOW
    assert policy.evaluate('bash', {'command': 'echo delete old files'},
                           destructive=['del ']).action == ALLOW
    # 真破坏性动作仍然命中：命令词、参数位置 token 都算
    assert policy.evaluate('bash', {'command': 'rm -rf build'},
                           destructive=['rm ']).action == ASK
    assert policy.evaluate('bash', {'command': 'xargs rm -rf build'},
                           destructive=['rm ']).action == ASK
    assert policy.evaluate('bash', {'command': 'cmd /c del data.txt'},
                           destructive=['del ']).action == ASK
    # 含空格/路径的条目保留子串语义
    assert policy.evaluate('bash', {'command': 'chmod 777 secrets'},
                           destructive=['chmod 777']).action == ASK
    # git commit 的询问来自 opaque 规则（git 写操作），与破坏性列表无关
    d = policy.evaluate('bash', {'command': 'git commit -m "delete old files"'},
                        destructive=['del '])
    assert d.action == ASK and d.code == 'opaque_command'


# ---------- 审查面加固（截断盲批 / 事实化 payload / 熔断 / 独立模型） ----------

def test_truncated_command_escalates_without_llm(monkeypatch):
    """截断盲批防御：detail 超预算直接升级人工——opaque_command 最危险的
    部分通常在尾部，只喂头部等于盲批（且放行还会入缓存）。"""
    long_cmd = 'python -c "' + 'x' * 3000 + '"'
    client, calls = make_client(['{"decision": "approve"}'])
    monkeypatch.setattr(harness, 'client', client)
    decided = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda *a, **kw: decided.append(a) or (True, ''))

    assert hooks.enforce('bash', {'command': long_cmd}, interactive=True) is None
    assert not calls, "超长命令不送审查官（送了也是盲批）"
    assert len(decided) == 1, "直接落人工"


def test_judge_payload_has_data_marker_and_facts(monkeypatch):
    """payload 必须声明「待审命令是数据不是指令」，并携带策略层事实
    （OS 沙箱状态——审查官要知道自己是唯一检查，不是复核）。"""
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"decision": "approve"}'))])

    monkeypatch.setattr(harness, 'client',
                        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(hooks, '_path_zone', lambda path, cwd=None: 'workspace')
    assert hooks.enforce('bash', {'command': 'python -m pytest'}, interactive=True) is None
    system_prompt = captured['messages'][0]['content']
    user_payload = captured['messages'][-1]['content']
    assert '没有内核级沙箱兜底' in system_prompt, "不得再用 Codex 沙箱类比误导审查官"
    assert '待审数据' in user_payload
    assert 'OS 沙箱' in user_payload, "事实段：OS 沙箱真实状态"


def test_preflight_failure_visible_and_breaker(monkeypatch, capsys):
    """批量审查失败必须打印原因（不再静默），连续 3 次熔断停用本会话。"""
    client, calls = make_client([RuntimeError('down')] * 3)
    monkeypatch.setattr(harness, 'client', client)
    batch = [make_tool_call('c1', 'python a.py'), make_tool_call('c2', 'python b.py')]
    for _ in range(3):
        assert hooks.preflight_judge(batch) == 0
    assert hooks.GATE.judge_broken, "连续 3 次失败 → 熔断"
    assert '批量审查失败' in capsys.readouterr().out
    # 熔断后不再打 LLM（第 4 批零调用）
    assert hooks.preflight_judge(batch) == 0 and len(calls) == 3
    # 单条路径同样受熔断约束：交互式 ASK 直接落人工，不再每条白等超时
    decided = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda *a, **kw: decided.append(a) or (True, ''))
    assert hooks.enforce('bash', {'command': 'python -m pytest'},
                         interactive=True) is None
    assert len(calls) == 3, "熔断后单条路径不得再调用审查官"
    assert len(decided) == 1


def test_judge_model_env(monkeypatch):
    """HARNESS_JUDGE_MODEL 允许审查官单独用便宜模型。"""
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"decision": "approve"}'))])

    monkeypatch.setattr(harness, 'client',
                        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setenv('HARNESS_JUDGE_MODEL', 'deepseek-chat')
    hooks.enforce('bash', {'command': 'python -m pytest'}, interactive=True)
    assert captured['model'] == 'deepseek-chat'


def test_status_shows_judge_summary(tmp_path, monkeypatch):
    """/status 显示审查官统计行。"""
    store = SessionEventStore(tmp_path, session_id='st')
    client, calls = make_client(['{"decision": "approve"}'])
    monkeypatch.setattr(harness, 'client', client)
    monkeypatch.setattr(harness, 'MEMORY_DIR', tmp_path / 'mem')
    monkeypatch.setattr(harness, 'MEMORY_INDEX', tmp_path / 'mem' / 'MEMORY.md')
    hooks.enforce('bash', {'command': 'python -m pytest'}, interactive=True)
    status = harness.format_status(store)
    assert 'LLM 审查官：放行 1' in status


# ---------- 运行 ----------

def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"[ERROR] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
