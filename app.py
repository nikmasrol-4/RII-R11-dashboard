from flask import Flask, request, jsonify
from datetime import datetime, timezone
import os
import requests
from threading import Lock
import hmac

app = Flask(__name__)

signals = []
signals_lock = Lock()


def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        app.logger.error("Telegram environment variables are missing")
        return False, "Telegram configuration missing"

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10
        )

        result = response.json()

        if response.ok and result.get("ok") is True:
            return True, "Telegram message sent"

        app.logger.error("Telegram API error: %s", result)
        return False, "Telegram API rejected the message"

    except (requests.RequestException, ValueError):
        app.logger.exception("Telegram notification failed")
        return False, "Telegram request failed"


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "system": "RII R11",
        "status": "online",
        "message": "RII R11 Backend Ready"
    })


@app.route("/test-telegram", methods=["POST"])
def test_telegram():
    # Protect this test endpoint using the same secret.
    secret = os.getenv("WEBHOOK_SECRET", "").strip()
    data = request.get_json(silent=True) or {}
    supplied_secret = str(data.get("secret", ""))

    if not secret or not hmac.compare_digest(supplied_secret, secret):
        return jsonify({"error": "Unauthorized"}), 401

    ok, message = send_telegram("RII R11: Telegram connection test.")
    if not ok:
        return jsonify({"ok": False, "message": message}), 502

    return jsonify({"ok": True, "message": message}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    secret = os.getenv("WEBHOOK_SECRET", "").strip()
    data = request.get_json(silent=True) or {}
    supplied_secret = str(data.get("secret", ""))

    if not secret or not hmac.compare_digest(supplied_secret, secret):
        return jsonify({"error": "Unauthorized"}), 401

    signal_type = str(data.get("signal", "")).upper()
    symbol = str(data.get("symbol", "XAUUSD"))

    if signal_type not in ("BUY", "SELL"):
        return jsonify({"error": "Signal must be BUY or SELL"}), 400

    signal = {
        "time": datetime.now(timezone.utc).isoformat(),
        "signal": signal_type,
        "symbol": symbol,
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

    message = (
        f"RII R11 {signal['signal']}\n"
        f"Symbol: {signal['symbol']}\n"
        f"Entry: {signal['entry']}\n"
        f"TP1: {signal['tp1']}\n"
        f"TP2: {signal['tp2']}\n"
        f"SL: {signal['sl']}"
    )

    ok, result_message = send_telegram(message)

    return jsonify({
        "ok": True,
        "telegram_sent": ok,
        "telegram_message": result_message,
        "signal": signal
    }), 201


@app.route("/signals", methods=["GET"])
def get_signals():
    with signals_lock:
        return jsonify(list(reversed(signals)))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
