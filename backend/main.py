"""
Plinkom Backend v3
-------------------
Virtual balans, 24 saatlıq bonus, 3 risk səviyyəli provably-fair Plinko,
mərc tarixçəsi, statistika, referral sistemi.

Verilənlər bazası: Postgres (Neon) — SQLite-dan fərqli olaraq server
restart/redeploy olanda MƏLUMAT İTMİR.

VACİB QEYD:
Bu backend heç bir real pul köçürməsi HƏYATA KEÇİRMİR.
- Depozit sistemi yoxdur.
- "Çıxarış tələbi" endpoint-i yalnız tələbi bazaya yazır (status=pending).
  Real ödənişi (kripto və s.) sən özün, öz seçdiyin qanuni yolla, əl ilə
  və ya öz inteqrasiya etdiyin lisenziyalı provayder vasitəsilə edirsən.
"""

import hashlib
import hmac
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional, Literal

import psycopg2
import psycopg2.extras
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import BOT_TOKEN, ADMIN_CHAT_ID, ADMIN_API_KEY, DATABASE_URL

# ---- Admin / Bot konfiqurasiyası ----
# BOT_TOKEN / ADMIN_CHAT_ID / ADMIN_API_KEY / DATABASE_URL config.py-dan
# (əslində Render-in Environment bölməsindən) gəlir.


def send_telegram_message(chat_id, text, reply_markup=None):
    """Bot API vasitəsilə mesaj göndərir. Token təyin olunmayıbsa səssizcə keçir."""
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE" or not chat_id:
        return
    try:
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=5,
        )
    except Exception:
        pass  # bildiriş uğursuz olsa belə əsas əməliyyatı pozmasın


def require_admin(admin_key: str):
    if not admin_key or admin_key != ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Admin icazəsi yoxdur.")


# ---- Oyun konfiqurasiyası ----
# 9 sıra peg -> 10 yuva (bin), hər risk səviyyəsi üçün ayrı əmsal cədvəli.
ROWS = 9
RISK_TABLES = {
    "LOW":    [2.8, 1.5, 1.2, 1.0, 0.8, 0.8, 1.0, 1.2, 1.5, 2.8],   # RTP ~95.5%
    "MEDIUM": [12,  3.5, 1.8, 1.0, 0.5, 0.5, 1.0, 1.8, 3.5, 12],    # RTP ~99.6%
    "HIGH":   [65,  10,  1.6, 0.3, 0.1, 0.1, 0.3, 1.6, 10,  65],    # RTP ~98.1%
}
DEFAULT_RISK = "MEDIUM"

STARTING_BALANCE_QEPIK = 150      # 2 AZN
BONUS_AMOUNT_QEPIK = 50           # 0.5 AZN
BONUS_INTERVAL_SECONDS = 24 * 60 * 60
MIN_WITHDRAWAL_QEPIK = 1000        # 5 AZN
MIN_BET_QEPIK = 10                # 0.10 AZN
MAX_BET_QEPIK = 15000             # 150 AZN
REFERRAL_BONUS_QEPIK = 25         # 0.25 AZN — dəvət edənə, dəvət olunan qoşulanda
HISTORY_LIMIT = 25


def qepik_to_azn(q: int) -> float:
    return round(q / 100, 2)


# =========================================================
# Verilənlər Bazası (Postgres / Neon)
# =========================================================

class DBWrapper:
    """sqlite3-ün 'db.execute(...).fetchone()/fetchall()' stilini saxlayan
    kiçik köməkçi — '?' placeholder-ləri avtomatik '%s'-ə çevirir."""

    def __init__(self, conn):
        self.conn = conn
        self.cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, query, params=()):
        pg_query = query.replace("?", "%s")
        self.cur.execute(pg_query, params)
        return self

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()


