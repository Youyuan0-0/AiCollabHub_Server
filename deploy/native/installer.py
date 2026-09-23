"""在麒麟服务器离线安装独立运行环境，保留已初始化的数据和凭据。"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

ROOT = Path("/opt/aicollabhub")
CONFIG = Path("/etc/aicollabhub")
RUNTIME = ROOT / "runtime"
STATE = CONFIG / "install-state.json"
SERVICES = ["postgres", "redis", "minio", "iam", "api", "web"]
ACCOUNTS = ["aih-db", "aih-redis", "aih-minio", "aih-app", "aih-web"]
PASSWORDS: dict[str, str] = {}
LOG = Path("/var/log/aih-native-install.log")


def write(path: Path, content: str, mode: int = 0o600, group: str | None = None) -> None:
    """原子写入受限权限文件，避免半写配置及凭据临时暴露。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(content)
    if group:
        shutil.chown(temporary, user="root", group=group)
    temporary.replace(path)


def run(
    command: list[str],
    *,
    label: str,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    input_text: str | None = None,
) -> str:
    """执行本地命令；完整结果写入脱敏日志，失败时保留真实退出状态。"""
    print(label, flush=True)
    result = subprocess.run(
        command,
        env=env,
        cwd=cwd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = result.stdout
    for value in PASSWORDS.values():
        output = output.replace(value, "[REDACTED]")
    with LOG.open("a", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(f"\n[{label}] exit={result.returncode}\n{output}\n")
    if result.returncode:
        raise RuntimeError(f"{label}失败，详细信息见 {LOG}")
    return output


def normalize_config(config: dict) -> dict:
    """旧配置沿用原库名，仅补充测试用途；不把已有数据切换到一个新空库。"""
    return {"database_environment": "test", "postgres_db": "aicollabhub", **config}


def validate_config(path: Path) -> dict:
    """限制输入为明确的内网地址、端口与专属数据目录，不执行配置中的代码。"""
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    template = json.loads((Path(__file__).parent / "site.example.json").read_text())
    if set(config) - set(template):
        raise ValueError("配置含有未知字段：" + ", ".join(sorted(set(config) - set(template))))
    config = {**template, **normalize_config(config)}
    if config["profile"] != "rehearsal":
        raise ValueError("当前项目仅提供模拟认证，本版本只支持 rehearsal 联调配置")
    if config["database_environment"] != "test":
        raise ValueError("此包仅用于测试数据库，正式环境需独立配置并验证真实 IAM")
    if not isinstance(config["postgres_db"], str) or not re.fullmatch(
        r"[a-z][a-z0-9_]{0,62}", config["postgres_db"]
    ):
        raise ValueError("postgres_db 仅允许小写字母、数字、下划线，以字母开头，最多 63 字符")
    origin = config["public_origin"]
    if not isinstance(origin, str) or not re.fullmatch(r"https?://[A-Za-z0-9.:-]+", origin):
        raise ValueError("public_origin 需填写 http(s)://实际服务器IP或域名[:端口]")
    parsed = urlsplit(origin)
    if not parsed.hostname or "CHANGE_ME" in origin or parsed.hostname in {"0.0.0.0", "localhost"}:
        raise ValueError("请先填写客户端实际访问的服务器 IP 或域名")
    ports = [key for key in template if key.endswith("_port")]
    for key in ports:
        if type(config[key]) is not int or not 1 <= config[key] <= 65535:
            raise ValueError(f"{key} 必须为 1—65535 的整数")
    if len({config[key] for key in ports}) != len(ports):
        raise ValueError("各服务端口不能重复")
    if (parsed.port or (443 if parsed.scheme == "https" else 80)) != config["web_port"]:
        raise ValueError("public_origin 的端口需与 web_port 一致")
    if type(config["api_workers"]) is not int or not 1 <= config["api_workers"] <= 4:
        raise ValueError("api_workers 必须为 1—4，当前数据库限制为 60 个连接")
    if type(config["open_firewall"]) is not bool:
        raise ValueError("open_firewall 必须为 true 或 false")
    data = str(config["data_dir"])
    if not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)+aicollabhub", data):
        raise ValueError("data_dir 需为 /data/aicollabhub 等专属绝对路径，不能含空格")
    if Path(data).resolve() != Path(data) or Path(data).is_relative_to(ROOT):
        raise ValueError("data_dir 不得为符号链接或位于程序目录内")
    for name in ("tls_certificate", "tls_private_key"):
        if parsed.scheme == "https":
            value = Path(config[name])
            if not value.is_absolute() or not value.is_file():
                raise ValueError(f"HTTPS 必须提供 {name} 的绝对文件路径")
    return config


def validate_update(state: dict, installed: dict, manifest: dict, config: dict) -> str:
    """只接受已完成的兼容部署，禁止更新时改库、变更运行时或绕过版本链。"""
    if state.get("status") != "installed":
        raise RuntimeError("上次部署未完成，请根据安装状态中的 backup 恢复后再更新")
    if normalize_config(state["config"]) != config:
        raise RuntimeError("检测到不同配置；更新不能更换数据库、地址、端口或数据目录")
    if installed.get("release_id") != state["release_id"]:
        raise RuntimeError("安装状态与已部署版本不一致，停止更新")
    if installed.get("versions") != manifest["versions"]:
        raise RuntimeError("运行环境版本有变化，不能使用本次代码更新流程")
    if installed.get("deployment") != "native-systemd" or installed.get("profile") != "rehearsal":
        raise RuntimeError("只允许更新本套原生测试环境")
    if state["release_id"] == manifest["release_id"]:
        return manifest["schema_head"]
    previous = manifest.get("upgrade_from", {}).get(state["release_id"])
    if not previous:
        raise RuntimeError("此安装版本不在本包支持的升级来源中，拒绝覆盖或降级")
    return previous["schema_head"]


def inspect_installed_schema(config: dict, expected: str) -> None:
    """通过现有只读迁移表权限确认实际数据库与配置、受支持基线一致。"""
    program = """
import json
from pathlib import Path
import psycopg
env = json.loads(Path('/etc/aicollabhub/app-env.json').read_text())
with psycopg.connect(host=env['POSTGRES_HOST'], port=env['POSTGRES_PORT'],
                    dbname=env['POSTGRES_DB'], user=env['POSTGRES_USER'],
                    password=env['POSTGRES_PASSWORD'], connect_timeout=5) as connection:
    versions = [row[0] for row in connection.execute('SELECT version_num FROM alembic_version')]
print(json.dumps({'environment': env['APP_ENV'], 'database': env['POSTGRES_DB'],
                  'host': env['POSTGRES_HOST'], 'versions': versions}))
"""
    result = subprocess.run(
        [str(ROOT / "current/venv/bin/python"), "-c", program],
        env={**os.environ, "LD_LIBRARY_PATH": f"{RUNTIME}/lib:{RUNTIME}/postgres/lib"},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("无法读取已安装测试库的迁移版本；请先检查原服务，不执行覆盖")
    actual = json.loads(result.stdout)
    if actual != {
        "environment": "test",
        "database": config["postgres_db"],
        "host": "127.0.0.1",
        "versions": [expected],
    }:
        raise RuntimeError("实际数据库与预期测试库或迁移版本不一致，停止更新")


def preflight(config: dict, manifest: dict, *, upgrade: bool = False) -> bool:
    """安装前检查系统、目录、容量和冲突；已管理的同版安装允许安全重试。"""
    if os.geteuid() != 0 or platform.machine() != "x86_64":
        raise RuntimeError("需以 root 身份在 x86_64 麒麟上安装")
    release = Path("/etc/kylin-release").read_text()
    if "V10" not in release or platform.libc_ver()[0] != "glibc":
        raise RuntimeError("仅支持麒麟 V10 SP3 / glibc 系统")
    system_info = Path("/etc/.kyinfo")
    if not system_info.exists() or "Kylin-Server-V10-SP3-2403" not in system_info.read_text():
        raise RuntimeError("此包已针对麒麟 V10 SP3 2403 构建；其他版本需重新做兼容验证")
    libc = tuple(int(value) for value in platform.libc_ver()[1].split("."))
    if libc < (2, 28):
        raise RuntimeError("需要 glibc >= 2.28；安装程序不会替换系统 glibc")
    if not Path("/run/systemd/system").exists():
        raise RuntimeError("需由 systemd 管理的麒麟服务器")
    for command in ("systemctl", "useradd", "runuser", "tar", "ldd", "getconf"):
        if not shutil.which(command):
            raise RuntimeError(f"操作系统缺少基础工具 {command}")
    managed = STATE.exists()
    if upgrade and not managed:
        raise RuntimeError("未发现已安装环境；全新安装请使用 install.sh")
    if managed:
        state = json.loads(STATE.read_text())
        if upgrade:
            installed = json.loads((ROOT / "release-manifest.json").read_text())
            expected = validate_update(state, installed, manifest, config)
            inspect_installed_schema(config, expected)
        elif (
            state["release_id"] != manifest["release_id"]
            or normalize_config(state["config"]) != config
        ):
            raise RuntimeError("检测到另一版本或不同配置；已有环境请使用 update.sh，不覆盖已有部署")
        elif state.get("status") not in {"installed", "installing"}:
            raise RuntimeError("上次更新未完成，请根据安装状态中的 backup 恢复后再操作")
        if not (CONFIG / "credentials.json").is_file():
            raise RuntimeError("已有安装缺少凭据文件；请恢复备份，不能重新生成密码")
    else:
        for directory in (ROOT, CONFIG, Path(config["data_dir"])):
            if directory.exists() and any(directory.iterdir()):
                raise RuntimeError(f"目录 {directory} 已含有其他内容，停止以免覆盖")
        for account in ACCOUNTS:
            try:
                pwd.getpwnam(account)
            except KeyError:
                pass
            else:
                raise RuntimeError(f"系统账号 {account} 已存在，需确认归属")
        for service in SERVICES:
            unit = "aih-" + service + ".service"
            if subprocess.run(["systemctl", "cat", unit], capture_output=True).returncode == 0:
                raise RuntimeError(f"系统服务 {unit} 已存在，需确认归属")
        for key, port in config.items():
            if key.endswith("_port"):
                with socket.socket() as probe:
                    try:
                        probe.bind(("0.0.0.0", port))
                    except OSError as error:
                        raise RuntimeError(f"{key}={port} 已被占用") from error
    for directory, gib in ((ROOT.parent, 3), (Path(config["data_dir"]), 10)):
        while not directory.exists():
            directory = directory.parent
        if shutil.disk_usage(directory).free < gib * 1024**3:
            raise RuntimeError(f"{directory} 可用空间不足 {gib} GiB")
    for directory in (ROOT, CONFIG, Path(config["data_dir"])):
        if directory.resolve() != directory:
            raise RuntimeError(f"拒绝使用符号链接目录：{directory}")
    print(f"预检通过：{release.strip()}，glibc {platform.libc_ver()[1]}")
    print(f"数据库用途：test；本机数据库：{config['postgres_db']}（不连接云端数据库）")
    print("此包用于原生部署演练/现场联调，真实 IAM 和模型服务需另行接入。")
    return managed


def create_credentials() -> dict[str, str]:
    """仅在首次安装创建密码；重试始终复用原有凭据。"""
    path = CONFIG / "credentials.json"
    if path.exists():
        return json.loads(path.read_text())
    values = {
        name: secrets.token_urlsafe(36)
        for name in (
            "postgres_admin_password",
            "postgres_owner_password",
            "postgres_app_password",
            "redis_password",
            "minio_root_password",
            "minio_app_password",
            "auth_mock_secret",
        )
    }
    values.update(minio_root_user="aih-storage-admin", minio_app_user="aih-platform")
    write(path, json.dumps(values, indent=2) + "\n")
    return values


def owned_directory(path: Path, account: str, mode: int = 0o700) -> None:
    """创建专属服务目录；不递归改动其他目录的权限。"""
    path.mkdir(parents=True, exist_ok=True)
    shutil.chown(path, user=account, group=account)
    path.chmod(mode)


def service_unit(
    name: str,
    account: str,
    start: str,
    *,
    data: Path,
    working: Path | None = None,
    extra: str = "",
    dependencies: str = "",
    env_file: str = "",
) -> None:
    """生成前台进程 systemd 服务，限定可写目录并使用独立账号。"""
    runtime_env = f"LD_LIBRARY_PATH={RUNTIME}/lib:{RUNTIME}/postgres/lib"
    unit = f"""[Unit]
Description=AiCollabHub {name}
After=network.target {dependencies}
Requires={dependencies}
StartLimitIntervalSec=60
StartLimitBurst=10

[Service]
Type=simple
User={account}
Group={account}
WorkingDirectory={working or data}
Environment={runtime_env}
Environment=PYTHONNOUSERSITE=1
Environment=PYTHONDONTWRITEBYTECODE=1
{("EnvironmentFile=" + env_file) if env_file else ""}
ExecStart={start}
Restart=on-failure
RestartSec=3
TimeoutStopSec=120
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={data}
LimitNOFILE=65536
{extra}
[Install]
WantedBy=multi-user.target
"""
    write(Path(f"/etc/systemd/system/aih-{name}.service"), unit, 0o644)


def app_environment(config: dict) -> dict[str, str]:
    """为 API 生成有限权限凭据，不把数据库所有者或存储管理员交给应用。"""
    data = Path(config["data_dir"]) / "app"
    return {
        "APP_ENV": config["database_environment"],
        "AUTH_MODE": "mock",
        "INSTANCE_ID": "native",
        "AUTH_PUBLIC_ORIGIN": config["public_origin"],
        "AUTH_MOCK_IAM_URL": config["public_origin"],
        "AUTH_INTERNAL_ORIGIN": f"http://127.0.0.1:{config['api_port']}",
        "AUTH_MOCK_IAM_INTERNAL_URL": f"http://127.0.0.1:{config['iam_port']}",
        "AUTH_MOCK_SECRET": PASSWORDS["auth_mock_secret"],
        "AUTH_COOKIE_SECURE": str(config["public_origin"].startswith("https:")).lower(),
        "APPLICATIONS_STORAGE": "database",
        "DATASETS_STORAGE": "database",
        "KNOWLEDGE_STORAGE": "database",
        "APPLICATIONS_RUNTIME": "simulation",
        "TOOLCHAIN_MODE": "disabled",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": str(config["postgres_port"]),
        "POSTGRES_DB": config["postgres_db"],
        "POSTGRES_USER": "aih_app",
        "POSTGRES_PASSWORD": PASSWORDS["postgres_app_password"],
        "REDIS_HOST": "127.0.0.1",
        "REDIS_PORT": str(config["redis_port"]),
        "REDIS_PASSWORD": PASSWORDS["redis_password"],
        "MINIO_ENDPOINT": f"http://127.0.0.1:{config['minio_port']}",
        "MINIO_ACCESS_KEY": PASSWORDS["minio_app_user"],
        "MINIO_SECRET_KEY": PASSWORDS["minio_app_password"],
        "MINIO_BUCKET": "aicollabhub-files",
        "APPLICATIONS_DEMO_DIRECTORY": str(data / "marketplace"),
        "CONVERSATION_ATTACHMENT_DIR": str(data / "attachments"),
        "KNOWLEDGE_DOCUMENT_DIR": str(data / "knowledge"),
        "TOOLCHAIN_CONFIGURATION_FILE": str(data / "toolchain.json"),
        "TMPDIR": str(data / "tmp"),
    }


def wait_http(url: str, timeout: int = 60) -> None:
    """等待本机服务就绪，屏蔽代理变量以确保检查不走外网。"""
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=3) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError(f"服务未能就绪：{url}；请检查 systemctl status 和 journalctl")


def install(args: argparse.Namespace, config: dict, manifest: dict) -> None:
    """更新前完成一致性备份；失败保留旧版本和备份，并停止服务防止继续写入。"""
    managed = preflight(config, manifest, upgrade=args.upgrade)
    if args.check:
        return
    update_state = None
    if args.upgrade:
        from manage import backup

        previous = json.loads(STATE.read_text())
        archive = backup(config, keep_stopped=True)
        update_state = {
            "previous_release_id": previous["release_id"],
            "release_id": manifest["release_id"],
            "config": config,
            "status": "upgrading",
            "backup": str(archive),
        }
    try:
        install_payload(args, config, manifest, managed, update_state)
    except Exception:
        if update_state:
            subprocess.run(
                ["systemctl", "stop", *[f"aih-{item}" for item in reversed(SERVICES)]],
                check=False,
            )
            update_state["status"] = "upgrade_failed"
            write(STATE, json.dumps(update_state, indent=2) + "\n")
            print(f"更新未完成；已停止服务。旧版本保留，更新前备份：{update_state['backup']}")
            print("请按安装包内 README.md 恢复同机备份；不能只切换代码而忽略数据库迁移。")
        raise


def install_payload(
    args: argparse.Namespace,
    config: dict,
    manifest: dict,
    managed: bool,
    update_state: dict | None,
) -> None:
    """安装运行时、应用和服务，最后进行迁移及端到端依赖检查。"""
    global PASSWORDS
    release_id = manifest["release_id"]
    CONFIG.mkdir(parents=True, exist_ok=True)
    CONFIG.chmod(0o700)
    PASSWORDS = create_credentials()
    state = update_state or {"release_id": release_id, "config": config, "status": "installing"}
    write(STATE, json.dumps(state, indent=2) + "\n")
    write(CONFIG / "site.json", json.dumps(config, indent=2) + "\n")
    ROOT.mkdir(parents=True, exist_ok=True)
    ROOT.chmod(0o755)
    if managed and not args.upgrade:
        run(
            ["systemctl", "stop", *[f"aih-{item}" for item in reversed(SERVICES)]],
            label="暂停本套服务以完成同版本安装复核",
        )
    if not RUNTIME.exists():
        print("安装独立运行环境……", flush=True)
        shutil.copytree(args.runtime_source, RUNTIME, symlinks=True)
    runtime_env = {
        **os.environ,
        "LD_LIBRARY_PATH": f"{RUNTIME}/lib:{RUNTIME}/postgres/lib",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_CONFIG_FILE": "/dev/null",
        "LANG": "C.UTF-8",
    }
    release = ROOT / "releases" / release_id
    release.mkdir(parents=True, exist_ok=True)
    write(release / "manifest.json", json.dumps(manifest, indent=2) + "\n", 0o644)
    with tarfile.open(args.bundle / "backend.tar.gz") as archive:
        archive.extractall(release, filter="data")
    shutil.copytree(args.bundle / "frontend", release / "frontend", dirs_exist_ok=True)
    backend = release / "backend"
    venv = release / "venv"
    python = venv / "bin/python"
    if not python.exists():
        run(
            [str(RUNTIME / "python/bin/python3.13"), "-m", "venv", str(venv)],
            label="创建项目专用 Python 环境",
            env=runtime_env,
        )
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(args.bundle / "wheelhouse"),
            "--only-binary=:all:",
            "--force-reinstall",
            "aicollabhub-server==0.1.0",
        ],
        label="从随包 wheelhouse 安装全部 Python 依赖（禁止联网）",
        env=runtime_env,
    )
    run([str(python), "-m", "pip", "check"], label="验证依赖一致性", env=runtime_env)
    for account in ACCOUNTS:
        try:
            pwd.getpwnam(account)
        except KeyError:
            run(
                [
                    "useradd",
                    "--system",
                    "--user-group",
                    "--no-create-home",
                    "--home-dir",
                    "/nonexistent",
                    "--shell",
                    "/sbin/nologin",
                    account,
                ],
                label=f"创建服务账号 {account}",
            )
    # 配置目录只允许服务组穿过，文件本身分别限制为其使用者或 root 可读。
    CONFIG.chmod(0o711)
    data = Path(config["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    data.chmod(0o755)
    for folder, account in (
        ("postgres", "aih-db"),
        ("redis", "aih-redis"),
        ("minio", "aih-minio"),
        ("app", "aih-app"),
        ("web", "aih-web"),
    ):
        owned_directory(data / folder, account)
    owned_directory(data / "app/tmp", "aih-app")
    owned_directory(data / "minio/certs", "aih-minio")
    owned_directory(data / "web/body", "aih-web")
    for folder in ("fastcgi", "uwsgi", "scgi"):
        owned_directory(data / "web" / folder, "aih-web")
    for filename in ("access.log", "nginx.pid"):
        service_file = data / "web" / filename
        if service_file.is_symlink():
            raise RuntimeError(f"服务文件不能是符号链接：{service_file}")
        service_file.touch(exist_ok=True)
        shutil.chown(service_file, user="aih-web", group="aih-web")
        service_file.chmod(0o600)
    owned_directory(data / "postgres/socket", "aih-db", 0o755)
    pgdata = data / "postgres/cluster"
    if not (pgdata / "PG_VERSION").exists():
        if pgdata.exists() and any(pgdata.iterdir()):
            raise RuntimeError("PostgreSQL 数据目录非空但缺少 PG_VERSION，拒绝重新初始化")
        owned_directory(pgdata, "aih-db")
        password_file = CONFIG / "initdb-password"
        write(password_file, PASSWORDS["postgres_admin_password"] + "\n", 0o640, "aih-db")
        try:
            run(
                [
                    "runuser",
                    "-u",
                    "aih-db",
                    "--",
                    str(RUNTIME / "postgres/bin/initdb"),
                    "-D",
                    str(pgdata),
                    "--username=postgres",
                    "--encoding=UTF8",
                    "--locale=C.UTF-8",
                    "--auth-local=peer",
                    "--auth-host=scram-sha-256",
                    f"--pwfile={password_file}",
                ],
                label="首次初始化数据库（已有数据时跳过）",
                env=runtime_env,
            )
        finally:
            password_file.unlink(missing_ok=True)
    write(
        pgdata / "postgresql.conf",
        f"""listen_addresses = '127.0.0.1'
port = {config["postgres_port"]}
max_connections = 60
superuser_reserved_connections = 3
shared_buffers = '256MB'
password_encryption = 'scram-sha-256'
synchronous_commit = 'local'
unix_socket_directories = '{data}/postgres/socket'
logging_collector = off
log_min_duration_statement = 1000
log_statement = 'none'
timezone = 'Asia/Shanghai'
""",
        0o640,
        "aih-db",
    )
    write(pgdata / "pg_ident.conf", "aih_local aih-db postgres\n", 0o640, "aih-db")
    write(
        pgdata / "pg_hba.conf",
        "local all postgres peer map=aih_local\nhost all all 127.0.0.1/32 scram-sha-256\n",
        0o640,
        "aih-db",
    )
    write(
        CONFIG / "redis.conf",
        f"""bind 127.0.0.1
port {config["redis_port"]}
protected-mode yes
requirepass {PASSWORDS["redis_password"]}
dir {data}/redis
appendonly yes
appendfsync everysec
save 900 1
maxmemory 256mb
maxmemory-policy noeviction
logfile ""
daemonize no
""",
        0o640,
        "aih-redis",
    )
    write(
        CONFIG / "minio.env",
        "\n".join(
            [
                "MINIO_ROOT_USER=" + PASSWORDS["minio_root_user"],
                "MINIO_ROOT_PASSWORD=" + PASSWORDS["minio_root_password"],
                "MINIO_BROWSER=on",
                "MINIO_UPDATE=off",
            ]
        )
        + "\n",
    )
    app_env = app_environment(config)
    write(CONFIG / "app.env", "\n".join(f"{key}={value}" for key, value in app_env.items()) + "\n")
    write(CONFIG / "app-env.json", json.dumps(app_env, indent=2) + "\n")
    service_unit(
        "postgres",
        "aih-db",
        f"{RUNTIME}/postgres/bin/postgres -D {pgdata}",
        data=data / "postgres",
        extra="KillSignal=SIGINT",
    )
    service_unit(
        "redis",
        "aih-redis",
        f"{RUNTIME}/redis/bin/redis-server {CONFIG}/redis.conf",
        data=data / "redis",
    )
    service_unit(
        "minio",
        "aih-minio",
        f"{RUNTIME}/minio/minio server {data}/minio --certs-dir {data}/minio/certs "
        f"--address 127.0.0.1:{config['minio_port']} "
        f"--console-address 127.0.0.1:{config['minio_console_port']}",
        data=data / "minio",
        env_file=str(CONFIG / "minio.env"),
    )
    service_unit(
        "iam",
        "aih-app",
        f"{python} -m uvicorn "
        "app.modules.common.auth.mock_iam:create_mock_iam --factory "
        f"--host 127.0.0.1 --port {config['iam_port']} --no-access-log",
        data=data / "app",
        working=backend,
        env_file=str(CONFIG / "app.env"),
    )
    service_unit(
        "api",
        "aih-app",
        f"{python} -m uvicorn app.main:app --host 127.0.0.1 "
        f"--port {config['api_port']} --workers {config['api_workers']} "
        "--proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log",
        data=data / "app",
        working=backend,
        env_file=str(CONFIG / "app.env"),
        dependencies="aih-postgres.service aih-redis.service aih-minio.service",
    )
    configure_web(args.bundle, config, release, data)
    write(
        Path("/etc/logrotate.d/aih-native"),
        f"""{data}/web/access.log {{
    daily
    rotate 14
    missingok
    notifempty
    compress
    delaycompress
    su aih-web aih-web
    create 0600 aih-web aih-web
    sharedscripts
    postrotate
        /bin/systemctl kill -s USR1 --kill-who=main aih-web.service >/dev/null 2>&1 || true
    endscript
}}
""",
        0o644,
    )
    service_unit(
        "web",
        "aih-web",
        f"{RUNTIME}/nginx/sbin/nginx -e stderr -c {CONFIG}/nginx.conf -g 'daemon off;'",
        data=data / "web",
        extra="AmbientCapabilities=CAP_NET_BIND_SERVICE\n"
        "CapabilityBoundingSet=CAP_NET_BIND_SERVICE\nKillSignal=SIGQUIT",
    )
    run(["systemctl", "daemon-reload"], label="加载原生系统服务")
    run(
        ["systemctl", "start", "aih-postgres", "aih-redis", "aih-minio"],
        label="启动数据库、缓存和文件服务",
    )
    for _ in range(60):
        result = subprocess.run(
            [
                str(RUNTIME / "postgres/bin/pg_isready"),
                "-h",
                "127.0.0.1",
                "-p",
                str(config["postgres_port"]),
            ],
            env=runtime_env,
            capture_output=True,
        )
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("PostgreSQL 未就绪")
    wait_http(f"http://127.0.0.1:{config['minio_port']}/minio/health/live")
    provision = args.bundle / "provision.py"
    run(
        [str(python), str(provision)],
        label="初始化或迁移测试库结构及专属存储账号（保留已有业务数据）",
        env={**runtime_env, **app_env},
        cwd=backend,
    )
    for script in ("manage.py", "check.py"):
        shutil.copyfile(args.bundle / script, ROOT / script)
        (ROOT / script).chmod(0o644)
    current = ROOT / "current"
    temporary = ROOT / "current.new"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(release)
    temporary.replace(current)
    write(
        ROOT / "manage.sh",
        f"""#!/usr/bin/env bash
set -Eeuo pipefail
export LD_LIBRARY_PATH={RUNTIME}/lib:{RUNTIME}/postgres/lib
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
exec {python} {ROOT}/manage.py "$@"
""",
        0o755,
    )
    run(
        [
            str(RUNTIME / "nginx/sbin/nginx"),
            "-e",
            "stderr",
            "-t",
            "-c",
            str(CONFIG / "nginx.conf"),
        ],
        label="校验网站入口配置",
        env=runtime_env,
    )
    run(
        ["systemctl", "enable", *[f"aih-{item}" for item in SERVICES]], label="设置所有服务开机启动"
    )
    run(["systemctl", "start", "aih-iam", "aih-api", "aih-web"], label="启动后端和两个网站")
    wait_http(f"http://127.0.0.1:{config['api_port']}/api/v1/health/ready", timeout=90)
    if (
        config["open_firewall"]
        and subprocess.run(
            ["systemctl", "is-active", "--quiet", "firewalld"],
            check=False,
        ).returncode
        == 0
    ):
        for scope in ([], ["--permanent"]):
            run(
                ["firewall-cmd", *scope, f"--add-port={config['web_port']}/tcp"],
                label="放行网站端口（数据库等仍仅监听本机）",
            )
    run(
        [str(python), str(ROOT / "check.py")],
        env=runtime_env,
        cwd=backend,
        label="检查服务、业务表、持久化读写和前端文件",
    )
    state["status"] = "installed"
    write(STATE, json.dumps(state, indent=2) + "\n")
    write(ROOT / "release-manifest.json", json.dumps(manifest, indent=2) + "\n", 0o644)
    print(f"安装完成。用户端：{config['public_origin']}/；管理端：{config['public_origin']}/admin/")
    print(f"管理命令：sudo {ROOT}/manage.sh status|check|start|stop|restart|backup")
    print(f"凭据仅保存在 {CONFIG}/credentials.json（root 可读）；请单独备份。")
    if update_state:
        print(f"更新完成，更新前备份保留在：{update_state['backup']}")


def configure_web(bundle: Path, config: dict, release: Path, data: Path) -> None:
    """配置同源网站、分级上传限制及模拟认证代理，支持甲方提供的 TLS 证书。"""
    template = (bundle / "nginx.conf.template").read_text()
    tls = ""
    if config["public_origin"].startswith("https:"):
        for name, filename in (("tls_certificate", "web.crt"), ("tls_private_key", "web.key")):
            write(CONFIG / filename, Path(config[name]).read_text(), 0o640, "aih-web")
        tls = f"ssl_certificate {CONFIG}/web.crt;\nssl_certificate_key {CONFIG}/web.key;\n"
        tls += "ssl_protocols TLSv1.2 TLSv1.3;"
    values = {
        "DATA": str(data),
        "RUNTIME": str(RUNTIME),
        "CONFIG": str(CONFIG),
        "RELEASE": str(release),
        "WEB_PORT": str(config["web_port"]),
        "API_PORT": str(config["api_port"]),
        "IAM_PORT": str(config["iam_port"]),
        "TLS_LISTEN": " ssl" if tls else "",
        "TLS_CONFIG": tls,
    }
    for key, value in values.items():
        template = template.replace("@@" + key + "@@", value)
    write(CONFIG / "nginx.conf", template, 0o644)
    shutil.copyfile(bundle / "proxy.conf", CONFIG / "proxy.conf")
    (CONFIG / "proxy.conf").chmod(0o644)


def main() -> None:
    """持有互斥锁后执行预检或安装，失败不会删除任何业务数据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--runtime-source", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="只校验配置、系统和安装条件")
    parser.add_argument("--upgrade", action="store_true", help="备份后更新已有测试环境")
    args = parser.parse_args()
    os.umask(0o022)
    config = validate_config(args.config.resolve())
    manifest = json.loads((args.bundle / "manifest.json").read_text())
    if not re.fullmatch(r"[A-Za-z0-9._-]+", manifest["release_id"]):
        raise ValueError("非法发布编号")
    with Path("/run/lock/aih-native-install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        install(args, config, manifest)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as error:
        print(f"安装已停止：{error}", file=sys.stderr)
        sys.exit(1)
