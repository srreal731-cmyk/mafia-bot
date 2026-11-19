import asyncio
import logging
import os
import random
import sys
from collections import Counter
from aiohttp import web
from aiogram import Bot, Dispatcher, F, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", 10000))
dp = Dispatcher()

MIN_PLAYERS = 4  # sinash uchun vaqtincha kamaytirishingiz mumkin

MAFIA = "🕵️ Mafia"
DOCTOR = "👨‍⚕️ Shifokor"
COMMISSAR = "🔎 Komissar"
CIVIL = "👤 Tinch aholi"

games: dict[int, dict[int, str]] = {}      # {chat: {uid: ism}}
roles: dict[int, dict[int, str]] = {}      # {chat: {uid: rol}}
alive: dict[int, set[int]] = {}            # {chat: {uid}}
phase: dict[int, str] = {}                 # {chat: "night" | "day"}
night: dict[int, dict] = {}                # {chat: {"kill","heal","check"}}
votes: dict[int, dict[int, int]] = {}      # {chat: {voter: target}}


def players_text(chat_id: int) -> str:
    players = games[chat_id]
    if not players:
        return "Hozircha hech kim yo'q."
    lines = [f"{i}. {html.quote(n)}" for i, n in enumerate(players.values(), 1)]
    return f"Qatnashchilar ({len(players)}):\n" + "\n".join(lines)


def make_roles(n: int) -> list[str]:
    result = [MAFIA] * max(1, n // 4)
    if n >= 3:
        result.append(DOCTOR)
    if n >= 4:
        result.append(COMMISSAR)
    result += [CIVIL] * (n - len(result))
    random.shuffle(result)
    return result


def check_winner(chat_id: int) -> str | None:
    mafia = sum(1 for u in alive[chat_id] if roles[chat_id][u] == MAFIA)
    others = len(alive[chat_id]) - mafia
    if mafia == 0:
        return "🎉 Tinch aholi yutdi! Mafia qolmadi."
    if mafia >= others:
        return "😈 Mafia yutdi!"
    return None


def reveal_roles(chat_id: int) -> str:
    return "\n".join(f"{html.quote(games[chat_id][u])}: {r}" for u, r in roles[chat_id].items())


def finish(chat_id: int) -> None:
    for d in (games, roles, alive, phase, night, votes):
        d.pop(chat_id, None)


def alive_with_role(chat_id: int, role: str) -> list[int]:
    return [u for u in alive[chat_id] if roles[chat_id][u] == role]


async def is_admin(message: Message) -> bool:
    if message.chat.type == "private":
        return True
    member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
    return member.status in ("administrator", "creator")


async def end_if_winner(bot: Bot, chat_id: int, text: str) -> bool:
    winner = check_winner(chat_id)
    if winner:
        await bot.send_message(chat_id, f"{text}\n\n{winner}\n\nRollar:\n{reveal_roles(chat_id)}")
        finish(chat_id)
        return True
    return False


# ---------- KECHA ----------
async def start_night(bot: Bot, chat_id: int) -> None:
    phase[chat_id] = "night"
    night[chat_id] = {"kill": None, "heal": None, "check": None}
    await bot.send_message(chat_id, "🌙 <b>Kecha tushdi.</b> Mafia, shifokor va komissar tanlov qilmoqda...")

    def kb(kind: str, exclude: set[int]) -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton(text=games[chat_id][t], callback_data=f"act:{kind}:{chat_id}:{t}")]
            for t in alive[chat_id] if t not in exclude
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    mafias = set(alive_with_role(chat_id, MAFIA))
    prompts = [
        ("kill", MAFIA, "🌙 <b>Kimni o'ldirasiz?</b>", mafias),
        ("heal", DOCTOR, "💉 <b>Kimni davolaysiz?</b>", set()),
        ("check", COMMISSAR, "🔎 <b>Kimni tekshirasiz?</b>", set()),
    ]
    for kind, role, text, exclude in prompts:
        for uid in alive_with_role(chat_id, role):
            ex = exclude if kind == "kill" else (exclude | {uid})
            try:
                await bot.send_message(uid, text, reply_markup=kb(kind, ex))
            except Exception:
                await bot.send_message(chat_id, f"⚠️ {html.quote(games[chat_id][uid])} ga xabar yuborib bo'lmadi. Avval botga /start yuborsin.")


async def try_resolve_night(bot: Bot, chat_id: int) -> None:
    n = night[chat_id]
    if n["kill"] is None:
        return
    if alive_with_role(chat_id, DOCTOR) and n["heal"] is None:
        return
    if alive_with_role(chat_id, COMMISSAR) and n["check"] is None:
        return

    victim = n["kill"]
    if victim == n["heal"]:
        text = "☀️ <b>Tong otdi!</b>\nTunda hech kim o'lmadi: shifokor qurbonni qutqardi! 💉"
    else:
        alive[chat_id].discard(victim)
        text = f"☀️ <b>Tong otdi!</b>\nTunda {html.quote(games[chat_id][victim])} o'ldirildi. 💀"
    if await end_if_winner(bot, chat_id, text):
        return
    await bot.send_message(chat_id, text)
    await start_day(bot, chat_id)


