# =============================================================
#  "Unutilmas Ta'm" — Telegram tasdiqlash boti + Kuryer tizimi + HTTP API
#  Railway / Render uchun tayyor. aiogram 3.x + aiohttp + Firebase
#
#  Muhit o'zgaruvchilari (Railway -> Variables):
#    BOT_TOKEN               — @BotFather bergan token
#    FIREBASE_SERVICE_ACCOUNT — Firebase xizmat hisobi JSON (butun matn)
#    PORT                    — Railway o'zi beradi (default 8080)
# =============================================================
import asyncio
import json
import os
import secrets
import time

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import firebase_admin
from firebase_admin import credentials, firestore

BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", 8080))
CODE_TTL = 300      # kod 5 daqiqa amal qiladi
MAX_TRIES = 5       # bitta kodga 5 marta urinish

# token -> {code, phone, chat, exp, tries}
sessions: dict[str, dict] = {}
# chat_id -> token (kontakt kutilayotgan foydalanuvchilar, OTP uchun)
waiting: dict[int, str] = {}
# chat_id -> True (ism kutilayotgan, kuryer ro'yxatdan o'tishi uchun)
waiting_courier_name: dict[int, bool] = {}

dp = Dispatcher()

# ---------- Firebase ----------
_db = None
def get_db():
    global _db
    if _db is not None:
        return _db
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
    if not raw:
        return None
    cred = credentials.Certificate(json.loads(raw))
    firebase_admin.initialize_app(cred)
    _db = firestore.client()
    return _db

def uid(prefix: str) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return prefix + "_" + "".join(secrets.choice(alphabet) for _ in range(7))

def get_couriers() -> list:
    db = get_db()
    if not db: return []
    doc = db.collection("app").document("couriers").get()
    if not doc.exists: return []
    data = doc.to_dict() or {}
    try:
        return json.loads(data.get("v", "[]"))
    except Exception:
        return []

def save_couriers(couriers: list):
    db = get_db()
    if not db: return
    db.collection("app").document("couriers").set({
        "v": json.dumps(couriers, ensure_ascii=False),
        "t": int(time.time() * 1000),
    })


def new_code() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


# ---------- BOT: OTP (o'zgarishsiz) ----------

@dp.message(CommandStart(deep_link=True), F.text.startswith("/start kuryer"))
async def start_courier_registration(m: Message):
    couriers = get_couriers()
    existing = next((c for c in couriers if c.get("telegramChatId") == m.chat.id), None)
    if existing:
        await m.answer(
            f"Assalomu alaykum, {existing['name']}! Siz allaqachon kuryer sifatida ro'yxatdan o'tgansiz. ✅\n"
            "Yangi yetkazishlar shu yerga keladi."
        )
        return
    waiting_courier_name[m.chat.id] = True
    await m.answer(
        "Assalomu alaykum! 🚴 «Unutilmas Ta'm» kuryerlar tizimiga xush kelibsiz!\n\n"
        "Ro'yxatdan o'tish uchun to'liq ismingizni yozing (masalan: Bekzod Aliyev):"
    )


@dp.message(F.text, F.chat.id.in_(waiting_courier_name))
async def courier_name_received(m: Message):
    if not waiting_courier_name.get(m.chat.id):
        return
    name = m.text.strip()
    if len(name) < 2 or len(name) > 60:
        await m.answer("Iltimos, ismingizni to'g'ri kiriting.")
        return
    waiting_courier_name.pop(m.chat.id, None)
    couriers = get_couriers()
    courier = {
        "id": uid("cr"),
        "name": name,
        "telegramChatId": m.chat.id,
        "telegramUsername": m.from_user.username or "",
    }
    couriers.append(courier)
    save_couriers(couriers)
    await m.answer(
        f"Rahmat, {name}! Siz kuryer sifatida ro'yxatdan o'tdingiz. ✅\n\n"
        "Endi ilova admin panelidan sizga yetkazish topshiriqlari yuborilishi mumkin — "
        "ular shu yerga, botga kelib turadi."
    )


@dp.message(CommandStart(deep_link=True), F.text.startswith("/start t"))
async def start_with_token(m: Message, command: CommandObject):
    token = (command.args or "").strip()
    if not token or len(token) > 64:
        await m.answer("Iltimos, ilovadagi tugma orqali kiring.")
        return
    waiting[m.from_user.id] = token
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await m.answer(
        "Assalomu alaykum! 👋\n\n"
        "«Unutilmas Ta'm» ilovasida ro'yxatdan o'tishni tasdiqlash uchun "
        "pastdagi tugma orqali telefon raqamingizni yuboring 👇",
        reply_markup=kb,
    )


