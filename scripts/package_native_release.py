"""复用已验证的原生运行时和 Linux 依赖，为最新项目代码生成离线 ZIP。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy/native"
SCRIPTS = (
    "install.sh",
    "update.sh",
    "installer.py",
    "provision.py",
    "manage.py",
    "check.py",
    "diagnose.sh",
    "nginx.conf.template",
    "proxy.conf",
    "site.example.json",
    "auth.example.json",
    "auth.gateway.example.json",
    "gateway.example.json",
    "native_config.py",
)
VEGETA_VERSION = "12.13.0"
VEGETA_ARCHIVE_SHA256 = "e8759ce45c14e18374bdccd3ba6068197bc3a9f9b7e484db3837f701b9d12e61"


def sha256(path: Path) -> str:
    """按流读取大包，计算完整性摘要。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_bundle(bundle: Path) -> None:
    """复用前验证旧包每一项校验和，拒绝路径越界或损坏输入。"""
    for line in (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        path = (bundle / name).resolve()
        if not path.is_relative_to(bundle) or sha256(path) != digest:
            raise RuntimeError(f"旧包校验失败：{name}")


def schema_revisions(backend: Path) -> tuple[str, set[str]]:
    """静态解析资源基线迁移图，要求只有一个 head，不连接任何数据库。"""
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in (backend / "migrations/resource_foundation/versions").glob("*.py"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign):
                names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                if "revision" in names:
                    revisions.add(ast.literal_eval(node.value))
                if "down_revision" in names:
                    value = ast.literal_eval(node.value)
                    if value:
                        parents.update([value] if isinstance(value, str) else value)
    heads = revisions - parents
    if len(heads) != 1 or not parents <= revisions:
        raise RuntimeError("资源基线迁移链不完整或存在多个 head")
    return heads.pop(), revisions


def source_state(repository: Path) -> dict:
    """记录实际源码提交和工作区状态，便于回溯本次打包输入。"""

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=repository).decode().strip()

    return {"commit": git("rev-parse", "HEAD"), "workingTreeStatus": git("status", "--short")}


def copy_backend(target: Path) -> None:
    """仅打包应用和迁移文件；操作文档独立交付，保留 wheel 构建所需的简短简介。"""
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    for name in names:
        if name.endswith(".md"):
            continue
        if name.startswith(("app/", "migrations/resource_foundation/")) or name in {
            "pyproject.toml",
            "alembic.ini",
            "scripts/verify_services.py",
            "scripts/set_user_role.py",
            "scripts/__init__.py",
        }:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
    (target / "README.md").write_text(
        "# AiCollabHub Server\n\nAiCollabHub backend runtime package.\n",
        encoding="utf-8",
        newline="\n",
    )


def build_frontend(repository: Path, *, admin: bool) -> None:
    """构建同源接口的静态网站；只在准备机运行，目标机无需 Node.js。"""
    env = {
        **os.environ,
        "VITE_API_MODE": "http",
        "VITE_API_SCOPE": "all",
        "VITE_API_BASE_URL": "/api/v1",
        "VITE_BASE": "/admin/" if admin else "/",
    }
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise RuntimeError("准备机缺少 npm，无法构建前端")
    subprocess.run([npm, "run", "build"], cwd=repository, env=env, check=True)


