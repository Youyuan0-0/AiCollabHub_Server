"""采样麒麟服务器 CPU、内存、TCP 和数据库连接，不读取业务密码或会话。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path


def cpu_times() -> list[int]:
    """读取系统 CPU 时间计数。"""
    return list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))


def memory() -> dict[str, int]:
    """读取可用内存和交换空间，单位为 KiB。"""
    result = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            result[key] = int(value.split()[0])
    return result


def connections(ports: dict[str, int]) -> dict[str, int]:
    """统计已建立 TCP 连接，包含空闲连接，不等于活动请求数。"""
    counts = {key: 0 for key in ports}
    for source in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in Path(source).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == "01":
                port = int(fields[1].rsplit(":", 1)[1], 16)
                for key, expected in ports.items():
                    if port == expected:
                        counts[key] += 1
    return counts


def database(config: dict, program_root: Path) -> dict:
    """通过本机 peer 认证统计连接，排除采样连接自身。"""
    query = """SELECT json_build_object(
        'total', count(*),
        'active', count(*) FILTER (WHERE state = 'active'),
        'idle', count(*) FILTER (WHERE state = 'idle'),
        'waiting', count(*) FILTER (WHERE state = 'active' AND wait_event IS NOT NULL))
        FROM pg_stat_activity WHERE datname = current_database()
        AND backend_type = 'client backend' AND pid <> pg_backend_pid()"""
    result = subprocess.run(
        [
            "runuser",
            "-u",
            "aih-db",
            "--",
            str(program_root / "runtime/postgres/bin/psql"),
            "-h",
            str(Path(config["data_dir"]) / "postgres/socket"),
            "-p",
            str(config["postgres_port"]),
            "-U",
            "postgres",
            "-d",
            config["postgres_db"],
            "-At",
            "-c",
            query,
        ],
        env={
            **os.environ,
            "LD_LIBRARY_PATH": f"{program_root}/runtime/lib:{program_root}/runtime/postgres/lib",
        },
        cwd="/",
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode:
        return {"error": "database_sampling_failed"}
    return json.loads(result.stdout)


def main() -> None:
    """每两秒写入 JSONL，停止文件出现或到达时限后退出。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--program-root", type=Path, default=Path("/opt/aicollabhub"))
    parser.add_argument("--max-seconds", type=int, default=900)
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(Path("/etc/aicollabhub/site.json").read_text())
    previous = cpu_times()
    started = time.monotonic()
    sampling_passed = True
    with args.output.open("w", encoding="utf-8") as stream:
        while not args.stop_file.exists() and time.monotonic() - started < args.max_seconds:
            current = cpu_times()
            delta = [after - before for after, before in zip(current, previous, strict=True)]
            total = sum(delta)
            value = {
                "utc": datetime.now(UTC).isoformat(),
                "cpuBusyPercent": round(100 * (total - delta[3] - delta[4]) / total, 2)
                if total
                else 0,
                "cpuIoWaitPercent": round(100 * delta[4] / total, 2) if total else 0,
                "memoryKiB": memory(),
                "loadAverage": list(os.getloadavg()),
                "connections": connections(
                    {"webEstablished": config["web_port"], "apiEstablished": config["api_port"]}
                ),
                "database": database(config, args.program_root),
            }
            sampling_passed = sampling_passed and "error" not in value["database"]
            stream.write(json.dumps(value) + "\n")
            stream.flush()
            previous = current
            time.sleep(2)
    if not sampling_passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