@dp.message(CommandStart())
async def start_plain(m: Message):
    await m.answer(
        "Bu bot «Unutilmas Ta'm» ilovasi uchun tasdiqlash kodlarini yuboradi va "
        "kuryerlar bilan aloqa qiladi.\n"
        "Ro'yxatdan o'tishni ilovaning o'zidan boshlang — u sizni shu yerga olib keladi."
    )


@dp.message(F.contact)
async def got_contact(m: Message):
    if not m.contact or m.contact.user_id != m.from_user.id:
        await m.answer("Iltimos, tugma orqali O'ZINGIZNING raqamingizni yuboring.")
        return
    token = waiting.pop(m.from_user.id, None)
    if not token:
        await m.answer(
            "Sessiya topilmadi. Ilovadagi «Telegram orqali kod olish» tugmasini qayta bosing.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    code = new_code()
    sessions[token] = {
        "code": code,
        "phone": m.contact.phone_number,
        "chat": m.chat.id,
        "exp": time.time() + CODE_TTL,
        "tries": 0,
    }
    await m.answer(
        f"✅ Tasdiqlash kodingiz:\n\n<code>{code}</code>\n\n"
        "Ilovaga qaytib shu kodni kiriting. Kod 5 daqiqa amal qiladi.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="♻️ Yangi kod olish", callback_data=f"re:{token}")]]
    )
    await m.answer("Kod ishlamasa yoki eskirsa:", reply_markup=kb)


@dp.callback_query(F.data.startswith("re:"))
async def resend_code(cb: CallbackQuery):
    token = cb.data[3:]
    s = sessions.get(token)
    if not s or s["chat"] != cb.message.chat.id:
        await cb.answer("Sessiya eskirgan. Ilovadan qaytadan boshlang.", show_alert=True)
        return
    s["code"] = new_code()
    s["exp"] = time.time() + CODE_TTL
    s["tries"] = 0
    await cb.message.answer(
        f"♻️ Yangi kodingiz:\n\n<code>{s['code']}</code>",
        parse_mode="HTML",
    )
    await cb.answer("Yangi kod yuborildi")


# ---------- HTTP API ----------

def json_cors(data: dict, status: int = 200) -> web.Response:
    return web.json_response(
        data,
        status=status,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
    )


async def verify_handler(request: web.Request) -> web.Response:
    if request.method == "OPTIONS":
        return json_cors({})
    try:
        data = await request.json()
    except Exception:
        return json_cors({"ok": False, "error": "bad_request"})
    token = str(data.get("token", ""))[:64]
    code = str(data.get("code", "")).strip()

    s = sessions.get(token)
    if not s:
        return json_cors({"ok": False, "error": "not_found"})
    if time.time() > s["exp"]:
        sessions.pop(token, None)
        return json_cors({"ok": False, "error": "expired"})
    s["tries"] += 1
    if s["tries"] > MAX_TRIES:
        sessions.pop(token, None)
        return json_cors({"ok": False, "error": "too_many"})
    if code != s["code"]:
        return json_cors({"ok": False, "error": "wrong_code"})

    phone = s["phone"]
    sessions.pop(token, None)
    return json_cors({"ok": True, "phone": phone})


async def send_courier_handler(request: web.Request, bot: Bot) -> web.Response:
    """Ilova shu yerga so'rov yuboradi: {chat_id, message} -> kuryerga botdan xabar boradi."""
    if request.method == "OPTIONS":
        return json_cors({})
    try:
        data = await request.json()
    except Exception:
        return json_cors({"ok": False, "error": "bad_request"})
    chat_id = data.get("chat_id")
    message = str(data.get("message", "")).strip()
    if not chat_id or not message:
        return json_cors({"ok": False, "error": "missing_fields"})
    try:
        await bot.send_message(int(chat_id), message)
        return json_cors({"ok": True})
    except Exception as e:
        return json_cors({"ok": False, "error": str(e)})


async def health(request: web.Request) -> web.Response:
    return json_cors({"ok": True, "service": "unutilmas-tasdiqlash-va-kuryer"})


async def cleanup_loop():
    while True:
        now = time.time()
        for t in [t for t, s in sessions.items() if now > s["exp"] + 600]:
            sessions.pop(t, None)
        await asyncio.sleep(120)


async def main():
    bot = Bot(BOT_TOKEN)

    app = web.Application()
    app.router.add_route("POST", "/verify", verify_handler)
    app.router.add_route("OPTIONS", "/verify", verify_handler)
    app.router.add_route("POST", "/send-courier", lambda r: send_courier_handler(r, bot))
    app.router.add_route("OPTIONS", "/send-courier", lambda r: send_courier_handler(r, bot))
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"HTTP API {PORT}-portda ishga tushdi")

    asyncio.create_task(cleanup_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
