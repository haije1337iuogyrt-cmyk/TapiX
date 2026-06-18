import asyncio
import sqlite3
import random
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiohttp import web

BOT_TOKEN = "8913794504:AAGqTwhUYZ4QDAUbUOw6SGVclNKzkvDGKOY"  # <--- НОВЫЙ ТОКЕН
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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
''')
conn.commit()

def get_user(tg_id):
    cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_id,))
    return cursor.fetchone()

def create_user(tg_id, username, ref_code=None):
    code = ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=6))
    cursor.execute('INSERT INTO users (telegram_id, username, referral_code, referred_by) VALUES (?, ?, ?, ?)', (tg_id, username, code, ref_code))
    conn.commit()
    return cursor.lastrowid

def update_balance(tg_id, amount):
    cursor.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (amount, tg_id))
    conn.commit()

@dp.message(Command("start"))
async def start(message: types.Message):
    user = get_user(message.from_user.id)
    if not user:
        ref = None
        if len(message.text.split()) > 1:
            ref_code = message.text.split()[1]
            cursor.execute("SELECT id FROM users WHERE referral_code = ?", (ref_code,))
            ref_user = cursor.fetchone()
            if ref_user:
                ref = ref_user[0]
                create_user(message.from_user.id, message.from_user.username, ref)
                update_balance(ref, 50)
                await bot.send_message(ref, "🎉 +50 коинов за приглашение!")
            else:
                create_user(message.from_user.id, message.from_user.username)
        else:
            create_user(message.from_user.id, message.from_user.username)

    # Используем новый URL (он будет такой же, как у старого, но можно поменять потом)
    WEBAPP_URL = "https://tapix1.onrender.com"  # Если создашь новый сервис с другим именем — поменяешь
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Играть", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(text="👥 Рефералка", callback_data="referral")],
        [InlineKeyboardButton(text="🏆 Топ", callback_data="top")]
    ])
    await message.answer("🐱 Добро пожаловать в TapiX!", reply_markup=keyboard)

@dp.callback_query(F.data == "referral")
async def show_referral(callback: types.CallbackQuery):
    user = get_user(callback.from_user.id)
    if user:
        code = user[6]
        await callback.message.answer(f"👥 Твоя ссылка: https://t.me/taplx_bot?start={code}")
    await callback.answer()

@dp.callback_query(F.data == "top")
async def show_top(callback: types.CallbackQuery):
    cursor.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10")
    top = cursor.fetchall()
    text = "🏆 ТОП:\n"
    for i, (name, bal) in enumerate(top, 1):
        text += f"{i}. @{name or 'Аноним'} — {bal:.2f} ₿\n"
    await callback.message.answer(text)
    await callback.answer()

# ===== API =====
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
                'xp_max': user[9]
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

        cursor.execute("SELECT id FROM users WHERE username = ?", (to_username,))
        to_user = cursor.fetchone()
        if not to_user:
            return web.json_response({'success': False, 'error': 'Пользователь не найден'})

        fee = amount * 0.05
        total = amount + fee

        cursor.execute("SELECT balance FROM users WHERE telegram_id = ?", (from_user,))
        balance = cursor.fetchone()
        if not balance or balance[0] < total:
            return web.json_response({'success': False, 'error': 'Недостаточно средств'})

        update_balance(from_user, -total)
        update_balance(to_user[0], amount)

        cursor.execute('''
            INSERT INTO transfers (from_user, to_user, amount, fee, total)
            VALUES (?, ?, ?, ?, ?)
        ''', (from_user, to_user[0], amount, fee, total))
        conn.commit()

        return web.json_response({'success': True, 'new_balance': balance[0] - total})

    if action == 'get_top':
        cursor.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 50")
        top = cursor.fetchall()
        return web.json_response({
            'success': True,
            'top': [{'username': u[0], 'balance': u[1]} for u in top]
        })

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
    update = types.Update(**data)
    await dp.feed_update(bot, update)
    return web.Response(status=200)

# ===== ЗАПУСК =====
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
