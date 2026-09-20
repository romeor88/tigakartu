import os, time, secrets, sqlite3
from urllib.parse import quote
from functools import wraps
import requests
from dotenv import load_dotenv
from flask import (
    Flask, g, redirect, render_template, request,
    session, url_for, jsonify, flash
)

load_dotenv()
APP_NAME = "Tiga Kartu"
DATABASE = os.environ.get("DATABASE_PATH", "tigakartu.db")
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(16))
ADMIN_PIN = os.environ.get("ADMIN_PIN", "2468")
ADMIN_WA = os.environ.get("ADMIN_WA", "6281215550238")
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4-1-fast")
XAI_URL = "https://api.x.ai/v1/chat/completions"

PACKS = {
    "satu": {
        "name": "1 pertanyaan",
        "base": 5000,
        "shots": 1,
        "label": "3 kartu + arti + 1 langkah",
    }
}

SYSTEM = """Kamu pembaca 3 kartu untuk hiburan. Bahasa Indonesia, hangat, tidak menghakimi.
Bukan dukun, bukan psikolog, bukan agama, bukan jaminan masa depan.

Format WAJIB:

KARTU 1 — [nama kartu pendek]
[3-5 kalimat arti]

KARTU 2 — [nama kartu pendek]
[3-5 kalimat arti]

KARTU 3 — [nama kartu pendek]
[3-5 kalimat arti]

SATU LANGKAH BESOK
[1-2 kalimat konkret, aman, tidak memaksa orang lain]

PENUTUP
Ini hiburan. Bukan ramalan pasti.

Aturan:
- Pakai nama dan pertanyaan user.
- Jangan janjikan dia kembali / putus / menikah.
- Jangan minta nomor HP, ritual, pelet, sesajen.
- Nama kartu boleh orisinal (bukan merek tarot terkenal jika bisa dihindari).
"""

app = Flask(__name__)
app.secret_key = SECRET_KEY

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = sqlite3.connect(DATABASE)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pack TEXT, amount INTEGER, status TEXT DEFAULT 'waiting',
            wa TEXT, token TEXT UNIQUE, shots_left INTEGER,
            nama_kamu TEXT, nama_dia TEXT, pertanyaan TEXT,
            created_at INTEGER, paid_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER, role TEXT, content TEXT, created_at INTEGER
        );
        """
    )
    db.commit()
    db.close()

def now():
    return int(time.time())

def fmt_rp(n):
    return f"Rp {n:,}".replace(",", ".")

def next_amount(db, pack):
    base = PACKS[pack]["base"]
    db.execute(
        "UPDATE orders SET status='expired' WHERE status='waiting' AND created_at < ?",
        (now() - 3 * 3600,),
    )
    used = {
        r["amount"]
        for r in db.execute(
            "SELECT amount FROM orders WHERE pack=? AND status='waiting'", (pack,)
        )
    }
    for s in range(1, 100):
        if base + s not in used:
            return base + s
    raise RuntimeError("kode habis")

def admin_required(fn):
    @wraps(fn)
    def w(*a, **k):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*a, **k)
    return w

def call_grok(messages):
    if not XAI_API_KEY:
        return (
            "KARTU 1 — Jarak\nMode tes. Isi XAI_API_KEY.\n\n"
            "KARTU 2 — Diam\nMasih dummy.\n\n"
            "KARTU 3 — Besok\nMasih dummy.\n\n"
            "SATU LANGKAH BESOK\nJangan spam chat.\n\n"
            "PENUTUP\nIni hiburan. Bukan ramalan pasti."
        )
    try:
        r = requests.post(
            XAI_URL,
            headers={
                "Authorization": f"Bearer {XAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": XAI_MODEL,
                "messages": messages,
                "temperature": 0.8,
                "max_tokens": 900,
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return "Gagal membaca kartu. Coba sekali lagi."

@app.route("/")
def index():
    return render_template("index.html", app_name=APP_NAME, packs=PACKS)

@app.route("/order", methods=["POST"])
def create_order():
    pack = request.form.get("pack", "satu")
    if pack not in PACKS:
        return "Pack salah", 400
    if request.form.get("age") != "yes":
        flash("Harus 18+")
        return redirect(url_for("index"))
    db = get_db()
    amount = next_amount(db, pack)
    token = secrets.token_urlsafe(16)
    db.execute(
        """INSERT INTO orders
           (pack,amount,wa,token,shots_left,nama_kamu,nama_dia,pertanyaan,created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            pack,
            amount,
            request.form.get("wa", "").strip(),
            token,
            PACKS[pack]["shots"],
            request.form.get("nama_kamu", "").strip(),
            request.form.get("nama_dia", "").strip(),
            request.form.get("pertanyaan", "").strip(),
            now(),
        ),
    )
    db.commit()
    return redirect(url_for("pay", amount=amount))

