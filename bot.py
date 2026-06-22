import asyncio
import hashlib
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, Update
from aiogram.filters import Command
from aiohttp import web

# ===== КОНФИГ =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана. Установи её в Render → Environment.")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Переменная окружения DATABASE_URL не задана. Подключи PostgreSQL в Render → Environment.")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

WEBAPP_URL = os.getenv("WEBAPP_URL", "https://CHANGE-ME.onrender.com")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
pool: asyncpg.Pool = None

# ===== НАСТРОЙКИ ИГР =====
LOTTERY_BET_SECONDS = 30      # сколько идёт приём ставок
LOTTERY_REVEAL_SECONDS = 6    # сколько показываем результат перед новым раундом
LOTTERY_MIN_BET = 100
LOTTERY_MAX_BET = 100000
LOTTERY_HOUSE_EDGE = 0.95     # победители делят 95% банка

MINER_GRID_SIZE = 25          # сетка 5x5
MINER_MIN_BET = 100
MINER_MAX_BET = 100000

TAP_LIMIT_PER_SEC = 5

SHOP_TAP_CFG = [
    {'baseBonus': 0.0005, 'basePrice': 30, 'priceMult': 3.0},
    {'baseBonus': 0.0010, 'basePrice': 90, 'priceMult': 3.0},
    {'baseBonus': 0.0020, 'basePrice': 270, 'priceMult': 3.0},
    {'baseBonus': 0.0040, 'basePrice': 810, 'priceMult': 3.0},
    {'baseBonus': 0.0080, 'basePrice': 2430, 'priceMult': 3.0},
]
SHOP_SEC_CFG = [
    {'baseBonus': 0.00025, 'basePrice': 36, 'priceMult': 3.0},
    {'baseBonus': 0.00050, 'basePrice': 108, 'priceMult': 3.0},
    {'baseBonus': 0.00100, 'basePrice': 324, 'priceMult': 3.0},
    {'baseBonus': 0.00200, 'basePrice': 972, 'priceMult': 3.0},
    {'baseBonus': 0.00400, 'basePrice': 2916, 'priceMult': 3.0},
]

# ===== СХЕМА БД =====
SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    first_name TEXT,
    avatar_url TEXT,
    balance DOUBLE PRECISION DEFAULT 0,
    per_tap DOUBLE PRECISION DEFAULT 0.001,
    per_sec DOUBLE PRECISION DEFAULT 0.00005,
    total_earned DOUBLE PRECISION DEFAULT 0,
    total_taps BIGINT DEFAULT 0,
    level INTEGER DEFAULT 1,
    xp DOUBLE PRECISION DEFAULT 0,
    xp_max DOUBLE PRECISION DEFAULT 800,
    referral_code TEXT UNIQUE,
    referred_by INTEGER,
    created_at TIMESTAMPTZ DEFAULT now(),
    last_active TIMESTAMPTZ,
    sound_on BOOLEAN DEFAULT TRUE,
    theme TEXT DEFAULT 'dark',
    shop_tap0 INTEGER DEFAULT 0, shop_tap1 INTEGER DEFAULT 0, shop_tap2 INTEGER DEFAULT 0,
    shop_tap3 INTEGER DEFAULT 0, shop_tap4 INTEGER DEFAULT 0,
    shop_sec0 INTEGER DEFAULT 0, shop_sec1 INTEGER DEFAULT 0, shop_sec2 INTEGER DEFAULT 0,
    shop_sec3 INTEGER DEFAULT 0, shop_sec4 INTEGER DEFAULT 0,
    boost_active BOOLEAN DEFAULT FALSE,
    boost_cooldown TIMESTAMPTZ,
    promo_used BOOLEAN DEFAULT FALSE,
    dildo_activated BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS transfers (
    id SERIAL PRIMARY KEY,
    from_telegram_id BIGINT,
    to_telegram_id BIGINT,
    amount DOUBLE PRECISION,
    fee DOUBLE PRECISION,
    total DOUBLE PRECISION,
    message TEXT,
    timestamp TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lottery_rounds (
    id SERIAL PRIMARY KEY,
    seed TEXT,
    hash TEXT,
    start_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ,
    winner_side TEXT,
    total_bank DOUBLE PRECISION DEFAULT 0,
    status TEXT DEFAULT 'betting'
);

CREATE TABLE IF NOT EXISTS lottery_bets (
    id SERIAL PRIMARY KEY,
    round_id INTEGER REFERENCES lottery_rounds(id),
    telegram_id BIGINT,
    side TEXT,
    amount DOUBLE PRECISION,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS game_history (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT,
    game TEXT,
    result TEXT,
    amount DOUBLE PRECISION,
    date TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lottery_bets_round ON lottery_bets(round_id);
CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance DESC);
'''


async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)


# ===== ПОЛЬЗОВАТЕЛИ =====
async def get_user(telegram_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)


async def get_or_create_user(telegram_id, username, first_name=None, avatar_url=None, referred_by=None):
    user = await get_user(telegram_id)
    if user:
        return user
    code = secrets.token_hex(4).upper()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO users (telegram_id, username, first_name, avatar_url, referral_code, referred_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (telegram_id) DO NOTHING
        ''', telegram_id, username, first_name, avatar_url, code, referred_by)
    return await get_user(telegram_id)


async def update_balance(telegram_id, amount):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "UPDATE users SET balance = balance + $1 WHERE telegram_id = $2 RETURNING balance",
            amount, telegram_id
        )


