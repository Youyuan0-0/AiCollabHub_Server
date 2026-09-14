import argparse
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nginx-root", required=True, type=Path)
    parser.add_argument("--user-dist", type=Path, default=Path("../AiCollabHub_User/dist"))
    parser.add_argument("--admin-dist", type=Path, default=Path("../AiCollabHub_Admin/dist"))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--api-a", type=int, default=8000)
    parser.add_argument("--api-b", type=int, default=8001)
    args = parser.parse_args()
    for path in (args.user_dist, args.admin_dist):
        if not (path / "index.html").is_file():
            parser.error("Both frontends must be built before rendering the gateway")
        if any(c in str(path.resolve()) for c in ['"', "$", "\n", "\r"]):
            parser.error("Unsupported path characters")
    template = Path("deploy/nginx/nginx.conf.template").read_text(encoding="utf-8")
    for key, value in {
        "USER_DIST": args.user_dist.resolve().as_posix(),
        "ADMIN_DIST": args.admin_dist.resolve().as_posix(),
        "GATEWAY_PORT": args.port,
        "API_A": args.api_a,
        "API_B": args.api_b,
    }.items():
        template = template.replace(f"@@{key}@@", str(value))
    conf = args.nginx_root / "conf"
    conf.mkdir(parents=True, exist_ok=True)
    (args.nginx_root / "logs").mkdir(exist_ok=True)
    (conf / "nginx.conf").write_text(template, encoding="utf-8")
    shutil.copyfile("deploy/nginx/platform-proxy.conf", conf / "platform-proxy.conf")
    print("Gateway configuration rendered. Run nginx -t before starting it.")


if __name__ == "__main__":
    main()
