"""原生部署管理入口：控制服务、检查环境及生成停机一致性备份。"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

ROOT = Path("/opt/aicollabhub")
CONFIG = Path("/etc/aicollabhub")
SERVICES = ["aih-" + name for name in ("postgres", "redis", "minio", "iam", "api", "web")]


def backup(config: dict, *, keep_stopped: bool = False) -> Path:
    """停机保存数据、配置及版本指针；升级可保持停机，备份失败则恢复原服务。"""
    import hashlib
    import shutil

    data = Path(config["data_dir"])
    destination = data.parent / "aicollabhub-backups"
    destination.mkdir(mode=0o700, exist_ok=True)
    destination.chmod(0o700)
    size = sum(path.stat().st_size for path in data.rglob("*") if path.is_file())
    if shutil.disk_usage(destination).free < size + 1024**3:
        raise RuntimeError("备份目录可用空间不足；请先腾出数据大小加 1GiB 的空间")
    running = [
        service
        for service in SERVICES
        if subprocess.run(["systemctl", "is-active", "--quiet", service], check=False).returncode
        == 0
    ]
    name = "aicollabhub-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".tar.gz"
    target = destination / name
    if target.exists():
        raise RuntimeError("同名备份已存在")
    print("正在生成停机一致性备份，包含数据库、文件、缓存、凭据与部署配置。", flush=True)
    completed = False
    try:
        subprocess.run(["systemctl", "stop", *reversed(SERVICES)], check=True)
        # 自动升级调用时安装器使用 022，敏感备份必须从创建瞬间就限制访问。
        descriptor = os.open(str(target) + ".partial", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with tarfile.open(str(target) + ".partial", "w:gz") as archive:
            for directory in (data, CONFIG):
                archive.add(directory, arcname=str(directory).lstrip("/"))
            archive.add(
                ROOT / "release-manifest.json", arcname="opt/aicollabhub/release-manifest.json"
            )
            for name in ("current", "manage.sh", "manage.py", "check.py"):
                archive.add(ROOT / name, arcname="opt/aicollabhub/" + name, recursive=False)
            logrotate = Path("/etc/logrotate.d/aih-native")
            if logrotate.exists():
                archive.add(logrotate, arcname=str(logrotate).lstrip("/"))
            for service in SERVICES:
                path = Path("/etc/systemd/system") / (service + ".service")
                archive.add(path, arcname=str(path).lstrip("/"))
        partial = Path(str(target) + ".partial")
        partial.chmod(0o600)
        partial.replace(target)
        with target.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        checksum = target.with_suffix(target.suffix + ".sha256")
        checksum.write_text(digest + "  " + target.name + "\n")
        checksum.chmod(0o600)
        completed = True
        print(f"备份完成：{target}；请同时保留相同版本安装包。")
        return target
    finally:
        if running and (not completed or not keep_stopped):
            subprocess.run(["systemctl", "start", *running], check=True)


def main() -> None:
    """使用明确子命令操作本套服务，不影响系统已有的其他服务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "restart", "status", "check", "backup"])
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("请使用 sudo /opt/aicollabhub/manage.sh " + args.action)
    os.umask(0o077)
    config = json.loads((CONFIG / "site.json").read_text())
    if args.action == "check":
        os.execv(sys.executable, [sys.executable, str(ROOT / "check.py")])
    if args.action == "status":
        subprocess.run(["systemctl", "--no-pager", "--full", "status", *SERVICES], check=True)
        return
    with Path("/run/lock/aih-native-install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "backup":
            backup(config)
        elif args.action in ("stop", "restart"):
            subprocess.run(["systemctl", "stop", *reversed(SERVICES)], check=True)
            if args.action == "restart":
                subprocess.run(["systemctl", "start", *SERVICES], check=True)
        else:
            subprocess.run(["systemctl", "start", *SERVICES], check=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"操作未完成：{error}", file=sys.stderr)
        sys.exit(1)
