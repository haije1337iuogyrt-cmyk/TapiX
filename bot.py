import asyncio, hashlib, json, os, secrets, time
from datetime import datetime, timedelta, timezone
import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, Update, MenuButtonWebApp
from aiogram.filters import Command
from aiohttp import web

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://tapix-jbjj.onrender.com")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
pool: asyncpg.Pool = None

# ===== НАСТРОЙКИ =====
LOTTERY_BET_SECONDS = 300
LOTTERY_REVEAL_SECONDS = 8
LOTTERY_MIN_BET = 100
LOTTERY_MAX_BET = 150_000
LOTTERY_HOUSE_EDGE = 0.95
MINER_GRID = 25
MINER_MIN_BET = 100
MINER_MAX_BET = 150_000

# Магазин: фиксированный бонус, нормальные цены
SHOP_TAP = [
    {'bonus': 0.0001, 'price': 50,     'mult': 1.4, 'label': 'Коготь I'},
    {'bonus': 0.0002, 'price': 200,    'mult': 1.4, 'label': 'Коготь II'},
    {'bonus': 0.0005, 'price': 700,    'mult': 1.4, 'label': 'Коготь III'},
    {'bonus': 0.001,  'price': 2500,   'mult': 1.4, 'label': 'Коготь IV'},
    {'bonus': 0.002,  'price': 8000,   'mult': 1.4, 'label': 'Коготь V'},
    {'bonus': 0.005,  'price': 25000,  'mult': 1.4, 'label': 'Коготь VI'},
]
SHOP_SEC = [
    {'bonus': 0.00005, 'price': 40,    'mult': 1.4, 'label': 'Авто I'},
    {'bonus': 0.0001,  'price': 150,   'mult': 1.4, 'label': 'Авто II'},
    {'bonus': 0.0002,  'price': 600,   'mult': 1.4, 'label': 'Авто III'},
    {'bonus': 0.0005,  'price': 2200,  'mult': 1.4, 'label': 'Авто IV'},
    {'bonus': 0.001,   'price': 7000,  'mult': 1.4, 'label': 'Авто V'},
    {'bonus': 0.003,   'price': 22000, 'mult': 1.4, 'label': 'Авто VI'},
]

# ===== КЭШ ДЛЯ ТОП-ЛИСТА =====
_top_cache = {'data': None, 'ts': 0}

