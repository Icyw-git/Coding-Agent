"""Claude Code 风格终端 UI（Python 3.9 兼容）。

复刻 CC 的完整视觉语言（Visual Theme 层）：
- 语义化颜色 token：品牌橙 / 文本色 / 语义色 / 权限色 / Diff 背景色 / 子代理 8 色板
- Markdown 终端渲染：标题 / 粗斜体 / 行内代码 / 代码块 / 列表 / 引用 / 链接 / 表格
- Spinner 信息通道：随机动词池 / 两档速度（thinking 200ms / requesting 50ms）/
  停滞检测（>8s 颜色渐变到错误红）
- 工具状态行 / 权限横幅 / bash 粉色边框 / Diff 背景高亮 / 会话状态行

编码自适应：UTF-8 终端用图形符号（• ▸ ┌─┐），GBK/管道环境自动降级为 ASCII，
避免 UnicodeEncodeError 崩掉整个会话。任意文本请用 ui.safe_print() 兜底。

用法：
    import cc_ui as ui
    ui.safe_print(ui.c('success', 'ok'))
    ui.safe_print(ui.markdown(response_text))
    with ui.Spinner('Thinking'): ...            # override → thinking 慢速
    with ui.Spinner(): ...                      # 无 label → 随机动词，requesting 快速
    print(ui.tool_line('bash', {'command': 'dir'}))
    print(ui.permission_banner('Permission needed', ['删除文件？']))
    print(ui.diff(git_diff_text))
    print(ui.statusline(model='x', git_branch='main', ctx_pct=42))
"""
import os
import random
import re
import shutil
import sys
import threading
import time

# ---------- ANSI 原语（含 NO_COLOR / 色级降级） ----------

_RESET = '\x1b[0m'


def _detect_color_level():
    """按环境决定色彩深度：NO_COLOR 优先，其次 COLORTERM/TERM。"""
    if os.environ.get('NO_COLOR') is not None:
        return 0
    ct = os.environ.get('COLORTERM', '').lower()
    if 'truecolor' in ct or '24bit' in ct:
        return 16777216
    term = os.environ.get('TERM', '')
    if '256color' in term:
        return 256
    return 16


_COLOR_LEVEL = _detect_color_level()


def _rgb_to_256(r, g, b):
    """24bit RGB → 256 色表索引（6x6x6 立方体 + 灰阶）。"""
    if abs(r - g) < 8 and abs(g - b) < 8:
        gray = round((r + g + b) / 30.69)
        return 232 + min(23, gray)
    def cube(v):
        return 0 if v < 48 else 1 if v < 115 else 2 if v < 155 else \
               3 if v < 195 else 4 if v < 235 else 5
    return 16 + 36 * cube(r) + 6 * cube(g) + cube(b)


def _ansi_16_code(rgb):
    """24bit RGB → ANSI16 前景码（30-37 / 90-97）。"""
    r, g, b = rgb
    luma = (r * 299 + g * 587 + b * 114) // 1000
    sat = max(r, g, b) - min(r, g, b)
    if sat < 30:  # 灰阶
        if luma < 60:
            return 30
        if luma < 140:
            return 90
        if luma < 210:
            return 37
        return 97
    base = 31 if max(r, g, b) == r else 32 if max(r, g, b) == g else 34
    return base + 60 if luma >= 128 else base


def _ansi_16_bg_code(rgb):
    """24bit RGB → ANSI16 背景码（40-47 / 100-107）。"""
    fg = _ansi_16_code(rgb)
    return fg - 30 + 40 + (60 if fg >= 90 else 0)


def _fg(rgb):
    if _COLOR_LEVEL >= 16777216:
        return '\x1b[38;2;{};{};{}m'.format(*rgb)
    if _COLOR_LEVEL >= 256:
        return '\x1b[38;5;{}m'.format(_rgb_to_256(*rgb))
    if _COLOR_LEVEL >= 16:
        return '\x1b[{}m'.format(_ansi_16_code(rgb))
    return ''


