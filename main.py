import asyncio
import json
import logging
import os
import random
import sys
import time
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
DATABASE_URL = os.environ.get("DATABASE_URL")
dp = Dispatcher()

MIN_PLAYERS = 4          # sinash uchun kamaytirishingiz mumkin
NIGHT_SECONDS = 60
DAY_SECONDS = 90

MAFIA = "🕵️ Mafia"
DON = "🎩 Don"
DOCTOR = "👨‍⚕️ Shifokor"
COMMISSAR = "🔎 Komissar"
MAYOR = "🏛 Mayor"
MANIAC = "🔪 Maniak"
CIVIL = "👤 Tinch aholi"
TEAM = (MAFIA, DON)

DESCRIPTIONS = {
    MAFIA: "Kechasi Mafia jamoasi bilan birga odam o'ldirasiz.",
    DON: "Mafia boshlig'i. Kechasi o'ldirish tanlovi sizniki. Komissar sizni 'mafia emas' deb ko'radi.",
    DOCTOR: "Kechasi bir kishini davolaysiz (o'zingizni ham). Davolangan kishi o'lmaydi.",
    COMMISSAR: "Kechasi bir kishini tekshirasiz: mafia yoki yo'qligini bilasiz (Don ko'rinmaydi).",
    MAYOR: "Kunduzgi ovoz berishda sizning ovozingiz 2 ta hisoblanadi.",
    MANIAC: "Yolg'iz o'ynaysiz. Kechasi bir kishini o'ldirasiz. Oxirgi qolsangiz yutasiz.",
    CIVIL: "Kunduzi muhokama qilib, mafiani toping.",
}

G: dict[int, dict] = {}      # {chat_id: holat}
pool = None
TASKS: set = set()


# ---------- HOLAT VA BAZA ----------
def new_state() -> dict:
    return {"players": {}, "started": False, "roles": {}, "alive": [],
            "phase": "lobby", "night": {}, "votes": {}, "deadline": 0, "round": 0}


def load_state(d: dict) -> dict:
    for k in ("players", "roles", "votes"):
        d[k] = {int(a): b for a, b in d[k].items()}
    return d


async def save(chat_id: int) -> None:
    if not pool or chat_id not in G:
        return
    try:
        await pool.execute(
            "INSERT INTO games(chat_id, data) VALUES($1, $2) "
            "ON CONFLICT (chat_id) DO UPDATE SET data = $2",
            chat_id, json.dumps(G[chat_id]),
        )
    except Exception:
        logging.exception("Bazaga yozishda xato")


async def finish(chat_id: int) -> None:
    G.pop(chat_id, None)
    if pool:
        try:
            await pool.execute("DELETE FROM games WHERE chat_id = $1", chat_id)
        except Exception:
            logging.exception("Bazadan o'chirishda xato")


# ---------- YORDAMCHI ----------
def players_text(s: dict) -> str:
    if not s["players"]:
        return "Hozircha hech kim yo'q."
    lines = [f"{i}. {html.quote(n)}" for i, n in enumerate(s["players"].values(), 1)]
    return f"Qatnashchilar ({len(s['players'])}):\n" + "\n".join(lines)


