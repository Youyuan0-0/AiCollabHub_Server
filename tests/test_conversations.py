"""仅验证开发问答的 HTTP 契约、历史持久化和生产禁用边界。"""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.schemas.conversations import ChatReply


@pytest.fixture
def conversation_settings(tmp_path, monkeypatch):
    """通过正式迁移准备独立测试库，不触碰用户已有开发记录。"""
    url = f"sqlite:///{(tmp_path / 'conversations.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    return Settings(
        app_env="test",
        database_url=url,
        conversation_attachment_dir=tmp_path / "attachments",
        _env_file=None,
    )


def test_conversation_round_trip_and_fixed_history(conversation_settings):
    """固定历史可读可删；提交的实际问题回显并持久化，重启后仍可打开与删除。"""
    root = "/api/v1/conversations"
    data = {
        "question": "  分析发动机异常振动\n保留换行和空格  ",
        "model": "演示模型",
        "knowledgeBase": "技术文档",
        "tools": [],
    }
    with TestClient(create_app(conversation_settings)) as client:
        history = client.get(root)
        assert history.status_code == 200
        assert len(history.json()) == 3
        sample_id = history.json()[0]["id"]
        assert client.get(f"{root}/{sample_id}").status_code == 200
        assert client.delete(f"{root}/{sample_id}").status_code == 204
        response = client.post(
            root, data={"input": json.dumps(data)}, files={"files": ("sample.txt", b"unused")}
        )
        assert response.status_code == 200
        answer = response.json()
        assert answer["question"] == data["question"]
        assert answer["knowledgeBase"] == data["knowledgeBase"]
        # 回显文本必须与请求逐字符一致，历史读取也应保持相同结果。
        assert answer["text"] == data["question"]
        assert answer["sources"] == []
        assert client.get(root).json()[0]["id"] == answer["id"]
    with TestClient(create_app(conversation_settings)) as restarted:
        assert restarted.get(f"{root}/{sample_id}").status_code == 404
        assert restarted.get(f"{root}/{answer['id']}").json() == answer
        assert restarted.delete(f"{root}/{answer['id']}").status_code == 204
        assert restarted.get(f"{root}/{answer['id']}").status_code == 404
        assert len(restarted.get(root).json()) == 2


def test_invalid_inputs_and_production_disabled(conversation_settings):
    """空白问题和损坏 JSON 返回 422；所有演示端点在生产均拒绝开放。"""
    root = "/api/v1/conversations"
    with TestClient(create_app(conversation_settings)) as client:
        for raw in (
            "{}",
            "bad-json",
            json.dumps({"question": "  ", "model": "x", "knowledgeBase": ""}),
        ):
            assert client.post(root, data={"input": raw}).status_code == 422
        assert client.get(f"{root}/unknown").status_code == 404
        assert client.delete(f"{root}/unknown").status_code == 404
        # 无登录框架不再注册身份查询端点。
        assert client.get("/api/v1/auth/session").status_code == 404
    production = conversation_settings.model_copy(update={"app_env": "production"})
    with TestClient(create_app(production)) as client:
        for method, path in (
            ("get", root),
            ("post", root),
            ("get", f"{root}/demo-chat-1"),
            ("delete", f"{root}/demo-chat-1"),
            ("post", f"{root}/new"),
            ("get", f"{root}/demo-chat-1/messages"),
            ("post", f"{root}/demo-chat-1/messages"),
            ("post", f"{root}/demo-chat-1/attachments"),
            ("get", f"{root}/demo-chat-1/attachments/example/content"),
        ):
            response = getattr(client, method)(path)
            assert response.status_code == 503
            assert response.json() == {"detail": "conversations_not_configured"}


def test_empty_conversation_and_multiturn_context(conversation_settings):
    """创建空会话后追加两轮，提供器收到完整上下文，重启后全部消息仍可读取。"""
    contexts = []

    class RecordingProvider:
        """测试提供器记录收到的上下文，验证未来第三方适配器入口。"""

        def reply(self, messages, *, model, knowledge_base, tools):
            contexts.append([message.content for message in messages])
            return ChatReply(text=f"收到：{messages[-1].content}")

    root = "/api/v1/conversations"
    with TestClient(create_app(conversation_settings, chat_provider=RecordingProvider())) as client:
        created = client.post(f"{root}/new", json={})
        assert created.status_code == 201
        identifier = created.json()["id"]
        assert created.json()["messages"] == []
        assert client.get(root).json()[0]["title"] == "新对话"
        for question in ("第一问", "第二问"):
            response = client.post(
                f"{root}/{identifier}/messages",
                json={
                    "question": question,
                    "model": "test",
                    "knowledgeBase": "kb",
                    "tools": ["检索"],
                },
            )
            assert response.status_code == 200
            assert response.json()["id"] == identifier
        assert contexts == [["第一问"], ["第一问", "收到：第一问", "第二问"]]
    with TestClient(create_app(conversation_settings)) as client:
        record = client.get(f"{root}/{identifier}/messages").json()
        assert record["title"] == "第一问"
        assert record["version"] == 2
        assert [message["role"] for message in record["messages"]] == ["user", "assistant"] * 2
        assert record["messages"][0]["tools"] == ["检索"]
        assert client.get(f"{root}/{identifier}").json()["question"] == "第二问"
        assert client.delete(f"{root}/{identifier}").status_code == 204
        assert client.get(f"{root}/{identifier}/messages").status_code == 404


