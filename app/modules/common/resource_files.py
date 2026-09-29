"""资源教程、审核报告与共用文件表的关联及历史归档。"""

from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import delete, insert, select

from app.db.foundation import identifier, iso, utc_now


class ResourceFiles:
    """在资源业务事务内管理文件元数据，文件正文由上传服务写入 MinIO。"""

    def file_view(self, file_id: str | UUID, reference: dict | None = None) -> dict | None:
        """返回经过归属校验的服务端元数据，调用方负责生成受控下载响应。"""
        files = self.table("files")
        key = identifier(file_id)
        rows = getattr(self, "_file_rows", {})
        row = rows.get(key)
        if key not in rows:
            row = self.db.execute(select(files).where(files.c.id == key)).mappings().first()
        if row is None or row["status"] != "ready":
            return None
        actor = (
            self.foundation.user(self.db, row["uploader_id"])
            if row["uploader_id"]
            else {"id": "", "name": ""}
        )
        previews = self.table("file_previews")
        cached_previews = getattr(self, "_file_previews", {})
        preview = cached_previews.get(key)
        if key not in cached_previews:
            preview = (
                self.db.execute(
                    select(files)
                    .join(previews, previews.c.preview_file_id == files.c.id)
                    .where(previews.c.source_file_id == row["id"])
                )
                .mappings()
                .first()
            )
        result = {
            "id": str(row["id"]),
            "name": row["file_name"],
            "size": row["size_bytes"],
            "contentType": row["mime_type"],
            "createdBy": actor["id"],
            "createdAt": iso(row["created_at"]),
            "organizationId": "",
            "bucket": row["bucket"],
            "objectKey": row["object_key"],
            "previewKey": preview["object_key"] if preview else row["object_key"],
            "previewContentType": preview["mime_type"] if preview else row["mime_type"],
            "kind": "video" if (row["mime_type"] or "").startswith("video/") else "document",
            "securityLevel": "public" if "/public/" in row["object_key"] else "internal",
        }
        if reference is not None:
            publications = self.table(self.prefix + "_publications")
            review_id = reference["owner_id"] if reference["owner_type"] == "review" else None
            if review_id:
                reviews = self.table("resource_reviews")
                review = getattr(self, "_review_rows", {}).get(review_id)
                publication_id = (
                    review["publication_id"]
                    if review is not None
                    else self.db.scalar(
                        select(reviews.c.publication_id).where(reviews.c.id == review_id)
                    )
                )
            else:
                publication_id = reference["owner_id"]
            publication = getattr(self, "_publication_rows", {}).get(publication_id)
            if publication is None:
                publication = (
                    self.db.execute(select(publications).where(publications.c.id == publication_id))
                    .mappings()
                    .one()
                )
            context = publication["data"].get("publicationContext", {})
            result.update(
                publicationId=str(publication_id),
                publicationRevision=publication["revision"],
                reviewId=str(review_id) if review_id else None,
                **{
                    self.resource_key: str(
                        publication[self.prefix + "_id"] or context.get("plannedResourceId")
                    )
                },
            )
        return result

    def register_file(self, value: dict) -> None:
        """登记已成功上传的不可变文件及预览，尚未使用的原件设置回收期限。"""
        files = self.table("files")
        file_id = identifier(value["id"])
        if self.db.scalar(select(files.c.id).where(files.c.id == file_id)) is not None:
            return
        uploader_id = self.foundation.user_id(self.db, value["createdBy"])
        self.db.execute(
            insert(files).values(
                id=file_id,
                uploader_id=uploader_id,
                file_name=value["name"],
                size_bytes=value["size"],
                mime_type=value["contentType"],
                bucket=value.get("bucket") or self.foundation.settings.minio_bucket,
                object_key=value["objectKey"],
                content_hash=value.get("contentHash"),
                status="ready",
                expires_at=utc_now() + timedelta(days=1),
            )
        )
        if value.get("previewKey") and value["previewKey"] != value["objectKey"]:
            if "previewSize" not in value:
                raise HTTPException(422, "缺少预览文件大小")
            preview_id = uuid4()
            self.db.execute(
                insert(files).values(
                    id=preview_id,
                    uploader_id=uploader_id,
                    file_name="preview.html",
                    size_bytes=value["previewSize"],
                    mime_type=value["previewContentType"],
                    bucket=value.get("bucket") or self.foundation.settings.minio_bucket,
                    object_key=value["previewKey"],
                    content_hash=value.get("previewHash"),
                    status="ready",
                )
            )
            self.db.execute(
                insert(self.table("file_previews")).values(
                    id=uuid4(),
                    source_file_id=file_id,
                    preview_file_id=preview_id,
                )
            )

    def owner_files(self, owner_type: str, owner_id: UUID, purpose: str) -> list[dict]:
        """列出指定申请、审核或版本拥有的原始文件。"""
        refs = self.table("resource_file_references")
        rows = getattr(self, "_owner_file_rows", {}).get((owner_type, owner_id, purpose))
        if rows is None:
            rows = self.db.execute(
                select(refs)
                .where(
                    refs.c.resource_type == self.kind,
                    refs.c.owner_type == owner_type,
                    refs.c.owner_id == owner_id,
                    refs.c.purpose == purpose,
                )
                .order_by(refs.c.created_at, refs.c.id)
            ).mappings()
        return [
            self.file_view(row["file_id"], dict(row) if purpose == "review_report" else None)
            for row in rows
        ]

    def prefetch_owner_files(self, owner_type: str, owner_ids: list[UUID], purpose: str) -> None:
        """在只读快照内批量加载文件与预览；空关联同样缓存，避免重复查询。"""
        if not self.is_readonly:
            raise ValueError("File prefetch requires a read-only transaction")
        if not owner_ids:
            return
        refs, files, previews = (
            self.table("resource_file_references"),
            self.table("files"),
            self.table("file_previews"),
        )
        self._owner_file_rows = getattr(self, "_owner_file_rows", {})
        for key in owner_ids:
            self._owner_file_rows[owner_type, key, purpose] = []
        file_ids = set()
        for row in self.db.execute(
            select(refs)
            .where(
                refs.c.resource_type == self.kind,
                refs.c.owner_type == owner_type,
                refs.c.owner_id.in_(owner_ids),
                refs.c.purpose == purpose,
            )
            .order_by(refs.c.created_at, refs.c.id)
        ).mappings():
            self._owner_file_rows[owner_type, row["owner_id"], purpose].append(row)
            file_ids.add(row["file_id"])
        if not file_ids:
            return
        self._file_rows = getattr(self, "_file_rows", {})
        self._file_previews = getattr(self, "_file_previews", {})
        self._file_rows.update(dict.fromkeys(file_ids))
        self._file_previews.update(dict.fromkeys(file_ids))
        user_ids = set()
        for row in self.db.execute(select(files).where(files.c.id.in_(file_ids))).mappings():
            self._file_rows[row["id"]] = row
            if row["uploader_id"]:
                user_ids.add(row["uploader_id"])
        for row in self.db.execute(
            select(files, previews.c.source_file_id)
            .join(previews, previews.c.preview_file_id == files.c.id)
            .where(previews.c.source_file_id.in_(file_ids))
        ).mappings():
            if self._file_previews[row["source_file_id"]] is None:
                self._file_previews[row["source_file_id"]] = row
        self.foundation.prefetch_users(self.db, user_ids)

    def reference_file(
        self,
        owner_type: str,
        owner_id: UUID,
        file_id: UUID,
        purpose: str,
        creator_id: UUID,
        *,
        submission_no: int | None = None,
    ) -> None:
        """建立文件关联；数据库同步校验文件状态、申请轮次和不可变历史。"""
        self.db.execute(
            insert(self.table("resource_file_references")).values(
                id=uuid4(),
                resource_type=self.kind,
                owner_type=owner_type,
                owner_id=owner_id,
                file_id=file_id,
                purpose=purpose,
                creator_id=creator_id,
                submission_no=submission_no,
            )
        )

    def sync_tutorials(self, publication_id: UUID, data: dict, creator_id: UUID) -> None:
        """按草稿内容增删申请的教程引用，提交后由数据库触发器冻结。

        删除的是当前申请的关联，不删除文件本体；审核历史和正式版本可能仍引用
        同一文件。新关联同时核对上传者，不能仅凭客户端传来的文件编号借用他人文件。
        """
        expected = {
            identifier(item["attachment"]["id"])
            for item in data.get("tutorials", [])
            if item.get("source") == "file" and item.get("attachment")
        }
        refs = self.table("resource_file_references")
        condition = (
            refs.c.resource_type == self.kind,
            refs.c.owner_type == "publication",
            refs.c.owner_id == publication_id,
            refs.c.purpose == "tutorial",
        )
        existing = {
            row["file_id"]: row
            for row in self.db.execute(select(refs).where(*condition)).mappings()
        }
        for file_id in existing.keys() - expected:
            self.db.execute(delete(refs).where(refs.c.id == existing[file_id]["id"]))
        for file_id in expected - existing.keys():
            files = self.table("files")
            uploader = self.db.scalar(select(files.c.uploader_id).where(files.c.id == file_id))
            if uploader != creator_id:
                raise HTTPException(422, "教程文件不属于申请人")
            self.reference_file("publication", publication_id, file_id, "tutorial", creator_id)

    def archive_tutorials(self, publication_id: UUID, owner_type: str, owner_id: UUID) -> None:
        """把申请教程复制为审核或版本的独立引用，保留原文件编号。

        归档复制引用关系，不复制对象内容；后续申请删改教程时，历史所有者仍能
        保护并读取原文件。此方法必须与对应审核或版本的写入处于同一事务。
        """
        refs = self.table("resource_file_references")
        rows = (
            self.db.execute(
                select(refs).where(
                    refs.c.resource_type == self.kind,
                    refs.c.owner_type == "publication",
                    refs.c.owner_id == publication_id,
                    refs.c.purpose == "tutorial",
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            self.reference_file(owner_type, owner_id, row["file_id"], "tutorial", row["creator_id"])

    def review_files(self, publication_id: str) -> list[dict]:
        """汇总本申请的暂存报告和已经归档的历史报告。"""
        key = identifier(publication_id)
        result = self.owner_files("publication", key, "review_report")
        reviews = self.table("resource_reviews")
        for review_id in self.db.scalars(
            select(reviews.c.id).where(
                reviews.c.resource_type == self.kind, reviews.c.publication_id == key
            )
        ):
            result.extend(self.owner_files("review", review_id, "review_report"))
        return result

    def stage_report(self, value: dict) -> None:
        """上传报告仅关联当前待审轮次，审核后归档操作由结论事务统一处理。"""
        if value.get("reviewId"):
            return
        self.register_file(value)
        publication_id = identifier(value["publicationId"])
        publications = self.table(self.prefix + "_publications")
        publication = (
            self.db.execute(select(publications).where(publications.c.id == publication_id))
            .mappings()
            .one()
        )
        if publication["revision"] != value["publicationRevision"]:
            raise HTTPException(409, "申请已经更新，请刷新后上传")
        self.reference_file(
            "publication",
            publication_id,
            identifier(value["id"]),
            "review_report",
            self.foundation.user_id(self.db, value["createdBy"]),
            submission_no=publication["submission_no"],
        )

    def archive_reports(self, publication: dict, review_id: UUID, review: dict) -> None:
        """先建立审核历史引用，再移除审核者在申请上的暂存关联。

        整个引用转移属于结论事务，避免文件在归档过程中暂时失去保护。未选中的
        报告不在本方法内迁移，已归档的文件本体也不能随着暂存关联一起删除。
        """
        refs = self.table("resource_file_references")
        reviewer = self.foundation.user_id(self.db, review["reviewerId"])
        for file in review.get("reportFiles", []):
            file_id = identifier(file["id"])
            self.reference_file("review", review_id, file_id, "review_report", reviewer)
            self.db.execute(
                delete(refs).where(
                    refs.c.resource_type == self.kind,
                    refs.c.owner_type == "publication",
                    refs.c.owner_id == publication["id"],
                    refs.c.file_id == file_id,
                    refs.c.purpose == "review_report",
                    refs.c.creator_id == reviewer,
                )
            )

    def remove_review_file(self, key: str) -> None:
        """只移除暂存报告引用，历史关联由数据库拒绝删除。"""
        refs = self.table("resource_file_references")
        self.db.execute(
            delete(refs).where(
                refs.c.resource_type == self.kind,
                refs.c.owner_type == "publication",
                refs.c.file_id == identifier(key),
                refs.c.purpose == "review_report",
            )
        )
