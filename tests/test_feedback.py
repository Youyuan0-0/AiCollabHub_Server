"""反馈通过真实 HTTP/登录/数据库流程测试，文件服务使用可控替身。"""

import base64
import importlib.util
import os
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, inspect, select, update
from sqlalchemy.orm import Session
from test_auth import auth as auth
from test_auth import csrf, login

from app.db.foundation import identifier, utc_now
from app.modules.common.auth.mock_iam import ACCOUNTS
from app.modules.feedback import attachments
from app.storage.client import ObjectInfo, ObjectNotFound, StorageUnavailable
from app.storage.files import FileStore
from app.storage.maintenance import cleanup_files
from tests.test_foundation_resource_storage import foundation
from tests.test_resource_foundation import connection, schema_connection

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5WQAAAAASUVORK5CYII="
)
ROOT = "/api/v1/feedback"
__all__ = ["foundation", "connection", "schema_connection", "auth"]


class MemoryStorage:
    bucket = "test-feedback"

    def __init__(self):
        self.objects = {}
        self.fail_put = False
        self.fail_delete = False

    def put_object(self, key, data, *, length, content_type):
        if self.fail_put:
            raise StorageUnavailable()
        content = data.read()
        assert len(content) == length
        self.objects[key] = content
        return ObjectInfo(key, length, None, content_type)

    def stat_object(self, key):
        if key not in self.objects:
            raise ObjectNotFound()
        return ObjectInfo(key, len(self.objects[key]), None, "image/png")

    @contextmanager
    def open_object(self, key):
        content = self.objects[key]

        class Stream:
            def stream(self, size):
                yield content

        yield Stream()

    def remove_object(self, key):
        if self.fail_delete:
            raise StorageUnavailable()
        self.objects.pop(key, None)


@pytest.fixture
def feedback(auth, monkeypatch, request):
    if not os.environ.get("RESOURCE_FOUNDATION_TEST_DATABASE_URL"):
        pytest.skip("Feedback HTTP flows require the explicit PostgreSQL test connection")
    database = request.getfixturevalue("foundation")
    monkeypatch.setitem(ACCOUNTS, "mock-user-b", "第二个测试用户")
    monkeypatch.setitem(ACCOUNTS, "another-user", "其他测试用户")
    a, b, iam, engine = auth
    storage = MemoryStorage()
    a.app.state.resources.storage = storage
    b.app.state.resources.storage = storage
    users = database.table("users")
    with database.transaction() as session:
        for subject in ("mock-user", "mock-admin", "mock-user-b", "another-user"):
            session.execute(
                users.insert().values(
                    id=uuid4(),
                    iam_subject=subject,
                    status="active",
                    role="admin" if subject == "mock-admin" else "user",
                )
            )
    for client in (a, b):
        client.app.state.foundation = database
        client.app.state.file_store = FileStore(database, storage)
    login(a, iam)
    # 两个独立浏览器身份：切断 IAM Cookie，避免换账号触发前一账号的单点退出。
    iam.cookies.clear()
    login(b, iam, scope="admin", subject="mock-admin")
    iam.cookies.clear()
    yield a, b, iam, database.engine, storage


def payload(**overrides):
    return {
        "type": "function_error",
        "module": "applications",
        "title": "提交失败",
        "description": "填写内容后出现错误",
        "contact": "内线 1234",
        "attachmentIds": [],
        "clientRequestId": str(uuid4()),
        **overrides,
    }


def upload(client, request_id=None, data=PNG, name="issue.png"):
    return client.post(
        ROOT + "/attachments",
        files={"file": (name, data, "image/png")},
        data={"clientRequestId": request_id or str(uuid4())},
        headers=csrf(client),
    )


def create(client, **overrides):
    response = client.post(ROOT, json=payload(**overrides), headers=csrf(client))
    assert response.status_code == 201, response.text
    return response.json()


