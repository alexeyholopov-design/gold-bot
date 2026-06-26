import os
import time
import threading
import random
import asyncio
import requests
import pandas as pd
import numpy as np
import uuid
import json
import feedparser
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from datetime import time as dt_time, datetime, timedelta, timezone
from collections import defaultdict
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------- Конфигурация ----------
TOKEN = os.environ.get('TOKEN')
if not TOKEN:
    raise ValueError("TOKEN environment variable not set")

CHANNEL_ID = os.environ.get('CHANNEL_ID')

GIGACHAT_AUTH_KEY = os.environ.get('GIGACHAT_AUTH_KEY')
GIGACHAT_SCOPE = "GIGACHAT_API_PERS"

# Московское время
MSK = timezone(timedelta(hours=3))

# Flask для health-check
app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    return "Bot is running!", 200

def run_flask():
    app_flask.run(host='0.0.0.0', port=10000)

chat_id = None
signal_history = []           # История всех закрытых/отработанных сигналов для отчётов
active_signals = {}           # {asset_name: {tf: [signal_dict, ...]}}
gigachat_token = None
gigachat_token_expires = 0

news_sentiment = {}
LAST_NEWS_UPDATE = 0
NEWS_UPDATE_INTERVAL = 3600

# Таймфреймы для каждого актива
ASSET_TIMEFRAMES = {
    "GOLD": ["5m", "15m"],
    "BTC":  ["15m", "1h"],
    "ETH":  ["15m", "1h"],
    "SOL":  ["15m", "1h"],
}

ASSETS = {
    "GOLD": {"symbol": "XAUT-USDT"},
    "BTC":  {"symbol": "BTC-USDT"},
    "ETH":  {"symbol": "ETH-USDT"},
    "SOL":  {"symbol": "SOL-USDT"},
}

# Инициализируем структуру для активных сигналов
for name, asset in ASSETS.items():
    active_signals[name] = {}
    for tf in ASSET_TIMEFRAMES[name]:
        active_signals[name][tf] = []

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
LOOKBACK = 50
BARS_FOR_LEVELS = 10   # больше не используется для расчета целей, но оставлен для совместимости
EMA_FAST = 20
EMA_SLOW = 50
EMA_FAST_FAST = 3
EMA_SLOW_FAST = 10

# Множители ATR в зависимости от таймфрейма
ATR_MULTIPLIERS = {
    "5m":  {"SL": 1.2, "TP1": 1.5, "TP2": 2.0, "TP3": 3.0},
    "15m": {"SL": 1.5, "TP1": 2.0, "TP2": 3.0, "TP3": 5.0},
    "1h":  {"SL": 2.0, "TP1": 3.0, "TP2": 5.0, "TP3": 8.0},
}

# Уровни, используемые при отсутствии ATR (запасной вариант)
FALLBACK_LEVELS = {
    "5m":  {"SL": 0.5, "TP1": 0.8, "TP2": 1.2, "TP3": 2.0},
    "15m": {"SL": 0.8, "TP1": 1.5, "TP2": 2.5, "TP3": 4.0},
    "1h":  {"SL": 1.5, "TP1": 3.0, "TP2": 5.0, "TP3": 8.0},
}

# ---------- Утилиты ----------
def safe_format(value, format_spec=":.2f"):
    try:
        if value is None:
            return "0.00"
        num = float(value)
        if np.isnan(num) or not np.isfinite(num):
            return "0.00"
        return f"{num:{format_spec}}"
    except (ValueError, TypeError):
        return str(value)

def get_signal_stars(signal_type):
    stars = {"rsi": "⭐⭐", "ema": "⭐⭐", "combined": "⭐⭐⭐", "fast_ema": "⭐"}
    return stars.get(signal_type, "")

# ---------- GigaChat ----------
async def get_gigachat_token(force=False):
    global gigachat_token, gigachat_token_expires
    if not force and gigachat_token and time.time() < gigachat_token_expires:
        return gigachat_token
    if not GIGACHAT_AUTH_KEY:
        print("❌ GIGACHAT_AUTH_KEY не задан")
        return None
    try:
        url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        headers = {
            "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded"
        }
        data = {"scope": GIGACHAT_SCOPE}
        response = requests.post(url, headers=headers, data=data, verify=False, timeout=10)
        if response.status_code == 200:
            result = response.json()
            gigachat_token = result.get("access_token")
            expires_at = result.get("expires_at", time.time() + 1800)
            gigachat_token_expires = expires_at - 60
            print("✅ Токен GigaChat получен")
            return gigachat_token
        else:
            print(f"❌ Ошибка получения токена GigaChat: {response.status_code} {response.text}")
            return None
    except Exception as e:
        print(f"❌ Исключение при получении токена GigaChat: {e}")
        return None