@app.route("/pay/<int:amount>")
def pay(amount):
    order = get_db().execute(
        "SELECT * FROM orders WHERE amount=? ORDER BY id DESC", (amount,)
    ).fetchone()
    if not order:
        return "Tidak ketemu", 404
    wa_link = (
        "https://wa.me/"
        + ADMIN_WA
        + "?text="
        + quote(f"Halo Tiga Kartu, sudah transfer {fmt_rp(order['amount'])}")
    )
    return render_template(
        "pay.html",
        order=order,
        pack=PACKS[order["pack"]],
        amount_fmt=fmt_rp(order["amount"]),
        wa_link=wa_link,
        app_name=APP_NAME,
    )

@app.route("/hasil/<token>")
def hasil(token):
    order = get_db().execute(
        "SELECT * FROM orders WHERE token=?", (token,)
    ).fetchone()
    if not order:
        return "Link salah", 404
    if order["status"] != "paid":
        return redirect(url_for("pay", amount=order["amount"]))
    msgs = get_db().execute(
        "SELECT role, content FROM messages WHERE order_id=? ORDER BY id",
        (order["id"],),
    ).fetchall()
    return render_template(
        "hasil.html", order=order, messages=msgs, app_name=APP_NAME, token=token
    )

@app.route("/api/gen", methods=["POST"])
def api_gen():
    data = request.get_json(force=True)
    token = data.get("token")
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE token=?", (token,)).fetchone()
    if not order or order["status"] != "paid":
        return jsonify({"error": "Belum aktif"}), 403
    if order["shots_left"] <= 0:
        return jsonify({"error": "Sudah kebaca. Beli pertanyaan baru."}), 403
    user_msg = (
        f"Namaku: {order['nama_kamu']}\n"
        f"Namanya: {order['nama_dia']}\n"
        f"Pertanyaan: {order['pertanyaan'] or 'Dia masih kepikiran aku nggak?'}\n"
        "Baca 3 kartu."
    )
    db.execute(
        "INSERT INTO messages (order_id,role,content,created_at) VALUES (?,?,?,?)",
        (order["id"], "user", user_msg, now()),
    )
    reply = call_grok(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_msg}]
    )
    db.execute(
        "INSERT INTO messages (order_id,role,content,created_at) VALUES (?,?,?,?)",
        (order["id"], "assistant", reply, now()),
    )
    db.execute(
        "UPDATE orders SET shots_left=? WHERE id=?",
        (order["shots_left"] - 1, order["id"]),
    )
    db.commit()
    return jsonify({"reply": reply, "shots_left": order["shots_left"] - 1})

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("pin") == ADMIN_PIN:
            session["admin"] = True
            return redirect(url_for("admin_home"))
        flash("PIN salah")
    return render_template("admin_login.html", app_name=APP_NAME)

@app.route("/admin/home")
@admin_required
def admin_home():
    db = get_db()
    return render_template(
        "admin.html",
        waiting=db.execute(
            "SELECT * FROM orders WHERE status='waiting' ORDER BY id DESC"
        ).fetchall(),
        paid=db.execute(
            "SELECT * FROM orders WHERE status='paid' ORDER BY id DESC LIMIT 30"
        ).fetchall(),
        app_name=APP_NAME,
        fmt_rp=fmt_rp,
    )

@app.route("/admin/pay/<int:order_id>", methods=["POST"])
@admin_required
def admin_mark_paid(order_id):
    db = get_db()
    db.execute(
        "UPDATE orders SET status='paid', paid_at=? WHERE id=? AND status='waiting'",
        (now(), order_id),
    )
    db.commit()
    o = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    flash(f"Aktif: {request.host_url}hasil/{o['token']}")
    return redirect(url_for("admin_home"))

init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5003)), debug=True)