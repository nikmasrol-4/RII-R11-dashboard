from flask import Flask, request, jsonify
from datetime import datetime, timezone
import os
import requests
from threading import Lock

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
    secret = os.getenv("WEBHOOK_SECRET", "")
    received_secret = request.headers.get("X-Webhook-Secret", "")

    if not secret or received_secret != secret:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    signal = {
        "time": datetime.now(timezone.utc).isoformat(),
        "signal": str(data.get("signal", "")).upper(),
        "symbol": str(data.get("symbol", "XAUUSD")),
        "entry": data.get("entry"),
        "tp1": data.get("tp1"),
        "tp2": data.get("tp2"),
        "sl": data.get("sl"),
        "status": "ACTIVE"
    }

    if signal["signal"] not in ("BUY", "SELL"):
        return jsonify({"error": "Signal must be BUY or SELL"}), 400

    with signals_lock:
        signals.append(signal)
        if len(signals) > 500:
            del signals[:-500]

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if token and chat_id:
        message = (
            f"RII R11 {signal['signal']}\n"
            f"Symbol: {signal['symbol']}\n"
            f"Entry: {signal['entry']}\n"
            f"TP1: {signal['tp1']}\n"
            f"TP2: {signal['tp2']}\n"
            f"SL: {signal['sl']}"
        )
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=10
            )
        except requests.RequestException:
            app.logger.exception("Telegram notification failed")

    return jsonify({"ok": True, "signal": signal}), 201

@app.route("/signals", methods=["GET"])
def get_signals():
    with signals_lock:
        return jsonify(list(reversed(signals)))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