def copy_loadtest(bundle: Path, archive_path: Path) -> dict:
    """校验并随包提供官方静态 Linux 工具、许可及已受版本管理的压测脚本。"""
    if sha256(archive_path) != VEGETA_ARCHIVE_SHA256:
        raise RuntimeError("Vegeta 官方 Linux amd64 包 SHA256 不符，停止打包")
    target = bundle / "loadtest"
    target.mkdir()
    for name in ("loadtest.py", "monitor.py"):
        (target / name).write_text(
            (DEPLOY / "loadtest" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
            newline="\n",
        )
    with tarfile.open(archive_path) as archive:
        member = archive.getmember("vegeta")
        if not member.isfile():
            raise RuntimeError("Vegeta 压缩包缺少普通二进制文件")
        with archive.extractfile(member) as source, (target / "vegeta").open("wb") as output:
            shutil.copyfileobj(source, output)
    (target / "vegeta").chmod(0o755)
    for source, name in (
        ("LICENSE-vegeta", "LICENSE"),
        ("vegeta_12.13.0_checksums.txt", "upstream-checksums.txt"),
    ):
        shutil.copyfile(archive_path.parent / source, target / name)
    metadata = {
        "version": VEGETA_VERSION,
        "platform": "linux/amd64",
        "source": "https://github.com/tsenart/vegeta/releases/tag/v12.13.0",
        "archive_sha256": VEGETA_ARCHIVE_SHA256,
        "binary_sha256": sha256(target / "vegeta"),
        "license": "MIT",
    }
    (target / "version.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return metadata


def build(args: argparse.Namespace) -> Path:
    """生成自包含的 ZIP、文件校验表及可追溯版本清单，不覆盖旧交付。"""
    base = args.reuse_bundle.resolve()
    output = args.output.resolve()
    verify_bundle(base)
    old_manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    if args.release_id == old_manifest["release_id"]:
        raise RuntimeError("代码更新必须使用新的 release_id，不能替换已发布版本")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError("交付目录非空；请使用新的目录以保留已有交付和验收证据")
    head, revisions = schema_revisions(ROOT)
    repositories = {
        "server": ROOT,
        "user": args.user_repo.resolve(),
        "admin": args.admin_repo.resolve(),
    }
    sources = {name: source_state(path) for name, path in repositories.items()}
    for name in ("user", "admin"):
        if not args.frontend_built:
            build_frontend(repositories[name], admin=name == "admin")
        if not (repositories[name] / "dist/index.html").exists():
            raise RuntimeError(f"{name} 缺少前端构建产物")
    output.mkdir(parents=True, exist_ok=True)
    package_name = "AiCollabHub-KylinV10SP3-x86_64-" + args.release_id
    bundle = output / package_name
    bundle.mkdir()
    for name in ("runtime.tar.gz",):
        shutil.copyfile(base / name, bundle / name)
    for name in ("wheelhouse", "sources", "notices"):
        shutil.copytree(base / name, bundle / name)
    # 新增的浏览器兼容库随静态文件分发，保留其版权及许可文本。
    core_js = repositories["user"] / "node_modules/core-js"
    core_js_version = json.loads((core_js / "package.json").read_text())["version"]
    shutil.copyfile(
        core_js / "LICENSE", bundle / "notices" / f"core-js-{core_js_version}-LICENSE.txt"
    )
    for path in (bundle / "wheelhouse").glob("aicollabhub_server-*.whl"):
        path.unlink()
    for name in SCRIPTS:
        # 跨 Windows / Linux 打包时确保 shell 脚本和校验表使用 LF。
        (bundle / name).write_text(
            (DEPLOY / name).read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
        )
    shutil.copytree(
        DEPLOY / "diagnostics",
        bundle / "diagnostics",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copyfile(DEPLOY / "native_config.py", bundle / "diagnostics/native_config.py")
    loadtest = None
    if not args.without_loadtest:
        loadtest = copy_loadtest(bundle, args.vegeta_archive.resolve())
    for name in ("iam-callback.conf.template", "iam-sync.conf.template"):
        (bundle / name).write_text(
            (ROOT / "deploy/nginx" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
            newline="\n",
        )
    for name in ("user", "admin"):
        shutil.copytree(repositories[name] / "dist", bundle / "frontend" / name)
    with tempfile.TemporaryDirectory(prefix="aih-native-package-") as temporary:
        backend = Path(temporary) / "backend"
        copy_backend(backend)
        current_project = tomllib.loads((backend / "pyproject.toml").read_text(encoding="utf-8"))
        with tarfile.open(base / "backend.tar.gz") as archive:
            previous_project = tomllib.loads(
                archive.extractfile("backend/pyproject.toml").read().decode("utf-8")
            )
            archive.extractall(Path(temporary) / "previous", filter="data")
        if (
            current_project["project"]["dependencies"]
            != previous_project["project"]["dependencies"]
        ):
            raise RuntimeError("Python 依赖声明变化，需先在麒麟构建机更新 Linux wheelhouse")
        previous_head, _ = schema_revisions(Path(temporary) / "previous/backend")
        if previous_head not in revisions:
            raise RuntimeError("新迁移链不包含旧版基线，不能生成原地升级包")
        with tarfile.open(bundle / "backend.tar.gz", "w:gz") as archive:
            archive.add(backend, arcname="backend")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "hatchling",
                "build",
                "-t",
                "wheel",
                "-d",
                str(bundle / "wheelhouse"),
            ],
            cwd=backend,
            check=True,
        )
    manifest = {
        **old_manifest,
        "release_id": args.release_id,
        "built_at": datetime.now(UTC).isoformat(),
        "sources": sources,
        "database_environment": "test",
        "authentication": "Default group IAM; switch old mock; preserve IAM settings",
        "loadtest": (
            {"vegeta": loadtest, "entrypoint": "/opt/aicollabhub/loadtest.sh"} if loadtest else None
        ),
        "instructions_included": False,
        "diagnostics": {
            "entrypoint": "/opt/aicollabhub/diagnose.sh",
            "browser": "/diagnostics/auth.html",
            "schema": "aih-diagnostics-v1",
        },
        "schema_head": head,
        "schema_chain": "resource_foundation",
        "upgrade_from": {
            **{
                release: value
                for release, value in old_manifest.get("upgrade_from", {}).items()
                if value["schema_head"] in revisions
            },
            old_manifest["release_id"]: {"schema_head": previous_head},
        },
        "runtime_sha256": sha256(bundle / "runtime.tar.gz"),
        "database_policy": "Fresh: aicollabhub_test; upgrade: preserve installed database and data",
        "frontend_configuration": {
            "api_mode": "http",
            "api_base": "/api/v1",
            "admin_base": "/admin/",
            "browser_target": ["chrome90", "edge90"],
            "array_at_polyfill": "core-js " + core_js_version,
        },
        "wheels": sorted(path.name for path in (bundle / "wheelhouse").glob("*.whl")),
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    checksums = "".join(
        f"{sha256(path)}  {path.relative_to(bundle).as_posix()}\n"
        for path in sorted(bundle.rglob("*"))
        if path.is_file()
    )
    (bundle / "SHA256SUMS").write_text(checksums, encoding="utf-8", newline="\n")
    verify_bundle(bundle)
    archive_path = output / (package_name + ".zip")
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(
                    path, arcname=package_name + "/" + path.relative_to(bundle).as_posix()
                )
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip():
            raise RuntimeError("ZIP CRC 校验失败")
    (output / (archive_path.name + ".sha256")).write_text(
        sha256(archive_path) + "  " + archive_path.name + "\n",
        encoding="utf-8",
        newline="\n",
    )
    shutil.copyfile(bundle / "manifest.json", output / "manifest.json")
    shutil.copyfile(DEPLOY / "README.md", output / "部署与更新说明.md")
    shutil.copyfile(DEPLOY / "DIAGNOSTICS.md", output / "认证诊断使用说明.md")
    return archive_path


def main() -> None:
    """要求显式指定已验收旧包、发布编号和独立输出目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--vegeta-archive", type=Path)
    parser.add_argument("--without-loadtest", action="store_true", help="不包含压测工具和脚本")
    parser.add_argument("--user-repo", type=Path, default=ROOT.parent / "AiCollabHub_User")
    parser.add_argument("--admin-repo", type=Path, default=ROOT.parent / "AiCollabHub_Admin")
    parser.add_argument(
        "--frontend-built", action="store_true", help="本次已手动构建两端时复用 dist"
    )
    args = parser.parse_args()
    if not args.without_loadtest and args.vegeta_archive is None:
        parser.error("包含压测工具时必须提供 --vegeta-archive；无需压测请使用 --without-loadtest")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.release_id):
        raise ValueError("release_id 只能包含字母、数字、短横线、下划线和点")
    print(build(args))


if __name__ == "__main__":
    main()
