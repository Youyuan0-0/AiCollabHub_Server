"""只在明确指定的本机隔离 PostgreSQL 中运行，不读取项目远端数据库配置。"""

import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
API = "/api/v1"
SCORES = {"completeness": 4, "usability": 4, "security": 4}


@pytest.fixture
def database_app(tmp_path):
    url = os.environ.get("SCENARIO_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires an isolated local PostgreSQL test server")
    parsed = make_url(url)
    assert parsed.host in ("127.0.0.1", "localhost") and parsed.port == 55491

    def disable_prepared_statements(connection, _record):
        # PGlite 多路复用共用一个后端；只为此测试实例关闭 psycopg 命名预编译缓存。
        if getattr(getattr(connection, "info", None), "port", None) == 55491:
            connection.prepare_threshold = None

    event.listen(Engine, "connect", disable_prepared_statements)
    engine = create_engine(url)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        # 只有隔离 PGlite 测试实例可以执行重置，禁止误操作本机真实数据库。
        assert "emcc" in connection.scalar(text("SELECT version()"))
        connection.exec_driver_sql("DEALLOCATE ALL")
        connection.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        connection.exec_driver_sql(
            (ROOT / "database/scenario-applications/schema.sql").read_text(encoding="utf-8"),
            execution_options={"no_parameters": True},
        )
    settings = Settings(
        _env_file=None,
        app_env="test",
        auth_mode="disabled",
        database_url=url,
        applications_storage="database",
        applications_demo_directory=str(tmp_path / "provider"),
    )

    def client():
        application = create_app(settings)
        application.exception_handlers.pop(SQLAlchemyError, None)
        return TestClient(application)

    try:
        yield client, engine
    finally:
        engine.dispose()
        event.remove(Engine, "connect", disable_prepared_statements)


