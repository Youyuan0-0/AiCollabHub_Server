"""离线平台诊断：检查部署、记录登录窗口、关联日志并导出脱敏报告。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from core import (
    SENSITIVE,
    bounded,
    http_probe,
    parse_auth,
    redact,
    report_html,
    safe_url,
    summarize_flows,
)
from native_config import effective_environment

ROOT = Path("/opt/aicollabhub")
CONFIG = Path("/etc/aicollabhub")
DESTINATION = Path("/var/log/aicollabhub-diagnostics")
AUTH_FIELDS = (
    "AUTH_MODE",
    "AUTH_PUBLIC_ORIGIN",
    "AUTH_CALLBACK_ORIGIN",
    "AUTH_COOKIE_SECURE",
    "AUTH_IAM_ORIGIN",
    "AUTH_IAM_APP_ID",
    "AUTH_IAM_TIMEOUT",
    "AUTH_IAM_ADMIN_ROLE_CODE",
    "AUTH_LOGIN_TTL",
    "AUTH_SESSION_TTL",
    "AUTH_IDENTITY_SYNC_ENABLED",
)
LIMIT = 2000


def command(arguments: list[str], timeout: int = 10) -> dict:
    """有时限执行固定系统命令，不通过 shell，失败也保留退出码。"""
    try:
        process = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return {
            "exitCode": process.returncode,
            "stdout": process.stdout[:4000000],
            "stderr": process.stderr[:16000],
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"exitCode": None, "errorType": type(error).__name__, "stdout": "", "stderr": ""}


class Diagnosis:
    """隔离每一项探测，故障时继续收集后续证据。"""

    def __init__(self, ca_file: str | None = None) -> None:
        self.ca_file = ca_file
        self.site = {}
        self.env = {}
        self.secrets = ()
        self.report = {
            "schema": "aih-diagnostics-v1",
            "generatedAt": datetime.now(UTC).isoformat(),
            "checks": [],
            "network": [],
            "notes": [
                "诊断不修改服务、权限或业务数据；本机登录入口探测仅生成短期登录状态，不验票。",
                "网络可达不等于身份认证成功；本机探测不代表客户端到网关可达。",
            ],
        }

    def add(
        self, name: str, status: str, expected: str, actual: object, next_step: str = ""
    ) -> None:
        """记录中文结论；结构化实际值仍经最终脱敏。"""
        self.report["checks"].append(
            {
                "name": name,
                "status": status,
                "expected": expected,
                "actual": actual,
                "next": next_step,
            }
        )
        print(f"[{status}] {name}", flush=True)

    def load(self) -> None:
        """读取实际生效的文件；配置损坏时保留故障并允许继续收集日志。"""
        try:
            self.env = effective_environment(CONFIG)
            self.secrets = tuple(
                value
                for key, value in self.env.items()
                if SENSITIVE.search(key) and key not in AUTH_FIELDS and len(value) >= 4
            )
            self.site = json.loads((CONFIG / "site.json").read_text(encoding="utf-8"))
            self.add("部署配置读取", "PASS", "配置可解析", "app.env 优先于 app-env.json")
        except Exception as error:
            self.add(
                "部署配置读取",
                "FAIL",
                "配置可解析",
                {"errorType": type(error).__name__},
                "核对 app.env 的单行格式和 site.json；不要覆盖现有数据库配置。",
            )

    def baseline(self, network: bool = True) -> None:
        """检查版本、服务、有效配置、本机代理和可选外部网络。"""
        self.load()
        try:
            manifest = json.loads((ROOT / "current/manifest.json").read_text())
            self.report["release"] = {
                key: manifest.get(key) for key in ("release_id", "built_at", "schema_head")
            }
            self.add("当前版本", "PASS", "核对准备升级的版本", self.report["release"])
            # 对比当前版本与安装器的版本指针，不打印含凭据的整个安装状态。
            installed = json.loads((ROOT / "release-manifest.json").read_text())
            self.add(
                "安装版本一致性",
                "PASS" if installed.get("release_id") == manifest.get("release_id") else "FAIL",
                "版本清单相同",
                installed.get("release_id"),
                "不一致时核对升级日志及 current 指针。",
            )
        except Exception as error:
            self.add(
                "当前版本",
                "FAIL",
                "版本清单可读取",
                {"errorType": type(error).__name__},
                "检查 /opt/aicollabhub/current 是否有效。",
            )
        for service in ("api", "web", "postgres", "redis", "minio"):
            state = command(
                [
                    "systemctl",
                    "show",
                    "aih-" + service,
                    "--property=ActiveState,SubState,MainPID,ExecMainStatus,ActiveEnterTimestamp",
                ]
            )
            self.add(
                "服务 " + service,
                "PASS" if "ActiveState=active" in state["stdout"].splitlines() else "FAIL",
                "active",
                state,
                "失败时查看该服务的日志，不需要先重启掩盖现场。",
            )
        clock = command(["timedatectl", "show", "--property=NTPSynchronized,Timezone,TimeUSec"])
        self.add(
            "服务器时间",
            "PASS" if "NTPSynchronized=yes" in clock["stdout"] else "WARN",
            "时间准确且有可信校时",
            clock,
            "对照浏览器和 IAM 时间；未启用 NTP 本身不能证明时间错误。",
        )
        self.report["configuration"] = {
            key: safe_url(self.env[key])
            if "://" in self.env.get(key, "")
            else self.env.get(key, "<default>")
            for key in AUTH_FIELDS
        }
        self.report["listeners"] = command(["ss", "-lnt"])
        origin = self.env.get("AUTH_PUBLIC_ORIGIN", self.site.get("public_origin", ""))
        callback = self.env.get("AUTH_CALLBACK_ORIGIN") or origin
        host = ""
        try:
            host = urlsplit(origin).netloc
            valid = (
                bool(host)
                and urlsplit(origin).scheme in {"http", "https"}
                and urlsplit(callback).hostname == urlsplit(origin).hostname
                and urlsplit(callback).scheme == urlsplit(origin).scheme
            )
            self.add(
                "网站与回调主机",
                "PASS" if valid else "FAIL",
                "浏览器可访问，网站与回调的主机、协议一致",
                {"public": safe_url(origin), "callback": safe_url(callback)},
                "核对 HTTPS 网关对外地址；内部 8086 不等于浏览器对外端口。",
            )
            secure = self.env.get("AUTH_COOKIE_SECURE", "").lower()
            secure_ok = secure == ("true" if urlsplit(origin).scheme == "https" else "false")
            self.add(
                "会话 Cookie 配置",
                "PASS" if secure_ok else "FAIL",
                "HTTPS 对外入口启用 Secure，HTTP 入口关闭 Secure",
                {"secure": self.env.get("AUTH_COOKIE_SECURE", "<default>")},
                "以浏览器外部访问协议为准；HTTP 入口的 Secure Cookie 不会被浏览器携带。",
            )
            self.add(
                "网站配置一致性",
                "PASS"
                if origin.rstrip("/") == str(self.site.get("public_origin", "")).rstrip("/")
                else "WARN",
                "site.json 与 AUTH_PUBLIC_ORIGIN 一致",
                {"siteOrigin": safe_url(self.site.get("public_origin", ""))},
                "核对 Nginx 与应用使用的入口，使用升级器的网络配置参数校正。",
            )
            saved = json.loads((CONFIG / "app-env.json").read_text())
            differences = [
                key
                for key in AUTH_FIELDS
                if key in self.env and str(saved.get(key, "")) != self.env[key]
            ]
            self.add(
                "配置副本漂移",
                "WARN" if differences else "PASS",
                "AUTH 字段副本一致",
                differences,
                "app.env 是实际来源；先核对进程启动时间，确认修改后已重启 API。",
            )
        except Exception as error:
            self.add(
                "认证配置比较", "FAIL", "入口和副本可比较", {"errorType": type(error).__name__}
            )
        nginx = command(
            [
                str(ROOT / "runtime/nginx/sbin/nginx"),
                "-t",
                "-e",
                "stderr",
                "-c",
                str(CONFIG / "nginx.conf"),
            ]
        )
        self.add(
            "Nginx 配置语法",
            "PASS" if nginx["exitCode"] == 0 else "FAIL",
            "配置验证成功",
            nginx,
            "修正配置后先重新验证，再显式重载。",
        )
        if not self.site:
            return
        api = "http://127.0.0.1:" + str(self.site.get("api_port", 18000))
        dependency = http_probe(api + "/api/v1/health/dependencies", host=host)
        self.report["network"].append(dependency)
        self.add(
            "后端及依赖",
            "PASS" if dependency.get("healthStatus") == "ready" else "FAIL",
            "HTTP 200 且 status=ready",
            dependency,
            "核对 API、PostgreSQL、Redis 和对象存储；该探测不写业务数据。",
        )
        ports = [self.site.get("web_port", 80), *self.site.get("web_extra_ports", [])]
        for port in dict.fromkeys(ports):
            local = "http://127.0.0.1:" + str(port)
            if self.site.get("tls_termination") != "gateway" and origin.startswith("https:"):
                self.add(
                    "本机 TLS 入口",
                    "NOT_OBSERVED",
                    "使用证书匹配的主机连接",
                    "见外部地址探测；不关闭证书验证以伪造通过",
                )
                break
            for route in (
                "/",
                "/admin/",
                "/diagnostics/auth.html",
                "/api/v1/auth/config",
                "/api/v1/auth/session?scope=user",
                "/api/v1/auth/login?scope=user&return_to=%2F",
            ):
                probe = http_probe(local + route, host=host)
                self.report["network"].append(probe)
                expected = 401 if "/session" in route else 303 if "/login?" in route else 200
                ok = probe.get("status") == expected
                if "/config" in route:
                    ok = ok and probe.get("provider") == self.env.get("AUTH_MODE")
                if "/session" in route:
                    ok = ok and probe.get("detail") == "authentication_required"
                if "/login?" in route:
                    ok = (
                        ok
                        and probe.get("servicePath") == "/api/v1/auth/callback"
                        and {"state", "scope"}.issubset(probe.get("serviceQueryKeys", []))
                        and probe.get("serviceOrigin", "").rstrip("/") == callback.rstrip("/")
                    )
                if "/diagnostics/" in route:
                    ok = ok and probe.get("diagnosticPage") is True
                self.add(
                    f"本机 {port} {route.split('?')[0]}",
                    "PASS" if ok else "FAIL",
                    f"HTTP {expected}，内容结构符合接口约定",
                    probe,
                    "401 在匿名会话检查中是预期结果；HTML 登录页不能当作 JSON API 成功。",
                )
                if route in ("/", "/admin/"):
                    for asset in probe.get("assets", [])[:5]:
                        if not asset.startswith("/") or asset.startswith("//"):
                            continue
                        resource = http_probe(local + asset, host=host)
                        self.report["network"].append(resource)
                        self.add(
                            "静态资源 " + asset,
                            "PASS"
                            if resource.get("status") == 200
                            and resource.get("contentType") not in {"text/html", "application/json"}
                            else "FAIL",
                            "返回实际 JS/CSS",
                            resource,
                            "检查网关路径、SPA 回退和旧浏览器缓存。",
                        )
        if network:
            iam = (
                self.env.get("AUTH_IAM_ORIGIN", "").rstrip("/")
                + "/authn-api/v5/cas/"
                + self.env.get("AUTH_IAM_APP_ID", "")
                + "/login"
            )
            for label, address in (("平台外部入口", origin), ("IAM 登录地址", iam)):
                self.external(label, address)
        self.add(
            "真实验票与浏览器登录",
            "NOT_OBSERVED",
            "用户完成一次真实登录",
            "服务器检查不会提交 ticket；运行 start 后在浏览器自检页发起登录，再执行 finish。",
        )

    def external(self, label: str, address: str) -> None:
        """分别报告 DNS/TCP/HTTP，保留真实证书验证，禁止自动跟随重定向。"""
        try:
            target = urlsplit(address)
            if target.scheme not in {"http", "https"} or not target.hostname or target.username:
                raise ValueError("invalid_origin")
            port = target.port or (443 if target.scheme == "https" else 80)
            addresses = bounded(
                lambda: socket.getaddrinfo(target.hostname, port, type=socket.SOCK_STREAM), 5
            )
            self.add(label + " DNS", "PASS", "可以解析", sorted({item[4][0] for item in addresses}))

            def connect():
                with socket.create_connection((target.hostname, port), timeout=5):
                    return True

            bounded(connect, 6)
            self.add(
                label + " TCP", "PASS", "目标端口可连接", {"host": target.hostname, "port": port}
            )
            probe = http_probe(address, ca_file=self.ca_file)
            self.report["network"].append(probe)
            self.add(
                label + " HTTP",
                "PASS" if probe.get("status") in {200, 301, 302, 303, 307, 308} else "WARN",
                "收到合理的 HTTP 响应；不代表验票成功",
                probe,
                "对照地址、证书及首跳 Location；服务器到入口和客户端到入口是不同链路。",
            )
        except Exception as error:
            self.add(
                label + " 网络",
                "FAIL",
                "DNS 与 TCP 可用",
                {"errorType": type(error).__name__},
                "核对 DNS、路由、防火墙和目标端口。",
            )

    def logs(self, since: datetime) -> None:
        """限时限量收集平台日志，读取轮转前一份访问日志并按时间截取。"""
        since = since.astimezone(UTC)
        end = datetime.now(UTC)
        result = command(
            [
                "journalctl",
                "--no-pager",
                "-o",
                "json",
                "-n",
                str(LIMIT),
                "--since",
                since.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "-u",
                "aih-api",
                "-u",
                "aih-web",
                "-u",
                "aih-postgres",
                "-u",
                "aih-redis",
                "-u",
                "aih-minio",
            ],
            timeout=15,
        )
        logs, events = [], []
        for line in result["stdout"].splitlines():
            try:
                entry = json.loads(line)
                message = entry.get("MESSAGE", "")
                if not isinstance(message, str):
                    continue
                timestamp = datetime.fromtimestamp(
                    int(entry["__REALTIME_TIMESTAMP"]) / 1000000, UTC
                ).isoformat()
                logs.append(
                    {
                        "time": timestamp,
                        "unit": entry.get("_SYSTEMD_UNIT"),
                        "message": redact(message, self.secrets),
                    }
                )
                event = parse_auth(message)
                if event:
                    events.append({**event, "time": timestamp})
            except (ValueError, KeyError, TypeError):
                continue
        self.report["logWindow"] = {
            "since": since.isoformat(),
            "until": end.isoformat(),
            "journalCount": len(logs),
            "journalLimit": LIMIT,
            "possiblyTruncated": len(logs) >= LIMIT or len(result["stdout"]) >= 4000000,
            "journalExitCode": result["exitCode"],
        }
        if result["exitCode"] != 0:
            self.add(
                "日志读取",
                "WARN",
                "可以读取 journal",
                result.get("errorType") or redact(result["stderr"], self.secrets),
                "检查权限；日志不可用时不能把缺少阶段判断为未执行。",
            )
        self.report["logs"] = logs
        self.report["flows"] = summarize_flows(events)
        self.report["standaloneAuthEvents"] = [
            e
            for e in events
            if e.get("stage") in {"session", "logout", "home_callback"}
            or e.get("result") == "session_reused"
        ]
        access = []
        byte_truncated = False
        for name in ("access.log.1", "access.log"):
            source = Path(self.site.get("data_dir", "/data/aicollabhub")) / "web" / name
            try:
                if source.is_symlink():
                    continue
                byte_truncated = byte_truncated or source.stat().st_size > 2000000
                with source.open("rb") as stream:
                    stream.seek(max(0, source.stat().st_size - 2000000))
                    data = stream.read(2000000).decode("utf-8", "replace")
                for line in data.splitlines():
                    try:
                        timestamp = datetime.fromisoformat(line.split(" ", 1)[0])
                    except ValueError:
                        continue
                    if since <= timestamp <= end:
                        access.append(redact(line, self.secrets))
            except OSError:
                continue
        self.report["accessLogs"] = access[-LIMIT:]
        self.report["logWindow"]["accessPossiblyTruncated"] = len(access) >= LIMIT or byte_truncated
        if any(re.search(r" (?:/|/admin/?) .*ticket_present=1", line) for line in access):
            self.report["notes"].append(
                "窗口内出现首页携带 ticket 的请求；R10 会经 home-callback 转交原回调。"
                "各次处理结果见关联流程，不能把其他用户的请求拼成同一次登录。"
            )
            if not any(e.get("stage") == "home_callback" for e in events):
                self.add(
                    "首页票据转交",
                    "WARN",
                    "观察到 home_callback 阶段",
                    "首页收到票据，但本窗口没有转交记录。",
                    "核对运行版本、Nginx 配置和后续请求；日志不足也可能导致未观察到。",
                )
        if not self.report["flows"]:
            self.add(
                "认证流程记录",
                "NOT_OBSERVED",
                "窗口内有非探测的新登录",
                "未找到完整流程关联；可能没有新登录、正在复用会话、版本较旧或日志不完整。",
            )

    def browser(self, source: Path) -> None:
        """仅导入自检页固定字段；拒绝超大文件，不导出原始浏览器文件。"""
        if source.stat().st_size > 2_000_000:
            raise ValueError("浏览器报告超过 2MB")
        data = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or data.get("schema") != "aih-browser-diagnostics-v1":
            raise ValueError("不是浏览器自检页导出的报告")
        if not isinstance(data.get("events"), list) or not isinstance(data.get("userAgent"), str):
            raise ValueError("浏览器报告缺少事件数组或浏览器信息")
        keys = {
            "time",
            "event",
            "path",
            "method",
            "status",
            "elapsedMs",
            "requestId",
            "errorType",
            "responseKind",
            "reason",
            "moduleScripts",
            "abortController",
            "abortSignalTimeout",
            "fetch",
            "storage",
            "scope",
            "canAccessAdmin",
        }
        events = [
            {
                key: redact(value, self.secrets) if isinstance(value, str) else value
                for key, value in row.items()
                if key in keys and isinstance(value, (str, int, bool, type(None)))
            }
            for row in data.get("events", [])[:500]
            if isinstance(row, dict)
        ]
        self.report["browser"] = {
            "schema": data["schema"],
            "userAgent": redact(data.get("userAgent", ""), self.secrets),
            "events": events,
        }

    def save(self, directory: Path) -> None:
        """只归档本次生成的脱敏文件，不复制 env、数据库或原始 HAR。"""
        report = self.report

        # 普通结构已在产生处白名单化；对命令输出再消除已知配置中的凭据。
        def clean(value: object) -> object:
            if isinstance(value, str):
                return redact(value, self.secrets)
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value

        report = clean(report)
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        (directory / "report.json").write_text(serialized, encoding="utf-8")
        (directory / "report.html").write_text(report_html(report), encoding="utf-8")
        lines = ["人工智能协同平台诊断报告", "生成时间：" + report["generatedAt"]]
        for row in report["checks"]:
            lines.extend(
                [
                    f"[{row['status']}] {row['name']}",
                    "预期：" + row["expected"],
                    "实际：" + json.dumps(row["actual"], ensure_ascii=False),
                    "下一步：" + row["next"],
                    "",
                ]
            )
        for flow in report.get("flows", []):
            lines.extend(["流程：" + flow["flowId"], flow["conclusion"]])
            lines.extend(
                f"  [{step['status']}] {step['name']} 请求编号={','.join(step['requestIds'])}"
                for step in flow["steps"]
            )
        (directory / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        with tarfile.open(directory / "diagnostics.tar.gz", "w:gz") as archive:
            for name in ("report.json", "report.html", "report.txt"):
                archive.add(directory / name, arcname=name)
        digest = hashlib.sha256((directory / "diagnostics.tar.gz").read_bytes()).hexdigest()
        (directory / "diagnostics.tar.gz.sha256").write_text(
            digest + "  diagnostics.tar.gz\n", encoding="utf-8"
        )
        print("诊断文件：" + str(directory), flush=True)


def new_directory() -> Path:
    """在 root 专属目录创建唯一测试窗口，禁止覆盖已有报告。"""
    from uuid import uuid4

    DESTINATION.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = DESTINATION / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    return directory


def main() -> int:
    """提供检查、开始、结束、收集四种入口；预期故障使用非零退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "start", "finish", "collect"))
    parser.add_argument("case", nargs="?", type=Path)
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--ca-file", help="显式信任的 CA 文件；仍校验主机名和有效期")
    parser.add_argument("--browser-report", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("请使用 sudo /opt/aicollabhub/diagnose.sh")
    if not 1 <= args.minutes <= 1440:
        parser.error("minutes 必须在 1 到 1440 之间")
    os.umask(0o077)
    diagnosis = Diagnosis(args.ca_file)
    if args.action == "finish":
        if not args.case:
            parser.error("finish 需要 start 输出的报告目录")
        directory = args.case.resolve()
        if directory.parent != DESTINATION.resolve():
            parser.error("报告目录必须是诊断目录的直接子目录")
        state = json.loads((directory / "case.json").read_text())
        since = datetime.fromisoformat(state["startedAt"])
        diagnosis.load()
        if (directory / "baseline.json").exists():
            diagnosis.report["baseline"] = json.loads((directory / "baseline.json").read_text())
    else:
        directory = new_directory()
        since = datetime.now(UTC) - timedelta(minutes=args.minutes)
        if args.action in {"check", "start"}:
            diagnosis.baseline(network=not args.local_only)
        else:
            diagnosis.load()
    if args.action == "start":
        started = datetime.now(UTC).isoformat()
        (directory / "case.json").write_text(json.dumps({"startedAt": started}), encoding="utf-8")
        diagnosis.save(directory)
        (directory / "baseline.json").write_bytes((directory / "report.json").read_bytes())
        origin = diagnosis.env.get("AUTH_PUBLIC_ORIGIN", "").rstrip("/")
        print("浏览器打开：" + safe_url(origin + "/diagnostics/auth.html"))
        print("点击发起登录，完成后导出浏览器报告，再执行：")
        print("sudo /opt/aicollabhub/diagnose.sh finish " + str(directory))
    else:
        diagnosis.logs(since)
        if args.browser_report:
            diagnosis.browser(args.browser_report)
        diagnosis.save(directory)
    failed = any(row["status"] == "FAIL" for row in diagnosis.report["checks"])
    if args.action in {"finish", "collect"}:
        failed = failed or any(flow["failures"] for flow in diagnosis.report.get("flows", []))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print(
            "诊断未完成：" + type(error).__name__ + "；核对路径、权限和输入文件格式。",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
