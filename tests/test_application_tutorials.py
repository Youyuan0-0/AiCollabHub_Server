"""教程上传、权限、草稿/发布关联及内嵌阅读的集成测试。"""

from base64 import b64decode
from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.resources import ApplicationResources
from app.main import create_app
from app.modules.common.document_content import DOCUMENT_LIMIT
from app.storage.client import ObjectNotFound, StorageUnavailable

ROOT = "/api/v1"


class TutorialStorage:
    bucket = "tutorial-tests"
    """独立文件服务替身，记录实际字节和范围读取，不访问真实文件服务。"""

    max_upload_bytes = 100 * 1024 * 1024

    def __init__(self):
        self.files = {}
        self.fail = False

    def put_object(self, key, stream, *, length, content_type):
        if self.fail:
            raise StorageUnavailable()
        self.files[key] = stream.read()
        assert len(self.files[key]) == length

    def stat_object(self, key):
        if key not in self.files:
            raise ObjectNotFound()
        return SimpleNamespace(size=len(self.files[key]))

    @contextmanager
    def open_object(self, key, *, offset=0, length=0):
        yield BytesIO(self.files[key][offset : offset + length if length else None])

    def remove_object(self, key):
        self.files.pop(key, None)


@pytest.fixture
def tutorial_app():
    storage = TutorialStorage()
    application = create_app(
        Settings(_env_file=None), resources_factory=lambda _: ApplicationResources(storage=storage)
    )
    with TestClient(application) as client:
        yield client, storage


def upload(client, name="使用说明.txt", content=None, level="internal"):
    if content is None:
        content = "教程正文".encode()
    return client.post(
        ROOT + "/application-tutorial-files",
        files={"file": (name, content)},
        data={"securityLevel": level},
    )


def tutorial(file):
    return {
        "title": "操作指南",
        "kind": file["kind"],
        "url": "",
        "source": "file",
        "attachment": {key: value for key, value in file.items() if key != "kind"},
    }


def publication(client, file, **overrides):
    return client.post(
        ROOT + "/application-publications",
        json={
            "data": {
                "name": "教程应用",
                "summary": "教程上传验证",
                "owner": "研发单位",
                "triggerType": "手动",
                "communicationProtocol": "HTTP",
                "deployment": "工具链",
                "tutorials": [tutorial(file)],
            },
            **overrides,
        },
    )


def select_actor(client, actor_id, *, admin=False):
    path = ROOT + "/application-demo-session"
    response = client.post(
        path, json={"actorId": actor_id, "audience": "admin" if admin else "user"}
    )
    assert response.status_code == 200, response.text


