"""审批弹窗：y 允许 / n|Esc 拒绝，200ms 防误触。"""
from rich.markup import escape as markup_escape
from rich.text import Text
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static

from ui_events import bus


class PermissionModal(ModalScreen):
    """权限确认覆盖层：y 允许 / n|Esc 拒绝。应答后 dismiss（gate 自动唤醒 agent 线程）。

    200ms 防误触：弹窗后 0.2s 内按键无效（对应 CC 的 anti-misclick 设计）。"""

    BINDINGS = [
        ("y", "approve", "Allow"),
        ("n", "reject", "Deny"),
        ("escape", "reject", "Deny"),
    ]

    ARM_DELAY = 0.2

    def __init__(self, req_id: str, tool: str, args: dict, title: str = 'Permission needed',
                 risk: str = 'warning'):
        super().__init__()
        self.req_id = req_id
        self.tool = tool
        self.args = args
        self.title = title
        self.risk = risk if risk in ('warning', 'danger') else 'warning'
        self._armed = False

    def on_mount(self) -> None:
        self.set_timer(self.ARM_DELAY, self._arm)

    def _arm(self) -> None:
        self._armed = True

    def compose(self) -> ComposeResult:
        with Static(id="perm-box", classes=f"risk-{self.risk}"):
            label = 'HIGH RISK' if self.risk == 'danger' else 'CONFIRM ACTION'
            title_color = 'red' if self.risk == 'danger' else 'yellow'
            yield Static(f"[{title_color}][bold]{label}[/]  {markup_escape(self.title)}[/]",
                         id="perm-title")
            yield Static(Text("Tool: ", style="dim") + Text(self.tool))
            yield Static(Text(self._detail_text()))
            yield Static("")
            yield Static("[yellow]y[/] = Allow    [yellow]n[/]/[yellow]Esc[/] = Deny    "
                         f"([dim]{int(self.ARM_DELAY * 1000)}ms 防误触[/])")

    def _detail_text(self) -> str:
        """审批前给出可判断的上下文：补丁/内容预览优先于原始参数。"""
        patch = str(self.args.get('patch') or '')
        if patch:
            return 'Patch (preview):' + chr(10) + self._clip(patch)
        content = str(self.args.get('content') or '')
        if content and self.tool in ('write', 'edit'):
            return 'Content (preview):' + chr(10) + self._clip(content)
        return 'Target: ' + self._target_text()

    @staticmethod
    def _clip(text: str, lines: int = 20, chars: int = 1600) -> str:
        rows = text.splitlines()
        shown = chr(10).join(rows[:lines])
        if len(rows) > lines:
            shown += chr(10) + f'… {len(rows) - lines} more lines'
        if len(shown) > chars:
            shown = shown[:chars] + '…'
        return shown

    def _target_text(self) -> str:
        """Prefer the actionable command/path over a noisy raw argument dict."""
        for key in ('command', 'path', 'file_path', 'pattern'):
            if self.args.get(key):
                return str(self.args[key])
        return str(self.args)

    def action_approve(self):
        if not self._armed:
            return
        bus.emit('permission_resolve', req_id=self.req_id, approved=True)
        self.dismiss()

    def action_reject(self):
        if not self._armed:
            return
        bus.emit('permission_resolve', req_id=self.req_id, approved=False)
        self.dismiss()
