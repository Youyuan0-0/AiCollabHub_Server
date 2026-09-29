"""个人消息只读取现有临时通知，不加载应用详情、互动和运行记录。"""

from urllib.parse import quote

from fastapi import HTTPException

from app.modules.common.identity import Actor
from app.modules.common.resource_runtime import ResourceRuntime
from app.modules.common.transaction_ports import ResourceRepository
from app.modules.profile.resources import ProfileResourcesRepository


def message_view(item: dict) -> dict:
    """将应用审核和更新通知转换为个人中心展示契约。"""
    return {
        "id": item["id"],
        "title": item["title"],
        "content": "",
        "kind": "review" if item["kind"] == "review" else "update",
        "createdAt": item["createdAt"],
        "read": item["read"],
        "href": "/profile?tab=publications&kind=application"
        if item["kind"] == "review"
        else "/applications?resourceId=" + quote(item["applicationId"], safe=""),
    }


def mark_repository_messages_read(
    repository: ResourceRepository, actor: Actor, ids: list[str]
) -> list[str]:
    """隔离演示沿用原仓储事务，整批验证归属后写入，失败时全部回滚。"""
    ids = list(dict.fromkeys(ids))
    with repository.transaction() as session:
        records = [session.entry("notifications", key) for key in ids]
        if any(
            not item
            or item["recipient"] != actor.id
            or item["kind"] not in {"review", "new_version"}
            for item in records
        ):
            raise HTTPException(404, "消息已过期或无权访问，请刷新列表")
        for item in records:
            session.put_entry("notifications", item["id"], {**item, "read": True})
    return ids


class ProfileMessagesService:
    """保留通知原有存储与期限，按接收人和当前资源可见范围过滤。"""

    def __init__(
        self,
        resources: ProfileResourcesRepository,
        runtime: ResourceRuntime | None,
    ) -> None:
        self.resources = resources
        self.runtime = runtime

    def list_for_user(self, actor: Actor) -> list[dict]:
        """通知为空或只有本人审核结果时，不发起任何资源数据库查询。"""
        items = [
            item
            for item in (self.runtime.list("notifications") if self.runtime else [])
            if item["recipient"] == actor.id and item["kind"] in {"review", "new_version"}
        ]
        available = self.resources.visible_application_ids(
            actor,
            {item["applicationId"] for item in items if item["kind"] == "new_version"},
        )
        return [
            message_view(item)
            for item in sorted(
                items,
                key=lambda item: (item["createdAt"], item["id"]),
                reverse=True,
            )
            if item["kind"] == "review" or item["applicationId"] in available
        ]

    def mark_read(self, actor: Actor, ids: list[str]) -> list[str]:
        """一批只处理明确传入的本人消息，不误标记请求期间新增的通知。"""
        if self.runtime is None:
            raise HTTPException(503, "runtime_state_unavailable")
        return self.runtime.mark_notifications_read(actor.id, ids)
