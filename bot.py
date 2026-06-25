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

TOKEN = os.environ.get('TOKEN')
if not TOKEN:
    raise ValueError("TOKEN environment variable not set")

CHANNEL_ID = os.environ.get('CHANNEL_ID')

# === GigaChat ===
GIGACHAT_AUTH_KEY = os.environ.get('GIGACHAT_AUTH_KEY')
GIGACHAT_SCOPE = "GIGACHAT_API_PERS"

app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    return "Bot is running!", 200

def run_flask():
    app_flask.run(host='0.0.0.0', port=10000)

chat_id = None
signal_history = []
gigachat_token = None
gigachat_token_expires = 0

news_sentiment = {}
LAST_NEWS_UPDATE = 0
NEWS_UPDATE_INTERVAL = 3600  # 1 час

TIMEFRAMES = ["5m", "15m"]

ASSETS = {
    "GOLD": {"symbol": "XAUT-USDT", "data": {}},
    "BTC":  {"symbol": "BTC-USDT",  "data": {}},
    "ETH":  {"symbol": "ETH-USDT",  "data": {}},
    "SOL":  {"symbol": "SOL-USDT",  "data": {}},
}

for asset in ASSETS:
    for tf in TIMEFRAMES:
        ASSETS[asset]["data"][tf] = {
            "rsi_signal": None,
            "rsi_levels": None,
            "rsi_sent": None,
            "ema_signal": None,
            "ema_levels": None,
            "ema_sent": None,
            "combined_signal": None,
            "combined_levels": None,
            "combined_sent": None,
            "fast_ema_signal": None,
            "fast_ema_levels": None,
            "fast_ema_sent": None,
        }

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
LOOKBACK = 50
BARS_FOR_LEVELS = 10
EMA_FAST = 20
EMA_SLOW = 50

EMA_FAST_FAST = 3
EMA_SLOW_FAST = 10
SL_MULT = 1.2
TP1_MULT = 1.5
TP2_MULT = 2.0
TP3_MULT = 3.0

def safe_float(value, default=0.0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default

def safe_format(value, format_spec=":.2f"):
    """Форматирует число с заданной точностью, безопасно обрабатывая None и строки."""
    try:
        return f"{safe_float(value):{format_spec}}"
    except:
        return str(value)

def get_signal_stars(signal_type):
    if signal_type == "fast_ema":
        return "⭐"
    elif signal_type in ("rsi", "ema"):
        return "⭐⭐"
    elif signal_type == "combined":
        return "⭐⭐⭐"
    else:
        return ""

# === GigaChat ===
async def get_gigachat_token():
    global gigachat_token, gigachat_token_expires
    if gigachat_token and time.time() < gigachat_token_expires:
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
            "max_tokens": 200,
            "stream": False
        }
        response = requests.post(url, headers=headers, json=payload, verify=False, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"]["content"].strip()
            else:
                print(f"❌ Неожиданный ответ GigaChat: {data}")
                return None
        else:
            print(f"❌ Ошибка GigaChat API: {response.status_code} {response.text}")
            return None
    except Exception as e:
        print(f"❌ Исключение при запросе к GigaChat: {e}")
        return None

# === Новости ===
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
        if titles:
            return " ".join(titles)
        else:
            return ""
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

async def update_news_sentiment():
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

# === AI анализ ===
async def get_ai_analysis(asset_name, signal_type, signal, price, rsi, ema_fast=None, ema_slow=None,
                          atr=None, volume=None, higher_trend=None):
    if not GIGACHAT_AUTH_KEY:
        return None
    direction = "покупку" if signal == "BUY" else "продажу"
    price_str = safe_format(price)
    rsi_str = safe_format(rsi, ":.1f") if rsi is not None else "N/A"
    ema_text = ""
    if ema_fast is not None and ema_slow is not None:
        ema_text = f"EMA20: {safe_format(ema_fast)}, EMA50: {safe_format(ema_slow)}"
    atr_str = safe_format(atr) if atr is not None else ""
    volume_str = safe_format(volume, ":.0f") if volume is not None else ""
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
        analysis = await ask_gigachat(prompt)
        if analysis and len(analysis) > 350:
            analysis = analysis[:350] + "..."
        return analysis
    except Exception as e:
        print(f"❌ Ошибка AI: {e}")
        return None

async def send_to_chat(context, text):
    try:
        if CHANNEL_ID is not None:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=text)
        if chat_id is not None:
            await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        print(f"❌ Ошибка в send_to_chat: {e}")

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
            'open': 'Open',
            'close': 'Close',
            'high': 'High',
            'low': 'Low',
            'volume': 'Volume',
            'time': 'Timestamp'
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
    gain = np.where(delta>0, delta, 0)
    loss = np.where(delta<0, -delta, 0)
    avg_gain = np.zeros_like(gain)
    avg_loss = np.zeros_like(loss)
    avg_gain[:period] = np.mean(gain[:period])
    avg_loss[:period] = np.mean(loss[:period])
    for i in range(period, len(gain)):
        avg_gain[i] = (avg_gain[i-1]*(period-1)+gain[i])/period
        avg_loss[i] = (avg_loss[i-1]*(period-1)+loss[i])/period
    rs = avg_gain / avg_loss
    rsi = 100 - (100/(1+rs))
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
    high = np.asarray(high); low = np.asarray(low); close = np.asarray(close)
    tr = np.maximum(high - low, np.maximum(abs(high - np.roll(close,1)), abs(low - np.roll(close,1))))
    tr[0] = high[0] - low[0]
    atr = np.zeros_like(tr)
    atr[:period] = np.mean(tr[:period])
    for i in range(period, len(tr)):
        atr[i] = (atr[i-1]*(period-1)+tr[i])/period
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
    prev_rsi = rsi.iloc[-2] if len(rsi)>1 else current_rsi
    bars = df.tail(BARS_FOR_LEVELS)
    high = bars['High']; low = bars['Low']
    return current_rsi, prev_rsi, high, low

