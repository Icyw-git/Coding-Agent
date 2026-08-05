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
    ui.say('success', 'done')                   # 便捷打印一行

与 agent_loop 的集成时机（对应 hook 调用点）：
    - 调 API 前：with ui.Spinner(label='Thinking'): response = ...   # 覆盖慢速
    - 工具执行：ui.safe_print(ui.tool_line(name, args, preview=output[:120]))
    - 权限询问：ui.safe_print(ui.permission_banner('Permission needed', [tool, str(args)]))
    - 大段输出：ui.safe_print(ui.bash_box(command, output)) 或 ui.safe_print(ui.diff(diff_text))
    - 会话状态：ui.safe_print(ui.statusline(model=..., git_branch=..., ctx_pct=..., tokens=...))
    - 长任务：bar = ui.ProgressBar(); bar.update(40); ...; bar.finish()
"""
import os
import random
import re
import shutil
import sys
import threading
import time
from typing import List, Optional

# ---------- ANSI 原语（含 NO_COLOR / 色级降级） ----------

_RESET = '\x1b[0m'


def _detect_color_level():
    """按环境决定色彩深度：NO_COLOR 优先，其次 FORCE_COLOR / COLORTERM / WT_SESSION / TERM。"""
    if os.environ.get('NO_COLOR') is not None:
        return 0
    if os.environ.get('FORCE_COLOR'):  # CI 强制启用彩色
        return 16777216
    ct = os.environ.get('COLORTERM', '').lower()
    if 'truecolor' in ct or '24bit' in ct:
        return 16777216
    # Windows Terminal / ConEmu 默认支持真彩色，但通常不设 COLORTERM
    if os.environ.get('WT_SESSION') or os.environ.get('ConEmuANSI'):
        return 16777216
    term = os.environ.get('TERM', '')
    if '256color' in term:
        return 256
    return 16


_COLOR_LEVEL = _detect_color_level()


def reload_color_level() -> None:
    """运行时重新检测色彩深度：修改 NO_COLOR / FORCE_COLOR / TERM 后调用生效。"""
    global _COLOR_LEVEL
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
    return (fg % 10) + 40 + (60 if fg >= 90 else 0)


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
    return '' if _COLOR_LEVEL == 0 else '\x1b[1m'


def _italic():
    return '' if _COLOR_LEVEL == 0 else '\x1b[3m'


def _dim():
    return '' if _COLOR_LEVEL == 0 else '\x1b[2m'


def _underline():
    return '' if _COLOR_LEVEL == 0 else '\x1b[4m'


def _black():
    """黑字（用于 pill 背景上的前景，NO_COLOR 下禁用）。"""
    return '' if _COLOR_LEVEL == 0 else '\x1b[30m'


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
    'box_on':  '\u2611' if _is_unicode else '[x]', # 任务列表已勾选 ☑
    'box_off': '\u2610' if _is_unicode else '[ ]', # 任务列表未勾选 ☐
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
    'codeBg':      (60, 60, 66),      # 代码块背景：真彩为暗灰；16 色映射到 bright-black(100) 可见
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


def t(name: str) -> str:
    """取命名 token 的前景色转义码。"""
    rgb = TOKENS.get(name, TOKENS['text'])
    return _fg(rgb)


def bg(name: str) -> str:
    """取命名 token 的背景色转义码。"""
    rgb = TOKENS.get(name, TOKENS['subtle'])
    return _bg(rgb)


def c(name: str, text: str) -> str:
    """给文本上命名颜色。"""
    return t(name) + str(text) + _RESET


def b(text: str) -> str:
    """加粗。"""
    return _bold() + str(text) + _RESET


def dim(text: str) -> str:
    """置灰。"""
    return _dim() + str(text) + _RESET


def bold_c(name: str, text: str) -> str:
    """加粗 + 命名颜色。"""
    return _bold() + t(name) + str(text) + _RESET


def rule(char: Optional[str] = None, width: int = 80) -> str:
    return t('inactive') + (char or G['th']) * width + _RESET


def status(text: str) -> str:
    """Claude 风格的次要状态行（▸ 前缀 + 灰字）。"""
    return t('claude') + G['tri'] + ' ' + _RESET + t('subtle') + text + _RESET


def safe_print(text) -> None:
    """打印任意文本，GBK 等窄编码环境不崩溃（替换不可编码字符）。"""
    enc = sys.stdout.encoding or 'utf-8'
    try:
        print(text)
    except UnicodeEncodeError:
        try:
            print(str(text).encode(enc, errors='replace').decode(enc, errors='replace'))
        except Exception:
            print(str(text).encode('ascii', errors='replace').decode('ascii', errors='replace'))


def say(name: str, text: str) -> None:
    """按命名颜色打印一行（safe_print 兜底）：`ui.say('success', 'ok')`。"""
    safe_print(c(name, text))


# ---------- Markdown 渲染 ----------

_INLINE_RE = re.compile(
    r'(\*\*[^*]+\*\*|__[^_]+__|\*[^*\n]+\*|`[^`\n]+`|~~[^~]+~~|\[[^\]\n]+\]\([^)\n]+\))'
)


def _inline(text: str) -> str:
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
    r'|(?<![.\w])\d+(?![.\w])|\b[A-Za-z_]\w*\b)'
)


def _py_highlight(line: str) -> str:
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


def _code_block(lines, lang: str = '') -> str:
    """代码块：整行暗灰背景色块（对齐 CC）；python 代码启用最小语法高亮。"""
    if not lines:
        return ''
    if lang in ('python', 'py'):
        return '\n'.join(
            bg('codeBg') + _py_highlight(line) + _RESET
            for line in lines
        )
    return '\n'.join(
        bg('codeBg') + t('code') + line + _RESET
        for line in lines
    )


def _disp_w(s) -> int:
    """终端显示宽度：CJK/宽字符计 2，其余计 1（免 wcwidth 依赖的简化版）。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in str(s))


