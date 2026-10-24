"""Request models shared by the HTTP route modules.

Pydantic stays optional: CLI-only installs import this module without the API
extras, and the models simply do not exist there.
"""

try:
    from pydantic import BaseModel, Field
except ImportError:  # Keep CLI-only installs importable without API dependencies.
    BaseModel = None
    Field = None

# 请求模型必须放在模块层：端点注解经 `from __future__ import annotations`
# 惰性化后，FastAPI 只在注册模块的全局命名空间解析；若定义在工厂函数内部会
# 解析失败，request 被降级成 query 参数（表现为 422 Field required）。
if BaseModel is not None:
    class CreateSessionRequest(BaseModel):
        id: str | None = Field(default=None, min_length=1)
        workspace: str | None = Field(default=None, min_length=1)


    class SendMessageRequest(BaseModel):
        content: str = Field(min_length=1)

    class SessionActionRequest(BaseModel):
        action: str
        session: dict


    class WorkspaceRequest(BaseModel):
        path: str = Field(min_length=1)


    class CreateWorkspaceRequest(BaseModel):
        path: str = Field(min_length=1)
        name: str | None = None


    class CreateTaskRequest(BaseModel):
        command: str = Field(min_length=1)
        cwd: str | None = None


    class ResolvePermissionRequest(BaseModel):
        allow: bool
        by: str = Field(default='remote', min_length=1)
        scope: str | None = None     # 'session' = 本会话同类自动通过
        # 刻意不接受 klass：同类判定键由服务端按被审批的 tool/args 重算，
        # 调用方只能决定「允许吗、要不要扩到同类」，不能决定扩到哪一类。


    class RevokePermissionRequest(BaseModel):
        session_id: str | None = None


    class PolicyRequest(BaseModel):
        sandbox_mode: str | None = None       # 缺省表示不改这一档
        approval_policy: str | None = None


    class ForkSessionRequest(BaseModel):
        upto_seq: int | None = None  # 复制到的事件序号（缺省=全部）


    class RenameSessionRequest(BaseModel):
        title: str = Field(min_length=1)  # 新会话主题