def submit(client, *, name="数据库应用", submit=True, **extra):
    response = client.post(
        API + "/application-publications",
        json={
            "submit": submit,
            "data": {
                "name": name,
                "owner": "研发单位",
                "summary": "验证持久化",
                "triggerType": "手动触发",
                "communicationProtocol": "HTTP",
                "deployment": "工具链托管",
                "contact": "测试联系人",
                **extra,
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def review(client, record, decision="approved"):
    response = client.post(
        f"{API}/admin/application-publications/{record['id']}/review",
        json={
            "revision": record["revision"],
            "decision": decision,
            "scores": SCORES if decision == "approved" else None,
            "reason": "请补充使用说明" if decision != "approved" else "",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_publication_metadata_survives_restart_review_and_new_version(database_app):
    factory, _ = database_app
    metadata = {
        "developmentTeam": "应用研发组",
        "sharingAgreement": "集团内共享",
        "toolList": ["检索资料", "生成报告"],
    }
    with factory() as client:
        record = submit(client, **metadata)
    with factory() as client:
        saved = client.get(f"{API}/application-publications/{record['id']}").json()
        assert all(saved["data"][key] == value for key, value in metadata.items())
        approved = review(client, saved)
        assert all(approved["reviews"][-1]["data"][key] == value for key, value in metadata.items())
    with factory() as client:
        path = f"{API}/applications/{record['applicationId']}"
        resource = client.get(path).json()
        assert all(resource[key] == value for key, value in metadata.items())
        draft = client.post(path + "/version-publications").json()
        assert all(draft["data"][key] == value for key, value in metadata.items())


def test_restart_keeps_publication_interactions_runs_downloads(database_app):
    factory, engine = database_app
    request_id, download_id = str(uuid4()), str(uuid4())
    with factory() as first:
        record = review(first, submit(first))
        path = f"{API}/applications/{record['applicationId']}"
        response = first.put(path + "/interaction", json={"favorite": True, "rating": 5})
        assert response.status_code == 200, response.text
        run = first.post(path + "/runs", json={"requestId": request_id, "input": {"q": "测试"}})
        assert run.status_code == 200, run.text
        download = first.post(path + "/downloads", json={"requestId": download_id})
        assert download.status_code == 200, download.text
    with factory() as second:
        saved = second.get(path)
        assert saved.status_code == 200, saved.text
        assert saved.headers["X-Application-Storage"] == "postgresql"
        assert saved.json()["favoriteCount"] == 1
        assert saved.json()["rating"] == 5
        assert saved.json()["usage"] == 1
        assert saved.json()["downloadCount"] == 1
        assert (
            second.get(f"{API}/application-publications/{record['id']}").json()["status"]
            == "approved"
        )
        assert (
            second.post(
                path + "/runs",
                json={
                    "requestId": request_id,
                    "input": {"q": "测试"},
                },
            ).json()
            == run.json()
        )
        assert (
            second.post(path + "/downloads", json={"requestId": download_id}).json()
            == download.json()
        )
        assert (
            second.post(
                path + "/runs",
                json={
                    "requestId": request_id,
                    "input": {"q": "内容变化"},
                },
            ).status_code
            == 409
        )
    with engine.connect() as connection:
        values = connection.scalar(text("SELECT local_metadata FROM scene_application LIMIT 1"))
        assert values["contact"] == "测试联系人"
        assert not {"name", "summary", "version", "inputSchema", "category"} & values.keys()
        assert connection.scalar(text("SELECT count(*) FROM scene_review_note")) == 1
        assert (
            connection.scalar(text("SELECT count(*) FROM scene_activity WHERE operation='run'"))
            == 1
        )


def test_resubmission_preserves_old_round_and_no_external_draft_in_sql(database_app):
    factory, engine = database_app
    with factory() as client:
        record = review(client, submit(client), "needs_changes")
        response = client.put(
            f"{API}/application-publications/{record['id']}",
            json={
                "revision": record["revision"],
                "submit": True,
                "data": {**record["data"], "name": "完善后的应用", "contact": "新联系人"},
            },
        )
        assert response.status_code == 200, response.text
        review(client, response.json())
    with factory() as restarted:
        record = restarted.get(f"{API}/application-publications/{record['id']}").json()
        assert record["status"] == "approved" and len(record["reviews"]) == 2
        assert record["reviews"][0]["data"]["name"] == "数据库应用"
        assert record["reviews"][1]["data"]["name"] == "完善后的应用"
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT round_no,local_fields FROM scene_submission_extension ORDER BY round_no")
        ).all()
        assert [row[0] for row in rows] == [1, 2]
        assert [row[1]["contact"] for row in rows] == ["测试联系人", "新联系人"]
        assert all("name" not in row[1] for row in rows)


def test_version_and_notifications_survive_restart(database_app):
    factory, engine = database_app
    with factory() as client:
        record = review(client, submit(client))
        path = f"{API}/applications/{record['applicationId']}"
        client.put(path + "/interaction", json={"subscribed": True})
        draft = client.post(path + "/version-publications")
        assert draft.status_code == 200, draft.text
        draft = draft.json()
        response = client.put(
            f"{API}/application-publications/{draft['id']}",
            json={
                "revision": draft["revision"],
                "submit": True,
                "data": {**draft["data"], "version": "v2.0"},
            },
        )
        assert response.status_code == 200, response.text
        review(client, response.json())
        notices = client.get(API + "/application-activity").json()["notifications"]
        notice = next(item for item in notices if item["kind"] == "new_version")
        assert client.put(f"{API}/application-notifications/{notice['id']}/read").status_code == 200
    with factory() as second:
        resource = second.get(path).json()
        assert resource["version"] == "v2.0" and len(resource["versions"]) == 2
        notices = second.get(API + "/application-activity").json()["notifications"]
        assert next(item for item in notices if item["id"] == notice["id"])["read"] is True
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM scene_application")) == 1


def test_permissions_and_configuration_shared_between_instances(database_app):
    factory, engine = database_app
    with factory() as client:
        record = review(client, submit(client))
        path = f"{API}/applications/{record['applicationId']}"
        response = client.put(
            f"{API}/admin/applications/{record['applicationId']}/visibility",
            json={
                "scope": "members",
                "targets": ["demo-publisher"],
                "revision": 1,
            },
        )
        assert response.status_code == 200, response.text
        policy = client.get(API + "/admin/application-policy").json()
        response = client.put(
            API + "/admin/application-policy",
            json={
                **policy,
                "announcement": "已持久化公告",
                "hotCategories": ["会议办公"],
            },
        )
        assert response.status_code == 200, response.text
    with factory() as other:
        other.post(API + "/application-demo-session", json={"actorId": "demo-reader"})
        assert other.get(path).status_code == 404
        listing = other.get(API + "/applications").json()
        assert record["applicationId"] not in {item["id"] for item in listing["items"]}
        assert other.get(path + "/discovery").status_code == 404
        assert other.put(path + "/interaction", json={"favorite": True}).status_code == 404
        policy = other.get(API + "/admin/application-policy").json()
        assert policy["announcement"] == "已持久化公告"
        assert policy["hotCategories"] == ["会议办公"]
    with engine.connect() as connection:
        value = connection.scalar(
            text("SELECT value FROM scene_configuration WHERE key='workflow'")
        )
        assert "hotCategories" not in value
        assert value["hotCategoryRefs"] == [
            {"marketProfile": "demo-file", "categoryId": "demo-category-1"}
        ]


def test_concurrent_approval_commits_only_once(database_app):
    factory, engine = database_app
    with factory() as client:
        record = submit(client)

        def approve(_):
            return client.post(
                f"{API}/admin/application-publications/{record['id']}/review",
                json={
                    "revision": record["revision"],
                    "decision": "approved",
                    "scores": SCORES,
                },
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(approve, range(2))) == [200, 409]
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM scene_review_note")) == 1


def test_review_failure_keeps_draft_unpublished(database_app, monkeypatch):
    from app.modules.applications.database_session import DatabaseApplicationSession

    factory, _ = database_app
    with factory() as client:
        record = submit(client)

        def fail(*args):
            raise RuntimeError("Injected failure after preparing simulated external snapshot")

        monkeypatch.setattr(DatabaseApplicationSession, "save_review", fail)
        with pytest.raises(RuntimeError):
            review(client, record)
        assert client.get(f"{API}/applications/{record['applicationId']}").status_code == 404
        assert (
            client.get(f"{API}/application-publications/{record['id']}").json()["status"]
            == "pending"
        )


def test_toolchain_development_and_sync_survive_restart(database_app):
    factory, engine = database_app
    with factory() as client:
        created = client.post(
            API + "/application-toolchain/development-publications",
            json={
                "data": {
                    "name": "模拟开发产物",
                    "owner": "研发单位",
                    "summary": "仅外部模拟器保存",
                    "triggerType": "手动触发",
                    "communicationProtocol": "HTTP",
                    "deployment": "工具链托管",
                },
            },
        )
        assert created.status_code == 200, created.text
        resource = created.json()
    with factory() as restarted:
        body = {
            "externalId": resource["externalId"],
            "externalVersion": resource["externalVersion"],
            "requestId": str(uuid4()),
        }
        result = restarted.post(API + "/application-toolchain/syncs", json=body)
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "succeeded", result.text
    with factory() as another:
        assert another.post(API + "/application-toolchain/syncs", json=body).json() == result.json()
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM scene_application")) == 1


def test_listing_read_does_not_seed_database(database_app):
    factory, engine = database_app
    with factory() as client:
        assert client.get(API + "/applications").status_code == 200
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM scene_application")) == 0


def test_interrupted_request_is_not_invoked_again(database_app, monkeypatch):
    factory, engine = database_app
    request_id = str(uuid4())
    with factory() as client:
        record = review(client, submit(client))
        service = client.app.state.application_service
        application_id = record["applicationId"]
        from app.modules.applications.identity import DEFAULT_USER
        from app.modules.applications.workflow_schemas import RunInput

        calls = []

        def interrupted(*args):
            calls.append(True)
            raise KeyboardInterrupt("Simulate termination after the request was persisted")

        monkeypatch.setattr(service.provider, "invoke", interrupted)
        payload = RunInput(request_id=request_id, input={"q": "测试"})
        with pytest.raises(KeyboardInterrupt):
            service.workflows.run(application_id, DEFAULT_USER, payload)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE scene_activity SET deadline_at=now()-interval '1 second' "
                    "WHERE operation='run'"
                )
            )
        activity = client.get(API + "/application-activity")
        assert activity.status_code == 200, activity.text
        assert activity.json()["runs"][0]["status"] == "unknown"
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT status FROM scene_activity WHERE operation='run'"))
                == "running"
            )
        result = service.workflows.run(application_id, DEFAULT_USER, payload)
        assert result["status"] == "unknown" and len(calls) == 1


def test_catalog_batches_reads_and_preserves_persisted_metrics(database_app):
    factory, _ = database_app
    with factory() as client:
        record = review(client, submit(client))
        application_id = record["applicationId"]
        path = f"{API}/applications/{application_id}"
        assert client.put(path + "/interaction", json={"favorite": True, "rating": 4}).is_success
        assert client.post(path + "/downloads", json={"requestId": str(uuid4())}).is_success
        repository = client.app.state.application_service.repository
        with repository.transaction() as session:
            expected = session.list_resources()
            expected_visibility = {
                item["id"]: session.get_visibility(item["id"]) for item in expected
            }
        statements = []

        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(repository.engine, "before_cursor_execute", observe)
        try:
            with repository.transaction(readonly=True) as session:
                actual = session.list_resources()
                visibility = {item["id"]: session.get_visibility(item["id"]) for item in actual}
            assert actual == expected
            assert visibility == expected_visibility
            small_count = len(statements)
            assert small_count <= 8
            # 扩大目录后仍批量查询，不能恢复成每个应用各查一遍。
            seed = next(iter(repository.seeds.values()))
            for index in range(100):
                key = f"bulk-catalog-{index}"
                repository.seeds[key] = {**deepcopy(seed), "id": key}
            statements.clear()
            with repository.transaction(readonly=True) as session:
                actual = session.list_resources()
                for item in actual:
                    session.get_visibility(item["id"])
            assert len(actual) == len(expected) + 100
            assert len(statements) == small_count
            assert not any("pg_advisory" in statement for statement in statements)
        finally:
            event.remove(repository.engine, "before_cursor_execute", observe)


def test_read_transaction_is_snapshot_and_does_not_change_write_connections(database_app):
    factory, _ = database_app
    with factory() as client:
        repository = client.app.state.application_service.repository
        policy = text(
            "SELECT current_setting('transaction_read_only'), "
            "current_setting('transaction_isolation')"
        )
        with repository.transaction(readonly=True) as session:
            assert tuple(session.db.execute(policy).one()) == ("on", "repeatable read")
            with pytest.raises(RuntimeError, match="Cannot write"):
                session.write(text("DELETE FROM scene_configuration"))
            assert len(session.rows("configuration")) == 2
        with repository.transaction() as session:
            assert tuple(session.db.execute(policy).one()) == ("off", "read committed")


def test_missing_tables_do_not_silently_use_memory(database_app):
    factory, engine = database_app
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE scene_configuration"))
    with factory() as client:
        response = client.get(API + "/applications")
        assert response.status_code == 503
        assert response.json()["detail"] == "application_database_schema_not_ready"
