"""外部知识引擎的适配契约，与平台本地知识库审批和解析字段隔离。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from app.modules.common.access_schemas import StrictPayload
from app.modules.common.schemas import PageQuery, Payload


class ChunkQuery(PageQuery):
    """知识片段查询的分页、文档名称和关键词参数。"""

    document_name: str = Field(default="", alias="documentName")


class IndexInput(StrictPayload):
    """知识库分段、向量模型与检索阈值配置。"""

    mode: Literal["高质量模式", "经济模式"] = "高质量模式"
    embedding_model: str = Field(default="BGE-M3-Aero", min_length=1)
    chunk_mode: Literal["自动分段", "按段落", "固定长度"] = "自动分段"
    chunk_size: int = Field(default=800, ge=100, le=2000)
    chunk_overlap: int = Field(default=120, ge=0, le=500)
    top_k: int = Field(default=6, ge=1, le=20)
    similarity_threshold: float = Field(default=0.5, ge=0, le=1)
    retrieval_mode: str = "混合检索"
    reranking: bool = True
    semantic_search: bool = True
    keyword_search: bool = True

    @model_validator(mode="after")
    def check_overlap(self) -> Self:
        """按字符校验重叠关系，并要求至少一种检索方式。"""
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("分段重叠必须小于分段大小")
        if not self.semantic_search and not self.keyword_search:
            raise ValueError("至少启用语义检索或关键词检索")
        return self


class CreateInput(StrictPayload):
    """创建知识库的基本资料及可选索引配置。"""

    name: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2000)
    description: str = Field(default="", max_length=5000)
    category: str = "技术文档"
    index_config: IndexInput | None = None


class DocumentInput(Payload):
    """模拟上传接口接收的文件名称和字节数。"""

    name: str = Field(min_length=1, max_length=180)
    size: int = Field(gt=0, le=100 * 1024 * 1024)

    @model_validator(mode="after")
    def check_extension(self) -> Self:
        """仅接受任务表要求的文档格式。"""
        if not self.name.lower().endswith(
            (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt", ".md", ".markdown")
        ):
            raise ValueError("支持 PDF、Word、Excel、TXT、Markdown")
        return self


class UploadInput(Payload):
    """知识库模拟上传的文件元信息列表。"""

    files: list[DocumentInput] = Field(min_length=1, max_length=20)
    config: IndexInput | None = None


class RetrievalInput(Payload):
    """检索问题及本次使用的索引和召回配置。"""

    query: str = Field(min_length=1, max_length=5000)
    config: IndexInput
