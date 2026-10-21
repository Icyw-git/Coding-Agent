"""AGENTS.md/CLAUDE.md 注入与内建/工作区技能合并。"""
import pytest

import harness
import project_context
import skill_system


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


@pytest.fixture(autouse=True)
def _restore_prompt_cache():
    harness._prompt_cache.set(None)
    yield
    harness._prompt_cache.set(None)


@pytest.fixture(autouse=True)
def _restore_skill_registry():
    saved = {name: dict(skill) for name, skill in skill_system.SKILL_REGISTRY.items()}
    saved_signature = skill_system._last_signature
    yield
    skill_system.SKILL_REGISTRY.clear()
    skill_system.SKILL_REGISTRY.update(saved)
    skill_system._last_signature = saved_signature


def _write_skill(root, name, description, body='skill body'):
    manifest = root / name / 'skill.md'
    _write(manifest, f'---\nname: {name}\ndescription: {description}\n---\n{body}\n')
    return manifest


def test_loads_agents_md_from_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv('HARNESS_PROJECT_CONTEXT', raising=False)
    _write(tmp_path / 'AGENTS.md', '# Rules\n\nUse pytest.\n')

    result = project_context.load_project_instructions(tmp_path)

    assert result.source == 'AGENTS.md'
    assert 'Use pytest.' in result.text
    assert not result.truncated
    assert 'Use pytest.' in project_context.format_project_instructions(result)


def test_override_then_agents_then_claude_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv('HARNESS_PROJECT_CONTEXT', raising=False)
    _write(tmp_path / 'AGENTS.override.md', 'override')
    _write(tmp_path / 'AGENTS.md', 'agents')
    _write(tmp_path / 'CLAUDE.md', 'claude')

    assert project_context.load_project_instructions(tmp_path).text == 'override'
    (tmp_path / 'AGENTS.override.md').unlink()
    assert project_context.load_project_instructions(tmp_path).text == 'agents'
    (tmp_path / 'AGENTS.md').unlink()
    assert project_context.load_project_instructions(tmp_path).text == 'claude'


def test_clips_large_instruction_file(tmp_path, monkeypatch):
    monkeypatch.delenv('HARNESS_PROJECT_CONTEXT', raising=False)
    _write(tmp_path / 'AGENTS.md', 'x' * 100)

    result = project_context.load_project_instructions(tmp_path, max_bytes=16)

    assert result.truncated
    assert result.limit == 16
    assert len(result.text) <= 16
    assert '[truncated at 16 bytes]' in project_context.format_project_instructions(result)


def test_ignores_binary_and_disabled_context(tmp_path, monkeypatch):
    _write(tmp_path / 'AGENTS.md', 'text\x00binary')
    assert project_context.load_project_instructions(tmp_path) is None

    _write(tmp_path / 'AGENTS.md', 'normal')
    monkeypatch.setenv('HARNESS_PROJECT_CONTEXT', 'off')
    assert project_context.load_project_instructions(tmp_path) is None


def test_symlink_escape_is_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv('HARNESS_PROJECT_CONTEXT', raising=False)
    outside = _write(tmp_path.parent / 'outside-agents.md', 'secret')
    workspace = tmp_path / 'project'
    workspace.mkdir()
    try:
        (workspace / 'AGENTS.md').symlink_to(outside)
    except OSError:
        pytest.skip('symlinks are not available on this host')

    assert project_context.load_project_instructions(workspace) is None


def test_system_prompt_includes_project_instructions(tmp_path, monkeypatch):
    monkeypatch.delenv('HARNESS_PROJECT_CONTEXT', raising=False)
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)
    monkeypatch.setattr(harness, 'SKILLS_DIR', tmp_path / 'skills')
    monkeypatch.setattr(harness, 'BUILTIN_SKILLS_DIR', tmp_path / 'builtin-skills')
    _write(tmp_path / 'AGENTS.md', 'Prefer small commits.')

    context = harness.update_context({}, [])
    prompt = harness.assemble_system_prompt(context)

    assert 'AGENTS.md' in prompt
    assert 'Prefer small commits.' in prompt
    assert 'cannot override the security' in prompt


