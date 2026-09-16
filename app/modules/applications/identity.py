"""场景应用身份与权限判断；正式会话和开发演示分别通过身份适配器提供 Actor。"""

from dataclasses import asdict, dataclass
from secrets import token_urlsafe
from threading import RLock
from typing import Literal, Protocol

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class Actor:
    """场景应用的可信操作身份，携带角色、组织与项目归属。"""

    id: str
    name: str
    role: str
    organization_id: str
    project_ids: tuple[str, ...] = ()

    def public(self) -> dict:
        """转换为前端使用的 camelCase 身份资料。"""
        data = asdict(self)
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "organizationId": data["organization_id"],
            "projectIds": data["project_ids"],
        }


ACTORS = (
    Actor("demo-publisher", "研发发布者", "publisher", "org-research", ("project-a",)),
    Actor("demo-reader", "研发普通用户", "user", "org-research", ("project-a",)),
    Actor("demo-outsider", "生产普通用户", "user", "org-production", ("project-b",)),
    Actor("demo-org-admin", "研发单位管理员", "org_admin", "org-research", ("project-a",)),
    Actor("demo-admin", "系统管理员", "admin", "org-group"),
)
DEFAULT_USER, DEFAULT_ADMIN = ACTORS[0], ACTORS[-1]
DIRECTORY = {
    "organizations": [
        {"id": "org-research", "name": "研发单位"},
        {"id": "org-production", "name": "生产单位"},
        {"id": "org-group", "name": "集团管理"},
    ],
    "projects": [
        {"id": "project-a", "name": "研发项目组"},
        {"id": "project-b", "name": "生产项目组"},
    ],
    "members": [actor.public() for actor in ACTORS],
}


class IdentityProvider(Protocol):
    """按用户端或管理端入口解析可信身份的适配器契约。"""

    def resolve(self, request: Request, audience: Literal["user", "admin"]) -> Actor:
        """解析指定入口的操作身份；认证失败时抛出 HTTP 异常。"""
        ...


class SessionIdentityProvider:
    """启用登录时，复用公共认证依赖验证过的用户或管理端会话。"""

    def resolve(self, request: Request, audience: Literal["user", "admin"]) -> Actor:
        """复用认证依赖已验证的会话，并在配置目录时检查成员状态和角色。"""
        identity = getattr(request.state, "business_identity", None)
        if not identity or identity["scope"] != audience:
            raise HTTPException(401, "authentication_required")
        from app.modules.applications.identity_directory import configured_directory

        directory = configured_directory(request)
        if directory is not None:
            member = next((item for item in directory.members if item.id == identity["id"]), None)
            if member is None or not member.active:
                raise HTTPException(403, "账号未同步或已停用，请联系管理员")
            if audience == "admin" and member.role not in ("admin", "org_admin"):
                raise HTTPException(403, "当前账号没有应用管理权限")
            return Actor(
                member.id,
                member.name,
                member.role,
                member.organization_id,
                tuple(member.project_ids),
            )
        # 管理端会话已校验准入；普通登录用户可以发布自己的应用。
        # IAM 尚未提供组织和项目归属，不从浏览器字段或演示账号补出这些权限。
        return Actor(
            identity["id"],
            identity["name"],
            "admin" if audience == "admin" else "publisher",
            "",
            (),
        )


class DemoIdentityProvider:
    """为开发演示保存预置账号会话，不接受浏览器自报的角色或组织。"""

    def __init__(self) -> None:
        self._sessions: dict[str, Actor] = {}
        self._lock = RLock()

    def select(self, actor_id: str) -> tuple[str, Actor]:
        """选择预置账号，返回随机会话令牌和对应身份。"""
        actor = next((item for item in ACTORS if item.id == actor_id), None)
        if actor is None:
            raise HTTPException(422, "请选择已配置的演示账号")
        token = token_urlsafe(32)
        with self._lock:
            self._sessions[token] = actor
        return token, actor

    def resolve(self, request: Request, audience: Literal["user", "admin"]) -> Actor:
        """读取演示会话；未携带令牌时使用该入口的默认演示身份。"""
        token = request.cookies.get(f"application_{audience}")
        # 无会话时保留开发入口；任意浏览器角色/组织字段不参与授权。
        if not token:
            return DEFAULT_ADMIN if audience == "admin" else DEFAULT_USER
        with self._lock:
            actor = self._sessions.get(token)
        if actor is None:
            raise HTTPException(401, "演示会话已失效，请重新选择演示账号")
        return actor


def current_actor(request: Request, is_admin: bool = False) -> Actor:
    """通过已配置的身份适配器取得当前入口的操作身份。"""
    return request.app.state.application_identity.resolve(request, "admin" if is_admin else "user")


def require_admin(actor: Actor) -> None:
    """要求系统或单位管理员角色，否则返回 403。"""
    if actor.role not in ("admin", "org_admin"):
        raise HTTPException(403, "当前账号没有应用管理权限")


def can_manage(actor: Actor, record: dict) -> bool:
    """判断是否为系统管理员，或记录所属单位的管理员。"""
    return actor.role == "admin" or (
        actor.role == "org_admin"
        and bool(actor.organization_id)
        and record.get("organizationId", "") == actor.organization_id
    )


def can_edit(actor: Actor, record: dict) -> bool:
    """允许有管理权限的身份或记录原发布者编辑。"""
    return can_manage(actor, record) or (
        actor.role == "publisher" and record.get("createdBy", DEFAULT_USER.id) == actor.id
    )


def visible(actor: Actor, resource: dict, visibility: dict) -> bool:
    """按开放范围检查可见性；有权管理该资源的身份始终可见。"""
    if can_manage(actor, resource):
        return True
    scope, targets = visibility["scope"], visibility["targets"]
    return (
        scope == "all"
        or (scope == "organizations" and actor.organization_id in targets)
        or (scope == "members" and actor.id in targets)
        or (scope == "projects" and bool(set(actor.project_ids).intersection(targets)))
    )
