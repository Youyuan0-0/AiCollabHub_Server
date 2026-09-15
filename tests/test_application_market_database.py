"""复用隔离 PostgreSQL fixture，检查外部请求跨实例持久化与恢复。"""

import json

import httpx
from sqlalchemy import text
from test_application_database_storage import database_app, review, submit  # noqa: F401
from test_application_market import Supplier, draft

from app.modules.applications.identity import DEFAULT_USER
from app.modules.applications.market_contract import MarketMutation
from app.modules.applications.market_workflows import MarketWorkflows


def test_market_journal_reuses_existing_table_and_survives_restart(database_app, tmp_path):  # noqa: F811
    clients, engine = database_app
    supplier = Supplier()
    config_path = tmp_path / "market.json"
    with clients() as client:
        record = submit(client)
        review(client, record)
        app_id = record["applicationId"]
        config_path.write_text(
            json.dumps(
                {
                    "profile": "isolated-market",
                    "developer_token_env": {DEFAULT_USER.id: "TEST_JWT"},
                    "bindings": {
                        app_id: {
                            "owner_subject": DEFAULT_USER.id,
                            "organization_id": DEFAULT_USER.organization_id,
                            "resource_id": "resource-1",
                            "listing_id": "listing-1",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        settings = client.app.state.settings.model_copy(
            update={
                "applications_market": "himarket",
                "applications_market_url": "https://market.invalid",
                "applications_market_configuration": str(config_path),
            }
        )
        from pydantic import SecretStr

        settings.applications_market_admin_token = SecretStr("admin-test")
        settings.applications_market_developer_tokens = {"TEST_JWT": SecretStr("developer-test")}
        service = MarketWorkflows(
            client.app.state.application_service, settings, httpx.MockTransport(supplier)
        )

        def lost_reply(request):
            if request.method == "POST":
                supplier.listing.update(json.loads(request.content))
                raise httpx.ReadTimeout("lost reply", request=request)

        supplier.failure = lost_reply
        payload = MarketMutation(
            action="save_draft", requestId="database-draft-request", draft=draft()
        )
        operation = service.execute(app_id, DEFAULT_USER, payload)
        assert operation["status"] == "unknown"
    supplier.failure = None
    with clients() as client:
        service = MarketWorkflows(
            client.app.state.application_service, settings, httpx.MockTransport(supplier)
        )
        assert service.execute(app_id, DEFAULT_USER, payload)["status"] == "unknown"
        result = service.reconcile(app_id, operation["id"], DEFAULT_USER)
        assert result["status"] == "succeeded"
        assert service.execute(app_id, DEFAULT_USER, payload) == result
    assert sum(request.method == "POST" for request in supplier.calls) == 1
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT status, detail, request_data, result_data FROM scene_activity "
                    "WHERE provider_profile='isolated-market'"
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 1 and rows[0]["status"] == "succeeded"
        assert rows[0]["request_data"] is None and rows[0]["result_data"] is None
        assert "详细说明" not in json.dumps(rows[0]["detail"], ensure_ascii=False)