@contextmanager
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        wrapper = DBWrapper(conn)
        yield wrapper
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id TEXT PRIMARY KEY,
                balance_qepik INTEGER NOT NULL DEFAULT 0,
                last_bonus_claim TEXT,
                server_seed TEXT NOT NULL,
                server_seed_hash TEXT NOT NULL,
                client_seed TEXT NOT NULL,
                nonce INTEGER NOT NULL DEFAULT 0,
                total_wagered_qepik INTEGER NOT NULL DEFAULT 0,
                total_won_qepik INTEGER NOT NULL DEFAULT 0,
                drops_count INTEGER NOT NULL DEFAULT 0,
                best_multiplier REAL NOT NULL DEFAULT 0,
                referred_by TEXT,
                referral_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS bets (
                id SERIAL PRIMARY KEY,
                telegram_id TEXT NOT NULL,
                risk TEXT NOT NULL,
                bet_qepik INTEGER NOT NULL,
                bin_index INTEGER NOT NULL,
                multiplier REAL NOT NULL,
                payout_qepik INTEGER NOT NULL,
                server_seed_hash TEXT NOT NULL,
                client_seed TEXT NOT NULL,
                nonce INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id SERIAL PRIMARY KEY,
                telegram_id TEXT NOT NULL,
                amount_qepik INTEGER NOT NULL,
                payout_note TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_at TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        # köhnə bazalarda bu sütunlar yoxdursa əlavə et (Postgres bunu təhlükəsiz dəstəkləyir)
        db.execute("ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS completed_at TEXT")
        db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by TEXT")
        db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_count INTEGER NOT NULL DEFAULT 0")


def new_server_seed_pair():
    seed = secrets.token_hex(32)
    seed_hash = hashlib.sha256(seed.encode()).hexdigest()
    return seed, seed_hash


def get_or_create_user(db, telegram_id: str, referred_by: Optional[str] = None):
    db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = db.fetchone()
    if row:
        return row

    seed, seed_hash = new_server_seed_pair()
    client_seed = secrets.token_hex(8)
    now = datetime.now(timezone.utc).isoformat()

    # özünü referral etməyin qarşısını al
    if referred_by == telegram_id:
        referred_by = None

    db.execute(
        """INSERT INTO users
           (telegram_id, balance_qepik, last_bonus_claim, server_seed,
            server_seed_hash, client_seed, nonce, total_wagered_qepik,
            total_won_qepik, drops_count, best_multiplier, referred_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, ?, ?)""",
        (telegram_id, STARTING_BALANCE_QEPIK, None, seed, seed_hash, client_seed, referred_by, now),
    )
    db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    new_row = db.fetchone()

    # Referral bonusu: yalnız YENİ istifadəçi yaradılanda, bir dəfə
    if referred_by:
        db.execute("SELECT * FROM users WHERE telegram_id = ?", (referred_by,))
        referrer = db.fetchone()
        if referrer:
            new_ref_balance = referrer["balance_qepik"] + REFERRAL_BONUS_QEPIK
            new_ref_count = referrer["referral_count"] + 1
            db.execute(
                "UPDATE users SET balance_qepik = ?, referral_count = ? WHERE telegram_id = ?",
                (new_ref_balance, new_ref_count, referred_by),
            )
            send_telegram_message(
                referred_by,
                f"🎉 Dəvət etdiyin bir dost Plinkom-a qoşuldu!\n"
                f"Hesabına {qepik_to_azn(REFERRAL_BONUS_QEPIK):.2f} ₼ əlavə olundu.\n"
                f"Yeni balans: {qepik_to_azn(new_ref_balance):.2f} ₼\n"
                f"Cəmi dəvət etdiyin: {new_ref_count} nəfər",
            )

    return new_row


def compute_drop(server_seed: str, client_seed: str, nonce: int):
    """9 sıra üçün HMAC-SHA256 əsasında sol/sağ yol -> yuva indeksi (0-9).
    Path da qaytarılır ki, frontend animasiyası dəqiq bu yolu göstərsin."""
    msg = f"{client_seed}:{nonce}".encode()
    digest = hmac.new(server_seed.encode(), msg, hashlib.sha256).digest()
    path = []
    rights = 0
    for i in range(ROWS):
        byte = digest[i % len(digest)]
        step_right = 1 if byte % 2 == 1 else 0
        path.append(step_right)
        rights += step_right
    return rights, path


app = FastAPI(title="Plinkom API v3")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    if not DATABASE_URL:
        print("⚠️ DATABASE_URL təyin olunmayıb (config.py / Render Environment) — baza işləməyəcək.")
    else:
        init_db()

    # Bot-u eyni prosesdə, arxa planda (thread) başlat.
    if BOT_TOKEN and BOT_TOKEN != "PUT_YOUR_BOT_TOKEN_HERE":
        try:
            from bot import main as bot_main
            t = threading.Thread(target=bot_main, daemon=True)
            t.start()
            print("✅ Telegram bot arxa planda başladıldı.")
        except Exception as e:
            print(f"⚠️ Bot başladıla bilmədi: {e}")
    else:
        print("ℹ️ BOT_TOKEN təyin olunmayıb — bot başladılmadı.")


RiskLevel = Literal["LOW", "MEDIUM", "HIGH"]


class BetRequest(BaseModel):
    telegram_id: str
    bet_qepik: int = Field(gt=0)
    risk: RiskLevel = DEFAULT_RISK


class WithdrawRequest(BaseModel):
    telegram_id: str
    amount_qepik: int = Field(gt=0)
    payout_note: Optional[str] = None


class ClientSeedRequest(BaseModel):
    telegram_id: str
    client_seed: str


class AdminAddBalanceRequest(BaseModel):
    admin_key: str
    telegram_id: str
    amount_azn: float = Field(gt=0)
    note: Optional[str] = None


class AdminWithdrawActionRequest(BaseModel):
    admin_key: str
    request_id: int


class AdminBroadcastRequest(BaseModel):
    admin_key: str
    message: str


def bonus_status(last_claim_iso: Optional[str]):
    if not last_claim_iso:
        return True, 0
    last = datetime.fromisoformat(last_claim_iso)
    now = datetime.now(timezone.utc)
    elapsed = (now - last).total_seconds()
    remaining = BONUS_INTERVAL_SECONDS - elapsed
    if remaining <= 0:
        return True, 0
    return False, int(remaining)


@app.get("/api/user/{telegram_id}")
def get_user(telegram_id: str, ref: Optional[str] = None):
    with get_db() as db:
        user = get_or_create_user(db, telegram_id, referred_by=ref)
        ready, remaining = bonus_status(user["last_bonus_claim"])
        return {
            "telegram_id": telegram_id,
            "balance_azn": qepik_to_azn(user["balance_qepik"]),
            "bonus_ready": ready,
            "bonus_seconds_remaining": remaining,
            "server_seed_hash": user["server_seed_hash"],
            "client_seed": user["client_seed"],
            "nonce": user["nonce"],
            "min_withdrawal_azn": qepik_to_azn(MIN_WITHDRAWAL_QEPIK),
            "min_bet_azn": qepik_to_azn(MIN_BET_QEPIK),
            "max_bet_azn": qepik_to_azn(MAX_BET_QEPIK),
            "referral_count": user["referral_count"],
            "referral_bonus_azn": qepik_to_azn(REFERRAL_BONUS_QEPIK),
            "stats": {
                "total_wagered_azn": qepik_to_azn(user["total_wagered_qepik"]),
                "total_won_azn": qepik_to_azn(user["total_won_qepik"]),
                "drops_count": user["drops_count"],
                "best_multiplier": user["best_multiplier"],
                "net_azn": qepik_to_azn(user["total_won_qepik"] - user["total_wagered_qepik"]),
            },
            "risk_tables": RISK_TABLES,
        }


@app.post("/api/bonus/{telegram_id}")
def claim_bonus(telegram_id: str):
    with get_db() as db:
        user = get_or_create_user(db, telegram_id)
        ready, remaining = bonus_status(user["last_bonus_claim"])
        if not ready:
            raise HTTPException(
                status_code=400,
                detail=f"Bonus hazır deyil. {remaining} saniyə qalıb.",
            )
        now = datetime.now(timezone.utc).isoformat()
        new_balance = user["balance_qepik"] + BONUS_AMOUNT_QEPIK
        db.execute(
            "UPDATE users SET balance_qepik = ?, last_bonus_claim = ? WHERE telegram_id = ?",
            (new_balance, now, telegram_id),
        )
        return {
            "claimed_azn": qepik_to_azn(BONUS_AMOUNT_QEPIK),
            "new_balance_azn": qepik_to_azn(new_balance),
        }


@app.post("/api/seed/rotate")
def rotate_seed(req: ClientSeedRequest):
    with get_db() as db:
        user = get_or_create_user(db, req.telegram_id)
        old_seed = user["server_seed"]
        old_hash = user["server_seed_hash"]
        new_seed, new_hash = new_server_seed_pair()
        db.execute(
            """UPDATE users SET server_seed = ?, server_seed_hash = ?,
               client_seed = ?, nonce = 0 WHERE telegram_id = ?""",
            (new_seed, new_hash, req.client_seed, req.telegram_id),
        )
        return {
            "revealed_previous_server_seed": old_seed,
            "previous_server_seed_hash": old_hash,
            "new_server_seed_hash": new_hash,
            "new_client_seed": req.client_seed,
        }


@app.post("/api/bet")
def place_bet(req: BetRequest):
    if req.bet_qepik < MIN_BET_QEPIK:
        raise HTTPException(status_code=400, detail=f"Minimum mərc {qepik_to_azn(MIN_BET_QEPIK)} ₼-dir.")
    if req.bet_qepik > MAX_BET_QEPIK:
        raise HTTPException(status_code=400, detail=f"Maksimum mərc {qepik_to_azn(MAX_BET_QEPIK)} ₼-dir.")

    multipliers = RISK_TABLES[req.risk]

    with get_db() as db:
        user = get_or_create_user(db, req.telegram_id)
        if user["balance_qepik"] < req.bet_qepik:
            raise HTTPException(status_code=400, detail="Balans kifayət etmir.")

        nonce = user["nonce"]
        bin_index, path = compute_drop(user["server_seed"], user["client_seed"], nonce)
        multiplier = multipliers[bin_index]
        payout_qepik = round(req.bet_qepik * multiplier)

        new_balance = user["balance_qepik"] - req.bet_qepik + payout_qepik
        new_total_wagered = user["total_wagered_qepik"] + req.bet_qepik
        new_total_won = user["total_won_qepik"] + payout_qepik
        new_drops = user["drops_count"] + 1
        new_best = max(user["best_multiplier"], multiplier)

        db.execute(
            """UPDATE users SET balance_qepik = ?, nonce = ?, total_wagered_qepik = ?,
               total_won_qepik = ?, drops_count = ?, best_multiplier = ?
               WHERE telegram_id = ?""",
            (new_balance, nonce + 1, new_total_wagered, new_total_won,
             new_drops, new_best, req.telegram_id),
        )
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            """INSERT INTO bets
               (telegram_id, risk, bet_qepik, bin_index, multiplier, payout_qepik,
                server_seed_hash, client_seed, nonce, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (req.telegram_id, req.risk, req.bet_qepik, bin_index, multiplier, payout_qepik,
             user["server_seed_hash"], user["client_seed"], nonce, now),
        )
        return {
            "bin_index": bin_index,
            "path": path,
            "multiplier": multiplier,
            "payout_azn": qepik_to_azn(payout_qepik),
            "new_balance_azn": qepik_to_azn(new_balance),
            "server_seed_hash": user["server_seed_hash"],
            "client_seed": user["client_seed"],
            "nonce": nonce,
            "stats": {
                "total_wagered_azn": qepik_to_azn(new_total_wagered),
                "total_won_azn": qepik_to_azn(new_total_won),
                "drops_count": new_drops,
                "best_multiplier": new_best,
            },
        }


@app.get("/api/history/{telegram_id}")
def get_history(telegram_id: str):
    with get_db() as db:
        db.execute(
            """SELECT risk, bet_qepik, bin_index, multiplier, payout_qepik, created_at
               FROM bets WHERE telegram_id = ? ORDER BY id DESC LIMIT ?""",
            (telegram_id, HISTORY_LIMIT),
        )
        rows = db.fetchall()
        return [
            {
                "risk": r["risk"],
                "bet_azn": qepik_to_azn(r["bet_qepik"]),
                "bin_index": r["bin_index"],
                "multiplier": r["multiplier"],
                "payout_azn": qepik_to_azn(r["payout_qepik"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]


@app.post("/api/withdraw")
def request_withdrawal(req: WithdrawRequest):
    with get_db() as db:
        user = get_or_create_user(db, req.telegram_id)
        if req.amount_qepik < MIN_WITHDRAWAL_QEPIK:
            raise HTTPException(
                status_code=400,
                detail=f"Minimum çıxarış {qepik_to_azn(MIN_WITHDRAWAL_QEPIK)} AZN-dir.",
            )
        if user["balance_qepik"] < req.amount_qepik:
            raise HTTPException(status_code=400, detail="Balans kifayət etmir.")

        new_balance = user["balance_qepik"] - req.amount_qepik
        db.execute(
            "UPDATE users SET balance_qepik = ? WHERE telegram_id = ?",
            (new_balance, req.telegram_id),
        )
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            """INSERT INTO withdrawals (telegram_id, amount_qepik, payout_note, status, requested_at)
               VALUES (?, ?, ?, 'pending', ?) RETURNING id""",
            (req.telegram_id, req.amount_qepik, req.payout_note, now),
        )
        request_id = db.fetchone()["id"]

        amount_azn = qepik_to_azn(req.amount_qepik)
        send_telegram_message(
            ADMIN_CHAT_ID,
            f"🔔 Yeni çıxarış tələbi #{request_id}\n"
            f"İstifadəçi: {req.telegram_id}\n"
            f"Məbləğ: {amount_azn} AZN\n"
            f"Qeyd: {req.payout_note or '-'}\n\n"
            f"Ödənişi etdikdən sonra botda təsdiqlə: /confirm {request_id}",
            reply_markup={
                "inline_keyboard": [[
                    {"text": "✅ Təsdiqlə", "callback_data": f"confirm_{request_id}"},
                    {"text": "❌ Rədd et", "callback_data": f"reject_{request_id}"},
                ]]
            },
        )

        return {
            "request_id": request_id,
            "status": "pending",
            "new_balance_azn": qepik_to_azn(new_balance),
            "note": "Tələbiniz qeydə alındı. Ödəniş operator tərəfindən əl ilə təsdiqlənəcək.",
        }


@app.get("/api/withdrawals/{telegram_id}")
def list_withdrawals(telegram_id: str):
    with get_db() as db:
        db.execute(
            "SELECT * FROM withdrawals WHERE telegram_id = ? ORDER BY id DESC",
            (telegram_id,),
        )
        rows = db.fetchall()
        return [
            {
                "id": r["id"],
                "amount_azn": qepik_to_azn(r["amount_qepik"]),
                "status": r["status"],
                "requested_at": r["requested_at"],
            }
            for r in rows
        ]


@app.get("/")
def health_check():
    return {"status": "ok", "service": "Plinkom API"}


@app.get("/api/leaderboard")
def leaderboard():
    with get_db() as db:
        db.execute(
            """SELECT telegram_id, total_wagered_qepik, total_won_qepik, best_multiplier, drops_count
               FROM users WHERE drops_count > 0
               ORDER BY (total_won_qepik - total_wagered_qepik) DESC LIMIT 10"""
        )
        rows = db.fetchall()
        result = []
        for r in rows:
            tid = r["telegram_id"]
            masked = tid[:2] + "***" + tid[-2:] if len(tid) > 4 else "***"
            result.append({
                "telegram_id_masked": masked,
                "net_azn": qepik_to_azn(r["total_won_qepik"] - r["total_wagered_qepik"]),
                "best_multiplier": r["best_multiplier"],
                "drops_count": r["drops_count"],
            })
        return result


@app.get("/api/admin/stats")
def admin_stats(admin_key: str):
    require_admin(admin_key)
    with get_db() as db:
        users_count = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        totals = db.execute(
            "SELECT COALESCE(SUM(total_wagered_qepik),0) w, COALESCE(SUM(total_won_qepik),0) p FROM users"
        ).fetchone()
        pending = db.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount_qepik),0) s FROM withdrawals WHERE status='pending'"
        ).fetchone()
        completed = db.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount_qepik),0) s FROM withdrawals WHERE status='completed'"
        ).fetchone()
        active_balance = db.execute(
            "SELECT COALESCE(SUM(balance_qepik),0) s FROM users"
        ).fetchone()["s"]

        return {
            "users_count": users_count,
            "total_wagered_azn": qepik_to_azn(totals["w"]),
            "total_paid_out_azn": qepik_to_azn(totals["p"]),
            "house_profit_azn": qepik_to_azn(totals["w"] - totals["p"]),
            "outstanding_balance_azn": qepik_to_azn(active_balance),
            "pending_withdrawals_count": pending["c"],
            "pending_withdrawals_azn": qepik_to_azn(pending["s"]),
            "completed_withdrawals_count": completed["c"],
            "completed_withdrawals_azn": qepik_to_azn(completed["s"]),
        }


@app.post("/api/admin/broadcast")
def admin_broadcast(req: AdminBroadcastRequest):
    require_admin(req.admin_key)
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="Mesaj boş ola bilməz.")

    with get_db() as db:
        rows = db.execute("SELECT telegram_id FROM users").fetchall()

    sent = 0
    failed = 0
    for r in rows:
        try:
            send_telegram_message(r["telegram_id"], req.message)
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.05)  # Telegram rate-limit-inə hörmətlə yanaşmaq üçün

    return {"total_users": len(rows), "sent": sent, "failed": failed}


@app.get("/api/multipliers")
def get_multipliers():
    return {"risk_tables": RISK_TABLES, "rows": ROWS}


# =========================================================
# ADMIN ENDPOINT-LƏRİ
# Bunlar yalnız bot.py (sənin BotFather botun) tərəfindən çağırılmalıdır,
# hər sorğu admin_key ilə qorunur (bax: ADMIN_API_KEY yuxarıda).
# =========================================================

@app.post("/api/admin/add-balance")
def admin_add_balance(req: AdminAddBalanceRequest):
    require_admin(req.admin_key)
    amount_qepik = round(req.amount_azn * 100)
    with get_db() as db:
        user = get_or_create_user(db, req.telegram_id)
        new_balance = user["balance_qepik"] + amount_qepik
        db.execute(
            "UPDATE users SET balance_qepik = ? WHERE telegram_id = ?",
            (new_balance, req.telegram_id),
        )
        send_telegram_message(
            req.telegram_id,
            f"💰 Hesabınıza {req.amount_azn:.2f} AZN əlavə olundu."
            + (f"\nQeyd: {req.note}" if req.note else "")
            + f"\nYeni balans: {qepik_to_azn(new_balance):.2f} AZN",
        )
        return {"telegram_id": req.telegram_id, "new_balance_azn": qepik_to_azn(new_balance)}


@app.get("/api/admin/pending-withdrawals")
def admin_pending_withdrawals(admin_key: str):
    require_admin(admin_key)
    with get_db() as db:
        db.execute(
            "SELECT * FROM withdrawals WHERE status = 'pending' ORDER BY id ASC"
        )
        rows = db.fetchall()
        return [
            {
                "id": r["id"],
                "telegram_id": r["telegram_id"],
                "amount_azn": qepik_to_azn(r["amount_qepik"]),
                "payout_note": r["payout_note"],
                "requested_at": r["requested_at"],
            }
            for r in rows
        ]


@app.post("/api/admin/confirm-withdrawal")
def admin_confirm_withdrawal(req: AdminWithdrawActionRequest):
    require_admin(req.admin_key)
    with get_db() as db:
        db.execute("SELECT * FROM withdrawals WHERE id = ?", (req.request_id,))
        row = db.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Tələb tapılmadı.")
        if row["status"] != "pending":
            raise HTTPException(status_code=400, detail=f"Tələb artıq '{row['status']}' statusundadır.")

        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE withdrawals SET status = 'completed', completed_at = ? WHERE id = ?",
            (now, req.request_id),
        )
        amount_azn = qepik_to_azn(row["amount_qepik"])
        send_telegram_message(
            row["telegram_id"],
            f"✅ Ödənişiniz təsdiqləndi!\n{amount_azn:.2f} AZN hesabınıza köçürüldü.",
        )
        return {"request_id": req.request_id, "status": "completed", "telegram_id": row["telegram_id"], "amount_azn": amount_azn}


@app.post("/api/admin/reject-withdrawal")
def admin_reject_withdrawal(req: AdminWithdrawActionRequest):
    """Tələbi rədd edir və məbləği istifadəçinin balansına geri qaytarır."""
    require_admin(req.admin_key)
    with get_db() as db:
        db.execute("SELECT * FROM withdrawals WHERE id = ?", (req.request_id,))
        row = db.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Tələb tapılmadı.")
        if row["status"] != "pending":
            raise HTTPException(status_code=400, detail=f"Tələb artıq '{row['status']}' statusundadır.")

        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE withdrawals SET status = 'rejected', completed_at = ? WHERE id = ?",
            (now, req.request_id),
        )
        # balansı geri qaytar
        db.execute("SELECT * FROM users WHERE telegram_id = ?", (row["telegram_id"],))
        user = db.fetchone()
        refunded_balance = user["balance_qepik"] + row["amount_qepik"]
        db.execute(
            "UPDATE users SET balance_qepik = ? WHERE telegram_id = ?",
            (refunded_balance, row["telegram_id"]),
        )
        amount_azn = qepik_to_azn(row["amount_qepik"])
        send_telegram_message(
            row["telegram_id"],
            f"❌ Çıxarış tələbiniz rədd edildi.\n{amount_azn:.2f} AZN balansınıza geri qaytarıldı.",
        )
        return {"request_id": req.request_id, "status": "rejected", "refunded_azn": amount_azn}