def _ljust(s, w) -> str:
    """按终端显示宽度左对齐（len() 对中文计 1 会错位）。"""
    s = str(s)
    return s + ' ' * max(0, w - _disp_w(s))


def _table_grid(headers, rows):
    """宽屏表格：对齐网格，表头加粗 + 主题色，分隔线。"""
    widths = [_disp_w(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], _disp_w(cell))
    out = [bold_c('gridHeader', '  '.join(
        _ljust(h, widths[i]) for i, h in enumerate(headers)))]
    out.append(t('inactive') + '  ' + '  '.join('-' * w for w in widths) + _RESET)
    for r in rows:
        out.append('  ' + '  '.join(
            (_ljust(c, widths[i]) if i < len(widths) else str(c))
            for i, c in enumerate(r)))
    return '\n'.join(out)


def _table_records(headers: List[str], rows) -> str:
    """窄屏表格：每行转成 label: value 记录（Codex/Claude 响应式布局）。"""
    out = []
    for r in rows:
        for i, h in enumerate(headers):
            val = str(r[i]) if i < len(r) else ''
            out.append('  ' + bold_c('subtle', h + ': ') + t('text') + val + _RESET)
        out.append('')
    return '\n'.join(out).rstrip('\n')


def table(headers: List[str], rows, max_width: Optional[int] = None) -> str:
    """响应式表格：终端够宽用网格，太窄自动转 label: value 记录。"""
    if max_width is None:
        try:
            max_width = shutil.get_terminal_size().columns
        except Exception:
            max_width = 80
    grid_w = sum(_disp_w(h) + 2 for h in headers) + len(headers)
    if grid_w > max_width - 4:
        return _table_records(headers, rows)
    return _table_grid(headers, rows)


def _table_block(lines, width: int) -> str:
    """把连续的 Markdown 表格行解析成响应式表格。lines[0]=表头, lines[1]=分隔线, 其余=行。"""
    def cells(line):
        return [c.strip() for c in line.strip().strip('|').split('|')]
    if not lines:
        return ''
    headers = cells(lines[0])
    rows = [cells(l) for l in lines[2:] if l.strip().strip('|').strip()]
    return table(headers, rows, max_width=width)


def markdown(text, width: int = 80) -> str:
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
        elif re.match(r'^(\s*)[-*] \[[ xX]\] ', line):
            m = re.match(r'^(\s*)[-*] \[([ xX])\] (.*)$', line)
            depth = len(m.group(1)) // 2
            box = c('success', G['box_on']) if m.group(2).lower() == 'x' \
                else c('inactive', G['box_off'])
            out.append('  ' * (depth + 1) + box + ' ' + _inline(m.group(3)))
        elif re.match(r'^(\s*)[-*] ', line):
            m = re.match(r'^(\s*)[-*] (.*)$', line)
            depth = len(m.group(1)) // 2
            out.append('  ' * (depth + 1) + t('claude') + G['bullet'] + _RESET +
                       ' ' + _inline(m.group(2)))
        elif re.match(r'^(\s*)\d+\. ', line):
            m = re.match(r'^(\s*)(\d+)\. (.*)$', line)
            depth = len(m.group(1)) // 2
            out.append('  ' * (depth + 1) + t('subtle') + m.group(2) + '.' +
                       _RESET + ' ' + _inline(m.group(3)))
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

def tool_line(name: str, args: Optional[dict] = None, preview: str = '',
              preview_len: int = 120) -> str:
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

