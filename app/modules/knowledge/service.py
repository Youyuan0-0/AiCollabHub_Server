"""知识库业务：权限、版本、文档处理、索引生命周期与召回统计。"""

import logging
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from fastapi import HTTPException, Request, UploadFile

from app.modules.common.fixtures import paginate
from app.modules.common.identity import Actor, can_edit, can_manage, require_admin, visible
from app.modules.common.schemas import PageQuery
from app.modules.knowledge.documents import (
    MAX_CHUNKS,
    MAX_FILE_BYTES,
    document_name,
    parse_document,
    split_text,
)
from app.modules.knowledge.repository import KnowledgeRepository
from app.modules.knowledge.retrieval import RetrievalProvider, cosine, keyword_scores
from app.modules.knowledge.reviews import is_approved, pending_review, review_state
from app.modules.knowledge.schemas import (
    AdminInput,
    AdminUpdate,
    ChunkQuery,
    CreateInput,
    IndexInput,
    RetrievalInput,
    VisibilityInput,
)
from app.modules.knowledge.storage import KnowledgeFiles

logger = logging.getLogger(__name__)
INDEX_FIELDS = ("mode", "embeddingModel", "chunkMode", "chunkSize", "chunkOverlap")
PUBLIC_FIELDS = (
    "id",
    "name",
    "description",
    "descriptionMarkdown",
    "category",
    "owner",
    "createdAt",
    "updatedAt",
    "documentCount",
    "chunkCount",
    "storageBytes",
    "status",
    "indexConfig",
    "revision",
    "tone",
    "needsRebuild",
    "indexError",
)


def timestamp() -> str:
    """返回带时区的系统时间，供版本记录及任务租约使用。"""
    return datetime.now(UTC).isoformat()