def get_ema_cross(symbol, interval, fast, slow):
    df = get_klines(symbol, interval=interval, limit=LOOKBACK)
    if df is None or len(df) < slow:
        return None, None, None, None, None
    close = df['Close'].values
    ema_fast = ema_indicator(close, fast)
    ema_slow = ema_indicator(close, slow)
    cur_fast = ema_fast[-1]; cur_slow = ema_slow[-1]
    prev_fast = ema_fast[-2] if len(ema_fast)>1 else cur_fast
    prev_slow = ema_slow[-2] if len(ema_slow)>1 else cur_slow
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
    high = df['High'].values; low = df['Low'].values; close = df['Close'].values
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
    else:
        return None

def calculate_levels(price, high, low, signal_type):
    buffer = 2.0
    min_stop = 5.0
    if signal_type == "BUY":
        sl = low.min() - buffer
        if price - sl < min_stop:
            sl = price - min_stop
        tp1 = price + (price - sl)
        tp2 = price + 2*(price - sl)
    else:
        sl = high.max() + buffer
        if sl - price < min_stop:
            sl = price + min_stop
        tp1 = price - (sl - price)
        tp2 = price - 2*(sl - price)
    return sl, tp1, tp2, None

def calculate_atr_levels(price, atr, signal_type):
    if signal_type == "BUY":
        sl = price - atr * SL_MULT
        tp1 = price + atr * TP1_MULT
        tp2 = price + atr * TP2_MULT
        tp3 = price + atr * TP3_MULT
    else:
        sl = price + atr * SL_MULT
        tp1 = price - atr * TP1_MULT
        tp2 = price - atr * TP2_MULT
        tp3 = price - atr * TP3_MULT
    return sl, tp1, tp2, tp3

def record_signal_event(asset_name, tf, signal_type, signal, price, sl=None, tp1=None, tp2=None, tp3=None, ai_analysis=None):
    entry = {
        "timestamp": datetime.now(),
        "asset": asset_name,
        "tf": tf,
        "type": signal_type,
        "signal": signal,
        "entry_price": price,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "sl_hit": False,
        "closed": False,
        "ai_analysis": ai_analysis
    }
    signal_history.append(entry)

def update_signal_event(asset_name, tf, signal_type, event_type, value):
    for entry in reversed(signal_history):
        if entry["asset"] == asset_name and entry["tf"] == tf and entry["type"] == signal_type and not entry["closed"]:
            if event_type == "tp1":
                entry["tp1_hit"] = True; entry["closed"] = True
            elif event_type == "tp2":
                entry["tp2_hit"] = True; entry["closed"] = True
            elif event_type == "tp3":
                entry["tp3_hit"] = True; entry["closed"] = True
            elif event_type == "sl":
                entry["sl_hit"] = True; entry["closed"] = True
            break

def check_signal(asset_name, interval):
    asset = ASSETS[asset_name]
    symbol = asset["symbol"]
    tf_data = asset["data"][interval]
    price = get_current_price(symbol)
    if price is None:
        return (None, None, None, None, None, None, None, None, None,
                None, None, None, None, None, None, None, None)

    current_rsi, prev_rsi, high, low = get_rsi_and_bars(symbol, interval)
    rsi_signal = None; rsi_levels = None
    if current_rsi is not None and prev_rsi is not None:
        if prev_rsi < RSI_OVERSOLD and current_rsi >= RSI_OVERSOLD:
            rsi_signal = "BUY"
        elif prev_rsi > RSI_OVERBOUGHT and current_rsi <= RSI_OVERBOUGHT:
            rsi_signal = "SELL"
        if rsi_signal and rsi_signal != tf_data["rsi_signal"]:
            sl, tp1, tp2, _ = calculate_levels(price, high, low, rsi_signal)
            rsi_levels = {'price': price, 'sl': sl, 'tp1': tp1, 'tp2': tp2, 'rsi': current_rsi}
            tf_data["rsi_signal"] = rsi_signal
            tf_data["rsi_levels"] = rsi_levels

    ema_signal, cur_fast, cur_slow, _, _ = get_ema_cross(symbol, interval, EMA_FAST, EMA_SLOW)
    ema_levels = None
    if ema_signal and ema_signal != tf_data["ema_signal"]:
        atr = get_atr_value(symbol, interval)
        if atr is not None:
            sl, tp1, tp2, _ = calculate_atr_levels(price, atr, ema_signal)
            ema_levels = {'price': price, 'sl': sl, 'tp1': tp1, 'tp2': tp2, 'atr': atr}
            tf_data["ema_signal"] = ema_signal
            tf_data["ema_levels"] = ema_levels
        else:
            ema_signal = None

    combined_signal = None; combined_levels = None
    if rsi_signal:
        if rsi_signal == "BUY" and cur_fast > cur_slow:
            combined_signal = "BUY"
        elif rsi_signal == "SELL" and cur_fast < cur_slow:
            combined_signal = "SELL"
        if combined_signal and combined_signal != tf_data["combined_signal"]:
            if rsi_levels:
                combined_levels = rsi_levels.copy()
            else:
                sl, tp1, tp2, _ = calculate_levels(price, high, low, combined_signal)
                combined_levels = {'price': price, 'sl': sl, 'tp1': tp1, 'tp2': tp2, 'rsi': current_rsi}
            tf_data["combined_signal"] = combined_signal
            tf_data["combined_levels"] = combined_levels

    fast_signal = None; fast_levels = None
    if interval == "5m":
        higher_tf = "15m"
    elif interval == "15m":
        higher_tf = "1h"
    else:
        higher_tf = None

    fast_cross, cur_fast3, cur_slow10, _, _ = get_ema_cross(symbol, interval, EMA_FAST_FAST, EMA_SLOW_FAST)
    if fast_cross:
        trend_ok = False
        if higher_tf:
            trend = get_trend_direction(symbol, interval, higher_tf)
            if fast_cross == "BUY" and trend == "UP":
                trend_ok = True
            elif fast_cross == "SELL" and trend == "DOWN":
                trend_ok = True
        else:
            trend_ok = True
        if trend_ok and fast_cross != tf_data["fast_ema_signal"]:
            atr = get_atr_value(symbol, interval)
            if atr is not None:
                sl, tp1, tp2, tp3 = calculate_atr_levels(price, atr, fast_cross)
                fast_levels = {'price': price, 'sl': sl, 'tp1': tp1, 'tp2': tp2, 'tp3': tp3, 'atr': atr}
                tf_data["fast_ema_signal"] = fast_cross
                tf_data["fast_ema_levels"] = fast_levels
                fast_signal = fast_cross
            else:
                fast_signal = None

    return (rsi_signal, rsi_levels, current_rsi,
            ema_signal, ema_levels, price, cur_fast, cur_slow,
            combined_signal, combined_levels,
            fast_signal, fast_levels, cur_fast3, cur_slow10, interval)

