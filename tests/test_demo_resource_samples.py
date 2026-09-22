"""原型资料转换及显式导入目标的检查，不连接数据库。"""

from collections import Counter
from uuid import UUID

import pytest

from app.core.config import Settings
from app.modules.datasets.publishing import sample_preview
from scripts.demo_resources import main_projection, sample_resources
from scripts.seed_demo_resources import validate_target


def test_all_samples_have_stable_ids_and_no_fake_activity():
    """保留五模块的完整样例目录，编号稳定且不导入虚构历史和统计。"""
    samples = sample_resources()
    assert Counter(item.kind for item in samples) == {
        "dataset": 8,
        "model": 16,
        "application": 12,
        "knowledge": 6,
        "tool": 12,
    }
    assert [item.identifier() for item in samples] == [
        item.identifier() for item in sample_resources()
    ]
    ids = [
        UUID(item.identifier(entity))
        for item in samples
        for entity in ("resource", "version", "publication", "review")
    ]
    assert len(ids) == len(set(ids))
    omitted = {
        "usage",
        "rating",
        "favoriteCount",
        "downloadCount",
        "metrics",
        "versions",
        "documents",
        "chunkCount",
    }
    for sample in samples:
        assert not omitted.intersection(sample.data)
        assert "Mock" in sample.data["versionNotes"]


def test_dataset_samples_and_application_dependencies_survive_mapping():
    """字段清单和实际三行预览保持一致，应用引用变为同批资源的 UUID。"""
    samples = sample_resources()
    keys = {item.identifier() for item in samples}
    for sample in samples:
        if sample.kind == "dataset":
            preview = sample_preview(sample.data["sampleData"])
            assert len(preview["rows"]) == 3
            assert preview["columns"] == ["record_id", "scenario", "content", "label"]
            assert main_projection(sample)["dataSizeBytes"] > 0
        if sample.kind == "application":
            assert all(
                dependency["resourceId"] in keys for dependency in sample.data["dependencies"]
            )
        if sample.kind == "tool":
            assert main_projection(sample)["sourceType"] == (
                "self_developed" if sample.data["sourceType"] == "自研工具" else "third_party"
            )


@pytest.mark.parametrize(
    "environment,name",
    [("production", "catalog_test"), ("test", "other_test"), ("test", "catalog")],
)
def test_rejects_unintended_database(environment, name):
    """生产环境及未确认的目标不能执行样例导入。"""
    settings = Settings(
        _env_file=None,
        app_env=environment,
        database_url="postgresql+psycopg://localhost/catalog_test",
    )
    with pytest.raises(ValueError):
        validate_target(settings, name)


def test_accepts_explicit_test_database():
    """测试目标匹配时仅完成配置校验，不依赖数据库可达。"""
    validate_target(
        Settings(
            _env_file=None,
            app_env="test",
            database_url="postgresql+psycopg://localhost/catalog_test",
        ),
        "catalog_test",
    )
