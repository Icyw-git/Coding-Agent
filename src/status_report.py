"""只读状态与诊断：/status、/sandbox 说明、/approvals、/cache profile 与 --doctor。

这里只做汇总和格式化，不持有运行时状态。路径、快照配置与后台计数器仍在
``harness`` 的全局区，所以统一经 ``_h()`` 在调用期读取；这样测试对
``harness.WORKDIR`` / ``harness.format_main_target`` 等 patch 点依旧生效。
"""
from __future__ import annotations

from pathlib import Path

import approval_view
import browser_profile
import credentials
import grants
import net_grants
import rules as user_rules
import sandbox
import screen_capture
import tool_cache
import workspace_diff
from exec_policy import (
    CLIPBOARD_ENV,
    DESKTOP_ENV,
    SCREEN_ENV,
    clipboard_tools_enabled,
    desktop_control_enabled,
    load_policy,
    screen_tools_enabled,
    set_tier,
)

SANDBOX_MODE_HELP = (
    ('read-only', '只放公认只读的命令，写改与其余命令都要审批'),
    ('workspace-write', '默认档：工作区内写改放行，破坏性命令询问'),
    ('danger-full-access', '取消提示级限制与工作区路径约束，只保留黑名单硬拒'),
)
APPROVAL_POLICY_HELP = (
    ('untrusted', '每条 bash 都要审批'),
    ('on-request', '默认档：只在策略层判定有风险时询问'),
    ('never', '不打断；除 danger-full-access 外，需要审批的操作直接失败'),
)


def _h():
    """延迟取 harness：模块导入期避免循环，调用期读到最新全局状态。"""
    import harness
    return harness


def format_sandbox_tier() -> str:
    """档位清单 + 当前选中项，/status 与 --doctor 都复用这段说明。"""
    policy = load_policy()
    lines = [
        f'  执行档位：{policy.sandbox_mode} · 审批档位：{policy.approval_policy}',
        f'  OS 沙箱：{sandbox.status_line()}（{sandbox.ENV}）',
    ]
    for title, options, current in (
            ('执行档位（/sandbox <档位>）', SANDBOX_MODE_HELP, policy.sandbox_mode),
            ('审批档位（/sandbox approvals <档位>）', APPROVAL_POLICY_HELP,
             policy.approval_policy)):
        lines.append(f'  {title}')
        lines.extend(f'    {"*" if name == current else " "}{name:<20}{note}'
                     for name, note in options)
    lines.append('  说明：档位是策略级判定（四个执行入口同一套规则），本身不做 OS 强制，'
                 '不隔离文件读取与网络。')
    lines.append('  OS 沙箱独立于此开关：require 时沙箱不可用则拒绝执行；'
                 '启用后命令以 Low IL 运行，工作区外写入被内核拒绝，读取与网络仍不隔离。')
    return '\n'.join(lines)


def apply_sandbox_tier(argument: str) -> str:
    """/sandbox <档位> 与 /sandbox approvals <档位>。参数已由 command_error 校验。"""
    # 档位名大小写不敏感（load_policy 也是 casefold 读的）；分支判定同样要归一，
    # 否则 '/sandbox APPROVALS never' 校验通过却走进执行档位分支。
    parts = [part.casefold() for part in argument.split()]
    previous = load_policy()
    if parts[0] == 'approvals':
        policy = set_tier(approval_policy=parts[1])
        changed = f'审批档位 → {policy.approval_policy}'
    else:
        policy = set_tier(sandbox_mode=parts[0])
        changed = f'执行档位 → {policy.sandbox_mode}'
    # 档位写进环境：load_policy 即时读取，下一条工具调用就生效，子进程也继承
    if policy != previous:
        # 换档本身就是一次安全状态变更：丢掉所有「本会话允许同类」预授权，
        # 否则切到 read-only 还会吃到 workspace-write 时批下的键（/v1/policy 同理）
        _h().hooks.GATE.revoke_session()
    return (f'  已切换 {changed}'
            f'（当前 {policy.sandbox_mode} · {policy.approval_policy}）')


