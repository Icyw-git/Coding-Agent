"""永远不代做的操作：第四档 ``MUST_DO_SELF``。

为什么需要第四档
----------------
现有三档是 allow / ask / deny。``ask`` 的问题是**它会被说服**：模型换一种说法
（"用户已经同意了"、"这是测试环境"、"只是演示一下"）就可能把审批人说动。而这
一类事情根本不该进入讨论——改密码、转账、输入验证码，无论模型说什么，都只能是
用户自己动手。

dots 把这个写在产品里：

    Certain sensitive tasks, such as changing a password, **always stay with you**.
    Built-in safety requirements always apply.   (Custom Rules 不能覆盖)

Muse 的对应做法是「assume breach」：不问模型会不会被说服，只问它被说服了能造成
什么。所以这一档**不接受任何配置覆盖**：不看档位、不看规则文件、不看会话白名单、
不看 LLM 审查官，也不进审批队列（排队等来的"是"同样是错的）。

与 ``deny`` 的区别
------------------
``deny`` 是"这次不做"（可以换个安全做法完成目标）。
``MUST_DO_SELF`` 是"这件事只能你做"——拒绝语要说清这一点，并建议由人操作，
否则模型会不断换写法重试别的工具。

识别方式
--------
按**动作意图**匹配，不按工具名：同一个 ``bash`` 既能 ``ls`` 也能 ``passwd``。
所以这里给的是命令词 / 参数模式，和 ``policy_bash`` 的黑名单并行、互不替代。
"""
from __future__ import annotations

import re

import settings

#: 第四档的动作码。``Decision.action`` 仍然只有三态（结构不变），
#: 「不可覆盖」由 ``enforce`` 在规则应用之后单独检查这个码来保证。
MUST_DO_SELF = 'must_do_self'

#: 给模型的固定拒绝语。必须点明「只能你做」，否则模型会换个工具重试。
_TEMPLATE = (
    'Refused: {what} must be done by the user personally. This is not an approval '
    'question — there is no way for me to do it, and no setting can enable it. '
    'Tell the user what needs to be done and stop.'
)


def _result(what: str) -> str:
    return _TEMPLATE.format(what=what)


#: 命令词 → 人类可读的动作名。顺序即匹配顺序。
_COMMAND_RULES: tuple[tuple[re.Pattern, str], ...] = (
    # 改密码 / 账号凭据
    (re.compile(r'(?i)\b(passwd|chpasswd|chage)\b'), 'changing a password'),
    (re.compile(r'(?i)\bnet\s+user\s+\S+\s+/?(password|pwd)'), 'changing a password'),
    (re.compile(r'(?i)\bSet-ADAccountPassword\b'), 'changing an account password'),
    (re.compile(r'(?i)\b(wmic|Get-WmiObject)\b.*\buseraccount\b'), 'changing account settings'),
    (re.compile(r'(?i)\bnet\s+localgroup\s+administrators\s+\S+\s+/add'), 'granting admin rights'),
    (re.compile(r'(?i)\bsudo\s+visudo\b|\bvisudo\b'), 'editing sudoers'),
    # 2FA / 验证码
    (re.compile(r'(?i)\b(oathtool|authenticator|totp)\b'), 'generating a 2FA code'),
    # 支付 / 转账
    (re.compile(r'(?i)\b(stripe|paypal|alipay|wechatpay|venmo)\b.*\b(pay|charge|transfer|refund|send)\b'),
     'making a payment'),
    (re.compile(r'(?i)\b(pay|transfer|remit)\s+(money|funds|\$|usd|cny|rmb)'), 'transferring money'),
    (re.compile(r'(?i)\b(bank|wire)\s+transfer\b'), 'transferring money'),
)