def calc_miner_multiplier(bombs, opened):
    total_tiles = MINER_GRID_SIZE
    safe_tiles = total_tiles - bombs
    if bombs <= 0 or opened < 0 or opened > safe_tiles:
        return 1.0
    prob = 1.0
    for i in range(opened):
        prob *= (safe_tiles - i) / (total_tiles - i)
    if prob <= 0:
        return 1.0
    multiplier = (1 / prob) * (1 - 0.03)
    if multiplier < 1.0:
        multiplier = 1.0
    return min(multiplier, 100)


# in-memory сессии активной игры в "Минёр" (один сервер-инстанс, без масштабирования)
miner_sessions = {}
tap_limits = {}


def check_rate_limit(telegram_id):
    now = time.time()
    bucket = tap_limits.setdefault(telegram_id, [])
    bucket[:] = [t for t in bucket if now - t < 1]
    if len(bucket) >= TAP_LIMIT_PER_SEC:
        return False
    bucket.append(now)
    return True


# ===== ЛОТЕРЕЯ (полностью серверная, без ботов) =====
lottery_state = {
    'round_id': None,
    'phase': 'betting',   # betting | resolving
    'phase_end': 0,
    'hash': '',
    'seed': '',
    'result': None,
}


async def lottery_loop():
    while True:
        try:
            seed = secrets.token_hex(16)
            round_hash = hashlib.sha256(seed.encode()).hexdigest()
            start = datetime.now(timezone.utc)
            async with pool.acquire() as conn:
                round_id = await conn.fetchval(
                    "INSERT INTO lottery_rounds (seed, hash, start_time, status) VALUES ($1, $2, $3, 'betting') RETURNING id",
                    seed, round_hash, start
                )
            lottery_state.update({
                'round_id': round_id, 'phase': 'betting',
                'phase_end': time.time() + LOTTERY_BET_SECONDS,
                'hash': round_hash, 'seed': seed, 'result': None,
            })

            await asyncio.sleep(LOTTERY_BET_SECONDS)

            lottery_state['phase'] = 'resolving'
            lottery_state['phase_end'] = time.time() + LOTTERY_REVEAL_SECONDS

            # результат детерминирован от seed — честно и проверяемо
            result = 'eagle' if int(round_hash, 16) % 2 == 0 else 'tails'
            lottery_state['result'] = result

            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT telegram_id, side, SUM(amount) AS amount FROM lottery_bets WHERE round_id = $1 GROUP BY telegram_id, side",
                    round_id
                )
                total_bank = sum(r['amount'] for r in rows)
                winners = [r for r in rows if r['side'] == result]
                winning_total = sum(r['amount'] for r in winners)
                if winners and winning_total > 0:
                    async with conn.transaction():
                        for r in winners:
                            share = (r['amount'] / winning_total) * total_bank * LOTTERY_HOUSE_EDGE
                            await conn.execute(
                                "UPDATE users SET balance = balance + $1, total_earned = total_earned + $1 WHERE telegram_id = $2",
                                share, r['telegram_id']
                            )
                            await conn.execute(
                                "INSERT INTO game_history (telegram_id, game, result, amount) VALUES ($1, 'lottery', 'win', $2)",
                                r['telegram_id'], share
                            )
                losers = [r for r in rows if r['side'] != result]
                for r in losers:
                    await conn.execute(
                        "INSERT INTO game_history (telegram_id, game, result, amount) VALUES ($1, 'lottery', 'lose', $2)",
                        r['telegram_id'], -r['amount']
                    )
                await conn.execute(
                    "UPDATE lottery_rounds SET end_time = now(), winner_side = $1, total_bank = $2, status = 'finished' WHERE id = $3",
                    result, total_bank, round_id
                )

            await asyncio.sleep(LOTTERY_REVEAL_SECONDS)
        except Exception as e:
            print(f"Ошибка в lottery_loop: {e}")
            await asyncio.sleep(5)