def test_system_prompt_cache_invalidates_when_file_changes(tmp_path, monkeypatch):
    monkeypatch.delenv('HARNESS_PROJECT_CONTEXT', raising=False)
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)
    monkeypatch.setattr(harness, 'SKILLS_DIR', tmp_path / 'skills')
    monkeypatch.setattr(harness, 'BUILTIN_SKILLS_DIR', tmp_path / 'builtin-skills')
    instructions = _write(tmp_path / 'AGENTS.md', 'version one')

    first = harness.get_system_prompt(harness.update_context({}, []))
    _write(instructions, 'version two, longer')
    second = harness.get_system_prompt(harness.update_context({}, []))

    assert 'version one' in first
    assert 'version two' in second
    assert 'version one' not in second


def test_prompt_debug_output_uses_stderr(monkeypatch, capsys):
    monkeypatch.setenv('HARNESS_DEBUG', '1')
    context = {'workspace': 'example', 'enabled_tools': []}

    first = harness.get_system_prompt(context)
    second = harness.get_system_prompt(context)

    captured = capsys.readouterr()
    assert first == second
    assert captured.out == ''
    assert '[assembled]' in captured.err
    assert '[cache hit]' in captured.err


def test_builtin_and_project_skills_merge_with_project_override(tmp_path, monkeypatch):
    builtin = tmp_path / 'builtin'
    project = tmp_path / 'project'
    _write_skill(builtin, 'alpha', 'builtin alpha')
    _write_skill(builtin, 'beta', 'builtin beta')
    _write_skill(project, 'alpha', 'project alpha')
    monkeypatch.setattr(harness, 'BUILTIN_SKILLS_DIR', builtin)
    monkeypatch.setattr(harness, 'SKILLS_DIR', project)

    skill_system.scan_skills(force=True)

    assert skill_system.SKILL_REGISTRY['alpha']['description'] == 'project alpha'
    assert skill_system.SKILL_REGISTRY['beta']['description'] == 'builtin beta'
    assert skill_system.load_skill('alpha') == 'skill body'


def test_workspace_switch_rescans_project_skills(tmp_path, monkeypatch):
    builtin = tmp_path / 'builtin'
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    _write_skill(builtin, 'alpha', 'builtin alpha')
    _write_skill(first, 'alpha', 'first alpha')
    _write_skill(first, 'beta', 'first beta')
    _write_skill(second, 'gamma', 'second gamma')
    monkeypatch.setattr(harness, 'BUILTIN_SKILLS_DIR', builtin)
    monkeypatch.setattr(harness, 'SKILLS_DIR', first)
    skill_system.scan_skills(force=True)

    monkeypatch.setattr(harness, 'SKILLS_DIR', second)
    skill_system.scan_skills()

    assert skill_system.SKILL_REGISTRY['alpha']['description'] == 'builtin alpha'
    assert 'beta' not in skill_system.SKILL_REGISTRY
    assert skill_system.SKILL_REGISTRY['gamma']['description'] == 'second gamma'


def test_edited_skill_manifest_is_rescanned(tmp_path, monkeypatch):
    builtin = tmp_path / 'builtin'
    project = tmp_path / 'project'
    monkeypatch.setattr(harness, 'BUILTIN_SKILLS_DIR', builtin)
    monkeypatch.setattr(harness, 'SKILLS_DIR', project)
    manifest = _write_skill(builtin, 'alpha', 'before')
    skill_system.scan_skills(force=True)
    assert skill_system.SKILL_REGISTRY['alpha']['description'] == 'before'

    manifest.write_text(
        '---\nname: alpha\ndescription: after a longer description\n---\nbody\n',
        encoding='utf-8')
    skill_system.scan_skills()

    assert skill_system.SKILL_REGISTRY['alpha']['description'] == 'after a longer description'


def test_missing_skill_directories_are_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'BUILTIN_SKILLS_DIR', tmp_path / 'missing-a')
    monkeypatch.setattr(harness, 'SKILLS_DIR', tmp_path / 'missing-b')

    skill_system.scan_skills(force=True)

    assert skill_system.SKILL_REGISTRY == {}
    assert skill_system.list_skills() == 'No skills registered'