def make_roles(n: int) -> list[str]:
    result = [MAFIA] * max(1, n // 4)
    for role, min_n in [(DOCTOR, 3), (COMMISSAR, 4), (MAYOR, 5), (MANIAC, 6), (DON, 7)]:
        if n >= min_n and len(result) < n:
            result.append(role)
    result += [CIVIL] * (n - len(result))
    random.shuffle(result)
    return result


def alive_with(s: dict, *role_list: str) -> list[int]:
    return [u for u in s["alive"] if s["roles"][u] in role_list]


def check_winner(s: dict) -> str | None:
    r, alive = s["roles"], s["alive"]
    maf = sum(1 for u in alive if r[u] in TEAM)
    man = sum(1 for u in alive if r[u] == MANIAC)
    civ = len(alive) - maf - man
    if maf == 0 and man == 0:
        return "🎉 Tinch aholi yutdi! Mafia qolmadi."
    if maf == 0:
        return "🔪 Maniak yutdi!" if civ <= 1 else None
    if man == 0:
        return "😈 Mafia yutdi!" if maf >= civ else None
    if civ == 0:
        if maf > man:
            return "😈 Mafia yutdi!"
        if man > maf:
            return "🔪 Maniak yutdi!"
        return "🤝 Durang!"
    return None


def reveal_roles(s: dict) -> str:
    return "\n".join(f"{html.quote(s['players'][u])}: {r}" for u, r in s["roles"].items())


async def end_if_winner(bot: Bot, chat_id: int, text: str) -> bool:
    s = G[chat_id]
    winner = check_winner(s)
    if winner:
        await bot.send_message(chat_id, f"{text}\n\n{winner}\n\nRollar:\n{reveal_roles(s)}")
        await finish(chat_id)
        return True
    return False


async def is_admin(message: Message) -> bool:
    if message.chat.type == "private":
        return True
    member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
    return member.status in ("administrator", "creator")


def schedule(bot: Bot, chat_id: int, delay: float) -> None:
    s = G[chat_id]
    ph, rnd = s["phase"], s["round"]

    async def run() -> None:
        await asyncio.sleep(max(0, delay))
        s2 = G.get(chat_id)
        if not s2 or s2["phase"] != ph or s2["round"] != rnd:
            return
        try:
            if ph == "night":
                await resolve_night(bot, chat_id)
            elif ph == "day":
                await resolve_day(bot, chat_id)
        except Exception:
            logging.exception("Taymerda xato")

    task = asyncio.create_task(run())
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


# ---------- KECHA ----------
async def start_night(bot: Bot, chat_id: int) -> None:
    s = G[chat_id]
    s["phase"] = "night"
    s["round"] += 1
    s["night"] = {"kill": None, "heal": None, "check": None, "maniac": None}
    s["deadline"] = time.time() + NIGHT_SECONDS
    await save(chat_id)
    rnd = s["round"]
    await bot.send_message(
        chat_id,
        f"🌙 <b>{rnd}-kecha tushdi.</b> Maxsus rollar {NIGHT_SECONDS} soniya ichida tanlov qiladi...",
    )

    team_ids = set(alive_with(s, *TEAM))

    def kb(kind: str, exclude: set[int]) -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton(text=s["players"][t], callback_data=f"act:{kind}:{chat_id}:{t}:{rnd}")]
            for t in s["alive"] if t not in exclude
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    killers = alive_with(s, DON) or alive_with(s, MAFIA)
    jobs = [
        ("kill", killers, "🌙 <b>Kimni o'ldirasiz?</b>", team_ids),
        ("heal", alive_with(s, DOCTOR), "💉 <b>Kimni davolaysiz?</b>", set()),
        ("check", alive_with(s, COMMISSAR), "🔎 <b>Kimni tekshirasiz?</b>", None),
        ("maniac", alive_with(s, MANIAC), "🔪 <b>Kimni o'ldirasiz?</b>", None),
    ]
    for kind, uids, text, exclude in jobs:
        for uid in uids:
            ex = exclude if exclude is not None else {uid}
            try:
                await bot.send_message(uid, text, reply_markup=kb(kind, ex))
            except Exception:
                await bot.send_message(
                    chat_id,
                    f"⚠️ {html.quote(s['players'][uid])} ga xabar yuborib bo'lmadi. Avval botga /start yuborsin.",
                )
    schedule(bot, chat_id, NIGHT_SECONDS)


def night_complete(s: dict) -> bool:
    n = s["night"]
    if (alive_with(s, DON) or alive_with(s, MAFIA)) and n["kill"] is None:
        return False
    if alive_with(s, DOCTOR) and n["heal"] is None:
        return False
    if alive_with(s, COMMISSAR) and n["check"] is None:
        return False
    if alive_with(s, MANIAC) and n["maniac"] is None:
        return False
    return True