def test_cross_instance_submission_processing_history_and_counts(feedback):
    a, b, iam, engine, storage = feedback
    attachment = upload(a).json()
    record = create(a, attachmentIds=[attachment["id"]])
    assert record["version"] == 1 and record["events"][0]["action"] == "submitted"
    assert a.get(ROOT).json()["total"] == 1
    assert b.get("/api/v1/admin/feedback").json()["items"][0]["id"] == record["id"]
    change = {
        "reply": "已修复",
        "status": "resolved",
        "expectedVersion": 1,
        "clientRequestId": str(uuid4()),
    }
    url = "/api/v1/admin/feedback/" + record["id"]
    saved = b.patch(url, json=change, headers=csrf(b, "admin"))
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 2
    assert b.patch(url, json=change, headers=csrf(b, "admin")).json()["version"] == 2
    detail = a.get(ROOT + "/" + record["id"]).json()
    assert detail["status"] == "resolved" and detail["reply"] == "已修复"
    assert len(detail["events"]) == 2
    assert b.get("/api/v1/admin/overview").json() == {"total": 1, "open": 0, "resolved": 1}
    download_url = ROOT + "/" + record["id"] + "/attachments/" + attachment["id"] + "/content"
    downloaded = a.get(download_url)
    assert downloaded.content == PNG
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    login(a, iam, subject="mock-user-b")
    assert a.get(ROOT).json()["items"] == []
    assert a.get(ROOT + "/" + record["id"]).status_code == 404
    assert a.get(download_url).status_code == 404
    assert a.get(url).status_code == 403
    with Session(engine) as db:
        assert (
            db.scalar(
                select(func.count()).select_from(a.app.state.foundation.table("feedback_events"))
            )
            == 2
        )


def test_submission_retries_and_changed_content(feedback):
    a, _, _, engine, _ = feedback
    value = payload()
    first = a.post(ROOT, json=value, headers=csrf(a))
    assert first.status_code == 201
    second = a.post(ROOT, json=value, headers=csrf(a))
    assert second.status_code == 200 and second.json()["id"] == first.json()["id"]
    changed = a.post(ROOT, json={**value, "title": "变化"}, headers=csrf(a))
    assert changed.status_code == 409
    assert (
        a.get(ROOT + "/submissions/" + value["clientRequestId"]).json()["id"] == first.json()["id"]
    )
    with Session(engine) as db:
        assert (
            db.scalar(
                select(func.count()).select_from(a.app.state.foundation.table("feedback_records"))
            )
            == 1
        )


@pytest.mark.parametrize(
    "change",
    [
        {"contact": "  "},
        {"description": " "},
        {"type": "other"},
        {"module": "other"},
        {"type": "unknown"},
        {"submitterId": "someone-else"},
        {"attachmentIds": [str(uuid4()) for _ in range(4)]},
    ],
)
def test_required_fields_and_ownership_fields_rejected(feedback, change):
    a, _, _, _, _ = feedback
    assert a.post(ROOT, json=payload(**change), headers=csrf(a)).status_code == 422


def test_three_attachments_other_and_invalid_binding_atomicity(feedback):
    a, _, iam, engine, _ = feedback
    files = [upload(a).json()["id"] for _ in range(3)]
    failed = a.post(ROOT, json=payload(attachmentIds=[files[0], str(uuid4())]), headers=csrf(a))
    assert failed.status_code == 409
    with Session(engine) as db:
        table = a.app.state.foundation.table("files")
        assert (
            db.scalar(select(table.c.status).where(table.c.id == identifier(files[0]))) == "ready"
        )
        assert (
            db.scalar(
                select(func.count()).select_from(a.app.state.foundation.table("feedback_records"))
            )
            == 0
        )
    record = create(
        a,
        type="other",
        typeOther="权限问题",
        module="other",
        moduleOther="新模块",
        attachmentIds=files,
    )
    assert len(record["attachments"]) == 3
    assert a.delete(ROOT + "/attachments/" + files[0], headers=csrf(a)).status_code == 409
    login(a, iam, subject="another-user")
    assert a.post(ROOT, json=payload(attachmentIds=[files[1]]), headers=csrf(a)).status_code == 409