def _bg(rgb):
    if _COLOR_LEVEL >= 16777216:
        return '\x1b[48;2;{};{};{}m'.format(*rgb)
    if _COLOR_LEVEL >= 256:
        return '\x1b[48;5;{}m'.format(_rgb_to_256(*rgb))
    if _COLOR_LEVEL >= 16:
        return '\x1b[{}m'.format(_ansi_16_bg_code(rgb))
    return ''


def _bold():
    return '\x1b[1m'


def _italic():
    return '\x1b[3m'


def _dim():
    return '\x1b[2m'


def _underline():
    return '\x1b[4m'


# ---------- 编码自适应图形符号 ----------

def _can_encode(ch):
    try:
        ch.encode(sys.stdout.encoding or 'utf-8')
        return True
    except (UnicodeEncodeError, LookupError):
        return False


_is_unicode = _can_encode('\u2022') and _can_encode('\u250c')  # • 和 ┌

G = {
    'bullet':  '\u2022' if _is_unicode else '-',
    'tri':     '\u25b8' if _is_unicode else '>',
    'quote':   '\u258d' if _is_unicode else '>',
    'pipe':    '\u2502' if _is_unicode else '|',
    'tl':      '\u250c' if _is_unicode else '+',
    'th':      '\u2500' if _is_unicode else '-',
    'tr':      '\u2510' if _is_unicode else '+',
    'bl':      '\u2514' if _is_unicode else '+',
    'br':      '\u2518' if _is_unicode else '+',
    'warn':    '\u26a0' if _is_unicode else '!',
    'deny':    '\u26d4' if _is_unicode else 'X',
    'play':    '\u25b6' if _is_unicode else '>',
    'square':  '\u25a0' if _is_unicode else '#',
    'check':   '\u2713' if _is_unicode else 'v',
    'block':   '\u2588' if _is_unicode else '#',   # 进度条已用
    'hollow':  '\u2591' if _is_unicode else '.',   # 进度条未用
    'spinner': ('\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2827\u280f'
                if _is_unicode else '|/-\\'),
}

# ---------- 语义化颜色 token（Claude Code Dark 主题实际 RGB） ----------

TOKENS = {
    'claude':      (215, 119, 87),    # 品牌橙
    'text':        (255, 255, 255),   # 主文本
    'subtle':      (167, 163, 162),   # 微弱提示/边框
    'inactive':    (110, 106, 106),   # 非活跃/禁用
    'suggestion':  (120, 110, 170),
    'success':     (78, 186, 101),    # 成功绿
    'error':       (255, 107, 128),   # 错误红（暗红）
    'warning':     (255, 193, 7),     # 警告黄
    'info':        (106, 176, 255),
    'permission':  (177, 185, 249),   # 权限蓝紫
    'planMode':    (96, 165, 250),    # Plan 模式蓝
    'header':      (255, 220, 150),
    'code':        (255, 200, 130),
    'link':        (106, 176, 255),
    'quote':       (160, 160, 170),
    'diffAdded':   (34, 92, 43),      # 新增行背景（暗绿）
    'diffRemoved': (122, 41, 54),     # 删除行背景（暗红）
    'diffMeta':    (150, 150, 180),
    'diffInfo':    (120, 190, 250),
    'bashBorder':  (236, 183, 190),   # bash 输出边框（Claude Code 粉色）
    'gridHeader':  (255, 220, 150),
    # 子代理 8 色板（仅限子 Agent 使用，区分并行 Agent）
    'subagent': [
        (239, 68, 68), (249, 115, 22), (234, 179, 8), (34, 197, 94),
        (59, 130, 246), (99, 102, 241), (168, 85, 247), (236, 72, 153),
    ],
}

_is_tty = sys.stdout.isatty()


