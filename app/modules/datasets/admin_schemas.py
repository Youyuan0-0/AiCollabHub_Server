"""数据集管理沿用公共协议；新增范围限制在数据集服务中处理，旧授权保持不变。"""

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

__all__ = [
    "DATASET_STATUSES",
    "DatasetDecision",
    "DatasetListing",
    "DatasetAdminQuery",
    "DatasetVisibility",
    "DatasetScores",
    "ReviewDataset",
    "DatasetReviewQuery",
    "DatasetAuditQuery",
    "PublishDataset",
]