def test_upload_deduplication_limits_and_cancellation(feedback, monkeypatch):
    a, _, _, engine, storage = feedback
    request_id = str(uuid4())
    first = upload(a, request_id)
    assert first.status_code == 201, first.text
    assert upload(a, request_id).json()["id"] == first.json()["id"]
    assert upload(a, request_id, PNG + b"different").status_code == 409
    assert upload(a, data=b"not png").status_code == 415
    assert upload(a, data=b"", name="empty.png").status_code in (415, 422)
    monkeypatch.setattr(attachments, "MAX_FILE_BYTES", len(PNG))
    assert upload(a).status_code == 201
    assert upload(a, data=PNG + b"x").status_code == 413
    assert a.delete(ROOT + "/attachments/" + first.json()["id"], headers=csrf(a)).status_code == 204
    assert a.get(ROOT + "/uploads/" + request_id).json()["state"] == "deleting"


def test_storage_failure_and_cleanup_never_delete_attached(feedback):
    a, _, _, engine, storage = feedback
    attached = upload(a).json()
    create(a, attachmentIds=[attached["id"]])
    temporary = upload(a).json()
    storage.fail_put = True
    assert upload(a).status_code == 503
    storage.fail_put = False
    database = a.app.state.foundation
    table = database.table("files")
    engine.execute(
        update(table)
        .where(table.c.id == identifier(temporary["id"]))
        .values(expires_at=utc_now() - timedelta(seconds=1))
    )
    cleanup_files(database, storage)
    assert (
        engine.scalar(select(table.c.object_key).where(table.c.id == identifier(attached["id"])))
        in storage.objects
    )
    assert (
        engine.scalar(select(table.c.status).where(table.c.id == identifier(temporary["id"])))
        == "deleted"
    )


def test_stale_admin_updates_and_history_pagination(feedback):
    a, b, _, _, _ = feedback
    record = create(a)
    url = "/api/v1/admin/feedback/" + record["id"]
    for version in range(1, 23):
        response = b.patch(
            url,
            json={
                "reply": str(version),
                "status": "processing",
                "expectedVersion": version,
                "clientRequestId": str(uuid4()),
            },
            headers=csrf(b, "admin"),
        )
        assert response.status_code == 200, response.text
    conflict = b.patch(
        url,
        json={
            "reply": "过期处理",
            "status": "resolved",
            "expectedVersion": 1,
            "clientRequestId": str(uuid4()),
        },
        headers=csrf(b, "admin"),
    )
    assert conflict.status_code == 409
    page = a.get(ROOT + "/" + record["id"]).json()
    assert len(page["events"]) == 20 and page["nextEventVersion"] == 4
    older = a.get(ROOT + "/" + record["id"], params={"beforeEventVersion": 4}).json()
    assert len(older["events"]) == 3 and older["events"][-1]["action"] == "submitted"


def test_upload_csrf_and_anonymous_requests(feedback):
    a, _, _, _, _ = feedback
    assert a.post(ROOT + "/attachments", files={"file": ("a.png", PNG)}).status_code == 403
    a.cookies.clear()
    assert a.get(ROOT).status_code == 401
    assert a.post(ROOT + "/attachments", files={"file": ("a.png", PNG)}).status_code == 401


def test_migration_creates_and_removes_only_feedback_tables(tmp_path):
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20260917_01_feedback.py"
    spec = importlib.util.spec_from_file_location("feedback_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            assert set(inspect(connection).get_table_names()) == {
                "feedback_records",
                "feedback_attachments",
                "feedback_events",
            }
            module.downgrade()
            assert inspect(connection).get_table_names() == []
    engine.dispose()