# ===== СХЕМА БД =====
SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT, first_name TEXT, avatar_url TEXT,
    balance DOUBLE PRECISION DEFAULT 0,
    per_tap DOUBLE PRECISION DEFAULT 0.0001,
    per_sec DOUBLE PRECISION DEFAULT 0,
    total_earned DOUBLE PRECISION DEFAULT 0,
    total_taps BIGINT DEFAULT 0,
    level INTEGER DEFAULT 1,
    xp DOUBLE PRECISION DEFAULT 0,
    xp_max DOUBLE PRECISION DEFAULT 1000,
    referral_code TEXT UNIQUE,
    referred_by INTEGER,
    created_at TIMESTAMPTZ DEFAULT now(),
    last_active TIMESTAMPTZ,
    sound_on BOOLEAN DEFAULT TRUE,
    theme TEXT DEFAULT 'dark',
    shop_tap0 INT DEFAULT 0, shop_tap1 INT DEFAULT 0, shop_tap2 INT DEFAULT 0,
    shop_tap3 INT DEFAULT 0, shop_tap4 INT DEFAULT 0, shop_tap5 INT DEFAULT 0,
    shop_sec0 INT DEFAULT 0, shop_sec1 INT DEFAULT 0, shop_sec2 INT DEFAULT 0,
    shop_sec3 INT DEFAULT 0, shop_sec4 INT DEFAULT 0, shop_sec5 INT DEFAULT 0,
    boost_active BOOLEAN DEFAULT FALSE,
    boost_cooldown TIMESTAMPTZ,
    promo_used BOOLEAN DEFAULT FALSE,
    dildo_activated BOOLEAN DEFAULT FALSE,
    ban_until TIMESTAMPTZ DEFAULT NULL
);
CREATE TABLE IF NOT EXISTS transfers (
    id SERIAL PRIMARY KEY,
    from_telegram_id BIGINT, to_telegram_id BIGINT,
    amount DOUBLE PRECISION, fee DOUBLE PRECISION, total DOUBLE PRECISION,
    message TEXT, timestamp TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS lottery_rounds (
    id SERIAL PRIMARY KEY, seed TEXT, hash TEXT, inner_hash TEXT,
    start_time TIMESTAMPTZ, end_time TIMESTAMPTZ,
    winner_side TEXT, total_bank DOUBLE PRECISION DEFAULT 0,
    status TEXT DEFAULT 'betting'
);
CREATE TABLE IF NOT EXISTS lottery_bets (
    id SERIAL PRIMARY KEY, round_id INTEGER REFERENCES lottery_rounds(id),
    telegram_id BIGINT, side TEXT, amount DOUBLE PRECISION,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS game_history (
    id SERIAL PRIMARY KEY, telegram_id BIGINT,
    game TEXT, result TEXT, amount DOUBLE PRECISION,
    date TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS miner_sessions (
    telegram_id BIGINT PRIMARY KEY,
    bet DOUBLE PRECISION, bombs INTEGER,
    bomb_positions TEXT, opened INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lottery_bets_round ON lottery_bets(round_id);
CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance DESC);
CREATE INDEX IF NOT EXISTS idx_game_history_user ON game_history(telegram_id, game);
CREATE INDEX IF NOT EXISTS idx_transfers_user ON transfers(from_telegram_id, to_telegram_id);
ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_until TIMESTAMPTZ DEFAULT NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS shop_tap5 INT DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS shop_sec5 INT DEFAULT 0;
ALTER TABLE lottery_rounds ADD COLUMN IF NOT EXISTS inner_hash TEXT;

'''

async def init_db():
    global pool
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=5,
        max_size=20,
        command_timeout=10,
        max_inactive_connection_lifetime=300,
    )
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)

# ===== XP/УРОВЕНЬ =====
def calc_xp(total_earned: float):
    level, xp_max = 1, 1000.0
    xp = total_earned * 0.05
    while xp >= xp_max:
        xp -= xp_max
        level += 1
        xp_max = round(xp_max * 1.4, 2)
    return level, round(xp, 2), round(xp_max, 2)

async def update_balance_and_xp(conn, telegram_id, amount):
    row = await conn.fetchrow(
        "UPDATE users SET balance=balance+$1, "
        "total_earned=CASE WHEN $1>0 THEN total_earned+$1 ELSE total_earned END, "
        "total_taps=CASE WHEN $1>0 THEN total_taps ELSE total_taps END "
        "WHERE telegram_id=$2 RETURNING balance, total_earned",
        float(amount), telegram_id
    )
    if row and amount > 0:
        lvl, xp, xp_max = calc_xp(float(row['total_earned']))
        await conn.execute(
            "UPDATE users SET level=$1, xp=$2, xp_max=$3 WHERE telegram_id=$4",
            lvl, xp, xp_max, telegram_id
        )
    return float(row['balance']) if row else 0

# ===== ПОЛЬЗОВАТЕЛИ =====
async def get_user(tid):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1", tid)

async def get_or_create_user(tid, username, first_name=None, avatar_url=None, referred_by=None):
    u = await get_user(tid)
    if u: return u
    code = secrets.token_hex(4).upper()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users(telegram_id,username,first_name,avatar_url,referral_code,referred_by)"
            " VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(telegram_id) DO NOTHING",
            tid, username, first_name, avatar_url, code, referred_by
        )
    return await get_user(tid)

def calc_miner_mult(bombs, opened):
    safe = MINER_GRID - bombs
    if bombs <= 0 or opened <= 0: return 1.0
    prob = 1.0
    for i in range(opened):
        prob *= (safe - i) / (MINER_GRID - i)
    if prob <= 0: return 1.0
    return round((1 / prob) * 0.99, 4)

# ===== ЛОТЕРЕЯ =====
lottery_state = {'round_id': None, 'phase': 'betting', 'phase_end': 0,
                 'hash': '', 'seed': '', 'result': None}
tap_limits = {}

def rate_ok(tid):
    now = time.time()
    b = tap_limits.setdefault(tid, [])
    b[:] = [t for t in b if now - t < 1]
    if len(b) >= 12: return False  # 12 тапов/сек максимум
    b.append(now)
    return True

# Очищаем старые записи раз в минуту чтобы не копилось
_last_cleanup = 0
def maybe_cleanup_limits():
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < 60: return
    _last_cleanup = now
    stale = [k for k,v in tap_limits.items() if not v or now - max(v) > 5]
    for k in stale: tap_limits.pop(k, None)

async def lottery_loop():
    """
    Логика:
    - Раунд стартует, ждём LOTTERY_BET_SECONDS
    - Если ставки только на одной стороне — раунд продлевается ещё на LOTTERY_BET_SECONDS
    - Ставки НЕ отменяются и НЕ возвращаются — они переносятся в следующий цикл ожидания
    - Раунд завершается ТОЛЬКО когда есть ставки на обеих сторонах
    """
    while True:
        try:
            seed = secrets.token_hex(16)
            inner_hash = hashlib.sha256(seed.encode()).hexdigest()
            display_hash = hashlib.sha256(inner_hash.encode()).hexdigest()
            async with pool.acquire() as conn:
                rid = await conn.fetchval(
                    "INSERT INTO lottery_rounds(seed,hash,inner_hash,start_time,status)"
                    " VALUES($1,$2,$3,now(),'betting') RETURNING id", seed, display_hash, inner_hash
                )
            lottery_state.update({
                'round_id': rid, 'phase': 'betting',
                'phase_end': time.time() + LOTTERY_BET_SECONDS,
                'hash': display_hash,
                'inner_hash': inner_hash,
                'seed': seed,
                'result': None
            })

            # Ждём пока не появятся ставки на обеих сторонах
            while True:
                await asyncio.sleep(LOTTERY_BET_SECONDS)
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT telegram_id, side, SUM(amount) as amt FROM lottery_bets"
                        " WHERE round_id=$1 GROUP BY telegram_id, side", rid
                    )
                sides_with_bets = set(r['side'] for r in rows)
                if len(sides_with_bets) >= 2:
                    # Есть ставки на обеих сторонах — можно разыграть
                    break
                # Иначе — продлеваем ожидание, ставки остаются в этом же раунде
                lottery_state['phase_end'] = time.time() + LOTTERY_BET_SECONDS
                print(f"[lottery] Round {rid}: only one side has bets, extending round...")

            # Разыгрываем раунд
            lottery_state['phase'] = 'resolving'
            lottery_state['phase_end'] = time.time() + LOTTERY_REVEAL_SECONDS
            result = 'eagle' if int(inner_hash, 16) % 2 == 0 else 'tails'
            lottery_state['result'] = result

            # Пересчитываем суммы по игрокам (агрегируем все ставки одного игрока)
            async with pool.acquire() as conn:
                all_bets = await conn.fetch(
                    "SELECT telegram_id, side, SUM(amount) as amt FROM lottery_bets"
                    " WHERE round_id=$1 GROUP BY telegram_id, side", rid
                )
            bank = sum(float(r['amt']) for r in all_bets)
            winners = [r for r in all_bets if r['side'] == result]
            losers  = [r for r in all_bets if r['side'] != result]
            win_total = sum(float(r['amt']) for r in winners)

            async with pool.acquire() as conn:
                async with conn.transaction():
                    for r in winners:
                        share = (float(r['amt']) / win_total) * bank * LOTTERY_HOUSE_EDGE
                        await update_balance_and_xp(conn, r['telegram_id'], share)
                        await conn.execute(
                            "INSERT INTO game_history(telegram_id,game,result,amount)"
                            " VALUES($1,'lottery','win',$2)", r['telegram_id'], share
                        )
                    for r in losers:
                        await conn.execute(
                            "INSERT INTO game_history(telegram_id,game,result,amount)"
                            " VALUES($1,'lottery','lose',$2)", r['telegram_id'], -float(r['amt'])
                        )
                    await conn.execute(
                        "UPDATE lottery_rounds SET end_time=now(),winner_side=$1,total_bank=$2,status='finished' WHERE id=$3",
                        result, bank, rid
                    )
            await asyncio.sleep(LOTTERY_REVEAL_SECONDS)
        except Exception as e:
            print(f"lottery_loop error: {e}")
            await asyncio.sleep(5)

async def passive_income_loop():
    while True:
        await asyncio.sleep(5)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET balance=balance+per_sec*5,"
                    "total_earned=CASE WHEN per_sec>0 THEN total_earned+per_sec*5 ELSE total_earned END"
                    " WHERE per_sec>0"
                )
        except Exception as e:
            print(f"passive_income error: {e}")

async def keep_alive_loop():
    import aiohttp
    await asyncio.sleep(30)
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                resp = await s.get(WEBAPP_URL + '/ping', timeout=aiohttp.ClientTimeout(total=10))
                print(f"[keep-alive] ping → {resp.status}")
        except Exception as e:
            print(f"[keep-alive] error: {e}")
        await asyncio.sleep(300)  # каждые 5 минут

async def disable_boost(tid, orig_sec):
    await asyncio.sleep(300)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET per_sec=$1, boost_active=FALSE WHERE telegram_id=$2",
                orig_sec, tid
            )
    except Exception as e:
        print(f"disable_boost error: {e}")

# ===== АДМИН-ПАНЕЛЬ (только для владельца) =====
ADMIN_ID = 8511084538

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users")
        online = await conn.fetchval("SELECT COUNT(*) FROM users WHERE last_active > now() - interval '5 minutes'")
        bank = await conn.fetchval("SELECT SUM(balance) FROM users") or 0
    text = (
        f"👑 <b>TapiX Admin Panel</b>\n\n"
        f"📊 Игроков всего: <b>{total}</b>\n"
        f"🟢 Онлайн (5 мин): <b>{online}</b>\n"
        f"💰 Общий банк: <b>{float(bank):,.0f} ₿</b>\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "💰 <b>Начислить коины:</b>\n"
        "<code>/addcoins @username 50000</code>\n\n"
        "💸 <b>Снять коины:</b>\n"
        "<code>/removecoins @username 1000</code>\n\n"
        "🔍 <b>Инфо об игроке:</b>\n"
        "<code>/userinfo @username</code>\n\n"
        "📋 <b>Топ игроков:</b>\n"
        "<code>/players</code>\n\n"
        "🔄 <b>Сбросить игрока:</b>\n"
        "<code>/resetuser @username</code>\n\n"
        "💰 <b>Установить баланс:</b>\n"
        "<code>/setbalance @username 50000</code>\n\n"
        "🚫 <b>Бан игрока:</b>\n"
        "<code>/ban @username 1h</code> (m/h/d/y/forever)\n\n"
        "✅ <b>Разбан:</b>\n"
        "<code>/unban @username</code>\n\n"
        "🟢 <b>Кто онлайн прямо сейчас:</b>\n"
        "<code>/online</code>\n\n"
        "📢 <b>Рассылка всем (с кнопкой):</b>\n"
        "<code>/broadcast Текст</code>\n\n"
        "📣 <b>Красивый анонс с кнопкой играть:</b>\n"
        "<code>/announce Текст анонса</code>\n\n"
        "📊 <b>Статистика:</b>\n"
        "<code>/stats</code>\n\n"
        "🪙 <b>Статистика монет:</b>\n"
        "<code>/coinstat</code>"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("userinfo"))
async def cmd_userinfo(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("❌ Формат: /userinfo @username")
        return
    username = parts[1].lstrip('@')
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT first_name, username, balance, level, total_earned, total_taps, "
            "per_tap, per_sec, created_at, last_active, ban_until, promo_used FROM users WHERE LOWER(username)=LOWER($1)", username
        )
    if not row:
        await message.answer(f"❌ @{username} не найден")
        return
    banned = row['ban_until'] and row['ban_until'] > datetime.now(timezone.utc)
    ban_str = f"🚫 До {row['ban_until'].strftime('%d.%m.%Y %H:%M')}" if banned else "✅ Нет"
    last = row['last_active'].strftime('%d.%m %H:%M') if row['last_active'] else "—"
    reg = row['created_at'].strftime('%d.%m.%Y') if row['created_at'] else "—"
    await message.answer(
        f"👤 <b>{row['first_name'] or username}</b> (@{row['username']})\n\n"
        f"💰 Баланс: <b>{float(row['balance']):,.2f} ₿</b>\n"
        f"⭐ Уровень: <b>{row['level']}</b>\n"
        f"📈 Заработано: <b>{float(row['total_earned']):,.0f} ₿</b>\n"
        f"👆 Тапов: <b>{row['total_taps']:,}</b>\n"
        f"⚡ За тап: <b>{float(row['per_tap']):.5f}</b>\n"
        f"💎 В сек: <b>{float(row['per_sec']):.5f}</b>\n"
        f"📅 Регистрация: <b>{reg}</b>\n"
        f"🕐 Был онлайн: <b>{last}</b>\n"
        f"🎟 Промо: <b>{'Использован' if row['promo_used'] else 'Нет'}</b>\n"
        f"🚫 Бан: <b>{ban_str}</b>",
        parse_mode="HTML"
    )

@dp.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("❌ Формат: /ban @username 1h (m=мин, h=час, d=день, y=год, forever=навсегда)")
        return
    username = parts[1].lstrip('@')
    duration_str = parts[2].lower()
    now = datetime.now(timezone.utc)
    if duration_str == 'forever':
        ban_until = now + timedelta(days=36500)
        human = 'навсегда'
    else:
        try:
            num = int(duration_str[:-1])
            unit = duration_str[-1]
            if unit == 'm': delta = timedelta(minutes=num); human = f"{num} мин"
            elif unit == 'h': delta = timedelta(hours=num); human = f"{num} ч"
            elif unit == 'd': delta = timedelta(days=num); human = f"{num} дн"
            elif unit == 'y': delta = timedelta(days=num*365); human = f"{num} лет"
            else: raise ValueError
            ban_until = now + delta
        except:
            await message.answer("❌ Неверный формат времени. Примеры: 30m, 2h, 7d, 1y, forever")
            return
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT telegram_id, first_name FROM users WHERE LOWER(username)=LOWER($1)", username)
        if not row:
            await message.answer(f"❌ @{username} не найден")
            return
        await conn.execute("UPDATE users SET ban_until=$1 WHERE telegram_id=$2", ban_until, row['telegram_id'])
    await message.answer(f"🚫 <b>@{username}</b> заблокирован на <b>{human}</b>", parse_mode="HTML")
    try:
        await bot.send_message(row['telegram_id'], "🚫 Ваш аккаунт заблокирован администратором.")
    except: pass

@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("❌ Формат: /unban @username")
        return
    username = parts[1].lstrip('@')
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT telegram_id, first_name FROM users WHERE LOWER(username)=LOWER($1)", username)
        if not row:
            await message.answer(f"❌ @{username} не найден")
            return
        await conn.execute("UPDATE users SET ban_until=NULL WHERE telegram_id=$1", row['telegram_id'])
    await message.answer(f"✅ <b>@{username}</b> разблокирован", parse_mode="HTML")
    try:
        await bot.send_message(row['telegram_id'], "✅ Ваш аккаунт разблокирован. Добро пожаловать обратно!")
    except: pass

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text[len("/broadcast"):].strip()
    if not text:
        await message.answer("❌ Формат: /broadcast Текст сообщения")
        return
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT telegram_id FROM users WHERE ban_until IS NULL OR ban_until < now()")
    sent, failed = 0, 0
    for u in users:
        try:
            await bot.send_message(u['telegram_id'], text, parse_mode="HTML")
            sent += 1
        except: failed += 1
        await asyncio.sleep(0.05)
    await message.answer(f"✅ Рассылка завершена\n📤 Отправлено: {sent}\n❌ Ошибок: {failed}")

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users")
        online_5m = await conn.fetchval("SELECT COUNT(*) FROM users WHERE last_active > now()-interval '5 minutes'")
        online_1h = await conn.fetchval("SELECT COUNT(*) FROM users WHERE last_active > now()-interval '1 hour'")
        online_24h = await conn.fetchval("SELECT COUNT(*) FROM users WHERE last_active > now()-interval '24 hours'")
        bank = await conn.fetchval("SELECT COALESCE(SUM(balance),0) FROM users")
        earned = await conn.fetchval("SELECT COALESCE(SUM(total_earned),0) FROM users")
        taps = await conn.fetchval("SELECT COALESCE(SUM(total_taps),0) FROM users")
        bets = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM lottery_bets")
        top = await conn.fetch("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 3")
    top_str = "\n".join(f"  {i+1}. @{r['username'] or '?'} — {float(r['balance']):,.0f} ₿" for i,r in enumerate(top))
    await message.answer(
        f"📊 <b>Статистика TapiX</b>\n\n"
        f"👥 Игроков: <b>{total}</b>\n"
        f"🟢 Онлайн 5м: <b>{online_5m}</b>\n"
        f"🟡 Онлайн 1ч: <b>{online_1h}</b>\n"
        f"🔵 Онлайн 24ч: <b>{online_24h}</b>\n\n"
        f"💰 Банк (сумма балансов): <b>{float(bank):,.0f} ₿</b>\n"
        f"📈 Всего заработано: <b>{float(earned):,.0f} ₿</b>\n"
        f"👆 Всего тапов: <b>{int(taps):,}</b>\n"
        f"🎰 Ставок в лотерее: <b>{float(bets):,.0f} ₿</b>\n\n"
        f"🏆 Топ-3:\n{top_str}",
        parse_mode="HTML"
    )

@dp.message(Command("coinstat"))
async def cmd_coinstat(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with pool.acquire() as conn:
        # Проиграно в играх (лотерея + минёр)
        lost_1d  = await conn.fetchval("SELECT COALESCE(ABS(SUM(amount)),0) FROM game_history WHERE result IN ('lose','bomb') AND date > now()-interval '1 day'") or 0
        lost_7d  = await conn.fetchval("SELECT COALESCE(ABS(SUM(amount)),0) FROM game_history WHERE result IN ('lose','bomb') AND date > now()-interval '7 days'") or 0
        lost_30d = await conn.fetchval("SELECT COALESCE(ABS(SUM(amount)),0) FROM game_history WHERE result IN ('lose','bomb') AND date > now()-interval '30 days'") or 0
        # Сожжено в комиссиях (5% от суммы переводов)
        fee_1d   = await conn.fetchval("SELECT COALESCE(SUM(fee),0) FROM transfers WHERE timestamp > now()-interval '1 day'") or 0
        fee_7d   = await conn.fetchval("SELECT COALESCE(SUM(fee),0) FROM transfers WHERE timestamp > now()-interval '7 days'") or 0
        fee_30d  = await conn.fetchval("SELECT COALESCE(SUM(fee),0) FROM transfers WHERE timestamp > now()-interval '30 days'") or 0
        # Натапано (через тапы)
        tap_1d   = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM game_history WHERE result='win' AND game NOT IN ('lottery','miner') AND date > now()-interval '1 day'") or 0
        # Считаем натапано через total_earned приближённо — используем per_tap записи
        tap_earned_1d  = await conn.fetchval("""
            SELECT COALESCE(SUM(amount),0) FROM game_history 
            WHERE game='tap' AND date > now()-interval '1 day'
        """) or 0
        tap_earned_7d  = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM game_history WHERE game='tap' AND date > now()-interval '7 days'") or 0
        tap_earned_30d = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM game_history WHERE game='tap' AND date > now()-interval '30 days'") or 0
        # Выиграно в играх
        won_1d   = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM game_history WHERE result IN ('win','cashout') AND amount>0 AND date > now()-interval '1 day'") or 0
        won_7d   = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM game_history WHERE result IN ('win','cashout') AND amount>0 AND date > now()-interval '7 days'") or 0
        won_30d  = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM game_history WHERE result IN ('win','cashout') AND amount>0 AND date > now()-interval '30 days'") or 0
        # Общая сумма переводов
        tr_1d    = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM transfers WHERE timestamp > now()-interval '1 day'") or 0
        tr_7d    = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM transfers WHERE timestamp > now()-interval '7 days'") or 0
        tr_30d   = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM transfers WHERE timestamp > now()-interval '30 days'") or 0
        # Кол-во переводов
        tr_cnt_1d  = await conn.fetchval("SELECT COUNT(*) FROM transfers WHERE timestamp > now()-interval '1 day'") or 0
        tr_cnt_7d  = await conn.fetchval("SELECT COUNT(*) FROM transfers WHERE timestamp > now()-interval '7 days'") or 0
        tr_cnt_30d = await conn.fetchval("SELECT COUNT(*) FROM transfers WHERE timestamp > now()-interval '30 days'") or 0
    await message.answer(
        f"🪙 <b>Статистика монет TapiX</b>\n\n"
        f"<b>           1д        7д        30д</b>\n"
        f"💀 Проиграно:\n"
        f"  <code>{float(lost_1d):>10,.0f}  {float(lost_7d):>10,.0f}  {float(lost_30d):>10,.0f}</code>\n\n"
        f"🔥 Сожжено (комис.):\n"
        f"  <code>{float(fee_1d):>10,.0f}  {float(fee_7d):>10,.0f}  {float(fee_30d):>10,.0f}</code>\n\n"
        f"👆 Выиграно в играх:\n"
        f"  <code>{float(won_1d):>10,.0f}  {float(won_7d):>10,.0f}  {float(won_30d):>10,.0f}</code>\n\n"
        f"💸 Переводов (₿):\n"
        f"  <code>{float(tr_1d):>10,.0f}  {float(tr_7d):>10,.0f}  {float(tr_30d):>10,.0f}</code>\n"
        f"  (кол-во: {int(tr_cnt_1d)} / {int(tr_cnt_7d)} / {int(tr_cnt_30d)})\n\n"
        f"📌 Все суммы в ₿",
        parse_mode="HTML"
    )


@dp.message(Command("addcoins"))
async def cmd_addcoins(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("❌ Формат: /addcoins @username 50000")
        return
    username = parts[1].lstrip('@')
    try:
        amount = float(parts[2])
    except:
        await message.answer("❌ Сумма должна быть числом")
        return
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT telegram_id, balance, first_name FROM users WHERE LOWER(username)=LOWER($1)", username)
        if not row:
            await message.answer(f"❌ Игрок @{username} не найден в базе")
            return
        new_bal = await update_balance_and_xp(conn, row['telegram_id'], amount)
    await message.answer(
        f"✅ Начислено <b>{amount:,.0f} ₿</b> игроку <b>{row['first_name'] or username}</b> (@{username})\n"
        f"💰 Новый баланс: <b>{new_bal:,.2f} ₿</b>",
        parse_mode="HTML"
    )

@dp.message(Command("removecoins"))
async def cmd_removecoins(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("❌ Формат: /removecoins @username 1000")
        return
    username = parts[1].lstrip('@')
    try:
        amount = float(parts[2])
    except:
        await message.answer("❌ Сумма должна быть числом")
        return
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT telegram_id, balance, first_name FROM users WHERE LOWER(username)=LOWER($1)", username)
        if not row:
            await message.answer(f"❌ Игрок @{username} не найден в базе")
            return
        new_bal = max(0, float(row['balance']) - amount)
        await conn.execute("UPDATE users SET balance=$1 WHERE telegram_id=$2", new_bal, row['telegram_id'])
    await message.answer(
        f"✅ Снято <b>{amount:,.0f} ₿</b> у игрока <b>{row['first_name'] or username}</b> (@{username})\n"
        f"💰 Новый баланс: <b>{new_bal:,.2f} ₿</b>",
        parse_mode="HTML"
    )

@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("❌ Формат: /balance @username")
        return
    username = parts[1].lstrip('@')
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT first_name, balance, level, total_earned FROM users WHERE LOWER(username)=LOWER($1)", username
        )
    if not row:
        await message.answer(f"❌ Игрок @{username} не найден в базе")
        return
    await message.answer(
        f"👤 <b>{row['first_name'] or username}</b> (@{username})\n"
        f"💰 Баланс: <b>{float(row['balance']):,.2f} ₿</b>\n"
        f"⭐ Уровень: <b>{row['level']}</b>\n"
        f"📈 Всего заработано: <b>{float(row['total_earned']):,.2f} ₿</b>",
        parse_mode="HTML"
    )

@dp.message(Command("players"))
async def cmd_players(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT first_name, username, balance, level FROM users ORDER BY balance DESC LIMIT 20"
        )
    if not rows:
        await message.answer("База пуста")
        return
    text = "📋 <b>Топ-20 игроков по балансу:</b>\n\n"
    for i, r in enumerate(rows, 1):
        name = r['first_name'] or r['username'] or '???'
        uname = f"@{r['username']}" if r['username'] else ''
        text += f"{i}. <b>{name}</b> {uname} — {float(r['balance']):,.0f} ₿ · Ур.{r['level']}\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("resetuser"))
async def cmd_resetuser(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    parts = message.text.split()
    if len(parts) < 2: await message.answer("❌ /resetuser @username"); return
    username = parts[1].lstrip('@')
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT telegram_id FROM users WHERE LOWER(username)=LOWER($1)", username)
        if not row: await message.answer(f"❌ @{username} не найден"); return
        await conn.execute(
            "UPDATE users SET balance=0,level=1,xp=0,total_earned=0,total_taps=0,"
            "per_tap=0.0001,per_sec=0,"
            "shop_tap0=0,shop_tap1=0,shop_tap2=0,shop_tap3=0,shop_tap4=0,shop_tap5=0,"
            "shop_sec0=0,shop_sec1=0,shop_sec2=0,shop_sec3=0,shop_sec4=0,shop_sec5=0,"
            "promo_used=FALSE,boost_active=FALSE,dildo_activated=FALSE"
            " WHERE telegram_id=$1",
            row['telegram_id']
        )
    await message.answer(f"✅ @{username} — прогресс полностью сброшен (рефералы сохранены)")

CHANNEL_ID = "@TapiX1"

# Хранилище ожидающих постов (telegram_id -> данные поста)
_pending_posts = {}

@dp.message(Command("post"))
async def cmd_post(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    _pending_posts[message.from_user.id] = {'step': 'waiting_photo'}
    await message.answer(
        "📸 Отправь картинку с подписью (текст поста пиши в подписи к картинке)."
    )

@dp.message(F.photo & F.from_user.func(lambda u: u.id == ADMIN_ID))
async def handle_post_photo(message: types.Message):
    state = _pending_posts.get(message.from_user.id)
    if not state or state.get('step') != 'waiting_photo':
        return
    _pending_posts[message.from_user.id] = {
        'step': 'waiting_choice',
        'photo_id': message.photo[-1].file_id,
        'caption': message.caption or '',
    }
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, добавить кнопку Играть", callback_data="post_with_btn"),
            InlineKeyboardButton(text="❌ Нет, без кнопки", callback_data="post_no_btn"),
        ]
    ])
    await message.answer("Добавить кнопку «Играть» под постом?", reply_markup=kb)

@dp.callback_query(F.data.in_({"post_with_btn", "post_no_btn"}))
async def handle_post_choice(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer()
        return
    state = _pending_posts.get(cb.from_user.id)
    if not state or state.get('step') != 'waiting_choice':
        await cb.answer("Нет активного поста.")
        return
    photo_id = state['photo_id']
    caption = state['caption']
    _pending_posts.pop(cb.from_user.id, None)
    if cb.data == "post_with_btn":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Играть", web_app=WebAppInfo(url=WEBAPP_URL))]
        ])
        await bot.send_photo(CHANNEL_ID, photo=photo_id, caption=caption, reply_markup=kb)
    else:
        await bot.send_photo(CHANNEL_ID, photo=photo_id, caption=caption)
    await cb.message.edit_text("✅ Пост опубликован в канале!")
    await cb.answer()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    ref = None
    if len(message.text.split()) > 1:
        code = message.text.split()[1]
        async with pool.acquire() as conn:
            r = await conn.fetchrow("SELECT id,telegram_id FROM users WHERE referral_code=$1", code)
        if r and r['telegram_id'] != message.from_user.id:
            ref = r['id']
    existing = await get_user(message.from_user.id)
    is_new = existing is None
    await get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.first_name, None, ref)
    # Устанавливаем встроенную кнопку «Играть» для этого чата
    try:
        await bot.set_chat_menu_button(
            chat_id=message.from_user.id,
            menu_button=MenuButtonWebApp(text="Играть", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    except: pass
    if is_new and ref:
        async with pool.acquire() as conn:
            ref_row = await conn.fetchrow("SELECT telegram_id FROM users WHERE id=$1", ref)
        if ref_row:
            async with pool.acquire() as conn:
                await update_balance_and_xp(conn, ref_row['telegram_id'], 1000)
            try:
                await bot.send_message(ref_row['telegram_id'], "🎉 +1000 ₿ — твой друг присоединился!")
            except: pass
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Играть", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(text="👥 Рефералка", callback_data="ref"),
         InlineKeyboardButton(text="🏆 Топ", callback_data="top")]
    ])
    await message.answer("🐱 Добро пожаловать в TapiX!\nТапай, зарабатывай, побеждай!", reply_markup=kb)

@dp.callback_query(F.data == "ref")
async def cb_ref(cb: types.CallbackQuery):
    u = await get_user(cb.from_user.id)
    if u:
        link = f"https://t.me/taplx_bot?start={u['referral_code']}"
        await cb.message.answer(f"👥 Твоя реферальная ссылка:\n{link}\n\n💰 +1000 ₿ за каждого приглашённого друга!")
    await cb.answer()

@dp.callback_query(F.data == "top")
async def cb_top(cb: types.CallbackQuery):
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT username,balance FROM users ORDER BY balance DESC LIMIT 10")
    txt = "🏆 ТОП-10:\n\n"
    for i, r in enumerate(rows, 1):
        txt += f"{i}. @{r['username'] or 'Аноним'} — {r['balance']:.0f} ₿\n"
    await cb.message.answer(txt)
    await cb.answer()

# ===== API =====
def user_dict(u, place=0, ref_count=0):
    return {
        'success': True,
        'balance': float(u['balance']),
        'per_tap': float(u['per_tap']),
        'per_sec': float(u['per_sec']),
        'total_earned': float(u['total_earned']),
        'total_taps': int(u['total_taps']),
        'level': int(u['level']),
        'xp': float(u['xp']),
        'xp_max': float(u['xp_max']),
        'referral_code': u['referral_code'] or '',
        'referral_total': ref_count,
        'referral_earned': ref_count * 1000,
        'shop_tap': [u[f'shop_tap{i}'] for i in range(6)],
        'shop_sec': [u[f'shop_sec{i}'] for i in range(6)],
        'sound_on': bool(u['sound_on']),
        'theme': u['theme'] or 'dark',
        'dildo_activated': bool(u['dildo_activated']),
        'place': place,
        'avatar_url': u['avatar_url'],
        'first_name': u['first_name'],
        'username': u['username'],
    }

async def api_handler(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'success': False, 'error': 'Bad request'}, status=400)
    action = data.get('action')
    try:
        tid = int(data.get('telegram_id', 0))
        if tid == 0: raise ValueError
    except (TypeError, ValueError):
        return web.json_response({'success': False, 'error': 'Invalid telegram_id'}, status=400)

    # ===== GET USER =====
    if action == 'get_user':
        u = await get_or_create_user(tid, data.get('username'), data.get('first_name'), data.get('avatar_url'))
        # Проверка бана
        if u and u['ban_until'] and u['ban_until'] > datetime.now(timezone.utc):
            return web.json_response({'success': False, 'banned': True, 'ban_until': u['ban_until'].isoformat()})
        fields_to_update = {}
        if data.get('username'): fields_to_update['username'] = data['username']
        if data.get('first_name'): fields_to_update['first_name'] = data['first_name']
        if data.get('avatar_url'): fields_to_update['avatar_url'] = data['avatar_url']
        # Автоматически запрашиваем аватарку если нет
        if not u['avatar_url']:
            try:
                photos = await bot.get_user_profile_photos(tid, limit=1)
                if photos.total_count > 0:
                    f = await bot.get_file(photos.photos[0][0].file_id)
                    fields_to_update['avatar_url'] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}"
            except: pass
        if fields_to_update:
            sets = ', '.join(f"{k}=${i+2}" for i, k in enumerate(fields_to_update))
            vals = list(fields_to_update.values())
            async with pool.acquire() as conn:
                await conn.execute(f"UPDATE users SET {sets}, last_active=now() WHERE telegram_id=$1", tid, *vals)
            u = await get_user(tid)
        async with pool.acquire() as conn:
            place = await conn.fetchval(
                "SELECT COUNT(*)+1 FROM users WHERE balance>(SELECT balance FROM users WHERE telegram_id=$1)", tid
            ) or 1
            ref_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referred_by=$1", u['id']) or 0
        return web.json_response(user_dict(u, place, ref_count))

    # ===== TAP =====
    if action == 'tap':
        maybe_cleanup_limits()
        if not rate_ok(tid):
            return web.json_response({'success': False, 'error': 'Слишком часто'})
        async with pool.acquire() as conn:
            u = await get_user(tid)
            if not u: return web.json_response({'success': False, 'error': 'Not found'})
            earn = float(u['per_tap'])
            bal = await conn.fetchval(
                "UPDATE users SET balance=balance+$1, total_earned=total_earned+$1, total_taps=total_taps+1"
                " WHERE telegram_id=$2 RETURNING balance", earn, tid
            )
            # Обновляем XP/уровень каждые 10 тапов (не каждый чтобы не грузить БД)
            new_taps = int(u['total_taps']) + 1
            if new_taps % 10 == 0:
                te = float(u['total_earned']) + earn * 10
                lvl, xp, xp_max = calc_xp(te)
                await conn.execute("UPDATE users SET level=$1,xp=$2,xp_max=$3 WHERE telegram_id=$4", lvl, xp, xp_max, tid)
        return web.json_response({'success': True, 'new_balance': float(bal)})

    # ===== TRANSFER =====
    if action == 'lookup_user':
        uname = (data.get('username') or '').lstrip('@').strip()
        if not uname:
            return web.json_response({'success': False, 'error': 'Пусто'})
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT username, first_name, avatar_url, level FROM users WHERE LOWER(username)=LOWER($1)", uname
            )
        if not row:
            return web.json_response({'success': False, 'error': 'Игрок не найден'})
        return web.json_response({
            'success': True,
            'username': row['username'],
            'first_name': row['first_name'] or '',
            'avatar_url': row['avatar_url'] or '',
            'level': row['level'],
        })

    if action == 'transfer':
        to_uname = (data.get('to_username') or '').lstrip('@').strip()
        try:
            amount = float(data.get('amount', 0))
        except: return web.json_response({'success': False, 'error': 'Некорректная сумма'})
        if amount < 50: return web.json_response({'success': False, 'error': 'Минимум 50 ₿'})
        fee = round(amount * 0.05, 2)
        total = amount + fee
        async with pool.acquire() as conn:
            to_u = await conn.fetchrow("SELECT telegram_id FROM users WHERE LOWER(username)=LOWER($1)", to_uname)
            if not to_u: return web.json_response({'success': False, 'error': 'Игрок не найден'})
            if to_u['telegram_id'] == tid: return web.json_response({'success': False, 'error': 'Нельзя себе'})
            async with conn.transaction():
                bal = await conn.fetchval("SELECT balance FROM users WHERE telegram_id=$1 FOR UPDATE", tid)
                if bal is None or bal < total:
                    return web.json_response({'success': False, 'error': 'Недостаточно средств'})
                new_bal = await conn.fetchval(
                    "UPDATE users SET balance=balance-$1 WHERE telegram_id=$2 RETURNING balance", total, tid
                )
                await conn.execute("UPDATE users SET balance=balance+$1 WHERE telegram_id=$2", amount, to_u['telegram_id'])
                await conn.execute(
                    "INSERT INTO transfers(from_telegram_id,to_telegram_id,amount,fee,total,message)"
                    " VALUES($1,$2,$3,$4,$5,$6)",
                    tid, to_u['telegram_id'], amount, fee, total, data.get('message')
                )
        return web.json_response({'success': True, 'new_balance': float(new_bal)})

    # ===== GET TRANSFERS =====
    if action == 'get_transfers':
        async with pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT t.amount,t.fee,t.total,t.message,t.timestamp,
                       t.from_telegram_id,t.to_telegram_id,
                       uf.username as fu, uf.avatar_url as fa,
                       ut.username as tu, ut.avatar_url as ta
                FROM transfers t
                LEFT JOIN users uf ON uf.telegram_id=t.from_telegram_id
                LEFT JOIN users ut ON ut.telegram_id=t.to_telegram_id
                WHERE t.from_telegram_id=$1 OR t.to_telegram_id=$1
                ORDER BY t.timestamp DESC LIMIT 50
            ''', tid)
        result = []
        for r in rows:
            out = r['from_telegram_id'] == tid
            result.append({
                'outgoing': out,
                'amount': float(r['amount']),
                'total': float(r['total']),
                'message': r['message'] or '',
                'timestamp': r['timestamp'].isoformat(),
                'other_username': r['tu'] if out else r['fu'],
                'other_avatar': r['ta'] if out else r['fa'],
            })
        return web.json_response({'success': True, 'transfers': result})

    # ===== TOP =====
    if action == 'get_top':
        now_ts = time.time()
        if _top_cache['data'] and now_ts - _top_cache['ts'] < 30:
            return web.json_response(_top_cache['data'])
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT telegram_id,username,first_name,avatar_url,balance,per_tap,per_sec,total_taps,level"
                " FROM users ORDER BY balance DESC LIMIT 50"
            )
        result = {'success': True, 'top': [{
            'telegram_id': r['telegram_id'],
            'username': r['username'],
            'first_name': r['first_name'],
            'avatar': r['avatar_url'],
            'balance': float(r['balance']),
            'per_sec': float(r['per_sec']),
            'total_taps': int(r['total_taps']),
            'level': int(r['level']),
        } for r in rows]}
        _top_cache['data'] = result
        _top_cache['ts'] = now_ts
        return web.json_response(result)

    # ===== SHOP =====
    if action == 'buy_shop':
        type_ = data.get('type')
        idx = data.get('idx')
        if type_ not in ('tap','sec') or not isinstance(idx,int) or not 0<=idx<=5:
            return web.json_response({'success': False, 'error': 'Некорректный товар'})
        u = await get_user(tid)
        if not u: return web.json_response({'success': False, 'error': 'Not found'})
        cfg = SHOP_TAP if type_=='tap' else SHOP_SEC
        col = f'shop_{type_}{idx}'
        lvl = u[col]
        price = int(cfg[idx]['price'] * (cfg[idx]['mult'] ** lvl))
        if u['balance'] < price:
            return web.json_response({'success': False, 'error': f'Нужно {price} ₿'})
        bonus = cfg[idx]['bonus']  # ФИКСИРОВАННЫЙ бонус, не растёт
        field = 'per_tap' if type_=='tap' else 'per_sec'
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE users SET balance=balance-$1,{col}={col}+1,{field}={field}+$2"
                f" WHERE telegram_id=$3 RETURNING balance,{field}",
                float(price), bonus, tid
            )
        return web.json_response({'success': True, 'new_balance': float(row['balance']), field: float(row[field])})

    # ===== BOOST =====
    if action == 'buy_boost':
        u = await get_user(tid)
        if not u: return web.json_response({'success': False, 'error': 'Not found'})
        if u['boost_active']: return web.json_response({'success': False, 'error': 'Уже активен'})
        now = datetime.now(timezone.utc)
        if u['boost_cooldown'] and u['boost_cooldown'] > now:
            return web.json_response({'success': False, 'error': 'На перезарядке'})
        if u['balance'] < 500: return web.json_response({'success': False, 'error': 'Нужно 500 ₿'})
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE users SET balance=balance-500,per_sec=per_sec*2,boost_active=TRUE,boost_cooldown=$1"
                " WHERE telegram_id=$2 RETURNING balance,per_sec",
                now + timedelta(hours=1), tid
            )
        asyncio.create_task(disable_boost(tid, float(row['per_sec'])/2))
        return web.json_response({'success': True, 'new_balance': float(row['balance']), 'per_sec': float(row['per_sec'])})

    # ===== PROMO =====
    if action == 'activate_promo':
        code = (data.get('code') or '').upper().strip()
        u = await get_user(tid)
        if not u: return web.json_response({'success': False, 'error': 'Not found'})
        if code == 'WELCOME':
            if u['promo_used']:
                return web.json_response({'success': False, 'error': 'Промокод уже использован'})
            async with pool.acquire() as conn:
                new_bal = await update_balance_and_xp(conn, tid, 1000)
                await conn.execute("UPDATE users SET promo_used=TRUE WHERE telegram_id=$1", tid)
            return web.json_response({'success': True, 'new_balance': new_bal, 'bonus': 1000})
        if code == 'DILDO':
            if u['dildo_activated']:
                return web.json_response({'success': False, 'error': 'Уже активирован'})
            async with pool.acquire() as conn:
                await conn.execute("UPDATE users SET dildo_activated=TRUE WHERE telegram_id=$1", tid)
            return web.json_response({'success': True, 'message': 'Дилдокликер активирован!'})
        return web.json_response({'success': False, 'error': 'Неверный промокод'})

    # ===== SET DILDO =====
    if action == 'set_dildo':
        activated = bool(data.get('activated', False))
        async with pool.acquire() as conn:
            await conn.execute("UPDATE users SET dildo_activated=$1 WHERE telegram_id=$2", activated, tid)
        return web.json_response({'success': True})

    # ===== THEME / SOUND =====
    if action == 'set_theme':
        async with pool.acquire() as conn:
            await conn.execute("UPDATE users SET theme=$1 WHERE telegram_id=$2", data.get('theme','dark'), tid)
        return web.json_response({'success': True})
    if action == 'set_sound':
        async with pool.acquire() as conn:
            await conn.execute("UPDATE users SET sound_on=$1 WHERE telegram_id=$2", bool(data.get('sound_on',True)), tid)
        return web.json_response({'success': True})

    # ===== LOTTERY =====
    if action == 'get_lottery_state':
        if lottery_state['round_id'] is None:
            return web.json_response({'success': False, 'error': 'Лотерея ещё не запущена'})
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT lb.telegram_id,lb.side,SUM(lb.amount) amt,u.avatar_url"
                " FROM lottery_bets lb JOIN users u ON u.telegram_id=lb.telegram_id"
                " WHERE lb.round_id=$1 GROUP BY lb.telegram_id,lb.side,u.avatar_url ORDER BY amt DESC",
                lottery_state['round_id']
            )
        eagle = [{'telegram_id':r['telegram_id'],'avatar':r['avatar_url'],'amount':float(r['amt'])} for r in rows if r['side']=='eagle']
        tails = [{'telegram_id':r['telegram_id'],'avatar':r['avatar_url'],'amount':float(r['amt'])} for r in rows if r['side']=='tails']
        is_resolving = lottery_state['phase'] == 'resolving'
        return web.json_response({
            'success': True, 'round_id': lottery_state['round_id'],
            'phase': lottery_state['phase'],
            'time_left': max(0, lottery_state['phase_end']-time.time()),
            # Во время ставок — показываем display_hash (нельзя вычислить результат)
            # После завершения — раскрываем inner_hash + seed для верификации
            'hash': lottery_state['hash'],  # display_hash — всегда
            'inner_hash': lottery_state.get('inner_hash') if is_resolving else None,
            'seed': lottery_state['seed'] if is_resolving else None,
            'result': lottery_state['result'], 'eagle': eagle, 'tails': tails,
        })

    if action == 'lottery_bet':
        if lottery_state['phase'] != 'betting':
            return web.json_response({'success': False, 'error': 'Ставки закрыты'})
        side = data.get('side')
        if side not in ('eagle','tails'):
            return web.json_response({'success': False, 'error': 'Укажи сторону'})
        try: amount = float(data.get('amount', 0))
        except: return web.json_response({'success': False, 'error': 'Некорректная ставка'})
        if not LOTTERY_MIN_BET <= amount <= LOTTERY_MAX_BET:
            return web.json_response({'success': False, 'error': f'Ставка {LOTTERY_MIN_BET}–{LOTTERY_MAX_BET}'})
        async with pool.acquire() as conn:
            async with conn.transaction():
                bal = await conn.fetchval("SELECT balance FROM users WHERE telegram_id=$1 FOR UPDATE", tid)
                if bal is None or bal < amount:
                    return web.json_response({'success': False, 'error': 'Недостаточно средств'})
                new_bal = await conn.fetchval(
                    "UPDATE users SET balance=balance-$1 WHERE telegram_id=$2 RETURNING balance", amount, tid
                )
                await conn.execute(
                    "INSERT INTO lottery_bets(round_id,telegram_id,side,amount) VALUES($1,$2,$3,$4)",
                    lottery_state['round_id'], tid, side, amount
                )
        return web.json_response({'success': True, 'new_balance': float(new_bal)})

    # ===== МІНЁР (сессии в БД) =====
    if action == 'start_miner':
        try:
            bet = float(data.get('bet', 0))
            bombs = int(data.get('bombs', 3))
        except: return web.json_response({'success': False, 'error': 'Некорректные параметры'})
        if not MINER_MIN_BET <= bet <= MINER_MAX_BET:
            return web.json_response({'success': False, 'error': f'Ставка {MINER_MIN_BET}–{MINER_MAX_BET}'})
        if not 1 <= bombs <= 5:
            return web.json_response({'success': False, 'error': 'Бомб от 1 до 5'})
        async with pool.acquire() as conn:
            # Проверяем активную сессию
            existing = await conn.fetchrow("SELECT bet FROM miner_sessions WHERE telegram_id=$1", tid)
            if existing:
                return web.json_response({'success': False, 'error': 'Игра уже идёт, забери выигрыш или дождись проигрыша'})
            async with conn.transaction():
                bal = await conn.fetchval("SELECT balance FROM users WHERE telegram_id=$1 FOR UPDATE", tid)
                if bal is None or bal < bet:
                    return web.json_response({'success': False, 'error': 'Недостаточно средств'})
                new_bal = await conn.fetchval(
                    "UPDATE users SET balance=balance-$1 WHERE telegram_id=$2 RETURNING balance", bet, tid
                )
                pos = set()
                while len(pos) < bombs:
                    pos.add(secrets.randbelow(MINER_GRID))
                await conn.execute(
                    "INSERT INTO miner_sessions(telegram_id,bet,bombs,bomb_positions,opened)"
                    " VALUES($1,$2,$3,$4,0) ON CONFLICT(telegram_id) DO UPDATE"
                    " SET bet=$2,bombs=$3,bomb_positions=$4,opened=0,created_at=now()",
                    tid, bet, bombs, json.dumps(list(pos))
                )
        return web.json_response({'success': True, 'new_balance': float(new_bal), 'total_gems': MINER_GRID-bombs})

    if action == 'get_miner_session':
        async with pool.acquire() as conn:
            s = await conn.fetchrow("SELECT * FROM miner_sessions WHERE telegram_id=$1", tid)
        if not s:
            return web.json_response({'success': True, 'active': False})
        return web.json_response({
            'success': True, 'active': True,
            'bet': float(s['bet']), 'bombs': s['bombs'],
            'opened': s['opened'],
            'total_gems': MINER_GRID - s['bombs'],
            'coef': calc_miner_mult(s['bombs'], s['opened']),
        })

    if action == 'reveal_miner_cell':
        try: index = int(data.get('index'))
        except: return web.json_response({'success': False, 'error': 'Некорректная ячейка'})
        if not 0 <= index < MINER_GRID:
            return web.json_response({'success': False, 'error': 'Ячейка вне поля'})
        async with pool.acquire() as conn:
            async with conn.transaction():
                s = await conn.fetchrow("SELECT * FROM miner_sessions WHERE telegram_id=$1 FOR UPDATE", tid)
                if not s:
                    return web.json_response({'success': False, 'error': 'Нет активной игры — начни новую'})
                bomb_pos = set(json.loads(s['bomb_positions']))
                already_opened = json.loads(s.get('opened_cells') or '[]') if 'opened_cells' in s else []
                # fallback if no opened_cells column: use opened count only
                if index in bomb_pos:
                    await conn.execute("DELETE FROM miner_sessions WHERE telegram_id=$1", tid)
                    await conn.execute(
                        "INSERT INTO game_history(telegram_id,game,result,amount) VALUES($1,'miner','lose',$2)",
                        tid, -float(s['bet'])
                    )
                    other_bombs = list(bomb_pos - {index})
                    return web.json_response({'success': True, 'result': 'bomb', 'bomb_positions': other_bombs})
                opened = s['opened'] + 1
                coef = calc_miner_mult(s['bombs'], opened)
                total_gems = MINER_GRID - s['bombs']
                if opened >= total_gems:
                    win = float(s['bet']) * coef
                    new_bal = await update_balance_and_xp(conn, tid, win)
                    await conn.execute("DELETE FROM miner_sessions WHERE telegram_id=$1", tid)
                    await conn.execute(
                        "INSERT INTO game_history(telegram_id,game,result,amount) VALUES($1,'miner','win',$2)",
                        tid, win
                    )
                    return web.json_response({'success': True, 'result': 'gem', 'coef': coef,
                                              'complete': True, 'win_amount': win, 'new_balance': new_bal})
                await conn.execute("UPDATE miner_sessions SET opened=$1 WHERE telegram_id=$2", opened, tid)
        return web.json_response({'success': True, 'result': 'gem', 'coef': coef, 'complete': False})

    if action == 'miner_cashout':
        async with pool.acquire() as conn:
            s = await conn.fetchrow("SELECT * FROM miner_sessions WHERE telegram_id=$1", tid)
        if not s:
            return web.json_response({'success': False, 'error': 'Нет активной игры'})
        if s['opened'] == 0:
            return web.json_response({'success': False, 'error': 'Открой хотя бы одну клетку'})
        coef = calc_miner_mult(s['bombs'], s['opened'])
        win = float(s['bet']) * coef
        bomb_pos = list(json.loads(s['bomb_positions']))
        async with pool.acquire() as conn:
            new_bal = await update_balance_and_xp(conn, tid, win)
            await conn.execute("DELETE FROM miner_sessions WHERE telegram_id=$1", tid)
            await conn.execute(
                "INSERT INTO game_history(telegram_id,game,result,amount) VALUES($1,'miner','cashout',$2)",
                tid, win
            )
        return web.json_response({'success': True, 'win_amount': win, 'new_balance': new_bal, 'bomb_positions': bomb_pos})

    # ===== GAME HISTORY =====
    if action == 'get_game_history':
        game = data.get('game', '')
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT game,result,amount,date FROM game_history"
                " WHERE telegram_id=$1 AND game=$2 ORDER BY date DESC LIMIT 50",
                tid, game
            )
        return web.json_response({'success': True, 'history': [
            {'game': r['game'], 'result': r['result'],
             'amount': float(r['amount']), 'date': r['date'].isoformat()} for r in rows
        ]})

    # ===== SYNC PROGRESS =====
    if action == 'sync_progress':
        try:
            te = float(data.get('total_earned', 0))
            tt = int(data.get('total_taps', 0))
        except: return web.json_response({'success': False})
        lvl, xp, xp_max = calc_xp(te)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET level=$1,xp=$2,xp_max=$3,"
                "total_taps=GREATEST(total_taps,$4) WHERE telegram_id=$5",
                lvl, xp, xp_max, tt, tid
            )
        return web.json_response({'success': True, 'level': lvl, 'xp': xp, 'xp_max': xp_max})

    # ===== LADDER (ЛЕСЕНКА) =====
    # Коэффициенты зависят от числа бомб: чем больше бомб — тем выше множители
    # Формула: на каждой ступени multiplier[bombs][step]
    # В каждой строке 3 клетки, bombs бомб из 3
    LADDER_CELLS = 3  # 3 клетки в строке
    LADDER_STEPS = 10
    # Коэффициенты для каждого числа бомб (1–2 из 3 клеток)
    # Вероятность выжить на 1 ступени: (cells-bombs)/cells
    # p(1 bomb) = 2/3, p(2 bombs) = 1/3
    # Множители растут как 1/p с house edge 0.97 (3% казино)
    LADDER_MULTS = {
        1: [1.45, 2.10, 3.05, 4.40, 6.35, 9.20, 13.30, 19.25, 27.85, 40.30],  # 2/3 шанс, ~1.45x за шаг
        2: [2.90, 8.40, 24.30, 70.30, 203.30, 587.80, 1699.90, 4914.00, 14207.00, 41079.00],  # 1/3 шанс, ~2.9x за шаг
    }

    def calc_ladder_mult(bombs_count, step):
        """Вернуть множитель для указанного числа бомб и номера ступени (0-based)."""
        bombs_count = max(1, min(2, bombs_count))
        mults = LADDER_MULTS[bombs_count]
        return mults[min(step, len(mults)-1)]

    if action == 'ladder_step':
        try:
            bet   = float(data.get('bet', 0))
            step  = int(data.get('step', 0))
            cell  = int(data.get('cell', 0))
            bombs = int(data.get('bombs', 1))
        except: return web.json_response({'success': False, 'error': 'Некорректные параметры'})
        if not 1 <= bombs <= 2: return web.json_response({'success': False, 'error': 'Бомб 1 или 2'})
        if not 0 <= cell < LADDER_CELLS: return web.json_response({'success': False, 'error': 'Некорректная клетка'})
        # Генерируем позиции бомб серверно
        bomb_positions = set()
        while len(bomb_positions) < bombs:
            bomb_positions.add(secrets.randbelow(LADDER_CELLS))
        bomb_list = list(bomb_positions)
        is_bomb = cell in bomb_positions
        next_step = step + 1
        if is_bomb:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO game_history(telegram_id,game,result,amount) VALUES($1,'ladder','lose',$2)",
                    tid, -bet
                )
            return web.json_response({'success': True, 'result': 'bomb', 'bombs': bomb_list})
        else:
            complete = next_step >= LADDER_STEPS
            if complete:
                coef = calc_ladder_mult(bombs, LADDER_STEPS - 1)
                win = bet * coef
                async with pool.acquire() as conn:
                    new_bal = await update_balance_and_xp(conn, tid, win)
                    await conn.execute(
                        "INSERT INTO game_history(telegram_id,game,result,amount) VALUES($1,'ladder','win',$2)",
                        tid, win
                    )
                return web.json_response({'success': True, 'result': 'safe', 'bombs': bomb_list,
                                          'complete': True, 'win_amount': win, 'new_balance': new_bal})
            return web.json_response({'success': True, 'result': 'safe', 'bombs': bomb_list, 'complete': False})

    if action == 'ladder_cashout':
        try:
            bet   = float(data.get('bet', 0))
            step  = int(data.get('step', 0))
            bombs = int(data.get('bombs', 1))
        except: return web.json_response({'success': False, 'error': 'Некорректные параметры'})
        if step <= 0: return web.json_response({'success': False, 'error': 'Шаг должен быть > 0'})
        bombs = max(1, min(2, bombs))
        LADDER_MULTS_LOCAL = {
            1: [1.45, 2.10, 3.05, 4.40, 6.35, 9.20, 13.30, 19.25, 27.85, 40.30],
            2: [2.90, 8.40, 24.30, 70.30, 203.30, 587.80, 1699.90, 4914.00, 14207.00, 41079.00],
        }
        mults = LADDER_MULTS_LOCAL[bombs]
        coef = mults[min(step - 1, len(mults) - 1)]
        win = bet * coef
        async with pool.acquire() as conn:
            new_bal = await update_balance_and_xp(conn, tid, win)
            await conn.execute(
                "INSERT INTO game_history(telegram_id,game,result,amount) VALUES($1,'ladder','cashout',$2)",
                tid, win
            )
        return web.json_response({'success': True, 'win_amount': win, 'new_balance': new_bal})

    return web.json_response({'success': False, 'error': 'Unknown action'})