async def ask_gigachat(prompt):
    token = await get_gigachat_token()
    if not token:
        return None
    try:
        url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        payload = {
            "model": "GigaChat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 500,
            "stream": False
        }
        response = requests.post(url, headers=headers, json=payload, verify=False, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"]["content"].strip()
        elif response.status_code == 401:
            print("⚠️ Токен GigaChat истёк, обновляем...")
            new_token = await get_gigachat_token(force=True)
            if new_token:
                headers["Authorization"] = f"Bearer {new_token}"
                response = requests.post(url, headers=headers, json=payload, verify=False, timeout=15)
                if response.status_code == 200:
                    data = response.json()
                    if "choices" in data and len(data["choices"]) > 0:
                        return data["choices"][0]["message"]["content"].strip()
        print(f"❌ Ошибка GigaChat API: {response.status_code}")
        return None
    except Exception as e:
        print(f"❌ Исключение при запросе к GigaChat: {e}")
        return None

# ---------- Новости ----------
def fetch_news(asset):
    rss_urls = {
        "GOLD": "https://ru.investing.com/rss/news_295.rss",
        "BTC": "https://cointelegraph.com/rss",
        "ETH": "https://cointelegraph.com/rss",
        "SOL": "https://cointelegraph.com/rss",
    }
    url = rss_urls.get(asset)
    if not url:
        return ""
    try:
        feed = feedparser.parse(url)
        entries = feed.entries[:10]
        titles = [entry.title for entry in entries if hasattr(entry, 'title')]
        return " ".join(titles) if titles else ""
    except Exception as e:
        print(f"❌ Ошибка RSS для {asset}: {e}")
        return ""

async def analyze_news_with_gigachat(asset, news_text):
    if not news_text:
        return "Новостей нет."
    prompt = f"""
Проанализируй новости по активу {asset} за последние часы. Новости:
{news_text}

Дай краткую оценку (1–2 предложения):
- общее настроение (бычье/медвежье/нейтральное)
- ключевые события
- влияние на цену в ближайшие часы
"""
    return await ask_gigachat(prompt)

async def update_news_sentiment(context: ContextTypes.DEFAULT_TYPE):
    global news_sentiment
    try:
        print("📰 Обновление новостного фона...")
        for asset in ASSETS:
            news_text = fetch_news(asset)
            if news_text:
                analysis = await analyze_news_with_gigachat(asset, news_text)
                news_sentiment[asset] = analysis
                print(f"📰 {asset}: {analysis[:100]}...")
            else:
                news_sentiment[asset] = "Новостей не найдено."
        print("✅ Новостной фон обновлён")
    except Exception as e:
        print(f"❌ Ошибка в update_news_sentiment: {e}")

# ---------- AI-анализ и проверка целей ----------
async def get_ai_analysis(asset_name, signal_type, signal, price, rsi, ema_fast=None, ema_slow=None,
                          atr=None, volume=None, higher_trend=None):
    if not GIGACHAT_AUTH_KEY:
        return None
    direction = "покупку" if signal == "BUY" else "продажу"
    price_str = safe_format(price)
    rsi_str = safe_format(rsi, ":.1f") if rsi is not None else "N/A"
    ema_text = ""
    if ema_fast is not None and ema_slow is not None:
        ema_text = f"EMA{EMA_FAST}: {safe_format(ema_fast)}, EMA{EMA_SLOW}: {safe_format(ema_slow)}"
    atr_str = safe_format(atr) if atr else ""
    volume_str = safe_format(volume, ":.0f") if volume else ""
    trend_text = f"Тренд на старшем ТФ: {higher_trend}" if higher_trend else ""
    news_text = news_sentiment.get(asset_name, "Новостной фон не оценён.")

    prompt = f"""
Ты – опытный трейдер по золоту и криптовалютам. Оцени сигнал и учти новостной фон.

Актив: {asset_name}
Тип сигнала: {signal_type} (сигнал на {direction})
Цена: ${price_str}
RSI (14): {rsi_str}
{ema_text}
{atr_str}
{volume_str}
{trend_text}
Новостной фон (последние часы): {news_text}

Ответь кратко, строго в формате:
1. Оценка ситуации (одно предложение).
2. Риск (одно предложение).
3. Рекомендация: BUY/SELL/HOLD с пояснением.
"""
    try:
        return await ask_gigachat(prompt)
    except Exception as e:
        print(f"❌ Ошибка AI: {e}")
        return None

async def validate_levels_with_ai(asset_name, tf, signal_type, price, levels):
    """Проверка адекватности рассчитанных целей через GigaChat (только для старших ТФ)"""
    if tf not in ["1h", "15m"]:  # для примера проверяем 1h и 15m
        return levels, False
    if not GIGACHAT_AUTH_KEY:
        return levels, False
    prompt = f"""
Ты – риск-менеджер. Проверь уровни для сделки.
Актив: {asset_name}
Таймфрейм: {tf}
Тип: {signal_type}
Цена входа: {price}
Стоп-лосс: {levels['sl']}
TP1: {levels['tp1']}
TP2: {levels['tp2']}
TP3: {levels.get('tp3', 'N/A')}
ATR: {levels.get('atr', 'N/A')}

Слишком ли узкие/широкие стопы и цели для данного таймфрейма? Если нужно скорректировать, предложи новые значения SL, TP1, TP2, TP3, сохраняя соотношение риска и реалистичность. Ответь строго в JSON формате:
{{"sl": число, "tp1": число, "tp2": число, "tp3": число, "comment": "пояснение"}}
Если корректировка не нужна, напиши {{"no_change": true}}.
"""
    try:
        resp = await ask_gigachat(prompt)
        if not resp:
            return levels, False
        resp = resp.strip()
        if "no_change" in resp:
            return levels, False
        # Парсим JSON (упрощённо, без обработки ошибок, но в реальном коде нужен try)
        new_levels = json.loads(resp)
        for key in ['sl', 'tp1', 'tp2', 'tp3']:
            if key in new_levels and isinstance(new_levels[key], (int, float)):
                levels[key] = round(new_levels[key], 2)
        print(f"🔧 AI скорректировал уровни: {levels}")
        return levels, True
    except Exception as e:
        print(f"❌ Ошибка валидации уровней AI: {e}")
        return levels, False

# ---------- Рыночные данные ----------
def get_current_price(symbol):
    try:
        url = "https://open-api.bingx.com/openApi/swap/v2/quote/price"
        params = {"symbol": symbol}
        response = requests.get(url, params=params, timeout=5)
        if response.status_code != 200:
            return None
        data = response.json()
        if data.get("code") == 0:
            return float(data["data"]["price"])
        return None
    except:
        return None

def get_klines(symbol, interval, limit=100):
    try:
        url = "https://open-api.bingx.com/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        response = requests.get(url, params=params, timeout=5)
        if response.status_code != 200:
            return None
        data = response.json()
        if data.get("code") != 0:
            return None
        candles = data["data"]
        df = pd.DataFrame(candles)
        df.rename(columns={
            'open': 'Open', 'close': 'Close', 'high': 'High', 'low': 'Low',
            'volume': 'Volume', 'time': 'Timestamp'
        }, inplace=True)
        for col in ["Open", "High", "Low", "Close"]:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        return df
    except:
        return None

def rsi_indicator(close, period=14):
    close = np.asarray(close)
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = np.zeros_like(gain)
    avg_loss = np.zeros_like(loss)
    avg_gain[:period] = np.mean(gain[:period])
    avg_loss[:period] = np.mean(loss[:period])
    for i in range(period, len(gain)):
        avg_gain[i] = (avg_gain[i-1]*(period-1) + gain[i]) / period
        avg_loss[i] = (avg_loss[i-1]*(period-1) + loss[i]) / period
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def ema_indicator(close, period):
    close = np.asarray(close)
    alpha = 2/(period+1)
    ema = np.zeros_like(close)
    ema[0] = close[0]
    for i in range(1, len(close)):
        ema[i] = alpha*close[i] + (1-alpha)*ema[i-1]
    return ema

def atr_indicator(high, low, close, period=14):
    high = np.asarray(high)
    low = np.asarray(low)
    close = np.asarray(close)
    tr = np.maximum(high - low, np.maximum(abs(high - np.roll(close, 1)), abs(low - np.roll(close, 1))))
    tr[0] = high[0] - low[0]
    atr = np.zeros_like(tr)
    atr[:period] = np.mean(tr[:period])
    for i in range(period, len(tr)):
        atr[i] = (atr[i-1]*(period-1) + tr[i]) / period
    return atr

def get_rsi_and_bars(symbol, interval):
    df = get_klines(symbol, interval=interval, limit=LOOKBACK)
    if df is None or len(df) < 2:
        return None, None, None, None
    close = df['Close']
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
    rs = gain / loss
    rsi = 100 - (100/(1+rs))
    current_rsi = rsi.iloc[-1]
    prev_rsi = rsi.iloc[-2] if len(rsi) > 1 else current_rsi
    bars = df.tail(BARS_FOR_LEVELS)
    high = bars['High']
    low = bars['Low']
    return current_rsi, prev_rsi, high, low

def get_ema_cross(symbol, interval, fast, slow):
    df = get_klines(symbol, interval=interval, limit=LOOKBACK)
    if df is None or len(df) < slow:
        return None, None, None, None, None
    close = df['Close'].values
    ema_fast = ema_indicator(close, fast)
    ema_slow = ema_indicator(close, slow)
    cur_fast = ema_fast[-1]
    cur_slow = ema_slow[-1]
    prev_fast = ema_fast[-2] if len(ema_fast) > 1 else cur_fast
    prev_slow = ema_slow[-2] if len(ema_slow) > 1 else cur_slow
    signal = None
    if prev_fast <= prev_slow and cur_fast > cur_slow:
        signal = "BUY"
    elif prev_fast >= prev_slow and cur_fast < cur_slow:
        signal = "SELL"
    return signal, cur_fast, cur_slow, prev_fast, prev_slow

def get_atr_value(symbol, interval):
    df = get_klines(symbol, interval=interval, limit=LOOKBACK)
    if df is None or len(df) < 14:
        return None
    high = df['High'].values
    low = df['Low'].values
    close = df['Close'].values
    atr = atr_indicator(high, low, close, 14)
    return atr[-1]

def get_trend_direction(symbol, base_interval, check_interval, fast=20, slow=50):
    df = get_klines(symbol, interval=check_interval, limit=LOOKBACK)
    if df is None or len(df) < slow:
        return None
    close = df['Close'].values
    ema_fast = ema_indicator(close, fast)
    ema_slow = ema_indicator(close, slow)
    if ema_fast[-1] > ema_slow[-1]:
        return "UP"
    elif ema_fast[-1] < ema_slow[-1]:
        return "DOWN"
    return None

# ---------- Расчёт уровней через ATR ----------
def calculate_atr_levels(price, atr, signal_type, tf):
    mult = ATR_MULTIPLIERS.get(tf, {"SL": 1.5, "TP1": 2.0, "TP2": 3.0, "TP3": 5.0})
    if signal_type == "BUY":
        sl = price - atr * mult["SL"]
        tp1 = price + atr * mult["TP1"]
        tp2 = price + atr * mult["TP2"]
        tp3 = price + atr * mult["TP3"]
    else:
        sl = price + atr * mult["SL"]
        tp1 = price - atr * mult["TP1"]
        tp2 = price - atr * mult["TP2"]
        tp3 = price - atr * mult["TP3"]
    return {
        'price': round(price, 2),
        'sl': round(sl, 2),
        'tp1': round(tp1, 2),
        'tp2': round(tp2, 2),
        'tp3': round(tp3, 2),
        'atr': round(atr, 2)
    }

def create_signal_dict(asset_name, tf, signal_type, signal, levels, ai_analysis=None):
    return {
        'timestamp': datetime.now(timezone.utc),
        'asset': asset_name,
        'tf': tf,
        'type': signal_type,
        'signal': signal,
        'levels': levels.copy(),   # копия, чтобы не менять оригинал
        'tp1_hit': False,
        'tp2_hit': False,
        'tp3_hit': False,
        'sl_hit': False,
        'closed': False,
        'ai_analysis': ai_analysis,
        'adjusted_by_ai': False    # флаг, что цели скорректированы AI
    }

# ---------- Добавление сигнала в активные ----------
def add_active_signal(asset_name, tf, signal_dict):
    if asset_name not in active_signals:
        active_signals[asset_name] = {}
    if tf not in active_signals[asset_name]:
        active_signals[asset_name][tf] = []
    # Проверяем, нет ли уже такого же сигнала (по типу и направлению) который не закрыт
    # Не добавляем дубль, но можно и добавлять — оставим возможность нескольких входов
    active_signals[asset_name][tf].append(signal_dict)

# ---------- Проверка уровней для одного сигнала ----------
async def check_signal_levels(bot, signal_dict):
    levels = signal_dict['levels']
    if signal_dict['closed']:
        return
    asset_name = signal_dict['asset']
    symbol = ASSETS[asset_name]['symbol']
    price = get_current_price(symbol)
    if price is None:
        return

    is_buy = signal_dict['signal'] == 'BUY'
    # Проверка SL
    if not signal_dict['sl_hit']:
        sl_hit = (is_buy and price <= levels['sl']) or (not is_buy and price >= levels['sl'])
        if sl_hit:
            signal_dict['sl_hit'] = True
            signal_dict['closed'] = True
            msg = (f"❌ Стоп-лосс сработал по {asset_name} [{signal_dict['tf']}] ({signal_dict['type']})\n"
                   f"Вход: ${safe_format(levels['price'])}\nSL: ${safe_format(levels['sl'])}")
            await send_to_chat(FakeContext(bot), msg)
            print(f"✅ Отправлено уведомление о SL для {asset_name} {signal_dict['tf']}")
            return

    # Проверка TP (только если SL не сработал)
    if not signal_dict['sl_hit']:
        # TP1
        if not signal_dict['tp1_hit']:
            tp1_hit = (is_buy and price >= levels['tp1']) or (not is_buy and price <= levels['tp1'])
            if tp1_hit:
                signal_dict['tp1_hit'] = True
                signal_dict['closed'] = True   # закрываем после TP1
                msg = (f"✅ TP1 достигнут по {asset_name} [{signal_dict['tf']}] ({signal_dict['type']})\n"
                       f"Вход: ${safe_format(levels['price'])}\nTP1: ${safe_format(levels['tp1'])}")
                await send_to_chat(FakeContext(bot), msg)
                print(f"✅ Отправлено уведомление о TP1 для {asset_name} {signal_dict['tf']}")
                return
        # TP2
        if not signal_dict['tp2_hit'] and not signal_dict['closed']:
            tp2_hit = (is_buy and price >= levels['tp2']) or (not is_buy and price <= levels['tp2'])
            if tp2_hit:
                signal_dict['tp2_hit'] = True
                signal_dict['closed'] = True
                msg = (f"✅ TP2 достигнут по {asset_name} [{signal_dict['tf']}] ({signal_dict['type']})\n"
                       f"Вход: ${safe_format(levels['price'])}\nTP2: ${safe_format(levels['tp2'])}")
                await send_to_chat(FakeContext(bot), msg)
                return
        # TP3
        if not signal_dict['tp3_hit'] and not signal_dict['closed']:
            tp3_hit = (is_buy and price >= levels['tp3']) or (not is_buy and price <= levels['tp3'])
            if tp3_hit:
                signal_dict['tp3_hit'] = True
                signal_dict['closed'] = True
                msg = (f"✅ TP3 достигнут по {asset_name} [{signal_dict['tf']}] ({signal_dict['type']})\n"
                       f"Вход: ${safe_format(levels['price'])}\nTP3: ${safe_format(levels['tp3'])}")
                await send_to_chat(FakeContext(bot), msg)

# ---------- Проверка всех активных сигналов ----------
async def check_all_active_signals(bot):
    for asset_name, tf_dict in active_signals.items():
        for tf, signals in tf_dict.items():
            for sig in signals[:]:  # итерируемся по копии списка
                await check_signal_levels(bot, sig)
                if sig['closed']:
                    # перемещаем в историю и удаляем из активных
                    signal_history.append(sig)
                    signals.remove(sig)

# ---------- Генерация и отправка нового сигнала ----------
async def handle_new_signal(asset_name, tf, signal_type, signal, price, rsi=None, ema_fast=None, ema_slow=None,
                            cur_fast3=None, cur_slow10=None, atr=None, higher_trend=None, context=None):
    levels = calculate_atr_levels(price, atr, signal, tf)
    # Опциональная AI-валидация для старших ТФ
    adjusted = False
    if tf in ["1h", "15m"] and GIGACHAT_AUTH_KEY:
        levels, adjusted = await validate_levels_with_ai(asset_name, tf, signal_type, price, levels)

    # AI-анализ сигнала
    ai_analysis = await get_ai_analysis(asset_name, signal_type, signal, price, rsi,
                                        ema_fast=ema_fast, ema_slow=ema_slow,
                                        atr=atr, higher_trend=higher_trend)
    # Создаём запись сигнала
    signal_dict = create_signal_dict(asset_name, tf, signal_type, signal, levels, ai_analysis)
    signal_dict['adjusted_by_ai'] = adjusted

    # Добавляем в активные
    add_active_signal(asset_name, tf, signal_dict)

    # Формируем текст уведомления
    stars = get_signal_stars(signal_type)
    direction = "покупку" if signal == "BUY" else "продажу"
    symbol = ASSETS[asset_name]['symbol']
    msg = f"{stars} 📢 Сигнал на {direction} по {signal_type.upper()} для {asset_name} ({symbol}) [{tf}]\n"
    msg += f"💰 Вход: ${safe_format(levels['price'])}\n"
    msg += f"🛑 SL: ${safe_format(levels['sl'])} (ATR×{ATR_MULTIPLIERS.get(tf, {}).get('SL', '?')})\n"
    msg += f"🎯 TP1: ${safe_format(levels['tp1'])} (ATR×{ATR_MULTIPLIERS.get(tf, {}).get('TP1', '?')})\n"
    msg += f"🎯 TP2: ${safe_format(levels['tp2'])} (ATR×{ATR_MULTIPLIERS.get(tf, {}).get('TP2', '?')})\n"
    msg += f"🎯 TP3: ${safe_format(levels['tp3'])} (ATR×{ATR_MULTIPLIERS.get(tf, {}).get('TP3', '?')})\n"
    if rsi is not None:
        msg += f"📊 RSI: {safe_format(rsi, ':.1f')}\n"
    if ema_fast is not None and ema_slow is not None:
        msg += f"📊 EMA: {safe_format(ema_fast)} / {safe_format(ema_slow)}\n"
    if cur_fast3 is not None:
        msg += f"📊 EMA(3/10): {safe_format(cur_fast3)} / {safe_format(cur_slow10)}\n"
    if adjusted:
        msg += "🔧 Цели скорректированы AI\n"
    if ai_analysis:
        msg += f"\n🧠 {ai_analysis}"
    await send_to_chat(context, msg)

# ---------- Проверка рыночных условий и генерация сигналов ----------
async def check_and_send_signal(context: ContextTypes.DEFAULT_TYPE):
    print("⏰ Запущена автоматическая проверка...")
    if CHANNEL_ID is None and chat_id is None:
        print("⏰ Нет получателей – пропускаем")
        return

    for name, asset in ASSETS.items():
        symbol = asset['symbol']
        for tf in ASSET_TIMEFRAMES[name]:
            try:
                price = get_current_price(symbol)
                if price is None:
                    continue
                current_rsi, prev_rsi, high, low = get_rsi_and_bars(symbol, tf)
                # RSI сигнал
                rsi_signal = None
                if current_rsi is not None and prev_rsi is not None:
                    if prev_rsi < RSI_OVERSOLD and current_rsi >= RSI_OVERSOLD:
                        rsi_signal = "BUY"
                    elif prev_rsi > RSI_OVERBOUGHT and current_rsi <= RSI_OVERBOUGHT:
                        rsi_signal = "SELL"
                    if rsi_signal:
                        atr = get_atr_value(symbol, tf)
                        if atr is not None:
                            await handle_new_signal(name, tf, "rsi", rsi_signal, price, rsi=current_rsi, atr=atr, context=context)

                # EMA (20/50)
                ema_signal, cur_fast, cur_slow, _, _ = get_ema_cross(symbol, tf, EMA_FAST, EMA_SLOW)
                if ema_signal:
                    atr = get_atr_value(symbol, tf)
                    if atr is not None:
                        await handle_new_signal(name, tf, "ema", ema_signal, price,
                                                ema_fast=cur_fast, ema_slow=cur_slow, atr=atr, context=context)

                # Combined (RSI + EMA)
                combined_signal = None
                if rsi_signal and ema_signal:
                    if rsi_signal == "BUY" and cur_fast > cur_slow:
                        combined_signal = "BUY"
                    elif rsi_signal == "SELL" and cur_fast < cur_slow:
                        combined_signal = "SELL"
                    if combined_signal:
                        atr = get_atr_value(symbol, tf)
                        if atr is not None:
                            await handle_new_signal(name, tf, "combined", combined_signal, price,
                                                    rsi=current_rsi, ema_fast=cur_fast, ema_slow=cur_slow, atr=atr, context=context)

                # FAST EMA (3/10)
                fast_cross, cur_fast3, cur_slow10, _, _ = get_ema_cross(symbol, tf, EMA_FAST_FAST, EMA_SLOW_FAST)
                if fast_cross:
                    # Проверка тренда на старшем ТФ
                    higher_tf = "1h" if tf == "15m" else "15m" if tf != "1h" else None
                    trend_ok = True
                    if higher_tf:
                        trend = get_trend_direction(symbol, tf, higher_tf)
                        if (fast_cross == "BUY" and trend != "UP") or (fast_cross == "SELL" and trend != "DOWN"):
                            trend_ok = False
                    if trend_ok:
                        atr = get_atr_value(symbol, tf)
                        if atr is not None:
                            await handle_new_signal(name, tf, "fast_ema", fast_cross, price,
                                                    cur_fast3=cur_fast3, cur_slow10=cur_slow10, atr=atr,
                                                    higher_trend=trend if higher_tf else None, context=context)

            except Exception as e:
                print(f"❌ Ошибка в check_and_send_signal для {name} {tf}: {e}")

    # Проверяем уровни для всех активных сигналов
    await check_all_active_signals(context.bot)

# ---------- Отчёты ----------
def get_moscow_time():
    return datetime.now(timezone.utc) + timedelta(hours=3)

def calculate_stats(signals):
    """Возвращает словарь со статистикой по сигналам из списка"""
    total = len(signals)
    if total == 0:
        return None
    tp1_count = sum(1 for s in signals if s['tp1_hit'])
    tp2_count = sum(1 for s in signals if s['tp2_hit'])
    tp3_count = sum(1 for s in signals if s['tp3_hit'])
    sl_count = sum(1 for s in signals if s['sl_hit'])
    closed = tp1_count + tp2_count + tp3_count + sl_count
    success_rate = (tp1_count / closed * 100) if closed > 0 else 0
    return {
        'total': total,
        'tp1': tp1_count,
        'tp2': tp2_count,
        'tp3': tp3_count,
        'sl': sl_count,
        'closed': closed,
        'success_rate': success_rate
    }

def generate_insights(stats_by_asset, stats_by_type):
    """Генерирует заметки для отчёта"""
    insights = []
    # Эффективные инструменты
    best = []
    worst = []
    for asset, st in stats_by_asset.items():
        if st['closed'] > 0:
            best.append((asset, st['success_rate'], st['tp1'], st['closed']))
            worst.append((asset, st['success_rate'], st['tp1'], st['closed']))
    best.sort(key=lambda x: x[1], reverse=True)
    worst.sort(key=lambda x: x[1])
    if best:
        top3 = best[:3]
        insights.append("🏆 Самые эффективные инструменты (по % успеха):")
        for a, rate, tp1, closed in top3:
            insights.append(f"{a} – {rate:.1f}% ({tp1}/{closed})")
    if worst:
        bottom3 = worst[:3]
        insights.append("📉 Самые неэффективные:")
        for a, rate, tp1, closed in bottom3:
            insights.append(f"{a} – {rate:.1f}% ({tp1}/{closed})")
    # Заметки по типам сигналов
    for typ, st in stats_by_type.items():
        if st['closed'] >= 3:  # минимальная выборка для вывода
            if st['success_rate'] == 100:
                insights.append(f"💡 {typ.upper()} показал 100% попаданий ({st['tp1']} из {st['closed']}), но выборка мала.")
            elif st['success_rate'] == 0:
                insights.append(f"💡 {typ.upper()} показал 0% успеха – стоит пересмотреть логику.")
    return insights

async def generate_daily_report():
    now = get_moscow_time()
    yesterday = now - timedelta(days=1)
    signals = [s for s in signal_history if s['timestamp'].replace(tzinfo=timezone.utc) + timedelta(hours=3) >= yesterday]
    return format_report(signals, f"📊 Ежедневный отчёт за {now.strftime('%d.%m.%Y')}")

async def generate_weekly_report():
    now = get_moscow_time()
    week_ago = now - timedelta(days=7)
    signals = [s for s in signal_history if s['timestamp'].replace(tzinfo=timezone.utc) + timedelta(hours=3) >= week_ago]
    return format_report(signals, f"📊 Воскресный отчёт за неделю ({ (now-timedelta(days=7)).strftime('%d.%m')} - {now.strftime('%d.%m.%Y')})")

async def generate_today_report():
    now = get_moscow_time()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    signals = [s for s in signal_history if s['timestamp'].replace(tzinfo=timezone.utc) + timedelta(hours=3) >= today_start]
    return format_report(signals, f"📊 Отчёт за сегодня ({now.strftime('%d.%m.%Y')})")

def format_report(signals, title):
    if not signals:
        return f"{title}\n\nСигналов не было."

    total = len(signals)
    stats_by_asset = defaultdict(list)
    stats_by_type = defaultdict(list)
    for s in signals:
        if not s['closed']:
            continue   # не учитываем открытые
        stats_by_asset[s['asset']].append(s)
        stats_by_type[s['type']].append(s)

    # Общая статистика
    all_stats = calculate_stats([s for s in signals if s['closed']])
    lines = [title, ""]
    if all_stats:
        lines.append(f"Всего сигналов: {all_stats['total']}")
        lines.append(f"Успешных (TP1 достигнут): {all_stats['tp1']}")
        lines.append(f"Общая успешность: ~{all_stats['success_rate']:.1f}%")
        if all_stats['tp2'] == 0 and all_stats['tp3'] == 0:
            lines.append("TP2 и TP3 не достигнуты ни разу.")
        lines.append("")

    # По активам
    asset_stats = {}
    for asset, lst in stats_by_asset.items():
        st = calculate_stats(lst)
        if st:
            asset_stats[asset] = st
    lines.append("📌 По инструментам:")
    for asset, st in sorted(asset_stats.items()):
        lines.append(f"{asset}: всего {st['total']}, TP1: {st['tp1']}, успешность {st['success_rate']:.1f}%")
    lines.append("")

    # По типам сигналов
    type_stats = {}
    for typ, lst in stats_by_type.items():
        st = calculate_stats(lst)
        if st:
            type_stats[typ] = st
    lines.append("🔹 По типам сигналов:")
    for typ, st in sorted(type_stats.items()):
        lines.append(f"{typ.upper()}: всего {st['total']}, TP1: {st['tp1']}, успешность {st['success_rate']:.1f}%")
    lines.append("")

    # Инсайты
    insights = generate_insights(asset_stats, type_stats)
    if insights:
        lines.append("💡 Заметка:")
        lines.extend(insights)

    return "\n".join(lines)

# ---------- Отправка сообщений ----------
class FakeContext:
    def __init__(self, bot):
        self.bot = bot

async def send_to_chat(context, text):
    try:
        if CHANNEL_ID is not None:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=text)
        if chat_id is not None:
            await context.bot.send_message(chat_id=chat_id, text=text)
        if CHANNEL_ID is None and chat_id is None:
            print("⚠️ Нет получателя для сообщения")
    except Exception as e:
        print(f"❌ Ошибка в send_to_chat: {e}")

