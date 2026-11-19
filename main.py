import asyncio
import logging
import os
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

# Har bir chat uchun o'yin: {chat_id: {user_id: ism}}
games: dict[int, dict[int, str]] = {}


def players_text(chat_id: int) -> str:
    players = games[chat_id]
    if not players:
        return "Hozircha hech kim yo'q."
    lines = [f"{i}. {html.quote(name)}" for i, name in enumerate(players.values(), 1)]
    return f"Qatnashchilar ({len(players)}):\n" + "\n".join(lines)


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
        "/help - shu yordam xabari\n\n"
        "O'yinni guruh chatida o'ynang."
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
    del games[chat_id][user.id]
    await message.answer(f"👋 {html.quote(user.full_name)} o'yindan chiqdi.\n\n{players_text(chat_id)}")


@dp.message(Command("players"))
async def players_handler(message: Message) -> None:
    chat_id = message.chat.id
    if chat_id not in games:
        await message.answer("Bu guruhda o'yin yo'q. /newgame bilan oching.")
        return
    await message.answer(players_text(chat_id))


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
