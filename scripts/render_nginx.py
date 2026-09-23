"""根据前端产物路径与服务端口生成单机 Nginx 网关配置。"""

import argparse
import shutil
import sys
from pathlib import Path


def main() -> None:
    """校验部署参数并生成网关配置，支持按平台设置 worker 数量。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--nginx-root", required=True, type=Path)
    parser.add_argument("--user-dist", type=Path, default=Path("../AiCollabHub_User/dist"))
    parser.add_argument("--admin-dist", type=Path, default=Path("../AiCollabHub_Admin/dist"))
    parser.add_argument("--port", type=int, default=18880)
    parser.add_argument("--api-port", type=int, default=18000)
    parser.add_argument("--iam-callback-port", type=int)
    parser.add_argument("--iam-sync-port", type=int)
    # Windows Nginx 的多 worker 支持受限；Linux 部署默认利用多核。
    parser.add_argument("--nginx-workers", default="1" if sys.platform == "win32" else "auto")
    parser.add_argument("--listen-host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    args = parser.parse_args()
    if args.nginx_workers != "auto" and not (
        args.nginx_workers.isdigit() and 1 <= int(args.nginx_workers) <= 256
    ):
        parser.error("Nginx workers must be auto or an integer between 1 and 256")
    ingress_ports = (args.iam_callback_port, args.iam_sync_port)
    if (ingress_ports[0] is None) != (ingress_ports[1] is None):
        parser.error("IAM callback and sync ports must be configured together")
    ports = [args.port, args.api_port, *(port for port in ingress_ports if port is not None)]
    if not all(1 <= port <= 65535 for port in ports):
        parser.error("Ports must be between 1 and 65535")
    if len(set(ports)) != len(ports):
        parser.error("Gateway, API and IAM ingress ports must differ")
    for path in (args.user_dist, args.admin_dist):
        if not (path / "index.html").is_file():
            parser.error("Both frontends must be built before rendering the gateway")
        if any(c in str(path.resolve()) for c in ['"', "$", "\n", "\r"]):
            parser.error("Unsupported path characters")
    root = Path(__file__).resolve().parents[1]
    template = (root / "deploy/nginx/nginx.conf.template").read_text(encoding="utf-8")
    ingress = ""
    if args.iam_callback_port is not None:
        ingress = (root / "deploy/nginx/iam-ingress.conf.template").read_text(encoding="utf-8")
        for key, value in {
            "LISTEN_HOST": args.listen_host,
            "IAM_CALLBACK_PORT": args.iam_callback_port,
            "IAM_SYNC_PORT": args.iam_sync_port,
        }.items():
            ingress = ingress.replace(f"@@{key}@@", str(value))
    template = template.replace("@@IAM_INGRESS_SERVERS@@", ingress)
    for key, value in {
        "USER_DIST": args.user_dist.resolve().as_posix(),
        "ADMIN_DIST": args.admin_dist.resolve().as_posix(),
        "GATEWAY_PORT": args.port,
        "API_PORT": args.api_port,
        "NGINX_WORKERS": args.nginx_workers,
        "LISTEN_HOST": args.listen_host,
    }.items():
        template = template.replace(f"@@{key}@@", str(value))
    conf = args.nginx_root / "conf"
    conf.mkdir(parents=True, exist_ok=True)
    (args.nginx_root / "logs").mkdir(exist_ok=True)
    (conf / "nginx.conf").write_text(template, encoding="utf-8")
    shutil.copyfile(root / "deploy/nginx/platform-proxy.conf", conf / "platform-proxy.conf")
    print("Single-server gateway rendered. Run nginx -t before starting it.")


if __name__ == "__main__":
    main()
