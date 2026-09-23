"""验证单服务器网关只在显式配置时暴露集团 IAM 的两个入口。"""

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


def test_iam_ingress_is_disabled_unless_both_ports_are_given(tmp_path, monkeypatch):
    """开发网关默认不向外提供集团 IAM 专用入口。"""
    value = render(tmp_path, monkeypatch)
    assert "listen 127.0.0.1:18880;" in value
    assert "listen 127.0.0.1:8085;" not in value
    assert "listen 127.0.0.1:8086;" not in value
    assert "@@IAM_INGRESS_SERVERS@@" not in value


def test_iam_ingress_only_proxies_exact_callback_logout_and_sync(tmp_path, monkeypatch):
    """8086 与 8085 分开监听，其他路径明确返回 404。"""
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
    assert "location = /api/v1/auth/single-logout" in value
    assert "location = /api/v1/auth/iam/identity-sync" in value
    assert value.count("location / { return 404; }") == 2
    assert "@@" not in value


@pytest.mark.parametrize("ports", [("8086", None), ("8086", "8086"), ("18880", "8085")])
def test_iam_ingress_requires_both_distinct_ports(tmp_path, monkeypatch, ports):
    """不生成只有一个入口或与网站端口冲突的配置。"""
    options = ["--iam-callback-port", ports[0]]
    if ports[1]:
        options.extend(("--iam-sync-port", ports[1]))
    with pytest.raises(SystemExit):
        render(tmp_path, monkeypatch, *options)
