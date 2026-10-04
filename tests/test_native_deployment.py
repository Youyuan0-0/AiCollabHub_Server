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
    monkeypatch.syspath_prepend(str(source.parent))
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


@pytest.fixture
def auth_deployment(installer, monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "CONFIG", tmp_path / "etc")
    installer.CONFIG.mkdir()
    config = {
        "public_origin": "http://10.0.0.10",
        "web_port": 80,
        "api_port": 18000,
        "iam_port": 18002,
        "postgres_port": 5432,
    }
    auth = {
        "AUTH_MODE": "iam",
        "AUTH_IDENTITY_SYNC_ENABLED": "false",
        "AUTH_PUBLIC_ORIGIN": config["public_origin"],
        "AUTH_CALLBACK_ORIGIN": "http://10.0.0.10:8086",
        "AUTH_IAM_ORIGIN": "http://iam.example.test",
        "AUTH_IAM_APP_ID": "796872537136820224",
        "AUTH_SESSION_SECRET": "s" * 40,
    }
    return config, auth


def test_update_preserves_installed_iam_config(installer, auth_deployment):
    config, auth = auth_deployment
    (installer.CONFIG / "app-env.json").write_text(json.dumps({**auth, "POSTGRES_DB": "old"}))
    actual = installer.load_auth_environment(config)
    assert all(actual[key] == value for key, value in auth.items())
    assert installer.authentication_ports(config, auth) == {"callback": 8086}


def test_fresh_install_defaults_to_group_iam_without_writing_secrets(installer, auth_deployment):
    config, _ = auth_deployment
    actual = installer.load_auth_environment(config)
    assert actual["AUTH_MODE"] == "iam"
    assert actual["AUTH_IAM_ORIGIN"] == "http://hfidaas-sso.aecc.cn"
    assert actual["AUTH_IAM_APP_ID"] == "796872537136820224"
    assert actual["AUTH_CALLBACK_ORIGIN"] == "http://10.0.0.10:8086"
    assert actual["AUTH_IDENTITY_SYNC_ENABLED"] == "false"
    assert len(actual["AUTH_SESSION_SECRET"]) >= 32
    assert not list(installer.CONFIG.iterdir())


def test_mock_upgrade_switches_to_iam_and_reuses_persisted_secret(installer, auth_deployment):
    config, _ = auth_deployment
    (installer.CONFIG / "app-env.json").write_text(
        json.dumps(
            {
                "AUTH_MODE": "mock",
                "AUTH_PUBLIC_ORIGIN": config["public_origin"],
                "AUTH_IDENTITY_SYNC_ENABLED": "false",
                "AUTH_MOCK_SECRET": "old-mock-secret",
            }
        )
    )
    actual = installer.load_auth_environment(config)
    assert actual["AUTH_MODE"] == "iam"
    assert actual["AUTH_SESSION_SECRET"] != "old-mock-secret"
    (installer.CONFIG / "app-env.json").write_text(json.dumps(actual))
    assert (
        installer.load_auth_environment(config)["AUTH_SESSION_SECRET"]
        == actual["AUTH_SESSION_SECRET"]
    )


def test_empty_auth_template_secret_is_generated(installer, auth_deployment):
    config, auth = auth_deployment
    source = installer.CONFIG / "auth.json"
    source.write_text(json.dumps({**auth, "AUTH_SESSION_SECRET": ""}))
    actual = installer.load_auth_environment(config, source)
    assert len(actual["AUTH_SESSION_SECRET"]) >= 32
    (installer.CONFIG / "app-env.json").write_text(json.dumps(actual))
    assert (
        installer.load_auth_environment(config, source)["AUTH_SESSION_SECRET"]
        == actual["AUTH_SESSION_SECRET"]
    )


def test_explicit_auth_file_controls_mode_without_database_override(installer, auth_deployment):
    config, auth = auth_deployment
    source = installer.CONFIG / "auth.json"
    source.write_text(json.dumps(auth))
    assert installer.load_auth_environment(config, source)["AUTH_MODE"] == "iam"
    source.write_text(json.dumps({**auth, "POSTGRES_DB": "other"}))
    with pytest.raises(ValueError, match="不能修改数据库"):
        installer.load_auth_environment(config, source)


@pytest.mark.parametrize(
    "key,value",
    [
        ("AUTH_CALLBACK_ORIGIN", "http://different.example.test:8086"),
        ("AUTH_CALLBACK_ORIGIN", "https://10.0.0.10:8086"),
        ("AUTH_CALLBACK_ORIGIN", "http://10.0.0.10:18000"),
        ("AUTH_CALLBACK_ORIGIN", "http://10.0.0.10:8086/api/callback"),
        ("AUTH_SESSION_SECRET", "CHANGE_ME"),
        ("AUTH_IDENTITY_SYNC_ENABLED", "true"),
    ],
)
def test_invalid_iam_configuration_rejected(installer, auth_deployment, key, value):
    config, auth = auth_deployment
    source = installer.CONFIG / "auth.json"
    source.write_text(json.dumps({**auth, key: value}))
    with pytest.raises(ValueError):
        installer.load_auth_environment(config, source)


def test_sync_listener_requires_explicit_sources(installer, auth_deployment):
    config, auth = auth_deployment
    auth.update(
        AUTH_IDENTITY_SYNC_ENABLED="true",
        AUTH_IAM_ADMIN_ROLE_CODE="platform_admin",
        AUTH_IAM_SYNC_ALLOWED_NETWORKS='["10.0.0.20/32"]',
    )
    assert installer.authentication_ports(config, auth) == {"callback": 8086, "sync": 8085}


