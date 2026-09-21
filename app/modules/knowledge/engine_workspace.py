"""知识引擎业务编排：先鉴权再调用，按资源隔离操作，保留外部处理状态。"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException, UploadFile

from app.modules.common.identity import Actor, can_manage
from app.modules.common.service import timestamp
from app.modules.common.toolchain.contracts import authorize, source_reference
from app.modules.common.toolchain.mock import DocumentedToolchainMock
from app.modules.knowledge.engine import KnowledgeEngineProvider
from app.modules.knowledge.schemas import (
    ChunkQuery,
    CreateInput,
    DocumentInput,
    IndexInput,
    RetrievalInput,
)
from app.modules.knowledge.workspace import KnowledgeWorkspace
from app.storage.client import MinioStorage


class EngineKnowledgeWorkspace(KnowledgeWorkspace):
    """复用知识库资料、审核和权限，仅替换文档与引擎相关操作。"""

    def __init__(
        self, provider: KnowledgeEngineProvider, mock: DocumentedToolchainMock | None
    ) -> None:
        super().__init__(provider)
        # 旧种子没有外部绑定，不能把模拟资源当成引擎资源继续操作。
        self.resources.clear()
        self.engine = provider
        self.mock = mock
        self._busy: set[str] = set()

    def create(self, payload: CreateInput, actor: Actor, identity: dict) -> dict:
        """先校验参数及权限，保留创建记录；外部失败不伪造成功绑定。"""
        authorize(self.engine.client.settings, actor)
        tenant_id = None
        if self.engine.configuration.get("ownership") == "organization":
            tenant_id = next(
                (
                    binding["tenantId"]
                    for binding in self.engine.configuration.get("tenantBindings", [])
                    if binding["organizationId"] == actor.organization_id and actor.organization_id
                ),
                None,
            )
            if not tenant_id:
                raise HTTPException(422, "当前账号所属单位尚未配置知识库 tenantId，请联系管理员")
        self.engine.index_options(payload.index_config or IndexInput())
        item = super().create(payload, actor, identity)
        with self.lock:
            self.resources[item["id"]].update(status="syncing", toolchainMode=self.engine.mode)
        try:
            identifier = self.engine.create(
                payload, payload.name[:150] + "-" + uuid4().hex[:12], tenant_id
            )
        except HTTPException:
            with self.lock:
                self.resources[item["id"]].update(
                    status="failed", engineMessage="外部建库结果未确认，请联系管理员核对"
                )
            raise
        with self.lock:
            self.resources[item["id"]].update(
                externalId=identifier,
                externalTenantId=tenant_id,
                status="ready",
                source=source_reference(
                    self.engine.client.settings, "knowledge-engine", identifier
                ),
                engineMessage="模拟引擎结果，仅验证接口流程" if self.mock else "",
            )
        return self.resource(item["id"], actor)

    @contextmanager
    def operation(self, identifier: str, actor: Actor, *, manage: bool = True) -> Iterator[dict]:
        """在任何外部写入前校验管理权限；不持有全库锁等待网络。"""
        with self.lock:
            item = self.resource(identifier, actor)
            if manage:
                authorize(self.engine.client.settings, actor)
                if item.get("createdBy") != actor.id and not can_manage(actor, item):
                    raise HTTPException(403, "只有维护者可以管理知识库")
                publication = self.records.get(item.get("publicationId"), {})
                if publication.get("status") == "pending":
                    raise HTTPException(409, "审核中的知识库不可修改")
            if not item.get("externalId"):
                raise HTTPException(409, "知识库外部创建尚未确认，请联系管理员核对")
            if identifier in self._busy:
                raise HTTPException(409, "该知识库有操作正在进行，请稍后刷新")
            self._busy.add(identifier)
        try:
            yield item
        finally:
            with self.lock:
                self._busy.discard(identifier)

    def commit_documents(self, identifier: str, documents: list[dict], status: str) -> None:
        """保存已验证的外部文档状态；轮询不创建虚假的业务版本。"""
        with self.lock:
            previous = self.resources[identifier]
            if (
                status == "ready"
                and documents
                and (previous.get("status") != "ready" or not previous.get("indexBuiltAt"))
            ):
                # 指南没有构建完成时间，记录平台首次确认完成的时刻，轮询不重复刷新。
                previous["indexBuiltAt"] = timestamp()
            self.resources[identifier].update(
                documents=documents,
                documentCount=len(documents),
                totalSize=sum(item["sizeBytes"] for item in documents),
                chunkCount=None if documents else 0,
                status="syncing" if status == "parsing" else status,
                updatedAt=timestamp(),
                engineMessage="模拟引擎结果，仅验证接口流程" if self.mock else "",
            )

    def ensure_ready_for_submission(self, identifier: str, actor: Actor) -> None:
        """提交审核前确认处理完成且无并行操作；调用方持锁保存审核记录。"""
        with self.lock:
            item = self.resource(identifier, actor)
            if item["status"] != "ready" or identifier in self._busy:
                raise HTTPException(409, "知识库操作尚未完成，请先刷新处理状态再提交审核")

    def refresh(self, identifier: str, actor: Actor) -> dict:
        """刷新处理进度与文件列表，未知状态继续显示处理中。"""
        with self.operation(identifier, actor, manage=False) as item:
            status = self.engine.status(item["externalId"])
            documents = self.engine.documents(item["externalId"])
            if any(doc["status"] == "failed" for doc in documents):
                status = "failed"
            elif any(doc["status"] != "ready" for doc in documents):
                status = "parsing"
            self.commit_documents(identifier, documents, status)
        return self.resource(identifier, actor)

    def upload_files(
        self,
        identifier: str,
        actor: Actor,
        files: list[UploadFile],
        config: IndexInput,
        storage: MinioStorage | None,
    ) -> dict:
        """真实传递文件字节，登记后显示处理中；签名文件地址只传给知识引擎。"""
        with self.operation(identifier, actor) as item:
            fields = ("mode", "embedding_model", "chunk_mode", "chunk_size", "chunk_overlap")
            previous = IndexInput(**item["indexConfig"])
            if any(getattr(config, field) != getattr(previous, field) for field in fields):
                raise HTTPException(422, "当前指南未明确现有库的分段配置修改接口，请按建库配置上传")
            self.engine.index_options(config)
            if not 1 <= len(files) <= 20:
                raise HTTPException(422, "每批请选择 1–20 个文件")
            checked = []
            total = 0
            for file in files:
                file.file.seek(0, 2)
                size = file.file.tell()
                file.file.seek(0)
                name = file.filename or ""
                if "/" in name or "\\" in name or any(ord(char) < 32 for char in name):
                    raise HTTPException(422, "文件名称无效")
                try:
                    metadata = DocumentInput(name=name, size=size)
                except ValueError:
                    raise HTTPException(
                        422, "支持 PDF、Word、Excel、TXT、Markdown，单文件不超过 100 MB"
                    ) from None
                total += size
                checked.append((file, metadata))
            if total > 200 * 1024 * 1024:
                raise HTTPException(413, "每批文件总大小不能超过 200 MB")
            if self.mock is None and storage is None:
                raise HTTPException(503, "知识库文件存储尚未配置")
            registered = []
            for file, metadata in checked:
                if self.mock:
                    url = self.mock.store_file(metadata.name, file.file.read())
                else:
                    key = f"knowledge/{identifier}/{uuid4().hex}/{metadata.name}"
                    storage.put_object(
                        key,
                        file.file,
                        length=metadata.size,
                        content_type=file.content_type or "application/octet-stream",
                    )
                    url = storage.presigned_read_url(key, expires=timedelta(hours=24))
                registered.append({"name": metadata.name, "url": url})
            try:
                self.engine.add(item["externalId"], registered)
                documents = self.engine.documents(item["externalId"])
                self.commit_documents(identifier, documents, "parsing")
                with self.lock:
                    self.resources[identifier]["uploadedAt"] = timestamp()
            except HTTPException:
                # 上传登记可能已执行，保留文件等待状态核对，不能自动删除或重发。
                with self.lock:
                    self.resources[identifier].update(
                        status="syncing", engineMessage="登记结果尚未确认，请刷新处理状态"
                    )
                raise
        return self.resource(identifier, actor)

    def read_chunks(self, identifier: str, actor: Actor, query: ChunkQuery) -> dict:
        """按文档拉取片段；未选文档时合并后分页，并限制读取总量。"""
        item = self.resource(identifier, actor)
        if not item.get("externalId"):
            raise HTTPException(409, "知识库尚未绑定外部资源")
        docs = [
            doc
            for doc in item["documents"]
            if doc["status"] == "ready"
            and (not query.document_name or doc["name"] == query.document_name)
        ]
        values = []
        for doc in docs:
            for offset in range(0, 10000, 100):
                page = self.engine.chunks(item["externalId"], doc, offset, 100)
                values.extend(page["items"])
                if offset + len(page["items"]) >= page["total"]:
                    break
                if not page["items"]:
                    raise HTTPException(502, "知识引擎片段分页不完整")
            else:
                raise HTTPException(422, "文档片段过多，请缩小查询范围")
            if len(values) > 10000:
                raise HTTPException(422, "片段过多，请选择单个文档")
        from app.modules.common.fixtures import paginate

        return paginate(
            [value for value in values if query.q.casefold() in value["text"].casefold()],
            query.page,
            query.page_size,
        )

    def delete_file(self, identifier: str, actor: Actor, name: str) -> dict:
        """以已授权列表解析文档 ID；同名歧义时拒绝删除。"""
        with self.operation(identifier, actor) as item:
            docs = [doc for doc in item["documents"] if doc["name"] == name or doc["id"] == name]
            if len(docs) != 1:
                raise HTTPException(409, "文档不存在或名称重复，请刷新后按文档编号操作")
            self.engine.delete(item["externalId"], docs[0]["id"])
            self.commit_documents(identifier, self.engine.documents(item["externalId"]), "parsing")
        return self.resource(identifier, actor)

    def retrieve(self, item: dict, payload: RetrievalInput) -> list[dict]:
        """只允许已登记的就绪文档出现在结果中，权限及统计由公共流程处理。"""
        return self.engine.retrieve(item["externalId"], item["documents"], payload)
