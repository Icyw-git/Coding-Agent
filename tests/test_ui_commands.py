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


def test_help_lists_all_commands_as_ported():
    text = commands.help_text()
    assert 'TUI commands:' in text
    assert '/sessions' in text and '/review' in text
    assert 'Only in line REPL' not in text
    assert all(spec.ported for spec in commands.COMMANDS)
    assert 'Ctrl+O' in commands.hotkeys_text()


def test_cleanup_and_workspace_argument_completions(monkeypatch):
    archive = commands.argument_suggestions(commands.find('archive'), 're')
    assert archive and archive[0][0] == 'restore'

    actions = commands.argument_suggestions(commands.find('workspace'), 're')
    assert any(value == 'remove' for value, _label in actions)

    import harness
    monkeypatch.setattr(harness, 'registered_workspaces', lambda: [
        {'id': 'w1', 'name': 'demo', 'path': 'C:/demo'},
    ])
    items = commands.argument_suggestions(commands.find('workspace'),
                                          'remove de')
    assert items and items[0][0] == 'remove 1'


def test_multi_token_argument_completions_keep_subcommand(tmp_path,
                                                          monkeypatch):
    import harness
    from ui.composer.completions import apply_suggestion

    (tmp_path / 'alpha').mkdir()
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    monkeypatch.setattr(harness, 'registered_workspaces', lambda: [
        {'id': 'w1', 'name': 'demo', 'path': 'C:/demo'},
    ])

    add = commands.argument_suggestions(commands.find('workspace'), 'add al')
    assert add and add[0][0].startswith('add alpha')
    assert (apply_suggestion('/workspace add al', add[0][0])
            == f'/workspace {add[0][0]}')

    new = commands.argument_suggestions(commands.find('new'), '--ws ')
    assert new and new[0][0] == '--ws 1'
    assert apply_suggestion('/new --ws ', new[0][0]) == '/new --ws 1'

    watch = commands.argument_suggestions(commands.find('watch'), 'bg_0001 ')
    assert watch and watch[0][0] == 'bg_0001 --timeout 60'
    assert (apply_suggestion('/watch bg_0001 ',
                             watch[0][0]).endswith('bg_0001 --timeout 60'))


def test_cd_argument_completion_lists_only_directories(tmp_path, monkeypatch):
    import harness
    (tmp_path / 'alpha').mkdir()
    (tmp_path / 'afile.txt').write_text('x', encoding='utf-8')
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    items = commands.argument_suggestions(commands.find('cd'),
                                          str(tmp_path) + '/a')
    values = [value for value, _label in items]
    assert any('alpha' in value for value in values)
    assert not any('afile' in value for value in values)


def test_skill_commands_are_registered_dynamically():
    skills = commands.skill_commands()
    if not skills:      # 发行版可以不带技能
        return
    spec = skills[0]
    assert spec.name.startswith('skill:')
    assert spec.ported
    assert commands.find(spec.name) is not None
    assert any(item.name == spec.name for item in commands.matches('skill:'))


def test_snapshot_commands_are_ported_to_tui():
    for name in ('checkpoint', 'checkpoints', 'rewind', 'undo'):
        spec = commands.find(name)
        assert spec is not None and spec.ported
    text = commands.help_text()
    assert '/rewind' in text and '/undo' in text


def test_compound_checkpoint_gc_completions_keep_previous_args():
    from ui.composer.completions import apply_suggestion

    spec = commands.find('checkpoint-gc')
    all_done = commands.argument_suggestions(spec, '--all ')
    assert all_done
    assert all_done[0][0].startswith('--all --keep ')
    assert all(value.startswith('--all ') for value, _ in all_done)
    assert not any(value == '--all' for value, _ in all_done)

    kept = commands.argument_suggestions(spec, '--all --keep 1')
    assert any(value == '--all --keep 10' for value, _ in kept)
    assert all(value.startswith('--all --keep ') for value, _ in kept)
    assert (apply_suggestion('/checkpoint-gc --all ', all_done[0][0])
            == f'/checkpoint-gc {all_done[0][0]}')

    after_keep = commands.argument_suggestions(spec, '--keep 10 ')
    assert after_keep and after_keep[0][0] == '--keep 10 --all'
    assert commands.argument_suggestions(spec, '--all --keep 10 ') == []


def test_compound_cache_and_watch_completions():
    cache = commands.argument_suggestions(commands.find('cache'), 'gc 30 ')
    assert cache and cache[0][0] == 'gc 30 --apply'

    watch = commands.argument_suggestions(commands.find('watch'),
                                         'bg_1 --timeout ')
    values = [value for value, _ in watch]
    assert 'bg_1 --timeout 30' in values
    assert 'bg_1 --timeout 300' in values
