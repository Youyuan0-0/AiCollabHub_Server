"""使用真实登录的专用会话执行 Vegeta 只读压测，记录业务校验和服务器资源。"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent
ROUTES = {
    "/api/v1/datasets?page=1&pageSize=10": "page",
    "/api/v1/models?page=1&pageSize=10": "page",
    "/api/v1/applications?page=1&pageSize=10": "page",
    "/api/v1/knowledge-bases": "list_or_page",
    "/api/v1/tools?page=1&pageSize=10": "page",
}


class NoRedirect(HTTPRedirectHandler):
    """拒绝登录跳转，防止过期会话被算作成功或 Cookie 被带往其他主机。"""

    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        """重定向由调用方按失败处理。"""
        return None


def write_json(path: Path, value: object) -> None:
    """原子写入仅当前账号可读的 JSON 文件。"""
    temporary = path.with_name(path.name + ".new")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(path)


def valid_body(body: bytes, contract: str) -> bool:
    """检查列表结构，避免将 HTML 登录页或错误对象视为业务成功。"""
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return False
    if contract == "list_or_page" and isinstance(value, list):
        return True
    return isinstance(value, dict) and isinstance(value.get("items"), list)


def read_identities(path: Path) -> list[dict[str, str]]:
    """读取专用会话，要求受限权限、唯一用户和合法 Cookie 字符。"""
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("会话文件权限必须为 600 或 400，请先执行 chmod 600")
    identities = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(identities, list) or not identities:
        raise ValueError("会话文件需为非空数组，逐项填写 subject 和 cookie")
    for identity in identities:
        if (
            not isinstance(identity, dict)
            or set(identity) != {"subject", "cookie"}
            or not all(isinstance(value, str) and value for value in identity.values())
            or any(not 33 <= ord(char) <= 126 or char in ';,"' for char in identity["cookie"])
        ):
            raise ValueError("会话文件含有无效身份或 Cookie，请使用专用测试会话")
    if len({identity["subject"] for identity in identities}) != len(identities):
        raise ValueError("会话文件的 subject 不能重复")
    return identities


def session_opener(cookie: str) -> OpenerDirector:
    """仅向给定平台发送会话，不采用环境代理或允许重定向。"""
    opener = build_opener(ProxyHandler({}), NoRedirect())
    opener.addheaders = [("Cookie", "ach_session=" + cookie)]
    return opener


def verified_session(origin: str, identity: dict[str, str]) -> dict:
    """校验登录会话中的 workNo，避免误用普通运维会话或已过期 Cookie。"""
    with session_opener(identity["cookie"]).open(
        origin + "/api/v1/auth/session?scope=user", timeout=15
    ) as response:
        session = json.load(response)
    if session.get("user", {}).get("id") != identity["subject"] or not session.get("csrfToken"):
        raise RuntimeError("会话身份与填写的 workNo 不一致，停止压测")
    return session


def capture_session(origin: str, path: Path) -> None:
    """提示录入已真实登录的测试身份和 Cookie，验证后保存，支持逐个追加。"""
    if not sys.stdin.isatty():
        raise ValueError("录入会话需在交互终端执行，不接受命令行或管道明文 Cookie")
    identity = {
        "subject": input("专用测试账号的 workNo：").strip(),
        "cookie": getpass.getpass("ach_session 的值（输入不回显）：").strip(),
    }
    if (
        not identity["subject"]
        or any(not 33 <= ord(char) <= 126 or char in ';,"' for char in identity["cookie"])
        or not identity["cookie"]
    ):
        raise ValueError("workNo 或 Cookie 格式不正确")
    verified_session(origin, identity)
    identities = read_identities(path) if path.exists() else []
    identities = [item for item in identities if item["subject"] != identity["subject"]]
    identities.append(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, identities)
    print(f"会话验证通过，已保存 {len(identities)} 个专用测试账号；Cookie 未输出。")


def prepare_targets(origin: str, path: Path, identities: list[dict], sessions: list[dict]) -> list:
    """逐个校验身份及五类只读列表，确认匿名请求仍然被拒绝。"""
    anonymous = build_opener(ProxyHandler({}), NoRedirect())
    with anonymous.open(origin + "/api/v1/health/ready", timeout=15) as response:
        if json.load(response).get("status") != "ready":
            raise RuntimeError("后端依赖未就绪")
    try:
        anonymous.open(origin + next(iter(ROUTES)), timeout=15)
    except HTTPError as error:
        if error.code != 401:
            raise RuntimeError(f"匿名业务接口状态不是预期的 401：{error.code}") from None
    else:
        raise RuntimeError("匿名访问业务接口未被拒绝，停止压测")
    targets = []
    smoke = []
    for identity in identities:
        session = verified_session(origin, identity)
        header = {"Cookie": ["ach_session=" + identity["cookie"]]}
        sessions.append(
            {"subject": identity["subject"], "header": header, "csrf": session["csrfToken"]}
        )
        opener = session_opener(identity["cookie"])
        for route, contract in ROUTES.items():
            with opener.open(origin + route, timeout=20) as response:
                body = response.read()
                if response.status != 200 or not valid_body(body, contract):
                    raise RuntimeError(f"业务请求未通过结构校验：{route}")
                value = json.loads(body)
                items = value if isinstance(value, list) else value["items"]
                smoke.append({"subject": identity["subject"], "route": route, "items": len(items)})
            targets.append({"method": "GET", "url": origin + route, "header": header})
    with path.open("w", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        for target in targets:
            stream.write(json.dumps(target, ensure_ascii=False) + "\n")
    return smoke


def validate_results(binary: Path, results: Path) -> dict:
    """流式检查全部状态码和正文，不将 Cookie 或响应正文写入公开报告。"""
    with subprocess.Popen(
        [str(binary), "encode", "-to=json", str(results)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ) as process:
        if process.stdout is None:
            raise RuntimeError("无法读取 Vegeta 响应")
        counters: dict[str, Counter] = {route: Counter() for route in ROUTES}
        for line in process.stdout:
            row = json.loads(line)
            parsed = urlsplit(row["url"])
            route = parsed.path + (("?" + parsed.query) if parsed.query else "")
            count = counters.setdefault(route, Counter())
            count["requests"] += 1
            if row.get("code") != 200:
                count["non200"] += 1
            elif route not in ROUTES or not valid_body(
                base64.b64decode(row.get("body") or ""), ROUTES[route]
            ):
                count["invalidBusinessBody"] += 1
            else:
                count["validBusinessResponses"] += 1
        if process.wait() != 0:
            raise RuntimeError("Vegeta 响应解码失败")
    total = sum(value["requests"] for value in counters.values())
    valid = sum(value["validBusinessResponses"] for value in counters.values())
    return {
        "requests": total,
        "validBusinessResponses": valid,
        "passed": total > 0 and valid == total,
        "routes": {key: dict(value) for key, value in counters.items()},
    }


def cleanup_sessions(origin: str, sessions: list[dict]) -> list[dict]:
    """撤销专用测试会话，不退出集团其他系统、不改角色、不删除业务数据。"""
    results = []
    for session in sessions:
        try:
            request = Request(
                origin + "/api/v1/auth/logout?scope=user&global_logout=false",
                data=b"",
                headers={
                    "Origin": origin,
                    "Cookie": session["header"]["Cookie"][0],
                    "X-CSRF-Token": session["csrf"],
                },
            )
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=15) as response:
                response.read()
                results.append({"subject": session["subject"], "status": response.status})
        except Exception as error:
            results.append({"subject": session["subject"], "error": type(error).__name__})
    return results


def positive(value: str) -> int:
    """校验负载参数为正整数，零请求速率仅由固定并发模式内部设置。"""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("参数必须为正整数")
    return number


def build_cases(args: argparse.Namespace) -> list[tuple[str, int, int, int, int]]:
    """区分每秒请求数与固定并发请求数，不能把 worker 数冒充独立用户数。"""
    if args.profile == "custom":
        if args.rate is not None:
            return [("custom-rate", args.rate, min(10, args.workers), args.workers, args.duration)]
        if args.concurrency is not None:
            return [("custom-concurrency", 0, args.concurrency, args.concurrency, args.duration)]
        raise ValueError("custom 模式必须选择 --rate 或 --concurrency")
    if args.rate is not None or args.concurrency is not None:
        raise ValueError("--rate 和 --concurrency 只能搭配 --profile custom")
    if args.profile == "smoke":
        return [("rate-20", 20, 10, 50, 10), ("concurrency-10", 0, 10, 10, 10)]
    return [
        ("rate-20", 20, 10, 100, 10),
        ("rate-50", 50, 10, 100, 15),
        ("rate-100", 100, 10, 100, 15),
        ("rate-200", 200, 10, 100, 15),
        ("concurrency-10", 0, 10, 10, 10),
        ("concurrency-25", 0, 25, 25, 10),
        ("concurrency-50", 0, 50, 50, 10),
        ("concurrency-100", 0, 100, 100, 10),
    ]


def main() -> None:
    """录入会话或执行有时限的压测；失败停止加压并返回非零退出状态。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="默认读取 /etc/aicollabhub/site.json")
    parser.add_argument("--session-file", type=Path, required=True)
    parser.add_argument("--capture-session", action="store_true", help="交互录入专用测试会话")
    parser.add_argument("--work-dir", type=Path, help="独立结果目录，不能重复使用")
    parser.add_argument("--profile", choices=("smoke", "baseline", "custom"), default="smoke")
    load = parser.add_mutually_exclusive_group()
    load.add_argument("--rate", type=positive, help="每秒请求数，不代表并发用户数")
    load.add_argument("--concurrency", type=positive, help="固定并发请求 worker 数，无思考时间")
    parser.add_argument("--workers", type=positive, default=100, help="速率模式最大 worker 数")
    parser.add_argument("--duration", type=positive, default=30, help="custom 持续秒数，最多 3600")
    parser.add_argument("--keep-sessions", action="store_true", help="本轮结束不撤销专用测试会话")
    parser.add_argument(
        "--no-monitor", action="store_true", help="发压机与应用机分开时禁用本机采样"
    )
    args = parser.parse_args()
    os.umask(0o077)
    origin = (
        args.base_url or json.loads(Path("/etc/aicollabhub/site.json").read_text())["public_origin"]
    )
    origin = origin.rstrip("/")
    parts = urlsplit(origin)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or any((parts.path, parts.query, parts.fragment, parts.username, parts.password))
    ):
        raise ValueError("base-url 必须是不带路径、查询串和用户名的 http(s) 地址")
    if args.capture_session:
        capture_session(origin, args.session_file)
        return
    cases = build_cases(args)
    if args.duration > 3600 or args.work_dir is None:
        raise ValueError("压测需指定 --work-dir，持续时间最多 3600 秒")
    if not args.no_monitor and os.geteuid() != 0:
        raise ValueError("本机资源及数据库采样需 sudo；独立发压机可使用 --no-monitor")
    identities = read_identities(args.session_file)
    args.work_dir.mkdir(parents=True, exist_ok=False)
    private = args.work_dir / "private"
    public = args.work_dir / "reports"
    private.mkdir(mode=0o700)
    public.mkdir(mode=0o700)
    binary = ROOT / "vegeta"
    version = subprocess.check_output([str(binary), "-version"], text=True).strip()
    report = {
        "startedAt": datetime.now(UTC).isoformat(),
        "origin": origin,
        "profile": args.profile,
        "vegeta": version,
        "preparedIdentities": len(identities),
        "limitations": [
            "Workers are concurrent requests, not unique users or browser connections.",
            "Read-only catalogs; IAM authentication and AI execution throughput are not tested.",
            "Same-server load generation competes with application CPU, RAM and bandwidth.",
            "This test does not certify 20000 persistent connections or 5000 active users.",
        ],
        "cases": [],
    }
    sessions: list[dict] = []
    monitor = None
    stop_file = private / "monitor-stop"
    try:
        targets = private / "targets.jsonl"
        report["smokeChecks"] = prepare_targets(origin, targets, identities, sessions)
        if not args.no_monitor:
            monitor = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "monitor.py"),
                    "--output",
                    str(public / "resources.jsonl"),
                    "--stop-file",
                    str(stop_file),
                    "--max-seconds",
                    str(sum(case[4] + 40 for case in cases) + 60),
                ],
                stdin=subprocess.DEVNULL,
            )
        for name, rate, workers, maximum, seconds in cases:
            result = private / (name + ".bin")
            rate_value = f"{rate}/s" if rate else "0"
            print(
                f"开始 {name}：速率 {rate_value}，worker {workers}，上限 {maximum}，{seconds} 秒",
                flush=True,
            )
            subprocess.run(
                [
                    str(binary),
                    "attack",
                    "-format=json",
                    "-targets=" + str(targets),
                    "-output=" + str(result),
                    "-name=" + name,
                    "-rate=" + rate_value,
                    f"-duration={seconds}s",
                    f"-workers={workers}",
                    f"-max-workers={maximum}",
                    f"-max-connections={maximum}",
                    f"-connections={maximum}",
                    "-redirects=0",
                    "-timeout=5s",
                    "-max-body=1048576",
                    "-http2=false",
                ],
                check=True,
                timeout=seconds + 30,
            )
            metrics = json.loads(
                subprocess.check_output(
                    [str(binary), "report", "-type=json", str(result)], text=True
                )
            )
            validation = validate_results(binary, result)
            case = {
                "name": name,
                "offeredRate": rate_value,
                "initialWorkers": workers,
                "maximumWorkers": maximum,
                "durationSeconds": seconds,
                "metrics": metrics,
                "businessValidation": validation,
            }
            report["cases"].append(case)
            write_json(public / (name + ".json"), case)
            (public / (name + ".txt")).write_text(
                subprocess.check_output([str(binary), "report", str(result)], text=True),
                encoding="utf-8",
            )
            with (public / (name + ".html")).open("wb") as output:
                subprocess.run(
                    [str(binary), "plot", str(result)], stdout=output, check=True, timeout=40
                )
            print(
                json.dumps(
                    {
                        "case": name,
                        "requests": metrics["requests"],
                        "rps": round(metrics["throughput"], 2),
                        "p95Ms": round(metrics["latencies"]["95th"] / 1_000_000, 2),
                        "businessPassed": validation["passed"],
                    }
                ),
                flush=True,
            )
            if not validation["passed"]:
                report["stoppedAfterFailure"] = name
                break
            time.sleep(2)
        report["passed"] = len(report["cases"]) == len(cases) and all(
            case["businessValidation"]["passed"] for case in report["cases"]
        )
    finally:
        if monitor is not None:
            stop_file.touch()
            try:
                code = monitor.wait(timeout=10)
            except subprocess.TimeoutExpired:
                monitor.kill()
                code = monitor.wait()
            report["monitorPassed"] = code == 0
        report["sessionCleanup"] = [] if args.keep_sessions else cleanup_sessions(origin, sessions)
        report["cleanupPassed"] = (
            args.keep_sessions
            or bool(sessions)
            and all(item.get("status") == 200 for item in report["sessionCleanup"])
        )
        report["sessionsKept"] = args.keep_sessions
        report["finishedAt"] = datetime.now(UTC).isoformat()
        write_json(public / "summary.json", report)
        (private / "targets.jsonl").unlink(missing_ok=True)
    print(f"结果：{public}；仅可对外提供 reports，private 原始响应不应外传。")
    if (
        not report.get("passed")
        or not report["cleanupPassed"]
        or report.get("monitorPassed") is False
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print(f"压测已停止：{error}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, subprocess.SubprocessError) as error:
        # HTTPError 的 URL 可能来自登录流程；只输出类型，避免暴露会话或票据。
        print(
            f"压测已停止：{type(error).__name__}；请检查参数、会话、依赖及结果目录。",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