def t(name):
    """取命名 token 的前景色转义码。"""
    rgb = TOKENS.get(name, TOKENS['text'])
    return _fg(rgb)


def bg(name):
    """取命名 token 的背景色转义码。"""
    rgb = TOKENS.get(name, TOKENS['subtle'])
    return _bg(rgb)


def c(name, text):
    """给文本上命名颜色。"""
    return t(name) + str(text) + _RESET


def b(text):
    """加粗。"""
    return _bold() + str(text) + _RESET


def dim(text):
    """置灰。"""
    return _dim() + str(text) + _RESET


def bold_c(name, text):
    """加粗 + 命名颜色。"""
    return _bold() + t(name) + str(text) + _RESET


def rule(char=None, width=80):
    return t('inactive') + (char or G['th']) * width + _RESET


def status(text):
    """Claude 风格的次要状态行（▸ 前缀 + 灰字）。"""
    return t('claude') + G['tri'] + ' ' + _RESET + t('subtle') + text + _RESET


def safe_print(text):
    """打印任意文本，GBK 等窄编码环境不崩溃（替换不可编码字符）。"""
    enc = sys.stdout.encoding or 'utf-8'
    try:
        print(text)
    except UnicodeEncodeError:
        try:
            print(str(text).encode(enc, errors='replace').decode(enc, errors='replace'))
        except Exception:
            print(str(text).encode('ascii', errors='replace').decode('ascii', errors='replace'))


# ---------- Markdown 渲染 ----------

_INLINE_RE = re.compile(
    r'(\*\*[^*]+\*\*|__[^_]+__|\*[^*\n]+\*|`[^`\n]+`|~~[^~]+~~|\[[^\]\n]+\]\([^)\n]+\))'
)


def _inline(text):
    """渲染行内样式：加粗 / 斜体 / 代码 / 删除线 / 链接。"""
    out = []
    for tok in _INLINE_RE.split(text):
        if not tok:
            continue
        if tok.startswith('**') and tok.endswith('**'):
            out.append(_bold() + tok[2:-2] + _RESET)
        elif tok.startswith('__') and tok.endswith('__'):
            out.append(_bold() + tok[2:-2] + _RESET)
        elif tok.startswith('`') and tok.endswith('`'):
            out.append(t('code') + tok[1:-1] + _RESET)
        elif tok.startswith('~~') and tok.endswith('~~'):
            out.append(_dim() + tok[2:-2] + _RESET)
        elif tok.startswith('*') and tok.endswith('*'):
            out.append(_italic() + tok[1:-1] + _RESET)
        elif tok.startswith('['):
            m = re.match(r'\[([^\]\n]+)\]\(([^)\n]+)\)', tok)
            if m:
                # OSC 8 超链接：标准包裹格式 \x1b]8;;URL\x1b\\text\x1b]8;;\x1b\\
                out.append(_underline() + t('link') +
                           '\x1b]8;;' + m.group(2) + '\x1b\\' +
                           m.group(1) + '\x1b]8;;\x1b\\' + _RESET)
            else:
                out.append(tok)
        else:
            out.append(tok)
    return ''.join(out)


_PY_KEYWORDS = {
    'def', 'class', 'return', 'import', 'from', 'if', 'elif', 'else', 'for',
    'while', 'try', 'except', 'finally', 'with', 'as', 'lambda', 'and', 'or',
    'not', 'in', 'is', 'None', 'True', 'False', 'pass', 'break', 'continue',
    'raise', 'global', 'nonlocal', 'yield', 'assert', 'del',
}
_PY_RE = re.compile(
    r'("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"[^"\n]*"|\'[^\'\n]*\'|#[^\n]*'
    r'|\b\d+\b|\b[A-Za-z_]\w*\b)'
)