async def check_and_notify_levels(bot, asset_name, interval, signal_type, levels, sent_flag):
    if not levels:
        return
    price = get_current_price(ASSETS[asset_name]["symbol"])
    if price is None:
        return
    if levels.get('tp1') is not None:
        is_buy = levels['price'] < levels['tp1']
    else:
        is_buy = levels['price'] > levels['sl']
    if not levels.get('tp1_hit', False) and not levels.get('tp2_hit', False) and not levels.get('tp3_hit', False):
        if not levels.get('sl_hit', False):
            if is_buy and price <= levels['sl']:
                levels['sl_hit'] = True
                update_signal_event(asset_name, interval, signal_type, "sl", price)
                msg = f"❌ Стоп-лосс сработал по {asset_name} [{interval}] ({signal_type})\nВход: ${safe_format(levels['price'])}\nSL: ${safe_format(levels['sl'])}"
                await send_to_chat(FakeContext(bot), msg)
                return
    if not levels.get('sl_hit', False):
        if levels.get('tp1') is not None and not levels.get('tp1_hit', False):
            if is_buy and price >= levels['tp1']:
                levels['tp1_hit'] = True
                update_signal_event(asset_name, interval, signal_type, "tp1", price)
                msg = f"✅ TP1 достигнут по {asset_name} [{interval}] ({signal_type})\nВход: ${safe_format(levels['price'])}\nTP1: ${safe_format(levels['tp1'])}"
                await send_to_chat(FakeContext(bot), msg)
        if levels.get('tp2') is not None and not levels.get('tp2_hit', False):
            if is_buy and price >= levels['tp2']:
                levels['tp2_hit'] = True
                update_signal_event(asset_name, interval, signal_type, "tp2", price)
                msg = f"✅ TP2 достигнут по {asset_name} [{interval}] ({signal_type})\nВход: ${safe_format(levels['price'])}\nTP2: ${safe_format(levels['tp2'])}"
                await send_to_chat(FakeContext(bot), msg)
        if levels.get('tp3') is not None and not levels.get('tp3_hit', False):
            if is_buy and price >= levels['tp3']:
                levels['tp3_hit'] = True
                update_signal_event(asset_name, interval, signal_type, "tp3", price)
                msg = f"✅ TP3 достигнут по {asset_name} [{interval}] ({signal_type})\nВход: ${safe_format(levels['price'])}\nTP3: ${safe_format(levels['tp3'])}"
                await send_to_chat(FakeContext(bot), msg)

class FakeContext:
    def __init__(self, bot):
        self.bot = bot

# === Команды ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await send_current_signals(context)
    await update.message.reply_text(
        "👋 Бот запущен!\n"
        "Отслеживаю: GOLD (XAUT), BTC, ETH, SOL.\n"
        "Таймфреймы: 5м и 15м.\n"
        "Типы сигналов с оценкой надёжности:\n"
        "⭐ FAST EMA (3/10) – быстрый, но частый\n"
        "⭐⭐ RSI или EMA (20/50) – средняя надёжность\n"
        "⭐⭐⭐ RSI+EMA (20/50) – самый сильный\n\n"
        "📰 Новостной фон обновляется каждый час.\n"
        "📊 Утренний обзор рынка в 10:00 МСК.\n\n"
        "Команды:\n"
        "/gold, /btc, /eth, /sol – цена и все сигналы\n"
        "/crypto – сводка\n"
        "/status – последние сигналы по всем активам\n"
        "/today – отчёт за сегодня\n"
        "/ai {актив} – AI-анализ по активу (например, /ai BTC)"
    )

async def asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, asset_name):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"⏳ Загружаю данные по {asset_name}...")
    await asyncio.sleep(random.uniform(0.5, 1.5))
    asset = ASSETS[asset_name]
    symbol = asset["symbol"]
    msg = f"💰 {asset_name} ({symbol})\n\n"
    price = get_current_price(symbol)
    if price is None:
        await update.message.reply_text(f"❌ Не удалось получить цену для {asset_name}")
        return
    for tf in TIMEFRAMES:
        tf_data = asset["data"][tf]
        msg += f"⏱ {tf}\n"
        if tf_data["rsi_signal"]:
            lv = tf_data["rsi_levels"]
            direction = "покупку" if tf_data["rsi_signal"] == "BUY" else "продажу"
            msg += f"🔹 Сигнал на {direction} по RSI"
            if lv:
                msg += f" | Вход: ${safe_format(lv['price'])} | SL: ${safe_format(lv['sl'])} | TP1: ${safe_format(lv['tp1'])} | TP2: ${safe_format(lv['tp2'])}"
            msg += "\n"
        else:
            msg += "🔹 RSI: Нет\n"
        if tf_data["ema_signal"]:
            lv = tf_data["ema_levels"]
            direction = "покупку" if tf_data["ema_signal"] == "BUY" else "продажу"
            msg += f"🔸 Сигнал на {direction} по EMA (20/50)"
            if lv:
                msg += f" | Вход: ${safe_format(lv['price'])} | SL: ${safe_format(lv['sl'])} | TP1: ${safe_format(lv['tp1'])} | TP2: ${safe_format(lv['tp2'])}"
            msg += "\n"
        else:
            msg += "🔸 EMA (20/50): Нет\n"
        if tf_data["combined_signal"]:
            lv = tf_data["combined_levels"]
            direction = "покупку" if tf_data["combined_signal"] == "BUY" else "продажу"
            msg += f"🔹 Сигнал на {direction} по RSI+EMA"
            if lv:
                msg += f" | Вход: ${safe_format(lv['price'])} | SL: ${safe_format(lv['sl'])} | TP1: ${safe_format(lv['tp1'])} | TP2: ${safe_format(lv['tp2'])}"
            msg += "\n"
        else:
            msg += "🔹 RSI+EMA: Нет\n"
        if tf_data["fast_ema_signal"]:
            lv = tf_data["fast_ema_levels"]
            direction = "покупку" if tf_data["fast_ema_signal"] == "BUY" else "продажу"
            msg += f"⭐ Сигнал на {direction} по FAST EMA (3/10)"
            if lv:
                msg += f" | Вход: ${safe_format(lv['price'])} | SL: ${safe_format(lv['sl'])} | TP1: ${safe_format(lv['tp1'])} | TP2: ${safe_format(lv['tp2'])} | TP3: ${safe_format(lv['tp3'])}"
            msg += "\n"
        else:
            msg += "⭐ FAST EMA: Нет\n"
        for sig_type in ["rsi","ema","combined","fast_ema"]:
            key = sig_type+"_levels"
            if tf_data.get(key):
                lv = tf_data[key]
                if lv.get('sl_hit') or lv.get('tp1_hit') or lv.get('tp2_hit') or lv.get('tp3_hit'):
                    msg += f"   {sig_type.upper()} статус:"
                    if lv.get('sl_hit'): msg += " SL сработал"
                    if lv.get('tp1_hit'): msg += " TP1 достигнут"
                    if lv.get('tp2_hit'): msg += " TP2 достигнут"
                    if lv.get('tp3_hit'): msg += " TP3 достигнут"
                    msg += "\n"
        msg += "\n"
    await update.message.reply_text(msg)

