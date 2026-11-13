"""诊断脚本不能污染真实授权账本（next-design §9.3 R0-b1）。

`scripts/profile_new_tools.py` 的 policy 阶段会：

    rules.write(rules.parse({...}))      # 覆盖用户的规则文件
    net_grants.add('*.example.com')      # 往用户的域名账本里塞一条 allow

它自称"只做只读操作"，但这两行会**改写真实授权**——跑一次基准，用户的自定义规则
被换成脚本里那两条，域名白名单里多出一个 `*.example.com`。这是设计文档点名的
"profile 会污染真实授权"。

守卫做法：把真实路径指到临时目录里的哨兵文件，跑一次 `profile_policy()`，
断言哨兵**一个字节都没变**、以及结束之后模块常量恢复原值。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'scripts' / 'profile_new_tools.py'


def _load_script():
    """把脚本当模块加载（不执行 main）。"""
    spec = importlib.util.spec_from_file_location('profile_new_tools_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def profile(monkeypatch):
    module = _load_script()
    monkeypatch.setattr(module, 'timed', lambda fn, repeat=10: (0.0, 0.0, 0.0, 0.0))
    return module


def test_policy_stage_does_not_touch_the_real_rule_file(tmp_path, monkeypatch, profile):
    import net_grants
    import rules

    real_rules = tmp_path / 'real_rules.yaml'
    real_rules.write_text('version: 1\nrules: []\n', encoding='utf-8')
    before_rules = real_rules.read_bytes()

    real_net = tmp_path / 'real_grants.json'
    real_net.write_text('{"version": 1, "domains": []}', encoding='utf-8')
    before_net = real_net.read_bytes()

    monkeypatch.setattr(rules, 'DURABLE_PATH', real_rules)
    monkeypatch.setattr(net_grants, 'DURABLE_PATH', real_net)
    monkeypatch.setattr(net_grants, 'LOCK_PATH', tmp_path / 'real.lock')

    profile.profile_policy()

    assert real_rules.read_bytes() == before_rules, '真实规则文件被诊断脚本改写了'
    assert real_net.read_bytes() == before_net, '真实域名账本被诊断脚本塞了记录'


def test_module_paths_are_restored_after_the_stage(tmp_path, monkeypatch, profile):
    """临时把常量指到别处之后必须还原：否则同进程后续判定会读错账本。"""
    import net_grants
    import rules

    original = (rules.DURABLE_PATH, net_grants.DURABLE_PATH, net_grants.LOCK_PATH)
    sentinel_dir = tmp_path / 'sentinel'
    monkeypatch.setattr(rules, 'DURABLE_PATH', sentinel_dir / 'rules.yaml')
    monkeypatch.setattr(net_grants, 'DURABLE_PATH', sentinel_dir / 'net.json')
    monkeypatch.setattr(net_grants, 'LOCK_PATH', sentinel_dir / 'net.lock')

    profile.profile_policy()

    assert rules.DURABLE_PATH == sentinel_dir / 'rules.yaml'
    assert net_grants.DURABLE_PATH == sentinel_dir / 'net.json'
    assert net_grants.LOCK_PATH == sentinel_dir / 'net.lock'
    assert not sentinel_dir.exists() or not list(sentinel_dir.glob('*')), \
        '诊断脚本在哨兵目录里留下了文件'
    assert original  # 记录用