def test_provider_failure_does_not_persist_partial_turn(conversation_settings):
    """第三方回复失败保留原会话，不写入只有用户消息的半轮记录。"""

    class FailingProvider:
        """只用于验证提供器异常会被统一处理，不回显内部异常内容。"""

        def reply(self, messages, **options):
            raise RuntimeError("private upstream details")

    with TestClient(create_app(conversation_settings, chat_provider=FailingProvider())) as client:
        root = "/api/v1/conversations"
        identifier = client.post(f"{root}/new", json={}).json()["id"]
        response = client.post(
            f"{root}/{identifier}/messages",
            json={
                "question": "test",
                "model": "test",
                "knowledgeBase": "",
            },
        )
        assert response.status_code == 502
        assert response.json() == {"detail": "chat_provider_unavailable"}
        record = client.get(f"{root}/{identifier}/messages").json()
        assert record["messages"] == []
        assert record["version"] == 0


def test_legacy_record_and_stale_update(conversation_settings):
    """旧单轮 JSON 无需迁移；过时的版本不能覆盖更新或复活已删除的会话。"""
    from datetime import UTC, datetime

    from fastapi import HTTPException
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db.conversations import DemoConversation
    from app.services.conversation_store import SqlConversationStore

    engine = create_engine(conversation_settings.database_url.get_secret_value())
    try:
        with Session(engine) as session:
            session.add(
                DemoConversation(
                    id="legacy",
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                    deleted=False,
                    payload=json.dumps(
                        {
                            "id": "legacy",
                            "question": "旧问题",
                            "model": "test",
                            "knowledgeBase": "",
                            "text": "旧回答",
                            "sources": [],
                        }
                    ),
                )
            )
            session.commit()
            store = SqlConversationStore(session)
            original = store.get("legacy")
            assert len(original.messages) == 2
            changed = original.model_copy(deep=True)
            changed.title = "已更新"
            changed.version = 1
            store.save(changed, expected_version=0)
            with pytest.raises(HTTPException) as conflict:
                store.save(original, expected_version=0)
            assert conflict.value.status_code == 409
            store.delete("legacy")
            with pytest.raises(HTTPException) as deleted:
                store.save(changed, expected_version=1)
            assert deleted.value.status_code == 409
            assert store.get("legacy") is None
    finally:
        engine.dispose()


def test_attachment_round_trip_restart_and_scope(conversation_settings):
    """验证原文件下载、消息与后续上下文关联、重启保留，以及跨会话和删除后的访问限制。"""
    root = "/api/v1/conversations"
    contexts = []

    class RecordingProvider:
        """记录附件元信息，确保上下文不会只保留问题文字。"""

        def reply(self, messages, **options):
            contexts.append(messages)
            return ChatReply(text="收到问题")

    with TestClient(create_app(conversation_settings, chat_provider=RecordingProvider())) as client:
        identifier = client.post(f"{root}/new", json={}).json()["id"]
        uploaded = client.post(
            f"{root}/{identifier}/attachments", files={"file": ("资料.txt", "附件正文".encode())}
        )
        assert uploaded.status_code == 201
        attachment = uploaded.json()
        assert attachment["status"] == "stored"
        data = {
            "question": "请查收附件",
            "model": "test",
            "knowledgeBase": "",
            "attachmentIds": [attachment["id"]],
        }
        assert client.post(f"{root}/{identifier}/messages", json=data).status_code == 200
        data["attachmentIds"] = []
        assert client.post(f"{root}/{identifier}/messages", json=data).status_code == 200
        assert contexts[-1][0].attachments[0].id == attachment["id"]
        other = client.post(f"{root}/new", json={}).json()["id"]
        data["attachmentIds"] = [attachment["id"]]
        assert client.post(f"{root}/{other}/messages", json=data).status_code == 404
        assert (
            client.get(f"{root}/{other}/attachments/{attachment['id']}/content").status_code == 404
        )
        data["attachmentIds"] *= 2
        assert client.post(f"{root}/{identifier}/messages", json=data).status_code == 422
    with TestClient(create_app(conversation_settings)) as client:
        record = client.get(f"{root}/{identifier}/messages").json()
        assert record["messages"][0]["attachments"] == [attachment]
        download_url = f"{root}/{identifier}/attachments/{attachment['id']}/content"
        downloaded = client.get(download_url)
        assert downloaded.content == "附件正文".encode()
        assert downloaded.headers["content-disposition"].startswith("attachment;")
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        assert client.delete(f"{root}/{identifier}").status_code == 204
        assert client.get(download_url).status_code == 404


