"""第四档 MUST_DO_SELF：这类操作只能用户自己做。

与 deny 的区别：deny 是「这次不做」（可以换个安全做法完成目标）；第四档是
「没有这条路径」——不看档位、不看规则文件、不看会话白名单、不进审批队列。
一旦落到 ask，模型就有机会把审批人说动，而改密码/转账这类事无论模型说什么
都只能人自己动手（dots: "Certain sensitive tasks always stay with you"）。
"""
from __future__ import annotations

import pytest

import never_do

# ---------------------------------------------------------------- 命中

@pytest.mark.parametrize('command,what', [
    ('passwd', 'password'),
    ('chpasswd', 'password'),
    ('net user bob /password:new123', 'password'),
    ('Set-ADAccountPassword -Identity bob', 'password'),
    ('net localgroup administrators bob /add', 'admin rights'),
    ('visudo', 'sudoers'),
    ('oathtool --totp -b ABCDEF', '2FA'),
    ('paypal send money 100', 'payment'),
    ('stripe charge 50', 'payment'),
    ('bank transfer 1000', 'money'),
])
def test_bash_commands_are_refused(command, what):
    refusal = never_do.check('bash', {'command': command})
    assert refusal, command
    assert what in refusal
    assert 'user personally' in refusal


def test_powershell_account_change_is_refused():
    assert never_do.check('bash', {'command': 'wmic useraccount where name=bob set password=x'})


def test_typing_password_via_desktop_is_refused():
    refusal = never_do.check('desktop_action', {'action': 'type', 'text': 'my password'})
    assert refusal
    assert 'password' in refusal


def test_typing_otp_via_desktop_is_refused():
    assert never_do.check('desktop_action', {'action': 'type', 'text': 'code 123456 验证码'})


def test_copying_credential_to_clipboard_is_refused():
    assert never_do.check('clipboard_write', {'text': 'sk-' + 'a' * 24})
    assert never_do.check('clipboard_write', {'text': 'ghp_' + 'a' * 30})


# ---------------------------------------------------------------- 不误伤

@pytest.mark.parametrize('command', [
    'ls -la',
    'git status',
    'grep password docs/',
    'cat README.md',
    'python -m pytest',
    'npm install',
    'echo "my password is in the vault"',   # 讨论密码≠改密码
    'git log --grep=payment',
])
def test_normal_commands_are_not_refused(command):
    assert never_do.check('bash', {'command': command}) == ''


def test_normal_typing_is_not_refused():
    assert never_do.check('desktop_action', {'action': 'type', 'text': 'hello world'}) == ''


def test_empty_args_are_not_refused():
    assert never_do.check('bash', {}) == ''
    assert never_do.check('bash', {'command': ''}) == ''


def test_non_string_args_do_not_crash():
    assert never_do.check('desktop_action', {'x': 10, 'y': 20}) == ''


# ---------------------------------------------------------------- 不可绕过

def test_refusal_message_points_at_the_user():
    """拒绝语必须点明「只能你做」，否则模型会换个工具重试。"""
    refusal = never_do.check('bash', {'command': 'passwd'})
    assert 'not an approval question' in refusal
    assert 'no setting can enable it' in refusal


def test_disabled_by_incident_switch(monkeypatch):
    """事故排查开关（误伤导致完全无法工作时用），不是普通配置项。"""
    monkeypatch.setenv('HARNESS_NEVER_DO', '0')
    assert never_do.check('bash', {'command': 'passwd'}) == ''


def test_enabled_by_default():
    assert never_do.enabled()


def test_actions_list_is_human_readable():
    actions = never_do.actions()
    assert actions == sorted(actions)
    assert any('password' in item for item in actions)
    assert any('payment' in item for item in actions)


# ---------------------------------------------------------------- 接入 enforce

class _RecordingGate:
    """记录审批是否被咨询过：第四档绝不能走到问人。"""

    timeout = 1

    def __init__(self):
        self.asked = 0

    def decide(self, *_args, **_kwargs):
        self.asked += 1
        return True, 'approved'

    def decide_queued(self, *_args, **_kwargs):
        self.asked += 1
        return True, 'approved'

    def session_allows(self, *_args, **_kwargs):
        return True