async def resolve_night(bot: Bot, chat_id: int) -> None:
    s = G.get(chat_id)
    if not s or s["phase"] != "night":
        return
    s["phase"] = "resolving"
    n = s["night"]
    victims = []
    for k in ("kill", "maniac"):
        t = n[k]
        if t is not None and t != n["heal"] and t in s["alive"] and t not in victims:
            victims.append(t)
    saved = n["heal"] is not None and n["heal"] in (n["kill"], n["maniac"])
    for v in victims:
        s["alive"].remove(v)

    text = "☀️ <b>Tong otdi!</b>\n"
    if victims:
        for v in victims:
            text += f"Tunda {html.quote(s['players'][v])} o'ldirildi. 💀\n"
    else:
        text += "Tunda hech kim o'lmadi.\n"
    if saved:
        text += "Shifokor kimnidir qutqardi! 💉"

    if await end_if_winner(bot, chat_id, text):
        return
    await bot.send_message(chat_id, text)
    await start_day(bot, chat_id)


@dp.callback_query(F.data.startswith("act:"))
async def act_handler(call: CallbackQuery, bot: Bot) -> None:
    _, kind, chat_s, target_s, rnd_s = call.data.split(":")
    chat_id, target, rnd = int(chat_s), int(target_s), int(rnd_s)
    uid = call.from_user.id
    s = G.get(chat_id)

    if not s or s["phase"] != "night" or s["round"] != rnd:
        await call.answer("Bu tugma eskirgan yoki hozir kecha emas.", show_alert=True)
        return
    need = {"kill": TEAM, "heal": (DOCTOR,), "check": (COMMISSAR,), "maniac": (MANIAC,)}[kind]
    if uid not in s["alive"] or s["roles"][uid] not in need:
        await call.answer("Bu sizga tegishli emas.", show_alert=True)
        return
    if kind == "kill" and s["roles"][uid] == MAFIA and alive_with(s, DON):
        await call.answer("Don tirik, tanlovni Don qiladi.", show_alert=True)
        return
    if target not in s["alive"]:
        await call.answer("Bu o'yinchi tirik emas.", show_alert=True)
        return
    if s["night"][kind] is not None:
        await call.answer("Tanlov allaqachon qilingan.", show_alert=True)
        return

    s["night"][kind] = target
    await save(chat_id)
    name = html.quote(s["players"][target])
    if kind == "check":
        result = "🕵️ MAFIA" if s["roles"][target] == MAFIA else "✅ Mafia emas"
        await call.message.edit_text(f"🔎 {name}: <b>{result}</b>")
    else:
        await call.message.edit_text(f"✅ Siz tanladingiz: {name}")
    await call.answer()
    if night_complete(s):
        await resolve_night(bot, chat_id)


