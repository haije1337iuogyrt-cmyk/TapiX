import asyncio
import sqlite3
import random
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.types import Update
from aiohttp import web

BOT_TOKEN = os.getenv("BOT_TOKEN", "8913794504:AAGqTwhUYZ4QDAUbUOw6SGVclNKzkvDGKOY")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ===== БАЗА ДАННЫХ =====
conn = sqlite3.connect('game.db', check_same_thread=False)
cursor = conn.cursor()

cursor.executescript('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE,
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
        sound_on INTEGER DEFAULT 1,
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
        boost_active INTEGER DEFAULT 0,
        boost_cooldown INTEGER DEFAULT 0,
        promo_used INTEGER DEFAULT 0,
        dildo_activated INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS transfers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user INTEGER,
        to_user INTEGER,
        amount REAL,
        fee REAL,
        total REAL,
        message TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS lottery_rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_time TIMESTAMP,
        end_time TIMESTAMP,
        winner_side TEXT,
        total_bank REAL,
        status TEXT DEFAULT 'active'
    );

    CREATE TABLE IF NOT EXISTS lottery_bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER,
        user_id INTEGER,
        side TEXT,
        amount REAL,
        is_bot INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS game_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        game TEXT,
        result TEXT,
        amount REAL,
        date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
''')
conn.commit()

# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
def get_user(telegram_id):
    cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    return cursor.fetchone()

def get_or_create_user(telegram_id, username, referred_by=None):
    user = get_user(telegram_id)
    if user:
        return user
    code = ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=6))
    cursor.execute('''
        INSERT INTO users (telegram_id, username, referral_code, referred_by)
        VALUES (?, ?, ?, ?)
    ''', (telegram_id, username, code, referred_by))
    conn.commit()
    return get_user(telegram_id)

def update_balance(telegram_id, amount):
    cursor.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (amount, telegram_id))
    conn.commit()

def get_top_balance(limit=50):
    cursor.execute("SELECT telegram_id, username, balance FROM users ORDER BY balance DESC LIMIT ?", (limit,))
    return cursor.fetchall()

# ===== КОМАНДА /start =====
@dp.message(Command("start"))
async def start(message: types.Message):
    ref = None
    if len(message.text.split()) > 1:
        ref_code = message.text.split()[1]
        cursor.execute("SELECT id FROM users WHERE referral_code = ?", (ref_code,))
        ref_user = cursor.fetchone()
        if ref_user:
            ref = ref_user[0]
    user = get_or_create_user(message.from_user.id, message.from_user.username, ref)
    if ref and user[5] == 0:
        update_balance(ref, 50)
        await bot.send_message(ref, "🎉 Вам начислено 50 коинов за приглашённого!")

    WEBAPP_URL = "https://tapix-3k3q.onrender.com?v=5"  # <--- НОВЫЙ URL
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
    user = get_user(callback.from_user.id)
    if user:
        code = user[6]
        link = f"https://t.me/TapiX1_bot?start={code}"
        await callback.message.answer(
            f"👥 Твоя реферальная ссылка:\n{link}\n\n"
            f"За каждого приглашённого ты получаешь 50 коинов!"
        )
    await callback.answer()

@dp.callback_query(F.data == "top")
async def show_top(callback: types.CallbackQuery):
    top = get_top_balance(10)
    text = "🏆 ТОП ПО БАЛАНСУ:\n\n"
    for i, (tg_id, username, balance) in enumerate(top, 1):
        text += f"{i}. @{username or 'Аноним'} — {balance:.2f} ₿\n"
    await callback.message.answer(text)
    await callback.answer()

# ===== API ДЛЯ ИГРЫ =====
async def api_handler(request):
    data = await request.json()
    action = data.get('action')
    telegram_id = data.get('telegram_id')

    if action == 'get_user':
        user = get_user(telegram_id)
        if user:
            return web.json_response({
                'success': True,
                'balance': user[2],
                'per_tap': user[3],
                'per_sec': user[4],
                'total_earned': user[5],
                'total_taps': user[6],
                'level': user[7],
                'xp': user[8],
                'xp_max': user[9],
                'referral_code': user[10],
                'referral_total': cursor.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user[0],)).fetchone()[0],
                'referral_earned': 0,
                'shop_tap': [user[17], user[18], user[19], user[20], user[21]],
                'shop_sec': [user[22], user[23], user[24], user[25], user[26]]
            })
        return web.json_response({'success': False, 'error': 'User not found'})

    if action == 'tap':
        user = get_user(telegram_id)
        if user:
            earn = user[3]
            update_balance(telegram_id, earn)
            cursor.execute("UPDATE users SET total_taps = total_taps + 1, total_earned = total_earned + ? WHERE telegram_id = ?", (earn, telegram_id))
            conn.commit()
            return web.json_response({'success': True, 'new_balance': user[2] + earn})
        return web.json_response({'success': False, 'error': 'User not found'})

    if action == 'transfer':
        from_user = telegram_id
        to_username = data.get('to_username')
        amount = float(data.get('amount', 0))
        fee = amount * 0.05
        total = amount + fee

        cursor.execute("SELECT id FROM users WHERE username = ?", (to_username,))
        to_user = cursor.fetchone()
        if not to_user:
            return web.json_response({'success': False, 'error': 'Пользователь не найден'})

        cursor.execute("SELECT balance FROM users WHERE telegram_id = ?", (from_user,))
        balance = cursor.fetchone()
        if not balance or balance[0] < total:
            return web.json_response({'success': False, 'error': 'Недостаточно средств'})

        update_balance(from_user, -total)
        update_balance(to_user[0], amount)
        cursor.execute('INSERT INTO transfers (from_user, to_user, amount, fee, total) VALUES (?, ?, ?, ?, ?)',
                       (from_user, to_user[0], amount, fee, total))
        conn.commit()
        return web.json_response({'success': True, 'new_balance': balance[0] - total})

    if action == 'get_top':
        top = get_top_balance(50)
        return web.json_response({
            'success': True,
            'top': [{'telegram_id': t[0], 'username': t[1], 'balance': t[2]} for t in top]
        })

    if action == 'buy_shop':
        type_ = data.get('type')
        idx = data.get('idx')
        user = get_user(telegram_id)
        if not user:
            return web.json_response({'success': False, 'error': 'User not found'})
        if type_ == 'tap':
            col = f'shop_tap{idx}'
            levels = [user[17], user[18], user[19], user[20], user[21]]
            cfg = [
                {'baseBonus': 0.0005, 'basePrice': 30, 'priceMult': 3.0},
                {'baseBonus': 0.0010, 'basePrice': 90, 'priceMult': 3.0},
                {'baseBonus': 0.0020, 'basePrice': 270, 'priceMult': 3.0},
                {'baseBonus': 0.0040, 'basePrice': 810, 'priceMult': 3.0},
                {'baseBonus': 0.0080, 'basePrice': 2430, 'priceMult': 3.0}
            ]
            level = levels[idx]
            price = int(cfg[idx]['basePrice'] * (cfg[idx]['priceMult'] ** level))
            if user[2] < price:
                return web.json_response({'success': False, 'error': 'Недостаточно средств'})
            update_balance(telegram_id, -price)
            cursor.execute(f"UPDATE users SET {col} = {col} + 1, per_tap = per_tap + ? WHERE telegram_id = ?",
                           (cfg[idx]['baseBonus'] * (2 ** level), telegram_id))
            conn.commit()
            user = get_user(telegram_id)
            return web.json_response({'success': True, 'new_balance': user[2], 'per_tap': user[3]})
        elif type_ == 'sec':
            col = f'shop_sec{idx}'
            levels = [user[22], user[23], user[24], user[25], user[26]]
            cfg = [
                {'baseBonus': 0.00025, 'basePrice': 36, 'priceMult': 3.0},
                {'baseBonus': 0.00050, 'basePrice': 108, 'priceMult': 3.0},
                {'baseBonus': 0.00100, 'basePrice': 324, 'priceMult': 3.0},
                {'baseBonus': 0.00200, 'basePrice': 972, 'priceMult': 3.0},
                {'baseBonus': 0.00400, 'basePrice': 2916, 'priceMult': 3.0}
            ]
            level = levels[idx]
            price = int(cfg[idx]['basePrice'] * (cfg[idx]['priceMult'] ** level))
            if user[2] < price:
                return web.json_response({'success': False, 'error': 'Недостаточно средств'})
            update_balance(telegram_id, -price)
            cursor.execute(f"UPDATE users SET {col} = {col} + 1, per_sec = per_sec + ? WHERE telegram_id = ?",
                           (cfg[idx]['baseBonus'] * (2 ** level), telegram_id))
            conn.commit()
            user = get_user(telegram_id)
            return web.json_response({'success': True, 'new_balance': user[2], 'per_sec': user[4]})

    if action == 'buy_boost':
        user = get_user(telegram_id)
        if not user:
            return web.json_response({'success': False, 'error': 'User not found'})
        if user[27] == 1:
            return web.json_response({'success': False, 'error': 'Бустер уже активен'})
        import time
        now = int(time.time())
        if user[28] > now:
            return web.json_response({'success': False, 'error': 'Бустер на перезарядке'})
        if user[2] < 300:
            return web.json_response({'success': False, 'error': 'Недостаточно средств'})
        update_balance(telegram_id, -300)
        cursor.execute("UPDATE users SET per_sec = per_sec * 2, boost_active = 1, boost_cooldown = ? WHERE telegram_id = ?",
                       (now + 3600, telegram_id))
        conn.commit()
        asyncio.create_task(disable_boost(telegram_id, user[4] * 2))
        user = get_user(telegram_id)
        return web.json_response({'success': True, 'new_balance': user[2], 'per_sec': user[4]})

    if action == 'activate_promo':
        code = data.get('code', '').upper()
        user = get_user(telegram_id)
        if not user:
            return web.json_response({'success': False, 'error': 'User not found'})
        if code == 'WELCOME' and user[29] == 0:
            update_balance(telegram_id, 100)
            cursor.execute("UPDATE users SET promo_used = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
            return web.json_response({'success': True, 'new_balance': user[2] + 100, 'bonus': 100})
        elif code == 'DILDO' and user[30] == 0:
            cursor.execute("UPDATE users SET dildo_activated = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
            return web.json_response({'success': True, 'bonus': 0, 'message': 'Дилдокликер активирован!'})
        else:
            return web.json_response({'success': False, 'error': 'Неверный или уже использованный код'})

    if action == 'set_theme':
        theme = data.get('theme', 'dark')
        cursor.execute("UPDATE users SET theme = ? WHERE telegram_id = ?", (theme, telegram_id))
        conn.commit()
        return web.json_response({'success': True})

    if action == 'set_sound':
        sound = 1 if data.get('sound_on') else 0
        cursor.execute("UPDATE users SET sound_on = ? WHERE telegram_id = ?", (sound, telegram_id))
        conn.commit()
        return web.json_response({'success': True})

    if action == 'lottery_bet' or action == 'start_miner' or action == 'miner_lose' or action == 'miner_win' or action == 'miner_cashout':
        return web.json_response({'success': False, 'error': 'Функция временно недоступна'})

    return web.json_response({'success': False, 'error': 'Unknown action'})

async def disable_boost(telegram_id, original_per_sec):
    await asyncio.sleep(300)
    cursor.execute("UPDATE users SET per_sec = ?, boost_active = 0 WHERE telegram_id = ?", (original_per_sec / 2, telegram_id))
    conn.commit()

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
