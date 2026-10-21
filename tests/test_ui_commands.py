"""TUI 命令注册表与模糊匹配（移植 Pi fuzzy）。"""
from ui import commands
from ui.fuzzy import fuzzy_filter, fuzzy_match


def test_fuzzy_match_prefers_prefix_and_consecutive():
    matched, score_exact = fuzzy_match('model', 'model')
    assert matched and score_exact < 0
    matched, score_gap = fuzzy_match('mdl', 'model')
    assert matched
    assert score_exact < score_gap


def test_fuzzy_filter_orders_best_first():
    items = ['/checkpoint', '/model', '/mcp', '/hotkeys']
    assert fuzzy_filter(items, 'mdl', lambda item: item)[0] == '/model'


def test_command_aliases_and_suggestions():
    assert commands.find('?').name == 'help'
    assert commands.find('/exit').name == 'quit'
    assert commands.find('transcripts').name == 'sessions'
    assert commands.matches('mdl')[0].name == 'model'
    assert commands.did_you_mean('modl') == '/model'
    assert commands.did_you_mean('zzzz') is None


def test_help_lists_ported_and_line_only_commands():
    text = commands.help_text()
    assert 'TUI commands:' in text
    assert '/sessions' in text
    assert 'Only in line REPL' in text and '/review' in text
    assert 'Ctrl+O' in commands.hotkeys_text()


def test_skill_commands_are_registered_dynamically():
    skills = commands.skill_commands()
    if not skills:      # 发行版可以不带技能
        return
    spec = skills[0]
    assert spec.name.startswith('skill:')
    assert spec.ported
    assert commands.find(spec.name) is not None
    assert any(item.name == spec.name for item in commands.matches('skill:'))
