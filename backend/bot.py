"""
Plinkom Telegram Bot
---------------------
1) İstifadəçilər üçün: /start -> "Oyna" düyməsi ilə Plinkom Web App-ı açır.
2) Sənin üçün (admin): balans əlavə etmək və çıxarış tələblərini
   təsdiqləmək/rədd etmək əmrləri.

Bu fayl artıq main.py ilə EYNİ prosesdə, arxa planda bir "thread" kimi
işləyir (main.py-nin startup hissəsinə bax). Ayrıca server/servis lazım
deyil — Render-in pulsuz Web Service planı kifayət edir.

VACİB: Bu bot HEÇ BİR real ödəniş göndərmir. /balans əmri sadəcə oyun daxili
virtual balansı dəyişir. Çıxarış təsdiqi də sadəcə "mən bu ödənişi əl ilə
etdim" işarəsidir — real bank/kripto köçürməsini sən özün, botdan kənarda
edirsən.
"""

import os
import time
import requests

from config import BOT_TOKEN, ADMIN_CHAT_ID, ADMIN_API_KEY, BOT_USERNAME

# =========================================================
# KONFİQURASİYA
# =========================================================
# BOT_TOKEN / ADMIN_CHAT_ID / ADMIN_API_KEY artıq config.py-dan gəlir —
# onları YALNIZ config.py-da doldurmaq kifayətdir.
#
# FRONTEND_URL — bu tək manual doldurmalı olduğun dəyərdir (Cloudflare
# Pages linkin). Aşağıda dəyiş:
FRONTEND_URL = "https://plinkom.yusifabbasli1222.workers.dev"

# Eyni proses daxilində özünə müraciət edir (Render PORT-u avtomatik verir)
BACKEND_URL = f"http://127.0.0.1:{os.environ.get('PORT', 8000)}"

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def tg_call(method, **params):
    r = requests.post(f"{TG_API}/{method}", json=params, timeout=30)
    return r.json()


def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_call("sendMessage", **payload)


def answer_callback(callback_id, text=""):
    return tg_call("answerCallbackQuery", callback_query_id=callback_id, text=text)


def edit_message(chat_id, message_id, text):
    return tg_call("editMessageText", chat_id=chat_id, message_id=message_id, text=text)


def backend_get(path, params=None):
    params = params or {}
    r = requests.get(f"{BACKEND_URL}{path}", params=params, timeout=10)
    return r.status_code, r.json()


def backend_post(path, body):
    r = requests.post(f"{BACKEND_URL}{path}", json=body, timeout=10)
    return r.status_code, r.json()


def is_admin(user_id):
    return ADMIN_CHAT_ID and int(user_id) == int(ADMIN_CHAT_ID)


# =========================================================
# Əmr icraçıları
# =========================================================

def handle_start(chat_id, payload=None):
    referrer_id = None
    if payload and payload.startswith("ref_"):
        candidate = payload[len("ref_"):]
        if candidate.isdigit() and candidate != str(chat_id):
            referrer_id = candidate

    # İstifadəçini qeydiyyatdan keçir (yoxdursa yaradır), referrer varsa bonusu tetikləyir
    params = {"ref": referrer_id} if referrer_id else {}
    backend_get(f"/api/user/{chat_id}", params)

    text = "🎰 Plinkom-a xoş gəldin!\nAşağıdaki düymə ilə oyunu aç."
    if BOT_USERNAME:
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{chat_id}"
        text += (
            f"\n\n👥 Dostlarını dəvət et — hər qoşulan dost üçün 0.25 ₼ qazan:\n"
            f"{ref_link}"
        )

    send_message(
        chat_id,
        text,
        reply_markup={
            "inline_keyboard": [[
                {"text": "🎮 Oyna", "web_app": {"url": FRONTEND_URL}}
            ]]
        },
    )


def handle_balans(chat_id, args):
    if len(args) < 2:
        send_message(chat_id, "İstifadə: /balans <telegram_id> <məbləğ> [qeyd]")
        return
    target_id, amount_str = args[0], args[1]
    note = " ".join(args[2:]) if len(args) > 2 else None
    try:
        amount = float(amount_str)
    except ValueError:
        send_message(chat_id, "Məbləğ düzgün deyil.")
        return

    status, data = backend_post("/api/admin/add-balance", {
        "admin_key": ADMIN_API_KEY,
        "telegram_id": target_id,
        "amount_azn": amount,
        "note": note,
    })
    if status == 200:
        send_message(
            chat_id,
            f"✅ {target_id} istifadəçisinə {amount:.2f} AZN əlavə olundu.\n"
            f"Yeni balans: {data['new_balance_azn']:.2f} AZN",
        )
    else:
        send_message(chat_id, f"❌ Xəta: {data.get('detail', 'naməlum')}")


