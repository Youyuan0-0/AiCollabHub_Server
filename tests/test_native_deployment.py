"""验证离线更新的配置兼容规则，避免升级时意外改库或接受不兼容版本。"""

import importlib.util
import json
import sys
import types
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def installer(monkeypatch):
    if sys.platform == "win32":
        for name in ("fcntl", "pwd"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    source = Path(__file__).resolve().parents[1] / "deploy/native/installer.py"
    spec = importlib.util.spec_from_file_location("native_installer_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def deployment(installer):
    legacy = {"profile": "rehearsal", "data_dir": "/data/aicollabhub", "web_port": 80}
    state = {"release_id": "r1", "config": legacy, "status": "installed"}
    installed = {
        "release_id": "r1",
        "versions": {"postgresql": "17.11"},
        "deployment": "native-systemd",
        "profile": "rehearsal",
    }
    manifest = {
        "release_id": "r2",
        "versions": {"postgresql": "17.11"},
        "schema_head": "new",
        "upgrade_from": {"r1": {"schema_head": "old"}},
    }
    return state, installed, manifest, installer.normalize_config(legacy)


def test_legacy_database_is_preserved(installer, deployment):
    assert deployment[3]["postgres_db"] == "aicollabhub"
    assert deployment[3]["database_environment"] == "test"
    assert installer.validate_update(*deployment) == "old"


@pytest.mark.parametrize(
    "key,value",
    [
        ("postgres_db", "aicollabhub_test"),
        ("database_environment", "production"),
        ("web_port", 8080),
        ("data_dir", "/other/aicollabhub"),
    ],
)
def test_update_rejects_changed_target(installer, deployment, key, value):
    state, installed, manifest, config = deepcopy(deployment)
    config[key] = value
    with pytest.raises(RuntimeError, match="不同配置"):
        installer.validate_update(state, installed, manifest, config)


@pytest.mark.parametrize("status", ["upgrading", "upgrade_failed", "installing"])
def test_incomplete_update_requires_recovery(installer, deployment, status):
    state, installed, manifest, config = deepcopy(deployment)
    state["status"] = status
    with pytest.raises(RuntimeError, match="未完成"):
        installer.validate_update(state, installed, manifest, config)


def test_unknown_version_and_runtime_change_rejected(installer, deployment):
    state, installed, manifest, config = deepcopy(deployment)
    manifest["upgrade_from"] = {}
    with pytest.raises(RuntimeError, match="升级来源"):
        installer.validate_update(state, installed, manifest, config)
    manifest["versions"]["postgresql"] = "18.0"
    with pytest.raises(RuntimeError, match="运行环境版本"):
        installer.validate_update(state, installed, manifest, config)


def test_repeated_update_uses_current_schema(installer, deployment):
    state, installed, manifest, config = deepcopy(deployment)
    state["release_id"] = installed["release_id"] = manifest["release_id"]
    assert installer.validate_update(state, installed, manifest, config) == "new"


def test_explicit_database_name_is_respected(installer):
    assert installer.normalize_config({"postgres_db": "aicollabhub_test"})["postgres_db"] == (
        "aicollabhub_test"
    )


def test_failed_backup_never_updates_payload(installer, monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"release_id": "r1", "status": "installed"}))
    before = state.read_bytes()
    monkeypatch.setattr(installer, "STATE", state)
    monkeypatch.setattr(installer, "preflight", lambda *args, **kwargs: True)
    events = []

    def failed_backup(*args, **kwargs):
        events.append("backup")
        raise RuntimeError("disk full")

    monkeypatch.setitem(sys.modules, "manage", types.SimpleNamespace(backup=failed_backup))
    monkeypatch.setattr(installer, "install_payload", lambda *args: events.append("payload"))
    with pytest.raises(RuntimeError, match="disk full"):
        installer.install(
            types.SimpleNamespace(check=False, upgrade=True), {}, {"release_id": "r2"}
        )
    assert events == ["backup"]
    assert state.read_bytes() == before


def test_failed_update_records_backup_and_stops_services(installer, monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"release_id": "r1", "status": "installed"}))
    monkeypatch.setattr(installer, "STATE", state)
    monkeypatch.setattr(installer, "preflight", lambda *args, **kwargs: True)
    events = []

    def successful_backup(config, *, keep_stopped):
        assert keep_stopped is True
        events.append("backup")
        return tmp_path / "backup.tar.gz"

    def failed_payload(*args):
        events.append("payload")
        raise RuntimeError("migration failed")

    monkeypatch.setitem(sys.modules, "manage", types.SimpleNamespace(backup=successful_backup))
    monkeypatch.setattr(installer, "install_payload", failed_payload)
    monkeypatch.setattr(
        installer.subprocess, "run", lambda command, **kwargs: events.append(command)
    )
    monkeypatch.setattr(installer, "write", lambda path, content: path.write_text(content))
    with pytest.raises(RuntimeError, match="migration failed"):
        installer.install(
            types.SimpleNamespace(check=False, upgrade=True), {}, {"release_id": "r2"}
        )
    assert events[:2] == ["backup", "payload"]
    assert events[2] == [
        "systemctl",
        "stop",
        "aih-web",
        "aih-api",
        "aih-iam",
        "aih-minio",
        "aih-redis",
        "aih-postgres",
    ]
    saved = json.loads(state.read_text())
    assert saved["status"] == "upgrade_failed"
    assert saved["previous_release_id"] == "r1"
    assert saved["backup"] == str(tmp_path / "backup.tar.gz")


def test_check_only_does_not_backup_or_install(installer, monkeypatch):
    monkeypatch.setattr(installer, "preflight", lambda *args, **kwargs: True)
    monkeypatch.setattr(installer, "install_payload", lambda *args: pytest.fail("unexpected write"))
    monkeypatch.setitem(
        sys.modules,
        "manage",
        types.SimpleNamespace(
            backup=lambda *args, **kwargs: pytest.fail("unexpected backup"),
        ),
    )
    installer.install(types.SimpleNamespace(check=True, upgrade=True), {}, {"release_id": "r2"})
