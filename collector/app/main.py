"""采集与查询 API。"""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from . import db as dbmod
from . import service

HOUR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}$")


def _check_hour(hour: str) -> str:
    hour = hour.strip()
    if not HOUR_RE.match(hour):
        raise HTTPException(400, "hour must be a UTC hour like '2026-11-18T10'")
    try:
        datetime.strptime(hour, "%Y-%m-%dT%H")
    except ValueError:
        raise HTTPException(400, f"invalid UTC hour: {hour}")
    return hour


@asynccontextmanager
async def lifespan(app: FastAPI):
    dbmod.init()
    yield


app = FastAPI(title="PM2.5 Revision Collector", lifespan=lifespan)


class CollectRequest(BaseModel):
    hour: str


@app.get("/")
def index():
    return {"service": "pm25-collector", "docs": "/docs"}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/collections")
def create_collection(req: CollectRequest):
    """采集某 UTC 小时:先固定源站当前版,再逐版补齐到该版。"""
    hour = _check_hour(req.hour)
    try:
        return service.collect_hour(hour)
    except service.HourBusy:
        raise HTTPException(409, f"collection already running for hour {hour}")


@app.get("/collections/{hour}")
def collection_status(hour: str):
    hour = _check_hour(hour)
    db = dbmod.connect()
    try:
        row = dbmod.get_hour(db, hour)
        if row is None:
            raise HTTPException(404, f"hour {hour} never collected")
        return dict(row)
    finally:
        db.close()


@app.get("/hours")
def list_hours():
    db = dbmod.connect()
    try:
        rows = db.execute("SELECT * FROM hours ORDER BY hour").fetchall()
        return {"hours": [dict(r) for r in rows]}
    finally:
        db.close()


@app.get("/hours/{hour}/versions")
def list_versions(hour: str):
    hour = _check_hour(hour)
    db = dbmod.connect()
    try:
        row = dbmod.get_hour(db, hour)
        if row is None:
            raise HTTPException(404, f"hour {hour} never collected")
        vers = db.execute(
            "SELECT * FROM versions WHERE hour=? ORDER BY version", (hour,)
        ).fetchall()
        # 各版是否留存了源站原始操作(升级前确认的老版本可能没有)
        retained = {
            r["version"]: r["n"]
            for r in db.execute(
                "SELECT version, COUNT(*) AS n FROM raw_ops WHERE hour=? GROUP BY version",
                (hour,),
            ).fetchall()
        }
        return {
            "hour": hour,
            "current_version": row["current_version"],
            "versions": [
                {**dict(v), "raw_ops_retained": retained.get(v["version"], 0) > 0}
                for v in vers
            ],
        }
    finally:
        db.close()


@app.get("/hours/{hour}/data")
def hour_data(
    hour: str,
    version: Optional[int] = Query(default=None, description="缺省取当前版指针"),
    station_id: Optional[str] = Query(default=None),
):
    """查询某已确认版当时的完整数据。"""
    hour = _check_hour(hour)
    db = dbmod.connect()
    try:
        row = dbmod.get_hour(db, hour)
        if row is None:
            raise HTTPException(404, f"hour {hour} never collected")
        v = version if version is not None else row["current_version"]
        if v == 0:
            raise HTTPException(404, f"hour {hour} has no confirmed version yet")
        vrow = db.execute(
            "SELECT * FROM versions WHERE hour=? AND version=?", (hour, v)
        ).fetchone()
        if vrow is None:
            raise HTTPException(404, f"version {v} of hour {hour} is not confirmed")
        if station_id is not None:
            rows = db.execute(
                "SELECT station_id, pm25 FROM entries WHERE hour=? AND version=? AND station_id=?",
                (hour, v, station_id),
            ).fetchall()
            if not rows:
                raise HTTPException(404, f"station {station_id} not present in version {v}")
        else:
            rows = db.execute(
                "SELECT station_id, pm25 FROM entries WHERE hour=? AND version=? "
                "ORDER BY station_id",
                (hour, v),
            ).fetchall()
        return {
            "hour": hour,
            "version": v,
            "is_current": v == row["current_version"],
            "station_count": len(rows),
            "stations": [{"station_id": r["station_id"], "pm25": r["pm25"]} for r in rows],
        }
    finally:
        db.close()


SOURCE_DID_NOT_RETAIN = "来源未留存"


@app.get("/hours/{hour}/stations/{station_id}/trajectory")
def station_trajectory(hour: str, station_id: str):
    """按 UTC 小时 + 站点查修订轨迹。

    轨迹只来自采集时如实保存的源站原始操作(raw_ops),含同值 upsert、
    操作前后值与原因编号。已确认但没有原始操作留存的版本(例如升级前的
    老数据)只明确标注"来源未留存",绝不依据快照差额补造操作。
    缺页/抓取失败/各页原因不一致的版本根本不会确认,故不会出现在这里。
    """
    hour = _check_hour(hour)
    db = dbmod.connect()
    try:
        row = dbmod.get_hour(db, hour)
        if row is None:
            raise HTTPException(404, f"hour {hour} never collected")
        current_version = row["current_version"]
        confirmed = db.execute(
            "SELECT version, station_count FROM versions WHERE hour=? ORDER BY version",
            (hour,),
        ).fetchall()
        if not confirmed:
            raise HTTPException(404, f"hour {hour} has no confirmed version yet")

        events_rows = db.execute(
            "SELECT version, seq, op, pm25, before_pm25, after_pm25, reason_code, page "
            "FROM raw_ops WHERE hour=? AND station_id=? ORDER BY version, seq",
            (hour, station_id),
        ).fetchall()

        # 整版是否留存过任何原始操作:一行都没有的确认版才是"来源未留存"
        # (新版确认但该站本版无操作,不属于未留存,只是没有该站事件)
        versions_with_raw = {
            r["version"]
            for r in db.execute(
                "SELECT DISTINCT version FROM raw_ops WHERE hour=?", (hour,)
            ).fetchall()
        }

        # 当前确认快照中该站读数(照常可用的读数查询口径)
        cur = db.execute(
            "SELECT pm25 FROM entries WHERE hour=? AND version=? AND station_id=?",
            (hour, current_version, station_id),
        ).fetchone()
        ever_in_snapshot = db.execute(
            "SELECT 1 FROM entries WHERE hour=? AND station_id=? LIMIT 1",
            (hour, station_id),
        ).fetchone()

        # 该站在所有确认版中既无原始操作、也从未出现在快照中:视为未知站点
        if not events_rows and ever_in_snapshot is None:
            raise HTTPException(
                404, f"station {station_id} has no record in hour {hour}"
            )

        events = [
            {
                "version": r["version"],
                "seq": r["seq"],
                "op": r["op"],
                "pm25": r["pm25"],
                "before_pm25": r["before_pm25"],
                "after_pm25": r["after_pm25"],
                "reason_code": r["reason_code"],
                "source_page": r["page"],
            }
            for r in events_rows
        ]
        # 已确认但整版未留存原始操作的版本:明确告知,不补造
        gaps = [
            {
                "version": v["version"],
                "retained": False,
                "notice": SOURCE_DID_NOT_RETAIN,
            }
            for v in confirmed
            if v["version"] not in versions_with_raw
        ]
        return {
            "hour": hour,
            "station_id": station_id,
            "current_version": current_version,
            "current_pm25": None if cur is None else cur["pm25"],
            "events": events,
            "unretained_versions": gaps,
            "notice": SOURCE_DID_NOT_RETAIN if gaps else None,
        }
    finally:
        db.close()