def format_status(session_store) -> str:
    """Summarize local runtime state for the interactive REPL."""
    from turn_progress import relative_time

    h = _h()
    manager = h.get_checkpoint_manager(session_store)
    with h.cron_lock:
        cron_count = len(h.scheduled_jobs)
    with h.background_lock:
        background_count = len(h.background_tasks)
    activity = [f'{count} 个{label}' for label, count in (
        ('任务', len(h.list_tasks())), ('定时任务', cron_count),
        ('后台任务', background_count), ('协作成员', len(h.active_teammates))) if count]
    summary = session_store.session_summary()
    usage = session_store.usage_totals()   # 从 usage 事件聚合，跨重启准确
    policy = load_policy()
    shots = screen_capture.stats(h.SCREENSHOT_DIR)
    path_grants = grants.active_write_paths()
    grants_line = (f'\n  目录写授权：{len(path_grants)} 个（/grants 查看）'
                   if path_grants else '')
    screen_line = (
        f"\n  屏幕工具：开 · 截图 {shots['files']} 张"
        f"（{tool_cache.human_size(shots['bytes'])}，保留 7 天）"
        if screen_tools_enabled()
        else f'\n  屏幕工具：关（{SCREEN_ENV}=0）')
    clipboard_line = (
        '\n  剪贴板工具：开（读写都需审批）'
        if clipboard_tools_enabled()
        else f'\n  剪贴板工具：关（{CLIPBOARD_ENV}=0）')
    desktop_line = (
        '\n  桌面控制：开（每步需审批）'
        if desktop_control_enabled()
        else f'\n  桌面控制：关（设置 {DESKTOP_ENV}=1 启用）')
    rules_line = ('\n  ' + user_rules.load().describe()
                  + f'（{user_rules.path_for_display()}）')
    credentials_line = '\n  ' + credentials.describe()
    browser_line = '\n  ' + browser_profile.describe()
    allow_domains, deny_domains = net_grants.counts()
    domains_line = (f'\n  域名策略：允许 {allow_domains} · 拒绝 {deny_domains}'
                    if allow_domains or deny_domains
                    else '\n  域名策略：无（未知域名逐个审批）')
    usage_line = (f'\n  tokens：{h._fmt_tokens(usage["total"])}'
                  f'（输入 {h._fmt_tokens(usage["prompt"])} · 输出 {h._fmt_tokens(usage["completion"])}）'
                  f'{h._cache_hit_rate_line(usage)}'
                  if usage.get('total') else '')
    return (
        f"  会话：{summary['title']}\n"
        f"  最近活跃：{relative_time(summary['updated'])}\n"
        f'  工作区：{h.get_workdir()}\n'
        f'  模型：{h.format_main_target()}\n'
        f'  执行档位：{policy.sandbox_mode} · 审批：{policy.approval_policy} · '
        f'OS 沙箱：{sandbox.configured_mode()}'
        f'{h.hooks.GATE.judge_summary() if h.hooks.llm_approval_enabled() else " · LLM 审查官：关"}\n'
        f'  快照：{len(manager.list(limit=10_000))} 个\n'
        f'  任务概况：{" · ".join(activity) if activity else "暂无任务"}'
        f'{grants_line}'
        f'{screen_line}'
        f'{clipboard_line}'
        f'{desktop_line}'
        f'{domains_line}'
        f'{rules_line}'
        f'{credentials_line}'
        f'{browser_line}'
        f'{usage_line}'
    )


def format_workspace_diff(workspace=None, path='', *, max_chars=30000, color=False) -> str:
    """Return a bounded, read-only view of the workspace changes."""
    h = _h()
    return workspace_diff.format_diff(
        Path(workspace or h.get_workdir()), path,
        max_chars=max_chars, color=color, ignore=h.CHECKPOINT_IGNORE)


def format_pending_approvals() -> str:
    """Show actionable approval context without exposing internal request IDs."""
    pending = _h().hooks.GATE.pending()
    if not pending:
        return '  没有待审批操作。'
    lines = ['  待审批操作：']
    for index, request in enumerate(pending, 1):
        reason = str(request.reason or '').strip() or '未提供原因'
        tool = str(request.tool or '未知操作')
        lines.append(f'    {index}. {approval_view.safe_text(f"{tool} · {reason}")}')
        # 批的就是这条命令：只列工具名等于让人盲批（旧版就是这样）
        detail = approval_view.detail(tool, request.args)
        lines.extend(f'         {line}' for line in detail.splitlines())
    lines.append('  请使用飞书 /approve 或审批卡片处理；CLI 可回复 /approvals <序号> y|n 决议。')
    return '\n'.join(lines)