def permission_banner(title: str, lines: List[str]) -> str:
    """权限确认框：紫色边框 + 内容行。宽度随终端自适应（30~70），长标题不溢出。"""
    width = 50
    try:
        width = max(30, min(70, shutil.get_terminal_size().columns - 6))
    except Exception:
        pass
    top_fill = max(2, width - len(title) - 4)
    out = [t('permission') + G['tl'] + G['th'] + ' ' + title + ' ' +
           G['th'] * top_fill + _RESET]
    for ln in lines:
        out.append(t('permission') + G['pipe'] + ' ' + _RESET + ln)
    out.append(t('permission') + G['bl'] + G['th'] * width + _RESET)
    return '\n'.join(out)


# ---------- Bash 输出框（Claude Code 粉色边框） ----------

def bash_box(command: str, output, max_lines: int = 14,
             width: Optional[int] = None) -> str:
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
    cmd = str(command)
    if len(cmd) > inner - 12:
        cmd = cmd[:inner - 15] + '...'
    head = t('bashBorder') + G['tl'] + G['th'] + ' bash: ' + cmd + _RESET
    tail = t('bashBorder') + G['bl'] + G['th'] * inner + _RESET
    return head + '\n' + body + '\n' + tail


def code_box(path: str, content, max_lines: int = 14,
             width: Optional[int] = None) -> str:
    """文件内容代码块：暗灰背景（codeBg）+ 灰色路径头，超长截断并提示。

    用于 read 工具输出——比塞进一行 preview 更接近 Claude Code 的展示。
    """
    if width is None:
        try:
            width = shutil.get_terminal_size().columns
        except Exception:
            width = 80
    inner = max(20, width - 4)
    lines = str(content).splitlines()
    truncated = len(lines) > max_lines
    if truncated:
        lines = lines[:max_lines]
    body = '\n'.join(bg('codeBg') + line + _RESET for line in lines)
    if truncated:
        body += '\n' + bg('codeBg') + dim(
            '... {0} more lines'.format(len(str(content).splitlines()) - max_lines)) + _RESET
    head = t('subtle') + G['tl'] + G['th'] + ' read: ' + \
        str(path)[:inner - 10] + _RESET
    tail = t('subtle') + G['bl'] + G['th'] * inner + _RESET
    return head + '\n' + body + '\n' + tail


# ---------- 状态行（claude-statusline 风格） ----------

MODEL_TIERS = {
    'flash':     'success',    # 快模型（Haiku 档）：绿
    'chat':      'info',       # 标准（Sonnet 档）：蓝
    'reasoner':  'permission', # 深度推理（Opus 档）：紫
}


def model_color(model: str) -> str:
    """按模型档位返回语义色名（Claude Code 按 tier 着色的约定）。"""
    m = (model or '').lower()
    for key, color in MODEL_TIERS.items():
        if key in m:
            return color
    return 'info'


def mode_indicator(mode: str = '') -> str:
    """模式标签：Plan 蓝 / Review 紫 / 其他灰（对齐 CC 的 [Plan Mode] 指示）。"""
    m = (mode or '').lower()
    if 'plan' in m:
        return bold_c('planMode', '[Plan Mode]')
    if 'review' in m:
        return bold_c('permission', '[Review Mode]')
    return bold_c('inactive', '[Normal]')


def _pill(color_name: str, text: str) -> str:
    """状态行 pill：背景色块 + 黑字（NO_COLOR 下退化为纯文本）。"""
    return bg(color_name) + _black() + ' ' + text + ' ' + _RESET


def ctx_bar(pct, width: int = 10) -> str:
    """上下文使用率填充条：███████░░░ 42%（claude-statusline 风格）。"""
    pct = max(0.0, min(100.0, float(pct or 0)))
    filled = int(round(pct / 100.0 * width))
    color = 'error' if pct > 75 else ('warning' if pct > 50 else 'success')
    bar = G['block'] * filled + G['hollow'] * (width - filled)
    return c(color, '[' + bar + ']') + ' ' + c(color, '{0}%'.format(int(pct)))


def statusline(model: str = '', tools: int = 0, ctx_pct=None, tokens: str = '',
               workdir: str = '', git_branch: str = '', cost=None, mode: str = '') -> str:
    """会话状态行：模式标签 / model(tier 色 pill) / git 分支 / 工具数 / 进度条 / 成本 / token。

    上下文使用率按阈值变色：>75% 红、>50% 黄、否则绿（Claude Code 状态行规则）。
    model / tools / cost / tokens 以带底色 pill 呈现，层次感对齐 CC。
    """
    parts = []
    if mode:
        parts.append(mode_indicator(mode))
    if model:
        parts.append(_pill(model_color(model), model))
    if git_branch:
        parts.append(t('subtle') + git_branch + _RESET)
    if tools:
        parts.append(_pill('inactive', '{0} tools'.format(tools)))
    if ctx_pct is not None:
        parts.append(ctx_bar(ctx_pct))
    if cost is not None:
        parts.append(_pill('success', '${0:.2f}'.format(cost)))
    if tokens:
        parts.append(_pill('inactive', tokens))
    if workdir:
        parts.append(t('inactive') + workdir + _RESET)
    if not parts:
        return ''
    return t('claude') + '│' + _RESET + ' ' + '  '.join(parts)