async def gold(update: Update, context): await asset_cmd(update, context, "GOLD")
async def btc(update: Update, context): await asset_cmd(update, context, "BTC")
async def eth(update: Update, context): await asset_cmd(update, context, "ETH")
async def sol(update: Update, context): await asset_cmd(update, context, "SOL")

async def crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Загружаю сводку...")
    await asyncio.sleep(1)
    msg = "📊 СВОДКА ПО АКТИВАМ (последние сигналы):\n\n"
    for name in ASSETS:
        asset = ASSETS[name]
        msg += f"**{name}** ({asset['symbol']})\n"
        for tf in TIMEFRAMES:
            tf_data = asset["data"][tf]
            rsi_sig = tf_data["rsi_signal"] or "Нет"
            ema_sig = tf_data["ema_signal"] or "Нет"
            comb_sig = tf_data["combined_signal"] or "Нет"
            fast_sig = tf_data["fast_ema_signal"] or "Нет"
            msg += f"  {tf}: RSI={rsi_sig}, EMA={ema_sig}, RSI+EMA={comb_sig}, FAST={fast_sig}\n"
        msg += "\n"
    await update.message.reply_text(msg)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    msg = "📌 ПОСЛЕДНИЕ СИГНАЛЫ ПО ВСЕМ АКТИВАМ:\n\n"
    for name in ASSETS:
        asset = ASSETS[name]
        msg += f"**{name}** ({asset['symbol']})\n"
        for tf in TIMEFRAMES:
            tf_data = asset["data"][tf]
            msg += f"  ⏱ {tf}\n"
            msg += f"    RSI: {tf_data['rsi_signal'] or 'Нет'}\n"
            msg += f"    EMA (20/50): {tf_data['ema_signal'] or 'Нет'}\n"
            msg += f"    RSI+EMA: {tf_data['combined_signal'] or 'Нет'}\n"
            msg += f"    FAST EMA (3/10): {tf_data['fast_ema_signal'] or 'Нет'}\n"
        msg += "\n"
    await update.message.reply_text(msg)

async def today_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    report = await generate_today_report()
    await update.message.reply_text(report)

async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Укажите актив, например: /ai BTC")
        return
    asset_name = context.args[0].upper()
    if asset_name not in ASSETS:
        await update.message.reply_text(f"Актив {asset_name} не поддерживается. Доступны: GOLD, BTC, ETH, SOL")
        return
    await update.message.reply_text(f"⏳ Запрашиваю AI-анализ для {asset_name}...")
    asset = ASSETS[asset_name]
    symbol = asset["symbol"]
    price = get_current_price(symbol)
    if price is None:
        await update.message.reply_text("❌ Не удалось получить цену")
        return
    signal = None
    signal_type = None
    tf = None
    rsi = None
    cur_fast = None
    cur_slow = None
    for check_tf in ["15m", "5m"]:
        tf_data = asset["data"][check_tf]
        if tf_data["rsi_signal"]:
            signal = tf_data["rsi_signal"]
            signal_type = "RSI"
            tf = check_tf
            rsi = tf_data["rsi_levels"]["rsi"] if tf_data["rsi_levels"] else None
            break
        elif tf_data["ema_signal"]:
            signal = tf_data["ema_signal"]
            signal_type = "EMA"
            tf = check_tf
            break
        elif tf_data["combined_signal"]:
            signal = tf_data["combined_signal"]
            signal_type = "Combined"
            tf = check_tf
            rsi = tf_data["combined_levels"]["rsi"] if tf_data["combined_levels"] else None
            break
        elif tf_data["fast_ema_signal"]:
            signal = tf_data["fast_ema_signal"]
            signal_type = "FAST EMA"
            tf = check_tf
            break
    if not signal:
        await update.message.reply_text("На данный момент нет активного сигнала для AI-анализа")
        return
    if not rsi:
        rsi, _, _, _ = get_rsi_and_bars(symbol, tf or "15m")
    atr_val = get_atr_value(symbol, tf or "15m")
    df = get_klines(symbol, interval=tf or "15m", limit=10)
    volume = df['Volume'].iloc[-1] if df is not None and not df.empty else None
    higher_tf = "1h" if tf == "15m" else "15m"
    higher_trend = get_trend_direction(symbol, tf, higher_tf)
    analysis = await get_ai_analysis(asset_name, signal_type, signal, price, rsi, cur_fast, cur_slow,
                                     atr=atr_val, volume=volume, higher_trend=higher_trend)
    if analysis:
        await update.message.reply_text(f"🧠 Анализ для {asset_name} ({tf}):\n\n{analysis}")
    else:
        await update.message.reply_text("❌ Не удалось получить анализ. Проверьте настройки GigaChat.")

# === Отчёты ===
def get_moscow_time():
    return datetime.now(timezone.utc) + timedelta(hours=3)

