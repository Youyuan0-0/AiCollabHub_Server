"""验证单服务器网关可以独立启用 CAS 回调，身份推送入口保持可选。"""

import sys

import pytest

from scripts import render_nginx


def render(tmp_path, monkeypatch, *options):
    """建立隔离的前端产物并读取生成的 Nginx 配置。"""
    user = tmp_path / "user"
    admin = tmp_path / "admin"
    user.mkdir()
    admin.mkdir()
    (user / "index.html").write_text("user", encoding="utf-8")
    (admin / "index.html").write_text("admin", encoding="utf-8")
    root = tmp_path / "nginx"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_nginx",
            "--nginx-root",
            str(root),
            "--user-dist",
            str(user),
            "--admin-dist",
            str(admin),
            *options,
        ],
    )
    render_nginx.main()
    return (root / "conf" / "nginx.conf").read_text(encoding="utf-8")


def test_iam_ingress_is_disabled_unless_ports_are_given(tmp_path, monkeypatch):
    """开发网关默认不向外提供集团 IAM 专用入口。"""
    value = render(tmp_path, monkeypatch)
    assert "listen 127.0.0.1:18880;" in value
    assert "listen 127.0.0.1:8085;" not in value
    assert "listen 127.0.0.1:8086;" not in value
    assert "@@IAM_INGRESS_SERVERS@@" not in value


def test_iam_ingress_only_proxies_exact_callback_and_sync(tmp_path, monkeypatch):
    """8086 与 8085 分开监听；网站不转发身份推送，集团不开放登出通知。"""
    value = render(
        tmp_path,
        monkeypatch,
        "--listen-host",
        "0.0.0.0",
        "--iam-callback-port",
        "8086",
        "--iam-sync-port",
        "8085",
    )
    assert "listen 0.0.0.0:8086;" in value
    assert "listen 0.0.0.0:8085;" in value
    assert "location = /api/v1/auth/callback" in value
    assert "location = /api/v1/auth/single-logout" not in value
    assert "location = /api/v1/auth/iam/identity-sync" in value
    assert "location ^~ /api/v1/auth/iam/ { return 404; }" in value
    assert value.count("location / { return 404; }") == 2
    assert "@@" not in value


def test_sso_only_exposes_callback_without_sync(tmp_path, monkeypatch):
    """本期只打开 8086，不监听 8085 或代理身份同步。"""
    value = render(tmp_path, monkeypatch, "--iam-callback-port", "8086")
    assert "listen 127.0.0.1:8086;" in value
    assert "listen 127.0.0.1:8085;" not in value
    assert "location = /api/v1/auth/iam/identity-sync" not in value
    assert "@@" not in value


@pytest.mark.parametrize("ports", [(None, "8085"), ("8086", "8086"), ("18880", "8085")])
def test_iam_ingress_requires_distinct_ports_and_callback(tmp_path, monkeypatch, ports):
    """不生成只有同步入口或端口冲突的配置。"""
    options = ["--iam-callback-port", ports[0]] if ports[0] else []
    if ports[1]:
        options.extend(("--iam-sync-port", ports[1]))
    with pytest.raises(SystemExit):
        render(tmp_path, monkeypatch, *options)
