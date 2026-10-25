"""prompt_assembly：注入式上下文采集、缓存与入口解耦。"""
import subprocess
import sys
from pathlib import Path

import harness
import prompt_assembly

ROOT = Path(__file__).resolve().parents[1]


def _run_python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT,
        capture_output=True, text=True, timeout=60)


def test_import_does_not_load_entrypoint():
    """提示原语不反向依赖 harness：否则其他入口会连带加载 CLI 状态。"""
    result = _run_python(
        'import sys; sys.path.insert(0, "src"); import prompt_assembly; '
        'print("harness" in sys.modules)')

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'False'


def test_collect_context_uses_injected_dependencies(tmp_path):
    (tmp_path / 'AGENTS.md').write_text('Injected rule.', encoding='utf-8')

    context = prompt_assembly.collect_context(
        workdir=tmp_path, tool_names=('bash', 'read'), skills='- demo: demo')

    assert context['workspace'] == str(tmp_path)
    assert context['enabled_tools'] == ['bash', 'read']
    assert context['skills'] == '- demo: demo'
    assert 'Injected rule.' in context['project_instructions']
    prompt = prompt_assembly.assemble_system_prompt(context)
    assert 'Injected rule.' in prompt and 'demo' in prompt


def test_environment_message_renders_local_time():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 25, 9, 30, tzinfo=timezone(timedelta(hours=8)))

    message = prompt_assembly.environment_message(now)

    assert '2026-10-25 09:30:00' in message
    assert 'weekday 日' in message
    assert 'relative dates' in message


def test_rules_tell_model_to_use_tools_in_chat_clients():
    prompt = prompt_assembly.assemble_system_prompt({
        'workspace': 'D:/work', 'enabled_tools': [], 'skills': '',
        'project_instructions': '',
    })

    assert 'schedule_cron' in prompt and 'find_sessions' in prompt
    assert 'instead of telling them to type a command' in prompt


def test_build_sub_system_accepts_explicit_workspace(tmp_path):
    (tmp_path / 'CLAUDE.md').write_text('Sub rule.', encoding='utf-8')

    prompt = prompt_assembly.build_sub_system(workdir=tmp_path, skills='')

    assert str(tmp_path) in prompt
    assert 'Sub rule.' in prompt
    assert 'focused sub-agent' in prompt


def test_harness_wrapper_injects_live_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)
    monkeypatch.setattr(harness, 'SKILLS_DIR', tmp_path / 'skills')
    monkeypatch.setattr(harness, 'BUILTIN_SKILLS_DIR', tmp_path / 'builtin')

    context = harness.update_context({}, [])

    assert context['workspace'] == str(tmp_path)
    assert 'bash' in context['enabled_tools']
    assert 'read_document' in context['enabled_tools']
