#!/usr/bin/env python3
"""专项验证:已确认版没有原始操作记录时,轨迹必须明确返回"来源未留存",
且严禁根据快照差额补造操作。

做法:用升级前的旧表结构(hours/versions/entries,无 raw_ops)建一个 SQLite 库,
写入两个已确认版的快照(站点 S01 在两版间读数不同,制造快照差额),再以新代码
打开同一库(init 自动补建 raw_ops),验证:

  1. 原有读数查询照常可用;
  2. 轨迹 API 对两个老版本返回 unretained 标记与"来源未留存",events 为空,
     不会因为快照差额凭空造出 upsert;
  3. 之后采集的新版本正常留存原始操作(含同值 upsert),老版本的标记保持不变。

用法:python3 scripts/legacy_check.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))

OLD_SCHEMA = """
CREATE TABLE hours (
    hour TEXT PRIMARY KEY, current_version INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'idle', pinned_version INTEGER,
    last_error TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE versions (
    hour TEXT NOT NULL, version INTEGER NOT NULL, total_pages INTEGER NOT NULL,
    item_count INTEGER NOT NULL, station_count INTEGER NOT NULL,
    confirmed_at TEXT NOT NULL, PRIMARY KEY (hour, version)
);
CREATE TABLE entries (
    hour TEXT NOT NULL, version INTEGER NOT NULL, station_id TEXT NOT NULL,
    pm25 REAL NOT NULL, PRIMARY KEY (hour, version, station_id)
);
"""

HOUR = "2026-11-18T11"
checks = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global checks
    if not cond:
        raise AssertionError(f"CHECK FAILED: {name} {extra}")
    checks += 1
    print(f"  ok [{checks:02d}] {name}")


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class FakeClient:
    """替身:模拟源站 v3(含同值 upsert)及 current 接口。"""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, path: str):
        if path == "/api/revisions/current":
            return FakeResponse({"current_version": 3})
        if path == "/api/revisions/3/pages/1":
            return FakeResponse({
                "version": 3, "page": 1, "total_pages": 1, "reason_code": 55,
                "items": [
                    {"station_id": "S01", "op": "upsert", "pm25": 20.0,
                     "reason_code": 55},  # 与 v2 同值:同值 upsert
                    {"station_id": "S02", "op": "upsert", "pm25": 99.9,
                     "reason_code": 55},
                ],
            })
        raise AssertionError(f"unexpected source path: {path}")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="legacy-check-")
    db_path = os.path.join(tmp, "collector.db")

    print("-- 1. 用旧表结构造升级前数据库(只有快照,无 raw_ops)")
    old = sqlite3.connect(db_path)
    old.executescript(OLD_SCHEMA)
    old.execute(
        "INSERT INTO hours VALUES (?,?,?,?,?,?)",
        (HOUR, 2, "ok", 2, None, "2026-11-18T11:05:00+00:00"),
    )
    old.executemany(
        "INSERT INTO versions VALUES (?,?,?,?,?,?)",
        [
            (HOUR, 1, 1, 2, 2, "2026-11-18T11:00:00+00:00"),
            (HOUR, 2, 1, 1, 1, "2026-11-18T11:05:00+00:00"),
        ],
    )
    # 制造快照差额:S01 v1=10 -> v2=20;S02 在 v2 被删除(老库无法说明原因)
    old.executemany(
        "INSERT INTO entries VALUES (?,?,?,?)",
        [
            (HOUR, 1, "S01", 10.0),
            (HOUR, 1, "S02", 30.0),
            (HOUR, 2, "S01", 20.0),
        ],
    )
    old.commit()
    old.close()

    os.environ["DB_PATH"] = db_path

    from fastapi.testclient import TestClient

    from app import db as dbmod
    from app import service
    from app.main import app

    print("-- 2. 新代码打开旧库:init 补建 raw_ops,老数据原样保留")
    with TestClient(app) as client:
        pass  # 触发 lifespan -> dbmod.init()
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("raw_ops 表已补建", "raw_ops" in tables, str(tables))
    n_raw = conn.execute("SELECT COUNT(*) FROM raw_ops").fetchone()[0]
    check("旧库 raw_ops 为空(无法回填,也不补造)", n_raw == 0)
    conn.close()

    print("-- 3. 原有读数查询照常可用")
    with TestClient(app) as client:
        r = client.get(f"/hours/{HOUR}/data?version=1")
        check("v1 读数可查", r.status_code == 200 and
              {s["station_id"]: s["pm25"] for s in r.json()["stations"]}
              == {"S01": 10.0, "S02": 30.0}, r.text)
        r = client.get(f"/hours/{HOUR}/data?version=2")
        check("v2 读数可查(快照差额真实存在)", r.status_code == 200 and
              {s["station_id"]: s["pm25"] for s in r.json()["stations"]}
              == {"S01": 20.0}, r.text)
        r = client.get(f"/hours/{HOUR}/data")
        check("默认当前版查询照常", r.status_code == 200 and r.json()["version"] == 2)

        print("-- 4. 轨迹对老版本明确返回\"来源未留存\",不按快照差额补造")
        r = client.get(f"/hours/{HOUR}/stations/S01/trajectory")
        check("S01 轨迹 200", r.status_code == 200, r.text)
        body = r.json()
        check("events 为空(未补造 v1->v2 的 upsert)", body["events"] == [], str(body))
        check("两个老版本均标注未留存",
              [g["version"] for g in body["unretained_versions"]] == [1, 2], str(body))
        check("未留存提示文案为\"来源未留存\"",
              all(g["notice"] == "来源未留存" for g in body["unretained_versions"]) and
              body["notice"] == "来源未留存", str(body))
        check("当前读数仍取自快照(20.0)", body["current_pm25"] == 20.0)

        r = client.get(f"/hours/{HOUR}/stations/S02/trajectory")
        check("被删站 S02 同样不补造 delete 事件",
              r.status_code == 200 and r.json()["events"] == [] and
              [g["version"] for g in r.json()["unretained_versions"]] == [1, 2], r.text)
        check("S02 当前读数为 null(快照中已不存在)", r.json()["current_pm25"] is None)

        r = client.get(f"/hours/{HOUR}/versions")
        check("版本登记标注老版本未留存",
              [v["raw_ops_retained"] for v in r.json()["versions"]] == [False, False], r.text)

        print("-- 5. 升级后采集 v3:新原始操作正常留存,老标记不变")
        service.httpx.Client = FakeClient  # 注入源站替身
        r = client.post("/collections", json={"hour": HOUR})
        check("v3 采集成功", r.status_code == 200 and r.json()["confirmed_to"] == 3, r.text)

        r = client.get(f"/hours/{HOUR}/stations/S01/trajectory")
        body = r.json()
        check("v3 同值 upsert 被如实留存",
              len(body["events"]) == 1 and body["events"][0]["version"] == 3 and
              body["events"][0]["op"] == "upsert" and
              body["events"][0]["before_pm25"] == 20.0 and
              body["events"][0]["after_pm25"] == 20.0 and
              body["events"][0]["reason_code"] == 55, str(body))
        check("老版本仍标注未留存(不回填不补造)",
              [g["version"] for g in body["unretained_versions"]] == [1, 2], str(body))

        r = client.get(f"/hours/{HOUR}/stations/S02/trajectory")
        check("S02 出现 v3 真实操作,老版本依旧未留存",
              len(r.json()["events"]) == 1 and r.json()["events"][0]["version"] == 3 and
              [g["version"] for g in r.json()["unretained_versions"]] == [1, 2], r.text)

        r = client.get(f"/hours/{HOUR}/versions")
        check("留存标记逐版正确",
              [(v["version"], v["raw_ops_retained"]) for v in r.json()["versions"]]
              == [(1, False), (2, False), (3, True)], r.text)

    print(f"== legacy_check 全部通过 ({checks} 项检查)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
