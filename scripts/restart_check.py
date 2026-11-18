#!/usr/bin/env python3
"""重启持久化验证:真实 uvicorn 进程跑 source + collector,采集成功后重启
collector,验证成功版的读数查询与修订轨迹重启后仍可查,状态复位不丢数据。

用法:python3 scripts/restart_check.py
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable
BASE_PORT_SRC = 8101
BASE_PORT_COL = 8100
HOUR = time.strftime("%Y-%m-%dT", time.gmtime()) + f"{int(time.time()) % 24:02d}"

checks = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global checks
    if not cond:
        raise AssertionError(f"CHECK FAILED: {name} {extra}")
    checks += 1
    print(f"  ok [{checks:02d}] {name}")


def wait_ready(port: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.4)
    raise RuntimeError(f"service on {port} not ready: {last}")


def http(method: str, port: int, path: str, body=None):
    import json
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def start(service_dir: str, port: int, env: dict) -> subprocess.Popen:
    proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port)],
        cwd=os.path.join(ROOT, service_dir),
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    wait_ready(port)
    return proc


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="restart-check-")
    db_path = os.path.join(tmp, "collector.db")
    common_env = {"PYTHONPATH": os.path.join(ROOT, "collector")}

    print("-- 1. 启动 source + collector,完成采集")
    src = start("source", BASE_PORT_SRC, {**common_env,
                "PAGE_SIZE": "3", "STATION_COUNT": "10", "SEED": "7"})
    try:
        col = start("collector", BASE_PORT_COL, {
            **common_env,
            "SOURCE_BASE_URL": f"http://127.0.0.1:{BASE_PORT_SRC}",
            "DB_PATH": db_path,
            "PAGE_TIMEOUT_SECONDS": "3",
            "MAX_ATTEMPTS": "2",
            "RETRY_BACKOFF_SECONDS": "0.2",
        })
        try:
            code, r = http("POST", BASE_PORT_COL, "/collections", {"hour": HOUR})
            check("v1 采集成功", code == 200 and r["confirmed_to"] == 1, str(r))
            code, p = http("POST", BASE_PORT_SRC, "/admin/publish",
                           {"reason_code": 42, "ops": [
                               {"station_id": "S01", "op": "upsert", "pm25": 12.3}]})
            check("源站发布带原因修订", code == 200 and p["reason_code"] == 42, str(p))
            code, r = http("POST", BASE_PORT_COL, "/collections", {"hour": HOUR})
            check("v2 采集成功", code == 200 and r["confirmed_to"] == 2, str(r))
            code, traj_before = http(
                "GET", BASE_PORT_COL, f"/hours/{HOUR}/stations/S01/trajectory")
            v2_event = next(e for e in traj_before["events"] if e["version"] == 2)
            check("重启前轨迹可查(v2 事件原因/前后值完整)",
                  code == 200 and v2_event["reason_code"] == 42 and
                  v2_event["before_pm25"] == 52.0 and v2_event["after_pm25"] == 12.3,
                  str(traj_before))
            code, data_before = http("GET", BASE_PORT_COL, f"/hours/{HOUR}/data?version=1")
            check("重启前 v1 读数可查", code == 200 and data_before["station_count"] == 10)
        finally:
            print("-- 2. 杀掉 collector(模拟进程退出/宕机)")
            col.send_signal(signal.SIGTERM)
            col.wait(timeout=15)
    finally:
        src.send_signal(signal.SIGTERM)
        src.wait(timeout=15)

    print("-- 3. 用同一数据卷重启 collector(源站也重新拉起,版号从头)")
    src2 = start("source", BASE_PORT_SRC, {**common_env,
                 "PAGE_SIZE": "3", "STATION_COUNT": "10", "SEED": "7"})
    try:
        col2 = start("collector", BASE_PORT_COL, {
            **common_env,
            "SOURCE_BASE_URL": f"http://127.0.0.1:{BASE_PORT_SRC}",
            "DB_PATH": db_path,
        })
        try:
            code, data1 = http("GET", BASE_PORT_COL, f"/hours/{HOUR}/data?version=1")
            check("重启后 v1 读数照常可查", code == 200 and
                  data1["station_count"] == data_before["station_count"], str(data1))
            code, data2 = http("GET", BASE_PORT_COL, f"/hours/{HOUR}/data?version=2")
            check("重启后 v2 读数照常可查", code == 200 and
                  any(s["station_id"] == "S01" and s["pm25"] == 12.3
                      for s in data2["stations"]), str(data2))
            code, traj = http(
                "GET", BASE_PORT_COL, f"/hours/{HOUR}/stations/S01/trajectory")
            check("重启后轨迹仍可查且原因/前后值完整",
                  code == 200 and traj == traj_before, str(traj))
            code, vers = http("GET", BASE_PORT_COL, f"/hours/{HOUR}/versions")
            check("重启后确认版列表不变",
                  code == 200 and [v["version"] for v in vers["versions"]] == [1, 2],
                  str(vers))
            code, status = http("GET", BASE_PORT_COL, f"/collections/{HOUR}")
            check("重启后无残留 collecting 状态",
                  code == 200 and status["status"] != "collecting", str(status))
        finally:
            col2.send_signal(signal.SIGTERM)
            col2.wait(timeout=15)
    finally:
        src2.send_signal(signal.SIGTERM)
        src2.wait(timeout=15)

    print(f"== restart_check 全部通过 ({checks} 项检查)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
