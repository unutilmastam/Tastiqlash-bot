# =============================================================
#  "Unutilmas Ta'm" — Telegram tasdiqlash boti + Kuryer tizimi + HTTP API
#  Railway / Render uchun tayyor. aiogram 3.x + aiohttp + Firebase
#
#  YANGI: Kuryer bo'lish uchun botdan "so'rov" yuboriladi — faqat
#  ADMIN (siz) uni tasdiqlaganingizdan keyin haqiqiy kuryerga aylanadi.
#  Botdan o'zi ro'yxatdan o'tgan HAR KIM avtomatik kuryer bo'lib qolmaydi.
#
#  Muhit o'zgaruvchilari (Railway -> Variables):
#    BOT_TOKEN                — @BotFather bergan token (mavjud)
#    FIREBASE_SERVICE_ACCOUNT — Firebase xizmat hisobi JSON (yangi, to'liq matn)
#    ADMIN_CHAT_ID            — Sizning shaxsiy Telegram chat ID'ingiz (yangi)
#    PORT                     — Railway o'zi beradi (default 8080)
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
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0") or 0)
CODE_TTL = 300
MAX_TRIES = 5

sessions = {}
waiting = {}
# chat_id -> True (ism kutilayotgan, kuryerlikka so'rov uchun)
waiting_courier_name = {}
# vaqtincha: applicant_id -> {name, chat_id, username} — admin tasdiqlagunча
pending_applicants = {}

dp = Dispatcher()

# ---------------- Firebase ----------------
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

def uid(prefix):
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return prefix + "_" + "".join(secrets.choice(alphabet) for _ in range(7))

def get_couriers():
    db = get_db()
    if not db: return []
    doc = db.collection("app").document("couriers").get()
    if not doc.exists: return []
    data = doc.to_dict() or {}
    try:
        return json.loads(data.get("v", "[]"))
    except Exception:
        return []

def save_couriers(couriers):
    db = get_db()
    if not db: return
    db.collection("app").document("couriers").set({
        "v": json.dumps(couriers, ensure_ascii=False),
        "t": int(time.time() * 1000),
    })


def new_code():
    return f"{secrets.randbelow(900000) + 100000}"


# =========================================================
#  KURYERLIKKA SO'ROV — faqat ADMIN tasdiqlasa kuryer bo'ladi
# =========================================================

@dp.message(CommandStart(deep_link=True), F.text.startswith("/start kuryer"))
async def start_courier_request(m: Message):
    couriers = get_couriers()
    already = next((c for c in couriers if c.get("telegramChatId") == m.chat.id), None)
    if already:
        status = already.get("status", "active")
        if status == "active":
            await m.answer(f"Assalomu alaykum, {already['name']}! Siz allaqachon tasdiqlangan kuryersiz. ✅")
        else:
            await m.answer("So'rovingiz hali admin tomonidan ko'rib chiqilmoqda. Iltimos, kuting.")
        return
    waiting_courier_name[m.chat.id] = True
    await m.answer(
        "Assalomu alaykum! 🚴 «Unutilmas Ta'm»da kuryer bo'lish uchun so'rov yuborishingiz mumkin.\n\n"
        "To'liq ismingizni yozing (masalan: Bekzod Aliyev) — so'rovingiz administratorga yuboriladi "
        "va u tasdiqlagandan keyingina rasmiy kuryer bo'lasiz."
    )