# ---------- Планировщик ----------
async def start_scheduler(app):
    job_queue = app.job_queue
    if job_queue is None:
        print("⚠️ JobQueue не доступен.")
        return
    for job in job_queue.jobs():
        job.schedule_removal()

    # Проверка сигналов каждую минуту
    job_queue.run_repeating(check_and_send_signal, interval=60, first=10)
    # Ежедневный отчёт в 21:00 МСК
    job_queue.run_daily(lambda ctx: asyncio.create_task(daily_report_job(ctx)),
                        time=dt_time(hour=21, minute=0, tzinfo=MSK), days=tuple(range(7)))
    # Воскресный отчёт в 18:00 МСК
    job_queue.run_daily(lambda ctx: asyncio.create_task(weekly_report_job(ctx)),
                        time=dt_time(hour=18, minute=0, tzinfo=MSK), days=(6,))
    # Утренний обзор в 10:00 МСК
    job_queue.run_daily(send_morning_report, time=dt_time(hour=10, minute=0, tzinfo=MSK), days=tuple(range(7)))
    # Обновление новостей
    job_queue.run_repeating(update_news_sentiment, interval=NEWS_UPDATE_INTERVAL, first=30)

    print("📅 Планировщик запущен (проверка каждую минуту, отчёты: ежедневно в 21:00 МСК, воскресный в 18:00 МСК, утренний обзор в 10:00 МСК)")