def handle_pending(chat_id):
    status, data = backend_get("/api/admin/pending-withdrawals", {"admin_key": ADMIN_API_KEY})
    if status != 200:
        send_message(chat_id, f"❌ Xəta: {data.get('detail', 'naməlum')}")
        return
    if not data:
        send_message(chat_id, "Gözləyən çıxarış tələbi yoxdur. ✅")
        return
    for w in data:
        send_message(
            chat_id,
            f"🔔 Tələb #{w['id']}\n"
            f"İstifadəçi: {w['telegram_id']}\n"
            f"Məbləğ: {w['amount_azn']:.2f} AZN\n"
            f"Qeyd: {w['payout_note'] or '-'}\n"
            f"Tarix: {w['requested_at']}",
            reply_markup={
                "inline_keyboard": [[
                    {"text": "✅ Təsdiqlə", "callback_data": f"confirm_{w['id']}"},
                    {"text": "❌ Rədd et", "callback_data": f"reject_{w['id']}"},
                ]]
            },
        )


def handle_confirm(chat_id, request_id):
    status, data = backend_post("/api/admin/confirm-withdrawal", {
        "admin_key": ADMIN_API_KEY,
        "request_id": request_id,
    })
    if status == 200:
        send_message(chat_id, f"✅ Tələb #{request_id} təsdiqləndi. İstifadəçiyə bildiriş göndərildi.")
    else:
        send_message(chat_id, f"❌ Xəta: {data.get('detail', 'naməlum')}")


def handle_reject(chat_id, request_id):
    status, data = backend_post("/api/admin/reject-withdrawal", {
        "admin_key": ADMIN_API_KEY,
        "request_id": request_id,
    })
    if status == 200:
        send_message(chat_id, f"↩️ Tələb #{request_id} rədd edildi, balans geri qaytarıldı.")
    else:
        send_message(chat_id, f"❌ Xəta: {data.get('detail', 'naməlum')}")


def handle_stats(chat_id):
    status, data = backend_get("/api/admin/stats", {"admin_key": ADMIN_API_KEY})
    if status != 200:
        send_message(chat_id, f"❌ Xəta: {data.get('detail', 'naməlum')}")
        return
    send_message(
        chat_id,
        "📊 Plinkom statistikası\n\n"
        f"👥 İstifadəçi sayı: {data['users_count']}\n"
        f"🎯 Cəmi mərc: {data['total_wagered_azn']:.2f} AZN\n"
        f"💸 Cəmi ödənilib (oyun daxili): {data['total_paid_out_azn']:.2f} AZN\n"
        f"📈 Ev qazancı: {data['house_profit_azn']:.2f} AZN\n"
        f"💰 İstifadəçilərdə qalan cəmi balans: {data['outstanding_balance_azn']:.2f} AZN\n\n"
        f"⏳ Gözləyən çıxarışlar: {data['pending_withdrawals_count']} ədəd "
        f"({data['pending_withdrawals_azn']:.2f} AZN)\n"
        f"✅ Tamamlanmış çıxarışlar: {data['completed_withdrawals_count']} ədəd "
        f"({data['completed_withdrawals_azn']:.2f} AZN)",
    )


# chat_id -> gözləyən broadcast mətni (təsdiq gözləyir)
PENDING_BROADCAST = {}


def handle_broadcast_command(chat_id, message_text):
    if not message_text.strip():
        send_message(chat_id, "İstifadə: /broadcast <mesaj mətni>")
        return
    PENDING_BROADCAST[chat_id] = message_text
    send_message(
        chat_id,
        f"📢 Bu mesaj BÜTÜN istifadəçilərə göndəriləcək:\n\n\"{message_text}\"\n\nƏminsən?",
        reply_markup={
            "inline_keyboard": [[
                {"text": "✅ Bəli, hamıya göndər", "callback_data": "bcast_yes"},
                {"text": "❌ Ləğv et", "callback_data": "bcast_no"},
            ]]
        },
    )


