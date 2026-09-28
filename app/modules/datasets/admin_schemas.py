"""数据集管理沿用公共协议；新增范围限制在数据集服务中处理，旧授权保持不变。"""

from pydantic import ConfigDict, Field

from app.modules.common.management_schemas import (
    RESOURCE_STATUSES as DATASET_STATUSES,
)
from app.modules.common.management_schemas import (
    AdminCatalogQuery as DatasetAdminQuery,
)
from app.modules.common.management_schemas import (
    AuditQuery as DatasetAuditQuery,
)
from app.modules.common.management_schemas import (
    ListingInput as DatasetListing,
)
from app.modules.common.management_schemas import (
    PublishInput as PublishDataset,
)
from app.modules.common.management_schemas import (
    ResourceDecision as DatasetDecision,
)
from app.modules.common.management_schemas import (
    ResourceScores as DatasetScores,
)
from app.modules.common.management_schemas import (
    ReviewInput as ReviewDataset,
)
from app.modules.common.management_schemas import (
    ReviewQuery as DatasetReviewQuery,
)
from app.modules.common.management_schemas import (
    VisibilityInput as DatasetVisibility,
)
from app.modules.common.schemas import Payload


class DatasetMetadata(Payload):
    """维护已发布数据集的交付元数据，revision 用于防止旧页面覆盖。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    data_lineage: str = Field(default="", max_length=5000)
    storage_location: str = Field(default="", max_length=1000)
    api_endpoint: str = Field(default="", max_length=2000)
    revision: int = Field(ge=1, strict=True)

__all__ = [
    "DATASET_STATUSES",
    "DatasetDecision",
    "DatasetListing",
    "DatasetAdminQuery",
    "DatasetVisibility",
    "DatasetMetadata",
    "DatasetScores",
    "ReviewDataset",
    "DatasetReviewQuery",
    "DatasetAuditQuery",
    "PublishDataset",
]