def storage_profile_data(*, deep: bool = True) -> dict:
    """扫描启动期绑定的 .cache；TUI、行式 REPL 与 --doctor 共用同一份口径。

    存储目录不随 ``/cd`` 迁移，所以根目录取自存储路径本身，而不是当前工作区。
    """
    import storage_profile

    h = _h()
    return storage_profile.scan(
        h.SESSIONS_DIR.parent, sessions_dir=h.SESSIONS_DIR,
        checkpoint_keep=h.CHECKPOINT_MAX_NODES, deep=deep)


def format_storage_profile(*, deep: bool = True) -> str:
    import storage_profile
    return storage_profile.format_profile(storage_profile_data(deep=deep))


def run_doctor() -> int:
    """Report startup prerequisites without contacting the model provider."""
    import llm_config
    import storage_profile

    h = _h()
    try:
        main = h.model_target('main')
        checks = [
            ('model', True),
            (f'model target {main.ref}', True),
            (main.api_key_env or 'api key (not required)',
             bool(main.api_key) or not main.api_key_env),
        ]
    except llm_config.ConfigError as exc:
        checks = [('model', False)]
        print(f'[model] {exc}')
    workspace = h.get_workdir()
    checks.extend([
        ('workspace', workspace.is_dir()),
        ('git repository', (workspace / '.git').exists()),
    ])
    cache_root = h.SESSIONS_DIR.parent
    print(f'[home] {h.HOME_DIR}（会话/记忆/快照的根，可用 HARNESS_HOME 覆盖）')
    print(f'[sessions] {h.SESSIONS_DIR}')
    for directory in (h.SESSIONS_DIR, h.CHECKPOINT_DIR, h.MEMORY_DIR,
                      h.TOOL_RESULTS_DIR, h.SCREENSHOT_DIR):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            try:
                label = directory.relative_to(cache_root)
            except ValueError:
                label = directory
            checks.append((f'writable {label}', True))
        except OSError:
            checks.append((f'writable {directory}', False))
    for label, passed in checks:
        print(f'[{"ok" if passed else "missing"}] {label}')
    # 档位属于运行时配置，诊断输出里必须看得见：否则「以为开了只读其实没开」
    # 这类误差只能在事故发生后才发现。
    policy = load_policy()
    print(f'[policy] sandbox_mode={policy.sandbox_mode} '
          f'approval_policy={policy.approval_policy}（策略级判定）')
    print(f'[os-sandbox] {sandbox.ENV}={sandbox.configured_mode()}'
          f'（Windows 低完整性，仅约束 shell 子进程）')
    print(f'[optional] documents: {_document_extras_status()}')
    cache = tool_cache.scan(h.TOOL_RESULTS_DIR)
    print(f"[cache] tool_results: {cache['files']} files, "
          f"{tool_cache.human_size(cache['bytes'])} (use /cache gc)")
    shots = screen_capture.stats(h.SCREENSHOT_DIR)
    state = 'on' if screen_tools_enabled() else f'off ({SCREEN_ENV}=0)'
    print(f"[screen] tools={state} · screenshots: {shots['files']} files, "
          f"{tool_cache.human_size(shots['bytes'])} (retained 7 days)")
    clip_state = ('on' if clipboard_tools_enabled()
                  else f'off ({CLIPBOARD_ENV}=0)')
    print(f'[clipboard] tools={clip_state} (read and write require approval)')
    active_grants = grants.active_write_paths()
    print(f'[grants] {len(active_grants)} active path grant(s)'
          + (': ' + ', '.join(str(path) for path in active_grants)
             if active_grants else ' (directory writes outside workspace still ask)'))
    try:
        print(storage_profile.format_summary(storage_profile_data(deep=False)))
    except Exception as exc:
        print(f'[storage] unavailable: {exc}')
    return 0 if all(passed for _, passed in checks) else 1


def _document_extras_status() -> str:
    """文档工具是可选依赖：doctor 只报告可用性，缺包不算启动失败。"""
    from importlib.util import find_spec

    missing = [
        label for label, module in (
            ('pypdf', 'pypdf'), ('pypdfium2', 'pypdfium2'), ('Pillow', 'PIL'))
        if find_spec(module) is None
    ]
    if not missing:
        return 'installed'
    return (f"missing {', '.join(missing)} "
            f'(pip install -r requirements-docs.txt)')
