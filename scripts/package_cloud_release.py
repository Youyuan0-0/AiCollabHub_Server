"""将三个项目的运行代码和已构建前端打包，排除凭据、依赖目录和运行数据。"""

import argparse
import json
import subprocess
import tarfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path


def add_path(archive: tarfile.TarFile, path: Path, target: str) -> None:
    """递归加入指定发布文件；忽略缓存、符号链接与本地环境配置。"""
    if (
        path.is_symlink()
        or path.name in {".runtime", "__pycache__", ".env"}
        or path.name.startswith(".env.")
    ):
        return
    if path.is_dir():
        for child in sorted(path.iterdir()):
            add_path(archive, child, f"{target}/{child.name}")
    elif path.is_file() and path.suffix != ".pyc":
        archive.add(path, arcname=target, recursive=False)


def revision(directory: Path) -> dict[str, object]:
    """记录源码提交和是否包含未提交修改，便于追溯实际打包来源。"""

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(directory), *args], text=True, encoding="utf-8"
        ).strip()

    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def main() -> None:
    """校验三端输入并生成含版本清单的 gzip 发布包。"""
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-root", type=Path, default=root.parent / "AiCollabHub_User")
    parser.add_argument("--admin-root", type=Path, default=root.parent / "AiCollabHub_Admin")
    parser.add_argument("--user-dist", type=Path)
    parser.add_argument("--admin-dist", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sources = {"backend": root, "user": args.user_root, "admin": args.admin_root}
    bundles = {
        "user": args.user_dist or args.user_root / "dist",
        "admin": args.admin_dist or args.admin_root / "dist",
    }
    for name, directory in bundles.items():
        if not (directory / "index.html").is_file():
            parser.error(f"{name} build is missing: {directory / 'index.html'}")
    manifest = {
        "createdAt": datetime.now(UTC).isoformat(),
        "sources": {name: revision(directory) for name, directory in sources.items()},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, "w:gz") as archive:
        for name in (
            "Dockerfile",
            ".dockerignore",
            "pyproject.toml",
            "requirements-dev.lock",
            "README.md",
            "alembic.ini",
            "app",
            "migrations",
            "scripts/verify_services.py",
        ):
            add_path(archive, root / name, f"backend/{name}")
        for path in sorted((root / "config").glob("*.example.json")):
            add_path(archive, path, f"backend/config/{path.name}")
        for name, directory in bundles.items():
            add_path(archive, directory, f"{name}/dist")
            for filename in ("Dockerfile", ".dockerignore", "deploy/nginx.conf"):
                add_path(archive, sources[name] / filename, f"{name}/{filename}")
        add_path(archive, root / "deploy/cloud/compose.yaml", "compose.yaml")
        content = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(content)
        archive.addfile(info, BytesIO(content))
    print(f"Release: {args.output.resolve()} ({args.output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
