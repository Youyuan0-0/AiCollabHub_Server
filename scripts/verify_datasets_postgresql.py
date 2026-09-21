"""验证真实 PostgreSQL 数据集持久化，仅清理本次随机身份生成的验证记录。"""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from app.core.config import Settings
from app.db.session import create_db_engine
from app.main import create_app
from app.modules.common.identity import Actor
from app.modules.datasets.repository import DatasetRecord, DatasetRepository
from app.modules.datasets.workspace import DatasetWorkspace, activity_key


def require(condition: bool, label: str) -> None:
    """失败仅显示检查名称，不输出连接凭据或已有业务数据。"""
    if not condition:
        raise RuntimeError(label)


class ProbeIdentity:
    """只供内存中的测试应用使用随机身份，不修改运行服务的认证配置。"""

    def __init__(self, actor: Actor) -> None:
        self.actor = actor

    def resolve(self, request, audience):
        """为测试请求返回隔离身份，权限仍由原业务方法校验。"""
        return self.actor


def verify() -> dict:
    """使用两次完整应用生命周期验证落库与回填，并保留共享库全局版本。"""
    deployed = Settings()
    require(deployed.database_url is not None, "database_not_configured")
    settings = deployed.model_copy(
        update={
            "app_env": "test",
            "auth_mode": "disabled",
            "datasets_storage": "database",
            "datasets_publication_mode": "local",
            "applications_storage": "memory",
            "applications_runtime": "simulation",
            "applications_market": "disabled",
            "applications_identity_directory": None,
        }
    )
    actor = Actor("dataset-probe-" + uuid4().hex, "数据库验证", "admin", "probe-org")
    engine = create_db_engine(settings.database_url.get_secret_value())
    repository = DatasetRepository(engine)
    keys = []
    result = {}
    try:
        with engine.connect() as connection:
            require(engine.dialect.name == "postgresql", "postgresql_required")
            before = (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            )
        publication = None
        dataset_id = None
        for generation in range(2):
            application = create_app(settings)
            application.state.application_identity = ProbeIdentity(actor)
            with TestClient(application) as client:
                response = client.get("/api/v1/dataset-publication-defaults")
                require(response.status_code == 200, "defaults_failed")
                require(response.json()["storage"] == "database", "database_mode_required")
                if generation == 0:
                    # 真实 PostgreSQL 验证完整提交、审核和上架；只创建本次随机身份的资料。
                    response = client.post(
                        "/api/v1/dataset-publications",
                        json={
                            "submit": True,
                            "data": {
                                "name": actor.id,
                                "version": "v1",
                                "summary": "公开验证样例",
                                "descriptionMarkdown": "数据库发布验证",
                                "owner": "验证单位",
                                "contact": "验证联系人",
                                "dataApplication": ["智能试验"],
                                "dataShape": ["结构化数据"],
                                "aiPurpose": "评测数据集",
                                "fieldSchema": json.dumps(
                                    {"type": "object", "properties": {"value": {"type": "number"}}}
                                ),
                                "language": ["中文"],
                                "dataFormat": ["CSV"],
                                "dataLevel": "一般数据",
                                "recordCount": 1,
                                "sampleData": "value\n1",
                            },
                        },
                    )
                    require(response.status_code == 201, "publication_write_failed")
                    publication = response.json()
                    response = client.post(
                        "/api/v1/admin/dataset-publications/" + publication["id"] + "/review",
                        json={
                            "decision": "approved",
                            "revision": 1,
                            "scores": {
                                key: 4
                                for key in (
                                    "formatCompliance",
                                    "safetyCompliance",
                                    "annotationCompliance",
                                    "structuralCompleteness",
                                    "authenticity",
                                    "consistency",
                                    "cleanliness",
                                )
                            },
                        },
                    )
                    require(response.status_code == 200, "publication_review_failed")
                    publication = response.json()
                    require(
                        publication["syncStatus"] == "local_published", "publication_not_published"
                    )
                    dataset_id = publication["datasetId"]
                    keys.append(activity_key(dataset_id, actor))
                    path = "/api/v1/datasets/" + dataset_id
                    require(
                        client.put(
                            path + "/rating", json={"rating": 4, "comment": "持久化验证"}
                        ).status_code
                        == 200,
                        "rating_write_failed",
                    )
                    require(
                        client.put(path + "/favorite", json={"favorite": True}).status_code == 200,
                        "favorite_write_failed",
                    )
                    require(
                        client.post(path + "/sample-download").status_code == 200, "download_failed"
                    )
                    keys.append("publication:" + publication["id"])
                    # 使用不存在于目录的随机范围键验证存储，不改变任何已上架资源权限。
                    keys.append("visibility:" + actor.id)
                    repository.save(
                        keys[-1],
                        {"datasetId": actor.id, "scope": "members", "targets": [actor.id]},
                        None,
                    )
                else:
                    path = "/api/v1/datasets/" + dataset_id
                    rating = client.get(path + "/rating").json()
                    require(
                        rating["rating"] == 4 and rating["comment"] == "持久化验证",
                        "rating_not_persisted",
                    )
                    require(
                        dataset_id in client.get("/api/v1/dataset-activity").json()["favoriteIds"],
                        "favorite_not_persisted",
                    )
                    restored = client.get(
                        "/api/v1/dataset-publications/" + publication["id"]
                    ).json()
                    require(restored == publication, "publication_not_persisted")
                    require(
                        application.state.dataset_workspace.visibility(actor.id)["targets"]
                        == [actor.id],
                        "visibility_not_persisted",
                    )
                    detail = client.get("/api/v1/datasets/" + dataset_id)
                    require(detail.status_code == 200, "published_catalog_not_persisted")
                    require(
                        detail.json()["publishedAt"] == publication["publishedAt"],
                        "publication_time_changed",
                    )
                    result["published_catalog_persisted"] = True
                    result["api_recreated_application"] = True
        # 不同线程使用独立事务；并发下载不能覆盖同一记录上的评分与收藏。
        workspace = DatasetWorkspace(repository)

        def download_once(_: int) -> bool:
            """并发竞争超过业务重试次数可返回 409；只对成功操作核对下载计数。"""
            try:
                workspace.interact(dataset_id, actor)
            except HTTPException as error:
                require(error.status_code == 409, "unexpected_concurrency_status")
                return False
            return True

        with ThreadPoolExecutor(max_workers=4) as executor:
            outcomes = list(executor.map(download_once, range(4)))
        require(any(outcomes), "all_concurrent_writes_failed")
        activity = repository.get(keys[0])
        require(
            activity["downloadCount"] == 1 + sum(outcomes)
            and activity["rating"] == 4
            and activity["favorite"],
            "concurrent_activity_lost",
        )
        try:
            repository.save(keys[0], activity, 1)
        except HTTPException as error:
            require(error.status_code == 409, "unexpected_conflict_status")
        else:
            raise RuntimeError("stale_revision_accepted")
        result["concurrency_and_conflict"] = True
        result["concurrent_writes"] = {"saved": sum(outcomes), "conflicted": outcomes.count(False)}
        with engine.connect() as connection:
            after = (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            )
        require(before == after, "global_revision_changed")
        result["global_revision_preserved"] = True
        return result
    finally:
        # 仅删除本次随机身份记录；即使响应中断也能回收已提交但未取得编号的申请。
        with engine.begin() as connection:
            owned = list(
                connection.execute(
                    select(DatasetRecord.key).where(
                        DatasetRecord.data["createdBy"].as_string() == actor.id
                    )
                ).scalars()
            )
            connection.execute(delete(DatasetRecord).where(DatasetRecord.key.in_([*keys, *owned])))
        engine.dispose()


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False))
