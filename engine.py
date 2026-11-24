"""The measurement logic: from raw load-cell grams to inventory events.

Mirrors the working algorithm of the project deck:

1. Tare & baseline    - the last stable rack load is the reference weight.
2. Stabilise delay    - a reading only counts once three consecutive samples
                        agree within 90 g (about 2-3 s of real hardware), so a
                        hand resting on the shelf or a crate being nudged does
                        not register.
3. Weight delta       - previous stable weight minus current stable weight.
4. Item estimate      - removed grams divided by the rack's average item weight.
5. Breach alert       - on an armed rack a removal raises a theft alert, queues
                        the buzzer command for the ESP32 and notifies the
                        dashboard in real time.
"""
from __future__ import annotations

import json
import time
from collections import deque

NOISE_G = 120          # anything smaller is scale drift, not stock movement
SETTLE_SAMPLES = 3     # consecutive samples that must agree
SETTLE_TOL_G = 90      # max spread inside those samples
BUMP_FACTOR = 3        # a raw excursion this many times the noise floor is watched

_buffers: dict[int, deque] = {}
_pending_bump: set[int] = set()


def _buffer(rack_id: int) -> deque:
    buf = _buffers.get(rack_id)
    if buf is None:
        buf = deque(maxlen=8)
        _buffers[rack_id] = buf
    return buf


def reset_buffer(rack_id: int) -> None:
    _buffers.pop(rack_id, None)
    _pending_bump.discard(rack_id)


