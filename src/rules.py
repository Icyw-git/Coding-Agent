"""用户规则文件：把「这条命令以后别问我 / 以后别执行」写成可撤销的文本规则。

设计边界（顺序很重要，先读这一段再改）：

1. **规则只能收窄或提前决定「问不问」，不能改变硬拒。** 黑名单、根删除、秘密外发
   这类硬拒在策略层先判，规则文件拿不到它们——`allow` 不可能让 `rm -rf /` 变合法。
2. **allow 只对「本来就会询问」的操作有意义。** 对 ALLOW 的调用再 allow 是空操作，
   对 DENY 的调用直接忽略；规则链只在结果仍是 ASK 时生效。
3. **deny 立即生效，且比 allow 优先。** 同一层级同时命中时按拒绝处理；用户想
   「先禁掉某个工具」不该要先删掉另一条 allow。
4. **作用域分层**：`global`（所有会话）与 `session`（只对本会话）。会话级规则在
   会话结束时自然失效，不需要额外清理；`/rules` 只列全局规则。
5. **多段命令必须每段都命中 allow。** 这一点与 `approval_class` 的一致性判定同源：
   `ls && rm -rf ~` 里只有第一段命中 allow，整条命令仍然要问。
6. **文件坏了不影响启动。** 解析失败打印一行后当作「没有规则」，而不是让 agent
   起不来或静默启用半份规则。

文件格式（YAML，路径 `<home>/.cache/rules.yaml`）：

    version: 1
    rules:
      - decision: allow
        tools: [bash]
        prefixes: ["git status", "git diff", "npm test"]
        note: 只读 git 与测试
      - decision: deny
        tools: [bash]
        prefixes: ["npm publish"]
        note: 绝不自动发布

`prefixes` 按「命令词序列前缀」匹配，不做子串匹配：`git status` 命中
`git status -s`，但不命中 `git statusx`，也不命中 `echo git status`。
`tools` 省略时默认只看 `bash`；`*` 表示所有工具（仅对 allow/deny 的
「询问」阶段有效）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import paths

VERSION = 1
DURABLE_PATH = paths.cache_path('rules.yaml')
ALLOW = 'allow'
ASK = 'ask'
DENY = 'deny'
DECISIONS = (ALLOW, ASK, DENY)
SCOPE_GLOBAL = 'global'
SCOPE_SESSION = 'session'
SCOPES = (SCOPE_GLOBAL, SCOPE_SESSION)
MAX_RULES = 100
MAX_PREFIXES = 20
MAX_PREFIX_CHARS = 200
MAX_NOTE_CHARS = 200

# 规则**可以**改写的决策码。白名单而不是黑名单，这是刻意的：
# 以后新增任何决策码都会自动受保护，而不是因为忘了加进黑名单被规则悄悄放宽。
#
# 这三类恰好是「噪音」而不是「边界」：
#   destructive    —— 破坏性词误伤（`npm test` 撞上 `npm`）
#   opaque_command —— 解释器/编码载荷，静态判不了内容
#   external_tool  —— MCP 工具的副作用对策略层不可知
#
# 刻意**不**在这里、因而规则改不动的：
#   sandbox_read_only     档位是用户的显式选择，规则不能偷偷给它开权限
#   untrusted             同上，「每条都问」是档位
#   network_egress        外发边界由域名账本管（可审计、可撤销、有卡片）
#   secret_egress         秘密外发
#   outside_workspace_write 越界写由目录授权账本管
#   screen_* / clipboard_* / desktop_control_*  「每次都问」是能力定义
#   deny_* / secret_path / system_path / invalid_*  硬拒与参数错误
RULEABLE_CODES = frozenset({'destructive', 'opaque_command', 'external_tool'})

# 命令文本规范化：折叠空白、去引号外壳，避免 `git   status` 与 `git status`
# 被当成两条不同规则。
_WHITESPACE = re.compile(r'\s+')


class RuleError(ValueError):
    """规则文件内容不合法。"""


@dataclass(frozen=True)
class Rule:
    decision: str
    tools: tuple[str, ...] = ('bash',)
    prefixes: tuple[str, ...] = ()
    note: str = ''
    scope: str = SCOPE_GLOBAL
    source: str = ''

    def matches_tool(self, tool: str) -> bool:
        return '*' in self.tools or tool in self.tools

    def matches_command(self, command: str) -> bool:
        """命令的每一段都要以某个前缀开头才命中（多段命令不能只靠第一段）。"""
        normalized = normalize_command(command)
        if not normalized or not self.prefixes:
            return False
        segments = split_segments(normalized)
        if not segments:
            return False
        return all(any(_prefix_match(prefix, segment) for prefix in self.prefixes)
                   for segment in segments)


@dataclass
class RuleSet:
    """一次加载的结果：全局规则 + 会话规则，附加载错误。"""
    rules: list[Rule] = field(default_factory=list)
    error: str = ''

    def for_scope(self, scope: str) -> list[Rule]:
        return [rule for rule in self.rules if rule.scope == scope]

    def describe(self) -> str:
        if self.error:
            return f'规则文件：加载失败（{self.error}）'
        if not self.rules:
            return '规则文件：无'
        allowed = sum(1 for rule in self.rules if rule.decision == ALLOW)
        denied = sum(1 for rule in self.rules if rule.decision == DENY)
        asked = sum(1 for rule in self.rules if rule.decision == ASK)
        return f'规则文件：允许 {allowed} · 询问 {asked} · 拒绝 {denied}'


def normalize_command(command: str) -> str:
    text = str(command or '').strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in '"\'':
        text = text[1:-1]
    return _WHITESPACE.sub(' ', text).strip()


def split_segments(command: str) -> list[str]:
    """按 shell 的分段符切分命令（与 approval_class 的语义保持一致）。"""
    from policy_bash import _segments

    return _segments(normalize_command(command))


def _prefix_match(prefix: str, segment: str) -> bool:
    prefix = normalize_command(prefix)
    segment = normalize_command(segment)
    if not prefix or not segment:
        return False
    if segment == prefix:
        return True
    if not segment.startswith(prefix):
        return False
    # 必须在「词边界」收尾：`git status` 匹配 `git status -s`，不匹配 `git statusx`。
    return segment[len(prefix)] in ' \t;|&'


def _clean_prefix(raw) -> str:
    text = normalize_command(raw)
    if not text:
        raise RuleError('prefix must not be empty')
    if len(text) > MAX_PREFIX_CHARS:
        raise RuleError(f'prefix is longer than {MAX_PREFIX_CHARS} characters')
    return text


def _clean_tools(raw) -> tuple[str, ...]:
    if raw is None:
        return ('bash',)
    if isinstance(raw, str):
        items = [item.strip() for item in raw.split(',') if item.strip()]
    elif isinstance(raw, (list, tuple)):
        items = [str(item).strip() for item in raw if str(item).strip()]
    else:
        raise RuleError('tools must be a string or a list')
    if not items:
        return ('bash',)
    for item in items:
        if len(item) > 64:
            raise RuleError('tool name is too long')
    return tuple(items)


def parse(data) -> list[Rule]:
    """把已解析的 YAML 结构转成规则列表；任何格式问题都抛 RuleError。"""
    if not isinstance(data, dict):
        raise RuleError('rules file must be a mapping')
    if data.get('version') != VERSION:
        raise RuleError(f'unsupported rules version: {data.get("version")!r}')
    raw_rules = data.get('rules')
    if raw_rules is None:
        return []
    if not isinstance(raw_rules, list):
        raise RuleError('rules must be a list')
    if len(raw_rules) > MAX_RULES:
        raise RuleError(f'refusing more than {MAX_RULES} rules')
    rules: list[Rule] = []
    for index, item in enumerate(raw_rules, 1):
        if not isinstance(item, dict):
            raise RuleError(f'rule #{index} must be a mapping')
        decision = str(item.get('decision') or '').strip().casefold()
        if decision not in DECISIONS:
            raise RuleError(f'rule #{index}: decision must be one of '
                            f'{", ".join(DECISIONS)}')
        scope = str(item.get('scope') or SCOPE_GLOBAL).strip().casefold()
        if scope not in SCOPES:
            raise RuleError(f'rule #{index}: scope must be one of '
                            f'{", ".join(SCOPES)}')
        raw_prefixes = item.get('prefixes')
        if not isinstance(raw_prefixes, list) or not raw_prefixes:
            raise RuleError(f'rule #{index}: prefixes must be a non-empty list')
        if len(raw_prefixes) > MAX_PREFIXES:
            raise RuleError(f'rule #{index}: too many prefixes')
        prefixes = tuple(_clean_prefix(value) for value in raw_prefixes)
        tools = _clean_tools(item.get('tools'))
        note = str(item.get('note') or '')[:MAX_NOTE_CHARS]
        rules.append(Rule(decision=decision, tools=tools, prefixes=prefixes,
                          note=note, scope=scope, source=str(DURABLE_PATH)))
    return rules


def load(path=None) -> RuleSet:
    """读取规则文件；缺失或损坏都返回空规则集，不抛给调用方。"""
    target = Path(path) if path is not None else Path(DURABLE_PATH)
    if not target.exists():
        return RuleSet()
    try:
        import yaml

        text = target.read_text(encoding='utf-8-sig')
        data = yaml.safe_load(text)
    except Exception as exc:      # 缺 yaml / 语法错 / 读失败：都不该挡住启动
        message = f'{type(exc).__name__}: {exc}'
        print(f'  \033[33m[rules] {target} 加载失败，忽略全部规则（{message}）\033[0m')
        return RuleSet(error=message)
    try:
        rules = parse(data or {'version': VERSION, 'rules': []})
    except RuleError as exc:
        print(f'  \033[33m[rules] {target} 内容无效，忽略全部规则（{exc}）\033[0m')
        return RuleSet(error=str(exc))
    return RuleSet(rules=rules)


def explain(ruleset: RuleSet, tool: str, args: dict) -> tuple[str, str]:
    """返回 ``(decision, reason)``；无规则命中时 decision 为 ``''``。

    deny 先于 allow：同一批规则里同时命中时按拒绝处理。``ask`` 只是把决策
    固定成「问」，用来抵消更上层的自动放行（例如会话白名单之外的同类键）。
    """
    command = str((args or {}).get('command') or '')
    verdict = ''
    reason = ''
    for rule in ruleset.rules:
        if not rule.matches_tool(tool):
            continue
        if tool != 'bash':
            # 非 bash 工具没有命令文本：按工具名整体命中，前缀无意义。
            hit = bool(rule.prefixes)
        else:
            hit = rule.matches_command(command)
        if not hit:
            continue
        if rule.decision == DENY:
            return DENY, rule.note or f'denied by rule ({rule.prefixes[0]})'
        if rule.decision == ASK and not verdict:
            verdict, reason = ASK, rule.note or f'asked by rule ({rule.prefixes[0]})'
        elif rule.decision == ALLOW and not verdict:
            verdict, reason = ALLOW, rule.note or f'allowed by rule ({rule.prefixes[0]})'
    return verdict, reason


def apply(decision, ruleset: RuleSet, tool: str, args: dict, *,
          allow_permitted: bool = True):
    """把规则作用在策略层的结论上；返回新的 Decision。

    ``allow_permitted=False`` 用来表达「当前档位不允许规则放宽」：`read-only`
    与 `untrusted` 是用户的显式选择，规则文件不能替他把档位改回去。注意这与
    ``destructive`` 的判定顺序有关——read-only 档下 `rm -rf build` 先命中
    「破坏性命令询问」，如果这里放行，read-only 就形同虚设，所以必须在
    规则层挡住，而不是指望策略层换个 code。

    ``deny`` 规则不受这个开关影响：收窄永远是被允许的方向。
    """
    if ruleset.error or not ruleset.rules:
        return decision
    if decision.code not in RULEABLE_CODES:
        return decision
    if decision.action == 'deny':
        return decision          # 硬拒不可被规则放宽
    verdict, reason = explain(ruleset, tool, args)
    if not verdict:
        return decision
    from exec_policy import Decision

    if verdict == DENY:
        return Decision('deny', 'rule_denied', f'blocked by rule: {reason}')
    if verdict == ASK:
        if decision.action == 'ask':
            return decision      # 已经是询问，不必改文案
        return Decision('ask', 'rule_ask', f'must ask by rule: {reason}')
    # ALLOW：只把「本来会询问」的调用改成放行，且必须当前档位允许放宽。
    if not allow_permitted:
        return decision
    if decision.action == 'ask':
        return Decision('allow', 'rule_allowed', f'allowed by rule: {reason}')
    return decision


def write(rules: list[Rule], path=None) -> None:
    """把规则列表写回文件（只给人用的管理入口，模型没有对应工具）。"""
    import yaml

    from file_lock import exclusive_file_lock

    target = Path(path) if path is not None else Path(DURABLE_PATH)
    payload = {'version': VERSION, 'rules': [
        {'decision': rule.decision,
         'tools': list(rule.tools),
         'prefixes': list(rule.prefixes),
         **({'note': rule.note} if rule.note else {}),
         **({'scope': rule.scope} if rule.scope != SCOPE_GLOBAL else {})}
        for rule in rules
    ]}
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_suffix('.lock')
    with exclusive_file_lock(lock):
        tmp = target.with_suffix('.tmp')
        tmp.write_text(text, encoding='utf-8')
        tmp.replace(target)


def path_for_display() -> Path:
    return Path(os.fspath(DURABLE_PATH))
