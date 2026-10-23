from flask import Flask, request, jsonify
from datetime import datetime, timezone
from threading import Lock
import os
import hmac
import uuid
import math
import psycopg
from psycopg.rows import dict_row

app = Flask(__name__)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
db_lock = Lock()

ALLOWED_STATUSES = {"ACTIVE", "TP1 HIT", "TP2 HIT", "SL HIT", "CLOSED", "CANCELLED", "REJECTED"}
UNLOCK_STATUSES = {"TP1 HIT", "SL HIT"}
FINAL_STATUSES = {"TP2 HIT", "SL HIT", "CLOSED", "CANCELLED", "REJECTED"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "https://nikmasrol-4.github.io"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL belum ditetapkan di Render Environment")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=15)


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                time TEXT NOT NULL,
                signal TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT,
                entry DOUBLE PRECISION,
                tp1 DOUBLE PRECISION,
                tp2 DOUBLE PRECISION,
                sl DOUBLE PRECISION,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                updated_at TEXT,
                result_note TEXT,
                net_pips DOUBLE PRECISION
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(time)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol_status ON signals(symbol, status)")
        # Menaik taraf jadual lama jika ia telah wujud tanpa net_pips.
        conn.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS net_pips DOUBLE PRECISION")


def valid_secret(data):
    supplied = str(data.get("secret", ""))
    return bool(WEBHOOK_SECRET) and hmac.compare_digest(supplied, WEBHOOK_SECRET)


def find_active_setup(conn, symbol):
    return conn.execute("""
        SELECT id, signal, symbol, entry, tp1, sl, status, time
        FROM signals
        WHERE UPPER(symbol) = UPPER(%s) AND status = 'ACTIVE'
        ORDER BY time DESC LIMIT 1
    """, (symbol,)).fetchone()


def update_signal_status(data):
    signal_id = str(data.get("id", data.get("trade_id", ""))).strip()
    status = str(data.get("status", "")).upper().strip()
    note = str(data.get("note", ""))[:250] or None
    net_pips = data.get("net_pips")

    if not signal_id:
        return jsonify({"error": "ID signal diperlukan"}), 400
    if status not in ALLOWED_STATUSES:
        return jsonify({"error": "Status tidak dibenarkan", "allowed": sorted(ALLOWED_STATUSES)}), 400
    try:
        net_pips = float(net_pips) if net_pips is not None else None
        if net_pips is not None and not math.isfinite(net_pips):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "net_pips mesti nombor yang sah"}), 400

    with db_lock, get_db() as conn:
        row = conn.execute("SELECT id, symbol, signal, status FROM signals WHERE id = %s", (signal_id,)).fetchone()
        if row is None:
            return jsonify({"error": "Signal ID tidak ditemui", "id": signal_id}), 404
        current_status = row["status"]
        if current_status in FINAL_STATUSES:
            return jsonify({"ok": True, "message": "Status akhir dikekalkan", "id": signal_id, "status": current_status}), 200
        if current_status == "TP1 HIT" and status == "ACTIVE":
            return jsonify({"ok": True, "message": "Status TP1 dikekalkan", "id": signal_id, "status": current_status}), 200
        if current_status == "ACTIVE" and status not in ("ACTIVE", "TP1 HIT", "SL HIT"):
            return jsonify({"error": "LOCK AKTIF", "message": "Setup hanya boleh dilepaskan melalui TP1 HIT atau SL HIT.", "id": signal_id, "status": current_status}), 409
        conn.execute("""
            UPDATE signals SET status = %s, updated_at = %s,
                result_note = COALESCE(%s, result_note),
                net_pips = COALESCE(%s, net_pips)
            WHERE id = %s
        """, (status, utc_now(), note, net_pips, signal_id))
        record = conn.execute("SELECT * FROM signals WHERE id = %s", (signal_id,)).fetchone()
    return jsonify({"ok": True, "signal": record, "lock_released": status in UNLOCK_STATUSES}), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({"system": "RII R11", "status": "online", "storage": "Neon PostgreSQL", "signal_lock": "TP1_OR_SL", "endpoints": ["/webhook", "/signals", "/update-status", "/lock-status", "/stats"]})


@app.route("/health", methods=["GET"])
def health():
    try:
        with get_db() as conn:
            conn.execute("SELECT 1")
        return jsonify({"ok": True, "database": "connected"}), 200
    except Exception as exc:
        app.logger.exception("Health check failed")
        return jsonify({"ok": False, "database": "disconnected", "error": str(exc)}), 503


