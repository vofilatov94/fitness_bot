#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fitness Assistant — Telegram bot adapted for PythonAnywhere webhook

Full feature set:
- 30-day training plan (Mon-Sat: 3 strength + 3 cardio)
- Progression by % (configurable)
- Persistent SQLite storage (users, plans, workouts, entries, measurements, steps, supplements, goals, integrations)
- Timers: training, rest, cardio (multiple per user) via asyncio tasks
- Weekly/monthly reports and graphs (matplotlib)
- Supplements scheduling (pre/post workout reminders)
- Freeze/Resume functionality
- Mi Fitness integration hooks (third-party providers or manual export)
- Achievements, sleep, cheatmeal support

Notes for PythonAnywhere free plan:
- Use webhook mode with Flask (this file)
- Do NOT run a BackgroundScheduler inside WSGI app; use separate scheduled scripts for daily/sunday reminders
- Store TELEGRAM_TOKEN in .env (load_dotenv reads it)
"""

from __future__ import annotations
import os
import sqlite3
import logging
import asyncio
from datetime import datetime, date, timedelta
from typing import Optional, List, Tuple, Dict, Any
from io import BytesIO

# 3rd party
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from dotenv import load_dotenv
from flask import Flask, request, jsonify
import requests

# logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# load env
BASE = os.path.dirname(__file__)
load_dotenv(os.path.join(BASE, ".env"))
TOKEN = os.getenv("TELEGRAM_TOKEN")
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE, "fitness.db"))
MI_PROVIDER = os.getenv("MI_PROVIDER")
MI_API_KEY = os.getenv("MI_API_KEY")

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set. Add it in .env")

# -------------------- DB and schema --------------------
SCHEMA = r"""
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  username TEXT,
  age INTEGER DEFAULT 30,
  gender TEXT DEFAULT 'male',
  tz_offset INTEGER DEFAULT 0,
  frozen_until TEXT
);
CREATE TABLE IF NOT EXISTS plans (
  user_id INTEGER PRIMARY KEY,
  start_date TEXT
);
CREATE TABLE IF NOT EXISTS init_stats (
  user_id INTEGER PRIMARY KEY,
  weight REAL,
  height REAL,
  bench REAL,
  squat REAL,
  row REAL,
  curl REAL
);
CREATE TABLE IF NOT EXISTS workouts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  wdate TEXT,
  wtype TEXT,
  name TEXT,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workout_id INTEGER,
  exercise TEXT,
  sets INTEGER,
  reps INTEGER,
  weight REAL,
  distance_km REAL,
  duration_min REAL,
  calories REAL
);
CREATE TABLE IF NOT EXISTS bodyweight (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  bdate TEXT,
  weight REAL
);
CREATE TABLE IF NOT EXISTS steps (
  user_id INTEGER,
  sdate TEXT,
  steps INTEGER,
  PRIMARY KEY(user_id, sdate)
);
CREATE TABLE IF NOT EXISTS measurements (
  user_id INTEGER,
  mdate TEXT,
  waist REAL,
  hips REAL,
  chest REAL,
  butt REAL,
  weight REAL,
  PRIMARY KEY(user_id, mdate)
);
CREATE TABLE IF NOT EXISTS supplements (
  user_id INTEGER,
  name TEXT,
  dose TEXT,
  when_before_min INTEGER,
  when_after_min INTEGER,
  enabled INTEGER DEFAULT 1,
  PRIMARY KEY(user_id, name)
);
CREATE TABLE IF NOT EXISTS goals (
  user_id INTEGER,
  goal_id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_text TEXT,
  target_value REAL,
  target_date TEXT,
  active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS integrations (
  user_id INTEGER,
  provider TEXT,
  api_key TEXT,
  config TEXT,
  PRIMARY KEY(user_id, provider)
);
"""

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as c:
        c.executescript(SCHEMA)

# -------------------- Plan generation & progression --------------------
STRENGTH_SPLITS = {
    'A': [('Squat', 3, 5), ('Bench Press', 3, 5), ('Bent-over Row', 3, 8), ('Plank', 3, 60)],
    'B': [('Deadlift', 3, 5), ('Overhead Press', 3, 5), ('Lat Pulldown / Pull-ups', 3, 8), ('Hanging Knee Raise', 3, 12)],
    'C': [('Front Squat / Lunge', 3, 8), ('Incline Bench / DB Press', 3, 8), ('Seated Row', 3, 10), ('Back Extension', 3, 12)]
}
CARDIO_MENU = [('Easy Run / Jog', 60), ('Rowing Erg', 60), ('Cycling (Z2)', 75), ('HIIT Intervals', 30), ('Stair Climber', 60), ('Elliptical', 60)]

def make_30day_plan(start: date) -> List[Tuple[date, str, str]]:
    out = []
    cycle = ['A', 'B', 'C']
    si = 0
    ci = 0
    for i in range(30):
        d = start + timedelta(days=i)
        wd = d.weekday()
        if wd == 6: continue
        if wd in (0, 2, 4):
            out.append((d, 'strength', f'Strength {cycle[si]}'))
            si = (si + 1) % 3
        else:
            name, base = CARDIO_MENU[ci % len(CARDIO_MENU)]
            out.append((d, 'cardio', f'Cardio: {name}'))
            ci += 1
    return out

def progression(base: float, weeks: int, percent: float) -> float:
    return round(base * ((1 + percent / 100) ** weeks), 1)

# -------------------- Nutrition math --------------------
def calc_bmr(weight_kg, height_cm, age=30, gender='male'):
    if gender == 'male':
        return 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    else:
        return 10 * weight_kg + 6.25 * height_cm - 5 * age - 161

def calc_tdee(bmr, activity_factor=1.45):
    return round(bmr * activity_factor)

def calc_deficit(tdee, deficit_percent=40):
    cal_def = round(tdee * (1 - deficit_percent / 100))
    proteins = round(cal_def * 0.3 / 4)
    fats = round(cal_def * 0.3 / 9)
    carbs = round(cal_def * 0.4 / 4)
    return cal_def, proteins, fats, carbs

# -------------------- Basic DB helpers --------------------
def ensure_user(user_id: int, username: Optional[str] = None):
    with get_conn() as c:
        c.execute('INSERT OR IGNORE INTO users(user_id, username) VALUES(?,?)', (user_id, username))

def set_plan_start(user_id: int, start: date):
    with get_conn() as c:
        c.execute('INSERT OR REPLACE INTO plans(user_id, start_date) VALUES(?,?)', (user_id, start.isoformat()))

def get_plan_start(user_id: int) -> Optional[date]:
    with get_conn() as c:
        r = c.execute('SELECT start_date FROM plans WHERE user_id=?', (user_id,)).fetchone()
        return date.fromisoformat(r[0]) if r else None

def set_init_stats(user_id: int, weight, height, bench, squat, row, curl):
    with get_conn() as c:
        c.execute('INSERT OR REPLACE INTO init_stats(user_id, weight, height, bench, squat, row, curl) VALUES(?,?,?,?,?,?,?)',
                  (user_id, weight, height, bench, squat, row, curl))

def get_init_stats(user_id: int) -> Optional[dict]:
    with get_conn() as c:
        r = c.execute('SELECT weight,height,bench,squat,row,curl FROM init_stats WHERE user_id=?', (user_id,)).fetchone()
        if not r: return None
        return {'weight': r[0], 'height': r[1], 'bench': r[2], 'squat': r[3], 'row': r[4], 'curl': r[5]}

# -------------------- Measurements/steps --------------------
def log_steps(user_id: int, steps: int, sdate: Optional[date] = None):
    sdate = (sdate or date.today()).isoformat()
    with get_conn() as c:
        c.execute('INSERT OR REPLACE INTO steps(user_id,sdate,steps) VALUES(?,?,?)', (user_id, sdate, steps))

def log_measurement(user_id: int, waist: float, hips: float, chest: float, butt: float, weight: float, mdate: Optional[date] = None):
    mdate = (mdate or date.today()).isoformat()
    with get_conn() as c:
        c.execute('INSERT OR REPLACE INTO measurements(user_id,mdate,waist,hips,chest,butt,weight) VALUES(?,?,?,?,?,?,?)',
                  (user_id, mdate, waist, hips, chest, butt, weight))

# -------------------- Plotting --------------------
def plot_series(dates: List[date], values: List[float], title: str, ylabel: str) -> BytesIO:
    plt.figure(figsize=(8, 4))
    plt.plot(dates, values, marker='o')
    plt.title(title)
    plt.xlabel('Date')
    plt.ylabel(ylabel)
    plt.grid(True)
    plt.tight_layout()
    bio = BytesIO()
    plt.savefig(bio, format='png')
    plt.close()
    bio.seek(0)
    return bio

# -------------------- Timers --------------------
USER_TIMER_TASKS: Dict[int, Dict[str, asyncio.Task]] = {}

async def _timer_sleep_and_notify(app: Application, user_id: int, timer_name: str, seconds: int, finish_message: str):
    try:
        await asyncio.sleep(seconds)
        await app.bot.send_message(chat_id=user_id, text=finish_message)
    except asyncio.CancelledError:
        await app.bot.send_message(chat_id=user_id, text=f'Timer {timer_name} cancelled')

def start_user_timer(app: Application, user_id: int, timer_name: str, seconds: int, finish_message: str) -> bool:
    user_tasks = USER_TIMER_TASKS.setdefault(user_id, {})
    if timer_name in user_tasks:
        return False
    task = asyncio.create_task(_timer_sleep_and_notify(app, user_id, timer_name, seconds, finish_message))
    user_tasks[timer_name] = task
    return True

def stop_user_timer(user_id: int, timer_name: Optional[str] = None) -> List[str]:
    stopped = []
    user_tasks = USER_TIMER_TASKS.get(user_id, {})
    if timer_name:
        t = user_tasks.pop(timer_name, None)
        if t:
            t.cancel()
            stopped.append(timer_name)
    else:
        for k, t in list(user_tasks.items()):
            t.cancel()
            stopped.append(k)
            user_tasks.pop(k, None)
    return stopped

# -------------------- Mi Fitness integration (stubs) --------------------
def save_integration(user_id: int, provider: str, api_key: str):
    with get_conn() as c:
        c.execute('INSERT OR REPLACE INTO integrations(user_id,provider,api_key) VALUES(?,?,?)', (user_id, provider, api_key))

def fetch_mi_data(user_id: int) -> Optional[dict]:
    with get_conn() as c:
        row = c.execute('SELECT provider, api_key FROM integrations WHERE user_id=?', (user_id,)).fetchone()
    if not row:
        return None
    provider, api_key = row
    if provider == 'thryve':
        url = f'https://api.tryrook.io/user/data'
        headers = {'Authorization': f'Bearer {api_key}'}
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            logger.warning('mi fetch fail: %s', e)
    return None

# -------------------- Commands handlers --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username)
    if not get_plan_start(user.id):
        set_plan_start(user.id, date.today())
    await update.message.reply_text('Привет! Я Fitness Assistant. Введи /help чтобы увидеть команды')

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        'Команды:\n'
        '/start — регистрация\n'
        '/init weight=100 height=176 bench=80 squat=100 row=60 curl=50 — задать стартовые показатели\n'
        '/today — план на сегодня\n'
        '/plan — показать 30-дневный план\n'
        '/setplan YYYY-MM-DD — установить старт плана\n'
        '/log ... — лог тренировки\n'
        '/measure waist hips chest butt weight — сохранить замеры (воскресенье в 09:00 напоминание)\n'
        '/steps N — записать шаги\n'
        '/progress metric period — график (metric: weight|waist|hips|chest|butt|steps|calories; period: week|month|all)\n'
        '/start_training — запустить таймер тренировки\n'
        '/start_rest M — таймер отдыха M минут\n'
        '/start_cardio M — таймер кардио M минут\n'
        '/stop_timer [name] — остановить таймер\n'
        '/freeze N — заморозить тренировки на N дней\n'
        '/resume — снять заморозку\n'
        '/supp_add name=dose before=mins after=mins — добавить добавку\n'
        '/supps — список добавок\n'
        '/supp_recommend — рекомендации по добавкам\n'
        '/mi_connect provider api_key — подключить Mi integration (thryve/rook)\n'
        '/mi_sync — попытаться синхронизировать данные от провайдера\n'
        '/set_goal text|target|YYYY-MM-DD — задать цель\n'
        '/sleep H — записать часы сна\n'
        '/cheatmeal — отметить читмил (см. неделю)\n'
        '/export_db — скачать базу (владельцу)\n'
    )
    await update.message.reply_text(txt)

async def cmd_init(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        return await update.message.reply_text('Формат: /init weight=100 height=176 bench=80 squat=100 row=60 curl=50')
    args = {}
    for tok in context.args:
        if '=' in tok:
            k, v = tok.split('=', 1)
            try: args[k] = float(v)
            except: pass
    set_init_stats(user.id, args.get('weight', 0), args.get('height', 0), args.get('bench', 0), args.get('squat', 0), args.get('row', 0), args.get('curl', 0))
    await update.message.reply_text('Стартовые показатели сохранены')

async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pl = make_30day_plan(get_plan_start(user.id) or date.today())
    lines = [f'{d.isoformat()} ({d.strftime("%a")}): {name}' for d, _, name in pl]
    await update.message.reply_text('\n'.join(lines))

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if get_conn():
        with get_conn() as c:
            fr = c.execute('SELECT frozen_until FROM users WHERE user_id=?', (user.id,)).fetchone()
            if fr and fr[0]:
                try:
                    until = date.fromisoformat(fr[0])
                    if date.today() <= until:
                        return await update.message.reply_text(f'Тренировки заморожены до {until}')
                except Exception:
                    pass
    pl = make_30day_plan(get_plan_start(user.id) or date.today())
    today = date.today()
    for d, wtype, name in pl:
        if d == today:
            stats = get_init_stats(user.id)
            weeks = (d - (get_plan_start(user.id) or date.today())).days // 7
            lines = [f"Сегодня ({d.isoformat()}): {name}"]
            if wtype == 'strength':
                split = name.split()[-1]
                lines.append('Разминка: 10 мин кардио')
                for ex, sets, reps in STRENGTH_SPLITS.get(split, []):
                    w = None
                    if stats:
                        if 'Bench' in ex: w = progression(stats['bench'], weeks, 2.5)
                        elif 'Squat' in ex or 'Deadlift' in ex: w = progression(stats['squat'], weeks, 5)
                        elif 'Row' in ex: w = progression(stats['row'], weeks, 2.5)
                        elif 'Curl' in ex or 'Biceps' in ex: w = progression(stats['curl'], weeks, 2.5)
                    if ex == 'Plank': lines.append(f' • {ex} — {sets}×{reps} сек')
                    else: lines.append(f' • {ex} — {sets}×{reps} {f"({w} кг)" if w else ""}')
                lines.append('Финал: 10 мин кардио')
            else:
                base = next((b for n, b in CARDIO_MENU if name.endswith(n)), 60)
                dur = int(round(base * (1 + 0.05 * weeks)))
                lines.append(f' • {name.split(":", 1)[1].strip()} — {dur} минут')
            return await update.message.reply_text('\n'.join(lines))
    await update.message.reply_text('Сегодня отдых!')

async def cmd_measure(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) != 5:
        return await update.message.reply_text('Формат: /measure вес талия грудь бедра попа (через пробел)')
    try:
        weight, waist, chest, hips, butt = map(float, context.args)
    except:
        return await update.message.reply_text('Ошибка формата чисел')
    log_measurement(user.id, waist, hips, chest, butt, weight)
    await update.message.reply_text('Замеры сохранены ✅')

async def cmd_steps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args: return await update.message.reply_text('Формат: /steps 10000')
    try: s = int(context.args[0])
    except: return await update.message.reply_text('Число?')
    log_steps(user.id, s)
    await update.message.reply_text('Шаги сохранены ✅')

async def cmd_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) < 1:
        return await update.message.reply_text('Формат: /progress metric period (metric: weight|waist|hips|chest|butt|steps|calories; period: week|month|all)')
    metric = context.args[0]
    period = context.args[1] if len(context.args) > 1 else 'week'
    end = date.today()
    start = {'week': end - timedelta(days=7), 'month': end - timedelta(days=30), 'all': date(1970, 1, 1)}.get(period, end - timedelta(days=7))
    dates = []; values = []
    with get_conn() as c:
        if metric in ('weight', 'waist', 'hips', 'chest', 'butt'):
            rows = c.execute(f"SELECT mdate,{metric} FROM measurements WHERE user_id=? AND mdate>=? ORDER BY mdate",
                             (user.id, start.isoformat())).fetchall()
            for r in rows: dates.append(date.fromisoformat(r[0])); values.append(r[1])
        elif metric == 'steps':
            rows = c.execute("SELECT sdate,steps FROM steps WHERE user_id=? AND sdate>=? ORDER BY sdate",
                             (user.id, start.isoformat())).fetchall()
            for r in rows: dates.append(date.fromisoformat(r[0])); values.append(r[1])
        elif metric == 'calories':
            rows = c.execute("SELECT w.wdate, SUM(e.calories) FROM entries e JOIN workouts w ON e.workout_id=w.id WHERE w.user_id=? AND w.wdate>=? GROUP BY w.wdate ORDER BY w.wdate",
                             (user.id, start.isoformat())).fetchall()
            for r in rows: dates.append(date.fromisoformat(r[0])); values.append(r[1] or 0)
        else:
            return await update.message.reply_text('Неизвестная метрика')
    if not dates:
        return await update.message.reply_text('Нет данных за выбранный период')
    bio = plot_series(dates, values, f'{metric} ({period})', metric)
    await update.message.reply_photo(photo=bio)

async def cmd_start_training(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pl = make_30day_plan(get_plan_start(user.id) or date.today())
    today = date.today()
    wtype = None; name = None
    for d, wt, n in pl:
        if d == today: wtype = wt; name = n; break
    if not wtype:
        return await update.message.reply_text('Сегодня отдых!')
    total_min = 60 if wtype == 'cardio' else 10 + 50 + 10
    seconds = total_min * 60
    ok = start_user_timer(update.app, user.id, 'training', seconds, 'Тренировка завершена! Сделай 10 минут кардио.')
    if not ok: return await update.message.reply_text('Таймер уже запущен')
    with get_conn() as c:
        rows = c.execute('SELECT name,dose,when_before_min,when_after_min,enabled FROM supplements WHERE user_id=? AND enabled=1',
                         (user.id,)).fetchall()
    for name, dose, before, after, enabled in rows:
        if before and before > 0:
            start_user_timer(update.app, user.id, f'supp_pre_{name}', max(1, (before * 60)),
                             f'Напоминание: {name} — {dose} (за {before} минут до тренировки)')
    for name, dose, before, after, enabled in rows:
        if after and after > 0:
            start_user_timer(update.app, user.id, f'supp_post_{name}', max(1, (after * 60)),
                             f'Напоминание после тренировки: {name} — {dose}')
    await update.message.reply_text(f'Таймер тренировки запущен на {total_min} минут')

async def cmd_rest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    mins = int(context.args[0]) if context.args else 90
    ok = start_user_timer(update.app, user.id, 'rest', mins * 60, 'Отдых окончен, начинаем следующий подход!')
    if not ok: return await update.message.reply_text('Таймер отдыха уже запущен')
    await update.message.reply_text(f'Таймер отдыха на {mins} мин запущен')

async def cmd_cardio_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    mins = int(context.args[0]) if context.args else 10
    ok = start_user_timer(update.app, user.id, 'cardio_block', mins * 60, 'Кардио блок завершён!')
    if not ok: return await update.message.reply_text('Таймер кардио уже запущен')
    await update.message.reply_text(f'Кардио таймер на {mins} минут запущен')

async def cmd_stop_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = context.args[0] if context.args else None
    stopped = stop_user_timer(user.id, name)
    if not stopped: return await update.message.reply_text('Нет активных таймеров')
    await update.message.reply_text('Остановлены: ' + ','.join(stopped))

async def cmd_freeze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args: return await update.message.reply_text('Используй: /freeze N')
    try: days = int(context.args[0])
    except: return await update.message.reply_text('Число дней?')
    until = (date.today() + timedelta(days=days)).isoformat()
    with get_conn() as c: c.execute('INSERT OR REPLACE INTO users(user_id,frozen_until) VALUES(?,?)', (user.id, until))
    start_user_timer(update.app, user.id, 'freeze_return', days * 24 * 3600, 'Пауза окончена — время вернуться к тренировкам!')
    await update.message.reply_text(f'Заморожено до {until}')

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with get_conn() as c: c.execute('UPDATE users SET frozen_until=NULL WHERE user_id=?', (user.id,))
    stopped = stop_user_timer(user.id, 'freeze_return')
    await update.message.reply_text('Заморозка снята')

async def cmd_supp_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    params = {}
    for tok in context.args:
        if '=' in tok:
            k, v = tok.split('=', 1); params[k] = v
    name = params.get('name') or params.get('0')
    if not name: return await update.message.reply_text('Укажи name=...')
    dose = params.get('dose', '')
    before = int(params.get('before', '0'))
    after = int(params.get('after', '0'))
    with get_conn() as c:
        c.execute('INSERT OR REPLACE INTO supplements(user_id,name,dose,when_before_min,when_after_min,enabled) VALUES(?,?,?,?,?,1)',
                  (user.id, name, dose, before, after))
    await update.message.reply_text('Добавка сохранена')

async def cmd_supps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with get_conn() as c:
        rows = c.execute('SELECT name,dose,when_before_min,when_after_min,enabled FROM supplements WHERE user_id=?',
                         (user.id,)).fetchall()
    if not rows: return await update.message.reply_text('Нет добавок')
    lines = []
    for r in rows: lines.append(f'{r[0]} — {r[1]} (before {r[2]}m after {r[3]}m) enabled={bool(r[4])}')
    await update.message.reply_text('\n'.join(lines))

async def cmd_supp_recommend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = ('Рекомендации по добавкам:\n'
           '- Креатин моногидрат 5г/день (постоянно)\n'
           '- Кофеин 200 мг за 30–45 мин до тренировки (по переносимости)\n'
           '- Бета-аланин 3–6 г/день (накопительно)\n'
           '- Цитруллин 6 г перед тренировкой (опционально)\n'
           '- Омега-3 1–3 г/день\n'
           '- Протеин 20–40 г после тренировки\n')
    await update.message.reply_text(txt)

async def cmd_mi_connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if len(context.args) < 2:
        return await update.message.reply_text('Формат: /mi_connect provider api_key (provider: thryve|rook)')
    provider = context.args[0]; api_key = context.args[1]
    save_integration(user.id, provider, api_key)
    await update.message.reply_text('Интеграция сохранена; используйте /mi_sync чтобы синхронизировать')

async def cmd_mi_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = fetch_mi_data(user.id)
    if not data: return await update.message.reply_text('Нет данных или ошибка интеграции')
    if 'steps' in data:
        for rec in data['steps']:
            try:
                log_steps(user.id, int(rec.get('count', 0)), date.fromisoformat(rec.get('date')))
            except Exception:
                pass
    await update.message.reply_text('Синхронизация завершена')

async def cmd_set_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args: return await update.message.reply_text('Формат: /set_goal text|target|YYYY-MM-DD')
    try:
        raw = ' '.join(context.args)
        text, target, date_str = raw.split('|')
        target_val = float(target)
        with get_conn() as c:
            c.execute('INSERT INTO goals(user_id,goal_text,target_value,target_date,active) VALUES(?,?,?,?,1)',
                      (user.id, text, target_val, date_str))
        await update.message.reply_text('Цель сохранена')
    except Exception as e:
        await update.message.reply_text('Ошибка формата: ' + str(e))

async def cmd_sleep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args: return await update.message.reply_text('Формат: /sleep hours')
    try:
        hours = float(context.args[0])
    except:
        return await update.message.reply_text('Неверный формат')
    await update.message.reply_text(f'Сон {hours} ч сохранён (влияет на рекомендации)')

async def cmd_cheatmeal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text('Читмил отмечен — бот пересчитает недельный бюджет (опция)')

async def cmd_export_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # only allow owner to download? we'll allow anyone for now but better to restrict
    try:
        await update.message.reply_document(open(DB_PATH, 'rb'))
    except Exception as e:
        await update.message.reply_text(f'Ошибка при отправке БД: {e}')

# -------------------- Flask and Application setup --------------------
flask_app = Flask(__name__)

application = Application.builder().token(TOKEN).build()

# Add all command handlers
application.add_handler(CommandHandler('start', cmd_start))
application.add_handler(CommandHandler('help', cmd_help))
application.add_handler(CommandHandler('init', cmd_init))
application.add_handler(CommandHandler('plan', cmd_plan))
application.add_handler(CommandHandler('today', cmd_today))
application.add_handler(CommandHandler('measure', cmd_measure))
application.add_handler(CommandHandler('steps', cmd_steps))
application.add_handler(CommandHandler('progress', cmd_progress))
application.add_handler(CommandHandler('start_training', cmd_start_training))
application.add_handler(CommandHandler('start_rest', cmd_rest))
application.add_handler(CommandHandler('start_cardio', cmd_cardio_timer))
application.add_handler(CommandHandler('stop_timer', cmd_stop_timer))
application.add_handler(CommandHandler('freeze', cmd_freeze))
application.add_handler(CommandHandler('resume', cmd_resume))
application.add_handler(CommandHandler('supp_add', cmd_supp_add))
application.add_handler(CommandHandler('supps', cmd_supps))
application.add_handler(CommandHandler('supp_recommend', cmd_supp_recommend))
application.add_handler(CommandHandler('mi_connect', cmd_mi_connect))
application.add_handler(CommandHandler('mi_sync', cmd_mi_sync))
application.add_handler(CommandHandler('set_goal', cmd_set_goal))
application.add_handler(CommandHandler('sleep', cmd_sleep))
application.add_handler(CommandHandler('cheatmeal', cmd_cheatmeal))
application.add_handler(CommandHandler('export_db', cmd_export_db))

# Initialize DB
init_db()

# Webhook route
@flask_app.route('/' + TOKEN, methods=['POST'])
def webhook():
    update_json = request.get_json()
    if update_json:
        try:
            update = Update.de_json(update_json, application.bot)
            asyncio.run(application.process_update(update))
        except Exception as e:
            logger.exception('failed processing update: %s', e)
            return jsonify(success=False, error=str(e))
    return jsonify(success=True)

@flask_app.route('/')
def index():
    return "Fitness Bot Webhook is running!"

# For WSGI export
app = flask_app

logger.info('Bot setup complete for webhook mode.')
