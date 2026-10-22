
from flask import Flask, request, jsonify
from datetime import datetime, timezone
from threading import Lock
import os
import hmac
import sqlite3
import uuid

app = Flask(__name__)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
DB_PATH = os.getenv("DB_PATH", "/var/data/rii_signals.db")

db_lock = Lock()


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = (
        "https://nikmasrol-4.github.io"
    )
    response.headers["Access-Control-Allow-Methods"] = (
        "GET, POST, OPTIONS"
    )
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                time TEXT NOT NULL,
                signal TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT,
                entry REAL,
                tp1 REAL,
                tp2 REAL,
                sl REAL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                updated_at TEXT,
                result_note TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_signals_time
            ON signals(time)
        """)


init_db()


def valid_secret(data):
    supplied = str(data.get("secret", ""))
    return bool(WEBHOOK_SECRET) and hmac.compare_digest(
        supplied, WEBHOOK_SECRET
    )


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "system": "RII R11",
        "status": "online",
        "message": "RII R11 Backend Ready"
    })


@app.route("/webhook", methods=["POST", "OPTIONS"])
def webhook():
    if request.method == "OPTIONS":
        return "", 204

    if not WEBHOOK_SECRET:
        return jsonify({
            "error": "WEBHOOK_SECRET belum ditetapkan"
        }), 503

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"error": "JSON tidak sah"}), 400

    if not valid_secret(data):
        return jsonify({"error": "Unauthorized"}), 401

    action = str(
        data.get("action", data.get("signal", ""))
    ).upper()

    if action not in ("BUY", "SELL"):
        return jsonify({
            "error": "Signal mesti BUY atau SELL"
        }), 400

    try:
        entry = float(data["entry"]) if data.get("entry") is not None else None
        tp1 = float(data["tp1"]) if data.get("tp1") is not None else None
        tp2 = float(data["tp2"]) if data.get("tp2") is not None else None
        sl = float(data["sl"]) if data.get("sl") is not None else None
    except (TypeError, ValueError):
        return jsonify({
            "error": "Entry, TP1, TP2 dan SL mesti nombor"
        }), 400

    signal = {
        "id": str(uuid.uuid4()),
        "time": utc_now(),
        "signal": action,
        "symbol": str(
            data.get("pair", data.get("symbol", "XAUUSD"))
        ),
        "timeframe": str(data.get("timeframe", "")),
        "entry": entry,
        "tp1": tp1,
        "tp2": tp2,
        "sl": sl,
        "status": "ACTIVE",
        "updated_at": None,
        "result_note": None
    }

    with db_lock, get_db() as conn:
        conn.execute("""
            INSERT INTO signals (
                id, time, signal, symbol, timeframe,
                entry, tp1, tp2, sl, status,
                updated_at, result_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal["id"], signal["time"], signal["signal"],
            signal["symbol"], signal["timeframe"],
            signal["entry"], signal["tp1"], signal["tp2"],
            signal["sl"], signal["status"],
            signal["updated_at"], signal["result_note"]
        ))

    app.logger.info(
        "Signal diterima: %s %s",
        action, signal["symbol"]
    )

    return jsonify({
        "ok": True,
        "message": "Signal diterima",
        "signal": signal
    }), 201


@app.route("/signals", methods=["GET", "OPTIONS"])
def get_signals():
    if request.method == "OPTIONS":
        return "", 204

    with db_lock, get_db() as conn:
        records = conn.execute("""
            SELECT id, time, signal, symbol, timeframe,
                   entry, tp1, tp2, sl, status,
                   updated_at, result_note
            FROM signals
            ORDER BY time DESC
            LIMIT 500
        """).fetchall()

    return jsonify([dict(row) for row in records])


@app.route("/update-status", methods=["POST", "OPTIONS"])
def update_status():
    if request.method == "OPTIONS":
        return "", 204

    if not WEBHOOK_SECRET:
        return jsonify({
            "error": "WEBHOOK_SECRET belum ditetapkan"
        }), 503

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"error": "JSON tidak sah"}), 400

    if not valid_secret(data):
        return jsonify({"error": "Unauthorized"}), 401

    signal_id = str(data.get("id", "")).strip()
    status = str(data.get("status", "")).upper().strip()

    allowed_statuses = {
        "ACTIVE", "TP1 HIT", "TP2 HIT",
        "SL HIT", "CLOSED", "CANCELLED"
    }

    if not signal_id:
        return jsonify({"error": "ID signal diperlukan"}), 400

    if status not in allowed_statuses:
        return jsonify({
            "error": "Status tidak dibenarkan",
            "allowed": sorted(allowed_statuses)
        }), 400

    with db_lock, get_db() as conn:
        cursor = conn.execute("""
            UPDATE signals
            SET status = ?, updated_at = ?, result_note = ?
            WHERE id = ?
        """, (
            status,
            utc_now(),
            str(data.get("note", ""))[:250] or None,
            signal_id
        ))

        if cursor.rowcount == 0:
            return jsonify({
                "error": "Signal ID tidak ditemui"
            }), 404

        record = conn.execute("""
            SELECT id, time, signal, symbol, timeframe,
                   entry, tp1, tp2, sl, status,
                   updated_at, result_note
            FROM signals
            WHERE id = ?
        """, (signal_id,)).fetchone()

    return jsonify({
        "ok": True,
        "signal": dict(record)
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
    )
