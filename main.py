"""RackSense - Smart Weight-Sensing Retail Rack System.

FastAPI backend: authentication, rack registry, load-cell telemetry ingest,
the tare/delta/quantity engine, alerts, device command queue, Excel/CSV export,
live WebSocket feed and a built-in hardware simulator.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import database as store
import engine
from security import new_session_token, verify_password

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
SESSION_DAYS = 7
SIM_INTERVAL = float(os.environ.get("RACKSENSE_SIM_SECONDS", "7"))
ISO = "%d-%m-%Y %I:%M %p"
TZ = ZoneInfo(os.environ.get("RACKSENSE_TZ", "Asia/Kolkata"))


# --------------------------------------------------------------------------- #
# app + live connections
# --------------------------------------------------------------------------- #
app = FastAPI(title="RackSense API", version="1.0.0", docs_url="/api/docs", redoc_url=None)


class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.add(ws)

    async def drop(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        dead = []
        async with self.lock:
            clients = list(self.clients)
        for ws in clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.drop(ws)


hub = Hub()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime(ISO)


def _rack_payload(conn, row) -> dict:
    r = dict(row)
    cap = float(r["capacity_g"]) or 1.0
    weight = float(r["weight_g"])
    open_alerts = conn.execute(
        "SELECT COUNT(*) c FROM alerts WHERE rack_id=? AND state='open'", (r["id"],)
    ).fetchone()["c"]
    pct = max(0.0, min(120.0, weight / cap * 100))
    last_read = conn.execute(
        "SELECT ts FROM readings WHERE rack_id=? ORDER BY ts DESC LIMIT 1", (r["id"],)
    ).fetchone()
    last_seen = (last_read["ts"] if last_read else r["last_seen"]) or 0
    online = (time.time() - last_seen) < 90
    return {
        "id": r["id"],
        "code": r["code"],
        "name": r["name"],
        "location": r["location"],
        "tier": r["tier"],
        "capacity_kg": round(cap / 1000, 2),
        "tare_kg": round(float(r["tare_g"]) / 1000, 2),
        "avg_item_g": float(r["avg_item_g"]),
        "armed": bool(r["armed"]),
        "low_stock_pct": float(r["low_stock_pct"]),
        "weight_kg": round(weight / 1000, 3),
        "weight_g": round(weight, 1),
        "stock_pct": round(pct, 1),
        "cells_g": json.loads(r["cells_g"] or "[0,0,0,0]"),
        "device_key": r["device_key"],
        "online": online,
        "last_seen": last_seen,
        "open_alerts": open_alerts,
        "low_stock": pct <= float(r["low_stock_pct"]),
        "created_at": r["created_at"],
    }


def _event_payload(row) -> dict:
    e = dict(row)
    return {
        "id": e["id"],
        "rack_id": e["rack_id"],
        "rack_code": e.get("rack_code", ""),
        "kind": e["kind"],
        "prev_kg": round(float(e["prev_g"]) / 1000, 3),
        "curr_kg": round(float(e["curr_g"]) / 1000, 3),
        "delta_kg": round(float(e["delta_g"]) / 1000, 3),
        "qty": e["qty"],
        "status": e["status"],
        "note": e["note"],
        "ts": e["ts"],
        "time": _fmt(e["ts"]),
    }


def _alert_payload(row) -> dict:
    a = dict(row)
    return {
        "id": a["id"],
        "rack_id": a["rack_id"],
        "rack_code": a.get("rack_code", ""),
        "event_id": a["event_id"],
        "message": a["message"],
        "severity": a["severity"],
        "state": a["state"],
        "created_at": a["created_at"],
        "time": _fmt(a["created_at"]),
        "ack_by": a.get("ack_by"),
        "ack_at": a.get("ack_at"),
    }


def _overview(conn) -> dict:
    racks = conn.execute("SELECT * FROM racks ORDER BY code").fetchall()
    payload = [_rack_payload(conn, r) for r in racks]
    today_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    removals = conn.execute(
        "SELECT COALESCE(SUM(-delta_g),0) s FROM events WHERE delta_g<0 AND ts>=?",
        (today_start,),
    ).fetchone()["s"]
    removed_qty = conn.execute(
        "SELECT COALESCE(SUM(qty),0) s FROM events WHERE delta_g<0 AND ts>=?",
        (today_start,),
    ).fetchone()["s"]
    alerts_today = conn.execute(
        "SELECT COUNT(*) c FROM alerts WHERE created_at>=?", (today_start,)
    ).fetchone()["c"]
    open_alerts = conn.execute(
        "SELECT COUNT(*) c FROM alerts WHERE state='open'"
    ).fetchone()["c"]
    return {
        "racks_total": len(payload),
        "racks_online": sum(1 for r in payload if r["online"]),
        "racks_armed": sum(1 for r in payload if r["armed"]),
        "total_weight_kg": round(sum(r["weight_kg"] for r in payload), 2),
        "capacity_kg": round(sum(r["capacity_kg"] for r in payload), 2),
        "removed_kg_today": round(removals / 1000, 2),
        "items_removed_today": int(removed_qty or 0),
        "alerts_today": alerts_today,
        "open_alerts": open_alerts,
        "low_stock_racks": sum(1 for r in payload if r["low_stock"]),
    }


def snapshot() -> dict:
    with store.db() as conn:
        racks = [
            _rack_payload(conn, r)
            for r in conn.execute("SELECT * FROM racks ORDER BY code").fetchall()
        ]
        events = [
            _event_payload(r)
            for r in conn.execute(
                "SELECT e.*, r.code AS rack_code FROM events e JOIN racks r ON r.id=e.rack_id"
                " ORDER BY e.ts DESC, e.id DESC LIMIT 14"
            ).fetchall()
        ]
        alerts = [
            _alert_payload(r)
            for r in conn.execute(
                "SELECT a.*, r.code AS rack_code FROM alerts a JOIN racks r ON r.id=a.rack_id"
                " WHERE a.state!='resolved' ORDER BY a.created_at DESC LIMIT 20"
            ).fetchall()
        ]
        return {
            "type": "snapshot",
            "server_time": time.time(),
            "tz": str(TZ),
            "racks": racks,
            "events": events,
            "alerts": alerts,
            "overview": _overview(conn),
            "sim": {"enabled": sim.enabled},
        }


async def push_snapshot() -> None:
    await hub.broadcast(snapshot())


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
class LoginIn(BaseModel):
    username: str
    password: str


def current_user(request: Request) -> dict:
    token = ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.query_params.get("token", "")
    if not token:
        raise HTTPException(401, "Not signed in")
    now = time.time()
    with store.db() as conn:
        row = conn.execute(
            "SELECT s.token, s.expires_at, u.id, u.username, u.name, u.role"
            " FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
            (token,),
        ).fetchone()
        if not row or row["expires_at"] < now:
            raise HTTPException(401, "Session expired")
        return {
            "id": row["id"],
            "username": row["username"],
            "name": row["name"],
            "role": row["role"],
            "token": token,
        }


def owner_only(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "owner":
        raise HTTPException(403, "Only the shop owner can do that")
    return user


@app.post("/api/auth/login")
def login(body: LoginIn):
    with store.db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE lower(username)=lower(?)", (body.username.strip(),)
        ).fetchone()
        if not row or not verify_password(body.password, row["password_hash"]):
            raise HTTPException(401, "Wrong username or password")
        token = new_session_token()
        conn.execute(
            "INSERT INTO sessions (token,user_id,created_at,expires_at) VALUES (?,?,?,?)",
            (token, row["id"], time.time(), time.time() + SESSION_DAYS * 86400),
        )
        return {
            "token": token,
            "user": {"id": row["id"], "username": row["username"], "name": row["name"], "role": row["role"]},
        }


@app.post("/api/auth/demo")
def demo_login():
    """One-click demo session for the seeded shop owner (disable with RACKSENSE_DEMO=0)."""
    if os.environ.get("RACKSENSE_DEMO", "1") != "1":
        raise HTTPException(404, "Demo sign-in is disabled")
    with store.db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username='owner'").fetchone()
        if not row:
            raise HTTPException(503, "Demo shop is not seeded")
        token = new_session_token()
        conn.execute(
            "INSERT INTO sessions (token,user_id,created_at,expires_at) VALUES (?,?,?,?)",
            (token, row["id"], time.time(), time.time() + SESSION_DAYS * 86400),
        )
        return {
            "token": token,
            "user": {"id": row["id"], "username": row["username"], "name": row["name"], "role": row["role"]},
        }


@app.get("/api/auth/me")
def me(user: dict = Depends(current_user)):
    return {"user": user}


@app.post("/api/auth/logout")
def logout(user: dict = Depends(current_user)):
    with store.db() as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (user["token"],))
    return {"ok": True}


# --------------------------------------------------------------------------- #
# racks
# --------------------------------------------------------------------------- #
class RackIn(BaseModel):
    code: str | None = None
    name: str | None = None
    location: str | None = None
    tier: str | None = None
    capacity_kg: float | None = Field(default=None, gt=0, le=500)
    tare_kg: float | None = Field(default=None, ge=0, le=500)
    avg_item_g: float | None = Field(default=None, gt=0, le=20000)
    armed: bool | None = None
    low_stock_pct: float | None = Field(default=None, ge=1, le=90)


@app.get("/api/racks")
def list_racks(user: dict = Depends(current_user)):
    with store.db() as conn:
        return {
            "racks": [
                _rack_payload(conn, r)
                for r in conn.execute("SELECT * FROM racks ORDER BY code").fetchall()
            ]
        }


@app.post("/api/racks", status_code=201)
def create_rack(body: RackIn, user: dict = Depends(owner_only)):
    if not body.code or not body.name:
        raise HTTPException(422, "A rack needs a code and a name")
    key = store.new_device_key()
    with store.db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO racks (code,name,location,tier,capacity_g,tare_g,avg_item_g,"
                "armed,low_stock_pct,device_key,weight_g,cells_g,last_seen,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    body.code.strip().upper(),
                    body.name.strip(),
                    (body.location or "").strip(),
                    (body.tier or "").strip(),
                    (body.capacity_kg or 25) * 1000,
                    (body.tare_kg or 0) * 1000,
                    body.avg_item_g or 200,
                    1 if body.armed else 0,
                    body.low_stock_pct or 20,
                    key,
                    0,
                    json.dumps([0, 0, 0, 0]),
                    time.time(),
                    time.time(),
                ),
            )
        except Exception as exc:
            raise HTTPException(409, f"Rack code already exists ({exc})")
        rack = _rack_payload(conn, conn.execute("SELECT * FROM racks WHERE id=?", (cur.lastrowid,)).fetchone())
    return {"rack": rack}


@app.patch("/api/racks/{rack_id}")
def update_rack(rack_id: int, body: RackIn, user: dict = Depends(current_user)):
    with store.db() as conn:
        row = conn.execute("SELECT * FROM racks WHERE id=?", (rack_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Rack not found")
        sets, vals = [], []
        if body.name is not None:
            sets.append("name=?"); vals.append(body.name.strip())
        if body.location is not None:
            sets.append("location=?"); vals.append(body.location.strip())
        if body.tier is not None:
            sets.append("tier=?"); vals.append(body.tier.strip())
        if body.capacity_kg is not None:
            sets.append("capacity_g=?"); vals.append(body.capacity_kg * 1000)
        if body.tare_kg is not None:
            sets.append("tare_g=?"); vals.append(body.tare_kg * 1000)
        if body.avg_item_g is not None:
            sets.append("avg_item_g=?"); vals.append(body.avg_item_g)
        if body.armed is not None:
            if user["role"] != "owner" and bool(row["armed"]) != body.armed:
                raise HTTPException(403, "Only the shop owner can arm or disarm a rack")
            sets.append("armed=?"); vals.append(1 if body.armed else 0)
        if body.low_stock_pct is not None:
            sets.append("low_stock_pct=?"); vals.append(body.low_stock_pct)
        if body.code is not None and user["role"] == "owner":
            sets.append("code=?"); vals.append(body.code.strip().upper())
        if sets:
            vals.append(rack_id)
            conn.execute(f"UPDATE racks SET {', '.join(sets)} WHERE id=?", vals)
        rack = _rack_payload(conn, conn.execute("SELECT * FROM racks WHERE id=?", (rack_id,)).fetchone())
    return {"rack": rack}


@app.delete("/api/racks/{rack_id}")
def delete_rack(rack_id: int, user: dict = Depends(owner_only)):
    with store.db() as conn:
        conn.execute("DELETE FROM racks WHERE id=?", (rack_id,))
        conn.execute("DELETE FROM readings WHERE rack_id=?", (rack_id,))
        conn.execute("DELETE FROM alerts WHERE rack_id=?", (rack_id,))
        conn.execute("DELETE FROM commands WHERE rack_id=?", (rack_id,))
        conn.execute("DELETE FROM events WHERE rack_id=?", (rack_id,))
    engine.reset_buffer(rack_id)
    return {"ok": True}


@app.post("/api/racks/{rack_id}/tare")
def tare_rack(rack_id: int, user: dict = Depends(current_user)):
    with store.db() as conn:
        rack = conn.execute("SELECT * FROM racks WHERE id=?", (rack_id,)).fetchone()
        if not rack:
            raise HTTPException(404, "Rack not found")
        last = conn.execute(
            "SELECT weight_g FROM readings WHERE rack_id=? ORDER BY ts DESC LIMIT 1", (rack_id,)
        ).fetchone()
        weight = float(last["weight_g"]) if last else float(rack["weight_g"])
        conn.execute("UPDATE racks SET weight_g=? WHERE id=?", (weight, rack_id))
        cur = conn.execute(
            "INSERT INTO events (rack_id,kind,prev_g,curr_g,delta_g,qty,status,note,ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (rack_id, "tare", float(rack["weight_g"]), weight, weight - float(rack["weight_g"]),
             None, "Baseline saved", f"Tare saved at {weight/1000:.2f} kg", time.time()),
        )
        event = _event_payload(
            conn.execute(
                "SELECT e.*, r.code AS rack_code FROM events e JOIN racks r ON r.id=e.rack_id WHERE e.id=?",
                (cur.lastrowid,),
            ).fetchone()
        )
    engine.reset_buffer(rack_id)
    return {"ok": True, "event": event, "weight_kg": round(weight / 1000, 3)}


@app.get("/api/racks/{rack_id}/readings")
def rack_readings(rack_id: int, minutes: int = 60, user: dict = Depends(current_user)):
    since = time.time() - minutes * 60
    with store.db() as conn:
        rows = conn.execute(
            "SELECT ts, weight_g FROM readings WHERE rack_id=? AND ts>=? ORDER BY ts",
            (rack_id, since),
        ).fetchall()
    return {"points": [{"ts": r["ts"], "kg": round(r["weight_g"] / 1000, 3)} for r in rows]}


@app.get("/api/series")
def series(minutes: int = 240, user: dict = Depends(current_user)):
    """Total shop load over time, bucketed into at most 90 points."""
    since = time.time() - minutes * 60
    with store.db() as conn:
        rows = conn.execute(
            "SELECT ts, SUM(g) AS g FROM ("
            "  SELECT rack_id, CAST(ts/60 AS INT)*60 AS ts, AVG(weight_g) AS g"
            "  FROM readings WHERE ts>=? GROUP BY rack_id, CAST(ts/60 AS INT)"
            ") GROUP BY ts ORDER BY ts",
            (since,),
        ).fetchall()
        live = conn.execute("SELECT COALESCE(SUM(weight_g),0) s FROM racks").fetchone()["s"]
    points = [{"ts": r["ts"], "kg": round((r["g"] or 0) / 1000, 2)} for r in rows]
    if len(points) > 90:
        step = len(points) // 90 + 1
        points = points[::step]
    now_ts = time.time()
    if not points or now_ts - points[-1]["ts"] > 20:
        points.append({"ts": now_ts, "kg": round(live / 1000, 2)})
    return {"points": points, "tz": str(TZ)}


# --------------------------------------------------------------------------- #
# events + alerts
# --------------------------------------------------------------------------- #
@app.get("/api/events")
def list_events(
    rack_id: int | None = None,
    kind: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    user: dict = Depends(current_user),
):
    sql = "SELECT e.*, r.code AS rack_code FROM events e JOIN racks r ON r.id=e.rack_id WHERE 1=1"
    args: list[Any] = []
    if rack_id:
        sql += " AND e.rack_id=?"; args.append(rack_id)
    if kind and kind != "all":
        sql += " AND e.kind=?"; args.append(kind)
    if q:
        sql += " AND (r.code LIKE ? OR e.status LIKE ? OR e.note LIKE ?)"
        like = f"%{q}%"; args += [like, like, like]
    sql += " ORDER BY e.ts DESC, e.id DESC LIMIT ?"
    args.append(limit)
    with store.db() as conn:
        rows = conn.execute(sql, args).fetchall()
        return {"events": [_event_payload(r) for r in rows]}


@app.get("/api/alerts")
def list_alerts(state: str = "open", limit: int = Query(50, ge=1, le=500), user: dict = Depends(current_user)):
    sql = "SELECT a.*, r.code AS rack_code FROM alerts a JOIN racks r ON r.id=a.rack_id"
    args: list[Any] = []
    if state != "all":
        sql += " WHERE a.state=?"
        args.append(state)
    sql += " ORDER BY a.created_at DESC LIMIT ?"
    args.append(limit)
    with store.db() as conn:
        return {"alerts": [_alert_payload(r) for r in conn.execute(sql, args).fetchall()]}


@app.post("/api/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, user: dict = Depends(current_user)):
    with store.db() as conn:
        row = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Alert not found")
        conn.execute(
            "UPDATE alerts SET state='acknowledged', ack_by=?, ack_at=? WHERE id=?",
            (user["name"], time.time(), alert_id),
        )
        alert = _alert_payload(
            conn.execute(
                "SELECT a.*, r.code AS rack_code FROM alerts a JOIN racks r ON r.id=a.rack_id WHERE a.id=?",
                (alert_id,),
            ).fetchone()
        )
    return {"alert": alert}


@app.post("/api/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int, user: dict = Depends(current_user)):
    with store.db() as conn:
        row = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Alert not found")
        conn.execute(
            "UPDATE alerts SET state='resolved', ack_by=COALESCE(ack_by,?), ack_at=? WHERE id=?",
            (user["name"], time.time(), alert_id),
        )
    return {"ok": True}


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def _export_rows(conn, rack_id: int | None, kind: str | None):
    sql = "SELECT e.*, r.code AS rack_code FROM events e JOIN racks r ON r.id=e.rack_id WHERE 1=1"
    args: list[Any] = []
    if rack_id:
        sql += " AND e.rack_id=?"; args.append(rack_id)
    if kind and kind != "all":
        sql += " AND e.kind=?"; args.append(kind)
    sql += " ORDER BY e.ts ASC"
    return conn.execute(sql, args).fetchall()


@app.get("/api/export.xlsx")
def export_xlsx(rack_id: int | None = None, kind: str | None = None, user: dict = Depends(current_user)):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    with store.db() as conn:
        rows = _export_rows(conn, rack_id, kind)

    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory Log"
    headers = ["Date & Time", "Rack ID", "Prev (kg)", "Curr (kg)", "Removed", "Est. Qty", "Status"]
    ws.append(headers)
    head_fill = PatternFill("solid", fgColor="1F3B3A")
    for c in ws[1]:
        c.font = Font(bold=True, color="F4EFE4")
        c.fill = head_fill
        c.alignment = Alignment(horizontal="center")

    for r in rows:
        delta = float(r["delta_g"])
        removed = ("+" if delta > 0 else "") + f"{delta/1000:.2f} kg"
        ws.append([
            _fmt(r["ts"]),
            r["rack_code"],
            round(float(r["prev_g"]) / 1000, 2),
            round(float(r["curr_g"]) / 1000, 2),
            removed,
            r["qty"] if r["qty"] is not None else "—",
            r["status"],
        ])

    widths = [22, 10, 11, 11, 12, 10, 18]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    summary = wb.create_sheet("Summary")
    total_removed = sum(-float(r["delta_g"]) for r in rows if float(r["delta_g"]) < 0) / 1000
    alerts = sum(1 for r in rows if r["kind"] == "theft")
    summary.append(["RackSense inventory export"])
    summary["A1"].font = Font(bold=True, size=14)
    summary.append([])
    for label, value in [
        ("Generated", datetime.now(TZ).strftime(ISO)),
        ("Rows", len(rows)),
        ("Total removed", f"{total_removed:.2f} kg"),
        ("Theft alerts", alerts),
        ("Window", f"{_fmt(rows[0]['ts'])} → {_fmt(rows[-1]['ts'])}" if rows else "no rows"),
    ]:
        summary.append([label, value])
    summary.column_dimensions["A"].width = 18
    summary.column_dimensions["B"].width = 40

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    stamp = datetime.now().strftime("%Y-%m-%d")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="RackSense inventory log {stamp}.xlsx"'},
    )


@app.get("/api/export.csv")
def export_csv(rack_id: int | None = None, kind: str | None = None, user: dict = Depends(current_user)):
    with store.db() as conn:
        rows = _export_rows(conn, rack_id, kind)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date & Time", "Rack ID", "Prev (kg)", "Curr (kg)", "Removed", "Est. Qty", "Status", "Note"])
    for r in rows:
        delta = float(r["delta_g"])
        w.writerow([
            _fmt(r["ts"]), r["rack_code"], round(float(r["prev_g"]) / 1000, 2),
            round(float(r["curr_g"]) / 1000, 2), ("+" if delta > 0 else "") + f"{delta/1000:.2f} kg",
            r["qty"] if r["qty"] is not None else "", r["status"], r["note"],
        ])
    stamp = datetime.now().strftime("%Y-%m-%d")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="RackSense inventory log {stamp}.csv"'},
    )


# --------------------------------------------------------------------------- #
# overview + simulate
# --------------------------------------------------------------------------- #
@app.get("/api/overview")
def overview(user: dict = Depends(current_user)):
    with store.db() as conn:
        return _overview(conn)


class SimulateIn(BaseModel):
    action: Literal["sale", "theft", "restock", "bump", "empty"] = "sale"
    qty: int | None = Field(default=None, ge=1, le=200)


@app.post("/api/racks/{rack_id}/simulate")
def simulate_rack(rack_id: int, body: SimulateIn, user: dict = Depends(current_user)):
    with store.db() as conn:
        row = conn.execute("SELECT * FROM racks WHERE id=?", (rack_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Rack not found")
        rack = dict(row)
        events, alerts = engine.simulate(conn, rack, body.action, body.qty)
        _maybe_low_stock(conn, rack_id)
    return {"events": events, "alerts": alerts}


def _maybe_low_stock(conn, rack_id: int) -> None:
    row = conn.execute("SELECT * FROM racks WHERE id=?", (rack_id,)).fetchone()
    if not row:
        return
    pct = float(row["weight_g"]) / (float(row["capacity_g"]) or 1) * 100
    if pct <= float(row["low_stock_pct"]):
        last = conn.execute(
            "SELECT kind FROM events WHERE rack_id=? AND kind IN ('low_stock','restock','tare','empty')"
            " ORDER BY ts DESC, id DESC LIMIT 1",
            (rack_id,),
        ).fetchone()
        if not last or last["kind"] != "low_stock":
            conn.execute(
                "INSERT INTO events (rack_id,kind,prev_g,curr_g,delta_g,qty,status,note,ts)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (rack_id, "low_stock", row["weight_g"], row["weight_g"], 0, None,
                 "Low stock", f"Down to {pct:.0f}% of rated capacity", time.time()),
            )


class SimToggle(BaseModel):
    enabled: bool


@app.post("/api/sim")
async def toggle_sim(body: SimToggle, user: dict = Depends(owner_only)):
    sim.set_enabled(body.enabled)
    await push_snapshot()
    return {"enabled": sim.enabled}


@app.get("/api/sim")
def sim_state(user: dict = Depends(current_user)):
    return {"enabled": sim.enabled}


# --------------------------------------------------------------------------- #
# device (ESP32 / gateway) API
# --------------------------------------------------------------------------- #
class TelemetryIn(BaseModel):
    weight_g: float
    cells_g: list[float] | None = None
    ts: float | None = None


def _rack_by_key(conn, key: str):
    row = conn.execute("SELECT * FROM racks WHERE device_key=?", (key,)).fetchone()
    if not row:
        raise HTTPException(401, "Unknown device key")
    return dict(row)


@app.post("/api/device/telemetry")
async def device_telemetry(request: Request, body: TelemetryIn):
    key = request.headers.get("x-device-key", "")
    if not key:
        raise HTTPException(401, "Missing X-Device-Key header")
    with store.db() as conn:
        rack = _rack_by_key(conn, key)
        event, alert = engine.ingest_reading(conn, rack, body.weight_g, body.cells_g or [], body.ts)
        _maybe_low_stock(conn, rack["id"])
        current = conn.execute("SELECT * FROM racks WHERE id=?", (rack["id"],)).fetchone()
        armed = bool(current["armed"])
        weight = float(current["weight_g"])
    payload = {"ok": True, "armed": armed, "weight_g": round(weight, 1), "buzz": False,
               "event": event["kind"] if event else None}
    if alert:
        payload["buzz"] = True
        await push_snapshot()
    elif event:
        await push_snapshot()
    return payload


@app.get("/api/device/{key}/config")
def device_config(key: str):
    with store.db() as conn:
        rack = _rack_by_key(conn, key)
        return {
            "rack": rack["code"],
            "name": rack["name"],
            "armed": bool(rack["armed"]),
            "capacity_g": rack["capacity_g"],
            "tare_g": rack["tare_g"],
            "avg_item_g": rack["avg_item_g"],
            "settle_samples": engine.SETTLE_SAMPLES,
            "settle_tol_g": engine.SETTLE_TOL_G,
            "noise_g": engine.NOISE_G,
            "server_time": time.time(),
        }


@app.get("/api/device/{key}/commands")
def device_commands(key: str):
    with store.db() as conn:
        rack = _rack_by_key(conn, key)
        rows = conn.execute(
            "SELECT * FROM commands WHERE rack_id=? AND state='pending' ORDER BY id", (rack["id"],)
        ).fetchall()
        out = [
            {"id": r["id"], "cmd": r["cmd"], "payload": json.loads(r["payload"]), "created_at": r["created_at"]}
            for r in rows
        ]
        if out:
            conn.execute(
                f"UPDATE commands SET state='delivered', delivered_at=? WHERE id IN ({','.join('?'*len(out))})",
                [time.time()] + [o["id"] for o in out],
            )
        return {"commands": out}


@app.post("/api/device/{key}/heartbeat")
def device_heartbeat(key: str):
    with store.db() as conn:
        rack = _rack_by_key(conn, key)
        conn.execute("UPDATE racks SET last_seen=? WHERE id=?", (time.time(), rack["id"]))
    return {"ok": True}


@app.post("/api/device/{key}/tare")
def device_tare(key: str, body: TelemetryIn):
    """The rack itself reports its empty-scale reading so tare is hardware-exact."""
    with store.db() as conn:
        rack = _rack_by_key(conn, key)
        conn.execute("UPDATE racks SET tare_g=? WHERE id=?", (body.weight_g, rack["id"]))
    return {"ok": True, "tare_g": body.weight_g}


# --------------------------------------------------------------------------- #
# WebSocket live feed
# --------------------------------------------------------------------------- #
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = Query(default="")):
    now = time.time()
    with store.db() as conn:
        row = conn.execute(
            "SELECT expires_at FROM sessions WHERE token=?", (token,)
        ).fetchone()
    if not row or row["expires_at"] < now:
        await ws.close(code=4401)
        return
    await ws.accept()
    await hub.add(ws)
    try:
        await ws.send_json(snapshot())
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_json({"type": "pong", "server_time": time.time()})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.drop(ws)


# --------------------------------------------------------------------------- #
# built-in hardware simulator
# --------------------------------------------------------------------------- #
class Simulator:
    def __init__(self) -> None:
        self.enabled = True
        self.task: asyncio.Task | None = None

    def set_enabled(self, value: bool) -> None:
        self.enabled = value

    async def loop(self) -> None:
        await asyncio.sleep(2)
        while True:
            try:
                if self.enabled:
                    changed = self.step()
                    if changed:
                        await push_snapshot()
            except Exception:
                pass
            await asyncio.sleep(SIM_INTERVAL)

    def step(self) -> bool:
        with store.db() as conn:
            racks = [dict(r) for r in conn.execute("SELECT * FROM racks").fetchall()]
            if not racks:
                return False
            armed = [r for r in racks if r["armed"]]
            unarmed = [r for r in racks if not r["armed"]]
            low = [r for r in racks if float(r["weight_g"]) < float(r["capacity_g"]) * 0.35]
            roll = random.random()
            target = None
            action = "sale"
            if roll < 0.18 and low:
                target, action = random.choice(low), "restock"
            elif roll < 0.30 and armed:
                target = random.choice(armed)
                open_alerts = conn.execute(
                    "SELECT COUNT(*) c FROM alerts WHERE rack_id=? AND state='open'", (target["id"],)
                ).fetchone()["c"]
                if open_alerts:
                    target = None
                action = "theft"
            elif roll < 0.42:
                target, action = random.choice(racks), "bump"
            elif unarmed:
                target = random.choice(unarmed)
                action = "sale"
            if not target:
                return False
            weight = float(target["weight_g"])
            avg = float(target["avg_item_g"]) or 200
            if action == "sale" and weight < avg * 3:
                action = "restock"
            engine.simulate(conn, target, action, random.randint(2, 5))
            _maybe_low_stock(conn, target["id"])
            # every other rack keeps reporting its steady load, like real hardware
            for rack in racks:
                if rack["id"] == target["id"]:
                    continue
                engine.ingest_reading(
                    conn, rack, float(rack["weight_g"]) + random.uniform(-18, 18), []
                )
        return True


sim = Simulator()


@app.on_event("startup")
async def on_startup() -> None:
    store.init_db()
    sim.task = asyncio.create_task(sim.loop())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if sim.task:
        sim.task.cancel()


# --------------------------------------------------------------------------- #
# static site
# --------------------------------------------------------------------------- #
@app.get("/api/snapshot")
def get_snapshot(user: dict = Depends(current_user)):
    return snapshot()


@app.get("/api/health")
def health():
    return {"ok": True, "service": "racksense", "time": time.time()}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.exception_handler(404)
async def spa_fallback(request: Request, exc):
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