def _py_highlight(line):
    """Python 行最小语法高亮：字符串/注释/关键字/数字。"""
    out = []
    for tok in _PY_RE.split(line):
        if not tok:
            continue
        if tok.startswith(('#', '"', "'")):
            out.append(t('subtle') + tok + _RESET)
        elif tok.isdigit():
            out.append(t('info') + tok + _RESET)
        elif tok in _PY_KEYWORDS:
            out.append(t('permission') + tok + _RESET)
        else:
            out.append(t('code') + tok + _RESET)
    return ''.join(out)


def _code_block(lines, lang=''):
    """代码块：左置灰竖线 + 代码色内容；python 代码启用最小语法高亮。"""
    if not lines:
        return ''
    if lang in ('python', 'py'):
        return '\n'.join(
            t('subtle') + G['pipe'] + ' ' + _RESET + _py_highlight(line) + _RESET
            for line in lines
        )
    return '\n'.join(
        t('subtle') + G['pipe'] + ' ' + _RESET + t('code') + line + _RESET
        for line in lines
    )


def _table_grid(headers, rows):
    """宽屏表格：对齐网格，表头加粗 + 主题色，分隔线。"""
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(cell)))
    out = [bold_c('gridHeader', '  '.join(
        h.ljust(widths[i]) for i, h in enumerate(headers)))]
    out.append(t('inactive') + '  ' + '  '.join('-' * w for w in widths) + _RESET)
    for r in rows:
        out.append('  ' + '  '.join(
            (str(c).ljust(widths[i]) if i < len(widths) else str(c))
            for i, c in enumerate(r)))
    return '\n'.join(out)


def _table_records(headers, rows):
    """窄屏表格：每行转成 label: value 记录（Codex/Claude 响应式布局）。"""
    out = []
    for r in rows:
        for i, h in enumerate(headers):
            val = str(r[i]) if i < len(r) else ''
            out.append('  ' + bold_c('subtle', h + ': ') + t('text') + val + _RESET)
        out.append('')
    return '\n'.join(out).rstrip('\n')


def table(headers, rows, max_width=None):
    """响应式表格：终端够宽用网格，太窄自动转 label: value 记录。"""
    if max_width is None:
        try:
            max_width = shutil.get_terminal_size().columns
        except Exception:
            max_width = 80
    grid_w = sum(len(h) + 2 for h in headers) + len(headers)
    if grid_w > max_width - 4:
        return _table_records(headers, rows)
    return _table_grid(headers, rows)


def _table_block(lines, width):
    """把连续的 Markdown 表格行解析成响应式表格。lines[0]=表头, lines[1]=分隔线, 其余=行。"""
    def cells(line):
        return [c.strip() for c in line.strip().strip('|').split('|')]
    if not lines:
        return ''
    headers = cells(lines[0])
    rows = [cells(l) for l in lines[2:] if l.strip().strip('|').strip()]
    return table(headers, rows, max_width=width)


def markdown(text, width=80):
    """整段 Markdown 渲染：标题 / 引用 / 列表 / 表格 / 分隔线 / 代码块。"""
    if not text:
        return ''
    out = []
    in_code = False
    code_lang = ''
    code_buf = []
    lines = str(text).splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith('```'):
            if in_code:
                out.append(_code_block(code_buf, code_lang))
                code_buf = []
                in_code = False
                code_lang = ''
            else:
                in_code = True
                code_lang = stripped[3:].strip()
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue
        if stripped.startswith('#'):
            title = stripped.lstrip('#').strip()
            out.append(bold_c('header', title))
        elif stripped.startswith('> '):
            out.append(t('quote') + G['quote'] + ' ' + _inline(stripped[2:]) + _RESET)
        elif re.match(r'^[-*] ', stripped):
            out.append('  ' + t('claude') + G['bullet'] + _RESET + ' ' + _inline(stripped[2:]))
        elif re.match(r'^\d+\. ', stripped):
            m = re.match(r'^(\d+)\. ', stripped)
            out.append('  ' + t('subtle') + m.group(1) + '.' + _RESET + ' ' +
                       _inline(stripped[m.end():]))
        elif stripped in ('---', '***', '___'):
            out.append(rule(width=width))
        elif stripped.startswith('|') and stripped.endswith('|'):
            block = [stripped]
            j = i + 1
            while j < n:
                nxt = lines[j].lstrip()
                if nxt.startswith('|') and nxt.endswith('|'):
                    block.append(nxt)
                    j += 1
                else:
                    break
            out.append(_table_block(block, width))
            i = j
            continue
        elif not stripped:
            out.append('')
        else:
            out.append(_inline(line))
        i += 1
    if in_code:
        out.append(_code_block(code_buf, code_lang))
    return '\n'.join(out)