@dp.callback_query(F.data.startswith("act:"))
async def act_handler(call: CallbackQuery, bot: Bot) -> None:
    _, kind, chat_s, target_s = call.data.split(":")
    chat_id, target = int(chat_s), int(target_s)
    uid = call.from_user.id
    need = {"kill": MAFIA, "heal": DOCTOR, "check": COMMISSAR}[kind]

    if chat_id not in roles or phase.get(chat_id) != "night":
        await call.answer("Hozir kecha emas yoki o'yin tugagan.", show_alert=True)
        return
    if uid not in alive[chat_id] or roles[chat_id][uid] != need:
        await call.answer("Bu sizga tegishli emas.", show_alert=True)
        return
    if target not in alive[chat_id]:
        await call.answer("Bu o'yinchi tirik emas.", show_alert=True)
        return
    if night[chat_id][kind] is not None:
        await call.answer("Tanlov allaqachon qilingan.", show_alert=True)
        return

    night[chat_id][kind] = target
    name = html.quote(games[chat_id][target])
    if kind == "check":
        result = "🕵️ MAFIA" if roles[chat_id][target] == MAFIA else "✅ Mafia emas"
        await call.message.edit_text(f"🔎 {name}: <b>{result}</b>")
    else:
        await call.message.edit_text(f"✅ Siz tanladingiz: {name}")
    await call.answer()
    await try_resolve_night(bot, chat_id)


# ---------- KUNDUZ ----------
async def start_day(bot: Bot, chat_id: int) -> None:
    phase[chat_id] = "day"
    votes[chat_id] = {}
    rows = [
        [InlineKeyboardButton(text=games[chat_id][t], callback_data=f"vote:{chat_id}:{t}")]
        for t in alive[chat_id]
    ]
    names = "\n".join(f"• {html.quote(games[chat_id][u])}" for u in alive[chat_id])
    await bot.send_message(
        chat_id,
        f"🗳 <b>Kunduz. Ovoz berish!</b>\nKimni osamiz? Tirik o'yinchilar:\n{names}\n\n"
        "Hamma ovoz bergach natija chiqadi. Admin /endvote bilan tugatishi mumkin.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def resolve_day(bot: Bot, chat_id: int) -> None:
    counts = Counter(votes[chat_id].values())
    if not counts:
        text = "🤷 Hech kim ovoz bermadi. Bugun hech kim osilmadi."
    else:
        top = counts.most_common()
        if len(top) > 1 and top[0][1] == top[1][1]:
            text = "⚖️ Ovozlar teng bo'ldi. Bugun hech kim osilmadi."
        else:
            victim = top[0][0]
            alive[chat_id].discard(victim)
            text = (
                f"⚰️ Ko'pchilik ovozi bilan {html.quote(games[chat_id][victim])} osildi.\n"
                f"Uning roli: {roles[chat_id][victim]}"
            )
    if await end_if_winner(bot, chat_id, text):
        return
    await bot.send_message(chat_id, text)
    await start_night(bot, chat_id)


@dp.callback_query(F.data.startswith("vote:"))
async def vote_handler(call: CallbackQuery, bot: Bot) -> None:
    _, chat_s, target_s = call.data.split(":")
    chat_id, target = int(chat_s), int(target_s)
    uid = call.from_user.id

    if chat_id not in roles or phase.get(chat_id) != "day":
        await call.answer("Hozir ovoz berish vaqti emas.", show_alert=True)
        return
    if uid not in alive[chat_id]:
        await call.answer("Faqat tirik o'yinchilar ovoz beradi.", show_alert=True)
        return
    if target not in alive[chat_id]:
        await call.answer("Bu o'yinchi tirik emas.", show_alert=True)
        return

    votes[chat_id][uid] = target
    await call.answer(f"Ovoz: {games[chat_id][target]}")
    if len(votes[chat_id]) >= len(alive[chat_id]):
        phase[chat_id] = "resolving"
        await resolve_day(bot, chat_id)


# ---------- BUYRUQLAR ----------
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
        "/endvote - ovoz berishni tugatish (admin)\n"
        "/endgame - o'yinni tugatish (admin)\n"
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
    alive[chat_id] = set(assigned.keys())
    mafia_count = sum(1 for r in assigned.values() if r == MAFIA)
    await message.answer(
        f"🎲 O'yin boshlandi! Qatnashchilar: {len(players)}\n"
        f"Mafia soni: {mafia_count}\nRollar shaxsiy xabarda yuborildi."
    )
    await start_night(bot, chat_id)


@dp.message(Command("endvote"))
async def endvote_handler(message: Message, bot: Bot) -> None:
    chat_id = message.chat.id
    if phase.get(chat_id) != "day":
        await message.answer("Hozir ovoz berish vaqti emas.")
        return
    if not await is_admin(message):
        await message.answer("Faqat guruh adminlari buni qila oladi.")
        return
    phase[chat_id] = "resolving"
    await resolve_day(bot, chat_id)


@dp.message(Command("endgame"))
async def endgame_handler(message: Message) -> None:
    chat_id = message.chat.id
    if chat_id not in games:
        await message.answer("Bu guruhda o'yin yo'q.")
        return
    if not await is_admin(message):
        await message.answer("Faqat guruh adminlari o'yinni tugata oladi.")
        return
    if chat_id in roles:
        await message.answer("🏁 O'yin tugadi! Rollar:\n" + reveal_roles(chat_id))
    else:
        await message.answer("🏁 O'yin bekor qilindi.")
    finish(chat_id)


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
