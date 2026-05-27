import os
import logging
import requests
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, date, time
import pytz

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8784820707:AAHCrpcPYvYwvFidjqq_dp6xnO2_bOJ49ks")
CHAT_ID   = os.environ.get("CHAT_ID",   "8666034530")
DATABASE_URL = os.environ.get("DATABASE_URL")   # Set on Railway

app = Flask(__name__, static_folder="static")
CORS(app)  # Allow Telegram Mini App cross-origin calls

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────
#  DB HELPERS
# ─────────────────────────────────────────────
def get_conn():
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL, sslmode="require")
    raise RuntimeError("DATABASE_URL env var not set")

def init_db():
    """Create table if it doesn't exist."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS reminders (
                        id            SERIAL PRIMARY KEY,
                        task          TEXT        NOT NULL,
                        reminder_date DATE        NOT NULL,
                        reminder_time TIME        NOT NULL,
                        status        VARCHAR(20) NOT NULL DEFAULT 'pending',
                        created_at    TIMESTAMP   DEFAULT NOW()
                    );
                """)
            conn.commit()
        log.info("✅ DB initialised")
    except Exception as e:
        log.error(f"DB init error: {e}")

# ─────────────────────────────────────────────
#  TELEGRAM HELPER
# ─────────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info(f"📨 Telegram sent: {message[:60]}")
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ─────────────────────────────────────────────
#  SCHEDULER — checks reminders every minute
# ─────────────────────────────────────────────
def check_and_fire_reminders():
    now_ist  = datetime.now(IST)
    today    = now_ist.date()
    now_time = now_ist.time().replace(second=0, microsecond=0)

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT id, task
                    FROM   reminders
                    WHERE  status        = 'pending'
                      AND  reminder_date = %s
                      AND  reminder_time BETWEEN %s AND %s::time + interval '1 minute'
                """, (today, now_time, now_time))

                rows = cur.fetchall()
                for row in rows:
                    send_telegram(
                        f"⏰ <b>Reminder!</b>\n\n"
                        f"📝 {row['task']}\n\n"
                        f"🕐 {now_ist.strftime('%I:%M %p')} IST"
                    )
                    cur.execute(
                        "UPDATE reminders SET status = 'notified' WHERE id = %s",
                        (row["id"],)
                    )
            conn.commit()
    except Exception as e:
        log.error(f"Scheduler error: {e}")

scheduler = BackgroundScheduler(timezone=IST)
scheduler.add_job(check_and_fire_reminders, "interval", minutes=1)

# ─────────────────────────────────────────────
#  SERVE MINI APP HTML
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "miniapp.html")

@app.route("/miniapp.html")
def miniapp():
    return send_from_directory(".", "miniapp.html")

# ─────────────────────────────────────────────
#  REST API — /api/reminders
# ─────────────────────────────────────────────
@app.route("/api/reminders", methods=["GET"])
def list_reminders():
    """Return all reminders, newest first."""
    status_filter = request.args.get("status")  # ?status=pending
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                if status_filter:
                    cur.execute(
                        "SELECT * FROM reminders WHERE status = %s ORDER BY reminder_date, reminder_time",
                        (status_filter,)
                    )
                else:
                    cur.execute(
                        "SELECT * FROM reminders ORDER BY reminder_date, reminder_time"
                    )
                rows = cur.fetchall()
        result = []
        for row in rows:
            result.append({
                "id":            row["id"],
                "task":          row["task"],
                "reminder_date": str(row["reminder_date"]),
                "reminder_time": str(row["reminder_time"])[:5],   # HH:MM
                "status":        row["status"],
            })
        return jsonify({"success": True, "data": result})
    except Exception as e:
        log.error(f"list_reminders error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/reminders", methods=["POST"])
def create_reminder():
    """Add a new reminder."""
    body = request.get_json(force=True)
    task          = (body.get("task") or "").strip()
    reminder_date = body.get("reminder_date")   # YYYY-MM-DD
    reminder_time = body.get("reminder_time")   # HH:MM

    if not task or not reminder_date or not reminder_time:
        return jsonify({"success": False, "error": "task, reminder_date, reminder_time are required"}), 400

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO reminders (task, reminder_date, reminder_time, status)
                    VALUES (%s, %s, %s, 'pending')
                    RETURNING id
                    """,
                    (task, reminder_date, reminder_time)
                )
                new_id = cur.fetchone()[0]
            conn.commit()

        send_telegram(
            f"✅ <b>New Reminder Set!</b>\n\n"
            f"📝 {task}\n"
            f"📅 {reminder_date}  🕐 {reminder_time} IST"
        )
        return jsonify({"success": True, "id": new_id}), 201
    except Exception as e:
        log.error(f"create_reminder error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/reminders/<int:reminder_id>/complete", methods=["PUT"])
def complete_reminder(reminder_id):
    """Mark a reminder as completed."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    "UPDATE reminders SET status = 'completed' WHERE id = %s RETURNING task",
                    (reminder_id,)
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"success": False, "error": "Not found"}), 404
                task = row["task"]
            conn.commit()

        send_telegram(f"🎉 <b>Task Completed!</b>\n\n✅ {task}")
        return jsonify({"success": True})
    except Exception as e:
        log.error(f"complete_reminder error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/reminders/<int:reminder_id>", methods=["DELETE"])
def delete_reminder(reminder_id):
    """Delete a reminder."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM reminders WHERE id = %s", (reminder_id,))
            conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        log.error(f"delete_reminder error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/stats", methods=["GET"])
def get_stats():
    """Dashboard counts."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, COUNT(*) FROM reminders GROUP BY status")
                rows = cur.fetchall()
        counts = {"pending": 0, "completed": 0, "notified": 0}
        for status, cnt in rows:
            counts[status] = cnt
        return jsonify({"success": True, "data": counts})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────
#  HEALTH CHECK (Railway uses this)
# ─────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(IST).isoformat()})


# ─────────────────────────────────────────────
#  TELEGRAM WEBHOOK (optional — set via /setWebhook)
# ─────────────────────────────────────────────
@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    try:
        msg   = update.get("message", {})
        text  = msg.get("text", "").strip()
        chat  = msg.get("chat", {}).get("id")

        if text == "/start":
            send_telegram(
                "👋 <b>Smart Reminder Bot</b>\n\n"
                "Open the Mini App below to manage your reminders! 📋"
            )
        elif text == "/list":
            with get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute(
                        "SELECT task, reminder_date, reminder_time FROM reminders "
                        "WHERE status = 'pending' ORDER BY reminder_date, reminder_time LIMIT 10"
                    )
                    rows = cur.fetchall()
            if rows:
                lines = [f"📋 <b>Pending Reminders</b>\n"]
                for r in rows:
                    lines.append(f"• {r['task']}  —  {r['reminder_date']} {str(r['reminder_time'])[:5]}")
                send_telegram("\n".join(lines))
            else:
                send_telegram("✅ No pending reminders!")
    except Exception as e:
        log.error(f"Webhook error: {e}")
    return jsonify({"ok": True})


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    scheduler.start()
    port = int(os.environ.get("PORT", 5000))
    log.info(f"🚀 Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
