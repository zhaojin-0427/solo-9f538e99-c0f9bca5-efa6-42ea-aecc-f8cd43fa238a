#!/usr/bin/env python3
"""端到端冒烟测试:对运行中的 source + collector 验证全部关键行为。

用法:
    python3 scripts/smoke.py                 # 默认打 http://localhost:8000 / 8001
    COLLECTOR_URL=... SOURCE_URL=... python3 scripts/smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

COLLECTOR = os.environ.get("COLLECTOR_URL", "http://localhost:8000").rstrip("/")
SOURCE = os.environ.get("SOURCE_URL", "http://localhost:8001").rstrip("/")
# 默认用进程启动秒数派生唯一 UTC 小时(采集端只做格式校验,不查日历),
# 避免与库里历史采集数据冲突;可用 SMOKE_HOUR 固定
HOUR = os.environ.get("SMOKE_HOUR") or time.strftime(
    "%Y-%m-%dT", time.gmtime()) + f"{int(time.time()) % 24:02d}"
_checks = 0


def req(method: str, base: str, path: str, body=None, expect: int | None = 200):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            code, payload = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    if expect is not None and code != expect:
        raise AssertionError(f"{method} {url} -> {code}, want {expect}: {payload[:400]!r}")
    return code, (json.loads(payload) if payload else {})


def check(name: str, cond: bool, extra: str = "") -> None:
    global _checks
    if not cond:
        raise AssertionError(f"CHECK FAILED: {name} {extra}")
    _checks += 1
    print(f"  ok [{_checks:02d}] {name}")


def stations(base: str, hour: str, version: int | None = None) -> dict:
    q = f"?version={version}" if version is not None else ""
    _, data = req("GET", base, f"/hours/{hour}/data{q}")
    return {s["station_id"]: s["pm25"] for s in data["stations"]}


def main() -> int:
    print(f"== smoke: collector={COLLECTOR} source={SOURCE} hour={HOUR}")

    print("-- 0. 健康检查")
    req("GET", SOURCE, "/healthz")
    req("GET", COLLECTOR, "/healthz")

    print("-- 1. 重置源站,采集首版(全量)")
    _, reset = req("POST", SOURCE, "/admin/reset", {"station_count": 20, "seed": 20261118})
    check("reset -> current=1", reset["current_version"] == 1)
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("collect v1 ok", r["status"] == "ok" and r["confirmed_to"] == 1, json.dumps(r))
    v1 = stations(COLLECTOR, HOUR, 1)
    check("v1 全量 20 站", len(v1) == 20)
    check("默认查询=当前版指针", stations(COLLECTOR, HOUR) == v1)

    print("-- 2. 源站发布 v2/v3(增量),逐版补齐")
    _, p2 = req("POST", SOURCE, "/admin/publish")
    _, p3 = req("POST", SOURCE, "/admin/publish")
    deleted_v2 = [o["station_id"] for o in p2["ops"] if o["op"] == "delete"]
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("补齐到 v3", r["status"] == "ok" and r["confirmed_from"] == 1 and r["confirmed_to"] == 3,
          json.dumps(r))
    v2, v3 = stations(COLLECTOR, HOUR, 2), stations(COLLECTOR, HOUR, 3)
    check("v2 删除了目标站", all(s not in v2 for s in deleted_v2) and deleted_v2,
          f"deleted={deleted_v2}")
    check("v1 仍含被删站(已确认版均可查当时数据)", all(s in v1 for s in deleted_v2))
    check("v3 含新站 N03", "N03" in v3 and "N03" not in v1)
    check("v1 数据未被后续版本污染", stations(COLLECTOR, HOUR, 1) == v1)

    print("-- 3. 重复采集:已是最新")
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("up_to_date", r["status"] == "up_to_date" and r["current_version"] == 3, json.dumps(r))

    print("-- 4. 超时故障:一次性,重试后成功(重试不混残页)")
    req("PUT", SOURCE, "/admin/faults",
        {"faults": [{"version": 4, "page": 1, "kind": "timeout", "times": 1}]})
    req("POST", SOURCE, "/admin/publish")
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("v4 重试后成功", r["status"] == "ok" and r["confirmed_to"] == 4, json.dumps(r))
    check("v4 第 2 次尝试才成功", r["attempts"].get("4") == 2, json.dumps(r["attempts"]))

    print("-- 5. 缺页故障:持续缺页 -> 保留原供数版")
    req("PUT", SOURCE, "/admin/faults",
        {"faults": [{"version": 5, "page": 1, "kind": "missing", "times": -1}]})
    req("POST", SOURCE, "/admin/publish")
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("v5 缺页采集失败", r["status"] == "failed" and "missing page" in (r["error"] or ""),
          json.dumps(r))
    check("指针保留在 v4", r["current_version"] == 4, json.dumps(r))
    _, cur = req("GET", COLLECTOR, f"/hours/{HOUR}/data")
    check("默认查询仍是 v4(未暴露半版)", cur["version"] == 4 and cur["is_current"])
    req("GET", COLLECTOR, f"/hours/{HOUR}/data?version=5", expect=404)
    check("v5 不可查", True)
    req("DELETE", SOURCE, "/admin/faults")
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("清障后补齐 v5", r["status"] == "ok" and r["confirmed_to"] == 5, json.dumps(r))

    print("-- 6. 同版同站冲突:保留原供数版")
    req("PUT", SOURCE, "/admin/faults",
        {"faults": [{"version": 6, "page": 1, "kind": "conflict", "times": -1}]})
    req("POST", SOURCE, "/admin/publish")
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("v6 冲突采集失败", r["status"] == "failed" and "conflict" in (r["error"] or "").lower(),
          json.dumps(r))
    check("指针保留在 v5", r["current_version"] == 5, json.dumps(r))
    req("GET", COLLECTOR, f"/hours/{HOUR}/data?version=6", expect=404)
    check("v6 不可查", True)
    req("DELETE", SOURCE, "/admin/faults")
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("清障后补齐 v6", r["status"] == "ok" and r["confirmed_to"] == 6, json.dumps(r))

    print("-- 7. 已确认版完整性:逐版快照与版本登记一致")
    _, vers = req("GET", COLLECTOR, f"/hours/{HOUR}/versions")
    check("确认版 1..6 连续", [v["version"] for v in vers["versions"]] == [1, 2, 3, 4, 5, 6])
    for v in vers["versions"]:
        snap = stations(COLLECTOR, HOUR, v["version"])
        check(f"v{v['version']} 快照站数={v['station_count']}", len(snap) == v["station_count"])

    print("-- 8. 源站回退保护")
    req("POST", SOURCE, "/admin/reset", {"station_count": 20, "seed": 1})
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("源站回退被拒绝", r["status"] == "source_regressed" and r["current_version"] == 6,
          json.dumps(r))
    check("回退后历史版仍可查", len(stations(COLLECTOR, HOUR, 6)) > 0)

    print("-- 9. 同值 upsert 与原因编号:原始操作必须留存(含前后值与原因)")
    req("POST", SOURCE, "/admin/reset", {"station_count": 20, "seed": 20261118})
    # 新 UTC 小时,独立轨迹(主小时 +1 小时,跨天进位由采集端仅做格式校验、不查日历)
    date_part, hh = HOUR.rsplit("T", 1)
    HOUR2 = f"{date_part}T{(int(hh) + 1) % 24:02d}"
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR2})
    check("新小时采集 v1 成功", r["status"] == "ok" and r["confirmed_to"] == 1, json.dumps(r))
    s01_v1 = stations(COLLECTOR, HOUR2, 1)["S01"]
    # 显式发布:同值 upsert(S01 值不变)+ 改值(S02)+ 删除(S03),修订级原因 77,
    # 其中 S02 用单条原因 88 覆盖
    s02_v1 = stations(COLLECTOR, HOUR2, 1)["S02"]
    s03_v1 = stations(COLLECTOR, HOUR2, 1)["S03"]
    ops = [
        {"station_id": "S01", "op": "upsert", "pm25": s01_v1},
        {"station_id": "S02", "op": "upsert", "pm25": round(s02_v1 + 1.5, 1),
         "reason_code": 88},
        {"station_id": "S03", "op": "delete"},
    ]
    _, p = req("POST", SOURCE, "/admin/publish", {"reason_code": 77, "ops": ops})
    check("源站回显修订级原因", p["reason_code"] == 77, json.dumps(p))
    check("单条原因覆盖修订级原因",
          next(o for o in p["ops"] if o["station_id"] == "S02")["reason_code"] == 88)
    # 分页信封必须带出原因编号
    _, pg1 = req("GET", SOURCE, f"/api/revisions/{p['version']}/pages/1")
    check("分页信封带原因编号", pg1["reason_code"] == 77, json.dumps(pg1))
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR2})
    check("v2 采集成功", r["status"] == "ok" and r["confirmed_to"] == 2, json.dumps(r))

    _, traj = req("GET", COLLECTOR, f"/hours/{HOUR2}/stations/S01/trajectory")
    check("同值 upsert 仍有轨迹事件(基线 + 同值修订各一条)",
          len(traj["events"]) == 2 and
          [e["op"] for e in traj["events"]] == ["upsert", "upsert"], json.dumps(traj))
    ev = next(e for e in traj["events"] if e["version"] == 2)
    check("同值 upsert 前后值相等且等于原读数",
          ev["before_pm25"] == s01_v1 and ev["after_pm25"] == s01_v1 and
          ev["pm25"] == s01_v1, json.dumps(ev))
    check("事件带修订级原因与来源页", ev["reason_code"] == 77 and ev["source_page"] == 1,
          json.dumps(ev))
    check("v1 基线事件操作前值为 NULL、原因为 NULL",
          traj["events"][0]["before_pm25"] is None and
          traj["events"][0]["reason_code"] is None, json.dumps(traj["events"][0]))
    check("同值 upsert 后快照读数不变", stations(COLLECTOR, HOUR2, 2)["S01"] == s01_v1)

    _, traj_s02 = req("GET", COLLECTOR, f"/hours/{HOUR2}/stations/S02/trajectory")
    s02_v2_event = next(e for e in traj_s02["events"] if e["version"] == 2)
    check("单条原因覆盖生效", s02_v2_event["reason_code"] == 88, json.dumps(traj_s02))
    check("改值前后值正确", s02_v2_event["before_pm25"] == s02_v1 and
          s02_v2_event["after_pm25"] == round(s02_v1 + 1.5, 1))
    _, traj_s03 = req("GET", COLLECTOR, f"/hours/{HOUR2}/stations/S03/trajectory")
    s03_delete = next(e for e in traj_s03["events"] if e["version"] == 2)
    check("删除事件 after 为 NULL、before 有值",
          s03_delete["op"] == "delete" and
          s03_delete["after_pm25"] is None and
          s03_delete["before_pm25"] == s03_v1 and
          traj_s03["current_pm25"] is None, json.dumps(traj_s03))
    check("当前读数口径与轨迹一致",
          traj["current_pm25"] == stations(COLLECTOR, HOUR2)["S01"])
    # v1 为正常确认版且有原始全量操作留存,不应出现未留存提示
    check("正常确认版无未留存提示", traj["unretained_versions"] == [] and traj["notice"] is None,
          json.dumps(traj))
    req("GET", COLLECTOR, f"/hours/{HOUR2}/stations/NOPE99/trajectory", expect=404)
    check("未知站点轨迹 404", True)
    _, vers2 = req("GET", COLLECTOR, f"/hours/{HOUR2}/versions")
    check("版本登记标注原始操作已留存",
          all(v["raw_ops_retained"] for v in vers2["versions"]), json.dumps(vers2))

    print("-- 10. 各页原因不一致:该版读数与轨迹都不确认,清障后可补齐")
    # 造一个多页版本(20 条 op,源站 PAGE_SIZE=8 -> 3 页),故障篡改第 1 页信封原因,
    # 采集端在取第 2 页时即可发现各页原因不一致
    multi_ops = [
        {"station_id": f"S{i:02d}", "op": "upsert", "pm25": 40.0 + i}
        for i in range(1, 21)
    ]
    _, p_multi = req("POST", SOURCE, "/admin/publish",
                     {"reason_code": 77, "ops": multi_ops})
    bad_v = p_multi["version"]
    check("故障版为多页版本", p_multi["total_pages"] >= 2, json.dumps(p_multi))
    req("PUT", SOURCE, "/admin/faults",
        {"faults": [{"version": bad_v, "page": 1, "kind": "reason_mismatch", "times": -1}]})
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR2})
    check("原因不一致采集失败", r["status"] == "failed" and
          "reason_code differs" in (r["error"] or ""), json.dumps(r))
    check("指针保留在 v2", r["current_version"] == 2, json.dumps(r))
    req("GET", COLLECTOR, f"/hours/{HOUR2}/data?version={bad_v}", expect=404)
    check("问题版读数不可查", True)
    _, traj_during = req("GET", COLLECTOR, f"/hours/{HOUR2}/stations/S01/trajectory")
    check("问题版轨迹不确认(无该版事件)",
          all(e["version"] != bad_v for e in traj_during["events"]), json.dumps(traj_during))
    req("DELETE", SOURCE, "/admin/faults")
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR2})
    check("清障后补齐问题版", r["status"] == "ok" and r["confirmed_to"] == bad_v, json.dumps(r))
    _, traj_after = req("GET", COLLECTOR, f"/hours/{HOUR2}/stations/S01/trajectory")
    check("补齐后轨迹只含真实操作(无失败尝试残页)",
          any(e["version"] == bad_v for e in traj_after["events"]) and
          traj_after["unretained_versions"] == [], json.dumps(traj_after))
    check("补齐后该版操作原因正确",
          all(e["reason_code"] == 77 for e in traj_after["events"]
              if e["version"] == bad_v), json.dumps(traj_after))

    print("-- 11. 重启持久化:成功版重启后读数与轨迹仍可查")
    # 通过环境变量由外层重启脚本驱动;未设置 RESTARTED 时跳过实际重启,
    # 仅校验当前数据齐全(restart_check.py 负责真正的重启验证)
    if os.environ.get("RESTARTED") == "1":
        _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR2})
        check("重启后状态可恢复查询(up_to_date)", r["status"] == "up_to_date", json.dumps(r))
        check("重启后读数照常可用", len(stations(COLLECTOR, HOUR2, bad_v)) > 0)
        _, traj_rb = req("GET", COLLECTOR, f"/hours/{HOUR2}/stations/S01/trajectory")
        check("重启后轨迹仍可查且含同值 upsert",
              any(e["op"] == "upsert" and e["before_pm25"] == e["after_pm25"]
                  for e in traj_rb["events"]), json.dumps(traj_rb))
        check("重启后未留存标记仍正确", traj_rb["unretained_versions"] == [])
    else:
        print("       (跳过实际重启,由 scripts/restart_check.py 验证)")

    print(f"== smoke 全部通过 ({_checks} 项检查)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