async def daily_report_job(context):
    print("📊 Запущена задача daily_report")
    report = await generate_daily_report()
    await send_to_chat(context, report)

async def weekly_report_job(context):
    print("📊 Запущена задача weekly_report")
    report = await generate_weekly_report()
    await send_to_chat(context, report)

async def send_morning_report(context: ContextTypes.DEFAULT_TYPE):
    print("📊 Формирование утреннего обзора...")
    msg = "🌅 **Утренний обзор рынка**\n\n"
    for name, asset in ASSETS.items():
        symbol = asset['symbol']
        price = get_current_price(symbol)
        rsi, _, _, _ = get_rsi_and_bars(symbol, "15m")
        if price is not None and rsi is not None:
            msg += f"**{name}** ({symbol}): ${safe_format(price)}  |  RSI(14): {safe_format(rsi, ':.1f')}\n"
        else:
            msg += f"**{name}**: данные недоступны\n"
    msg += "\n📰 **Новостной фон:**\n"
    for asset_name in ASSETS:
        sentiment = news_sentiment.get(asset_name, "Нет данных")
        msg += f"**{asset_name}**: {sentiment}\n"
    await send_to_chat(context, msg)
    print("✅ Утренний обзор отправлен")

# ---------- Команды бота ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "👋 Бот запущен!\n"
        "Отслеживаю: GOLD, BTC, ETH, SOL.\n"
        "Таймфреймы: GOLD (5м, 15м), крипта (15м, 1ч).\n"
        "⭐ FAST EMA | ⭐⭐ RSI/EMA | ⭐⭐⭐ Combined\n"
        "📰 Новости каждый час. Утренний обзор в 10:00 МСК.\n\n"
        "Команды:\n"
        "/gold, /btc, /eth, /sol – цена и активные сигналы\n"
        "/crypto – сводка\n"
        "/status – активные сигналы\n"
        "/today – отчёт за сегодня\n"
        "/ai {актив} – AI-анализ"
    )
    # Отправим текущие активные сигналы
    msg = "📌 Активные сигналы:\n"
    for name in ASSETS:
        for tf in ASSET_TIMEFRAMES[name]:
            sigs = active_signals.get(name, {}).get(tf, [])
            for s in sigs:
                if not s['closed']:
                    direction = "BUY" if s['signal'] == "BUY" else "SELL"
                    msg += f"{name} {tf} {s['type']}: {direction} (вход {s['levels']['price']})\n"
    if msg == "📌 Активные сигналы:\n":
        msg += "Нет активных сигналов."
    await update.message.reply_text(msg)