def _settled_weight(buf: deque) -> float | None:
    if len(buf) < SETTLE_SAMPLES:
        return None
    window = [w for _, w in list(buf)[-SETTLE_SAMPLES:]]
    if max(window) - min(window) > SETTLE_TOL_G:
        return None
    window.sort()
    return window[len(window) // 2]


def _insert_event(conn, rack_id, kind, prev_g, curr_g, delta_g, qty, status, note, ts):
    cur = conn.execute(
        "INSERT INTO events (rack_id,kind,prev_g,curr_g,delta_g,qty,status,note,ts)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (rack_id, kind, prev_g, curr_g, delta_g, qty, status, note, ts),
    )
    return cur.lastrowid


def ingest_reading(conn, rack: dict, weight_g: float, cells_g: list, ts: float | None = None,
                   assume_armed: bool = False):
    """Feed one load-cell sample. Returns (event, alert) or (None, None)."""
    ts = ts or time.time()
    rack_id = rack["id"]
    weight_g = max(0.0, float(weight_g))
    buf = _buffer(rack_id)
    buf.append((ts, weight_g))

    cells = cells_g if cells_g else [int(weight_g * f) for f in (0.28, 0.25, 0.24, 0.23)]
    cells = [int(round(c)) for c in cells][:4]
    while len(cells) < 4:
        cells.append(0)

    conn.execute(
        "INSERT INTO readings (rack_id, weight_g, cells_g, ts) VALUES (?,?,?,?)",
        (rack_id, weight_g, json.dumps(cells), ts),
    )
    conn.execute(
        "UPDATE racks SET last_seen=?, cells_g=? WHERE id=?",
        (ts, json.dumps(cells), rack_id),
    )

    baseline = float(rack["weight_g"])
    settled = _settled_weight(buf)
    if settled is None:
        # unstable: remember a big excursion so we can report it as a bump later
        if baseline and abs(weight_g - baseline) > BUMP_FACTOR * NOISE_G:
            _pending_bump.add(rack_id)
        return None, None

    delta = settled - baseline

    if abs(delta) < NOISE_G:
        if rack_id in _pending_bump:
            _pending_bump.discard(rack_id)
            note = f"{abs(round(weight_g - baseline))} g excursion settled back - hand bump ignored"
            event_id = _insert_event(
                conn, rack_id, "bump", baseline, settled, delta, None, "Bump ignored", note, ts
            )
            conn.execute("UPDATE racks SET weight_g=? WHERE id=?", (settled, rack_id))
            return _event_row(conn, event_id, rack), None
        conn.execute("UPDATE racks SET weight_g=? WHERE id=?", (settled, rack_id))
        return None, None

    _pending_bump.discard(rack_id)
    capacity = float(rack["capacity_g"]) or 1.0
    avg_item = float(rack["avg_item_g"]) or 1.0
    qty = None
    alert = None

    if delta < 0:
        removed = abs(delta)
        qty = max(1, int(round(removed / avg_item)))
        if rack["armed"] or assume_armed:
            kind, status = "theft", "THEFT ALERT"
            note = f"{removed/1000:.2f} kg removed from an armed rack"
        else:
            kind, status = "sale", "Normal Sale"
            note = f"{removed/1000:.2f} kg removed, {qty} items estimated"

        before_pct = (baseline / capacity) * 100
        after_pct = (settled / capacity) * 100
        if after_pct <= float(rack["low_stock_pct"]) and before_pct > float(rack["low_stock_pct"]):
            note += f" · low stock ({after_pct:.0f}%)"
        elif after_pct <= 1:
            note += " · rack empty"
    else:
        kind, status = "restock", "Restocked"
        note = f"{delta/1000:.2f} kg added to the shelf"
        if settled > capacity + 50:
            note += " · above rated capacity"

    event_id = _insert_event(
        conn, rack_id, kind, baseline, settled, delta, qty, status, note, ts
    )
    conn.execute("UPDATE racks SET weight_g=? WHERE id=?", (settled, rack_id))

    if kind == "theft":
        # 60 s suppression window: one open alert per rack, not a flood
        recent = conn.execute(
            "SELECT id FROM alerts WHERE rack_id=? AND state='open' AND created_at>?",
            (rack_id, ts - 60),
        ).fetchone()
        if recent:
            conn.execute("UPDATE events SET note = note || ' · alert already open' WHERE id=?", (event_id,))
            return _event_row(conn, event_id, rack), None
        message = (
            f"{abs(delta)/1000:.2f} kg removed (~{qty} items) without a sale"
            + ("" if rack["armed"] else " · test breach")
        )
        cur = conn.execute(
            "INSERT INTO alerts (rack_id,event_id,message,severity,state,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (rack_id, event_id, message, "high", "open", ts),
        )
        alert = {
            "id": cur.lastrowid,
            "rack_id": rack_id,
            "rack_code": rack["code"],
            "event_id": event_id,
            "message": message,
            "severity": "high",
            "state": "open",
            "created_at": ts,
        }
        conn.execute(
            "INSERT INTO commands (rack_id,cmd,payload,state,created_at) VALUES (?,?,?,?,?)",
            (rack_id, "buzz", json.dumps({"pattern": "triple", "seconds": 6}), "pending", ts),
        )

    return _event_row(conn, event_id, rack), alert


def _event_row(conn, event_id: int, rack: dict) -> dict:
    row = conn.execute(
        "SELECT e.*, r.code AS rack_code FROM events e JOIN racks r ON r.id=e.rack_id"
        " WHERE e.id=?",
        (event_id,),
    ).fetchone()
    return dict(row)


def simulate(conn, rack: dict, action: str, qty: int | None = None):
    """Produce the stabilised sample burst a real rack would send for an action."""
    baseline = float(rack["weight_g"])
    avg = float(rack["avg_item_g"]) or 200
    if action == "sale":
        qty = qty or 3
    elif action == "theft":
        qty = qty or 5

    if action in ("sale", "theft"):
        target = max(0.0, baseline - qty * avg)
    elif action == "restock":
        target = float(rack["capacity_g"])
    elif action == "empty":
        target = 0.0
    else:  # bump
        target = baseline

    samples = []
    if action == "bump":
        samples = [baseline, baseline - 6 * NOISE_G, baseline - 4 * NOISE_G,
                   baseline - 30, baseline + 15, baseline]
    else:
        samples = [baseline - (baseline - target) * 0.3, target + 40, target - 20,
                   target + 5, target, target - 3]

    events = []
    alerts = []
    now = time.time()
    live = dict(rack)
    for i, w in enumerate(samples):
        ev, al = ingest_reading(conn, live, w, [], now + i * 0.05,
                                assume_armed=(action == "theft"))
        if ev:
            events.append(ev)
        if al:
            alerts.append(al)
        # each sample compares against the latest *stable* load of the rack
        row = conn.execute(
            "SELECT weight_g FROM racks WHERE id=?", (int(rack["id"]),)
        ).fetchone()
        live["weight_g"] = float(row["weight_g"])
    return events, alerts