async def generate_daily_report():
    now = get_moscow_time()
    yesterday = now - timedelta(days=1)
    events = [e for e in signal_history if e["timestamp"] >= yesterday]
    if not events:
        return "📊 За последние 24 часа сигналов не было."
    stats = {}
    for e in events:
        key = (e["asset"], e["tf"])
        if key not in stats:
            stats[key] = {"rsi":{"total":0,"tp1":0,"tp2":0,"tp3":0,"sl":0},
                          "ema":{"total":0,"tp1":0,"tp2":0,"tp3":0,"sl":0},
                          "combined":{"total":0,"tp1":0,"tp2":0,"tp3":0,"sl":0},
                          "fast_ema":{"total":0,"tp1":0,"tp2":0,"tp3":0,"sl":0}}
        typ = e["type"]
        stats[key][typ]["total"] += 1
        if e["tp1_hit"]: stats[key][typ]["tp1"] += 1
        if e["tp2_hit"]: stats[key][typ]["tp2"] += 1
        if e["tp3_hit"]: stats[key][typ]["tp3"] += 1
        if e["sl_hit"]: stats[key][typ]["sl"] += 1
    lines = [f"📊 **Ежедневный отчёт за {now.strftime('%d.%m.%Y')}**"]
    lines.append("")
    for (asset, tf), data in stats.items():
        lines.append(f"**{asset}** ({tf}):")
        for sig_type, vals in data.items():
            total = vals["total"]
            if total == 0: continue
            tp1, tp2, tp3, sl = vals["tp1"], vals["tp2"], vals["tp3"], vals["sl"]
            closed = tp1+tp2+tp3+sl
            success = (tp1+tp2+tp3)/closed*100 if closed>0 else 0
            lines.append(f"  {sig_type.upper()}: {total} сигн. | TP1: {tp1} | TP2: {tp2} | TP3: {tp3} | SL: {sl} | Успешность: {success:.1f}%")
        lines.append("")
    return "\n".join(lines)

async def generate_weekly_report():
    now = get_moscow_time()
    week_ago = now - timedelta(days=7)
    events = [e for e in signal_history if e["timestamp"] >= week_ago]
    if not events:
        return "📊 За последнюю неделю сигналов не было."
    stats = {}
    for e in events:
        key = (e["asset"], e["tf"])
        if key not in stats:
            stats[key] = {"rsi":{"total":0,"tp1":0,"tp2":0,"tp3":0,"sl":0},
                          "ema":{"total":0,"tp1":0,"tp2":0,"tp3":0,"sl":0},
                          "combined":{"total":0,"tp1":0,"tp2":0,"tp3":0,"sl":0},
                          "fast_ema":{"total":0,"tp1":0,"tp2":0,"tp3":0,"sl":0}}
        typ = e["type"]
        stats[key][typ]["total"] += 1
        if e["tp1_hit"]: stats[key][typ]["tp1"] += 1
        if e["tp2_hit"]: stats[key][typ]["tp2"] += 1
        if e["tp3_hit"]: stats[key][typ]["tp3"] += 1
        if e["sl_hit"]: stats[key][typ]["sl"] += 1
    lines = [f"📊 **Воскресный отчёт за неделю ({ (now - timedelta(days=7)).strftime('%d.%m')} - {now.strftime('%d.%m.%Y')})**"]
    lines.append("")
    for (asset, tf), data in stats.items():
        lines.append(f"**{asset}** ({tf}):")
        for sig_type, vals in data.items():
            total = vals["total"]
            if total == 0: continue
            tp1, tp2, tp3, sl = vals["tp1"], vals["tp2"], vals["tp3"], vals["sl"]
            closed = tp1+tp2+tp3+sl
            success = (tp1+tp2+tp3)/closed*100 if closed>0 else 0
            lines.append(f"  {sig_type.upper()}: {total} сигн. | TP1: {tp1} | TP2: {tp2} | TP3: {tp3} | SL: {sl} | Успешность: {success:.1f}%")
        lines.append("")
    return "\n".join(lines)

async def generate_today_report():
    now = get_moscow_time()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = [e for e in signal_history if (e["timestamp"].astimezone(timezone.utc) + timedelta(hours=3)) >= today_start]
    if not events:
        return "📊 За сегодня сигналов не было."
    stats = {}
    for e in events:
        key = (e["asset"], e["tf"])
        if key not in stats:
            stats[key] = {"rsi":{"total":0,"tp1":0,"tp2":0,"tp3":0,"sl":0},
                          "ema":{"total":0,"tp1":0,"tp2":0,"tp3":0,"sl":0},
                          "combined":{"total":0,"tp1":0,"tp2":0,"tp3":0,"sl":0},
                          "fast_ema":{"total":0,"tp1":0,"tp2":0,"tp3":0,"sl":0}}
        typ = e["type"]
        stats[key][typ]["total"] += 1
        if e["tp1_hit"]: stats[key][typ]["tp1"] += 1
        if e["tp2_hit"]: stats[key][typ]["tp2"] += 1
        if e["tp3_hit"]: stats[key][typ]["tp3"] += 1
        if e["sl_hit"]: stats[key][typ]["sl"] += 1
    lines = [f"📊 **Отчёт за сегодня ({now.strftime('%d.%m.%Y')})**"]
    lines.append("")
    for (asset, tf), data in stats.items():
        lines.append(f"**{asset}** ({tf}):")
        for sig_type, vals in data.items():
            total = vals["total"]
            if total == 0: continue
            tp1, tp2, tp3, sl = vals["tp1"], vals["tp2"], vals["tp3"], vals["sl"]
            closed = tp1+tp2+tp3+sl
            success = (tp1+tp2+tp3)/closed*100 if closed>0 else 0
            lines.append(f"  {sig_type.upper()}: {total} сигн. | TP1: {tp1} | TP2: {tp2} | TP3: {tp3} | SL: {sl} | Успешность: {success:.1f}%")
        lines.append("")
    return "\n".join(lines)

async def daily_report_task(context: ContextTypes.DEFAULT_TYPE):
    report = await generate_daily_report()
    await send_to_chat(context, report)

async def weekly_report_task(context: ContextTypes.DEFAULT_TYPE):
    report = await generate_weekly_report()
    await send_to_chat(context, report)