# ---------- 工具状态行（Claude 风格） ----------

def tool_line(name, args=None, preview='', preview_len=120):
    """工具执行行：`> bash  command=dir` + 灰字结果预览。"""
    args = args or {}
    arg_s = ' '.join('{}={}'.format(k, v) for k, v in args.items())
    head = t('claude') + '> ' + _RESET + bold_c('text', name)
    if arg_s:
        head += t('subtle') + '  ' + arg_s + _RESET
    out = [head]
    if preview:
        one_line = ' '.join(str(preview).split())[:preview_len]
        if one_line:
            out.append('  ' + dim(one_line))
    return '\n'.join(out)


# ---------- 权限横幅 ----------

def permission_banner(title, lines):
    """权限确认框：紫色边框 + 内容行。"""
    out = [t('permission') + G['tl'] + G['th'] + ' ' + title + ' ' +
           G['th'] * 30 + _RESET]
    for ln in lines:
        out.append(t('permission') + G['pipe'] + ' ' + _RESET + ln)
    out.append(t('permission') + G['bl'] + G['th'] * 50 + _RESET)
    return '\n'.join(out)


# ---------- Bash 输出框（Claude Code 粉色边框） ----------

def bash_box(command, output, max_lines=14, width=None):
    """bash 命令 + 粉色边框输出框；超长输出只保留前几行并提示。"""
    if width is None:
        try:
            width = shutil.get_terminal_size().columns
        except Exception:
            width = 80
    inner = max(20, width - 4)
    lines = str(output).splitlines()
    truncated = len(lines) > max_lines
    if truncated:
        lines = lines[:max_lines]
    body = '\n'.join(
        t('bashBorder') + G['pipe'] + _RESET + ' ' + line for line in lines)
    if truncated:
        body += '\n' + t('bashBorder') + G['pipe'] + _RESET + ' ' + \
            dim('... {0} more lines'.format(len(str(output).splitlines()) - max_lines))
    head = t('bashBorder') + G['tl'] + G['th'] + ' bash: ' + \
        command[:inner - 12] + _RESET
    tail = t('bashBorder') + G['bl'] + G['th'] * inner + _RESET
    return head + '\n' + body + '\n' + tail


# ---------- 状态行（claude-statusline 风格） ----------

MODEL_TIERS = {
    'flash':     'success',    # 快模型（Haiku 档）：绿
    'chat':      'info',       # 标准（Sonnet 档）：蓝
    'reasoner':  'permission', # 深度推理（Opus 档）：紫
}


def model_color(model):
    """按模型档位返回语义色名（Claude Code 按 tier 着色的约定）。"""
    m = (model or '').lower()
    for key, color in MODEL_TIERS.items():
        if key in m:
            return color
    return 'info'


def ctx_bar(pct, width=10):
    """上下文使用率填充条：███████░░░ 42%（claude-statusline 风格）。"""
    pct = max(0.0, min(100.0, float(pct or 0)))
    filled = int(round(pct / 100.0 * width))
    color = 'error' if pct > 75 else ('warning' if pct > 50 else 'success')
    bar = G['block'] * filled + G['hollow'] * (width - filled)
    return c(color, '[' + bar + ']') + ' ' + c(color, '{0}%'.format(int(pct)))