# ---------- KUNDUZ ----------
async def start_day(bot: Bot, chat_id: int) -> None:
    s = G[chat_id]
    s["phase"] = "day"
    s["votes"] = {}
    s["deadline"] = time.time() + DAY_SECONDS
    await save(chat_id)
    rnd = s["round"]
    rows = [
        [InlineKeyboardButton(text=s["players"][t], callback_data=f"vote:{chat_id}:{t}:{rnd}")]
        for t in s["alive"]
    ]
    names = "\n".join(f"• {html.quote(s['players'][u])}" for u in s["alive"])
    await bot.send_message(
        chat_id,
        f"🗳 <b>Kunduz. Ovoz berish!</b> ({DAY_SECONDS} soniya)\nKimni osamiz? Tirik o'yinchilar:\n{names}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    schedule(bot, chat_id, DAY_SECONDS)


async def resolve_day(bot: Bot, chat_id: int) -> None:
    s = G.get(chat_id)
    if not s or s["phase"] != "day":
        return
    s["phase"] = "resolving"
    counts: Counter = Counter()
    for voter, target in s["votes"].items():
        counts[target] += 2 if s["roles"][voter] == MAYOR else 1

    if not counts:
        text = "🤷 Hech kim ovoz bermadi. Bugun hech kim osilmadi."
    else:
        top = counts.most_common()
        if len(top) > 1 and top[0][1] == top[1][1]:
            text = "⚖️ Ovozlar teng bo'ldi. Bugun hech kim osilmadi."
        else:
            victim = top[0][0]
            s["alive"].remove(victim)
            text = (
                f"⚰️ Ko'pchilik ovozi bilan {html.quote(s['players'][victim])} osildi.\n"
                f"Uning roli: {s['roles'][victim]}"
            )
    if await end_if_winner(bot, chat_id, text):
        return
    await bot.send_message(chat_id, text)
    await start_night(bot, chat_id)


@dp.callback_query(F.data.startswith("vote:"))
async def vote_handler(call: CallbackQuery, bot: Bot) -> None:
    _, chat_s, target_s, rnd_s = call.data.split(":")
    chat_id, target, rnd = int(chat_s), int(target_s), int(rnd_s)
    uid = call.from_user.id
    s = G.get(chat_id)

    if not s or s["phase"] != "day" or s["round"] != rnd:
        await call.answer("Hozir ovoz berish vaqti emas.", show_alert=True)
        return
    if uid not in s["alive"]:
        await call.answer("Faqat tirik o'yinchilar ovoz beradi.", show_alert=True)
        return
    if target not in s["alive"]:
        await call.answer("Bu o'yinchi tirik emas.", show_alert=True)
        return

    s["votes"][uid] = target
    await save(chat_id)
    await call.answer(f"Ovoz: {s['players'][target]}")
    if len(s["votes"]) >= len(s["alive"]):
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
        "/roles - rollar haqida\n"
        "/endvote - ovoz berishni tugatish (admin)\n"
        "/endgame - o'yinni tugatish (admin)\n\n"
        "Rol olish uchun avval menga shaxsiy chatda /start yuboring."
    )


@dp.message(Command("roles"))
async def roles_handler(message: Message) -> None:
    text = "🎭 <b>Rollar</b>\n\n" + "\n\n".join(f"{r}\n{d}" for r, d in DESCRIPTIONS.items())
    text += "\n\nO'yinchilar soni: 3+ shifokor, 4+ komissar, 5+ mayor, 6+ maniak, 7+ don."
    await message.answer(text)


@dp.message(Command("newgame"))
async def newgame_handler(message: Message) -> None:
    if message.chat.type == "private":
        await message.answer("O'yinni guruh chatida boshlang. Meni guruhga qo'shing.")
        return
    chat_id = message.chat.id
    if chat_id in G:
        await message.answer("Bu guruhda o'yin allaqachon ochilgan. /join bilan qo'shiling.")
        return
    G[chat_id] = new_state()
    await save(chat_id)
    await message.answer("🎮 Yangi o'yin ochildi! Qo'shilish uchun /join yuboring.")


@dp.message(Command("join"))
async def join_handler(message: Message) -> None:
    chat_id = message.chat.id
    s = G.get(chat_id)
    if not s:
        await message.answer("Avval /newgame bilan o'yin oching.")
        return
    if s["started"]:
        await message.answer("O'yin boshlanib bo'lgan, qo'shilib bo'lmaydi.")
        return
    user = message.from_user
    if user.id in s["players"]:
        await message.answer("Siz allaqachon o'yindasiz.")
        return
    s["players"][user.id] = user.full_name
    await save(chat_id)
    await message.answer(f"✅ {html.quote(user.full_name)} o'yinga qo'shildi!\n\n{players_text(s)}")


@dp.message(Command("leave"))
async def leave_handler(message: Message) -> None:
    chat_id = message.chat.id
    s = G.get(chat_id)
    user = message.from_user
    if not s or user.id not in s["players"]:
        await message.answer("Siz o'yinda emassiz.")
        return
    if s["started"]:
        await message.answer("O'yin boshlangan, chiqib bo'lmaydi.")
        return
    del s["players"][user.id]
    await save(chat_id)
    await message.answer(f"👋 {html.quote(user.full_name)} o'yindan chiqdi.\n\n{players_text(s)}")


@dp.message(Command("players"))
async def players_handler(message: Message) -> None:
    s = G.get(message.chat.id)
    if not s:
        await message.answer("Bu guruhda o'yin yo'q. /newgame bilan oching.")
        return
    await message.answer(players_text(s))