# === Утренний обзор ===
async def send_morning_report(context: ContextTypes.DEFAULT_TYPE):
    print("📊 Формирование утреннего обзора...")
    msg = "🌅 **Утренний обзор рынка**\n\n"
    for name, asset in ASSETS.items():
        symbol = asset["symbol"]
        price = get_current_price(symbol)
        rsi, _, _, _ = get_rsi_and_bars(symbol, "15m")
        if price is not None and rsi is not None:
            msg += f"**{name}** ({symbol}): ${safe_format(price)}  |  RSI(14): {safe_format(rsi, ':.1f')}\n"
        else:
            msg += f"**{name}**: данные недоступны\n"
    msg += "\n📰 **Новостной фон (последние часы):**\n"
    for asset_name in ASSETS:
        sentiment = news_sentiment.get(asset_name, "Нет данных")
        msg += f"**{asset_name}**: {sentiment}\n"
    await send_to_chat(context, msg)
    print("✅ Утренний обзор отправлен")

# === Отправка текущих сигналов (исправлено форматирование) ===
async def send_current_signals(context):
    print("📤 Принудительная отправка текущих сигналов...")
    if CHANNEL_ID is None and chat_id is None:
        return
    for name in ASSETS:
        asset = ASSETS[name]
        symbol = asset["symbol"]
        for tf in TIMEFRAMES:
            (rsi_signal, rsi_levels, current_rsi,
             ema_signal, ema_levels, price, cur_fast, cur_slow,
             combined_signal, combined_levels,
             fast_signal, fast_levels, cur_fast3, cur_slow10, _) = check_signal(name, tf)
            tf_data = asset["data"][tf]
            # RSI
            if rsi_signal and rsi_levels and rsi_signal != tf_data.get("rsi_sent"):
                lv = rsi_levels
                stars = get_signal_stars("rsi")
                direction = "покупку" if rsi_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по RSI для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${safe_format(lv['price'])}\n"
                msg += f"🛑 SL: ${safe_format(lv['sl'])}\n"
                msg += f"🎯 TP1: ${safe_format(lv['tp1'])} (1:1)\n"
                msg += f"🎯 TP2: ${safe_format(lv['tp2'])} (1:2)\n"
                msg += f"📊 RSI: {safe_format(lv['rsi'], ':.1f')}"
                # AI
                atr_val = get_atr_value(symbol, tf)
                df = get_klines(symbol, interval=tf, limit=10)
                volume = df['Volume'].iloc[-1] if df is not None and not df.empty else None
                higher_tf = "1h" if tf == "15m" else "15m"
                higher_trend = get_trend_direction(symbol, tf, higher_tf)
                ai_text = await get_ai_analysis(name, "RSI", rsi_signal, lv['price'], lv['rsi'],
                                                atr=atr_val, volume=volume, higher_trend=higher_trend)
                if ai_text: msg += f"\n\n{ai_text}"
                await send_to_chat(context, msg)
                tf_data["rsi_sent"] = rsi_signal
                record_signal_event(name, tf, "rsi", rsi_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'], ai_analysis=ai_text)
            # EMA
            if ema_signal and ema_levels and ema_signal != tf_data.get("ema_sent"):
                lv = ema_levels
                stars = get_signal_stars("ema")
                direction = "покупку" if ema_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по EMA (20/50) для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${safe_format(lv['price'])}\n"
                msg += f"🛑 SL: ${safe_format(lv['sl'])} (ATR×{SL_MULT})\n"
                msg += f"🎯 TP1: ${safe_format(lv['tp1'])} (ATR×{TP1_MULT})\n"
                msg += f"🎯 TP2: ${safe_format(lv['tp2'])} (ATR×{TP2_MULT})\n"
                msg += f"📊 ATR: {safe_format(lv['atr'])}\n"
                msg += f"📊 EMA20: {safe_format(cur_fast)}, EMA50: {safe_format(cur_slow)}\n"
                msg += f"🔹 Действие: {ema_signal}"
                # AI
                atr_val = lv.get('atr')
                df = get_klines(symbol, interval=tf, limit=10)
                volume = df['Volume'].iloc[-1] if df is not None and not df.empty else None
                higher_tf = "1h" if tf == "15m" else "15m"
                higher_trend = get_trend_direction(symbol, tf, higher_tf)
                ai_text = await get_ai_analysis(name, "EMA", ema_signal, lv['price'], None,
                                                ema_fast=cur_fast, ema_slow=cur_slow,
                                                atr=atr_val, volume=volume, higher_trend=higher_trend)
                if ai_text: msg += f"\n\n{ai_text}"
                await send_to_chat(context, msg)
                tf_data["ema_sent"] = ema_signal
                record_signal_event(name, tf, "ema", ema_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'], ai_analysis=ai_text)
            # Combined
            if combined_signal and combined_levels and combined_signal != tf_data.get("combined_sent"):
                lv = combined_levels
                stars = get_signal_stars("combined")
                direction = "покупку" if combined_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по RSI+EMA для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${safe_format(lv['price'])}\n"
                msg += f"🛑 SL: ${safe_format(lv['sl'])}\n"
                msg += f"🎯 TP1: ${safe_format(lv['tp1'])} (1:1)\n"
                msg += f"🎯 TP2: ${safe_format(lv['tp2'])} (1:2)\n"
                msg += f"📊 RSI: {safe_format(lv['rsi'], ':.1f')}\n"
                msg += f"🔹 EMA20: {safe_format(cur_fast)}, EMA50: {safe_format(cur_slow)}"
                # AI
                atr_val = None
                df = get_klines(symbol, interval=tf, limit=10)
                volume = df['Volume'].iloc[-1] if df is not None and not df.empty else None
                higher_tf = "1h" if tf == "15m" else "15m"
                higher_trend = get_trend_direction(symbol, tf, higher_tf)
                ai_text = await get_ai_analysis(name, "RSI+EMA", combined_signal, lv['price'], lv['rsi'],
                                                ema_fast=cur_fast, ema_slow=cur_slow,
                                                atr=atr_val, volume=volume, higher_trend=higher_trend)
                if ai_text: msg += f"\n\n{ai_text}"
                await send_to_chat(context, msg)
                tf_data["combined_sent"] = combined_signal
                record_signal_event(name, tf, "combined", combined_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'], ai_analysis=ai_text)
            # FAST
            if fast_signal and fast_levels and fast_signal != tf_data.get("fast_ema_sent"):
                lv = fast_levels
                stars = get_signal_stars("fast_ema")
                direction = "покупку" if fast_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по FAST EMA (3/10) для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${safe_format(lv['price'])}\n"
                msg += f"🛑 SL: ${safe_format(lv['sl'])} (ATR×{SL_MULT})\n"
                msg += f"🎯 TP1: ${safe_format(lv['tp1'])} (ATR×{TP1_MULT})\n"
                msg += f"🎯 TP2: ${safe_format(lv['tp2'])} (ATR×{TP2_MULT})\n"
                msg += f"🎯 TP3: ${safe_format(lv['tp3'])} (ATR×{TP3_MULT})\n"
                msg += f"📊 ATR: {safe_format(lv['atr'])}\n"
                msg += f"📊 EMA3: {safe_format(cur_fast3)}, EMA10: {safe_format(cur_slow10)}"
                # AI
                atr_val = lv.get('atr')
                df = get_klines(symbol, interval=tf, limit=10)
                volume = df['Volume'].iloc[-1] if df is not None and not df.empty else None
                higher_tf = "1h" if tf == "15m" else "15m"
                higher_trend = get_trend_direction(symbol, tf, higher_tf)
                ai_text = await get_ai_analysis(name, "FAST EMA", fast_signal, lv['price'], None,
                                                ema_fast=cur_fast3, ema_slow=cur_slow10,
                                                atr=atr_val, volume=volume, higher_trend=higher_trend)
                if ai_text: msg += f"\n\n{ai_text}"
                await send_to_chat(context, msg)
                tf_data["fast_ema_sent"] = fast_signal
                record_signal_event(name, tf, "fast_ema", fast_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'], lv['tp3'], ai_analysis=ai_text)

# === Автоматическая проверка (исправлено форматирование) ===
async def check_and_send_signal(bot):
    print("⏰ Запущена автоматическая проверка...")
    if CHANNEL_ID is None and chat_id is None:
        return
    context = FakeContext(bot)
    for name in ASSETS:
        asset = ASSETS[name]
        symbol = asset["symbol"]
        for tf in TIMEFRAMES:
            print(f"🔍 Проверка {name} {tf}...")
            (rsi_signal, rsi_levels, current_rsi,
             ema_signal, ema_levels, price, cur_fast, cur_slow,
             combined_signal, combined_levels,
             fast_signal, fast_levels, cur_fast3, cur_slow10, _) = check_signal(name, tf)
            tf_data = asset["data"][tf]

            if rsi_signal and rsi_levels and rsi_signal != tf_data.get("rsi_sent"):
                lv = rsi_levels
                stars = get_signal_stars("rsi")
                direction = "покупку" if rsi_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по RSI для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${safe_format(lv['price'])}\n"
                msg += f"🛑 SL: ${safe_format(lv['sl'])}\n"
                msg += f"🎯 TP1: ${safe_format(lv['tp1'])} (1:1)\n"
                msg += f"🎯 TP2: ${safe_format(lv['tp2'])} (1:2)\n"
                msg += f"📊 RSI: {safe_format(lv['rsi'], ':.1f')}"
                atr_val = get_atr_value(symbol, tf)
                df = get_klines(symbol, interval=tf, limit=10)
                volume = df['Volume'].iloc[-1] if df is not None and not df.empty else None
                higher_tf = "1h" if tf == "15m" else "15m"
                higher_trend = get_trend_direction(symbol, tf, higher_tf)
                ai_text = await get_ai_analysis(name, "RSI", rsi_signal, lv['price'], lv['rsi'],
                                                atr=atr_val, volume=volume, higher_trend=higher_trend)
                if ai_text: msg += f"\n\n{ai_text}"
                await send_to_chat(context, msg)
                tf_data["rsi_sent"] = rsi_signal
                record_signal_event(name, tf, "rsi", rsi_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'], ai_analysis=ai_text)

            if ema_signal and ema_levels and ema_signal != tf_data.get("ema_sent"):
                lv = ema_levels
                stars = get_signal_stars("ema")
                direction = "покупку" if ema_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по EMA (20/50) для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${safe_format(lv['price'])}\n"
                msg += f"🛑 SL: ${safe_format(lv['sl'])} (ATR×{SL_MULT})\n"
                msg += f"🎯 TP1: ${safe_format(lv['tp1'])} (ATR×{TP1_MULT})\n"
                msg += f"🎯 TP2: ${safe_format(lv['tp2'])} (ATR×{TP2_MULT})\n"
                msg += f"📊 ATR: {safe_format(lv['atr'])}\n"
                msg += f"📊 EMA20: {safe_format(cur_fast)}, EMA50: {safe_format(cur_slow)}\n"
                msg += f"🔹 Действие: {ema_signal}"
                atr_val = lv.get('atr')
                df = get_klines(symbol, interval=tf, limit=10)
                volume = df['Volume'].iloc[-1] if df is not None and not df.empty else None
                higher_tf = "1h" if tf == "15m" else "15m"
                higher_trend = get_trend_direction(symbol, tf, higher_tf)
                ai_text = await get_ai_analysis(name, "EMA", ema_signal, lv['price'], None,
                                                ema_fast=cur_fast, ema_slow=cur_slow,
                                                atr=atr_val, volume=volume, higher_trend=higher_trend)
                if ai_text: msg += f"\n\n{ai_text}"
                await send_to_chat(context, msg)
                tf_data["ema_sent"] = ema_signal
                record_signal_event(name, tf, "ema", ema_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'], ai_analysis=ai_text)

            if combined_signal and combined_levels and combined_signal != tf_data.get("combined_sent"):
                lv = combined_levels
                stars = get_signal_stars("combined")
                direction = "покупку" if combined_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по RSI+EMA для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${safe_format(lv['price'])}\n"
                msg += f"🛑 SL: ${safe_format(lv['sl'])}\n"
                msg += f"🎯 TP1: ${safe_format(lv['tp1'])} (1:1)\n"
                msg += f"🎯 TP2: ${safe_format(lv['tp2'])} (1:2)\n"
                msg += f"📊 RSI: {safe_format(lv['rsi'], ':.1f')}\n"
                msg += f"🔹 EMA20: {safe_format(cur_fast)}, EMA50: {safe_format(cur_slow)}"
                atr_val = None
                df = get_klines(symbol, interval=tf, limit=10)
                volume = df['Volume'].iloc[-1] if df is not None and not df.empty else None
                higher_tf = "1h" if tf == "15m" else "15m"
                higher_trend = get_trend_direction(symbol, tf, higher_tf)
                ai_text = await get_ai_analysis(name, "RSI+EMA", combined_signal, lv['price'], lv['rsi'],
                                                ema_fast=cur_fast, ema_slow=cur_slow,
                                                atr=atr_val, volume=volume, higher_trend=higher_trend)
                if ai_text: msg += f"\n\n{ai_text}"
                await send_to_chat(context, msg)
                tf_data["combined_sent"] = combined_signal
                record_signal_event(name, tf, "combined", combined_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'], ai_analysis=ai_text)

            if fast_signal and fast_levels and fast_signal != tf_data.get("fast_ema_sent"):
                lv = fast_levels
                stars = get_signal_stars("fast_ema")
                direction = "покупку" if fast_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по FAST EMA (3/10) для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${safe_format(lv['price'])}\n"
                msg += f"🛑 SL: ${safe_format(lv['sl'])} (ATR×{SL_MULT})\n"
                msg += f"🎯 TP1: ${safe_format(lv['tp1'])} (ATR×{TP1_MULT})\n"
                msg += f"🎯 TP2: ${safe_format(lv['tp2'])} (ATR×{TP2_MULT})\n"
                msg += f"🎯 TP3: ${safe_format(lv['tp3'])} (ATR×{TP3_MULT})\n"
                msg += f"📊 ATR: {safe_format(lv['atr'])}\n"
                msg += f"📊 EMA3: {safe_format(cur_fast3)}, EMA10: {safe_format(cur_slow10)}"
                atr_val = lv.get('atr')
                df = get_klines(symbol, interval=tf, limit=10)
                volume = df['Volume'].iloc[-1] if df is not None and not df.empty else None
                higher_tf = "1h" if tf == "15m" else "15m"
                higher_trend = get_trend_direction(symbol, tf, higher_tf)
                ai_text = await get_ai_analysis(name, "FAST EMA", fast_signal, lv['price'], None,
                                                ema_fast=cur_fast3, ema_slow=cur_slow10,
                                                atr=atr_val, volume=volume, higher_trend=higher_trend)
                if ai_text: msg += f"\n\n{ai_text}"
                await send_to_chat(context, msg)
                tf_data["fast_ema_sent"] = fast_signal
                record_signal_event(name, tf, "fast_ema", fast_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'], lv['tp3'], ai_analysis=ai_text)

            # Проверка уровней
            if tf_data.get("rsi_levels"):
                await check_and_notify_levels(bot, name, tf, "rsi", tf_data["rsi_levels"], tf_data.get("rsi_sent"))
            if tf_data.get("ema_levels"):
                await check_and_notify_levels(bot, name, tf, "ema", tf_data["ema_levels"], tf_data.get("ema_sent"))
            if tf_data.get("combined_levels"):
                await check_and_notify_levels(bot, name, tf, "combined", tf_data["combined_levels"], tf_data.get("combined_sent"))
            if tf_data.get("fast_ema_levels"):
                await check_and_notify_levels(bot, name, tf, "fast_ema", tf_data["fast_ema_levels"], tf_data.get("fast_ema_sent"))

# === Фоновый цикл ===
async def scheduler_loop(app):
    global LAST_NEWS_UPDATE
    await asyncio.sleep(10)
    print("🔄 Фоновый планировщик запущен (проверка каждую минуту)")
    last_daily_report = None
    last_weekly_report = None
    last_morning_report = None
    while True:
        try:
            now = get_moscow_time()
            # Ежедневный отчёт в 21:00
            if now.hour == 21 and now.minute == 0 and last_daily_report != now.date():
                await daily_report_task(FakeContext(app.bot))
                last_daily_report = now.date()
            # Воскресный отчёт в 18:00
            if now.weekday() == 6 and now.hour == 18 and now.minute == 0 and last_weekly_report != now.date():
                await weekly_report_task(FakeContext(app.bot))
                last_weekly_report = now.date()
            # Утренний обзор в 10:00
            if now.hour == 10 and now.minute == 0 and last_morning_report != now.date():
                await send_morning_report(FakeContext(app.bot))
                last_morning_report = now.date()
            # Обновление новостного фона раз в час
            if time.time() - LAST_NEWS_UPDATE > NEWS_UPDATE_INTERVAL:
                await update_news_sentiment()
                LAST_NEWS_UPDATE = time.time()
            # Основная проверка сигналов
            await check_and_send_signal(app.bot)
        except Exception as e:
            print(f"❌ Ошибка в планировщике: {e}")
        await asyncio.sleep(60)

# === Запуск ===
def run_bot():
    print("🤖 Бот запускается...")
    if GIGACHAT_AUTH_KEY:
        key_preview = GIGACHAT_AUTH_KEY[:10] + "..." if len(GIGACHAT_AUTH_KEY) > 10 else GIGACHAT_AUTH_KEY
        print(f"🧠 GigaChat AI включён (ключ: {key_preview})")
    else:
        print("⚠️ GigaChat AI отключён (ключ не задан или пустой)")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gold", gold))
    app.add_handler(CommandHandler("btc", btc))
    app.add_handler(CommandHandler("eth", eth))
    app.add_handler(CommandHandler("sol", sol))
    app.add_handler(CommandHandler("crypto", crypto))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("today", today_report))
    app.add_handler(CommandHandler("ai", ai_command))

    print("🧪 Тестируем подключение к BingX...")
    for name in ASSETS:
        price = get_current_price(ASSETS[name]["symbol"])
        if price:
            print(f"✅ {name}: ${price:.2f}")
        else:
            print(f"❌ {name}: не удалось")
    if CHANNEL_ID:
        print(f"📢 Будет дублировать сообщения в канал {CHANNEL_ID}")
    else:
        print("📢 Канал не задан")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(scheduler_loop(app))

    print("✅ Бот готов, запускаем поллинг...")
    app.run_polling(drop_pending_updates=True)

def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()

if __name__ == "__main__":
    main()