def test_attachment_validation_and_failed_turn_retry(conversation_settings):
    """坏文件不能落盘；回答失败不关联半轮消息，修复提供器后可复用原附件编号。"""
    from app.services.chat_provider import EchoChatProvider

    class FailingProvider:
        """模拟上传已完成但模型调用失败。"""

        def reply(self, messages, **options):
            raise RuntimeError("failed")

    app = create_app(conversation_settings, chat_provider=FailingProvider())
    with TestClient(app) as client:
        root = "/api/v1/conversations"
        identifier = client.post(f"{root}/new", json={}).json()["id"]
        upload_url = f"{root}/{identifier}/attachments"
        for name, content, status in (
            ("empty.txt", b"", 422),
            ("program.exe", b"test", 415),
            ("big.txt", b"x" * (10 * 1024 * 1024 + 1), 413),
            ("huge.txt", b"x" * (11 * 1024 * 1024), 413),
        ):
            assert client.post(upload_url, files={"file": (name, content)}).status_code == status
        assert not list(conversation_settings.conversation_attachment_dir.glob("*"))
        attachment = client.post(upload_url, files={"file": ("ok.md", b"content")}).json()
        data = {
            "question": "测试",
            "model": "test",
            "knowledgeBase": "",
            "attachmentIds": [attachment["id"]],
        }
        assert client.post(f"{root}/{identifier}/messages", json=data).status_code == 502
        assert client.get(f"{root}/{identifier}/messages").json()["messages"] == []
        app.state.chat_provider = EchoChatProvider()
        assert client.post(f"{root}/{identifier}/messages", json=data).status_code == 200


def test_attachment_and_new_conversation_owner_isolation(conversation_settings):
    """模拟认证依赖已验证的身份，确认新历史和附件不会被其他用户查看、追加或删除。"""
    app = create_app(conversation_settings)

    @app.middleware("http")
    async def test_identity(request, call_next):
        # 仅测试注入可信状态，生产代码仍只能从登录依赖获取身份。
        request.state.business_identity = {"id": request.headers.get("x-test-user", "alice")}
        return await call_next(request)

    with TestClient(app) as client:
        root = "/api/v1/conversations"
        identifier = client.post(f"{root}/new", json={}).json()["id"]
        attachment = client.post(
            f"{root}/{identifier}/attachments", files={"file": ("private.txt", b"private")}
        ).json()
        forbidden = {"x-test-user": "bob"}
        assert identifier not in [item["id"] for item in client.get(root, headers=forbidden).json()]
        for path in (
            f"{root}/{identifier}",
            f"{root}/{identifier}/messages",
            f"{root}/{identifier}/attachments/{attachment['id']}/content",
        ):
            assert client.get(path, headers=forbidden).status_code == 404
        assert client.delete(f"{root}/{identifier}", headers=forbidden).status_code == 404
        assert (
            client.post(
                f"{root}/{identifier}/attachments",
                files={"file": ("a.txt", b"a")},
                headers=forbidden,
            ).status_code
            == 404
        )
        data = {
            "question": "test",
            "model": "test",
            "knowledgeBase": "",
            "attachmentIds": [attachment["id"]],
        }
        assert (
            client.post(f"{root}/{identifier}/messages", json=data, headers=forbidden).status_code
            == 404
        )
        assert (
            client.post(
                f"{root}/demo-chat-1/attachments", files={"file": ("a.txt", b"a")}
            ).status_code
            == 409
        )


def test_attachment_message_limits_and_unavailable_storage(conversation_settings, tmp_path):
    """单文件符合上限不代表整轮可提交；超限、过多编号和磁盘故障均不保存消息。"""
    root = "/api/v1/conversations"
    with TestClient(create_app(conversation_settings)) as client:
        identifier = client.post(f"{root}/new", json={}).json()["id"]
        attachments = [
            client.post(
                f"{root}/{identifier}/attachments",
                files={"file": (f"{index}.txt", b"x" * (7 * 1024 * 1024))},
            ).json()["id"]
            for index in range(3)
        ]
        data = {
            "question": "合计超限",
            "model": "test",
            "knowledgeBase": "",
            "attachmentIds": attachments,
        }
        assert client.post(f"{root}/{identifier}/messages", json=data).status_code == 413
        data["attachmentIds"] = [f"attachment-{index:032x}" for index in range(11)]
        assert client.post(f"{root}/{identifier}/messages", json=data).status_code == 422
        assert client.get(f"{root}/{identifier}/messages").json()["messages"] == []
    # 用文件占据目录位置模拟存储配置不可写，错误不包含本地绝对路径。
    blocked = tmp_path / "not-a-directory"
    blocked.write_bytes(b"blocked")
    settings = conversation_settings.model_copy(update={"conversation_attachment_dir": blocked})
    with TestClient(create_app(settings)) as client:
        response = client.post(f"{root}/{identifier}/attachments", files={"file": ("a.txt", b"a")})
        assert response.status_code == 503
        assert response.json() == {"detail": "attachment_storage_unavailable"}
