"""离线诊断的有界网络探测、脱敏及认证流程归并，不依赖应用或数据库。"""

from __future__ import annotations

import html
import json
import queue
import re
import ssl
import threading
import time
from collections.abc import Callable
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

SENSITIVE = re.compile(
    r"password|passwd|secret|token|cookie|credential|authorization|ticket|csrf", re.I
)
SAFE_FLAGS = {"ticket_present", "state_present", "binding_cookie_present", "session_cookie_present"}
STAGES = (
    ("login", "redirect_to_iam", "发起新登录"),
    ("callback", "received", "收到登录回调"),
    ("binding", "accepted", "校验浏览器登录绑定"),
    ("cas_validate", "success", "IAM 验票成功"),
    ("identity", "resolved", "解析并取得平台用户"),
    ("callback", "session_created", "创建平台会话"),
    ("session", "accepted", "浏览器携带会话访问成功"),
)
REASONS = {
    "invalid_callback_parameters": (
        "首页登录参数缺失、重复或格式错误；核对网关保留的 ticket、state、scope 后重新登录。"
    ),
    "login_request_expired": "登录绑定未通过：核对绑定 Cookie、state、有效期、重复回调及 Redis。",
    "invalid_ticket": "IAM 拒绝票据：核对原始 service 是否一致、票据是否过期或被重复使用。",
    "ticket_already_closed": "票据已使用或已撤销，必须发起新的登录，不要重放旧 ticket。",
    "timeout": "验票请求超时：核对平台服务器到 IAM 的 DNS、路由、端口及 IAM 响应。",
    "network_error": "验票网络异常：核对 DNS、连接、证书与代理。",
    "http_status": "IAM 验票返回非成功 HTTP 状态，核对接口路径、网关策略及服务状态。",
    "invalid_response": "验票响应无法解析，核对是否返回登录页/网关 HTML 或不符合约定的 JSON/XML。",
    "missing_work_no": "验票缺少稳定用户编号，核对 workNo 字段映射。",
    "iam_unavailable": "查看同一请求编号的 cas_validate 原因；此提示不能单独判定 IAM 宕机。",
    "admin_not_allowed": "账号已认证但没有管理员权限；普通用户访问管理端返回 403 是预期行为。",
    "account_unavailable": "平台账号停用或授权不可用，核对用户状态。",
}


def safe_url(value: str) -> str:
    """保留地址主机和路径；所有查询值、用户信息及片段均不导出。"""
    try:
        target = urlsplit(value)
        host = target.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if target.port:
            host += ":" + str(target.port)
        keys = [re.sub(r"[^a-zA-Z0-9_-]", "", key)[:40] for key in parse_qs(target.query)]
        return urlunsplit(
            (
                target.scheme,
                host,
                target.path[:1000],
                "&".join(k + "=[hidden]" for k in keys[:20]),
                "",
            )
        )
    except ValueError:
        return "[invalid URL]"


def redact(value: object, secrets: tuple[str, ...] = ()) -> str:
    """清除已知部署凭据、URL 查询值、编码票据和认证头，限制扫描与输出大小。"""
    text = str(value)[:16000]
    for _ in range(2):
        text = unquote(text)
    for secret in secrets:
        if len(secret) >= 4:
            text = text.replace(secret, "[hidden]")
    text = re.sub(
        r"(?i)\b(?:set-cookie|cookie|authorization|proxy-authorization)\s*[:=][^\r\n]*",
        "[hidden header]",
        text,
    )
    text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[^\s,;]+", "[hidden authorization]", text)
    text = re.sub(r"https?://[^\s\"'<>]+", lambda match: safe_url(match[0]), text)
    pattern = (
        r"(?i)\b([\w-]*(?:password|passwd|secret|token|cookie|ticket|csrf|credential)"
        r"[\w-]*|state|code)[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"
    )
    text = re.sub(
        pattern,
        lambda m: (
            m[0] if m[1] in SAFE_FLAGS and re.search(r"[=:][01]$", m[0]) else m[1] + "=[hidden]"
        ),
        text,
    )
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)[:4000]


def bounded[T](action: Callable[[], T], seconds: float = 12) -> T:
    """为 DNS、TLS 和正文读取设置总时限；不让系统解析器卡住整份诊断。"""
    result = queue.Queue(maxsize=1)

    def run():
        try:
            result.put((True, action()))
        except Exception as error:
            result.put((False, error))

    threading.Thread(target=run, daemon=True).start()
    try:
        ok, value = result.get(timeout=seconds)
    except queue.Empty:
        raise TimeoutError("probe_deadline") from None
    if not ok:
        raise value
    return value


