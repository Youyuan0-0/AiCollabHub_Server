"""使用指定 .env 验证真实 PostgreSQL 会话；只创建并清理本次随机会话，不测试真实登录。"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, delete, inspect, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.conversations import Conversation, StoredConversationMessage
from app.db.session import create_db_engine
from app.main import create_app
from app.services.conversation_store import SqlConversationStore


def require(condition, label):
    """断言只使用固定检查名称，失败日志不携带连接密码、业务内容或 SQL 参数。"""
    if not condition:
        raise RuntimeError(label)


def verify(settings, *, migrate, output):
    """迁移可选；API 测试使用隔离应用连接真实库，保留部署认证配置并清理自己的数据。"""
    report = {
        "checkedAt": datetime.now(UTC).isoformat(),
        "authentication": "isolated test application; deployment authentication unchanged",
        "checks": {},
    }
    checks = report["checks"]
    require(settings.database_url is not None, "database_not_configured")
    primary = create_db_engine(settings.database_url.get_secret_value(), target_role="primary")
    replica = None
    identifiers = []
    try:
        with primary.connect() as connection:
            require(connection.dialect.name == "postgresql", "postgresql_required")
            global_version = (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            )
            checks["primary"] = not connection.scalar(text("SELECT pg_is_in_recovery()"))
            report["globalVersionBefore"] = global_version
            tables = inspect(connection).get_table_names()
            if migrate and "demo_conversations" in tables:
                legacy = Table("demo_conversations", MetaData(), autoload_with=connection)
                rows = [dict(row) for row in connection.execute(select(legacy)).mappings()]
                # 迁移前保留本模块旧数据副本，不备份凭据；不向终端打印会话正文。
                backup = output.with_suffix(".legacy-backup.json")
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_text(
                    json.dumps(rows, default=str, ensure_ascii=False), encoding="utf-8"
                )
                report["legacyBackup"] = str(backup)
                report["legacyRowsBefore"] = len(rows)
        if migrate:
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.conversations.ini"))
            config.attributes["settings"] = settings
            command.upgrade(config, "head")
        with primary.connect() as connection:
            checks["independent_migration"] = (
                connection.scalar(text("SELECT version_num FROM alembic_version_conversations"))
                == "20260915_chat01"
            )
            require(checks["independent_migration"], "conversation_migration_required")
            checks["global_version_preserved"] = (
                global_version
                == connection.execute(text("SELECT version_num FROM alembic_version"))
                .scalars()
                .all()
            )
            types = dict(
                connection.execute(
                    text(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema=current_schema() AND table_name='conversation_messages'"
                    )
                ).all()
            )
            checks["native_postgresql_types"] = (
                types.get("attachments") == "jsonb"
                and types.get("created_at") == "timestamp with time zone"
            )
        if settings.database_read_url:
            replica = create_db_engine(
                settings.database_read_url.get_secret_value(), target_role="standby", readonly=True
            )
            with replica.connect() as connection:
                checks["standby"] = connection.scalar(text("SELECT pg_is_in_recovery()"))
        # 不占用用户服务端口；附件使用临时目录，测试退出时自动移除自己的原文件。
        with TemporaryDirectory(prefix="aicollab-pg-probe-") as directory:
            probe_settings = settings.model_copy(
                update={
                    "auth_mode": "disabled",
                    "conversation_attachment_dir": Path(directory),
                    "redis_url": None,
                    "minio_endpoint": None,
                    "minio_access_key": None,
                    "minio_secret_key": None,
                }
            )
            root = "/api/v1/conversations"
            with TestClient(create_app(probe_settings)) as client:
                created = client.post(f"{root}/new", json={"title": "PostgreSQL integration probe"})
                require(created.status_code == 201, "create_failed")
                identifier = created.json()["id"]
                identifiers.append(identifier)
                checks["create_empty"] = created.json()["messages"] == []
                uploaded = client.post(
                    f"{root}/{identifier}/attachments",
                    files={"file": ("probe.txt", b"postgresql-attachment-probe")},
                )
                require(uploaded.status_code == 201, "attachment_upload_failed")
                attachment = uploaded.json()
                questions = ["  PostgreSQL 第一轮\n保留空格与换行  ", "第二轮：历史持久化"]
                for index, question in enumerate(questions):
                    response = client.post(
                        f"{root}/{identifier}/messages",
                        json={
                            "question": question,
                            "model": "postgresql-probe",
                            "knowledgeBase": "",
                            "tools": [],
                            "attachmentIds": [attachment["id"]] if index == 0 else [],
                        },
                    )
                    require(response.status_code == 200, "send_failed")
                    require(response.json()["text"] == question, "echo_changed")
                checks["multiturn_echo"] = True
                record = client.get(f"{root}/{identifier}/messages").json()
                require(
                    record["version"] == 2 and len(record["messages"]) == 4, "history_incomplete"
                )
                require(record["messages"][0]["attachments"] == [attachment], "attachment_lost")
                checks["history"] = any(
                    item["id"] == identifier for item in client.get(root).json()
                )
            with TestClient(create_app(probe_settings)) as restarted:
                restored = restarted.get(f"{root}/{identifier}/messages")
                checks["restart_persistence"] = (
                    restored.status_code == 200 and restored.json() == record
                )
                download = restarted.get(
                    f"{root}/{identifier}/attachments/{attachment['id']}/content"
                )
                checks["attachment_download"] = (
                    download.status_code == 200
                    and download.content == b"postgresql-attachment-probe"
                )
                if replica is not None:
                    with replica.connect() as connection:
                        positions = (
                            connection.execute(
                                select(StoredConversationMessage.position)
                                .where(StoredConversationMessage.conversation_id == identifier)
                                .order_by(StoredConversationMessage.position)
                            )
                            .scalars()
                            .all()
                        )
                        checks["standby_sees_committed_messages"] = positions == [0, 1, 2, 3]
                barrier = Barrier(2)

                def race(index):
                    """两个独立连接读取同版本后同时提交，确认仅一方成功且无消息覆盖。"""
                    with Session(primary) as session:
                        store = SqlConversationStore(session)
                        current = store.get(identifier)
                        expected = current.version
                        current.version += 1
                        current.title = f"probe-{index}"
                        barrier.wait(timeout=10)
                        try:
                            store.save(current, expected_version=expected)
                            return 200
                        except HTTPException as error:
                            return error.status_code

                with ThreadPoolExecutor(max_workers=2) as executor:
                    outcomes = sorted(executor.map(race, (1, 2)))
                checks["concurrent_version_conflict"] = outcomes == [200, 409]
                after_race = restarted.get(f"{root}/{identifier}/messages").json()
                checks["messages_preserved_after_conflict"] = (
                    after_race["messages"] == record["messages"]
                )
                removed = restarted.delete(f"{root}/{identifier}")
                checks["delete"] = (
                    removed.status_code == 204
                    and restarted.get(f"{root}/{identifier}/messages").status_code == 404
                )
                with primary.connect() as connection:
                    checks["messages_deleted"] = (
                        connection.scalar(
                            select(StoredConversationMessage.id).where(
                                StoredConversationMessage.conversation_id == identifier
                            )
                        )
                        is None
                    )
        require(all(checks.values()), "one_or_more_checks_failed")
    finally:
        try:
            if identifiers:
                # API 删除保留墓碑；验证结束后只清理本次随机 ID，恢复测试前的会话数量。
                with primary.begin() as connection:
                    connection.execute(text("SET LOCAL statement_timeout = '15s'"))
                    connection.execute(
                        delete(StoredConversationMessage).where(
                            StoredConversationMessage.conversation_id.in_(identifiers)
                        )
                    )
                    connection.execute(delete(Conversation).where(Conversation.id.in_(identifiers)))
                checks["probe_data_cleaned"] = True
        finally:
            primary.dispose()
            if replica is not None:
                replica.dispose()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    """命令行显式指定配置及报告路径；默认不迁移，只验证已部署的会话结构。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path(".runtime/conversations-postgresql.json")
    )
    args = parser.parse_args()
    try:
        report = verify(Settings(_env_file=args.env), migrate=args.migrate, output=args.output)
    except Exception as error:
        # 不打印驱动异常正文或 Pydantic 原始输入，它们可能包含凭据。
        print(
            json.dumps({"ok": False, "errorType": type(error).__name__, "report": str(args.output)})
        )
        raise SystemExit(1) from None
    print(json.dumps({"ok": True, "checks": report["checks"], "report": str(args.output)}))


if __name__ == "__main__":
    main()
