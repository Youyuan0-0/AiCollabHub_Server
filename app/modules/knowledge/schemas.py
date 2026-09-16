from __future__ import annotations

from pydantic import Field

from app.modules.common.schemas import PageQuery, Payload


class ChunkQuery(PageQuery):
    """知识片段查询的分页、文档名称和关键词参数。"""

    document_name: str = Field(default="", alias="documentName")


class IndexInput(Payload):
    """知识库分段、向量模型与检索阈值配置。"""

    mode: str = "高质量模式"
    embedding_model: str = "bge-m3"
    chunk_mode: str = "智能分段"
    chunk_size: int = Field(default=500, ge=1, le=10000)
    chunk_overlap: int = Field(default=50, ge=0, le=10000)
    top_k: int = Field(default=5, ge=1, le=100)
    similarity_threshold: float = Field(default=0.5, ge=0, le=1)
    retrieval_mode: str = "混合检索"


class CreateInput(Payload):
    """创建知识库的基本资料及可选索引配置。"""

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    category: str = "技术文档"
    visibility: str = "集团公开"
    index_config: IndexInput | None = None


class DocumentInput(Payload):
    """模拟上传接口接收的文件名称和字节数。"""

    name: str = Field(min_length=1, max_length=180)
    size: int = Field(ge=0)


class UploadInput(Payload):
    """知识库模拟上传的文件元信息列表。"""

    files: list[DocumentInput] = Field(min_length=1, max_length=20)


class RetrievalInput(Payload):
    """检索问题及本次使用的索引和召回配置。"""

    query: str = Field(min_length=1, max_length=5000)
    config: IndexInput