async def passive_income_loop():
    while True:
        await asyncio.sleep(1)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET balance = balance + per_sec, total_earned = total_earned + per_sec WHERE per_sec > 0"
                )
        except Exception as e:
            print(f"Ошибка пассивного дохода: {e}")


async def disable_boost(telegram_id, original_per_sec):
    await asyncio.sleep(300)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET per_sec = $1, boost_active = FALSE WHERE telegram_id = $2",
                original_per_sec, telegram_id
            )
    except Exception as e:
        print(f"Ошибка disable_boost: {e}")


# ===== КОМАНДА /start =====
@dp.message(Command("start"))
async def start(message: types.Message):
    ref = None
    if len(message.text.split()) > 1:
        ref_code = message.text.split()[1]
        async with pool.acquire() as conn:
            ref_row = await conn.fetchrow("SELECT id FROM users WHERE referral_code = $1", ref_code)
        if ref_row:
            ref = ref_row['id']

    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name, None, ref
    )
    if ref and user['total_taps'] == 0 and user['total_earned'] == 0:
        async with pool.acquire() as conn:
            ref_telegram_row = await conn.fetchrow("SELECT telegram_id FROM users WHERE id = $1", ref)
        if ref_telegram_row:
            await update_balance(ref_telegram_row['telegram_id'], 50)
            try:
                await bot.send_message(ref_telegram_row['telegram_id'], "🎉 Вам начислено 50 коинов за приглашённого!")
            except Exception:
                pass

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Играть", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(text="👥 Рефералка", callback_data="referral")],
        [InlineKeyboardButton(text="🏆 Топ", callback_data="top")]
    ])
    await message.answer(
        "🐱 Добро пожаловать в TapiX!\n\n"
        "Нажимай на монету, зарабатывай коины, участвуй в лотереях и становись самым богатым!",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "referral")