async def asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, asset_name):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"⏳ Загружаю данные по {asset_name}...")
    await asyncio.sleep(random.uniform(0.5, 1.5))
    symbol = ASSETS[asset_name]['symbol']
    msg = f"💰 {asset_name} ({symbol})\n"
    price = get_current_price(symbol)
    if price is None:
        await update.message.reply_text(f"❌ Не удалось получить цену для {asset_name}")
        return
    msg += f"Цена: ${safe_format(price)}\n\n"
    for tf in ASSET_TIMEFRAMES[asset_name]:
        msg += f"⏱ {tf}\n"
        sigs = active_signals.get(asset_name, {}).get(tf, [])
        if not sigs:
            msg += "  Нет активных сигналов.\n"
        else:
            for s in sigs:
                if s['closed']: continue
                direction = "покупку" if s['signal'] == "BUY" else "продажу"
                stars = get_signal_stars(s['type'])
                msg += f"  {stars} {s['type'].upper()} на {direction}\n"
                msg += f"    Вход: {safe_format(s['levels']['price'])} | SL: {safe_format(s['levels']['sl'])} | TP1: {safe_format(s['levels']['tp1'])}\n"
        msg += "\n"
    await update.message.reply_text(msg)

async def gold(update, context): await asset_cmd(update, context, "GOLD")
async def btc(update, context): await asset_cmd(update, context, "BTC")
async def eth(update, context): await asset_cmd(update, context, "ETH")
async def sol(update, context): await asset_cmd(update, context, "SOL")

