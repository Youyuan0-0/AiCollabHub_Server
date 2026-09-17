"""数据集用户端业务：目录、详情、个人收藏、下载样例与发布申请。"""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException, Request

from app.modules.applications.identity import Actor, visible
from app.modules.common.fixtures import fixture
from app.modules.common.schemas import CatalogQuery
from app.modules.datasets.publishing import DatasetPublisher, LocalDatasetPublisher
from app.modules.datasets.repository import DatasetRepository
from app.modules.datasets.schemas import DatasetRankingMetric, SaveDataset, validate_submission
from app.modules.datasets.service import list_resources

# 活动修订冲突最多重试三次，避免竞争请求无限占用工作线程。
MAX_ACTIVITY_ATTEMPTS = 3
# 展示说明由实际配置生成，不能沿用固定样例中与默认全部可见不一致的文案。
VISIBILITY_LABELS = {
    "all": "全部用户",
    "organizations": "指定组织",
    "members": "指定成员",
    "projects": "指定项目组",
}


def activity_key(dataset_id: str, actor: Actor) -> str:
    """为可信用户与数据集生成稳定活动键，评分、收藏和下载共用同一条记录。"""
    digest = hashlib.sha256(json.dumps([actor.id, dataset_id]).encode()).hexdigest()
    return "activity:" + digest


def timestamp() -> str:
    """返回带时区的系统时间，供创建、修订和下载排序统一使用。"""
    return datetime.now(UTC).isoformat(timespec="microseconds")