def test_upload_draft_review_read_and_visibility(tutorial_app):
    client, storage = tutorial_app
    uploaded = upload(client)
    assert uploaded.status_code == 201, uploaded.text
    file = uploaded.json()
    assert "objectKey" not in file
    content_url = ROOT + f"/application-tutorial-files/{file['id']}/content"
    original = client.get(content_url)
    assert original.content == "教程正文".encode()
    assert original.headers["content-disposition"].startswith("attachment;")
    preview = client.get(content_url + "?preview=true")
    assert "<pre>教程正文</pre>" in preview.text
    assert "sandbox" in preview.headers["content-security-policy"]
    record = publication(client, file).json()
    reloaded = client.get(ROOT + f"/application-publications/{record['id']}").json()
    assert reloaded["data"]["tutorials"] == [tutorial(file)]
    select_actor(client, "demo-reader")
    assert client.get(content_url).status_code == 404
    assert client.get(content_url + "?publicationId=" + record["id"]).status_code == 404
    admin_url = content_url.replace("/api/v1/", "/api/v1/admin/")
    assert client.get(admin_url).status_code == 200
    select_actor(client, "demo-reader", admin=True)
    assert client.get(admin_url).status_code == 403
    client.cookies.delete("application_admin")
    assert upload(client).status_code == 403
    select_actor(client, "demo-publisher")
    submitted = client.put(
        ROOT + f"/application-publications/{record['id']}",
        json={
            "revision": record["revision"],
            "data": record["data"],
            "submit": True,
        },
    ).json()
    reviewed = client.post(
        ROOT + f"/admin/application-publications/{record['id']}/review",
        json={
            "revision": submitted["revision"],
            "decision": "approved",
            "scores": {
                "formatCompliance": 4,
                "safetyCompliance": 4,
                "annotationCompliance": 4,
                "structuralCompleteness": 4,
                "authenticity": 4,
                "consistency": 4,
                "cleanliness": 4,
            },
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    select_actor(client, "demo-reader")
    linked = content_url + "?applicationId=" + record["applicationId"]
    assert client.get(linked).content == original.content
    assert client.get(content_url + "?applicationId=missing").status_code == 404
    visibility = client.put(
        ROOT + f"/admin/applications/{record['applicationId']}/visibility",
        json={
            "scope": "members",
            "targets": ["demo-publisher"],
        },
    )
    assert visibility.status_code == 200, visibility.text
    assert client.get(linked).status_code == 404
    assert len(storage.files) == 2


def test_file_metadata_cannot_be_forged_and_security_is_enforced(tutorial_app):
    client, _ = tutorial_app
    file = upload(client).json()
    forged = {**file, "name": "伪造文件名.txt", "securityLevel": "public"}
    record = publication(client, forged)
    assert record.status_code == 201
    assert record.json()["data"]["tutorials"][0]["attachment"]["name"] == file["name"]
    assert publication(client, forged, securityLevel="public").status_code == 422
    assert publication(client, {**file, "kind": "video"}).status_code == 422
    select_actor(client, "demo-org-admin")
    # 单位管理员可以维护本单位发布者上传的资料。
    assert publication(client, file).status_code == 201
    select_actor(client, "demo-outsider")
    assert client.get(ROOT + f"/application-tutorial-files/{file['id']}/content").status_code == 404
    client.cookies.clear()
    actor = client.app.state.application_identity
    from app.modules.common.identity import Actor

    token, _ = actor.select("demo-publisher")
    actor._sessions[token] = Actor("another-publisher", "其他发布者", "publisher", "org-production")
    client.cookies.set("application_user", token)
    assert publication(client, file).status_code == 422


def test_video_and_pdf_range_and_head(tutorial_app):
    client, _ = tutorial_app
    content = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00" + b"video-data" * 10
    file = upload(client, "演示.mp4", content).json()
    path = ROOT + f"/application-tutorial-files/{file['id']}/content?preview=true"
    response = client.get(path, headers={"Range": "bytes=8-15"})
    assert response.status_code == 206 and response.content == content[8:16]
    assert response.headers["content-range"] == f"bytes 8-15/{len(content)}"
    assert response.headers["content-type"] == "video/mp4"
    assert client.get(path, headers={"Range": "bytes=-5"}).content == content[-5:]
    assert client.get(path, headers={"Range": "bytes=9999-"}).status_code == 416
    assert client.head(path).headers["content-length"] == str(len(content))
    pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF"
    file = upload(client, "操作.pdf", pdf).json()
    response = client.get(ROOT + f"/application-tutorial-files/{file['id']}/content?preview=true")
    assert response.content == pdf and response.headers["content-type"] == "application/pdf"


def test_word_preview_escapes_active_content(tutorial_app):
    client, _ = tutorial_app
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>&lt;script&gt;alert(1)&lt;/script&gt;</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格内容</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>',
        )
    file = upload(client, "指南.docx", stream.getvalue()).json()
    page = client.get(ROOT + f"/application-tutorial-files/{file['id']}/content?preview=true")
    assert page.status_code == 200
    assert "<script>" not in page.text and "&lt;script&gt;" in page.text
    assert "<table>" in page.text and "表格内容" in page.text


@pytest.mark.parametrize(
    "name,content,status",
    [
        ("空.txt", b"", 422),
        ("危险.html", b"<script>alert(1)</script>", 422),
        ("伪装.pdf", b"executable", 422),
        ("损坏.docx", b"bad zip", 422),
        ("大文档.txt", b"x" * (DOCUMENT_LIMIT + 1), 413),
    ],
    ids=["empty", "html", "fake-pdf", "broken-word", "oversized"],
)
def test_invalid_uploads_are_not_registered(tutorial_app, name, content, status):
    client, storage = tutorial_app
    assert upload(client, name, content).status_code == status
    assert storage.files == {}


def test_failed_storage_returns_error_without_saved_file(tutorial_app):
    client, storage = tutorial_app
    storage.fail = True
    assert upload(client).status_code == 503
    assert storage.files == {}
    storage.fail = False
    assert upload(client, level="confidential").status_code == 422


def test_word_inline_images_are_retained_without_external_images(tutorial_app):
    """保留 Word 内置截图，同时不加载关系文件中的外部图片。"""
    client, _ = tutorial_app
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                "<w:document "
                'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                '<w:body><w:p><w:r><w:drawing><a:blip r:embed="rId1"/>'
                '<a:blip r:embed="rId2"/></w:drawing></w:r></w:p></w:body></w:document>'
            ),
        )
        archive.writestr(
            "word/_rels/document.xml.rels",
            (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="example/image" Target="media/image1.png"/>'
                '<Relationship Id="rId2" Type="example/image" TargetMode="External" '
                'Target="https://external.example/image.png"/></Relationships>'
            ),
        )
        archive.writestr(
            "word/media/image1.png",
            b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO6yfiYAAAAASUVORK5CYII="
            ),
        )
    file = upload(client, "截图教程.docx", stream.getvalue()).json()
    response = client.get(ROOT + f"/application-tutorial-files/{file['id']}/content?preview=true")
    assert response.text.count('<img src="data:image/png;base64,') == 1
    assert "external.example" not in response.text
    assert "img-src data:" in response.headers["content-security-policy"]
