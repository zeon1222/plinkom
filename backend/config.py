"""
Plinkom — Ortaq Konfiqurasiya
------------------------------
TƏHLÜKƏSİZLİK QEYDİ: Bu dəyərlər əvvəllər birbaşa bu faylda (və PUBLIC GitHub
repo-da) saxlanılırdı. İndi əvvəlcə ƏTRAF MÜHİT DƏYİŞƏNLƏRİNDƏN (environment
variables) oxunur — Render-də "Environment" bölməsində təyin et, kod isə
ictimai qala bilər, sirlər gizli qalır.

Render-də təyin etməli olduğun dəyişənlər (Dashboard -> sənin servis ->
Environment -> "Add Environment Variable"):

  BOT_TOKEN      -> BotFather-dan aldığın token
  ADMIN_CHAT_ID  -> sənin Telegram ID-n (ədəd)
  ADMIN_API_KEY  -> özün uydur, gizli bir söz
  DATABASE_URL   -> Neon Postgres connection string
  BOT_USERNAME   -> botunun @ olmadan username-i (məs: plinkom_bot)

Aşağıdakı fallback dəyərlər YALNIZ lokal (Termux) test üçündür — production-da
mütləq Render-in Environment bölməsindən təyin et.
"""

import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0") or "0")
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "change-me-to-a-strong-secret")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")  # @ olmadan, məs: plinkom_bot
