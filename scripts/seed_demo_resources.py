"""显式向开发测试库导入旧原型资源，默认只预览，不建表、不自动启动执行。"""

import argparse
import json
from collections import Counter
from copy import deepcopy

from sqlalchemy import insert, select
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db.foundation import RESOURCE_TABLES, FoundationDatabase, identifier, utc_now
from app.db.session import create_db_engine
from app.modules.common.resource_database import ResourceDatabase, ResourceDatabaseSession
from scripts.demo_resources import IMPORT_NOTE, DemoResource, main_projection, sample_resources


def validate_target(settings: Settings, database_name: str) -> None:
    """要求开发测试环境和显式数据库名匹配，错误信息不包含账号或连接串。"""
    if settings.app_env not in {"development", "test"} or not settings.database_url:
        raise ValueError("样例只允许导入已配置数据库的 development/test 环境")
    url = make_url(settings.database_url.get_secret_value())
    if url.get_backend_name() != "postgresql" or url.database != database_name:
        raise ValueError("目标数据库与 --database 不一致，未执行导入")
    if "test" not in database_name.lower() and "dev" not in database_name.lower():
        raise ValueError("目标数据库名必须明确包含 test 或 dev")


def import_resources(
    database: FoundationDatabase,
    samples: list[DemoResource],
    *,
    publisher: str,
    reviewer: str,
    apply: bool = False,
) -> list[dict]:
    """一次事务导入全部样例；固定编号保证重跑不覆盖已编辑或下架的资源。"""
    result = []
    with database.transaction(is_readonly=not apply) as session:
        if apply:
            database.lock(session, "demo-resource-import:v1")
        users = database.table("users")
        identities = {
            row["iam_subject"]: dict(row)
            for row in session.execute(
                select(users).where(users.c.iam_subject.in_([publisher, reviewer]))
            ).mappings()
        }
        if any(
            subject not in identities or identities[subject]["status"] != "active"
            for subject in (publisher, reviewer)
        ):
            raise ValueError("发布者和审核者必须是已登录入库的启用用户")
        if identities[reviewer]["role"] != "admin":
            raise ValueError("模拟审核者必须具有管理员角色")
        for sample in samples:
            unit = ResourceDatabaseSession(
                ResourceDatabase(database, sample.kind), session, is_readonly=not apply
            )
            main_name, prefix, _ = RESOURCE_TABLES[sample.kind]
            main, versions = database.table(main_name), database.table(prefix + "_versions")
            existing_id = session.scalar(
                select(main.c.id).where(main.c.id == identifier(sample.identifier()))
            )
            action = "import"
            if existing_id is not None:
                # 后续正式版本可以改为人工来源；用首次导入的不可变版本识别样例。
                snapshot = session.scalar(
                    select(versions.c.snapshot).where(
                        versions.c.id == identifier(sample.identifier("version")),
                        versions.c[prefix + "_id"] == existing_id,
                    )
                )
                source = (snapshot or {}).get("publicationContext", {}).get("source", {})
                if source.get("sampleId") != sample.sample_id or source.get("kind") != "sample":
                    raise ValueError(f"样例编号与现有资源冲突：{sample.kind}/{sample.sample_id}")
                action = "skip"
            if apply and action == "import":
                now = utc_now().isoformat()
                record = {
                    "id": sample.identifier("publication"),
                    unit.resource_key: sample.identifier(),
                    "versionId": sample.identifier("version"),
                    "createdBy": publisher,
                    "createdByName": identities[publisher]["display_name"],
                    "createdAt": now,
                    "updatedAt": now,
                    "submittedAt": now,
                    "status": "pending",
                    "revision": 1,
                    "reviews": [],
                    "data": deepcopy(sample.data),
                    "source": {
                        "kind": "sample",
                        "sampleId": sample.sample_id,
                        "label": IMPORT_NOTE,
                    },
                }
                unit.put_publication(record)
                record.update(status="approved", revision=record["revision"] + 1)
                record["reviews"].append(
                    {
                        "id": sample.identifier("review"),
                        "reviewerId": reviewer,
                        "decision": "approved",
                        "reason": "模拟审核通过。" + IMPORT_NOTE,
                        "reviewedAt": now,
                        "scores": None,
                        "reportFiles": [],
                    }
                )
                unit.put_resource(main_projection(sample))
                unit.put_publication(record)
                unit.save_resource_state({"id": sample.identifier(), "listingStatus": "listed"})
                unit.put_visibility(
                    sample.identifier(),
                    {
                        "scope": "all" if sample.is_public else "members",
                        "targets": [] if sample.is_public else [publisher],
                        "revision": 2,
                    },
                )
                session.execute(
                    insert(database.table("operation_logs")).values(
                        id=identifier(sample.identifier("log")),
                        actor_type="user",
                        user_id=identities[reviewer]["id"],
                        action="create",
                        target_type="knowledge_base" if sample.kind == "knowledge" else sample.kind,
                        target_id=sample.identifier(),
                        data={
                            "summary": "导入 Mock 示例资源：" + sample.data["name"],
                            "sampleId": sample.sample_id,
                            "mode": "simulation",
                        },
                    )
                )
            result.append(
                {
                    "kind": sample.kind,
                    "sampleId": sample.sample_id,
                    "id": sample.identifier(),
                    "name": sample.data["name"],
                    "action": action,
                }
            )
    return result


def main() -> None:
    """读取当前 .env 和已存在的模拟身份，预览或原子导入；不创建用户或提升权限。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="显式确认 .env 中的开发测试数据库名")
    parser.add_argument("--publisher", default="mock-user", help="已存在的发布者 IAM 标识")
    parser.add_argument("--reviewer", default="mock-admin", help="已存在的管理员 IAM 标识")
    parser.add_argument("--apply", action="store_true", help="写入样例；省略时只预览")
    args = parser.parse_args()
    settings = Settings()
    try:
        validate_target(settings, args.database)
    except ValueError as error:
        parser.error(str(error))
    samples = sample_resources()
    engine = create_db_engine(
        settings.database_url.get_secret_value(),
        pool_size=1,
        max_overflow=0,
        application_name="demo-resource-import",
    )
    try:
        database = FoundationDatabase(engine, settings)
        database.warm_up()
        result = import_resources(
            database, samples, publisher=args.publisher, reviewer=args.reviewer, apply=args.apply
        )
        print(
            json.dumps(
                {
                    "applied": args.apply,
                    "counts": dict(Counter(item["action"] for item in result)),
                    "resources": result,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
