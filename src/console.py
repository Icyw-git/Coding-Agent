"""控制台输出兜底：任何 print 都不能让一个回合失败。

Windows 上 Python 的 stdout 编码跟随系统 locale（中文机器是 GBK）。当 stdout 被重定向
到日志文件（nohup、计划任务、IDE 后台）时也是 GBK，于是打印模型回答、工具输出里
任何 emoji（例如 😊 U+1F60A）都会抛 ``UnicodeEncodeError``。而 print 出现在回合路径
里（hooks 的调试行、非流式回复、cron 提示词），异常会一路冒到 API 的 turn worker，
用户看到的是「处理未完成：'gbk' codec can't encode character ...」——一次成功的回合
被杀掉，只因日志里有个表情。

这里统一把标准流的 errors 调成 ``backslashreplace``：
- 编码仍然按原样（GBK 控制台不会变乱码）；
- 编码不了的字符写成 ``\\U0001f60a``，既不崩、也还能在日志里认出是什么；
- 只改错误策略，不重设 encoding，避免把日志/终端编码整个换掉。
"""
from __future__ import annotations

import sys

_ON_ERROR = 'backslashreplace'


def harden_stream(stream, *, errors: str = _ON_ERROR):
    """把一个文本流改成“编码失败也不抛”。不支持 reconfigure 的流原样返回。"""
    if stream is None:
        return stream
    reconfigure = getattr(stream, 'reconfigure', None)
    if reconfigure is None:
        return stream
    try:
        reconfigure(errors=errors)
    except (ValueError, OSError, AttributeError):
        # 已经被替换/关闭的流（测试里的 StringIO、已被 pytest 捕获）保持原样
        pass
    return stream


def harden_stdio() -> None:
    """入口处调用一次：stdout/stderr 遇到无法编码的字符不再抛异常。"""
    for stream in (sys.stdout, sys.stderr):
        harden_stream(stream)
