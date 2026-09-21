"""资源收藏及用户评分的公共输入，统计和权限由业务服务控制。"""

from pydantic import ConfigDict, Field

from app.modules.common.schemas import Payload


class FavoriteInput(Payload):
    """幂等设置当前用户的收藏状态，不接受客户端自报统计次数。"""

    model_config = ConfigDict(extra="forbid")
    favorite: bool


class RatingInput(Payload):
    """保存当前登录用户的星级；禁止自报用户、平均分或评价人数。"""

    model_config = ConfigDict(extra="forbid")
    rating: int = Field(ge=1, le=5, strict=True)
    # 与场景应用一致，文字评价可选，最多 1000 字，与星级一起保存。
    comment: str = Field(default="", max_length=1000)