async def show_referral(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    if user:
        code = user['referral_code']
        link = f"https://t.me/TapiX1_bot?start={code}"
        await callback.message.answer(
            f"👥 Твоя реферальная ссылка:\n{link}\n\nЗа каждого приглашённого ты получаешь 50 коинов!"
        )
    await callback.answer()


@dp.callback_query(F.data == "top")
async def show_top(callback: types.CallbackQuery):
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT telegram_id, username, balance FROM users ORDER BY balance DESC LIMIT 10")
    text = "🏆 ТОП ПО БАЛАНСУ:\n\n"
    for i, u in enumerate(rows, 1):
        text += f"{i}. @{u['username'] or 'Аноним'} — {u['balance']:.2f} ₿\n"
    await callback.message.answer(text)
    await callback.answer()


# ===== API =====
def user_to_dict(user, place=0):
    return {
        'success': True,
        'balance': float(user['balance']),
        'per_tap': float(user['per_tap']),
        'per_sec': float(user['per_sec']),
        'total_earned': float(user['total_earned']),
        'total_taps': user['total_taps'],
        'level': user['level'],
        'xp': float(user['xp']),
        'xp_max': float(user['xp_max']),
        'referral_code': user['referral_code'],
        'referral_total': 0,
        'referral_earned': 0,
        'shop_tap': [user['shop_tap0'], user['shop_tap1'], user['shop_tap2'], user['shop_tap3'], user['shop_tap4']],
        'shop_sec': [user['shop_sec0'], user['shop_sec1'], user['shop_sec2'], user['shop_sec3'], user['shop_sec4']],
        'sound_on': bool(user['sound_on']),
        'theme': user['theme'],
        'dildo_activated': bool(user['dildo_activated']),
        'place': place,
    }


async def api_handler(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'success': False, 'error': 'Bad request'}, status=400)

    action = data.get('action')
    telegram_id = data.get('telegram_id')
    if telegram_id is None:
        return web.json_response({'success': False, 'error': 'telegram_id required'}, status=400)
    try:
        telegram_id = int(telegram_id)
    except (TypeError, ValueError):
        return web.json_response({'success': False, 'error': 'Invalid telegram_id'}, status=400)

    if action == 'get_user':
        username = data.get('username')
        first_name = data.get('first_name')
        avatar_url = data.get('avatar_url')
        user = await get_or_create_user(telegram_id, username, first_name, avatar_url)

        # Если аватарки нет — запрашиваем у Telegram Bot API
        if not avatar_url and not user['avatar_url']:
            try:
                photos = await bot.get_user_profile_photos(telegram_id, limit=1)
                if photos.total_count > 0:
                    file = await bot.get_file(photos.photos[0][0].file_id)
                    avatar_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
            except Exception:
                avatar_url = None

        if username is not None or first_name is not None or avatar_url is not None:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET username = COALESCE($1, username), first_name = COALESCE($2, first_name), "
                    "avatar_url = COALESCE($3, avatar_url), last_active = now() WHERE telegram_id = $4",
                    username, first_name, avatar_url, telegram_id
                )
            user = await get_user(telegram_id)
        async with pool.acquire() as conn:
            place = await conn.fetchval(
                "SELECT COUNT(*) + 1 FROM users WHERE balance > (SELECT balance FROM users WHERE telegram_id = $1)",
                telegram_id
            )
        result = user_to_dict(user, place)
        result['avatar_url'] = user['avatar_url']
        result['first_name'] = user['first_name']
        return web.json_response(result)

    if action == 'tap':
        if not check_rate_limit(telegram_id):
            return web.json_response({'success': False, 'error': 'Слишком часто'})
        user = await get_user(telegram_id)
        if not user:
            return web.json_response({'success': False, 'error': 'User not found'})
        earn = float(user['per_tap'])
        async with pool.acquire() as conn:
            new_balance = await conn.fetchval(
                "UPDATE users SET balance = balance + $1, total_earned = total_earned + $1, "
                "total_taps = total_taps + 1 WHERE telegram_id = $2 RETURNING balance",
                earn, telegram_id
            )
        return web.json_response({'success': True, 'new_balance': float(new_balance)})

    if action == 'transfer':
        from_user = telegram_id
        to_username = (data.get('to_username') or '').lstrip('@')
        try:
            amount = float(data.get('amount', 0))
        except (TypeError, ValueError):
            return web.json_response({'success': False, 'error': 'Некорректная сумма'})
        if amount <= 0:
            return web.json_response({'success': False, 'error': 'Некорректная сумма'})
        fee = amount * 0.05
        total = amount + fee

        async with pool.acquire() as conn:
            to_user = await conn.fetchrow("SELECT telegram_id FROM users WHERE username = $1", to_username)
            if not to_user:
                return web.json_response({'success': False, 'error': 'Пользователь не найден'})
            if to_user['telegram_id'] == from_user:
                return web.json_response({'success': False, 'error': 'Нельзя перевести самому себе'})
            async with conn.transaction():
                from_balance = await conn.fetchval("SELECT balance FROM users WHERE telegram_id = $1 FOR UPDATE", from_user)
                if from_balance is None or from_balance < total:
                    return web.json_response({'success': False, 'error': 'Недостаточно средств'})
                new_balance = await conn.fetchval(
                    "UPDATE users SET balance = balance - $1 WHERE telegram_id = $2 RETURNING balance",
                    total, from_user
                )
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", amount, to_user['telegram_id'])
                await conn.execute(
                    "INSERT INTO transfers (from_telegram_id, to_telegram_id, amount, fee, total, message) VALUES ($1,$2,$3,$4,$5,$6)",
                    from_user, to_user['telegram_id'], amount, fee, total, data.get('message')
                )
        return web.json_response({'success': True, 'new_balance': float(new_balance)})

    if action == 'get_top':
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT telegram_id, username, avatar_url, balance FROM users ORDER BY balance DESC LIMIT 50"
            )
        top = [{
            'telegram_id': r['telegram_id'],
            'username': r['username'],
            'avatar': r['avatar_url'],
            'balance': float(r['balance']),
        } for r in rows]
        return web.json_response({'success': True, 'top': top})

    if action == 'buy_shop':
        type_ = data.get('type')
        idx = data.get('idx')
        if type_ not in ('tap', 'sec') or not isinstance(idx, int) or not (0 <= idx <= 4):
            return web.json_response({'success': False, 'error': 'Некорректный товар'})
        user = await get_user(telegram_id)
        if not user:
            return web.json_response({'success': False, 'error': 'User not found'})
        cfg = SHOP_TAP_CFG if type_ == 'tap' else SHOP_SEC_CFG
        col = f'shop_{type_}{idx}'
        level = user[col]
        price = int(cfg[idx]['basePrice'] * (cfg[idx]['priceMult'] ** level))
        if user['balance'] < price:
            return web.json_response({'success': False, 'error': 'Недостаточно средств'})
        bonus = cfg[idx]['baseBonus'] * (2 ** level)
        field = 'per_tap' if type_ == 'tap' else 'per_sec'
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE users SET balance = balance - $1, {col} = {col} + 1, {field} = {field} + $2 "
                f"WHERE telegram_id = $3 RETURNING balance, {field}",
                float(price), bonus, telegram_id
            )
        return web.json_response({'success': True, 'new_balance': float(row['balance']), field: float(row[field])})

    if action == 'buy_boost':
        user = await get_user(telegram_id)
        if not user:
            return web.json_response({'success': False, 'error': 'User not found'})
        if user['boost_active']:
            return web.json_response({'success': False, 'error': 'Бустер уже активен'})
        now = datetime.now(timezone.utc)
        if user['boost_cooldown'] and user['boost_cooldown'] > now:
            return web.json_response({'success': False, 'error': 'Бустер на перезарядке'})
        if user['balance'] < 300:
            return web.json_response({'success': False, 'error': 'Недостаточно средств'})
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE users SET balance = balance - 300, per_sec = per_sec * 2, boost_active = TRUE, "
                "boost_cooldown = $1 WHERE telegram_id = $2 RETURNING balance, per_sec",
                now + timedelta(hours=1), telegram_id
            )
        asyncio.create_task(disable_boost(telegram_id, float(row['per_sec']) / 2))
        return web.json_response({'success': True, 'new_balance': float(row['balance']), 'per_sec': float(row['per_sec'])})

    if action == 'activate_promo':
        code = (data.get('code') or '').upper()
        user = await get_user(telegram_id)
        if not user:
            return web.json_response({'success': False, 'error': 'User not found'})
        if code == 'WELCOME' and not user['promo_used']:
            async with pool.acquire() as conn:
                new_balance = await conn.fetchval(
                    "UPDATE users SET balance = balance + 100, promo_used = TRUE WHERE telegram_id = $1 RETURNING balance",
                    telegram_id
                )
            return web.json_response({'success': True, 'new_balance': float(new_balance), 'bonus': 100})
        elif code == 'DILDO' and not user['dildo_activated']:
            async with pool.acquire() as conn:
                await conn.execute("UPDATE users SET dildo_activated = TRUE WHERE telegram_id = $1", telegram_id)
            return web.json_response({'success': True, 'message': 'Дилдокликер активирован!'})
        else:
            return web.json_response({'success': False, 'error': 'Неверный или уже использованный код'})

    if action == 'set_theme':
        theme = data.get('theme', 'dark')
        async with pool.acquire() as conn:
            await conn.execute("UPDATE users SET theme = $1 WHERE telegram_id = $2", theme, telegram_id)
        return web.json_response({'success': True})

    if action == 'set_sound':
        sound = bool(data.get('sound_on', True))
        async with pool.acquire() as conn:
            await conn.execute("UPDATE users SET sound_on = $1 WHERE telegram_id = $2", sound, telegram_id)
        return web.json_response({'success': True})

    # ===== ЛОТЕРЕЯ =====
    if action == 'get_lottery_state':
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT lb.telegram_id, lb.side, SUM(lb.amount) AS amount, u.avatar_url "
                "FROM lottery_bets lb JOIN users u ON u.telegram_id = lb.telegram_id "
                "WHERE lb.round_id = $1 GROUP BY lb.telegram_id, lb.side, u.avatar_url ORDER BY amount DESC",
                lottery_state['round_id']
            )
        eagle = [{'telegram_id': r['telegram_id'], 'avatar': r['avatar_url'], 'amount': float(r['amount'])} for r in rows if r['side'] == 'eagle']
        tails = [{'telegram_id': r['telegram_id'], 'avatar': r['avatar_url'], 'amount': float(r['amount'])} for r in rows if r['side'] == 'tails']
        time_left = max(0, lottery_state['phase_end'] - time.time())
        return web.json_response({
            'success': True,
            'round_id': lottery_state['round_id'],
            'phase': lottery_state['phase'],
            'time_left': time_left,
            'hash': lottery_state['hash'],
            'seed': lottery_state['seed'] if lottery_state['phase'] == 'resolving' else None,
            'result': lottery_state['result'],
            'eagle': eagle,
            'tails': tails,
        })

    if action == 'lottery_bet':
        if lottery_state['phase'] != 'betting':
            return web.json_response({'success': False, 'error': 'Ставки уже закрыты'})
        side = data.get('side')
        if side not in ('eagle', 'tails'):
            return web.json_response({'success': False, 'error': 'Некорректная сторона'})
        try:
            amount = float(data.get('amount', 0))
        except (TypeError, ValueError):
            return web.json_response({'success': False, 'error': 'Некорректная ставка'})
        if amount < LOTTERY_MIN_BET or amount > LOTTERY_MAX_BET:
            return web.json_response({'success': False, 'error': f'Ставка от {LOTTERY_MIN_BET} до {LOTTERY_MAX_BET} ₿'})
        round_id = lottery_state['round_id']
        async with pool.acquire() as conn:
            async with conn.transaction():
                balance = await conn.fetchval("SELECT balance FROM users WHERE telegram_id = $1 FOR UPDATE", telegram_id)
                if balance is None or balance < amount:
                    return web.json_response({'success': False, 'error': 'Недостаточно средств'})
                new_balance = await conn.fetchval(
                    "UPDATE users SET balance = balance - $1 WHERE telegram_id = $2 RETURNING balance",
                    amount, telegram_id
                )
                await conn.execute(
                    "INSERT INTO lottery_bets (round_id, telegram_id, side, amount) VALUES ($1,$2,$3,$4)",
                    round_id, telegram_id, side, amount
                )
        return web.json_response({'success': True, 'new_balance': float(new_balance)})

    # ===== МИНЁР =====
    if action == 'start_miner':
        if telegram_id in miner_sessions:
            return web.json_response({'success': False, 'error': 'Игра уже идёт'})
        try:
            bet = float(data.get('bet', 0))
            bombs = int(data.get('bombs', 3))
        except (TypeError, ValueError):
            return web.json_response({'success': False, 'error': 'Некорректные параметры'})
        if bet < MINER_MIN_BET or bet > MINER_MAX_BET:
            return web.json_response({'success': False, 'error': f'Ставка от {MINER_MIN_BET} до {MINER_MAX_BET} ₿'})
        if bombs < 1 or bombs > 5:
            return web.json_response({'success': False, 'error': 'Бомб от 1 до 5'})
        async with pool.acquire() as conn:
            async with conn.transaction():
                balance = await conn.fetchval("SELECT balance FROM users WHERE telegram_id = $1 FOR UPDATE", telegram_id)
                if balance is None or balance < bet:
                    return web.json_response({'success': False, 'error': 'Недостаточно средств'})
                new_balance = await conn.fetchval(
                    "UPDATE users SET balance = balance - $1 WHERE telegram_id = $2 RETURNING balance",
                    bet, telegram_id
                )
        bomb_positions = set()
        while len(bomb_positions) < bombs:
            bomb_positions.add(secrets.randbelow(MINER_GRID_SIZE))
        miner_sessions[telegram_id] = {
            'bet': bet, 'bombs': bombs, 'bomb_positions': bomb_positions,
            'revealed': set(), 'opened': 0, 'active': True,
        }
        return web.json_response({'success': True, 'new_balance': float(new_balance), 'total_gems': MINER_GRID_SIZE - bombs})

    if action == 'reveal_miner_cell':
        session = miner_sessions.get(telegram_id)
        if not session or not session['active']:
            return web.json_response({'success': False, 'error': 'Нет активной игры'})
        try:
            index = int(data.get('index'))
        except (TypeError, ValueError):
            return web.json_response({'success': False, 'error': 'Некорректная ячейка'})
        if index < 0 or index >= MINER_GRID_SIZE or index in session['revealed']:
            return web.json_response({'success': False, 'error': 'Некорректная ячейка'})
        session['revealed'].add(index)
        if index in session['bomb_positions']:
            session['active'] = False
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO game_history (telegram_id, game, result, amount) VALUES ($1, 'miner', 'lose', $2)",
                    telegram_id, -session['bet']
                )
            bombs_left = list(session['bomb_positions'] - {index})
            del miner_sessions[telegram_id]
            return web.json_response({'success': True, 'result': 'bomb', 'bomb_positions': bombs_left})
        session['opened'] += 1
        coef = calc_miner_multiplier(session['bombs'], session['opened'])
        complete = session['opened'] == (MINER_GRID_SIZE - session['bombs'])
        if complete:
            win = session['bet'] * coef
            async with pool.acquire() as conn:
                new_balance = await conn.fetchval(
                    "UPDATE users SET balance = balance + $1, total_earned = total_earned + $1 WHERE telegram_id = $2 RETURNING balance",
                    win, telegram_id
                )
                await conn.execute(
                    "INSERT INTO game_history (telegram_id, game, result, amount) VALUES ($1, 'miner', 'win', $2)",
                    telegram_id, win
                )
            del miner_sessions[telegram_id]
            return web.json_response({'success': True, 'result': 'gem', 'coef': coef, 'complete': True,
                                       'win_amount': win, 'new_balance': float(new_balance)})
        return web.json_response({'success': True, 'result': 'gem', 'coef': coef, 'complete': False})

    if action == 'miner_cashout':
        session = miner_sessions.get(telegram_id)
        if not session or not session['active'] or session['opened'] == 0:
            return web.json_response({'success': False, 'error': 'Нечего забирать'})
        coef = calc_miner_multiplier(session['bombs'], session['opened'])
        win = session['bet'] * coef
        async with pool.acquire() as conn:
            new_balance = await conn.fetchval(
                "UPDATE users SET balance = balance + $1, total_earned = total_earned + $1 WHERE telegram_id = $2 RETURNING balance",
                win, telegram_id
            )
            await conn.execute(
                "INSERT INTO game_history (telegram_id, game, result, amount) VALUES ($1, 'miner', 'cashout', $2)",
                telegram_id, win
            )
        bomb_positions = list(session['bomb_positions'])
        del miner_sessions[telegram_id]
        return web.json_response({'success': True, 'win_amount': win, 'new_balance': float(new_balance), 'bomb_positions': bomb_positions})

    return web.json_response({'success': False, 'error': 'Unknown action'})


# ===== ОТДАЁМ INDEX.HTML =====
async def index_handler(request):
    try:
        with open('index.html', 'r', encoding='utf-8') as f:
            return web.Response(text=f.read(), content_type='text/html')
    except FileNotFoundError:
        return web.Response(text='<h1>404</h1><p>index.html not found</p>', content_type='text/html', status=404)


# ===== ВЕБХУК =====
async def webhook_handler(request):
    data = await request.json()
    update = Update(**data)
    await dp.feed_update(bot, update)
    return web.Response(status=200)


# ===== ЗАПУСК =====
async def main():
    await init_db()
    asyncio.create_task(passive_income_loop())
    asyncio.create_task(lottery_loop())
    app = web.Application()
    app.router.add_post('/webhook', webhook_handler)
    app.router.add_post('/api', api_handler)
    app.router.add_get('/', index_handler)
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ Сервер запущен на порту {port}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