def handle_broadcast_confirm(chat_id):
    message_text = PENDING_BROADCAST.pop(chat_id, None)
    if not message_text:
        send_message(chat_id, "Gözləyən broadcast tapılmadı (vaxtı keçmiş ola bilər).")
        return
    status, data = backend_post("/api/admin/broadcast", {
        "admin_key": ADMIN_API_KEY,
        "message": message_text,
    })
    if status == 200:
        send_message(
            chat_id,
            f"✅ Göndərildi: {data['sent']}/{data['total_users']} istifadəçiyə "
            f"(uğursuz: {data['failed']}).",
        )
    else:
        send_message(chat_id, f"❌ Xəta: {data.get('detail', 'naməlum')}")


def handle_referral_info(chat_id):
    status, data = backend_get(f"/api/user/{chat_id}")
    if status != 200:
        send_message(chat_id, "Xəta baş verdi, yenidən cəhd et.")
        return
    if BOT_USERNAME:
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{chat_id}"
        send_message(
            chat_id,
            f"👥 Dəvət sistemi\n\n"
            f"Dəvət etdiyin: {data.get('referral_count', 0)} nəfər\n"
            f"Hər dəvət üçün: {data.get('referral_bonus_azn', 0.25):.2f} ₼\n\n"
            f"Öz linkin:\n{ref_link}",
        )
    else:
        send_message(chat_id, "Dəvət sistemi hələ tam quraşdırılmayıb.")


def handle_text_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = (msg.get("text") or "").strip()

    if text.startswith("/start"):
        parts_start = text.split(maxsplit=1)
        payload = parts_start[1].strip() if len(parts_start) > 1 else None
        handle_start(chat_id, payload)
        return

    if text.startswith("/referral"):
        handle_referral_info(chat_id)
        return

    if not is_admin(user_id):
        if text.startswith("/"):
            send_message(chat_id, "Bu əmr yalnız admin üçündür.")
        return

    parts = text.split()
    cmd = parts[0] if parts else ""

    if cmd == "/balans":
        handle_balans(chat_id, parts[1:])
    elif cmd == "/pending":
        handle_pending(chat_id)
    elif cmd == "/confirm" and len(parts) > 1:
        handle_confirm(chat_id, int(parts[1]))
    elif cmd == "/reject" and len(parts) > 1:
        handle_reject(chat_id, int(parts[1]))
    elif cmd == "/stats":
        handle_stats(chat_id)
    elif cmd == "/broadcast":
        message_text = text[len("/broadcast"):].strip()
        handle_broadcast_command(chat_id, message_text)
    elif cmd == "/help":
        send_message(
            chat_id,
            "Admin əmrləri:\n"
            "/balans <telegram_id> <məbləğ> [qeyd] — balans əlavə et\n"
            "/pending — gözləyən çıxarış tələbləri\n"
            "/confirm <id> — tələbi təsdiqlə\n"
            "/reject <id> — tələbi rədd et (balans geri qayıdır)\n"
            "/stats — ümumi biznes statistikası\n"
            "/broadcast <mesaj> — bütün istifadəçilərə eyni anda mesaj göndər",
        )


def handle_callback_query(cb):
    user_id = cb["from"]["id"]
    data = cb.get("data", "")
    callback_id = cb["id"]
    chat_id = cb["message"]["chat"]["id"]

    if not is_admin(user_id):
        answer_callback(callback_id, "Yalnız admin üçün.")
        return

    if data.startswith("confirm_"):
        request_id = int(data.split("_", 1)[1])
        handle_confirm(chat_id, request_id)
        answer_callback(callback_id, "Təsdiqləndi ✅")
    elif data.startswith("reject_"):
        request_id = int(data.split("_", 1)[1])
        handle_reject(chat_id, request_id)
        answer_callback(callback_id, "Rədd edildi ❌")
    elif data == "bcast_yes":
        handle_broadcast_confirm(chat_id)
        answer_callback(callback_id, "Göndərilir...")
    elif data == "bcast_no":
        PENDING_BROADCAST.pop(chat_id, None)
        send_message(chat_id, "Ləğv edildi.")
        answer_callback(callback_id, "Ləğv edildi")
    else:
        answer_callback(callback_id)


# =========================================================
# Long polling loop
# =========================================================

def main():
    print("Plinkom bot işə düşdü. Dayandırmaq üçün Ctrl+C.")
    offset = 0
    while True:
        try:
            r = requests.get(
                f"{TG_API}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=40,
            )
            updates = r.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                if "message" in update:
                    handle_text_message(update["message"])
                elif "callback_query" in update:
                    handle_callback_query(update["callback_query"])
        except Exception as e:
            print("Xəta:", e)
            time.sleep(3)


if __name__ == "__main__":
    main()