async def crypto(update, context):
    msg = "📊 СВОДКА ПО АКТИВАМ (активные сигналы):\n\n"
    for name in ASSETS:
        msg += f"**{name}** ({ASSETS[name]['symbol']})\n"
        for tf in ASSET_TIMEFRAMES[name]:
            sigs = [s for s in active_signals.get(name, {}).get(tf, []) if not s['closed']]
            if sigs:
                signals_str = ", ".join(f"{s['type']}:{s['signal']}" for s in sigs)
                msg += f"  {tf}: {signals_str}\n"
            else:
                msg += f"  {tf}: нет\n"
    await update.message.reply_text(msg)

async def status(update, context):
    msg = "📌 АКТИВНЫЕ СИГНАЛЫ:\n\n"
    for name in ASSETS:
        for tf in ASSET_TIMEFRAMES[name]:
            sigs = [s for s in active_signals.get(name, {}).get(tf, []) if not s['closed']]
            for s in sigs:
                msg += f"{name} {tf} {s['type']}: {s['signal']} (вход {s['levels']['price']})\n"
    if msg == "📌 АКТИВНЫЕ СИГНАЛЫ:\n\n":
        msg += "Нет активных сигналов."
    await update.message.reply_text(msg)

async def today_report(update, context):
    report = await generate_today_report()
    await update.message.reply_text(report)