class KnowledgeService:
    """协调仓储、原文存储和检索提供器，不从浏览器接受角色或统计值。"""

    def __init__(
        self, repository: KnowledgeRepository, files: KnowledgeFiles, provider: RetrievalProvider
    ) -> None:
        self.repository = repository
        self.files = files
        self.provider = provider

    def record(
        self, base_id: str, actor: Actor, *, admin: bool = False, edit: bool = False
    ) -> dict:
        """先检查可见性，防止通过详情、分段或召回绕过列表权限。"""
        if admin:
            require_admin(actor)
        record = self.repository.get(base_id)
        if (
            record
            and not admin
            and not can_edit(actor, record)
            and hasattr(self.repository, "public_record")
        ):
            record = self.repository.public_record(record)
        allowed = record is not None and (
            can_manage(actor, record)
            if admin
            else can_edit(actor, record)
            or (self.is_published(record) and visible(actor, record, record["visibility"]))
        )
        if not allowed:
            raise HTTPException(404, "知识库不存在或无权访问")
        if edit and not can_edit(actor, record):
            raise HTTPException(403, "当前账号不能修改此知识库")
        return record

    @staticmethod
    def is_published(record: dict) -> bool:
        """审批通过且已启用才能对非维护者公开；内存测试实现保持原协议。"""
        return is_approved(record) and record.get("listingStatus", "listed") == "listed"

    def set_listing(self, base_id: str, status: str, revision: int, actor: Actor) -> dict:
        """仅管理员可以启停已经有正式版本的知识库。"""
        require_admin(actor)
        if not hasattr(self.repository, "set_listing"):
            raise HTTPException(409, "请使用数据库知识库")
        return self.repository.set_listing(base_id, status, revision, actor)

    def present(self, record: dict, actor: Actor, *, admin: bool = False) -> dict:
        """公开响应使用白名单，管理响应也不暴露原文件路径、正文缓存或向量。"""
        result = {key: record[key] for key in PUBLIC_FIELDS}
        result["indexConfig"] = IndexInput(**result["indexConfig"]).model_dump(by_alias=True)
        result["listingStatus"] = record.get("listingStatus", "listed")
        # 重启或 worker 中断不会伪装成永远构建中；租约过期后允许用户明确重试。
        expired = (
            record["status"] == "syncing"
            and (datetime.now(UTC) - datetime.fromisoformat(record["jobStartedAt"])).total_seconds()
            >= 900
        )
        if expired:
            result.update(status="failed", indexError="索引任务已中断或超时，请重建索引")
        result["canEdit"] = can_edit(actor, record)
        if admin or result["canEdit"]:
            result["review"] = review_state(record)
        result["documents"] = [
            {
                key: doc[key]
                for key in (
                    "id",
                    "name",
                    "type",
                    "size",
                    "sizeBytes",
                    "updatedAt",
                    "status",
                    "chunkCount",
                    "error",
                )
            }
            | ({"review": review_state(doc)} if admin or result["canEdit"] else {})
            for doc in record["documents"]
            if admin or result["canEdit"] or is_approved(doc)
        ]
        result["uploadedAt"] = max(
            (doc["updatedAt"] for doc in result["documents"]), default=result["createdAt"]
        )
        # 普通读者只能看到通过审批的文档及对应统计，待审名称和意见不能泄露。
        if not admin and not result["canEdit"]:
            result["documentCount"] = len(result["documents"])
            result["storageBytes"] = sum(doc["sizeBytes"] for doc in result["documents"])
        # 只投影超时状态，不在读取时修改仓储；下次写操作按租约撤销旧任务。
        if expired:
            for doc in result["documents"]:
                if doc["status"] == "parsing":
                    doc.update(status="failed", error="索引任务已中断或超时，请重试")
        statuses = [
            doc["status"] for doc in result["documents"] if doc["status"] != "awaiting_review"
        ]
        result["indexProgress"] = {
            "total": len(statuses),
            "ready": statuses.count("ready"),
            "failed": statuses.count("failed"),
            "pending": sum(status not in ("ready", "failed") for status in statuses),
        }
        if admin:
            result["reviewHistory"] = record.get("reviewHistory", [])
            # 与数据集后台一致，范围管理能力由可信身份计算，不接受客户端自报。
            result["canManageVisibility"] = can_manage(actor, record)
            result.update(
                {
                    key: record[key]
                    for key in (
                        "dataLevel",
                        "contact",
                        "organizationId",
                        "createdBy",
                        "versions",
                        "indexBuildMs",
                        "retrievalCount",
                        "retrievalSuccessCount",
                        "retrievalElapsedMs",
                        "publicationId",
                        "reviewRevision",
                    )
                }
            )
            result["visibility"] = {**record["visibility"], "revision": record["revision"]}
            count = record["retrievalCount"]
            result["averageRetrievalMs"] = (
                round(record["retrievalElapsedMs"] / count, 2) if count else None
            )
            result["retrievalSuccessRate"] = (
                record["retrievalSuccessCount"] / count if count else None
            )
        return result

    def catalog(
        self, actor: Actor, query: PageQuery | None = None, *, admin: bool = False
    ) -> list[dict] | dict:
        """在关键词过滤和分页前隔离不可见资源，目录不会泄露未授权的总数。"""
        if admin:
            require_admin(actor)
        source_records = self.repository.list()
        if not admin and hasattr(self.repository, "public_record"):
            source_records = [
                item
                for original in source_records
                if (
                    item := original
                    if can_edit(actor, original)
                    else self.repository.public_record(original)
                )
            ]
        records = [
            record
            for record in source_records
            if (
                can_manage(actor, record)
                if admin
                else can_edit(actor, record)
                or (self.is_published(record) and visible(actor, record, record["visibility"]))
            )
        ]
        records.sort(key=lambda record: (record["updatedAt"], record["id"]), reverse=True)
        if query:
            keyword = query.q.strip().casefold()
            records = [
                record
                for record in records
                if keyword
                in (record["name"] + " " + record["description"] + " " + record["owner"]).casefold()
            ]
        items = [self.present(record, actor, admin=admin) for record in records]
        return paginate(items, query.page, query.page_size) if query else items

    def published_resources(self, actor: Actor) -> list[dict]:
        """向应用依赖目录提供已通过创建审批且当前账号可见的本地知识库。"""
        records = self.repository.list()
        if hasattr(self.repository, "public_record"):
            records = [
                item for record in records if (item := self.repository.public_record(record))
            ]
        return [
            self.present(record, actor)
            for record in records
            if self.is_published(record) and visible(actor, record, record["visibility"])
        ]

    def publications(self, actor: Actor) -> list[dict]:
        """将本人的创建审批投影为个人中心协议，不读取工具链工作区的独立记录。"""
        return [
            {
                "id": record["id"],
                "resourceId": record["id"],
                "data": {"name": record["name"]},
                "status": review_state(record)["status"],
                "updatedAt": record["updatedAt"],
            }
            for record in self.repository.list()
            if record["createdBy"] == actor.id
        ]

    def create(self, payload: CreateInput, actor: Actor, defaults: dict) -> dict:
        """创建独立私有知识库；系统字段及授权组织始终取可信身份。"""
        if actor.role not in ("publisher", "admin", "org_admin"):
            raise HTTPException(403, "当前账号没有创建知识库的权限")
        now = timestamp()
        record = {
            **payload.model_dump(by_alias=True),
            "id": str(uuid4()),
            "owner": (payload.owner if isinstance(payload, AdminInput) else "")
            or defaults["owner"],
            "contact": (payload.contact if isinstance(payload, AdminInput) else "")
            or defaults["contact"],
            "dataLevel": payload.data_level if isinstance(payload, AdminInput) else "一般数据",
            "organizationId": actor.organization_id,
            "createdBy": actor.id,
            "visibility": {"scope": "members", "targets": [actor.id]},
            "createdAt": now,
            "updatedAt": now,
            "documents": [],
            "chunks": [],
            "documentCount": 0,
            "chunkCount": 0,
            "storageBytes": 0,
            "tone": "blue",
            "status": "ready",
            "needsRebuild": False,
            "indexError": "",
            "versions": [],
            "indexBuildMs": None,
            "retrievalCount": 0,
            "retrievalSuccessCount": 0,
            "retrievalElapsedMs": 0.0,
            "jobId": None,
            "review": pending_review(actor),
            "reviewHistory": [],
        }
        record.update(publicationId=record["id"], reviewRevision=1)
        return self._save(record, None, actor, "创建知识库")

    def _save(self, record: dict, expected: int | None, actor: Actor, action: str) -> dict:
        """汇总真实容量和分段数量，并将维护记录与当前快照一并提交。"""
        record["updatedAt"] = timestamp()
        record["documentCount"] = len(record["documents"])
        record["storageBytes"] = sum(doc["sizeBytes"] for doc in record["documents"])
        record["chunkCount"] = len(record["chunks"])
        record["versions"].append(
            {
                "revision": (expected or 0) + 1,
                "action": action,
                "actor": actor.name,
                "createdAt": record["updatedAt"],
            }
        )
        return self.repository.save(record, expected)

    def _check_revision(self, record: dict, revision: int) -> None:
        """校验页面修订及构建租约；过期任务由下一次人工操作接管。"""
        if record["revision"] != revision:
            raise HTTPException(409, "知识库已更新，请刷新后重试")
        # 进程中断的任务在租约到期后允许手动重建；旧 worker 由 jobId 隔离。
        if (
            record["status"] == "syncing"
            and (datetime.now(UTC) - datetime.fromisoformat(record["jobStartedAt"])).total_seconds()
            < 900
        ):
            raise HTTPException(409, "索引正在处理，请完成后再修改")
        if record["status"] == "syncing":
            # 过期任务的人工修改会撤销旧租约，迟到 worker 不得写回旧资料。
            record.update(status="failed", jobId=None, needsRebuild=True)

    def settings(
        self, base_id: str, config: IndexInput, revision: int, actor: Actor, *, admin: bool = False
    ) -> dict:
        """召回参数即时生效；模型或分段参数变更会清空旧索引并标记待重建。"""
        record = self.record(base_id, actor, admin=admin, edit=True)
        self._check_revision(record, revision)
        self._set_config(record, config)
        return self._save(record, revision, actor, "修改检索配置")

    def _set_config(self, record: dict, config: IndexInput) -> None:
        """索引相关参数变化时清理旧结果，召回阈值和数量只影响检索。"""
        value = config.model_dump(by_alias=True)
        if (
            any(value[key] != record["indexConfig"][key] for key in INDEX_FIELDS)
            and record["documents"]
        ):
            record.update(chunks=[], status="stale", needsRebuild=True, indexError="", jobId=None)
            for doc in record["documents"]:
                doc.update(
                    status="pending" if is_approved(doc) else "awaiting_review",
                    chunkCount=0,
                    error="",
                )
        record["indexConfig"] = value

    def update(self, base_id: str, payload: AdminUpdate, actor: Actor) -> dict:
        """管理端维护完整资料，修改展示单位不能改变可信组织归属。"""
        record = self.record(base_id, actor, admin=True, edit=True)
        self._check_revision(record, payload.revision)
        self._set_config(record, payload.index_config)
        record.update(payload.model_dump(by_alias=True, exclude={"revision", "index_config"}))
        return self._save(record, payload.revision, actor, "修改管理资料")

    def visibility(self, base_id: str, payload: VisibilityInput, actor: Actor) -> dict:
        """仅管理端可以修改开放范围；目标目录由路由先行校验。"""
        record = self.record(base_id, actor, admin=True, edit=True)
        self._check_revision(record, payload.revision)
        record["visibility"] = payload.model_dump(exclude={"revision"})
        return self._save(record, payload.revision, actor, "修改开放范围")

    def upload(
        self,
        base_id: str,
        uploads: list[UploadFile],
        revision: int,
        actor: Actor,
        *,
        admin: bool = False,
    ) -> dict:
        """保存实际原文件后原子登记；同名文档拒绝覆盖，批次失败清理本次全部文件。"""
        record = self.record(base_id, actor, admin=admin, edit=True)
        self._check_revision(record, revision)
        # 数据库待审申请尚无正式知识库外键；在写入对象存储前拒绝，避免失败批次留下文件。
        if hasattr(self.repository, "ensure_uploadable"):
            self.repository.ensure_uploadable(base_id)
        if not 1 <= len(uploads) <= 20 or len(record["documents"]) + len(uploads) > 500:
            raise HTTPException(422, "每批最多上传 20 个文档，每库最多 500 个文档")
        names = [document_name(upload.filename or "") for upload in uploads]
        existing = {doc["name"].casefold() for doc in record["documents"]}
        if len({name.casefold() for name in names}) != len(names) or existing.intersection(
            name.casefold() for name in names
        ):
            raise HTTPException(409, "存在同名文档，请重命名或先删除旧文档")
        written = []
        try:
            total = 0
            for upload, name in zip(uploads, names, strict=True):
                if hasattr(self.files, "save_document"):
                    identifier, size = self.files.save_document(upload.file, name, actor)
                else:
                    identifier, size = self.files.save(upload.file)
                written.append(identifier)
                total += size
                if total > MAX_FILE_BYTES:
                    raise HTTPException(413, "单批文档合计不能超过 100MB，请分批上传")
                record["documents"].append(
                    {
                        "id": identifier,
                        "name": name,
                        "type": name.rsplit(".", 1)[-1].upper(),
                        "size": f"{size / 1024:.1f} KB",
                        "sizeBytes": size,
                        "updatedAt": timestamp(),
                        "status": "awaiting_review",
                        "review": pending_review(actor),
                        "chunkCount": 0,
                        "error": "",
                    }
                )
            # 上传只登记待审核原文，不调用解析或模型，也不打断已批准内容的召回。
            return self._save(record, revision, actor, "上传文档并提交审批")
        except Exception:
            for identifier in written:
                self.files.delete(identifier)
            raise

    def rebuild(
        self,
        base_id: str,
        revision: int,
        actor: Actor,
        *,
        admin: bool = False,
        failed_only: bool = False,
    ) -> dict:
        """完整重建或仅重试未就绪文档；两种方式均使用已保存配置并校验修订。"""
        record = self.record(base_id, actor, admin=admin, edit=True)
        self._check_revision(record, revision)
        if not is_approved(record):
            raise HTTPException(409, "知识库创建审批通过后才能构建索引")
        if failed_only and not any(
            is_approved(doc) and doc["status"] != "ready" for doc in record["documents"]
        ):
            raise HTTPException(409, "没有需要重试的文档")
        return self._start_job(
            record,
            revision,
            actor,
            "重试未完成文档" if failed_only else "重建索引",
            pending_only=failed_only,
        )

    def _start_job(
        self,
        record: dict,
        revision: int,
        actor: Actor,
        action: str,
        *,
        pending_only: bool = False,
    ) -> dict:
        """登记新的索引任务，并按本次范围清理旧分段。

        pending_only=True 时只选择已批准且尚未就绪的文档，保留其他成功分段；
        False 表示重建所有已批准文档。jobId 标识本轮任务，后台每次保存都要核对它，
        防止被后续重建取代的旧任务继续写入。此方法只登记状态，不执行解析或模型调用。
        """
        selected = {
            doc["id"]
            for doc in record["documents"]
            if is_approved(doc) and (not pending_only or doc["status"] != "ready")
        }
        record.update(
            status="syncing",
            jobId=uuid4().hex,
            jobStartedAt=timestamp(),
            needsRebuild=True,
            indexError="",
            chunks=[chunk for chunk in record["chunks"] if chunk["documentId"] not in selected],
        )
        for doc in record["documents"]:
            if doc["id"] in selected:
                doc.update(status="parsing", chunkCount=0, error="")
        return self._save(record, revision, actor, action)

    def process(self, base_id: str, job_id: str) -> None:
        """逐篇处理已批准文档，在解析和可选的向量生成之后保存进度。

        单篇可预期失败保留在文档状态中，并继续处理后续文档；全部处理结束后，
        任一文档失败都会使知识库保持待重建状态。任务被替换时立即停止写入，
        已就绪文档可在增量重试中复用，不因其他文档失败而重复调用模型。
        """
        started = perf_counter()
        record = self.repository.get(base_id)
        if record is None or not job_id or record["jobId"] != job_id or not is_approved(record):
            return
        config = IndexInput(**record["indexConfig"])
        try:
            for doc in record["documents"]:
                # 就绪项来自同一配置下的先前成功索引，增量任务直接复用。
                if not is_approved(doc) or doc["status"] == "ready":
                    continue
                try:
                    text = parse_document(doc["name"], self.files.read(doc["id"]))
                    texts = split_text(text, config)
                    if len(record["chunks"]) + len(texts) > MAX_CHUNKS:
                        raise HTTPException(422, "知识库分段总数超过 20000，请拆分知识库")
                    vectors = (
                        self.provider.embed(texts, config.embedding_model)
                        if config.mode == "高质量模式"
                        else [[] for _ in texts]
                    )
                    record["chunks"].extend(
                        {
                            "id": f"{doc['id']}-{index}",
                            "documentId": doc["id"],
                            "documentName": doc["name"],
                            "index": index,
                            "text": text,
                            "characterCount": len(text),
                            "vector": vector,
                        }
                        for index, (text, vector) in enumerate(zip(texts, vectors, strict=True), 1)
                    )
                    doc.update(status="ready", chunkCount=len(texts), error="")
                except HTTPException as error:
                    doc.update(status="failed", chunkCount=0, error=str(error.detail))
                record["chunkCount"] = len(record["chunks"])
                record["jobStartedAt"] = timestamp()
                saved = self._save_progress(record, job_id)
                if saved is None:
                    return
                # 保留当前循环的文档引用，仅跟进原子保存产生的新修订。
                record["revision"] = saved["revision"]
            failed = any(doc["status"] == "failed" for doc in record["documents"])
            record.update(
                status="failed" if failed else "ready",
                needsRebuild=failed,
                indexError="部分文档处理失败，请查看文档错误后重试" if failed else "",
                indexBuildMs=round((perf_counter() - started) * 1000, 2),
                jobId=None,
            )
            record["updatedAt"] = timestamp()
            self._save_progress(record, job_id)
        except Exception:
            logger.exception("Knowledge indexing failed for %s", base_id)
            current = self.repository.get(base_id)
            if current and current["jobId"] == job_id:
                # 未预期异常也必须结束文档的处理中状态，允许两端明确重试未完成项。
                for doc in current["documents"]:
                    if doc["status"] == "parsing":
                        doc.update(status="failed", error="处理意外中断，请重试", chunkCount=0)
                current.update(
                    status="failed",
                    needsRebuild=True,
                    jobId=None,
                    indexError="索引处理失败，请重建索引重试",
                )
                self.repository.save(current, current["revision"])

    def _save_progress(self, record: dict, job_id: str) -> dict | None:
        """核对任务归属，合并最新召回统计后尝试保存索引进度。

        仅对修订冲突重新读取并重试，最多五次；这不是重新解析文档或重发模型请求。
        返回 None 表示知识库已删除或 jobId 已被替换，调用方必须结束旧任务。
        """
        for _ in range(5):
            current = self.repository.get(record["id"])
            if current is None or current["jobId"] != job_id:
                return None
            for field in ("retrievalCount", "retrievalSuccessCount", "retrievalElapsedMs"):
                record[field] = current[field]
            try:
                return self.repository.save(record, current["revision"])
            except HTTPException as error:
                if error.status_code != 409:
                    raise
        raise HTTPException(409, "索引进度更新冲突，请重建索引重试")

    def delete_document(
        self, base_id: str, name: str, revision: int, actor: Actor, *, admin: bool = False
    ) -> dict:
        """原子删除文档与对应分段，再清理原文件，避免删除后仍能召回旧内容。"""
        record = self.record(base_id, actor, admin=admin, edit=True)
        self._check_revision(record, revision)
        doc = next((doc for doc in record["documents"] if doc["name"] == name), None)
        if doc is None:
            raise HTTPException(404, "知识文档不存在")
        record["documents"] = [item for item in record["documents"] if item["id"] != doc["id"]]
        record["chunks"] = [item for item in record["chunks"] if item["documentId"] != doc["id"]]
        if all(item["status"] == "ready" for item in record["documents"] if is_approved(item)):
            record.update(status="ready", needsRebuild=False, indexError="", jobId=None)
        result = self._save(record, revision, actor, "删除文档：" + name)
        try:
            self.files.delete(doc["id"])
        except OSError:
            logger.warning("Unreferenced knowledge document needs cleanup: %s", doc["id"])
        return result

    def chunks(self, base_id: str, query: ChunkQuery, actor: Actor, *, admin: bool = False) -> dict:
        """只返回可展示的分段字段，向量不进入浏览器或预览。"""
        record = self.record(base_id, actor, admin=admin)
        approved_ids = {doc["id"] for doc in record["documents"] if is_approved(doc)}
        items = [
            {key: chunk[key] for key in ("id", "documentName", "index", "text", "characterCount")}
            for chunk in record["chunks"]
            if is_approved(record)
            and chunk["documentId"] in approved_ids
            and (not query.document_name or chunk["documentName"] == query.document_name)
            and query.q.casefold() in chunk["text"].casefold()
        ]
        return paginate(items, query.page, query.page_size)

    def retrieve(
        self, base_id: str, payload: RetrievalInput, actor: Actor, *, admin: bool = False
    ) -> list[dict]:
        """基于当前就绪索引召回真实片段，仅统计次数和耗时，不永久保存问题与结果。"""
        record = self.record(base_id, actor, admin=admin)
        if not is_approved(record):
            raise HTTPException(409, "知识库创建审批尚未通过")
        if record["status"] != "ready" or record["needsRebuild"]:
            raise HTTPException(409, "索引尚未就绪，请完成索引构建后再检索")
        config = payload.config
        if any(
            config.model_dump(by_alias=True)[key] != record["indexConfig"][key]
            for key in INDEX_FIELDS
        ):
            raise HTTPException(409, "索引配置尚未保存并重建，请使用当前已生效的配置")
        started, succeeded = perf_counter(), False
        try:
            approved_ids = {doc["id"] for doc in record["documents"] if is_approved(doc)}
            chunks = [chunk for chunk in record["chunks"] if chunk["documentId"] in approved_ids]
            if not chunks:
                succeeded = True
                return []
            texts = [chunk["text"] for chunk in chunks]
            scores = (
                keyword_scores(payload.query, texts)
                if config.keyword_search
                else [0.0] * len(texts)
            )
            if config.semantic_search:
                vector = self.provider.embed([payload.query], config.embedding_model)[0]
                similarities = [cosine(vector, chunk["vector"]) for chunk in chunks]
                scores = (
                    similarities
                    if not config.keyword_search
                    else [
                        (keyword + similarity) / 2
                        for keyword, similarity in zip(scores, similarities, strict=True)
                    ]
                )
            ranked = sorted(range(len(chunks)), key=lambda index: (-scores[index], index))
            if config.reranking:
                ranked = ranked[: max(40, config.top_k * 3)]
                reranked = self.provider.rerank(payload.query, [texts[index] for index in ranked])
                for index, score in zip(ranked, reranked, strict=True):
                    scores[index] = score
                ranked.sort(key=lambda index: (-scores[index], index))
            results = [
                {
                    "id": chunks[index]["id"],
                    "title": chunks[index]["documentName"],
                    "excerpt": chunks[index]["text"],
                    "score": round(scores[index], 6),
                    "location": f"第 {chunks[index]['index']} 段",
                }
                for index in ranked
                if scores[index] > 0 and scores[index] >= config.similarity_threshold
            ][: config.top_k]
            current = self.record(base_id, actor, admin=admin)
            # 模型请求期间可能发生删除、重建或收回权限，旧快照不再用于展示结果。
            if (
                current["status"] != "ready"
                or current["indexConfig"] != record["indexConfig"]
                or [chunk["id"] for chunk in current["chunks"]] != [chunk["id"] for chunk in chunks]
            ):
                raise HTTPException(409, "知识库内容已变化，请刷新后重新检索")
            succeeded = True
            return results
        finally:
            self._metrics(base_id, (perf_counter() - started) * 1000, succeeded)

    def _metrics(self, base_id: str, elapsed: float, succeeded: bool) -> None:
        """按最新修订累加毫秒耗时及请求状态，有限重试避免覆盖并发修改。"""
        # 独立读取最新版本后累加，统计写入不覆盖同时发生的配置或权限修改。
        for _ in range(5):
            current = self.repository.get(base_id)
            current["retrievalCount"] += 1
            current["retrievalSuccessCount"] += int(succeeded)
            current["retrievalElapsedMs"] += elapsed
            try:
                self.repository.save(current, current["revision"])
                return
            except HTTPException as error:
                if error.status_code != 409:
                    raise
        raise HTTPException(409, "召回统计更新冲突，请重试")


def knowledge_service(request: Request) -> KnowledgeService:
    """从应用实例取得共享服务，用户端与管理端读取相同仓储。"""
    return request.app.state.knowledge_service
