import os
import sys
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

TOKEN = "9025565078:AAH2em1-DEk_BQncxQWfGuaqfE-YL8YRPoQ"

# Render beradigan veb-manzil (Render settings'dagi URL yoki avtomatik aniqlanadi)
# Render o'zi PORT muhit o'zgaruvchisini beradi
PORT = int(os.environ.get("PORT", 10000))

dp = Dispatcher()

@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    await message.answer(f"Salom, {html.bold(message.from_user.full_name)}! Mafia botiga xush kelibsiz! 🐺")

async def on_startup(bot: Bot) -> None:
    # Render'dagi ilovangizning umumiy tashqi havolasi (Render Dashboard'dan olib yozasiz)
    # Masalan: https://mafia-bot-1.onrender.com
    RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
        await bot.set_webhook(webhook_url)

async def main() -> None:
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    
    # Webhook uchun aiohttp serverini sozlaymiz
    app = web.Application()
    
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    
    dp.startup.register(on_startup)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    
    # Server to'xtab qolmasligi uchun cheksiz kutish
    await asyncio.Event().wait()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    import asyncio
    asyncio.run(main())