async def ai_command(update, context):
    if not context.args:
        await update.message.reply_text("Укажите актив: /ai BTC")
        return
    asset_name = context.args[0].upper()
    if asset_name not in ASSETS:
        await update.message.reply_text("Доступны: GOLD, BTC, ETH, SOL")
        return
    # Анализируем последний активный сигнал
    last_signal = None
    for tf in reversed(ASSET_TIMEFRAMES[asset_name]):  # сначала старший ТФ
        sigs = [s for s in active_signals.get(asset_name, {}).get(tf, []) if not s['closed']]
        if sigs:
            last_signal = sigs[-1]
            break
    if not last_signal:
        await update.message.reply_text("Нет активных сигналов для анализа.")
        return
    analysis = last_signal.get('ai_analysis')
    if analysis:
        await update.message.reply_text(f"🧠 Анализ для {asset_name} ({last_signal['tf']}):\n\n{analysis}")
    else:
        await update.message.reply_text("AI-анализ отсутствует.")

# ---------- Запуск ----------
async def post_init(app):
    await start_scheduler(app)

def run_bot():
    print("🤖 Бот запускается...")
    if GIGACHAT_AUTH_KEY:
        print(f"🧠 GigaChat AI включён")
    else:
        print("⚠️ GigaChat AI отключён")
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gold", gold))
    app.add_handler(CommandHandler("btc", btc))
    app.add_handler(CommandHandler("eth", eth))
    app.add_handler(CommandHandler("sol", sol))
    app.add_handler(CommandHandler("crypto", crypto))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("today", today_report))
    app.add_handler(CommandHandler("ai", ai_command))
    print("✅ Бот готов")
    app.run_polling(drop_pending_updates=True)

def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()

if __name__ == "__main__":
    main()