@dp.message(Command("startgame"))
async def startgame_handler(message: Message, bot: Bot) -> None:
    chat_id = message.chat.id
    s = G.get(chat_id)
    if not s:
        await message.answer("Avval /newgame bilan o'yin oching.")
        return
    if s["started"]:
        await message.answer("O'yin allaqachon boshlangan.")
        return
    players = s["players"]
    if len(players) < MIN_PLAYERS:
        await message.answer(f"Kamida {MIN_PLAYERS} o'yinchi kerak. Hozir: {len(players)}.")
        return

    assigned = dict(zip(players.keys(), make_roles(len(players))))
    team_names = ", ".join(html.quote(players[u]) for u, r in assigned.items() if r in TEAM)
    failed = []
    for uid, role in assigned.items():
        text = f"Sizning rolingiz: <b>{role}</b>\n{DESCRIPTIONS[role]}\nSirni hech kimga aytmang! 🤫"
        if role in TEAM:
            text += f"\n\nMafia jamoasi: {team_names}"
        try:
            await bot.send_message(uid, text)
        except Exception:
            failed.append(players[uid])

    if failed:
        names = ", ".join(html.quote(n) for n in failed)
        await message.answer(
            f"⚠️ Bu o'yinchilarga xabar yuborib bo'lmadi: {names}\n"
            "Ular avval menga shaxsiy chatda /start yuborsin, keyin /startgame ni qayta yuboring."
        )
        return

    s["started"] = True
    s["roles"] = assigned
    s["alive"] = list(assigned.keys())
    mafia_count = sum(1 for r in assigned.values() if r in TEAM)
    await message.answer(
        f"🎲 O'yin boshlandi! Qatnashchilar: {len(players)}\n"
        f"Mafia jamoasi: {mafia_count} kishi\nRollar shaxsiy xabarda yuborildi."
    )
    await start_night(bot, chat_id)


@dp.message(Command("endvote"))
async def endvote_handler(message: Message, bot: Bot) -> None:
    chat_id = message.chat.id
    s = G.get(chat_id)
    if not s or s["phase"] != "day":
        await message.answer("Hozir ovoz berish vaqti emas.")
        return
    if not await is_admin(message):
        await message.answer("Faqat guruh adminlari buni qila oladi.")
        return
    await resolve_day(bot, chat_id)


@dp.message(Command("endgame"))
async def endgame_handler(message: Message) -> None:
    chat_id = message.chat.id
    s = G.get(chat_id)
    if not s:
        await message.answer("Bu guruhda o'yin yo'q.")
        return
    if not await is_admin(message):
        await message.answer("Faqat guruh adminlari o'yinni tugata oladi.")
        return
    if s["started"]:
        await message.answer("🏁 O'yin tugadi! Rollar:\n" + reveal_roles(s))
    else:
        await message.answer("🏁 O'yin bekor qilindi.")
    await finish(chat_id)


# ---------- ISHGA TUSHIRISH ----------
async def init_db(bot: Bot) -> None:
    global pool
    if not DATABASE_URL:
        logging.warning("DATABASE_URL yo'q: o'yinlar faqat xotirada saqlanadi.")
        return
    import asyncpg

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    await pool.execute("CREATE TABLE IF NOT EXISTS games (chat_id BIGINT PRIMARY KEY, data TEXT NOT NULL)")
    rows = await pool.fetch("SELECT chat_id, data FROM games")
    for row in rows:
        try:
            s = load_state(json.loads(row["data"]))
        except Exception:
            logging.exception("Holatni o'qishda xato")
            continue
        G[row["chat_id"]] = s
        if s["phase"] in ("night", "day"):
            schedule(bot, row["chat_id"], max(3, s["deadline"] - time.time()))
    logging.info("Bazadan %d ta o'yin tiklandi", len(rows))


async def on_startup(bot: Bot) -> None:
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:
        await bot.set_webhook(f"{url}/webhook")
    await init_db(bot)


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
