"""资源开放目标目录；演示对象和正式身份目录均由服务端决定。"""

from fastapi import HTTPException, Request
from sqlalchemy import select

from app.modules.common.access_schemas import ScopeTargets
from app.modules.common.identity import DIRECTORY, Actor
from app.modules.common.identity_directory import configured_directory, visible_directory


def visibility_directory(request: Request, actor: Actor) -> dict[str, list[dict]] | None:
    """返回当前管理员可选目标；None 表示正式目录未配置，允许手动填写已确认编号。"""
    foundation = getattr(request.app.state, "foundation", None)
    if foundation is not None:
        users = foundation.table("users")
        with foundation.transaction(is_readonly=True) as session:
            members = [
                {"id": row.iam_subject, "name": row.display_name}
                for row in session.execute(
                    select(users.c.iam_subject, users.c.display_name)
                    .where(users.c.status == "active")
                    .order_by(users.c.display_name, users.c.id)
                )
            ]
        directory = visible_directory(request, actor)
        if actor.role != "admin":
            allowed = {item["id"] for item in directory["members"]}
            members = [item for item in members if item["id"] in allowed]
        return {**directory, "members": members}
    if configured_directory(request) is not None:
        return visible_directory(request, actor)
    if request.app.state.settings.auth_mode != "disabled":
        return None
    # 演示身份只有固定对象，不能保存一个永远无法登录匹配的目标；单位管理员限本单位对象。
    return {
        "organizations": [
            dict(item)
            for item in DIRECTORY["organizations"]
            if actor.role == "admin" or item["id"] == actor.organization_id
        ],
        "members": [
            {"id": item["id"], "name": item["name"]}
            for item in DIRECTORY["members"]
            if actor.role == "admin" or item["organizationId"] == actor.organization_id
        ],
        "projects": [
            dict(item)
            for item in DIRECTORY["projects"]
            if actor.role == "admin" or item["id"] in actor.project_ids
        ],
    }


def validate_visibility_targets(request: Request, actor: Actor, payload: ScopeTargets) -> None:
    """按详情页同一目录校验目标；已配置但为空的目录不能当作未配置而跳过校验。"""
    if payload.scope == "all":
        return
    directory = visibility_directory(request, actor)
    if directory is None:
        return
    if payload.scope not in directory:
        raise HTTPException(422, "当前身份目录未提供该开放范围")
    allowed_ids = {item["id"] for item in directory[payload.scope]}
    if set(payload.targets) - allowed_ids:
        raise HTTPException(422, "开放范围包含不存在或当前管理员无权选择的对象")
