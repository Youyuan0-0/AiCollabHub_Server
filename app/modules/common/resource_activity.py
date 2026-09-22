"""收藏、评分、关注、下载与管理配置在共用业务表中的读写。"""

from copy import deepcopy
from datetime import UTC
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import String, cast, delete, insert, select, update

from app.db.foundation import identifier, iso, utc_now
from app.modules.common.resource_files import ResourceFiles


class ResourceActivity(ResourceFiles):
    """资源会话的共用能力，正式行为与外部临时运行状态分开保存。"""

    def interaction_entries(
        self, subject: str | None = None, resource_id: str | None = None
    ) -> list[dict]:
        """组合三个独立互动表，保持旧接口的当前用户互动视图。"""
        entries = {}
        users = self.table("users")
        for table_name, field in (
            ("resource_favorites", "favorite"),
            ("resource_follows", "subscribed"),
            ("resource_ratings", "rating"),
        ):
            table = self.table(table_name)
            query = (
                select(table, users.c.iam_subject)
                .join(users, users.c.id == table.c.user_id)
                .where(table.c.resource_type == self.kind)
            )
            if subject is not None:
                query = query.where(users.c.iam_subject == subject)
            if resource_id is not None:
                query = query.where(table.c.resource_id == identifier(resource_id))
            for row in self.db.execute(query).mappings():
                item_id, owner = str(row["resource_id"]), row["iam_subject"]
                key = f"{owner}:{item_id}"
                entry = entries.setdefault(
                    key,
                    {
                        "id": key,
                        "actorId": owner,
                        "userId": owner,
                        self.resource_key: item_id,
                        "favorite": False,
                        "subscribed": False,
                        "rating": None,
                        "comment": "",
                        "updatedAt": iso(row["created_at"]),
                    },
                )
                if field == "rating":
                    entry.update(
                        rating=row["rating"],
                        comment=row["comment"] or "",
                        updatedAt=iso(row["updated_at"]),
                    )
                else:
                    entry[field] = True
        return list(entries.values())

    def save_interaction(self, value: dict) -> None:
        """按用户和资源串行合并互动，收藏取消不会覆盖并发评分。"""
        subject = value.get("actorId") or value["userId"]
        resource_id = identifier(value[self.resource_key])
        self.lock(f"interaction:{subject}:{resource_id}")
        user_id = self.foundation.user_id(self.db, subject)
        for name, field in (
            ("resource_favorites", "favorite"),
            ("resource_follows", "subscribed"),
            ("resource_ratings", "rating"),
        ):
            if field not in value:
                continue
            table = self.table(name)
            condition = (
                table.c.resource_type == self.kind,
                table.c.resource_id == resource_id,
                table.c.user_id == user_id,
            )
            previous = self.db.execute(select(table).where(*condition)).mappings().first()
            selected = value[field]
            if field != "rating" and not selected:
                self.db.execute(delete(table).where(*condition))
            elif field == "rating" and selected is not None:
                fields = {
                    "rating": selected,
                    "comment": value.get("comment") or None,
                    "updated_at": utc_now(),
                }
                if previous:
                    self.db.execute(update(table).where(*condition).values(**fields))
                else:
                    self.db.execute(
                        insert(table).values(
                            id=uuid4(),
                            resource_type=self.kind,
                            resource_id=resource_id,
                            user_id=user_id,
                            **fields,
                        )
                    )
            elif selected and previous is None:
                self.db.execute(
                    insert(table).values(
                        id=uuid4(),
                        resource_type=self.kind,
                        resource_id=resource_id,
                        user_id=user_id,
                    )
                )

    def get_visibility(self, resource_id: str) -> dict:
        """读取指定用户范围，未配置时沿用平台默认全部登录用户可见。"""
        if resource_id in getattr(self, "_catalog_visibility", {}):
            return deepcopy(self._catalog_visibility[resource_id])
        policies, targets, users = (
            self.table("resource_visibility_policies"),
            self.table("resource_visibility_targets"),
            self.table("users"),
        )
        self.lock("visibility:" + resource_id)
        row = (
            self.db.execute(
                select(policies).where(
                    policies.c.resource_type == self.kind,
                    policies.c.resource_id == identifier(resource_id),
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return {"scope": "all", "targets": [], "revision": 1}
        subjects = list(
            self.db.scalars(
                select(users.c.iam_subject)
                .join(targets, targets.c.user_id == users.c.id)
                .where(targets.c.policy_id == row["id"])
                .order_by(users.c.iam_subject)
            )
        )
        return {"scope": row["scope"], "targets": subjects, "revision": row["revision"]}

    def put_visibility(self, resource_id: str, value: dict) -> None:
        """仅保存已确认的 all/members，不将模拟组织和项目写入用户范围表。"""
        if hasattr(self, "_catalog_visibility"):
            self._catalog_visibility.pop(resource_id, None)
        if value["scope"] not in {"all", "members"}:
            raise HTTPException(422, "当前开放范围支持全部用户或指定用户")
        self.lock("visibility:" + resource_id)
        policies, targets = (
            self.table("resource_visibility_policies"),
            self.table("resource_visibility_targets"),
        )
        condition = (
            policies.c.resource_type == self.kind,
            policies.c.resource_id == identifier(resource_id),
        )
        row = self.db.execute(select(policies).where(*condition)).mappings().first()
        current = row["revision"] if row else 1
        if value["revision"] != current + 1:
            raise HTTPException(409, "开放范围已更新，请刷新")
        policy_id = row["id"] if row else uuid4()
        if row is None:
            self.db.execute(
                insert(policies).values(
                    id=policy_id,
                    resource_type=self.kind,
                    resource_id=identifier(resource_id),
                    scope="all",
                    revision=1,
                )
            )
        self.db.execute(
            update(policies)
            .where(policies.c.id == policy_id)
            .values(scope=value["scope"], revision=current + 1, updated_at=utc_now())
        )
        self.db.execute(delete(targets).where(targets.c.policy_id == policy_id))
        if value["scope"] == "members":
            if not value["targets"]:
                raise HTTPException(422, "请选择可见用户")
            for subject in sorted(set(value["targets"])):
                self.db.execute(
                    insert(targets).values(
                        id=uuid4(),
                        policy_id=policy_id,
                        user_id=self.foundation.user_id(self.db, subject),
                    )
                )

    def configuration_record(self, key: str) -> dict:
        """读取已保存的模块配置，首次读取使用受类型模型校验的默认配置。"""
        self.lock("configuration:" + key)
        table = self.table("module_configurations")
        row = (
            self.db.execute(
                select(table).where(table.c.module_code == self.module, table.c.config_key == key)
            )
            .mappings()
            .first()
        )
        if row is None:
            return deepcopy(self.repository.configuration) if key == "form" else {}
        return {
            **deepcopy(row["value"]),
            "revision": row["revision"],
            "updatedAt": iso(row["updated_at"]),
        }

    def get_configuration(self) -> dict:
        """取得模块发布表单和管理显示配置。"""
        return self.configuration_record("form")

    def put_configuration(self, value: dict) -> None:
        """保存经业务模型验证的配置，凭证不进入此表。"""
        self.save_configuration("form", value)

    def save_configuration(self, key: str, value: dict) -> None:
        """使用修订锁更新配置，首次保存也执行相同并发检查。"""
        self.lock("configuration:" + key)
        table = self.table("module_configurations")
        condition = (table.c.module_code == self.module, table.c.config_key == key)
        row = self.db.execute(select(table).where(*condition)).mappings().first()
        old_revision = row["revision"] if row else self.repository.configuration.get("revision", 1)
        if value["revision"] != old_revision + 1:
            raise HTTPException(409, "配置已变化，请刷新")
        fields = {
            "value": {
                name: deepcopy(item)
                for name, item in value.items()
                if name not in {"revision", "updatedAt", "updatedBy"}
            },
            "revision": old_revision + 1,
            "updated_at": utc_now(),
            "updater_id": self.foundation.user_id(self.db, value["updatedBy"])
            if value.get("updatedBy")
            else None,
        }
        if row is None:
            self.db.execute(
                insert(table).values(
                    id=uuid4(), module_code=self.module, config_key=key, value={}, revision=1
                )
            )
        self.db.execute(update(table).where(*condition).values(**fields))

    def entries(self, kind: str) -> list[dict]:
        """按业务能力查询对应明细，外部临时运行结果使用共享协调存储。"""
        if kind == "interactions":
            return self.interaction_entries()
        if kind == "policy":
            value = self.configuration_record("workflow")
            return [value] if value else []
        if kind == "audit":
            if self.kind == "tool":
                return self.tool_audit_entries()
            logs = self.table("operation_logs")
            return [
                deepcopy(row["data"])
                for row in self.db.execute(
                    select(logs)
                    .where(
                        logs.c.target_type
                        == {"knowledge": "knowledge_base"}.get(self.kind, self.kind)
                    )
                    .order_by(logs.c.created_at.desc())
                ).mappings()
            ]
        return self.repository.runtime.list(kind) if self.repository.runtime else []

    def tool_audit_entries(self) -> list[dict]:
        """补齐工具日志展示字段；旧摘要缺失的操作和身份明确标为未记录。"""
        logs, resources, users = (
            self.table("operation_logs"),
            self.table("ai_tools"),
            self.table("users"),
        )
        rows = self.db.execute(
            select(logs, resources.c.name.label("resource_name"), users.c.display_name)
            .outerjoin(resources, logs.c.target_id == cast(resources.c.id, String))
            .outerjoin(users, logs.c.user_id == users.c.id)
            .where(logs.c.target_type == "tool")
            .order_by(logs.c.created_at.desc())
        ).mappings()
        return [
            {
                "id": str(row["id"]),
                "toolId": row["target_id"],
                "name": row["resource_name"] or "历史工具（名称未记录）",
                "reviewer": row["display_name"] or "系统／历史未记录",
                "decision": "历史操作（具体结论未记录）",
                "organizationId": "",
                "reason": "",
                **deepcopy(row["data"]),
                "createdAt": iso(row["created_at"].astimezone(UTC)),
            }
            for row in rows
        ]

    def entry(self, kind: str, key: str) -> dict | None:
        """取得幂等对象；临时调用状态读取失败时不能当作尚未执行。"""
        self.lock(kind + ":" + key)
        if kind == "interactions":
            subject, _, resource_id = key.rpartition(":")
            return next(iter(self.interaction_entries(subject, resource_id)), None)
        if kind == "policy":
            return self.configuration_record("workflow") or None
        if kind == "tutorial_files":
            return self.file_view(key)
        if kind == "review_files":
            refs = self.table("resource_file_references")
            row = (
                self.db.execute(
                    select(refs).where(
                        refs.c.resource_type == self.kind,
                        refs.c.purpose == "review_report",
                        refs.c.file_id == identifier(key),
                    )
                )
                .mappings()
                .first()
            )
            return self.file_view(key, dict(row)) if row else None
        if kind == "downloads":
            return self.download_entry(key)
        return self.repository.runtime.get(kind, key) if self.repository.runtime else None

    def put_entry(self, kind: str, key: str, value: dict) -> None:
        """把业务行为写入专用表，外部调用状态不塞入资源主表。"""
        if kind == "interactions":
            self.save_interaction(value)
        elif kind == "policy":
            self.save_configuration("workflow", value)
        elif kind == "tutorial_files":
            self.register_file(value)
        elif kind == "review_files":
            self.stage_report(value)
        elif kind == "downloads":
            self.save_download(key, value)
        elif kind == "audit":
            self.write_operation(key, value)
        else:
            if self.repository.runtime is None:
                raise HTTPException(503, "runtime_state_unavailable")
            self.repository.runtime.put(kind, key, value)
            if kind == "runs" and value.get("status") == "succeeded":
                self.write_operation(key, value, action="invoke")

    def write_operation(self, key: str, value: dict, *, action: str = "update") -> None:
        """只记录成功操作的必要摘要，不复制调用输入、输出、令牌或临时链接。"""
        table = self.table("operation_logs")
        self.lock("operation:" + key)
        if self.db.scalar(
            select(table.c.id).where(
                table.c.data["eventId"].astext == key,
                table.c.target_type == {"knowledge": "knowledge_base"}.get(self.kind, self.kind),
            )
        ):
            return
        subject = value.get("actorId") or value.get("reviewerId") or value.get("createdBy")
        summary = {
            name: deepcopy(value[name])
            for name in (
                "id",
                "applicationId",
                "toolId",
                "resourceId",
                "applicationName",
                "name",
                "organizationId",
                "reviewer",
                "decision",
                "version",
                "status",
                "mode",
                "durationMs",
                "createdAt",
                "completedAt",
                "action",
                "reason",
                "actorName",
            )
            if name in value
        }
        summary["eventId"] = key
        self.db.execute(
            insert(table).values(
                id=uuid4(),
                actor_type="user" if subject else "system",
                user_id=self.foundation.user_id(self.db, subject) if subject else None,
                action=action,
                target_type={"knowledge": "knowledge_base"}.get(self.kind, self.kind),
                target_id=value.get(self.resource_key) or value.get("resourceId"),
                data=summary,
            )
        )

    @staticmethod
    def download_key(key: str) -> str:
        """将模块请求键编码为下载表允许的有界幂等键。"""
        return sha256(key.encode()).hexdigest()

    def save_download(self, key: str, value: dict) -> None:
        """保存资料响应的下载请求，实际完成由传输结束后确认。"""
        self.lock("download:" + key)
        table = self.table("resource_downloads")
        user_id = self.foundation.user_id(self.db, value.get("actorId") or value["userId"])
        digest = self.download_key(key)
        if self.db.scalar(
            select(table.c.id).where(table.c.user_id == user_id, table.c.idempotency_key == digest)
        ):
            return
        self.db.execute(
            insert(table).values(
                id=uuid4(),
                resource_type=self.kind,
                resource_id=identifier(value[self.resource_key]),
                user_id=user_id,
                version_id=identifier(value["versionId"]) if value.get("versionId") else None,
                idempotency_key=digest,
                status="link_issued",
            )
        )

    def download_entry(self, key: str) -> dict | None:
        """按已固定的正式版本重建重试响应，不使用新的版本冒充原下载。"""
        table = self.table("resource_downloads")
        row = (
            self.db.execute(
                select(table).where(
                    table.c.resource_type == self.kind,
                    table.c.idempotency_key == self.download_key(key),
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        versions = self.table(self.prefix + "_versions")
        snapshot = (
            deepcopy(
                self.db.scalar(
                    select(versions.c.snapshot).where(versions.c.id == row["version_id"])
                )
            )
            if row["version_id"]
            else {}
        )
        snapshot.pop("publicationContext", None)
        snapshot["contact"] = ""
        return {
            "filename": f"{row['resource_id']}-{row['version_id']}.json",
            "content": snapshot,
            "downloadedAt": iso(row["created_at"]),
        }
