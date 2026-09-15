from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.modules.applications.application_service import ApplicationService
from app.modules.applications.memory_repository import InMemoryApplicationRepository
from app.modules.applications.providers import DemoApplicationProvider
from app.modules.applications.publication_schemas import (
    ReviewApplicationPublication,
    SaveApplicationPublication,
)


def pending(service):
    return service.save(
        SaveApplicationPublication.model_validate(
            {
                "data": {"name": "事务测试", "summary": "测试审批原子性", "owner": "测试单位"},
                "submit": True,
            }
        )
    )


def test_concurrent_reviews_commit_once():
    service = ApplicationService(InMemoryApplicationRepository(), DemoApplicationProvider())
    record = pending(service)

    def review(_):
        try:
            service.review(
                record["id"],
                ReviewApplicationPublication(
                    decision="approved",
                    scores={"completeness": 4, "usability": 4, "security": 4},
                    revision=record["revision"],
                ),
            )
            return 200
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(review, range(2))) == [200, 409]
    assert len(service.publication(record["id"])["reviews"]) == 1
    assert len(service.resource(record["applicationId"])["versions"]) == 1


def test_repository_failure_rolls_back_approval_and_catalog(monkeypatch):
    from app.modules.applications.memory_repository import MemorySession

    service = ApplicationService(InMemoryApplicationRepository(), DemoApplicationProvider())
    record = pending(service)

    def fail(*args):
        raise RuntimeError("write failed after resource update")

    monkeypatch.setattr(MemorySession, "put_publication", fail)
    with pytest.raises(RuntimeError):
        service.review(
            record["id"],
            ReviewApplicationPublication(
                decision="approved",
                scores={"completeness": 4, "usability": 4, "security": 4},
                revision=1,
            ),
        )
    assert service.publication(record["id"]) == record
    with pytest.raises(HTTPException) as error:
        service.resource(record["applicationId"])
    assert error.value.status_code == 404


def test_factory_injects_storage_and_provider_without_route_changes():
    class DelegatingRepository:
        def __init__(self):
            self.inner = InMemoryApplicationRepository()
            self.calls = 0

        @contextmanager
        def transaction(self):
            self.calls += 1
            with self.inner.transaction() as session:
                yield session

    class SuppliedProvider(DemoApplicationProvider):
        def dependencies(self):
            return [
                {
                    "kind": "model",
                    "resourceId": "external-model",
                    "name": "接入模型",
                    "owner": "供应方",
                    "version": "v1",
                }
            ]

        def runtime_status(self, application_id, version_id):
            return {
                "status": "available",
                "averageResponseMs": 250,
                "successRate": 0.95,
                "sampleCount": 100,
                "measuredAt": None,
            }

    repository = DelegatingRepository()
    factory = lambda: ApplicationService(repository, SuppliedProvider())  # noqa: E731
    with TestClient(create_app(Settings(), applications_factory=factory)) as client:
        assert (
            client.get("/api/v1/application-dependencies").json()[0]["resourceId"]
            == "external-model"
        )
        response = client.post(
            "/api/v1/application-publications",
            json={
                "data": {
                    "dependencies": [{"kind": "model", "resourceId": "external-model"}],
                }
            },
        )
        assert response.status_code == 201
        assert (
            client.get("/api/v1/admin/applications/app-qa").json()["runtime"]["sampleCount"] == 100
        )
    assert repository.calls >= 2
