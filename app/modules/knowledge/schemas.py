"""知识库契约；数值单位为字符，管理字段与用户创建字段独立校验。"""

from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from app.modules.common.schemas import PageQuery, Payload
from app.modules.datasets.admin_schemas import DatasetVisibility


class ChunkQuery(PageQuery):
    """按文档名称、关键词分页查询分段。"""

    document_name: str = Field(default="", alias="documentName", max_length=200)


class IndexInput(Payload):
    """索引和召回参数；保留旧中文枚举以兼容已保存的配置。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mode: Literal["高质量模式", "经济模式"] = "经济模式"
    embedding_model: str = Field(default="bge-m3", min_length=1, max_length=120)
    chunk_mode: Literal[
        "自动分段", "按段落", "固定长度", "智能分段", "语义分段", "规则分段", "递归分段"
    ] = "自动分段"
    chunk_size: int = Field(default=500, ge=100, le=2000, strict=True)
    chunk_overlap: int = Field(default=50, ge=0, le=500, strict=True)
    top_k: int = Field(default=5, ge=1, le=20, strict=True)
    similarity_threshold: float = Field(default=0.2, ge=0, le=1, allow_inf_nan=False)
    retrieval_mode: Literal["向量检索", "全文检索", "混合检索", "重排序检索"] = "全文检索"

    @model_validator(mode="after")
    def validate_overlap(self) -> Self:
        """重叠小于分段长度才能推进切分；经济模式不建立向量索引。"""
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("分段重叠必须小于分段大小")
        if self.mode == "经济模式" and self.retrieval_mode != "全文检索":
            raise ValueError("经济模式使用全文检索，向量或重排序请使用高质量模式")
        return self


class CreateInput(Payload):
    """用户可创建的资料；归属和联系人由可信身份生成，默认仅创建者可见。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)
    description_markdown: str = Field(default="", max_length=20000)
    category: str = Field(default="技术文档", min_length=1, max_length=80)
    index_config: IndexInput = Field(default_factory=IndexInput)


class AdminInput(CreateInput):
    """后台完整资料；单位和联系人只用于展示，不作为授权依据。"""

    data_level: Literal["核心数据", "重要数据", "一般数据"] = "一般数据"
    # 元数据对照表要求后台明确维护责任单位和联系人，不能保存空白责任信息。
    owner: str = Field(min_length=1, max_length=200)
    contact: str = Field(min_length=1, max_length=200)


class AdminUpdate(AdminInput):
    """按当前修订更新后台资料，拒绝旧页面覆盖新记录。"""

    revision: int = Field(ge=1, strict=True)


class SettingsInput(IndexInput):
    """配置更新须携带知识库修订；索引相关配置改变后需要重建。"""

    revision: int = Field(ge=1, strict=True)


class RevisionInput(Payload):
    """重建等写操作使用的乐观锁版本。"""

    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, strict=True)


class VisibilityInput(DatasetVisibility):
    """复用平台范围枚举与目标校验，revision 对应知识库当前修订。"""

    revision: int = Field(ge=1, strict=True)


class RebuildInput(RevisionInput):
    """默认完整重建；failedOnly 为真时只处理未就绪文档，保留已成功索引。"""

    failed_only: bool = Field(default=False, alias="failedOnly", strict=True)


class RetrievalInput(Payload):
    """问题与临时召回参数；不能用临时模型替换已经建立索引的模型。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=5000)
    config: IndexInput


class ReviewInput(RevisionInput):
    """审核当前修订；驳回和补充材料必须给出可执行的说明。"""

    decision: Literal["approved", "rejected", "needs_changes"]
    reason: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def require_reason(self) -> Self:
        """剔除纯空白意见，避免退回后申请人无法获知原因。"""
        self.reason = self.reason.strip()
        if self.decision != "approved" and not self.reason:
            raise ValueError("驳回或要求补充材料时，请填写审核意见")
        return self


class ResubmitInput(CreateInput):
    """退回后修改创建资料并重新送审，不能直接修改审批结果。"""

    revision: int = Field(ge=1, strict=True)


class ReviewQuery(PageQuery):
    """审核队列按类型和状态过滤，先隔离管理范围再统计分页。"""

    kind: Literal["base", "document"] = "base"
    status: Literal["", "pending", "approved", "rejected", "needs_changes"] = "pending"
