
from flask import Flask, request, jsonify
from datetime import datetime, timezone
from threading import Lock
import os
import hmac

app = Flask(__name__)

signals = []
signals_lock = Lock()


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "system": "RII R11",
        "status": "online",
        "message": "RII R11 Backend Ready"
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    secret = os.getenv("WEBHOOK_SECRET", "").strip()

    if not secret:
        return jsonify({"error": "Webhook secret belum ditetapkan"}), 503

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"error": "JSON tidak sah"}), 400

    supplied_secret = str(data.get("secret", ""))

    if not hmac.compare_digest(supplied_secret, secret):
        return jsonify({"error": "Unauthorized"}), 401

    signal_type = str(data.get("action", data.get("signal", ""))).upper()


    if signal_type not in ("BUY", "SELL"):
        return jsonify({"error": "Signal mesti BUY atau SELL"}), 400

    signal = {
        "time": datetime.now(timezone.utc).isoformat(),
        "signal": signal_type,
        
"symbol": str(data.get("pair", data.get("symbol", "XAUUSD"))),

        "entry": data.get("entry"),
        "tp1": data.get("tp1"),
        "tp2": data.get("tp2"),
        "sl": data.get("sl"),
        "status": "ACTIVE"
    }

    with signals_lock:
        signals.append(signal)
        if len(signals) > 500:
            del signals[:-500]

    app.logger.info(
        "Signal diterima: %s %s",
        signal["signal"],
        signal["symbol"]
    )

    return jsonify({
        "ok": True,
        "message": "Signal diterima",
        "signal": signal
    }), 201


@app.route("/signals", methods=["GET"])
def get_signals():
    with signals_lock:
        return jsonify(list(reversed(signals)))


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
    )
