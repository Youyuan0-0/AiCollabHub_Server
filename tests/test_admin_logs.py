"""管理日志查询的授权、筛选、关联和脱敏回归。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from test_auth import auth as auth
from test_auth import login

from app.modules.common.auth.models import AuthSession, OperationLog, User
from app.modules.common.system_logs import SystemLog

PREFIX = "/api/v1/admin/logs"


@pytest.fixture
def records(auth):
    """使用隔离数据库生成同时间记录和超过默认时间范围的关联事件。"""
    client, _, iam, engine = auth
    login(client, iam, subject="mock-admin", scope="admin")
    current = datetime.now(UTC) - timedelta(minutes=5)
    request_id = uuid4()
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.iam_subject == "mock-admin"))
        for index in range(1, 4):
            db.add(
                OperationLog(
                    id=UUID(int=index),
                    actor_type="user",
                    user_id=user.id,
                    action="upload",
                    target_type="file",
                    target_id=f"report_{index}%",
                    request_id=request_id,
                    data={"password": "hidden-operation-secret", "filename": "文件.txt"},
                    created_at=current,
                )
            )
        db.add(
            OperationLog(
                id=UUID(int=4),
                actor_type="iam",
                action="sync",
                target_type="identity_sync",
                request_id=request_id,
                created_at=current - timedelta(days=20),
            )
        )
        db.add_all(
            [
                SystemLog(
                    id=UUID(int=11),
                    level="error",
                    source="storage",
                    event="log_test_failure",
                    message="test_upload failed token=hidden-message-secret",
                    request_id=request_id,
                    details={"authorization": "hidden-context-secret", "attempt": 2},
                    created_at=current,
                ),
                SystemLog(
                    id=UUID(int=12),
                    level="info",
                    source="identity_sync",
                    event="log_test_synced",
                    message="同步完成",
                    request_id=request_id,
                    created_at=current - timedelta(days=20),
                ),
            ]
        )
        db.commit()
    return client, request_id, current


@pytest.mark.parametrize("path", ["/options", "/operations", "/system", f"/system/{uuid4()}"])
def test_log_admin_authorization_is_server_side(auth, path):
    client, _, iam, _ = auth
    assert client.get(PREFIX + path).status_code == 401
    login(client, iam)
    assert client.get(PREFIX + path).status_code == 403
    login(client, iam, subject="mock-admin", scope="admin")
    assert client.get(PREFIX + path).status_code == (404 if path.count("/") == 2 else 200)


@pytest.mark.parametrize("change", ["role", "status", "revoked", "expired", "auth_disabled"])
def test_log_access_rechecks_current_identity(auth, change):
    client, _, iam, engine = auth
    login(client, iam, subject="mock-admin", scope="admin")
    with Session(engine) as db:
        if change == "role":
            db.execute(update(User).values(role="user"))
        elif change == "status":
            db.execute(update(User).values(status="disabled"))
        elif change == "revoked":
            db.execute(update(AuthSession).values(revoked_at=datetime.now(UTC)))
        elif change == "expired":
            db.execute(
                update(AuthSession).values(
                    created_at=datetime.now(UTC) - timedelta(days=2),
                    expires_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
        else:
            client.app.state.settings.auth_mode = "disabled"
        db.commit()
    assert client.get(PREFIX + "/operations").status_code in (401, 403)


def test_operation_pagination_search_and_sort(records):
    client, _, _ = records
    params = {"action": "upload", "targetType": "file", "operator": "admin", "pageSize": 2}
    first = client.get(PREFIX + "/operations", params=params)
    assert first.status_code == 200
    assert first.headers["X-Data-Mode"] == "development"
    assert first.headers["Cache-Control"] == "no-store"
    payload = first.json()
    assert payload["total"] == 3 and payload["pageSize"] == 2
    assert [item["id"] for item in payload["items"]] == [str(UUID(int=3)), str(UUID(int=2))]
    assert payload["items"][0]["actorName"] == "模拟管理员A"
    assert payload["items"][0]["summary"] == "上传 · 文件"
    assert "data" not in payload["items"][0]
    second = client.get(PREFIX + "/operations", params={**params, "page": 2}).json()
    assert [item["id"] for item in second["items"]] == [str(UUID(int=1))]
    for keyword, count in [("report_2%", 1), ("上传", 3), ("模拟管理员A", 3), ("unknown%", 0)]:
        result = client.get(PREFIX + "/operations", params={**params, "q": keyword})
        assert result.status_code == 200 and result.json()["total"] == count
    ascending = client.get(PREFIX + "/operations", params={**params, "sortOrder": "asc"}).json()
    assert [item["id"] for item in ascending["items"]] == [str(UUID(int=1)), str(UUID(int=2))]
    last_page = client.get(
        PREFIX + "/operations", params={**params, "sortOrder": "asc", "page": 2}
    ).json()
    assert [item["id"] for item in last_page["items"]] == [str(UUID(int=3))]
    assert client.get(PREFIX + f"/operations/{UUID(int=2)}").status_code == 404


def test_system_filters_and_sensitive_values(records):
    client, _, _ = records
    result = client.get(
        PREFIX + "/system", params={"level": "error", "source": "storage", "q": "test_"}
    ).json()
    assert result["total"] == 1
    assert result["items"][0]["message"] == "test_upload failed token=[REDACTED]"
    assert result["items"][0]["summary"] == "文件存储：运行错误"
    assert "details" not in result["items"][0]
    assert client.get(PREFIX + "/system", params={"q": "log_test_%"}).json()["total"] == 0
    assert client.get(PREFIX + "/system", params={"source": "database"}).json()["total"] == 0
    detail = client.get(PREFIX + f"/system/{UUID(int=11)}").json()
    assert detail["details"] == {"authorization": "[REDACTED]", "attempt": 2}
    assert "diagnosis" not in detail
    assert "hidden" not in str(detail)
    assert client.get(PREFIX + f"/system/{uuid4()}").status_code == 404


def test_only_system_request_query_can_bypass_default_time_range(records):
    client, request_id, current = records
    assert client.get(PREFIX + "/operations", params={"action": "sync"}).json()["total"] == 0
    params = {"requestId": str(request_id)}
    assert client.get(PREFIX + "/system", params=params).json()["total"] == 2
    assert (
        client.get(PREFIX + "/operations", params={**params, "action": "sync"}).json()["total"] == 0
    )
    params.update(startAt=(current - timedelta(days=1)).isoformat(), endAt=current.isoformat())
    assert client.get(PREFIX + "/system", params=params).json()["total"] == 1
    iam_record = client.get(
        PREFIX + "/operations",
        params={
            "operator": "iam",
            "startAt": (current - timedelta(days=30)).isoformat(),
            "endAt": current.isoformat(),
        },
    ).json()["items"][0]
    assert iam_record["actorName"] == "集团 IAM" and iam_record["userId"] is None


def test_admin_filter_uses_actual_role_and_logout_summary_is_clear(records, auth):
    """普通用户历史日志不冒充管理员，退出行为只展示用户可理解的摘要。"""
    client, _, current = records
    engine = auth[3]
    with Session(engine) as db:
        regular = User(iam_subject="ordinary-log-user", display_name="普通用户", role="user")
        db.add(regular)
        db.flush()
        db.add(
            OperationLog(
                actor_type="user",
                user_id=regular.id,
                action="logout",
                target_type="session",
                created_at=current,
            )
        )
        db.commit()
    all_rows = client.get(PREFIX + "/operations").json()["items"]
    assert any(
        item["actorName"] == "普通用户" and item["summary"] == "退出登录" for item in all_rows
    )
    admin_rows = client.get(PREFIX + "/operations", params={"operator": "admin"}).json()["items"]
    assert len(admin_rows) == 4
    assert all(item["actorName"] == "模拟管理员A" for item in admin_rows)
    for order in ("asc", "desc"):
        result = client.get(PREFIX + "/operations", params={"sortOrder": order}).json()["items"]
        positions = [(item["createdAt"], item["id"]) for item in result]
        assert positions == sorted(positions, reverse=order == "desc")


@pytest.mark.parametrize(
    "params",
    [
        {"sortOrder": "invalid"},
        {"operator": "system"},
        {"operator": "user"},
        {"action": "invalid"},
        {"targetType": "invalid"},
    ],
)
def test_operation_filter_rejects_unsupported_values(records, params):
    assert records[0].get(PREFIX + "/operations", params=params).status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"pageSize": 101},
        {"sortOrder": "invalid"},
        {"page": 0},
        {"requestId": "invalid"},
        {"startAt": "2026-09-01T00:00:00Z"},
        {"startAt": "2026-09-01T00:00:00", "endAt": "2026-09-02T00:00:00"},
        {"startAt": "2026-09-02T00:00:00Z", "endAt": "2026-09-01T00:00:00Z"},
        {"startAt": "2026-01-01T00:00:00Z", "endAt": "2026-09-01T00:00:00Z"},
        {"level": "unknown"},
        {"level": "debug"},
        {"level": "critical"},
        {"source": "x' OR 1=1"},
    ],
)
def test_invalid_filters_fail_without_unbounded_query(records, params):
    client, _, _ = records
    assert client.get(PREFIX + "/system", params=params).status_code == 422


def test_log_routes_are_readonly_and_options_cover_existing_values(records):
    client, _, _ = records
    options = client.get(PREFIX + "/options").json()
    assert options["operators"] == [
        {"value": "admin", "label": "管理员"},
        {"value": "iam", "label": "集团 IAM"},
    ]
    assert [
        item["value"] for item in options["actions"]
    ] == OperationLog.__table__.c.action.type.enums
    assert [
        item["value"] for item in options["targetTypes"]
    ] == OperationLog.__table__.c.target_type.type.enums
    assert {item["value"] for item in options["levels"]} == {
        "info",
        "warning",
        "error",
    }
    assert [item["label"] for item in options["levels"]] == ["INFO", "WARNING", "ERROR"]
    for method in ("post", "patch", "delete"):
        assert getattr(client, method)(PREFIX + "/operations").status_code == 405


def test_chinese_summary_search_preserves_raw_evidence(records, auth):
    """验证事件映射、旧日志兼容、中文检索和原始上下文保留。"""
    client, _, current = records
    samples = [
        ("database_request_failed", "database", "Database request failed", {"sqlState": "53300"}),
        ("runtime_warning", "cache", "Cache read failed (TimeoutError)", {}),
        ("auth_login_failed", "auth", "auth action=login", {"result": "admin_not_allowed"}),
        ("identity_sync_applied", "identity_sync", "identity_sync result=applied", {}),
    ]
    with Session(auth[3]) as db:
        for index, (event, source, message, details) in enumerate(samples, 21):
            db.add(
                SystemLog(
                    id=UUID(int=index),
                    level="warning",
                    event=event,
                    source=source,
                    message=message,
                    details=details,
                    created_at=current,
                )
            )
        db.commit()
    for keyword, expected_id, summary in [
        ("数据库请求", 21, "数据库请求执行失败"),
        ("缓存读取", 22, "缓存读取失败"),
        ("登录校验", 23, "登录校验失败"),
        ("同步请求已处理", 24, "身份同步请求已处理"),
        ("文件存储：运行错误", 11, "文件存储：运行错误"),
    ]:
        page = client.get(PREFIX + "/system", params={"q": keyword, "pageSize": 1}).json()
        assert page["total"] == 1
        assert page["items"][0]["id"] == str(UUID(int=expected_id))
        assert page["items"][0]["summary"] == summary
    database = client.get(PREFIX + f"/system/{UUID(int=21)}").json()
    assert database["details"]["sqlState"] == "53300"
    assert database["message"] == "Database request failed"
    login_failure = client.get(PREFIX + f"/system/{UUID(int=23)}").json()
    assert login_failure["details"]["result"] == "admin_not_allowed"


def test_system_sorting_is_stable_across_pages_and_preserves_filters(records, auth):
    """正倒序均对全量筛选结果生效，同时间日志跨页时不重复、不遗漏。"""
    client, _, current = records
    expected = []
    with Session(auth[3]) as db:
        for index in range(12):
            record_id = UUID(int=100 + index)
            created_at = current + timedelta(seconds=index // 3)
            expected.append((created_at, str(record_id)))
            db.add(
                SystemLog(
                    id=record_id,
                    level="warning",
                    source="cache",
                    event="sorting_probe",
                    message="排序校验",
                    created_at=created_at,
                )
            )
        db.commit()
    params = {
        "q": "sorting_probe",
        "level": "warning",
        "source": "cache",
        "pageSize": 5,
        "startAt": current.isoformat(),
        "endAt": (current + timedelta(seconds=5)).isoformat(),
    }
    for order in ("asc", "desc"):
        all_rows = []
        for page in range(1, 4):
            response = client.get(
                PREFIX + "/system", params={**params, "sortOrder": order, "page": page}
            )
            assert response.status_code == 200
            result = response.json()
            assert result["total"] == 12
            all_rows.extend(result["items"])
        expected_ids = [item[1] for item in sorted(expected, reverse=order == "desc")]
        assert [row["id"] for row in all_rows] == expected_ids
        assert all(row["source"] == "cache" and row["level"] == "warning" for row in all_rows)
    default = client.get(PREFIX + "/system", params=params).json()
    assert default["items"][0]["id"] == str(UUID(int=111))
