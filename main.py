import asyncio
import logging
import os
import random
import sys
from aiohttp import web
from aiogram import Bot, Dispatcher, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", 10000))
dp = Dispatcher()

# Ochilgan o'yinlar: {chat_id: {user_id: ism}}
games: dict[int, dict[int, str]] = {}
# Boshlangan o'yinlar: {chat_id: {user_id: rol}}
roles: dict[int, dict[int, str]] = {}
MIN_PLAYERS = 2  # sinash uchun vaqtincha 2 qilib turing


def players_text(chat_id: int) -> str:
    players = games[chat_id]
    if not players:
        return "Hozircha hech kim yo'q."
    lines = [f"{i}. {html.quote(name)}" for i, name in enumerate(players.values(), 1)]
    return f"Qatnashchilar ({len(players)}):\n" + "\n".join(lines)


def make_roles(n: int) -> list[str]:
    mafia = max(1, n // 4)
    result = ["🕵️ Mafia"] * mafia
    if n >= 3:
        result.append("👨‍⚕️ Shifokor")
    if n >= 4:
        result.append("🔎 Komissar")
    result += ["👤 Tinch aholi"] * (n - len(result))
    random.shuffle(result)
    return result


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        f"Salom, {html.bold(html.quote(message.from_user.full_name))}! "
        "Mafia botiga xush kelibsiz! 🐺\nBuyruqlar uchun /help"
    )


@dp.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(
        "🐺 <b>Mafia bot buyruqlari</b>\n\n"
        "/newgame - yangi o'yin ochish\n"
        "/join - o'yinga qo'shilish\n"
        "/leave - o'yindan chiqish\n"
        "/players - qatnashchilar ro'yxati\n"
        "/startgame - o'yinni boshlash\n"
        "/endgame - o'yinni tugatish\n"
        "/help - shu yordam xabari\n\n"
        "O'yinni guruh chatida o'ynang.\n"
        "Rol olish uchun avval menga shaxsiy chatda /start yuboring."
    )


@dp.message(Command("newgame"))
async def newgame_handler(message: Message) -> None:
    if message.chat.type == "private":
        await message.answer("O'yinni guruh chatida boshlang. Meni guruhga qo'shing.")
        return
    chat_id = message.chat.id
    if chat_id in games:
        await message.answer("Bu guruhda o'yin allaqachon ochilgan. /join bilan qo'shiling.")
        return
    games[chat_id] = {}
    await message.answer("🎮 Yangi o'yin ochildi! Qo'shilish uchun /join yuboring.")


@dp.message(Command("join"))
async def join_handler(message: Message) -> None:
    chat_id = message.chat.id
    if chat_id not in games:
        await message.answer("Avval /newgame bilan o'yin oching.")
        return
    if chat_id in roles:
        await message.answer("O'yin boshlanib bo'lgan, qo'shilib bo'lmaydi.")
        return
    user = message.from_user
    if user.id in games[chat_id]:
        await message.answer("Siz allaqachon o'yindasiz.")
        return
    games[chat_id][user.id] = user.full_name
    await message.answer(f"✅ {html.quote(user.full_name)} o'yinga qo'shildi!\n\n{players_text(chat_id)}")


@dp.message(Command("leave"))
async def leave_handler(message: Message) -> None:
    chat_id = message.chat.id
    user = message.from_user
    if chat_id not in games or user.id not in games[chat_id]:
        await message.answer("Siz o'yinda emassiz.")
        return
    if chat_id in roles:
        await message.answer("O'yin boshlangan, chiqib bo'lmaydi.")
        return
    del games[chat_id][user.id]
    await message.answer(f"👋 {html.quote(user.full_name)} o'yindan chiqdi.\n\n{players_text(chat_id)}")


@dp.message(Command("players"))
async def players_handler(message: Message) -> None:
    chat_id = message.chat.id
    if chat_id not in games:
        await message.answer("Bu guruhda o'yin yo'q. /newgame bilan oching.")
        return
    await message.answer(players_text(chat_id))


@dp.message(Command("startgame"))
async def startgame_handler(message: Message, bot: Bot) -> None:
    chat_id = message.chat.id
    if chat_id not in games:
        await message.answer("Avval /newgame bilan o'yin oching.")
        return
    if chat_id in roles:
        await message.answer("O'yin allaqachon boshlangan.")
        return
    players = games[chat_id]
    if len(players) < MIN_PLAYERS:
        await message.answer(f"Kamida {MIN_PLAYERS} o'yinchi kerak. Hozir: {len(players)}.")
        return

    assigned = dict(zip(players.keys(), make_roles(len(players))))
    failed = []
    for uid, role in assigned.items():
        try:
            await bot.send_message(uid, f"Sizning rolingiz: <b>{role}</b>\nSirni hech kimga aytmang! 🤫")
        except Exception:
            failed.append(players[uid])

    if failed:
        names = ", ".join(html.quote(n) for n in failed)
        await message.answer(
            f"⚠️ Bu o'yinchilarga xabar yuborib bo'lmadi: {names}\n"
            "Ular avval menga shaxsiy chatda /start yuborsin, keyin /startgame ni qayta yuboring."
        )
        return

    roles[chat_id] = assigned
    mafia_count = sum(1 for r in assigned.values() if "Mafia" in r)
    await message.answer(
        f"🌙 O'yin boshlandi! Qatnashchilar: {len(players)}\n"
        f"Mafia soni: {mafia_count}\nRollar shaxsiy xabarda yuborildi."
    )


@dp.message(Command("endgame"))
async def endgame_handler(message: Message) -> None:
    chat_id = message.chat.id
    if chat_id not in games:
        await message.answer("Bu guruhda o'yin yo'q.")
        return
    if chat_id in roles:
        lines = [
            f"{html.quote(games[chat_id][uid])}: {role}"
            for uid, role in roles[chat_id].items()
        ]
        await message.answer("🏁 O'yin tugadi! Rollar:\n" + "\n".join(lines))
    else:
        await message.answer("🏁 O'yin bekor qilindi.")
    games.pop(chat_id, None)
    roles.pop(chat_id, None)


async def on_startup(bot: Bot) -> None:
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:
        await bot.set_webhook(f"{url}/webhook")


async def main() -> None:
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    dp.startup.register(on_startup)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=PORT).start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
