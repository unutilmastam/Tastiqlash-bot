import asyncio
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

BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", 8080))
CODE_TTL = 300
MAX_TRIES = 5

sessions = {}
waiting = {}

dp = Dispatcher()


def new_code():
    return f"{secrets.randbelow(900000) + 100000}"


@dp.message(CommandStart(deep_link=True))
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


async def health(request):
    return json_cors({"ok": True, "service": "unutilmas-tasdiqlash"})


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