#: 需要「工具 + 参数」联合判断的规则（单看命令词会误伤）。
_ARG_RULES: tuple[tuple[str, re.Pattern, str], ...] = (
    # 往密码框 / 验证码框里打字：desktop_action 的 type 动作。
    #
    # 中文**不能**用 `\b` 包：中文没有词边界，`\b验证码\b` 在
    # 「请输入验证码1234」里永远不匹配（两个汉字都是 word 字符），
    # 这条规则曾经因此静默失效（实测 `otp 1234` 被拦、`验证码123456` 放行）。
    # 所以英文用 `\b`、中文用裸词；`密码是/密码：` 只收显式的凭据陈述，
    # 不拦单写的「密码」（否则「写一段密码学的说明」会被误伤）。
    ('desktop_action', re.compile(
        r'(?i)\b(password|passwd|passcode|otp|2fa)\b'
        r'|验证码|短信码|动态码|校验码|密码[是为:：]'
        # 凭据「形状」比词更可靠：sk-xxx / ghp_xxx / AKIAxxx / 私钥块。
        # 不按 `token` 这个词猜——「加一个 token 字段」这种正常输入会被误伤，
        # 而 `token 1234567890` 和任意一个数字串没办法区分。
        r'|sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}'
        r'|-----BEGIN [A-Z ]*PRIVATE KEY-----'),
     'typing a password or verification code'),
    # 把凭据复制到剪贴板给别的程序用
    ('clipboard_write', re.compile(
        r'(?i)(sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|'
        r'-----BEGIN [A-Z ]*PRIVATE KEY-----)'),
     'copying a credential to the clipboard'),
)

#: 提交表单类（同意条款、签合同）：只按意图词匹配，无法枚举站点，保守留空。
#: 这里显式记录「为什么没做」——不是遗漏，而是纯文本匹配误报太高（"同意"两个字
#: 在日常对话里到处都是），需要结构化表单识别才有意义。
_FORM_RULES: tuple[tuple[re.Pattern, str], ...] = ()


#: 界面控件上的第四档：按**控件名**匹配。
#:
#: 和上面的命令规则是两套东西——shell 命令词表对「点一个写着"修改密码"的按钮」
#: 完全无效，而元素级动作恰恰不经过命令行。这里用 .NET 兼容的简单正则
#: （不含前后瞻），由 PowerShell 侧对控件名求值。
#:
#: 误伤是接受的：点「忘记密码」也会被挡。这一档的定义就是「宁可让用户自己点」。
_UI_RULES: tuple[str, ...] = (
    r'(?i)密码|passwo?rd|passcode',
    r'(?i)验证码|verification code|verify code|one.?time code|(?<![a-z])otp(?![a-z])|'
    r'2fa|two.factor|双重验证|两步验证',
    r'(?i)转账|汇款|付款|支付|下单|结算|(?<![a-z])pay(?![a-z])|payment|checkout|(?<![a-z])transfer(?![a-z])|purchase',
    r'(?i)提权|以管理员身份|管理员权限|run as administrator|elevat|(?<![a-z])uac(?![a-z])',
)


def ui_forbidden_patterns() -> list[str]:
    """给元素级桌面动作用的控件名模式（.NET 正则语法）。"""
    if not enabled():
        return []
    return list(_UI_RULES)


def check(tool: str, args: dict) -> str:
    """返回拒绝语（非空即命中第四档），未命中返回空串。

    只做纯文本/参数匹配，不读环境、不读规则：这一档的定义就是「无条件」。
    """
    if not enabled():
        # 留一个总开关只为排查事故（例如误伤导致完全无法工作），
        # 默认开启且不应被模型或单次会话改动。
        return ''
    payload = ''
    if isinstance(args, dict):
        payload = ' '.join(str(value) for value in args.values() if isinstance(value, str))
    payload = payload.strip()
    if not payload:
        return ''

    for pattern, what in _COMMAND_RULES:
        if pattern.search(payload):
            return _result(what)

    for tool_name, pattern, what in _ARG_RULES:
        if tool == tool_name and pattern.search(payload):
            return _result(what)

    for pattern, what in _FORM_RULES:
        if pattern.search(payload):
            return _result(what)
    return ''


def enabled() -> bool:
    """事故排查用的总开关，默认开。

    注意它**不是**一个普通配置项：只有设成 ``HARNESS_NEVER_DO=0`` 才关闭，
    且不提供用户规则文件、审批、会话白名单等任何模型可触达的绕过路径。
    """
    return settings.flag('HARNESS_NEVER_DO', True)


def actions() -> list[str]:
    """人能读的完整清单（``/status`` 与文档用）。"""
    return sorted({what for _p, what in _COMMAND_RULES}
                  | {what for _t, _p, what in _ARG_RULES}
                  | {what for _p, what in _FORM_RULES})