def test_iam_nginx_separates_callback_and_sync_ingress(
    installer, auth_deployment, monkeypatch, tmp_path
):
    import shutil

    config, auth = auth_deployment
    project = Path(__file__).resolve().parents[1]
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in ("nginx.conf.template", "proxy.conf"):
        shutil.copyfile(project / "deploy/native" / name, bundle / name)
    for name in ("iam-callback.conf.template", "iam-sync.conf.template"):
        shutil.copyfile(project / "deploy/nginx" / name, bundle / name)
    monkeypatch.setattr(
        installer, "write", lambda path, content, *args: path.write_text(content, encoding="utf-8")
    )
    installer.configure_web(bundle, config, tmp_path / "release", tmp_path / "data", auth)
    nginx = (installer.CONFIG / "nginx.conf").read_text(encoding="utf-8")
    assert "listen 0.0.0.0:8086;" in nginx
    assert "8085" not in nginx
    assert "location /api/v1/auth/iam/ { return 404; }" in nginx
    assert "location /authn-api/ { return 404; }" in nginx
    assert "location = /api/v1/auth/callback" in nginx
    assert "@@" not in nginx
    auth.update(
        AUTH_IDENTITY_SYNC_ENABLED="true",
        AUTH_IAM_ADMIN_ROLE_CODE="platform_admin",
        AUTH_IAM_SYNC_ALLOWED_NETWORKS='["10.0.0.20/32"]',
    )
    installer.configure_web(bundle, config, tmp_path / "release", tmp_path / "data", auth)
    assert "listen 0.0.0.0:8085;" in (installer.CONFIG / "nginx.conf").read_text(encoding="utf-8")


def test_network_override_cannot_change_database(installer, deployment):
    state, installed, manifest, config = deepcopy(deployment)
    config.update(
        public_origin="https://portal.test", tls_termination="gateway", web_extra_ports=[8086]
    )
    assert (
        installer.validate_update(state, installed, manifest, config, network_override=True)
        == "old"
    )
    config["postgres_db"] = "different"
    with pytest.raises(RuntimeError, match="不同配置"):
        installer.validate_update(state, installed, manifest, config, network_override=True)


def test_effective_env_preserves_manual_iam_and_session_secret(installer, auth_deployment):
    config, auth = auth_deployment
    (installer.CONFIG / "app-env.json").write_text(json.dumps(auth))
    (installer.CONFIG / "app.env").write_text(
        '# site operations\nAUTH_SESSION_SECRET="' + "new-key-" * 8 + '"\n'
        "AUTH_IAM_TIMEOUT=10\nAUTH_CALLBACK_ORIGIN='http://10.0.0.10'\n"
    )
    actual = installer.load_auth_environment(config)
    assert actual["AUTH_SESSION_SECRET"] == "new-key-" * 8
    assert actual["AUTH_IAM_TIMEOUT"] == "10"
    assert actual["AUTH_CALLBACK_ORIGIN"] == config["public_origin"]
    assert installer.authentication_ports(config, actual) == {}


def test_gateway_full_site_uses_http_ports_and_public_https_callback(
    installer, auth_deployment, monkeypatch, tmp_path
):
    import shutil

    config, auth = auth_deployment
    config.update(
        public_origin="https://portal.test", tls_termination="gateway", web_extra_ports=[8086]
    )
    auth.update(
        AUTH_PUBLIC_ORIGIN=config["public_origin"],
        AUTH_CALLBACK_ORIGIN=config["public_origin"],
        AUTH_COOKIE_SECURE="true",
    )
    assert installer.authentication_ports(config, auth) == {}
    project = Path(__file__).resolve().parents[1]
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in ("nginx.conf.template", "proxy.conf"):
        shutil.copyfile(project / "deploy/native" / name, bundle / name)
    monkeypatch.setattr(installer, "write", lambda path, value, *args: path.write_text(value))
    installer.configure_web(bundle, config, tmp_path / "release", tmp_path / "data", auth)
    nginx = (installer.CONFIG / "nginx.conf").read_text()
    assert "listen 0.0.0.0:80;" in nginx
    assert "listen 0.0.0.0:8086;" in nginx
    assert "absolute_redirect off;" in nginx
    assert "location = /api/v1/auth/callback" not in nginx
    assert "ssl_certificate" not in nginx
    assert "@@" not in nginx
    auth["AUTH_CALLBACK_ORIGIN"] = "https://portal.test:8086"
    with pytest.raises(ValueError, match="对外端口"):
        installer.authentication_ports(config, auth)


def test_environment_reader_does_not_execute_shell(installer, tmp_path):
    marker = tmp_path / "never-created"
    (tmp_path / "app.env").write_text("AUTH_IAM_APP_ID=$(touch " + marker.as_posix() + ")\n")
    assert installer.effective_environment(tmp_path)["AUTH_IAM_APP_ID"].startswith("$(touch ")
    assert not marker.exists()


def test_gateway_site_validation_and_port_conflicts(installer, monkeypatch, tmp_path):
    source = Path(installer.__file__).with_name("site.example.json")
    site = json.loads(source.read_text())
    site.update(
        tls_termination="gateway", public_origin="https://portal.test", web_extra_ports=[8086]
    )
    path = tmp_path / "site.json"
    path.write_text(json.dumps(site))
    # 数据路径属于目标 Linux 主机，Windows 单元测试只替换该平台路径判断。
    monkeypatch.setattr(installer.Path, "resolve", lambda self: self)
    assert installer.validate_config(path)["web_port"] == 80
    for ports in ([80], [18000], [8086, 8086], [False], [65536]):
        path.write_text(json.dumps({**site, "web_extra_ports": ports}))
        with pytest.raises(ValueError):
            installer.validate_config(path)