@dp.message(F.text, F.chat.id.in_(waiting_courier_name))
async def courier_name_received(m: Message, bot: Bot):
    if not waiting_courier_name.get(m.chat.id):
        return
    name = m.text.strip()
    if len(name) < 2 or len(name) > 60:
        await m.answer("Iltimos, ismingizni to'g'ri kiriting.")
        return
    waiting_courier_name.pop(m.chat.id, None)

    applicant_id = uid("app")
    pending_applicants[applicant_id] = {
        "name": name,
        "chat_id": m.chat.id,
        "username": m.from_user.username or "",
    }
    await m.answer(
        "Rahmat! So'rovingiz administratorga yuborildi. ⏳\n"
        "Tasdiqlangach, sizga xabar keladi."
    )

    if ADMIN_CHAT_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"crapprove:{applicant_id}"),
            InlineKeyboardButton(text="❌ Rad etish", callback_data=f"crreject:{applicant_id}"),
        ]])
        uname_txt = f"@{m.from_user.username}" if m.from_user.username else "(username yo'q)"
        try:
            await bot.send_message(
                ADMIN_CHAT_ID,
                f"🚴 <b>Yangi kuryerlik so'rovi</b>\n\nIsm: {name}\nTelegram: {uname_txt}\n\n"
                "Tasdiqlaysizmi?",
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception as e:
            print("Adminga xabar yuborishda xato:", e)


@dp.callback_query(F.data.startswith("crapprove:"))
async def approve_courier(cb: CallbackQuery, bot: Bot):
    if not ADMIN_CHAT_ID or cb.message.chat.id != ADMIN_CHAT_ID:
        await cb.answer("Bu amal faqat admin uchun.", show_alert=True)
        return
    applicant_id = cb.data.split(":", 1)[1]
    applicant = pending_applicants.pop(applicant_id, None)
    if not applicant:
        await cb.answer("So'rov topilmadi (eskirgan bo'lishi mumkin).", show_alert=True)
        return
    couriers = get_couriers()
    courier = {
        "id": uid("cr"),
        "name": applicant["name"],
        "telegramChatId": applicant["chat_id"],
        "telegramUsername": applicant["username"],
        "status": "active",
    }
    couriers.append(courier)
    save_couriers(couriers)
    await cb.message.edit_text(f"✅ {applicant['name']} kuryer sifatida tasdiqlandi.")
    try:
        await bot.send_message(applicant["chat_id"], f"🎉 Tabriklaymiz, {applicant['name']}! Siz rasmiy kuryer sifatida tasdiqlandingiz.\n\nEndi sizga yetkazish topshiriqlari shu botga kelib turadi.")
    except Exception:
        pass
    await cb.answer("Tasdiqlandi")


@dp.callback_query(F.data.startswith("crreject:"))
async def reject_courier(cb: CallbackQuery, bot: Bot):
    if not ADMIN_CHAT_ID or cb.message.chat.id != ADMIN_CHAT_ID:
        await cb.answer("Bu amal faqat admin uchun.", show_alert=True)
        return
    applicant_id = cb.data.split(":", 1)[1]
    applicant = pending_applicants.pop(applicant_id, None)
    if not applicant:
        await cb.answer("So'rov topilmadi (eskirgan bo'lishi mumkin).", show_alert=True)
        return
    await cb.message.edit_text(f"❌ {applicant['name']}ning so'rovi rad etildi.")
    try:
        await bot.send_message(applicant["chat_id"], "So'rovingiz hozircha qabul qilinmadi.")
    except Exception:
        pass
    await cb.answer("Rad etildi")


@dp.message(F.text == "/kuryerlar")
async def list_couriers(m: Message):
    if not ADMIN_CHAT_ID or m.chat.id != ADMIN_CHAT_ID:
        return
    couriers = get_couriers()
    active = [c for c in couriers if c.get("status", "active") == "active"]
    lines = "\n".join(f"• {c['name']}" for c in active) or "Hali tasdiqlangan kuryer yo'q."
    pending_lines = "\n".join(f"• {a['name']}" for a in pending_applicants.values()) or "Yo'q"
    await m.answer(
        f"🚴 <b>Tasdiqlangan kuryerlar:</b>\n{lines}\n\n"
        f"⏳ <b>Kutilayotgan so'rovlar:</b>\n{pending_lines}",
        parse_mode="HTML",
    )


# =========================================================
#  OTP (o'zgarishsiz)
# =========================================================

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
        "Bu bot «Unutilmas Ta'm» ilovasi uchun tasdiqlash kodlarini yuboradi.\n"
        "Ro'yxatdan o'tishni ilovaning o'zidan boshlang."
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

    # Ilovaga kiritilgan raqam token ichida yashiringan (oxirgi "_" dan keyin).
    # Telegram'dan kelgan raqam bilan solishtiramiz — mos kelmasa, kod YUBORILMAYDI.
    expected_phone = token.rsplit("_", 1)[1] if "_" in token else None
    contact_phone_digits = "".join(ch for ch in m.contact.phone_number if ch.isdigit())

    if expected_phone and contact_phone_digits != expected_phone:
        await m.answer(
            "❗️ Bu Telegram akkountingizning raqami ilovada kiritgan raqamingiz bilan "
            "MOS KELMADI.\n\n"
            "Kod xavfsizlik uchun faqat ilovada ko'rsatgan raqamingiz bilan bir xil "
            "Telegram akkountiga yuboriladi. Iltimos:\n"
            "• Ilovada to'g'ri raqamni kiriting, YOKI\n"
            "• Shu raqam ro'yxatdan o'tgan Telegram akkountingizdan ulaning.",
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


# =========================================================
#  HTTP API
# =========================================================

def json_cors(data, status=200):
    return web.json_response(
        data,
        status=status,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
    )


async def verify_handler(request):
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


async def send_courier_handler(request, bot):
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


async def health(request):
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