class NoRedirect(HTTPRedirectHandler):
    """保留首跳证据，不自动进入真实 IAM 或消费一次性票据。"""

    def redirect_request(
        self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        """不生成下一跳请求，让调用方读取重定向响应。"""
        return None


def http_probe(url: str, *, host: str = "", ca_file: str | None = None) -> dict:
    """读取有限正文并仅输出结构；响应 Cookie 只保留名称和安全属性。"""
    started = time.monotonic()

    def request() -> dict:
        context = ssl.create_default_context(cafile=ca_file)
        opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
        headers = {"X-AIH-Diagnostic-Probe": "1", "Cache-Control": "no-cache"}
        if host:
            headers["Host"] = host
        try:
            response = opener.open(Request(url, headers=headers), timeout=5)
        except HTTPError as error:
            response = error
        with response:
            body = response.read(65537)
            location = response.headers.get("Location", "")
            result = {
                "url": safe_url(url),
                "status": response.status,
                "contentType": response.headers.get_content_type(),
                "requestId": response.headers.get("X-Request-ID", "")[:100],
                "bytesRead": len(body),
                "truncated": len(body) > 65536,
            }
            cookies = []
            for header in response.headers.get_all("Set-Cookie", []):
                parts = [part.strip() for part in header.split(";")]
                cookies.append(
                    {
                        "name": parts[0].split("=", 1)[0],
                        "secure": "secure" in [p.lower() for p in parts],
                        "httpOnly": "httponly" in [p.lower() for p in parts],
                    }
                )
            if cookies:
                result["cookieAttributes"] = cookies
            if location:
                result["location"] = safe_url(location)
                service = parse_qs(urlsplit(location).query).get("service", [""])[0]
                if service:
                    result["service"] = safe_url(service)
                    result["servicePath"] = urlsplit(service).path
                    result["serviceOrigin"] = safe_url(
                        urlunsplit((*urlsplit(service)[:2], "", "", ""))
                    )
                    result["serviceQueryKeys"] = sorted(parse_qs(urlsplit(service).query))
            if result["contentType"] == "application/json":
                try:
                    payload = json.loads(body)
                    if isinstance(payload, dict):
                        result["jsonKeys"] = sorted(payload)
                        for key in ("enabled", "provider", "status", "detail"):
                            value = payload.get(key)
                            if isinstance(value, bool) or (
                                isinstance(value, str) and re.fullmatch(r"[a-z_]{1,60}", value)
                            ):
                                result[key if key != "status" else "healthStatus"] = value
                except ValueError:
                    result["invalidJson"] = True
            elif result["contentType"] == "text/html":
                document = body.decode("utf-8", "replace")
                result["assets"] = re.findall(r'(?:src|href)="([^"?#]+\.(?:js|css))"', document)[
                    :15
                ]
                result["diagnosticPage"] = "aih-browser-diagnostics-v1" in document
            return result

    try:
        result = bounded(request)
    except Exception as error:
        result = {"url": safe_url(url), "errorType": type(error).__name__}
        cause = getattr(error, "reason", None)
        if cause is not None:
            result["causeType"] = type(cause).__name__
    result["elapsedMs"] = round((time.monotonic() - started) * 1000)
    return result


def parse_auth(message: str) -> dict | None:
    """只提取固定认证字段，忽略日志中的用户、票据和任意附加内容。"""
    if "auth stage=" not in message:
        return None
    allowed = {
        "stage",
        "result",
        "reason",
        "status",
        "duration_ms",
        "request_id",
        "flow_id",
        "scope",
        "session_ref",
        "service_hash",
        "probe",
        "global_logout",
        *SAFE_FLAGS,
    }
    return {
        key: value
        for key, value in re.findall(r"\b([a-z_]+)=([a-zA-Z0-9_.-]+)", message)
        if key in allowed
    }


def summarize_flows(events: list[dict]) -> list[dict]:
    """按状态摘要和请求编号关联跨请求阶段；不把不同用户的日志拼成成功。"""
    requests, sessions, flows = {}, {}, {}
    for event in events:
        flow = event.get("flow_id", "-")
        if flow != "-":
            requests[event.get("request_id")] = flow
            if event.get("session_ref"):
                sessions[event["session_ref"]] = flow
            flows.setdefault(flow, [])
    for event in events:
        flow = requests.get(event.get("request_id")) or sessions.get(event.get("session_ref"))
        if flow in flows:
            flows[flow].append(event)
    summaries = []
    for flow, records in flows.items():
        if any(e.get("probe") == "1" for e in records):
            continue
        failed = [e for e in records if e.get("result") in {"failed", "rejected"}]
        steps = []
        stages = list(STAGES)
        if any(e.get("stage") == "home_callback" for e in records):
            stages.insert(1, ("home_callback", "forwarded", "首页票据转交"))
        for stage, expected, label in stages:
            observed = [
                e for e in records if e.get("stage") == stage and e.get("result") == expected
            ]
            steps.append(
                {
                    "stage": stage,
                    "expected": expected,
                    "name": label,
                    "status": "PASS" if observed else "NOT_OBSERVED",
                    "requestIds": sorted({e.get("request_id", "") for e in observed}),
                }
            )
        outcomes = {(step["stage"], step["expected"]): step["status"] for step in steps}
        if failed:
            reason = failed[0].get("reason", "unknown")
            conclusion = REASONS.get(reason, "认证失败，查看此流程的阶段、请求编号和异常记录。")
            failed_stage = (
                "binding" if reason == "login_request_expired" else failed[0].get("stage")
            )
            failed_at = next(
                (
                    i
                    for i, step in enumerate(steps)
                    if step["stage"] == failed_stage and step["status"] != "PASS"
                ),
                None,
            )
            if failed_at is not None:
                steps[failed_at]["status"] = "FAIL"
                for step in steps[failed_at + 1 :]:
                    if step["status"] != "PASS":
                        step["status"] = "NOT_EXECUTED"
        elif outcomes[("session", "accepted")] == "PASS":
            conclusion = (
                "已观察到后端创建会话及后续携带该会话的访问；业务页面是否渲染须对照浏览器报告。"
            )
        elif outcomes[("callback", "session_created")] == "PASS":
            conclusion = (
                "后端已创建会话；尚未观察到携带该会话的访问，"
                "核对 Cookie、浏览器请求及收集时间范围。"
            )
        elif outcomes[("callback", "received")] != "PASS":
            conclusion = (
                "已发起登录但未观察到对应回调；对照浏览器返回地址和网关记录，"
                "不能据此判定 IAM 验票失败。"
            )
        else:
            conclusion = (
                "流程尚不完整；对照最后一个已观察阶段及同一请求编号的服务日志，"
                "不把缺少日志直接当成失败。"
            )
        summaries.append(
            {
                "flowId": flow,
                "steps": steps,
                "failures": failed,
                "conclusion": conclusion,
                "events": records,
            }
        )
    return summaries


def report_html(report: dict) -> str:
    """生成可离线打开的中文报告，所有外部内容按文本转义。"""
    checks = "".join(
        "<tr>"
        + "".join(
            "<td>" + html.escape(str(row.get(key, ""))) + "</td>"
            for key in ("name", "status", "expected", "actual", "next")
        )
        + "</tr>"
        for row in report.get("checks", [])
    )
    flows = "".join(
        "<h3>流程 "
        + html.escape(flow["flowId"])
        + "</h3><p>"
        + html.escape(flow["conclusion"])
        + "</p><pre>"
        + html.escape(json.dumps(flow, ensure_ascii=False, indent=2))
        + "</pre>"
        for flow in report.get("flows", [])
    )
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width"><title>平台诊断报告</title><style>'
        "body{font:15px/1.6 Arial,sans-serif;color:#20252c;max-width:1200px;"
        "margin:32px auto;padding:0 20px}table{width:100%;border-collapse:collapse}"
        "td,th{border:1px solid #cdd2d8;padding:8px;text-align:left;word-break:break-word}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f6f8;padding:16px}"
        "h1{font-size:25px}</style><h1>人工智能协同平台诊断报告</h1>"
        "<p>未观察到表示当前记录不足；不等同于失败。浏览器与服务器时间可能存在偏差。</p>"
        "<table><tr><th>检查</th><th>结果</th><th>预期</th><th>实际</th><th>下一步</th></tr>"
        + checks
        + "</table>"
        + flows
        + "<h2>完整脱敏明细</h2><pre>"
        + html.escape(json.dumps(report, ensure_ascii=False, indent=2))
        + "</pre></html>"
    )
