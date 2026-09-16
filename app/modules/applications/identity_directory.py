"""读取由运维/公共身份同步程序维护的可信目录，不接受浏览器提交身份归属。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from fastapi import HTTPException, Request
from pydantic import Field, ValidationError, model_validator

from app.modules.applications.identity import Actor
from app.modules.applications.publication_schemas import ApplicationPayload, ApplicationVisibility


class DirectoryEntry(ApplicationPayload):
    """可信身份目录中的基础编号与名称。"""

    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)


class DirectoryProject(DirectoryEntry):
    """携带所属组织的可信项目资料。"""

    organization_id: str = Field(min_length=1, max_length=200)


class DirectoryMember(DirectoryEntry):
    """可信成员的角色、组织、项目归属和启用状态。"""

    role: Literal["admin", "org_admin", "publisher", "user"] = "user"
    organization_id: str = Field(min_length=1, max_length=200)
    project_ids: list[str] = Field(default_factory=list, max_length=100)
    active: bool = True


class IdentityDirectory(ApplicationPayload):
    """由可信同步程序维护的组织、项目和成员快照。"""

    organizations: list[DirectoryEntry] = Field(default_factory=list, max_length=10000)
    projects: list[DirectoryProject] = Field(default_factory=list, max_length=10000)
    members: list[DirectoryMember] = Field(default_factory=list, max_length=100000)

    @model_validator(mode="after")
    def references(self) -> Self:
        """检查目录编号唯一性和组织、项目引用完整性。"""
        for collection in (self.organizations, self.projects, self.members):
            if len({item.id for item in collection}) != len(collection):
                raise ValueError("目录编号重复")
        organizations = {item.id for item in self.organizations}
        projects = {item.id for item in self.projects}
        if any(item.organization_id not in organizations for item in self.projects + self.members):
            raise ValueError("身份归属组织不存在")
        if any(set(item.project_ids) - projects for item in self.members):
            raise ValueError("身份归属项目不存在")
        return self


def read_directory(path: str | Path) -> IdentityDirectory:
    """读取并校验受限大小的目录文件，异常时返回服务未就绪。"""
    try:
        source = Path(path)
        if source.stat().st_size > 20_000_000:
            raise ValueError("目录文件过大")
        return IdentityDirectory.model_validate_json(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        raise HTTPException(503, "身份目录尚未就绪，请联系管理员检查同步配置") from None


def configured_directory(request: Request) -> IdentityDirectory | None:
    """按请求缓存一次可信目录快照，未配置目录时返回空值。"""
    path = request.app.state.settings.applications_identity_directory
    if not path:
        return None
    # 一次请求固定一个快照；下一次请求重新读取，以便撤权及时生效。
    if not hasattr(request.state, "application_identity_directory"):
        request.state.application_identity_directory = read_directory(path)
    return request.state.application_identity_directory


def visible_directory(request: Request, actor: Actor) -> dict[str, list[dict]]:
    """按系统或单位管理权限筛选可用于配置开放范围的目录。"""
    directory = configured_directory(request)
    if directory is None or actor.role not in ("admin", "org_admin"):
        return {"organizations": [], "projects": [], "members": []}
    own = actor.organization_id
    return {
        "organizations": [
            item.model_dump()
            for item in directory.organizations
            if actor.role == "admin" or item.id == own
        ],
        "projects": [
            {"id": item.id, "name": item.name}
            for item in directory.projects
            if actor.role == "admin" or item.organization_id == own
        ],
        "members": [
            {"id": item.id, "name": item.name}
            for item in directory.members
            if item.active and (actor.role == "admin" or item.organization_id == own)
        ],
    }


def publication_defaults(request: Request, actor: Actor) -> dict:
    """仅带出当前用户资料；显示用单位名不作为授权依据。"""
    directory = configured_directory(request)
    if directory is not None:
        organizations = [item.model_dump() for item in directory.organizations]
    elif request.app.state.settings.auth_mode == "disabled":
        from app.modules.applications.identity import DIRECTORY

        organizations = DIRECTORY["organizations"]
    else:
        organizations = []
    owner = next(
        (item["name"] for item in organizations if item["id"] == actor.organization_id), ""
    )
    return {
        "owner": owner,
        "contact": actor.name,
        "organizationId": actor.organization_id,
        "ownerAvailable": bool(owner),
    }


def validate_visibility_targets(
    request: Request, actor: Actor, payload: ApplicationVisibility
) -> None:
    """检查开放目标是否存在且属于当前管理员可选择范围。"""
    if configured_directory(request) is None or payload.scope == "all":
        return
    allowed = {item["id"] for item in visible_directory(request, actor)[payload.scope]}
    if set(payload.targets) - allowed:
        raise HTTPException(422, "开放范围包含不存在或当前管理员无权选择的对象")