# ---------- Diff 高亮 ----------

def diff(text) -> str:
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

    def __init__(self, label: Optional[str] = None, token: str = 'claude',
                 stall_after: float = 8.0, speed: str = 'auto'):
        self.label = label
        self.token = token
        self.stall_after = stall_after
        self.speed = speed  # 'auto' | 'thinking'(200ms) | 'requesting'(50ms)
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self) -> 'Spinner':
        if not _is_tty:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _interval(self) -> float:
        if self.speed == 'thinking':
            return 0.2
        if self.speed == 'requesting':
            return 0.05
        return 0.2 if self.label else 0.05

    def _run(self) -> None:
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
            i += 1
            # Event.wait 替代 time.sleep：stop 信号一到立即醒来退出，不等满一个 interval
            if self._stop.wait(self._interval()):
                break

    def _clear_line(self):
        """清掉 Spinner 行（\r + 擦除到行尾），失败静默，避免阻塞传播。"""
        try:
            sys.stdout.write('\r\x1b[2K')
            sys.stdout.flush()
        except Exception:
            pass

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            try:
                self._thread.join(timeout=0.5)
            finally:
                # 无论正常退出还是 Ctrl+C（KeyboardInterrupt）都清行，避免光标残留
                self._clear_line()
        return False


# ---------- 进度条（定长百分比 / 不定长滚动） ----------

class ProgressBar:
    """终端进度条：定长（update 手动刷新百分比）或不定长（start/stop 滚动动画）。

    定长（耗时已知）：
        bar = ProgressBar()
        for pct in (10, 40, 90, 100):
            bar.update(pct)
            time.sleep(0.2)
        bar.finish()

    不定长（耗时未知，支持 with 语法自动清行）：
        with ProgressBar(indeterminate=True):
            do_slow_work()
    """

    def __init__(self, width: int = 20, indeterminate: bool = False,
                 token: str = 'success', stall_after: float = 8.0):
        self.width = width
        self.indeterminate = indeterminate
        self.token = token
        self.stall_after = stall_after
        self._stop = threading.Event()
        self._thread = None

    # ---------- 定长模式 ----------

    def update(self, pct: float) -> None:
        """渲染一帧定长进度条（\\r 原地刷新）。pct: 0~100，按阈值变色。"""
        pct = max(0.0, min(100.0, float(pct)))
        filled = int(round(pct / 100.0 * self.width))
        color = 'error' if pct > 75 else ('warning' if pct > 50 else 'success')
        bar = G['block'] * filled + G['hollow'] * (self.width - filled)
        sys.stdout.write('\r\x1b[2K' + c(color, '[' + bar + ']') +
                         ' ' + c(color, '{0}%'.format(int(pct))))
        sys.stdout.flush()

    def finish(self) -> None:
        """定长模式结束：清掉进度条行。"""
        self._clear()

    # ---------- 不定长模式 ----------

    def start(self) -> None:
        """启动滚动动画线程（仅 TTY）。"""
        if not _is_tty or self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止动画线程并清行。"""
        self._stop.set()
        if self._thread:
            try:
                self._thread.join(timeout=0.5)
            finally:
                self._clear()

    def _run(self) -> None:
        i = 0
        start = time.time()
        while not self._stop.wait(0.08):
            elapsed = time.time() - start
            if elapsed > self.stall_after:
                # 停滞：超过阈值后随时间渐变到错误红（复用 _interp）
                ratio = min((elapsed - self.stall_after) / 20.0, 1.0)
                color = _fg(_interp(TOKENS.get(self.token, TOKENS['claude']),
                                    TOKENS['error'], ratio))
            else:
                color = t(self.token)
            pos = i % (self.width + 1)
            bar = G['hollow'] * pos + G['block'] + G['hollow'] * (self.width - pos)
            sys.stdout.write('\r\x1b[2K' + color + '[' + bar + ']' + _RESET)
            sys.stdout.flush()
            i += 1

    def __enter__(self) -> 'ProgressBar':
        if self.indeterminate:
            self.start()
        return self

    def __exit__(self, *exc) -> bool:
        if self._thread:
            self.stop()
        return False

    def _clear(self) -> None:
        try:
            sys.stdout.write('\r\x1b[2K')
            sys.stdout.flush()
        except Exception:
            pass