def statusline(model='', tools=0, ctx_pct=None, tokens='', workdir='',
               git_branch='', cost=None):
    """会话状态行：model(tier 色) / git 分支 / 工具数 / 上下文进度条 / 成本 / token。

    上下文使用率按阈值变色：>75% 红、>50% 黄、否则绿（Claude Code 状态行规则）。
    """
    parts = []
    if model:
        parts.append(bold_c(model_color(model), model))
    if git_branch:
        parts.append(t('subtle') + git_branch + _RESET)
    if tools:
        parts.append(t('subtle') + '{0} tools'.format(tools) + _RESET)
    if ctx_pct is not None:
        parts.append(ctx_bar(ctx_pct))
    if cost is not None:
        parts.append(c('success', '${0:.2f}'.format(cost)))
    if tokens:
        parts.append(t('subtle') + tokens + _RESET)
    if workdir:
        parts.append(t('inactive') + workdir + _RESET)
    if not parts:
        return ''
    return t('claude') + '│' + _RESET + ' ' + '  '.join(parts)


# ---------- Diff 高亮 ----------

def diff(text):
    """git diff 高亮：+++/--- 标题、@@ 元信息、+/- 行用暗绿/暗红背景（CC 官方色）。"""
    out = []
    for line in str(text).splitlines():
        if line.startswith('+++') or line.startswith('---'):
            out.append(bold_c('text', line))
        elif line.startswith('@@'):
            out.append(t('diffInfo') + line + _RESET)
        elif line.startswith('+'):
            out.append(bg('diffAdded') + line + _RESET)
        elif line.startswith('-'):
            out.append(bg('diffRemoved') + line + _RESET)
        elif line.startswith('diff --git') or line.startswith('index '):
            out.append(t('diffMeta') + line + _RESET)
        else:
            out.append(t('inactive') + line + _RESET)
    return '\n'.join(out)


# ---------- Spinner（仅 TTY，编码信息通道） ----------

def _interp(a, b, t):
    """两个 RGB 之间线性插值（用于停滞渐变）。"""
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


class Spinner:
    """API 调用加载动画（仅 TTY；管道/测试环境自动静默）。

    - 两档速度：有 label（系统准确文案）→ thinking 200ms 慢速；
      无 label → 从动词池随机取（requesting 50ms 快速闪烁）。
    - 停滞检测：超过 stall_after 秒未产出 → 颜色从主题色渐变到错误红。
    """

    VERBS = ['Pondering', 'Cooking', 'Thinking', 'Processing', 'Reticulating',
             'Gathering', 'Weighing', 'Synthesizing']

    def __init__(self, label=None, token='claude', stall_after=8.0, speed='auto'):
        self.label = label
        self.token = token
        self.stall_after = stall_after
        self.speed = speed  # 'auto' | 'thinking'(200ms) | 'requesting'(50ms)
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        if not _is_tty:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _interval(self):
        if self.speed == 'thinking':
            return 0.2
        if self.speed == 'requesting':
            return 0.05
        return 0.2 if self.label else 0.05

    def _run(self):
        frames = G['spinner']
        i = 0
        start = time.time()
        label = self.label or random.choice(self.VERBS) + '...'
        while not self._stop.is_set():
            elapsed = time.time() - start
            if elapsed > self.stall_after:
                # 停滞：超过阈值后随时间渐变到错误红
                ratio = min((elapsed - self.stall_after) / 20.0, 1.0)
                color = _fg(_interp(TOKENS.get(self.token, TOKENS['claude']),
                                    TOKENS['error'], ratio))
            else:
                color = t(self.token)
            frame = frames[i % len(frames)]
            sys.stdout.write('\r\x1b[2K' + color + frame + _RESET + ' ' + label)
            sys.stdout.flush()
            time.sleep(self._interval())
            i += 1

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)
            sys.stdout.write('\r\x1b[2K')
            sys.stdout.flush()
        return False
