import asyncio
import os
import random
import time
import json
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.types import Update
from aiohttp import web
import asyncpg
from asyncpg import Pool

BOT_TOKEN = os.getenv("BOT_TOKEN", "8913794504:AAGqTwhUYZ4QDAUbUOw6SGVclNKzkvDGKOY")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tapix_user:HA6DOLoZz1IdobdfmKX8QT4U1IFVuoNI@dpg-d8qciaflk1mc739p6sag-a/tapix")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ===== ПУЛ ПОДКЛЮЧЕНИЙ К БАЗЕ =====
db_pool: Pool = None

# ===== КЭШ ТОПА =====
top_cache = {
    'data': [],
    'updated_at': 0
}

# ===== RATE LIMITING =====
tap_limits = {}

# ===== ИНИЦИАЛИЗАЦИЯ БАЗЫ =====
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=20)
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                balance REAL DEFAULT 0,
                per_tap REAL DEFAULT 0.001,
                per_sec REAL DEFAULT 0.00005,
                total_earned REAL DEFAULT 0,
                total_taps INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                xp REAL DEFAULT 0,
                xp_max REAL DEFAULT 800,
                referral_code TEXT UNIQUE,
                referred_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP,
                sound_on BOOLEAN DEFAULT TRUE,
                theme TEXT DEFAULT 'dark',
                shop_tap0 INTEGER DEFAULT 0,
                shop_tap1 INTEGER DEFAULT 0,
                shop_tap2 INTEGER DEFAULT 0,
                shop_tap3 INTEGER DEFAULT 0,
                shop_tap4 INTEGER DEFAULT 0,
                shop_sec0 INTEGER DEFAULT 0,
                shop_sec1 INTEGER DEFAULT 0,
                shop_sec2 INTEGER DEFAULT 0,
                shop_sec3 INTEGER DEFAULT 0,
                shop_sec4 INTEGER DEFAULT 0,
                boost_active BOOLEAN DEFAULT FALSE,
                boost_cooldown TIMESTAMP,
                promo_used BOOLEAN DEFAULT FALSE,
                dildo_activated BOOLEAN DEFAULT FALSE
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS transfers (
                id SERIAL PRIMARY KEY,
                from_user BIGINT,
                to_user BIGINT,
                amount REAL,
                fee REAL,
                total REAL,
                message TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS lottery_rounds (
                id SERIAL PRIMARY KEY,
                start_time TIMESTAMP,
                end_time TIMESTAMP,
                winner_side TEXT,
                total_bank REAL,
                status TEXT DEFAULT 'active'
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS lottery_bets (
                id SERIAL PRIMARY KEY,
                round_id INTEGER,
                user_id BIGINT,
                side TEXT,
                amount REAL,
                is_bot BOOLEAN DEFAULT FALSE
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS game_history (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                game TEXT,
                result TEXT,
                amount REAL,
                date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    print("✅ База данных инициализирована")

# ===== ФУНКЦИИ РАБОТЫ С БАЗОЙ =====
async def get_user(telegram_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)

async def get_or_create_user(telegram_id, username, referred_by=None):
    user = await get_user(telegram_id)
    if user:
        return user
    code = ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=6))
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO users (telegram_id, username, referral_code, referred_by)
            VALUES ($1, $2, $3, $4)
        ''', telegram_id, username, code, referred_by)
    return await get_user(telegram_id)

async def update_balance(telegram_id, amount):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", amount, telegram_id)

async def get_top_balance(limit=50):
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT telegram_id, username, balance FROM users ORDER BY balance DESC LIMIT $1", limit)

async def update_user_field(telegram_id, field, value):
    async with db_pool.acquire() as conn:
        await conn.execute(f"UPDATE users SET {field} = $1 WHERE telegram_id = $2", value, telegram_id)

# ===== ФОНОВАЯ ЗАДАЧА ДЛЯ ПАССИВНОГО ДОХОДА =====
async def passive_income_loop():
    while True:
        await asyncio.sleep(1)
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET balance = balance + per_sec, total_earned = total_earned + per_sec WHERE per_sec > 0")
        except Exception as e:
            print(f"Ошибка пассивного дохода: {e}")

# ===== ОБНОВЛЕНИЕ КЭША ТОПА =====
async def update_top_cache():
    global top_cache
    while True:
        await asyncio.sleep(10)
        try:
            rows = await get_top_balance(50)
            top_cache['data'] = [{'telegram_id': r['telegram_id'], 'username': r['username'], 'balance': float(r['balance'])} for r in rows]
            top_cache['updated_at'] = time.time()
        except Exception as e:
            print(f"Ошибка обновления кэша топа: {e}")

# ===== RATE LIMITING =====
def check_rate_limit(telegram_id):
    now = time.time()
    if telegram_id not in tap_limits:
        tap_limits[telegram_id] = []
    tap_limits[telegram_id] = [t for t in tap_limits[telegram_id] if now - t < 1]
    if len(tap_limits[telegram_id]) >= 5:
        return False
    tap_limits[telegram_id].append(now)
    return True

# ===== КОМАНДА /start =====
@dp.message(Command("start"))
async def start(message: types.Message):
    ref = None
    if len(message.text.split()) > 1:
        ref_code = message.text.split()[1]
        async with db_pool.acquire() as conn:
            ref_user = await conn.fetchrow("SELECT id FROM users WHERE referral_code = $1", ref_code)
            if ref_user:
                ref = ref_user['id']
    user = await get_or_create_user(message.from_user.id, message.from_user.username, ref)
    if ref and user['balance'] == 0:
        await update_balance(ref, 50)
        await bot.send_message(ref, "🎉 Вам начислено 50 коинов за приглашённого!")

    WEBAPP_URL = "https://tapix-3k3q.onrender.com?v=7"
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
            f"👥 Твоя реферальная ссылка:\n{link}\n\n"
            f"За каждого приглашённого ты получаешь 50 коинов!"
        )
    await callback.answer()

@dp.callback_query(F.data == "top")
async def show_top(callback: types.CallbackQuery):
    data = top_cache['data'][:10]
    text = "🏆 ТОП ПО БАЛАНСУ:\n\n"
    for i, u in enumerate(data, 1):
        text += f"{i}. @{u['username'] or 'Аноним'} — {u['balance']:.2f} ₿\n"
    await callback.message.answer(text)
    await callback.answer()

# ===== API ДЛЯ ИГРЫ =====
async def api_handler(request):
    data = await request.json()
    action = data.get('action')
    telegram_id = data.get('telegram_id')

    if action == 'get_user':
        user = await get_user(telegram_id)
        if user:
            return web.json_response({
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
                'sound_on': user['sound_on'],
                'theme': user['theme'],
                'dildo_activated': user['dildo_activated'],
                'place': 0
            })
        return web.json_response({'success': False, 'error': 'User not found'})

    if action == 'tap':
        if not check_rate_limit(telegram_id):
            return web.json_response({'success': False, 'error': 'Слишком часто'})
        user = await get_user(telegram_id)
        if user:
            earn = float(user['per_tap'])
            await update_balance(telegram_id, earn)
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET total_taps = total_taps + 1, total_earned = total_earned + $1 WHERE telegram_id = $2", earn, telegram_id)
            updated = await get_user(telegram_id)
            return web.json_response({'success': True, 'new_balance': float(updated['balance'])})
        return web.json_response({'success': False, 'error': 'User not found'})

    if action == 'transfer':
        from_user = telegram_id
        to_username = data.get('to_username')
        amount = float(data.get('amount', 0))
        fee = amount * 0.05
        total = amount + fee

        async with db_pool.acquire() as conn:
            to_user = await conn.fetchrow("SELECT id FROM users WHERE username = $1", to_username)
            if not to_user:
                return web.json_response({'success': False, 'error': 'Пользователь не найден'})
            from_balance = await conn.fetchval("SELECT balance FROM users WHERE telegram_id = $1", from_user)
            if from_balance < total:
                return web.json_response({'success': False, 'error': 'Недостаточно средств'})
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE telegram_id = $2", total, from_user)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE id = $2", amount, to_user['id'])
            await conn.execute('''
                INSERT INTO transfers (from_user, to_user, amount, fee, total)
                VALUES ($1, $2, $3, $4, $5)
            ''', from_user, to_user['id'], amount, fee, total)
        updated = await get_user(from_user)
        return web.json_response({'success': True, 'new_balance': float(updated['balance'])})

    if action == 'get_top':
        return web.json_response({
            'success': True,
            'top': top_cache['data']
        })

    if action == 'buy_shop':
        type_ = data.get('type')
        idx = data.get('idx')
        user = await get_user(telegram_id)
        if not user:
            return web.json_response({'success': False, 'error': 'User not found'})
        if type_ == 'tap':
            levels = [user['shop_tap0'], user['shop_tap1'], user['shop_tap2'], user['shop_tap3'], user['shop_tap4']]
            cfg = [
                {'baseBonus': 0.0005, 'basePrice': 30, 'priceMult': 3.0},
                {'baseBonus': 0.0010, 'basePrice': 90, 'priceMult': 3.0},
                {'baseBonus': 0.0020, 'basePrice': 270, 'priceMult': 3.0},
                {'baseBonus': 0.0040, 'basePrice': 810, 'priceMult': 3.0},
                {'baseBonus': 0.0080, 'basePrice': 2430, 'priceMult': 3.0}
            ]
            level = levels[idx]
            price = int(cfg[idx]['basePrice'] * (cfg[idx]['priceMult'] ** level))
            if user['balance'] < price:
                return web.json_response({'success': False, 'error': 'Недостаточно средств'})
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET balance = balance - $1 WHERE telegram_id = $2", price, telegram_id)
                col = f'shop_tap{idx}'
                await conn.execute(f"UPDATE users SET {col} = {col} + 1, per_tap = per_tap + $1 WHERE telegram_id = $2",
                                   cfg[idx]['baseBonus'] * (2 ** level), telegram_id)
            updated = await get_user(telegram_id)
            return web.json_response({'success': True, 'new_balance': float(updated['balance']), 'per_tap': float(updated['per_tap'])})
        elif type_ == 'sec':
            levels = [user['shop_sec0'], user['shop_sec1'], user['shop_sec2'], user['shop_sec3'], user['shop_sec4']]
            cfg = [
                {'baseBonus': 0.00025, 'basePrice': 36, 'priceMult': 3.0},
                {'baseBonus': 0.00050, 'basePrice': 108, 'priceMult': 3.0},
                {'baseBonus': 0.00100, 'basePrice': 324, 'priceMult': 3.0},
                {'baseBonus': 0.00200, 'basePrice': 972, 'priceMult': 3.0},
                {'baseBonus': 0.00400, 'basePrice': 2916, 'priceMult': 3.0}
            ]
            level = levels[idx]
            price = int(cfg[idx]['basePrice'] * (cfg[idx]['priceMult'] ** level))
            if user['balance'] < price:
                return web.json_response({'success': False, 'error': 'Недостаточно средств'})
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET balance = balance - $1 WHERE telegram_id = $2", price, telegram_id)
                col = f'shop_sec{idx}'
                await conn.execute(f"UPDATE users SET {col} = {col} + 1, per_sec = per_sec + $1 WHERE telegram_id = $2",
                                   cfg[idx]['baseBonus'] * (2 ** level), telegram_id)
            updated = await get_user(telegram_id)
            return web.json_response({'success': True, 'new_balance': float(updated['balance']), 'per_sec': float(updated['per_sec'])})

    if action == 'buy_boost':
        user = await get_user(telegram_id)
        if not user:
            return web.json_response({'success': False, 'error': 'User not found'})
        if user['boost_active']:
            return web.json_response({'success': False, 'error': 'Бустер уже активен'})
        now = datetime.now()
        if user['boost_cooldown'] and user['boost_cooldown'] > now:
            return web.json_response({'success': False, 'error': 'Бустер на перезарядке'})
        if user['balance'] < 300:
            return web.json_response({'success': False, 'error': 'Недостаточно средств'})
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance - 300, per_sec = per_sec * 2, boost_active = TRUE, boost_cooldown = $1 WHERE telegram_id = $2",
                               now + timedelta(hours=1), telegram_id)
        updated = await get_user(telegram_id)
        asyncio.create_task(disable_boost(telegram_id, float(updated['per_sec']) / 2))
        return web.json_response({'success': True, 'new_balance': float(updated['balance']), 'per_sec': float(updated['per_sec'])})

    if action == 'activate_promo':
        code = data.get('code', '').upper()
        user = await get_user(telegram_id)
        if not user:
            return web.json_response({'success': False, 'error': 'User not found'})
        if code == 'WELCOME' and not user['promo_used']:
            await update_balance(telegram_id, 100)
            await update_user_field(telegram_id, 'promo_used', True)
            updated = await get_user(telegram_id)
            return web.json_response({'success': True, 'new_balance': float(updated['balance']), 'bonus': 100})
        elif code == 'DILDO' and not user['dildo_activated']:
            await update_user_field(telegram_id, 'dildo_activated', True)
            return web.json_response({'success': True, 'message': 'Дилдокликер активирован!'})
        else:
            return web.json_response({'success': False, 'error': 'Неверный или уже использованный код'})

    if action == 'set_theme':
        theme = data.get('theme', 'dark')
        await update_user_field(telegram_id, 'theme', theme)
        return web.json_response({'success': True})

    if action == 'set_sound':
        sound = data.get('sound_on', True)
        await update_user_field(telegram_id, 'sound_on', sound)
        return web.json_response({'success': True})

    if action == 'lottery_bet' or action == 'start_miner' or action == 'miner_lose' or action == 'miner_win' or action == 'miner_cashout':
        return web.json_response({'success': False, 'error': 'Функция временно недоступна'})

    return web.json_response({'success': False, 'error': 'Unknown action'})

async def disable_boost(telegram_id, original_per_sec):
    await asyncio.sleep(300)
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET per_sec = $1, boost_active = FALSE WHERE telegram_id = $2", original_per_sec, telegram_id)

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

# ===== ЗАПУСК ВЕБ-СЕРВЕРА =====
async def main():
    await init_db()
    asyncio.create_task(passive_income_loop())
    asyncio.create_task(update_top_cache())
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
