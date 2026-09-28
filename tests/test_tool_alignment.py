"""工具任务表差异的真实数据库回归；全部写入在隔离 schema 中回滚。"""

import json

import pytest
from fastapi import FastAPI, HTTPException, Request

from app.modules.common.identity import Actor
from app.modules.common.resource_database import ResourceDatabase
from app.modules.common.visibility import validate_visibility_targets, visibility_directory
from app.modules.tools.providers import default_configuration
from app.modules.tools.schemas import (
    InvokeTool,
    ReviewTool,
    SaveTool,
    ToolCatalogQuery,
    ToolListing,
    ToolVisibility,
)
from app.modules.tools.workspace import ToolWorkspace
from tests.test_foundation_module_flows import ADMIN, SCORES, WRITER, Runtime
from tests.test_foundation_resource_storage import foundation
from tests.test_resource_foundation import (
    connection,
    flush_constraints,
    pytestmark,
    rejected,
    schema_connection,
)
from tests.test_tools import tool_data

__all__ = ["foundation", "connection", "schema_connection", "pytestmark"]
DEFAULTS = {"owner": "测试单位", "contact": "测试联系人"}


def workspace(foundation, runtime=None):
    """重建工作区，避免进程内对象掩盖数据库保存缺失。"""
    return ToolWorkspace(
        ResourceDatabase(
            foundation,
            "tool",
            configuration=default_configuration(),
            runtime=runtime,
        )
    )


def publish(foundation, source="第三方接入"):
    """发布并上架工具，显式使用数据库当前修订号。"""
    service = workspace(foundation)
    draft = service.save(SaveTool(data=tool_data(sourceType=source), submit=True), WRITER, DEFAULTS)
    service.review(
        draft["id"],
        ReviewTool(
            revision=draft["revision"],
            decision="approved",
            scores=SCORES,
        ),
        ADMIN,
    )
    current = service.resource(draft["toolId"], ADMIN, True)
    service.listing(
        current["id"], ToolListing(status="listed", revision=current["revision"]), ADMIN
    )
    return current["id"]


@pytest.mark.parametrize(
    "source, stored",
    [("第三方接入", "third_party"), ("开源集成", "third_party"), ("自研工具", "self_developed")],
)
def test_sources_and_immutable_version_notes(foundation, connection, source, stored):
    """两次发布保留变更说明，提交、审核快照、正式版本一致，旧版本不被改写。"""
    tool_id = publish(foundation, source)
    assert connection.exec_driver_sql("SELECT source_type FROM ai_tools").scalar_one() == stored
    assert workspace(foundation).resource(tool_id, WRITER)["canPublishVersion"]
    assert not workspace(foundation).resource(tool_id, ADMIN)["canPublishVersion"]
    with pytest.raises(HTTPException) as denied:
        workspace(foundation).new_version(tool_id, ADMIN)
    assert denied.value.status_code == 403
    draft = workspace(foundation).new_version(tool_id, WRITER)
    assert workspace(foundation).new_version(tool_id, WRITER)["id"] == draft["id"]
    pending = workspace(foundation).save(
        SaveTool(
            data={**draft["data"], "description": "本版更新简介"},
            submit=True,
            revision=draft["revision"],
        ),
        WRITER,
        DEFAULTS,
        draft["id"],
    )
    assert workspace(foundation).resource(tool_id, WRITER)["description"] != "本版更新简介"
    workspace(foundation).review(
        pending["id"],
        ReviewTool(
            revision=pending["revision"],
            decision="approved",
            scores=SCORES,
        ),
        ADMIN,
    )
    flush_constraints(connection)
    latest = workspace(foundation).resource(tool_id, WRITER)
    assert latest["description"] == "本版更新简介"
    assert [item["note"] for item in latest["versions"]] == ["更新：版本号、工具简介", "首次发布"]
    rows = (
        connection.exec_driver_sql("""
        SELECT v.version_notes, p.version_notes AS submitted,
               r.submission_snapshot->>'versionNotes' AS reviewed
        FROM ai_tool_versions v JOIN ai_tool_publications p ON p.id=v.publication_id
        JOIN resource_reviews r ON r.publication_id=p.id AND r.submission_no=p.submission_no
    """)
        .mappings()
        .all()
    )
    assert len(rows) == 2
    assert all(row["version_notes"] == row["submitted"] == row["reviewed"] for row in rows)


