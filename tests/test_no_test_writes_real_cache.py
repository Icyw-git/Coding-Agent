"""测试不得写进真实的 .cache（R0-b1 / R1-c 两次踩到）。

两次实例：

- `scripts/profile_new_tools.py` 会写真实 rules 与 net_grants（R0-b1 修）；
- 新增 `memory_tombstone.STORE_PATH` 后忘了隔离，一次全量跑把
  `.cache/memories_tombstones.json` 写进真实目录——而它会让同名记忆在之后所有
  会话里都召不回来。

守卫做法：跑完整个测试会话**之后**才看得出的问题，无法在单条用例里断言。
所以这里做两件事：

1. 断言 conftest 已经把已知的模块级路径隔离掉（新加的要在这里登记）；
2. 提供一条检查：`.cache` 下不应该出现墓碑文件——若出现，说明上一条失守。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_conftest_isolates_process_level_paths():
    """进程级路径常量必须被 conftest 指到 tmp_path。

    新增模块级路径时，把名字加进这个清单——它会提醒你同时加隔离。
    """
    source = (ROOT / 'tests' / 'conftest.py').read_text(encoding='utf-8')
    for name in ('memory_tombstone', 'cron_scheduler'):
        assert name in source, f'conftest 没有隔离 {name} 的模块级路径'


def test_no_tombstone_file_leaked_into_the_real_cache():
    """真实 .cache 里出现墓碑文件，说明某条用例没被隔离。"""
    leaked = ROOT / '.cache' / 'memories_tombstones.json'
    assert not leaked.exists(), (
        f'测试写进了真实数据目录：{leaked}。把它删掉，并在 conftest 里隔离该路径。')