@pytest.fixture
def permissive(monkeypatch):
    """最宽松的组合：全放行档位 + 会话白名单命中 + 审批都点同意。"""
    import hooks
    monkeypatch.setenv('HARNESS_SANDBOX', 'danger-full-access')
    monkeypatch.setenv('HARNESS_APPROVAL', 'never')
    gate = _RecordingGate()
    monkeypatch.setattr(hooks, 'GATE', gate)
    return hooks, gate


def test_enforce_refuses_even_with_danger_full_access_and_never(permissive):
    """核心断言：第四档不看档位。"""
    hooks, gate = permissive
    out = hooks.enforce('bash', {'command': 'passwd'}, interactive=True)
    assert out and 'Refused' in out
    assert gate.asked == 0        # 没问人，直接拒


def test_enforce_refuses_before_session_whitelist(permissive):
    """白名单是预授权，但预授权不等于可以代做这类事。"""
    hooks, gate = permissive
    out = hooks.enforce('bash', {'command': 'oathtool -b X'}, interactive=True)
    assert out and 'Refused' in out
    assert gate.asked == 0


def test_enforce_refuses_in_background_turns(permissive):
    """后台回合同样不行：不能因为没人看着就放宽。"""
    hooks, gate = permissive
    out = hooks.enforce('bash', {'command': 'passwd'}, interactive=False)
    assert out and 'Refused' in out


def test_enforce_still_allows_normal_commands(permissive):
    hooks, _gate = permissive
    assert hooks.enforce('bash', {'command': 'ls'}, interactive=True) is None


# ---------- 中文凭据 ----------

@pytest.mark.parametrize('text', [
    '验证码123456',
    '请输入验证码',
    '短信验证码 8899',
    '动态码 5566',
    '我的密码是abc',
    '密码：hunter2',
    'otp 1234',
    'password: x',
    'sk-abcdefghijklmnop1234',
    'ghp_abcdefghijklmnopqrstuvwxyz01',
    'AKIAIOSFODNN7EXAMPLE',
])
def test_typing_credentials_is_refused(text):
    """往输入框里打凭据属于第四档。

    踩过：规则原样是 `\b(...|验证码)\b`，而**中文没有词边界**——
    `\b验证码\b` 在「请输入验证码1234」里永远不匹配（两个汉字都是 word
    字符）。于是 `otp 1234` 被拦、`验证码123456` 放行，规则看起来还在。
    """
    assert never_do.check('desktop_action',
                          {'action': 'type', 'text': text})


@pytest.mark.parametrize('text', [
    '帮我写一段密码学的说明',
    '解释一下二次验证的原理',
    '加一个 token 字段到配置里',
    '今天天气不错',
])
def test_typing_ordinary_text_is_allowed(text):
    """不按词猜：单写「密码」「token」不该误伤正常输入。"""
    assert never_do.check('desktop_action',
                          {'action': 'type', 'text': text}) == ''


def test_credential_patterns_do_not_use_cjk_word_boundaries():
    """守护：不允许用 \b 包住中文（那等于这条规则不存在）。

    中文没有词边界，`\b验证码\b` 在「请输入验证码」里永远不匹配。
    直接看字符相邻关系，不用正则——用正则写这个检查本身就会踩同一个坑。
    """
    boundary = chr(92) + 'b'
    patterns = [pattern.pattern for pattern, _what in never_do._COMMAND_RULES]
    patterns += [pattern.pattern for _tool, pattern, _what in never_do._ARG_RULES]
    for pattern in patterns:
        index = pattern.find(boundary)
        while index != -1:
            before = pattern[index - 1] if index else ''
            after = pattern[index + 2] if index + 2 < len(pattern) else ''
            for neighbour in (before, after):
                assert not _is_cjk(neighbour), f'{boundary} 挨着中文: {pattern}'
            index = pattern.find(boundary, index + 2)


def _is_cjk(char: str) -> bool:
    return bool(char) and '一' <= char <= '鿿'
