"""可选报告的实际字节、权限、审核绑定及失败回滚测试。"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.resources import ApplicationResources
from app.main import create_app
from tests.test_application_tutorials import TutorialStorage, select_actor
from tests.test_foundation_module_flows import SCORES

API = "/api/v1/admin/application-publications"


class ReportStorage(TutorialStorage):
    """带存储桶元信息的独立文件服务替身。"""

    bucket = "test-reports"


@pytest.fixture
def report_app():
    storage = ReportStorage()
    application = create_app(
        Settings(_env_file=None), resources_factory=lambda _: ApplicationResources(storage=storage)
    )
    with TestClient(application) as client:
        yield client, storage


def submit_application(client, name="报告测试应用"):
    response = client.post(
        "/api/v1/application-publications",
        json={
            "submit": True,
            "data": {
                "name": name,
                "owner": "研发单位",
                "summary": "测试可选报告",
                "triggerType": "手动",
                "communicationProtocol": "HTTP",
                "deployment": "工具链",
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def upload_report(client, record, name="质量评测.txt", content=b"report evidence"):
    return client.post(
        f"{API}/{record['id']}/report-files?revision={record['revision']}",
        files={"file": (name, content)},
    )


def review_with_reports(client, record, ids=(), decision="approved"):
    return client.post(
        f"{API}/{record['id']}/review",
        json={
            "revision": record["revision"],
            "decision": decision,
            "reason": "审核意见",
            "scores": SCORES,
            "reportFileIds": list(ids),
        },
    )


def test_optional_report_persist_preview_and_history(report_app):
    client, storage = report_app
    record = submit_application(client)
    response = upload_report(client, record, content=b"<script>alert(1)</script>")
    assert response.status_code == 201, response.text
    file = response.json()
    assert "bucket" not in file and "objectKey" not in file
    url = f"{API}/{record['id']}/report-files/{file['id']}"
    assert client.get(f"{API}/{record['id']}/report-files").json() == [file]
    preview = client.get(url + "/content?preview=true")
    assert "&lt;script&gt;" in preview.text
    assert "sandbox" in preview.headers["content-security-policy"]
    assert "no-store" in preview.headers["cache-control"]
    assert client.get(url + "/content", headers={"Range": "bytes=0-6"}).content == b"<script"
    reviewed = review_with_reports(client, record, [file["id"]])
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["reviews"][-1]["reportFiles"] == [file]
    assert client.get(f"{API}/{record['id']}").json()["reviews"][-1]["reportFiles"] == [file]
    assert client.delete(url).status_code == 409
    assert upload_report(client, record).status_code == 409
    assert client.get(url + "/content").status_code == 200
    assert len(storage.files) == 2
    no_report = submit_application(client, "无报告也能审核")
    assert review_with_reports(client, no_report).status_code == 200


def test_report_removal_and_file_count(report_app):
    client, storage = report_app
    record = submit_application(client)
    files = [upload_report(client, record).json() for _ in range(3)]
    assert upload_report(client, record).status_code == 422
    assert len(storage.files) == 6
    file = files.pop()
    url = f"{API}/{record['id']}/report-files/{file['id']}"
    assert client.delete(url).status_code == 204
    assert client.get(url + "/content").status_code == 404
    assert len(storage.files) == 4
    assert upload_report(client, record).status_code == 201


def test_cannot_attach_other_application_or_other_reviewer(report_app):
    client, _ = report_app
    first, second = submit_application(client), submit_application(client, "第二个应用")
    file = upload_report(client, first).json()
    assert review_with_reports(client, second, [file["id"]]).status_code == 422
    assert review_with_reports(client, first, [file["id"], file["id"]]).status_code == 422
    select_actor(client, "demo-org-admin", admin=True)
    url = f"{API}/{first['id']}/report-files/{file['id']}"
    assert client.get(url + "/content").status_code == 404
    assert client.delete(url).status_code == 404
    assert review_with_reports(client, first, [file["id"]]).status_code == 422
    select_actor(client, "demo-reader", admin=True)
    assert upload_report(client, first).status_code == 403
    assert client.get(url + "/content").status_code == 403


def test_report_failure_does_not_submit_review(report_app):
    client, storage = report_app
    record = submit_application(client)
    storage.fail = True
    assert upload_report(client, record).status_code == 503
    assert client.get(f"{API}/{record['id']}/report-files").json() == []
    assert client.get(f"{API}/{record['id']}").json()["status"] == "pending"
    storage.fail = False
    file = upload_report(client, record).json()
    record["revision"] += 1
    assert review_with_reports(client, record, [file["id"]]).status_code == 409
    assert len(client.get(f"{API}/{record['id']}/report-files").json()) == 1


@pytest.mark.parametrize(
    "name,content",
    [
        ("报告.exe", b"MZ"),
        ("报告.pdf", b"not pdf"),
        ("空报告.txt", b""),
        ("../报告.txt", b"path"),
        ("报告.docx", b"not a docx"),
    ],
)
def test_invalid_report_files_rejected(report_app, name, content):
    client, storage = report_app
    assert upload_report(client, submit_application(client), name, content).status_code == 422
    assert storage.files == {}


def test_resubmission_keeps_old_report_and_rejects_reuse(report_app):
    client, _ = report_app
    record = submit_application(client)
    file = upload_report(client, record).json()
    old = review_with_reports(client, record, [file["id"]], "needs_changes").json()
    response = client.put(
        f"/api/v1/application-publications/{record['id']}",
        json={
            "revision": old["revision"],
            "data": old["data"],
            "submit": True,
        },
    )
    assert response.status_code == 200, response.text
    current = response.json()
    assert review_with_reports(client, current, [file["id"]]).status_code == 422
    result = review_with_reports(client, current).json()
    assert result["reviews"][0]["reportFiles"] == [file]
    assert result["reviews"][1]["reportFiles"] == []
    assert client.get(f"{API}/{record['id']}/report-files/{file['id']}/content").status_code == 200
