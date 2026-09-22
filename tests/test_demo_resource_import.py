"""样例导入在隔离 PostgreSQL schema 中验证，不修改 public 数据。"""

from collections import Counter
from copy import deepcopy
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from app.db.foundation import RESOURCE_TABLES, identifier
from app.modules.common.resource_database import ResourceDatabase
from scripts.demo_resources import DemoResource, sample_resources
from scripts.seed_demo_resources import import_resources
from tests.test_foundation_resource_storage import draft, foundation
from tests.test_resource_foundation import (
    connection,
    flush_constraints,
    pytestmark,
    schema_connection,
)

__all__ = ["connection", "schema_connection", "foundation", "pytestmark"]


def test_imports_all_samples_with_current_version_and_review(foundation, connection):
    """验证 54 条样例的完整关联约束和不同模块的主表映射。"""
    samples = sample_resources()
    result = import_resources(
        foundation, samples, publisher="writer", reviewer="reviewer", apply=True
    )
    flush_constraints(connection)
    assert Counter(item["action"] for item in result) == {"import": 54}
    for kind, expected in Counter(item.kind for item in samples).items():
        main_name, prefix, _ = RESOURCE_TABLES[kind]
        main = foundation.table(main_name)
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(main).where(main.c.status == "enabled")
            )
            == expected
        )
        for suffix in ("_publications", "_versions"):
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(foundation.table(prefix + suffix))
                )
                == expected
            )
    for name in ("resource_reviews", "resource_visibility_policies", "operation_logs"):
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(foundation.table(name))) == 54
        )
    for name in (
        "resource_ratings",
        "resource_downloads",
        "resource_favorites",
        "files",
        "knowledge_documents",
    ):
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(foundation.table(name))) == 0
        )
    dependencies = foundation.table("application_dependencies")
    assert (
        connection.scalar(
            sa.select(sa.func.count())
            .select_from(dependencies)
            .where(dependencies.c.resource_version_id.is_not(None))
        )
        == 2
    )


def test_repeated_import_preserves_disabled_status_and_favorite(foundation, connection):
    """幂等重跑不能重新启用资源、重置互动或追加审核和日志。"""
    sample = sample_resources()[0]
    import_resources(foundation, [sample], publisher="writer", reviewer="reviewer", apply=True)
    repository = ResourceDatabase(foundation, "dataset")
    with repository.transaction() as unit:
        unit.save_resource_state({"id": sample.identifier(), "listingStatus": "unlisted"})
        unit.put_entry(
            "interactions",
            "writer:" + sample.identifier(),
            {"actorId": "writer", "datasetId": sample.identifier(), "favorite": True},
        )
    result = import_resources(
        foundation, [sample], publisher="writer", reviewer="reviewer", apply=True
    )
    flush_constraints(connection)
    assert result[0]["action"] == "skip"
    with repository.transaction(is_readonly=True) as unit:
        resource = unit.get_resource(sample.identifier())
        assert resource["listingStatus"] == "unlisted" and resource["favoriteCount"] == 1
        assert len(resource["versions"]) == 1
    assert (
        connection.scalar(
            sa.select(sa.func.count()).select_from(foundation.table("operation_logs"))
        )
        == 1
    )


def test_failed_import_rolls_back_earlier_resources(foundation, connection):
    """缺失应用依赖时整批回滚，不能留下前面已处理的主表、版本或日志。"""
    samples = sample_resources()
    app = next(item for item in samples if item.sample_id == "app-qa")
    with pytest.raises(HTTPException):
        import_resources(
            foundation, [samples[0], app], publisher="writer", reviewer="reviewer", apply=True
        )
    for name in (
        "datasets",
        "dataset_versions",
        "dataset_publications",
        "resource_reviews",
        "operation_logs",
    ):
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(foundation.table(name))) == 0
        )


def test_repeated_import_preserves_later_manual_version(foundation, connection):
    """样例之后正常发布人工新版本，重跑仍能识别来源并保留新资料。"""
    sample = sample_resources()[0]
    import_resources(foundation, [sample], publisher="writer", reviewer="reviewer", apply=True)
    repository = ResourceDatabase(foundation, "dataset")
    record = draft("dataset")
    record["datasetId"] = sample.identifier()
    record["data"] = {**sample.data, "name": "更新后的数据集", "version": "v-next"}
    with repository.transaction() as unit:
        unit.put_publication(record)
        record.update(status="approved", revision=record["revision"] + 1)
        record["reviews"].append({"id": str(uuid4()), "reviewerId": "reviewer", "reason": "通过"})
        unit.put_publication(record)
    result = import_resources(
        foundation, [sample], publisher="writer", reviewer="reviewer", apply=True
    )
    flush_constraints(connection)
    assert result[0]["action"] == "skip"
    with repository.transaction(is_readonly=True) as unit:
        resource = unit.get_resource(sample.identifier())
        assert resource["name"] == "更新后的数据集" and resource["version"] == "v-next"
        assert len(resource["versions"]) == 2


def test_preview_does_not_write_and_existing_source_collision_is_rejected(foundation, connection):
    """预览保持空库；固定编号已有不相关资料时停止，不能把它当作已导入样例。"""
    sample = sample_resources()[0]
    result = import_resources(foundation, [sample], publisher="writer", reviewer="reviewer")
    assert result[0]["action"] == "import"
    main = foundation.table("datasets")
    assert connection.scalar(sa.select(sa.func.count()).select_from(main)) == 0
    values = deepcopy(sample.data)
    values.update(name="已有业务资料")
    with foundation.transaction() as session:
        session.execute(
            sa.insert(main).values(
                id=identifier(sample.identifier()),
                name=values["name"],
                summary="已有资料",
                description_markdown="已有资料",
                provider_name="测试单位",
                ai_purpose="training",
            )
        )
    with pytest.raises(ValueError, match="冲突"):
        import_resources(foundation, [sample], publisher="writer", reviewer="reviewer", apply=True)


def test_rejects_non_admin_reviewer(foundation, connection):
    """导入脚本也检查现有身份，不隐式创建账号或提高权限。"""
    with pytest.raises(ValueError, match="管理员"):
        import_resources(
            foundation,
            [DemoResource("dataset", "demo", {})],
            publisher="writer",
            reviewer="writer",
            apply=True,
        )
