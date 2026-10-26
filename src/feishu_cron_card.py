"""定时任务卡片：列任务、按按钮授权副作用、删除前二次确认。

为什么授权要单独做成按钮：`schedule_cron` 是模型可调用的工具，如果它自带
`writes` 开关，模型就能给自己授权。这里把授权做成**人**在卡片上的显式点击，
模型侧始终只能创建只读任务。

按钮 `value` 带 `cron_view`（每次渲染的短 token）和 `job_id`；网关校验 token
属于本聊天且未过期，避免旧卡片或别的聊天重放按钮。

任务多了会挤爆卡片（手机上一屏放不下），所以按「最近触发」排序并分页：每页
`PAGE_SIZE` 个任务，底部给上一页/下一页；分页按钮带同一个 token 与目标页码。
"""

PAGE_SIZE = 3


def _text(content: str) -> dict:
    return {'tag': 'div',
            'text': {'tag': 'plain_text', 'content': content}}


def sort_jobs(jobs: list[dict]) -> list[dict]:
    """最近触发的排前面，从未触发的排最后（同组按 id 倒序，稳定可预期）。"""
    return sorted(jobs, key=lambda job: (str(job.get('last_fired') or ''),
                                         str(job.get('id') or '')), reverse=True)


def _page_button(label: str, *, page: int, token: str) -> dict:
    return {'tag': 'button',
            'text': {'tag': 'plain_text', 'content': label},
            'type': 'default',
            'value': {'cron_action': 'page', 'page': page, 'cron_view': token}}


def _button(label: str, *, action: str, job_id: str, token: str,
            primary: bool = False, danger: bool = False) -> dict:
    return {'tag': 'button',
            'text': {'tag': 'plain_text', 'content': label},
            'type': 'danger' if danger else ('primary' if primary else 'default'),
            'value': {'cron_action': action, 'job_id': job_id,
                      'cron_view': token}}


def _push_label(job: dict, chat_id: str) -> str:
    target = str(job.get('deliver_to') or '')
    if not target:
        return '不推送'
    if chat_id and target == chat_id:
        return '推送本聊天'
    return f'推送→{target[:8]}'


def _idle_label(job: dict) -> str:
    seconds = int(job.get('idle_after_s') or 0)
    if seconds <= 0:
        return ''
    return f'空闲≥{max(1, seconds // 60)}分'


def _job_lines(job: dict, chat_id: str) -> list[str]:
    kind = 'recurring' if job.get('recurring') else '一次性'
    mode = '可写（副作用仍审批）' if job.get('allow_side_effects') else '只读'
    idle = _idle_label(job)
    gate = f' · {idle}' if idle else ''
    prompt = ' '.join(str(job.get('prompt') or '').split())
    if len(prompt) > 80:
        prompt = prompt[:80] + '…'
    return [f"{job.get('id', '')}",
            f"{job.get('cron', '')} · {kind} · {mode}{gate} · "
            f"{_push_label(job, chat_id)}",
            prompt]


def cron_card(jobs: list[dict], *, chat_id: str = '', token: str = '',
              intro: str = '', page: int = 0) -> dict:
    """定时任务卡片；每个任务带「允许写 / 收回写 / 删除」，超出时分页。"""
    elements: list[dict] = []
    if intro:
        elements.append(_text(intro))
    if not jobs:
        elements.append(_text('（没有定时任务）\n'
                              '可以直接说“每天早上 9 点提醒我…”，我会自动创建。'))
        return {
            'config': {'wide_screen_mode': True},
            'header': {'title': {'tag': 'plain_text', 'content': '定时任务'}},
            'elements': elements,
        }
    rows = sort_jobs(jobs)
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, int(page or 0)), pages - 1)
    if pages > 1:
        elements.append(_text(f'共 {len(rows)} 个任务 · 第 {page + 1}/{pages} 页'
                              '（按最近触发排序）'))
    for job in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        job_id = str(job.get('id') or '')
        elements.append(_text(chr(10).join(_job_lines(job, chat_id))))
        buttons = []
        if job.get('allow_side_effects'):
            buttons.append(_button('收回写权限', action='readonly',
                                   job_id=job_id, token=token))
        else:
            buttons.append(_button('允许写操作', action='writes',
                                   job_id=job_id, token=token))
        buttons.append(_button('删除', action='delete', job_id=job_id,
                               token=token, danger=True))
        elements.append({'tag': 'action', 'actions': buttons})
        elements.append({'tag': 'hr'})
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_page_button('← 上一页', page=page - 1, token=token))
        if page + 1 < pages:
            nav.append(_page_button('下一页 →', page=page + 1, token=token))
        if nav:
            elements.append({'tag': 'action', 'actions': nav})
            elements.append(_text(f'第 {page + 1}/{pages} 页'))
    return {
        'config': {'wide_screen_mode': True},
        'header': {'title': {'tag': 'plain_text', 'content': '定时任务'}},
        'elements': elements,
    }


def cron_delete_card(job: dict, *, token: str = '') -> dict:
    """删除前确认：删除不可恢复，必须先点一次「确认删除」。"""
    job_id = str(job.get('id') or '')
    prompt = ' '.join(str(job.get('prompt') or '').split())
    if len(prompt) > 80:
        prompt = prompt[:80] + '…'
    return {
        'config': {'wide_screen_mode': True},
        'header': {'title': {'tag': 'plain_text', 'content': '确认删除定时任务'}},
        'elements': [
            _text(f"{job_id}\n{job.get('cron', '')}\n{prompt}\n\n"
                  '删除后不可恢复；已产生的推送记录不受影响。'),
            {'tag': 'action', 'actions': [
                _button('确认删除', action='delete_confirm', job_id=job_id,
                        token=token, danger=True),
                _button('取消', action='cancel', job_id=job_id, token=token),
            ]},
        ],
    }


def cron_text(jobs: list[dict], *, chat_id: str = '') -> str:
    """没有卡片能力时的纯文本回退。"""
    if not jobs:
        return '（没有定时任务）'
    lines = ['定时任务:']
    for job in jobs:
        kind = 'recurring' if job.get('recurring') else '一次性'
        mode = '可写' if job.get('allow_side_effects') else '只读'
        idle = _idle_label(job)
        gate = f' · {idle}' if idle else ''
        prompt = ' '.join(str(job.get('prompt') or '').split())[:40]
        lines.append(f"  {job.get('id')}: '{job.get('cron')}' → {prompt} "
                     f"[{kind} · {mode}{gate} · {_push_label(job, chat_id)}]")
    return chr(10).join(lines)