@app.route("/webhook", methods=["POST", "OPTIONS"])
def webhook():
    if request.method == "OPTIONS":
        return "", 204
    if not WEBHOOK_SECRET:
        return jsonify({"error": "WEBHOOK_SECRET belum ditetapkan"}), 503
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON tidak sah"}), 400
    if not valid_secret(data):
        return jsonify({"error": "Unauthorized"}), 401

    event = str(data.get("event", "")).upper().strip()
    if event in ("TP1_HIT", "TP2_HIT", "SL_HIT"):
        data["status"] = {"TP1_HIT": "TP1 HIT", "TP2_HIT": "TP2 HIT", "SL_HIT": "SL HIT"}[event]
        return update_signal_status(data)

    action = str(data.get("action", data.get("signal", ""))).upper().strip()
    if action not in ("BUY", "SELL"):
        return jsonify({"error": "Action mesti BUY/SELL atau event TP1_HIT/TP2_HIT/SL_HIT"}), 400
    symbol = str(data.get("pair", data.get("symbol", "XAUUSD"))).strip().upper()
    if not symbol:
        return jsonify({"error": "Symbol diperlukan"}), 400
    try:
        entry, tp1, tp2, sl = (float(data[k]) for k in ("entry", "tp1", "tp2", "sl"))
        if not all(math.isfinite(v) and v > 0 for v in (entry, tp1, tp2, sl)):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Entry, TP1, TP2 dan SL mesti nombor positif yang sah"}), 400

    signal_id = str(data.get("id", data.get("trade_id", ""))).strip() or str(uuid.uuid4())
    signal = {"id": signal_id, "time": utc_now(), "signal": action, "symbol": symbol,
              "timeframe": str(data.get("timeframe", "")), "entry": entry, "tp1": tp1,
              "tp2": tp2, "sl": sl, "status": "ACTIVE", "updated_at": None,
              "result_note": None, "net_pips": None}

    with db_lock, get_db() as conn:
        if conn.execute("SELECT id FROM signals WHERE id = %s", (signal_id,)).fetchone():
            return jsonify({"error": "ID signal sudah wujud", "id": signal_id}), 409
        active = find_active_setup(conn, symbol)
        if active:
            note = "SIGNAL_LOCK_ACTIVE: signal ditolak kerana setup semasa belum menyentuh TP1 atau SL."
            conn.execute("""
                INSERT INTO signals (id,time,signal,symbol,timeframe,entry,tp1,tp2,sl,status,updated_at,result_note,net_pips)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'REJECTED',%s,%s,NULL)
            """, (signal_id, signal["time"], action, symbol, signal["timeframe"], entry, tp1, tp2, sl, utc_now(), note))
            rejected = dict(signal, status="REJECTED", updated_at=utc_now(), result_note=note)
            return jsonify({"ok": False, "rejected": True, "reason": "SIGNAL_LOCK_ACTIVE", "message": "Signal baharu direkod sebagai REJECTED. Setup aktif kekal dikunci sehingga TP1 atau SL.", "signal": rejected, "locked_by": dict(active), "lock_active": True}), 409
        conn.execute("""
            INSERT INTO signals (id,time,signal,symbol,timeframe,entry,tp1,tp2,sl,status,updated_at,result_note,net_pips)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVE',NULL,NULL,NULL)
        """, (signal_id, signal["time"], action, symbol, signal["timeframe"], entry, tp1, tp2, sl))
    return jsonify({"ok": True, "message": "Signal diterima; lock diaktifkan", "signal": signal, "lock_active": True}), 201


@app.route("/update-status", methods=["POST", "OPTIONS"])
def update_status():
    if request.method == "OPTIONS":
        return "", 204
    if not WEBHOOK_SECRET:
        return jsonify({"error": "WEBHOOK_SECRET belum ditetapkan"}), 503
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON tidak sah"}), 400
    if not valid_secret(data):
        return jsonify({"error": "Unauthorized"}), 401
    return update_signal_status(data)


@app.route("/signals", methods=["GET", "OPTIONS"])
def get_signals():
    if request.method == "OPTIONS":
        return "", 204
    with get_db() as conn:
        records = conn.execute("SELECT id,time,signal,symbol,timeframe,entry,tp1,tp2,sl,status,updated_at,result_note,net_pips FROM signals ORDER BY time DESC LIMIT 500").fetchall()
    return jsonify(records)


@app.route("/stats", methods=["GET", "OPTIONS"])
def stats():
    if request.method == "OPTIONS":
        return "", 204
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE time >= %s", ((datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)).isoformat(),)).fetchone()["n"]
        active = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE status = 'ACTIVE'").fetchone()["n"]
        wins = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE status IN ('TP1 HIT','TP2 HIT')").fetchone()["n"]
        losses = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE status = 'SL HIT'").fetchone()["n"]
        net = conn.execute("SELECT SUM(net_pips) AS n FROM signals WHERE net_pips IS NOT NULL").fetchone()["n"]
    decided = wins + losses
    return jsonify({"total": total, "active": active, "wins": wins, "losses": losses, "win_rate": round(wins / decided * 100, 2) if decided else None, "net_pips": net})


@app.route("/lock-status", methods=["GET", "OPTIONS"])
def lock_status():
    if request.method == "OPTIONS":
        return "", 204
    symbol = request.args.get("symbol", "XAUUSD").strip().upper()
    with db_lock, get_db() as conn:
        active = find_active_setup(conn, symbol)
    return jsonify({"symbol": symbol, "locked": active is not None, "active_setup": dict(active) if active else None, "unlock_conditions": ["TP1 HIT", "SL HIT"]})


with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
