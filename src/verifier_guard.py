"""评估器保护：改测试、改 CI、改 lint 配置只能人自己做。

为什么这条比其他安全规则更本质
------------------------------
我们所有的"改进了没有"的判断，最终都落到一个外部信号上：
`pytest 全绿`、`ruff 无错`、benchmark 数字下降。

RSI 领域公认的唯一诚实前提是：

    A frozen model with an **honest verifier** and a writable environment can
    climb on its own.

一旦 agent 能改 `tests/`，`pytest 全绿` 就不再是证据——它可以删掉失败用例、
放宽断言、跳过测试，然后"证明"自己变好了。这是奖励作弊的教科书路径：

    The moment the system owns the ruler, it will get very good at *looking*
    improved.

所以这条规则不是"别乱动用户文件"，而是**保护整个自我改进机制的地基**。

为什么不用普通审批
------------------
普通 ASK 可以被说服（"用户已经同意了"、"这只是修一个过时的断言"）。而改测试
恰恰是**听起来最合理**的那一类请求。所以它必须走第四档（`MUST_DO_SELF`）：
不进审批队列、不看档位、不看规则文件、不看会话白名单。

与既有规则的关系
----------------
- `.git`/`.hg`/`.svn` 归 `PATH_SECRET`（改完立即生效，比如 pre-commit hook）
- 这里是同一思路：**改动会立即影响"什么算正确"的东西，最危险**

范围
----
只拦**修改**（write/edit/apply_patch/delete）。读测试是正常的（agent 需要看断言
来理解期望行为），不拦。
"""
from __future__ import annotations

import os
import re

#: 保护路径（按路径段匹配，大小写不敏感；Windows 反斜杠已归一）。
#: 注意只列"影响判定"的，不列代码目录——代码本来就是给 agent 改的。
PROTECTED_SEGMENTS = frozenset({
    'tests',            # 测试：唯一的诚实评估器
    'test',             # 少数项目用单数
    'conftest.py',
    'pytest.ini',
    'tox.ini',
    'noxfile.py',
    'ruff.toml',
    '.ruff.toml',
    '.flake8',
    'mypy.ini',
    '.pre-commit-config.yaml',
})

#: 保护的文件名模式（**任意目录**下都保护：这些文件会影响全局收集/判定）。
#: 不把 `test_*.py` 放在这里：本仓 pytest 只收 `tests/`（testpaths），
#: 全仓型号会把 `src/test_helpers.py` 这类非测试文件也硬拒掉，反而阻碍正常工作。
PROTECTED_PATTERNS = (
    re.compile(r'(?i)^conftest\.py$'),
    re.compile(r'(?i)^pytest\.ini$'),
    re.compile(r'(?i)^tox\.ini$'),
    re.compile(r'(?i)^noxfile\.py$'),
    re.compile(r'(?i)^ruff\.toml$'),
    re.compile(r'(?i)^\.ruff\.toml$'),
    re.compile(r'(?i)^\.flake8$'),
    re.compile(r'(?i)^mypy\.ini$'),
)

#: 测试文件的文件名模式。只在**测试目录内或仓库根目录**生效（见 is_protected）。
TEST_FILE_PATTERNS = (
    re.compile(r'(?i)^test_.*\.py$'),
    re.compile(r'(?i)^.*_test\.py$'),
)

#: CI 目录（任意层级的 .github/workflows 等）。改 CI 等于改"什么算通过"。
PROTECTED_DIRS = (
    ('.github', 'workflows'),
    ('.gitlab', 'ci'),
)

#: 会改动文件内容的工具。
WRITE_TOOLS = frozenset({'write', 'edit', 'apply_patch', 'delete_paths'})

ENV = 'HARNESS_VERIFIER_GUARD'

_TEMPLATE = (
    'Refused: {target} is part of the evaluation surface (tests / CI / lint '
    'config) and must be changed by the user personally. This is not an '
    'approval question — if I could edit the tests, "all tests pass" would stop '
    'being evidence that anything improved. Report what you think is wrong and '
    'let the user decide.'
)


def enabled() -> bool:
    """事故排查开关，默认开。与 never_do 同一定位：不是普通配置项。"""
    return os.getenv(ENV, '1').strip().lower() not in ('0', 'off', 'false', 'no')


def _normalize(path: str) -> list[str]:
    """路径归一成小写段列表；去掉盘符、`.`、`..` 之外的噪音。"""
    text = str(path or '').strip().replace('\\', '/')
    text = re.sub(r'^[A-Za-z]:/', '', text)
    return [part for part in text.split('/') if part and part not in ('.', '..')]


def is_protected(path: str) -> bool:
    """这个路径是不是评估面的一部分。"""
    if not path:
        return False
    segments = _normalize(path)
    if not segments:
        return False
    lowered = [part.casefold() for part in segments]
    for segment in lowered:
        if segment in PROTECTED_SEGMENTS:
            return True
    name = lowered[-1]
    if any(pattern.match(name) for pattern in PROTECTED_PATTERNS):
        return True
    # 测试文件名：只在仓库根目录（深度 1）额外判。测试目录内的已由段名拦下，
    # 深层同名文件（如 src/test_helpers.py）不一定是测试，不硬拒。
    if len(lowered) <= 1 and any(
            pattern.match(name) for pattern in TEST_FILE_PATTERNS):
        return True
    # CI 目录：连续两段匹配才算（避免误伤名叫 .github 的普通文件）
    for lead, tail in PROTECTED_DIRS:
        for index in range(len(lowered) - 1):
            if lowered[index] == lead and lowered[index + 1] == tail:
                return True
    return False


def check(tool: str, args: dict) -> str:
    """命中返回拒绝语，否则空串。只拦写操作，读测试不拦。"""
    if not enabled() or tool not in WRITE_TOOLS:
        return ''
    if not isinstance(args, dict):
        return ''
    targets: list[str] = []
    for key in ('path', 'file_path', 'filename'):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            targets.append(value)
    # delete_paths 收的是列表
    for value in (args.get('paths') or []):
        if isinstance(value, str) and value.strip():
            targets.append(value)
    # apply_patch 的正文里带路径，只能粗查：见 delta 里是否出现受保护路径
    patch = args.get('patch')
    if isinstance(patch, str) and patch:
        for line in patch.splitlines():
            if line.startswith(('*** Update File:', '*** Add File:',
                                '*** Delete File:')):
                targets.append(line.split(':', 1)[-1].strip())

    for target in targets:
        if is_protected(target):
            return _TEMPLATE.format(target=target)
    return ''


def protected_paths() -> list[str]:
    """给 `/status` 与文档用的清单。"""
    return sorted(PROTECTED_SEGMENTS) + ['*.github/workflows/*']