def test_success_and_failure_survive_runtime_expiration(foundation, connection):
    """有效执行的成功、失败均持久化；校验失败不计调用，摘要不保留输入输出。"""
    tool_id = publish(foundation)
    runtime = Runtime()
    service = workspace(foundation, runtime)
    with pytest.raises(HTTPException):
        service.invoke(tool_id, InvokeTool(inputs={}), WRITER)
    run = service.invoke(tool_id, InvokeTool(inputs={"text": "不应保存的输入"}), WRITER)
    with pytest.raises(HTTPException) as failure:
        service.invoke(
            tool_id, InvokeTool(inputs={"text": "失败调用"}, simulateFailure=True), WRITER
        )
    assert failure.value.status_code == 502
    before = service.resource(tool_id, ADMIN, True)["runtime"]
    runtime.entries.clear()
    assert workspace(foundation).resource(tool_id, ADMIN, True)["runtime"] == before
    assert before["total"] == 2 and before["successRate"] == before["errorRate"] == 50
    assert before["averageResponseMs"] >= 0
    with service.repository.transaction() as session:
        session.put_entry("runs", run["id"], run)
    rows = (
        connection.exec_driver_sql("SELECT data FROM operation_logs WHERE action='invoke'")
        .scalars()
        .all()
    )
    assert len(rows) == 2 and {row["success"] for row in rows} == {True, False}
    assert all("inputs" not in row and "output" not in row and "text" not in row for row in rows)
    flush_constraints(connection)


def test_directory_policies_persist_and_protect_every_access(foundation, connection):
    """单位和项目规则在详情、目录、互动和调用中生效，可无残留切回指定用户与全员。"""
    tool_id = publish(foundation)
    reader = Actor(WRITER.id, WRITER.name, "publisher", "org-a", ("project-a",))
    outsider = Actor(ADMIN.id, ADMIN.name, "user", "org-b", ("project-b",))
    revision = 1
    for scope, target in (
        ("organizations", "org-a"),
        ("projects", "project-a"),
        ("members", WRITER.id),
        ("all", None),
    ):
        policy = ToolVisibility(scope=scope, targets=[target] if target else [], revision=revision)
        saved = workspace(foundation).visibility(tool_id, policy, ADMIN)
        revision = saved["revision"]
        flush_constraints(connection)
        assert workspace(foundation).resource(tool_id, ADMIN, True)["visibility"] == saved
        assert workspace(foundation).resource(tool_id, reader)["id"] == tool_id
        assert workspace(foundation).catalog(ToolCatalogQuery(), reader)["total"] == 1
        if target:
            assert workspace(foundation).catalog(ToolCatalogQuery(), outsider)["total"] == 0
            for action in (
                lambda: workspace(foundation).resource(tool_id, outsider),
                lambda: workspace(foundation).interact(tool_id, outsider, favorite=True),
                lambda: workspace(foundation).ratings(tool_id, outsider),
                lambda: workspace(foundation).invoke(
                    tool_id, InvokeTool(inputs={"text": "拒绝"}), outsider
                ),
            ):
                with pytest.raises(HTTPException) as denied:
                    action()
                assert denied.value.status_code == 404
        else:
            assert workspace(foundation).catalog(ToolCatalogQuery(), outsider)["total"] == 1
        with pytest.raises(HTTPException) as stale:
            workspace(foundation).visibility(tool_id, policy, ADMIN)
        assert stale.value.status_code == 409
    assert (
        connection.exec_driver_sql("SELECT count(*) FROM resource_visibility_targets").scalar_one()
        == 0
    )
    assert (
        connection.exec_driver_sql(
            "SELECT directory_targets FROM resource_visibility_policies"
        ).scalar_one()
        == []
    )
    for invalid in ([], [None], [""], ["a" * 201]):
        with rejected(connection):
            connection.exec_driver_sql(
                "UPDATE resource_visibility_policies SET scope='projects', "
                "directory_targets=%s, revision=revision+1",
                (invalid,),
            )


def test_trusted_directory_combines_database_members_and_admin_scope(
    foundation, tmp_path, monkeypatch
):
    """数据库模式也提供可信单位与项目，拒绝越权或不存在的目标编号。"""
    path = tmp_path / "directory.json"
    path.write_text(
        json.dumps(
            {
                "organizations": [{"id": value, "name": value} for value in ("a", "b")],
                "projects": [
                    {"id": "pa", "name": "项目A", "organizationId": "a"},
                    {"id": "pb", "name": "项目B", "organizationId": "b"},
                ],
                "members": [
                    {"id": WRITER.id, "name": WRITER.name, "organizationId": "a"},
                    {"id": ADMIN.id, "name": ADMIN.name, "organizationId": "b"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(foundation.settings, "applications_identity_directory", str(path))
    app = FastAPI()
    app.state.foundation = foundation
    app.state.settings = foundation.settings
    request = Request({"type": "http", "app": app, "state": {}})
    own_admin = Actor(WRITER.id, WRITER.name, "org_admin", "a")
    directory = visibility_directory(request, own_admin)
    assert [item["id"] for item in directory["organizations"]] == ["a"]
    assert [item["id"] for item in directory["projects"]] == ["pa"]
    assert [item["id"] for item in directory["members"]] == [WRITER.id]
    assert len(visibility_directory(request, ADMIN)["organizations"]) == 2
    for scope, target in (
        ("organizations", "b"),
        ("projects", "pb"),
        ("projects", "unknown"),
        ("members", ADMIN.id),
    ):
        with pytest.raises(HTTPException) as invalid:
            validate_visibility_targets(
                request, own_admin, ToolVisibility(scope=scope, targets=[target], revision=1)
            )
        assert invalid.value.status_code == 422
