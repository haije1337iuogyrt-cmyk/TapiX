import asyncio, hashlib, json, os, secrets, time
from datetime import datetime, timedelta, timezone
import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, Update
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
LOTTERY_MAX_BET = 1_000_000
LOTTERY_HOUSE_EDGE = 0.95
MINER_GRID = 25
MINER_MIN_BET = 100
MINER_MAX_BET = 1_000_000

# Магазин: фиксированный бонус, нормальные цены
SHOP_TAP = [
    {'bonus': 0.0001, 'price': 500,   'mult': 2.5, 'label': 'Коготь I'},
    {'bonus': 0.0002, 'price': 2000,  'mult': 2.5, 'label': 'Коготь II'},
    {'bonus': 0.0005, 'price': 8000,  'mult': 2.5, 'label': 'Коготь III'},
    {'bonus': 0.001,  'price': 30000, 'mult': 2.5, 'label': 'Коготь IV'},
    {'bonus': 0.002,  'price': 100000,'mult': 2.5, 'label': 'Коготь V'},
]
SHOP_SEC = [
    {'bonus': 0.00005, 'price': 300,   'mult': 2.5, 'label': 'Авто I'},
    {'bonus': 0.0001,  'price': 1500,  'mult': 2.5, 'label': 'Авто II'},
    {'bonus': 0.0002,  'price': 6000,  'mult': 2.5, 'label': 'Авто III'},
    {'bonus': 0.0005,  'price': 25000, 'mult': 2.5, 'label': 'Авто IV'},
    {'bonus': 0.001,   'price': 80000, 'mult': 2.5, 'label': 'Авто V'},
]

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
    shop_tap3 INT DEFAULT 0, shop_tap4 INT DEFAULT 0,
    shop_sec0 INT DEFAULT 0, shop_sec1 INT DEFAULT 0, shop_sec2 INT DEFAULT 0,
    shop_sec3 INT DEFAULT 0, shop_sec4 INT DEFAULT 0,
    boost_active BOOLEAN DEFAULT FALSE,
    boost_cooldown TIMESTAMPTZ,
    promo_used BOOLEAN DEFAULT FALSE,
    dildo_activated BOOLEAN DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS transfers (
    id SERIAL PRIMARY KEY,
    from_telegram_id BIGINT, to_telegram_id BIGINT,
    amount DOUBLE PRECISION, fee DOUBLE PRECISION, total DOUBLE PRECISION,
    message TEXT, timestamp TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS lottery_rounds (
    id SERIAL PRIMARY KEY, seed TEXT, hash TEXT,
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
'''

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
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
    return min((1 / prob) * 0.97, 200.0) if prob > 0 else 1.0

# ===== ЛОТЕРЕЯ =====
lottery_state = {'round_id': None, 'phase': 'betting', 'phase_end': 0,
                 'hash': '', 'seed': '', 'result': None}
tap_limits = {}

def rate_ok(tid):
    now = time.time()
    b = tap_limits.setdefault(tid, [])
    b[:] = [t for t in b if now - t < 1]
    if len(b) >= 8: return False
    b.append(now)
    return True

async def lottery_loop():
    while True:
        try:
            seed = secrets.token_hex(16)
            h = hashlib.sha256(seed.encode()).hexdigest()
            async with pool.acquire() as conn:
                rid = await conn.fetchval(
                    "INSERT INTO lottery_rounds(seed,hash,start_time,status)"
                    " VALUES($1,$2,now(),'betting') RETURNING id", seed, h
                )
            lottery_state.update({'round_id': rid, 'phase': 'betting',
                'phase_end': time.time()+LOTTERY_BET_SECONDS, 'hash': h, 'seed': seed, 'result': None})
            await asyncio.sleep(LOTTERY_BET_SECONDS)
            lottery_state['phase'] = 'resolving'
            lottery_state['phase_end'] = time.time() + LOTTERY_REVEAL_SECONDS
            result = 'eagle' if int(h, 16) % 2 == 0 else 'tails'
            lottery_state['result'] = result
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT telegram_id, side, SUM(amount) amt FROM lottery_bets"
                    " WHERE round_id=$1 GROUP BY telegram_id, side", rid
                )
                bank = sum(r['amt'] for r in rows)
                winners = [r for r in rows if r['side'] == result]
                win_total = sum(r['amt'] for r in winners)
                async with conn.transaction():
                    for r in winners:
                        share = (r['amt']/win_total)*bank*LOTTERY_HOUSE_EDGE
                        await update_balance_and_xp(conn, r['telegram_id'], share)
                        await conn.execute(
                            "INSERT INTO game_history(telegram_id,game,result,amount)"
                            " VALUES($1,'lottery','win',$2)", r['telegram_id'], share
                        )
                    for r in [x for x in rows if x['side'] != result]:
                        await conn.execute(
                            "INSERT INTO game_history(telegram_id,game,result,amount)"
                            " VALUES($1,'lottery','lose',$2)", r['telegram_id'], -r['amt']
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
        await asyncio.sleep(1)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET balance=balance+per_sec,"
                    "total_earned=CASE WHEN per_sec>0 THEN total_earned+per_sec ELSE total_earned END"
                    " WHERE per_sec>0"
                )
        except Exception as e:
            print(f"passive_income error: {e}")

async def keep_alive_loop():
    import aiohttp
    await asyncio.sleep(60)
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(WEBAPP_URL, timeout=aiohttp.ClientTimeout(total=10))
        except Exception: pass
        await asyncio.sleep(600)

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

# ===== /start =====
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
    if is_new and ref:
        async with pool.acquire() as conn:
            ref_row = await conn.fetchrow("SELECT telegram_id FROM users WHERE id=$1", ref)
        if ref_row:
            async with pool.acquire() as conn:
                await update_balance_and_xp(conn, ref_row['telegram_id'], 50)
            try:
                await bot.send_message(ref_row['telegram_id'], "🎉 +50 ₿ — твой друг присоединился!")
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
        await cb.message.answer(f"👥 Твоя ссылка:\n{link}\n\n+50 ₿ за каждого друга!")
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
        'referral_earned': ref_count * 50,
        'shop_tap': [u[f'shop_tap{i}'] for i in range(5)],
        'shop_sec': [u[f'shop_sec{i}'] for i in range(5)],
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
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT telegram_id,username,first_name,avatar_url,balance,per_tap,per_sec,total_taps,level"
                " FROM users ORDER BY balance DESC LIMIT 50"
            )
        return web.json_response({'success': True, 'top': [{
            'telegram_id': r['telegram_id'],
            'username': r['username'],
            'first_name': r['first_name'],
            'avatar': r['avatar_url'],
            'balance': float(r['balance']),
            'per_sec': float(r['per_sec']),
            'total_taps': int(r['total_taps']),
            'level': int(r['level']),
        } for r in rows]})

    # ===== SHOP =====
    if action == 'buy_shop':
        type_ = data.get('type')
        idx = data.get('idx')
        if type_ not in ('tap','sec') or not isinstance(idx,int) or not 0<=idx<=4:
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
        return web.json_response({
            'success': True, 'round_id': lottery_state['round_id'],
            'phase': lottery_state['phase'],
            'time_left': max(0, lottery_state['phase_end']-time.time()),
            'hash': lottery_state['hash'],
            'seed': lottery_state['seed'] if lottery_state['phase']=='resolving' else None,
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
        async with pool.acquire() as conn:
            s = await conn.fetchrow("SELECT * FROM miner_sessions WHERE telegram_id=$1", tid)
        if not s:
            return web.json_response({'success': False, 'error': 'Нет активной игры — начни новую'})
        try: index = int(data.get('index'))
        except: return web.json_response({'success': False, 'error': 'Некорректная ячейка'})
        if not 0 <= index < MINER_GRID:
            return web.json_response({'success': False, 'error': 'Ячейка вне поля'})
        bomb_pos = set(json.loads(s['bomb_positions']))
        if index in bomb_pos:
            async with pool.acquire() as conn:
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
        async with pool.acquire() as conn:
            await conn.execute("UPDATE miner_sessions SET opened=$1 WHERE telegram_id=$2", opened, tid)
        complete = opened >= total_gems
        if complete:
            win = float(s['bet']) * coef
            async with pool.acquire() as conn:
                new_bal = await update_balance_and_xp(conn, tid, win)
                await conn.execute("DELETE FROM miner_sessions WHERE telegram_id=$1", tid)
                await conn.execute(
                    "INSERT INTO game_history(telegram_id,game,result,amount) VALUES($1,'miner','win',$2)",
                    tid, win
                )
            return web.json_response({'success': True, 'result': 'gem', 'coef': coef,
                                      'complete': True, 'win_amount': win, 'new_balance': new_bal})
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
    app = web.Application()
    app.router.add_post('/webhook', webhook_handler)
    app.router.add_post('/api', api_handler)
    app.router.add_get('/reset', reset_handler)
    app.router.add_get('/', index_handler)
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', port).start()
    print(f"✅ Запущен на порту {port}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