class DatasetWorkspace:
    """组织数据集用户端流程；待审核资料不会加入公开目录。"""

    def __init__(
        self, repository: DatasetRepository, publisher: DatasetPublisher | None = None
    ) -> None:
        self.repository = repository
        self.publisher = publisher if publisher is not None else LocalDatasetPublisher()

    def visibility(self, dataset_id: str) -> dict:
        """读取独立开放范围记录；未配置时全部可见，修订号为 0。"""
        value = self.repository.get("visibility:" + dataset_id) or {
            "scope": "all",
            "targets": [],
            "revision": 0,
        }
        return {key: value[key] for key in ("scope", "targets", "revision")}

    def resources(self, actor: Actor | None = None) -> list[dict]:
        """生成目录并按可信身份过滤；actor=None 仅供内部管理读取完整目录。"""
        resources = fixture("datasets", "resources")
        # 审核与上架快照在同一记录事务提交；只读取已发布快照，不把草稿或待同步申请加入目录。
        resources.extend(
            deepcopy(record["publishedResource"])
            for record in self.repository.list("publication:")
            if record.get("status") == "approved" and record.get("publishedResource")
        )
        # 批量读取配置，数据库模式不为每张卡片单独查询；目标标识不返回给普通用户。
        scopes = {item["datasetId"]: item for item in self.repository.list("visibility:")}
        # 在统计、筛选选项和分页之前过滤，避免通过总数、排行或分类泄露不可见资源。
        resources = [
            item
            for item in resources
            if actor is None
            or visible(actor, item, scopes.get(item["id"], {"scope": "all", "targets": []}))
        ]
        activities = self.repository.list("activity:")
        for item in resources:
            scope = scopes.get(item["id"], {"scope": "all"})["scope"]
            # 未配置也明确显示默认规则，与过滤行为保持一致且不公开具体目标编号。
            item["metadata"] = [
                entry for entry in item.get("metadata", []) if entry["label"] != "开放范围"
            ] + [{"label": "开放范围", "value": VISIBILITY_LABELS[scope]}]
            facets = item.setdefault("facets", {})
            facets["keywords"] = item["tags"]
            # 组织维度与卡片单位保持一致，支持现有多选筛选契约。
            facets["organization"] = [item["owner"]]
            # 未标注任务类型的历史卡片不推测任务，只有真实字段参与动态筛选。
            if item.get("tasks"):
                facets["tasks"] = item["tasks"]
            metadata = {entry["label"]: entry["value"] for entry in item.get("metadata", [])}
            related_activities = [entry for entry in activities if entry["datasetId"] == item["id"]]
            # 仅聚合已保存的用户星级，固定样例评分不参与统计，也不改变质量审核分数。
            ratings = [
                entry["rating"]
                for entry in related_activities
                if type(entry.get("rating")) is int and 1 <= entry["rating"] <= 5
            ]
            item.update(
                englishName=item.get("englishName", item["name"]),
                descriptionMarkdown=item.get("descriptionMarkdown", item["description"]),
                dataSize=item.get("dataSize", metadata.get("数据规模", "")),
                updateFrequency=item.get("updateFrequency", metadata.get("更新频率", "")),
                downloadCount=sum(entry.get("downloadCount", 0) for entry in related_activities),
                favoriteCount=sum(bool(entry.get("favorite")) for entry in related_activities),
                rating=round(sum(ratings) / len(ratings), 2) if ratings else None,
                ratingCount=len(ratings),
                lastDownloadedAt=max(
                    (entry.get("lastDownloadedAt", "") for entry in related_activities), default=""
                )
                or None,
                # 评分来源未确认时不把用户评分冒充质量评分。
                qualityScore=item.get("qualityScore"),
            )
        return resources

    def catalog(self, query: CatalogQuery, actor: Actor | None = None) -> dict:
        """对当前身份可见目录进行组合筛选、稳定排序及元数据选项汇总。"""
        return list_resources(query, self.resources(actor))

    def rankings(
        self, metric: DatasetRankingMetric, limit: int, actor: Actor | None = None
    ) -> dict:
        """按指标返回前 limit 项；与目录共用统计，同分按编号排序且不公开用户明细。"""
        page = self.catalog(CatalogQuery(sort=metric, page=1, pageSize=limit), actor)
        return {
            "metric": metric,
            # 完整数据下载尚待第三方接入，明确当前统计口径，防止首页展示时混用。
            "downloadScope": "sample",
            "items": [
                {
                    "rank": rank,
                    "id": item["id"],
                    "name": item["name"],
                    "owner": item["owner"],
                    "downloadCount": item["downloadCount"],
                    "favoriteCount": item["favoriteCount"],
                }
                for rank, item in enumerate(page["items"], start=1)
            ],
        }

    def resource(self, dataset_id: str, actor: Actor | None = None) -> dict:
        """按编号和可信身份读取详情；不存在、未上架或无权访问均返回 404。"""
        item = next((item for item in self.resources(actor) if item["id"] == dataset_id), None)
        if item is None:
            raise HTTPException(404, "数据集不存在或尚未上架")
        return item

    def activity(self, actor: Actor) -> dict:
        """只返回本人仍有权访问的收藏编号；撤回范围后隐藏原收藏但保留活动记录。"""
        visible_ids = {item["id"] for item in self.resources(actor)}
        return {
            "favoriteIds": [
                entry["datasetId"]
                for entry in self.repository.list("activity:")
                if entry["userId"] == actor.id
                and entry.get("favorite")
                and entry["datasetId"] in visible_ids
            ]
        }

    def rating(self, dataset_id: str, actor: Actor) -> dict:
        """返回本人星级与公开汇总；不可见资源不允许读取评分，其他账号的明细不返回。"""
        resource = self.resource(dataset_id, actor)
        activity = self.repository.get(activity_key(dataset_id, actor)) or {}
        return {
            "rating": activity.get("rating"),
            "comment": activity.get("comment", ""),
            "ratingAverage": resource["rating"],
            "ratingCount": resource["ratingCount"],
        }

    def ratings(self, dataset_id: str, actor: Actor) -> list[dict]:
        """按更新时间返回可见数据集的评价，不公开评价者的账号或组织标识。"""
        self.resource(dataset_id, actor)
        reviews = [
            {
                "id": hashlib.sha256(
                    json.dumps([entry["userId"], dataset_id]).encode()
                ).hexdigest(),
                "rating": entry["rating"],
                "comment": entry.get("comment", ""),
                # 旧评分没有独立更新时间，保留空值，不用下载时间冒充评价时间。
                "updatedAt": entry.get("ratingUpdatedAt", ""),
            }
            for entry in self.repository.list("activity:")
            if entry["datasetId"] == dataset_id
            and type(entry.get("rating")) is int
            and 1 <= entry["rating"] <= 5
        ]
        return sorted(reviews, key=lambda item: (item["updatedAt"], item["id"]), reverse=True)

    def interact(
        self,
        dataset_id: str,
        actor: Actor,
        *,
        favorite: bool | None = None,
        rating: int | None = None,
        comment: str = "",
    ) -> dict:
        """有 rating 时保存星级，有 favorite 时设置收藏，两者均无时记录样例下载。"""
        if rating is not None and (type(rating) is not int or not 1 <= rating <= 5):
            raise HTTPException(422, "请选择 1–5 星评分")
        self.resource(dataset_id, actor)
        key = activity_key(dataset_id, actor)
        for attempt in range(MAX_ACTIVITY_ATTEMPTS):
            previous = self.repository.get(key)
            data = previous or {
                "userId": actor.id,
                "datasetId": dataset_id,
                "downloadCount": 0,
                "favorite": False,
            }
            if rating is not None:
                # 在每次 CAS 重试时合并最新记录，评分不会覆盖并发保存的收藏或下载次数。
                data = {
                    **data,
                    "rating": rating,
                    "comment": comment,
                    "ratingUpdatedAt": timestamp(),
                }
            elif favorite is None:
                data = {
                    **data,
                    "downloadCount": data["downloadCount"] + 1,
                    "lastDownloadedAt": timestamp(),
                }
            else:
                data = {**data, "favorite": favorite}
            try:
                self.repository.save(key, data, previous["revision"] if previous else None)
                break
            except HTTPException as error:
                if error.status_code != 409 or attempt == MAX_ACTIVITY_ATTEMPTS - 1:
                    raise
        return {
            "favorite": data["favorite"],
            "favoriteCount": self.resource(dataset_id)["favoriteCount"],
        }

    def download_sample(self, dataset_id: str, actor: Actor) -> bytes:
        """校验公开样例、记录下载并返回 UTF-8 JSON；缺少样例时返回 404 且不累计下载量。"""
        resource = self.resource(dataset_id, actor)
        if not resource.get("preview", {}).get("rows"):
            raise HTTPException(404, "该数据集暂无可下载的脱敏样例")
        content = json.dumps(
            {
                "name": resource["name"],
                "version": resource.get("version"),
                "sample": resource["preview"],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.interact(dataset_id, actor)
        return content

    def publications(self, actor: Actor) -> list[dict]:
        """按更新时间倒序返回本人发布申请，前端不能指定其他发布者。"""
        return sorted(
            [
                item
                for item in self.repository.list("publication:")
                if item["createdBy"] == actor.id
            ],
            key=lambda item: item["updatedAt"],
            reverse=True,
        )

    def publication(self, publication_id: str, actor: Actor) -> dict:
        """仅允许发布者读取申请；其他用户与记录不存在均返回 404。"""
        record = self.repository.get("publication:" + publication_id)
        if record is None or record["createdBy"] != actor.id:
            raise HTTPException(404, "发布申请不存在或无权查看")
        return record

    def save(self, payload: SaveDataset, actor: Actor, publication_id: str | None = None) -> dict:
        """保存本人草稿或提交审核，系统生成资源编号、时间和状态并校验修订冲突。"""
        if actor.role not in ("publisher", "admin", "org_admin"):
            raise HTTPException(403, "当前账号没有数据集发布权限")
        previous = self.publication(publication_id, actor) if publication_id else None
        if previous and previous["status"] not in ("draft", "rejected", "needs_changes"):
            raise HTTPException(409, "当前申请正在审核或已通过，不能继续编辑")
        if previous and payload.revision != previous["revision"]:
            raise HTTPException(409, "申请已更新，请重新加载后再保存")
        if not previous and payload.revision is not None:
            raise HTTPException(422, "新建申请不应携带修订号")
        if payload.submit:
            validate_submission(payload.data)
        now = timestamp()
        record = {
            **(previous or {}),
            "id": publication_id or f"dsp-{uuid4().hex}",
            "datasetId": previous["datasetId"] if previous else f"ds-{uuid4().hex}",
            "createdBy": actor.id,
            # 组织权限只来自可信会话，表单 owner 只是展示用归属单位名称。
            "organizationId": actor.organization_id,
            "data": payload.data.model_dump(by_alias=True),
            "status": "pending" if payload.submit else "draft",
            "createdAt": previous["createdAt"] if previous else now,
            "updatedAt": now,
            "submittedAt": now if payload.submit else None,
            "reviewComment": previous.get("reviewComment", "") if previous else "",
            # 退回重提保留此前审核快照；只有后台审核通过后才由发布适配器决定是否上架。
            "reviews": previous.get("reviews", []) if previous else [],
            "syncStatus": "not_ready",
        }
        return self.repository.save("publication:" + record["id"], record, payload.revision)


def dataset_workspace(request: Request) -> DatasetWorkspace:
    """从应用实例读取已装配的仓储，不在请求中临时创建状态或连接。"""
    return request.app.state.dataset_workspace