async def reset_handler(request):
    """Сброс прогресса — доступен только с секретным ключом"""
    secret = request.query.get('key', '')
    if secret != os.getenv('RESET_KEY', 'tapix_reset_2024'):
        return web.Response(text='Forbidden', status=403)
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users SET balance=0, total_earned=0, total_taps=0,
            level=1, xp=0, xp_max=1000, per_tap=0.0001, per_sec=0,
            shop_tap0=0,shop_tap1=0,shop_tap2=0,shop_tap3=0,shop_tap4=0,
            shop_sec0=0,shop_sec1=0,shop_sec2=0,shop_sec3=0,shop_sec4=0,
            promo_used=FALSE, boost_active=FALSE, dildo_activated=FALSE
        """)
        await conn.execute("DELETE FROM lottery_bets")
        await conn.execute("DELETE FROM lottery_rounds")
        await conn.execute("DELETE FROM game_history")
        await conn.execute("DELETE FROM transfers")
        await conn.execute("DELETE FROM miner_sessions")
    return web.Response(text='✅ Прогресс всех игроков сброшен!', content_type='text/plain')


async def index_handler(request):
    try:
        with open('index.html', encoding='utf-8') as f:
            return web.Response(text=f.read(), content_type='text/html')
    except FileNotFoundError:
        return web.Response(text='404', status=404)

async def webhook_handler(request):
    data = await request.json()
    await dp.feed_update(bot, Update(**data))
    return web.Response(status=200)

async def main():
    await init_db()
    asyncio.create_task(passive_income_loop())
    asyncio.create_task(lottery_loop())
    asyncio.create_task(keep_alive_loop())
    # Встроенная кнопка «Играть» в меню каждого чата с ботом
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Играть", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    except Exception as e:
        print(f"[menu button] {e}")
    app = web.Application()
    app.router.add_post('/webhook', webhook_handler)
    app.router.add_post('/api', api_handler)
    app.router.add_get('/reset', reset_handler)
    app.router.add_get('/ping', lambda r: web.json_response({'ok': True}))
    app.router.add_get('/', index_handler)
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', port).start()
    print(f"✅ Запущен на порту {port}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
