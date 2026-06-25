import os
import time
import threading
import random
import asyncio
import requests
import pandas as pd
import numpy as np
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from datetime import time as dt_time, datetime, timedelta

TOKEN = os.environ.get('TOKEN')
if not TOKEN:
    raise ValueError("TOKEN environment variable not set")

CHANNEL_ID = os.environ.get('CHANNEL_ID')

app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    return "Bot is running!", 200

def run_flask():
    app_flask.run(host='0.0.0.0', port=10000)

chat_id = None
signal_history = []

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
            "last_rsi_signal": None,
            "last_rsi_levels": None,
            "last_rsi_sent": None,
            "last_ema_signal": None,
            "last_ema_levels": None,
            "last_ema_sent": None,
            "last_combined_signal": None,
            "last_combined_levels": None,
            "last_combined_sent": None,
            "last_fast_ema_signal": None,
            "last_fast_ema_levels": None,
            "last_fast_ema_sent": None,
            "entry_price": None,
            "sl": None,
            "tp1": None,
            "tp2": None,
            "tp3": None,
            "tp1_hit": False,
            "tp2_hit": False,
            "tp3_hit": False,
            "sl_hit": False,
            "signal_type": None,
            "tp1_notified": False,
            "tp2_notified": False,
            "tp3_notified": False,
            "sl_notified": False,
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

def get_signal_stars(signal_type):
    if signal_type == "fast_ema":
        return "⭐"
    elif signal_type in ("rsi", "ema"):
        return "⭐⭐"
    elif signal_type == "combined":
        return "⭐⭐⭐"
    else:
        return ""

async def send_to_chat(context, text):
    if CHANNEL_ID is not None:
        await context.bot.send_message(chat_id=CHANNEL_ID, text=text)
    if chat_id is not None:
        await context.bot.send_message(chat_id=chat_id, text=text)

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
    alpha = 2 / (period + 1)
    ema = np.zeros_like(close)
    ema[0] = close[0]
    for i in range(1, len(close)):
        ema[i] = alpha * close[i] + (1 - alpha) * ema[i-1]
    return ema

def atr_indicator(high, low, close, period=14):
    high = np.asarray(high)
    low = np.asarray(low)
    close = np.asarray(close)
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
    rsi = 100 - (100 / (1 + rs))
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
    """
    Проверяет тренд на старшем таймфрейме.
    Возвращает: 'UP' если EMA20 > EMA50, 'DOWN' если EMA20 < EMA50, иначе None.
    """
    df = get_klines(symbol, interval=check_interval, limit=LOOKBACK)
    if df is None or len(df) < slow:
        return None
    close = df['Close'].values
    ema_fast = ema_indicator(close, fast)
    ema_slow = ema_indicator(close, slow)
    cur_fast = ema_fast[-1]
    cur_slow = ema_slow[-1]
    if cur_fast > cur_slow:
        return "UP"
    elif cur_fast < cur_slow:
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
        tp2 = price + 2 * (price - sl)
    else:
        sl = high.max() + buffer
        if sl - price < min_stop:
            sl = price + min_stop
        tp1 = price - (sl - price)
        tp2 = price - 2 * (sl - price)
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

def record_signal_event(asset_name, tf, signal_type, signal, price, sl=None, tp1=None, tp2=None, tp3=None):
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
        "closed": False
    }
    signal_history.append(entry)
    print(f"📝 Записано: {asset_name} {tf} {signal_type} {signal} @ {price}")

def update_signal_event(asset_name, tf, event_type, value):
    for entry in reversed(signal_history):
        if entry["asset"] == asset_name and entry["tf"] == tf and not entry["closed"]:
            if event_type == "tp1":
                entry["tp1_hit"] = True
                entry["closed"] = True
            elif event_type == "tp2":
                entry["tp2_hit"] = True
                entry["closed"] = True
            elif event_type == "tp3":
                entry["tp3_hit"] = True
                entry["closed"] = True
            elif event_type == "sl":
                entry["sl_hit"] = True
                entry["closed"] = True
            print(f"📝 Обновлено: {asset_name} {tf} {event_type}")
            break

def check_signal(asset_name, interval):
    asset = ASSETS[asset_name]
    symbol = asset["symbol"]
    tf_data = asset["data"][interval]
    price = get_current_price(symbol)
    if price is None:
        return (None, None, None, None, None, None, None, None, None,
                None, None, None, None, None, None, None, None)

    # RSI
    current_rsi, prev_rsi, high, low = get_rsi_and_bars(symbol, interval)
    rsi_signal = None
    rsi_levels = None
    if current_rsi is not None and prev_rsi is not None:
        if prev_rsi < RSI_OVERSOLD and current_rsi >= RSI_OVERSOLD:
            rsi_signal = "BUY"
        elif prev_rsi > RSI_OVERBOUGHT and current_rsi <= RSI_OVERBOUGHT:
            rsi_signal = "SELL"
        if rsi_signal and rsi_signal != tf_data["last_rsi_signal"]:
            sl, tp1, tp2, _ = calculate_levels(price, high, low, rsi_signal)
            rsi_levels = {'price': price, 'sl': sl, 'tp1': tp1, 'tp2': tp2, 'rsi': current_rsi}
            tf_data["last_rsi_signal"] = rsi_signal
            tf_data["last_rsi_levels"] = rsi_levels
            tf_data["entry_price"] = price
            tf_data["sl"] = sl
            tf_data["tp1"] = tp1
            tf_data["tp2"] = tp2
            tf_data["tp3"] = None
            tf_data["signal_type"] = "rsi"
            tf_data["tp1_hit"] = tf_data["tp2_hit"] = tf_data["tp3_hit"] = tf_data["sl_hit"] = False
            tf_data["tp1_notified"] = tf_data["tp2_notified"] = tf_data["tp3_notified"] = tf_data["sl_notified"] = False

    # EMA (20/50) + уровни по ATR (2 TP)
    ema_signal, cur_fast, cur_slow, _, _ = get_ema_cross(symbol, interval, EMA_FAST, EMA_SLOW)
    ema_levels = None
    if ema_signal and ema_signal != tf_data["last_ema_signal"]:
        atr = get_atr_value(symbol, interval)
        if atr is not None:
            sl, tp1, tp2, _ = calculate_atr_levels(price, atr, ema_signal)  # только 2 TP
            ema_levels = {'price': price, 'sl': sl, 'tp1': tp1, 'tp2': tp2, 'atr': atr}
            tf_data["last_ema_signal"] = ema_signal
            tf_data["last_ema_levels"] = ema_levels
            tf_data["entry_price"] = price
            tf_data["sl"] = sl
            tf_data["tp1"] = tp1
            tf_data["tp2"] = tp2
            tf_data["tp3"] = None
            tf_data["signal_type"] = "ema"
            tf_data["tp1_hit"] = tf_data["tp2_hit"] = tf_data["tp3_hit"] = tf_data["sl_hit"] = False
            tf_data["tp1_notified"] = tf_data["tp2_notified"] = tf_data["tp3_notified"] = tf_data["sl_notified"] = False
        else:
            ema_signal = None  # если ATR не получен, не отправляем
    else:
        ema_signal = None

    # Combined (RSI + EMA)
    combined_signal = None
    combined_levels = None
    if rsi_signal:
        if rsi_signal == "BUY" and cur_fast > cur_slow:
            combined_signal = "BUY"
        elif rsi_signal == "SELL" and cur_fast < cur_slow:
            combined_signal = "SELL"
        if combined_signal and combined_signal != tf_data["last_combined_signal"]:
            if rsi_levels:
                combined_levels = rsi_levels.copy()
            else:
                sl, tp1, tp2, _ = calculate_levels(price, high, low, combined_signal)
                combined_levels = {'price': price, 'sl': sl, 'tp1': tp1, 'tp2': tp2, 'rsi': current_rsi}
            tf_data["last_combined_signal"] = combined_signal
            tf_data["last_combined_levels"] = combined_levels
            if not rsi_levels:
                tf_data["entry_price"] = price
                tf_data["sl"] = sl
                tf_data["tp1"] = tp1
                tf_data["tp2"] = tp2
                tf_data["tp3"] = None
                tf_data["signal_type"] = "combined"
                tf_data["tp1_hit"] = tf_data["tp2_hit"] = tf_data["tp3_hit"] = tf_data["sl_hit"] = False
                tf_data["tp1_notified"] = tf_data["tp2_notified"] = tf_data["tp3_notified"] = tf_data["sl_notified"] = False
        else:
            combined_signal = None

    # FAST EMA (3/10) + ATR + фильтр тренда
    fast_signal = None
    fast_levels = None
    # Определяем старший ТФ для фильтра
    if interval == "5m":
        higher_tf = "15m"
    elif interval == "15m":
        higher_tf = "1h"
    else:
        higher_tf = None

    fast_cross, cur_fast3, cur_slow10, _, _ = get_ema_cross(symbol, interval, EMA_FAST_FAST, EMA_SLOW_FAST)
    if fast_cross:
        # Проверяем тренд на старшем ТФ
        trend_ok = False
        if higher_tf:
            trend = get_trend_direction(symbol, interval, higher_tf)
            if fast_cross == "BUY" and trend == "UP":
                trend_ok = True
            elif fast_cross == "SELL" and trend == "DOWN":
                trend_ok = True
        else:
            trend_ok = True

        if trend_ok and fast_cross != tf_data["last_fast_ema_signal"]:
            atr = get_atr_value(symbol, interval)
            if atr is not None:
                sl, tp1, tp2, tp3 = calculate_atr_levels(price, atr, fast_cross)
                fast_levels = {'price': price, 'sl': sl, 'tp1': tp1, 'tp2': tp2, 'tp3': tp3, 'atr': atr}
                tf_data["last_fast_ema_signal"] = fast_cross
                tf_data["last_fast_ema_levels"] = fast_levels
                tf_data["entry_price"] = price
                tf_data["sl"] = sl
                tf_data["tp1"] = tp1
                tf_data["tp2"] = tp2
                tf_data["tp3"] = tp3
                tf_data["signal_type"] = "fast_ema"
                tf_data["tp1_hit"] = tf_data["tp2_hit"] = tf_data["tp3_hit"] = tf_data["sl_hit"] = False
                tf_data["tp1_notified"] = tf_data["tp2_notified"] = tf_data["tp3_notified"] = tf_data["sl_notified"] = False
                fast_signal = fast_cross
            else:
                fast_signal = None
        else:
            fast_signal = None
    else:
        fast_signal = None

    # TP/SL проверка
    if tf_data["entry_price"] is not None and tf_data["sl"] is not None:
        if tf_data["tp1"] is not None:
            is_buy = tf_data["entry_price"] < tf_data["tp1"]
        else:
            is_buy = tf_data["entry_price"] > tf_data["sl"]

        if not tf_data["sl_hit"]:
            if is_buy and price <= tf_data["sl"]:
                tf_data["sl_hit"] = True
                update_signal_event(asset_name, interval, "sl", price)
            elif not is_buy and price >= tf_data["sl"]:
                tf_data["sl_hit"] = True
                update_signal_event(asset_name, interval, "sl", price)
        if tf_data["tp1"] is not None and not tf_data["tp1_hit"]:
            if is_buy and price >= tf_data["tp1"]:
                tf_data["tp1_hit"] = True
                update_signal_event(asset_name, interval, "tp1", price)
            elif not is_buy and price <= tf_data["tp1"]:
                tf_data["tp1_hit"] = True
                update_signal_event(asset_name, interval, "tp1", price)
        if tf_data["tp2"] is not None and not tf_data["tp2_hit"]:
            if is_buy and price >= tf_data["tp2"]:
                tf_data["tp2_hit"] = True
                update_signal_event(asset_name, interval, "tp2", price)
            elif not is_buy and price <= tf_data["tp2"]:
                tf_data["tp2_hit"] = True
                update_signal_event(asset_name, interval, "tp2", price)
        if tf_data["tp3"] is not None and not tf_data["tp3_hit"]:
            if is_buy and price >= tf_data["tp3"]:
                tf_data["tp3_hit"] = True
                update_signal_event(asset_name, interval, "tp3", price)
            elif not is_buy and price <= tf_data["tp3"]:
                tf_data["tp3_hit"] = True
                update_signal_event(asset_name, interval, "tp3", price)

    return (rsi_signal, rsi_levels, current_rsi,
            ema_signal, ema_levels, price, cur_fast, cur_slow,
            combined_signal, combined_levels,
            fast_signal, fast_levels, cur_fast3, cur_slow10, interval)

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
        "Команды:\n"
        "/gold, /btc, /eth, /sol – цена и все сигналы\n"
        "/crypto – сводка\n"
        "/status – последние сигналы по всем активам"
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
        (rsi_signal, rsi_levels, current_rsi,
         ema_signal, ema_levels, _, cur_fast, cur_slow,
         combined_signal, combined_levels,
         fast_signal, fast_levels, cur_fast3, cur_slow10, _) = check_signal(asset_name, tf)
        tf_data = asset["data"][tf]
        
        msg += f"⏱ {tf}\n"
        msg += f"📊 RSI: {current_rsi:.1f}\n" if current_rsi else "📊 RSI: —\n"
        # RSI
        if tf_data["last_rsi_signal"]:
            lv = tf_data["last_rsi_levels"]
            direction = "покупку" if tf_data["last_rsi_signal"] == "BUY" else "продажу"
            msg += f"🔹 Сигнал на {direction} по RSI"
            if lv:
                msg += f" | Вход: {lv['price']:.2f} | SL: {lv['sl']:.2f} | TP1: {lv['tp1']:.2f} | TP2: {lv['tp2']:.2f}"
            msg += "\n"
        else:
            msg += "🔹 RSI: Нет\n"
        # EMA (20/50)
        if tf_data["last_ema_signal"]:
            lv = tf_data["last_ema_levels"]
            direction = "покупку" if tf_data["last_ema_signal"] == "BUY" else "продажу"
            msg += f"🔸 Сигнал на {direction} по EMA (20/50)"
            if lv:
                msg += f" | Вход: {lv['price']:.2f} | SL: {lv['sl']:.2f} | TP1: {lv['tp1']:.2f} | TP2: {lv['tp2']:.2f}"
            else:
                msg += f" (EMA20: {cur_fast:.2f}, EMA50: {cur_slow:.2f})"
            msg += "\n"
        else:
            msg += "🔸 EMA (20/50): Нет\n"
        # Combined
        if tf_data["last_combined_signal"]:
            lv = tf_data["last_combined_levels"]
            direction = "покупку" if tf_data["last_combined_signal"] == "BUY" else "продажу"
            msg += f"🔹 Сигнал на {direction} по RSI+EMA"
            if lv:
                msg += f" | Вход: {lv['price']:.2f} | SL: {lv['sl']:.2f} | TP1: {lv['tp1']:.2f} | TP2: {lv['tp2']:.2f}"
            msg += "\n"
        else:
            msg += "🔹 RSI+EMA: Нет\n"
        # FAST EMA
        if tf_data["last_fast_ema_signal"]:
            lv = tf_data["last_fast_ema_levels"]
            direction = "покупку" if tf_data["last_fast_ema_signal"] == "BUY" else "продажу"
            msg += f"⭐ Сигнал на {direction} по FAST EMA (3/10)"
            if lv:
                msg += f" | Вход: {lv['price']:.2f} | SL: {lv['sl']:.2f} | TP1: {lv['tp1']:.2f} | TP2: {lv['tp2']:.2f} | TP3: {lv['tp3']:.2f}"
            msg += "\n"
        else:
            msg += "⭐ FAST EMA: Нет\n"
        # Позиция
        if tf_data["entry_price"] is not None:
            msg += f"📌 Позиция: {tf_data['signal_type']} | Вход: {tf_data['entry_price']:.2f}"
            if tf_data["sl_hit"]:
                msg += " | ❌ SL сработал"
            else:
                if tf_data["tp1_hit"]:
                    msg += " | ✅ TP1 достигнут"
                if tf_data["tp2_hit"]:
                    msg += " | ✅ TP2 достигнут"
                if tf_data["tp3_hit"]:
                    msg += " | ✅ TP3 достигнут"
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
            rsi_sig = tf_data["last_rsi_signal"] or "Нет"
            ema_sig = tf_data["last_ema_signal"] or "Нет"
            comb_sig = tf_data["last_combined_signal"] or "Нет"
            fast_sig = tf_data["last_fast_ema_signal"] or "Нет"
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
            msg += f"    RSI: {tf_data['last_rsi_signal'] or 'Нет'}\n"
            msg += f"    EMA (20/50): {tf_data['last_ema_signal'] or 'Нет'}\n"
            msg += f"    RSI+EMA: {tf_data['last_combined_signal'] or 'Нет'}\n"
            msg += f"    FAST EMA (3/10): {tf_data['last_fast_ema_signal'] or 'Нет'}\n"
            if tf_data["entry_price"] is not None:
                msg += f"    Позиция: {tf_data['signal_type']} | Вход: {tf_data['entry_price']:.2f}"
                if tf_data["sl_hit"]:
                    msg += " | SL сработал"
                else:
                    if tf_data["tp1_hit"]:
                        msg += " | TP1 достигнут"
                    if tf_data["tp2_hit"]:
                        msg += " | TP2 достигнут"
                    if tf_data["tp3_hit"]:
                        msg += " | TP3 достигнут"
                msg += "\n"
        msg += "\n"
    await update.message.reply_text(msg)

# === Отчёты ===
async def generate_daily_report():
    now = datetime.now()
    yesterday = now - timedelta(days=1)
    events = [e for e in signal_history if e["timestamp"] >= yesterday]
    if not events:
        return "📊 За последние 24 часа сигналов не было."

    stats = {}
    for e in events:
        key = (e["asset"], e["tf"])
        if key not in stats:
            stats[key] = {
                "rsi": {"total": 0, "tp1": 0, "tp2": 0, "tp3": 0, "sl": 0},
                "ema": {"total": 0, "tp1": 0, "tp2": 0, "tp3": 0, "sl": 0},
                "combined": {"total": 0, "tp1": 0, "tp2": 0, "tp3": 0, "sl": 0},
                "fast_ema": {"total": 0, "tp1": 0, "tp2": 0, "tp3": 0, "sl": 0}
            }
        typ = e["type"]
        stats[key][typ]["total"] += 1
        if e["tp1_hit"]:
            stats[key][typ]["tp1"] += 1
        if e["tp2_hit"]:
            stats[key][typ]["tp2"] += 1
        if e["tp3_hit"]:
            stats[key][typ]["tp3"] += 1
        if e["sl_hit"]:
            stats[key][typ]["sl"] += 1

    lines = ["📊 **Ежедневный отчёт за {}**".format(now.strftime("%d.%m.%Y"))]
    lines.append("")
    for (asset, tf), data in stats.items():
        lines.append(f"**{asset}** ({tf}):")
        for sig_type, vals in data.items():
            total = vals["total"]
            if total == 0:
                continue
            tp1 = vals["tp1"]
            tp2 = vals["tp2"]
            tp3 = vals["tp3"]
            sl = vals["sl"]
            closed = tp1 + tp2 + tp3 + sl
            success_rate = (tp1 + tp2 + tp3) / closed * 100 if closed > 0 else 0
            lines.append(f"  {sig_type.upper()}: {total} сигн. | TP1: {tp1} | TP2: {tp2} | TP3: {tp3} | SL: {sl} | Успешность: {success_rate:.1f}%")
        lines.append("")
    return "\n".join(lines)

async def generate_weekly_report():
    now = datetime.now()
    week_ago = now - timedelta(days=7)
    events = [e for e in signal_history if e["timestamp"] >= week_ago]
    if not events:
        return "📊 За последнюю неделю сигналов не было."

    stats = {}
    for e in events:
        key = (e["asset"], e["tf"])
        if key not in stats:
            stats[key] = {
                "rsi": {"total": 0, "tp1": 0, "tp2": 0, "tp3": 0, "sl": 0},
                "ema": {"total": 0, "tp1": 0, "tp2": 0, "tp3": 0, "sl": 0},
                "combined": {"total": 0, "tp1": 0, "tp2": 0, "tp3": 0, "sl": 0},
                "fast_ema": {"total": 0, "tp1": 0, "tp2": 0, "tp3": 0, "sl": 0}
            }
        typ = e["type"]
        stats[key][typ]["total"] += 1
        if e["tp1_hit"]:
            stats[key][typ]["tp1"] += 1
        if e["tp2_hit"]:
            stats[key][typ]["tp2"] += 1
        if e["tp3_hit"]:
            stats[key][typ]["tp3"] += 1
        if e["sl_hit"]:
            stats[key][typ]["sl"] += 1

    lines = ["📊 **Воскресный отчёт за неделю ({} - {})**".format(
        (now - timedelta(days=7)).strftime("%d.%m"), now.strftime("%d.%m.%Y"))]
    lines.append("")
    for (asset, tf), data in stats.items():
        lines.append(f"**{asset}** ({tf}):")
        for sig_type, vals in data.items():
            total = vals["total"]
            if total == 0:
                continue
            tp1 = vals["tp1"]
            tp2 = vals["tp2"]
            tp3 = vals["tp3"]
            sl = vals["sl"]
            closed = tp1 + tp2 + tp3 + sl
            success_rate = (tp1 + tp2 + tp3) / closed * 100 if closed > 0 else 0
            lines.append(f"  {sig_type.upper()}: {total} сигн. | TP1: {tp1} | TP2: {tp2} | TP3: {tp3} | SL: {sl} | Успешность: {success_rate:.1f}%")
        lines.append("")
    return "\n".join(lines)

async def daily_report_task(context: ContextTypes.DEFAULT_TYPE):
    report = await generate_daily_report()
    await send_to_chat(context, report)

async def weekly_report_task(context: ContextTypes.DEFAULT_TYPE):
    report = await generate_weekly_report()
    await send_to_chat(context, report)

# === Принудительная отправка текущих сигналов при старте ===
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
            if rsi_signal and rsi_levels and rsi_signal != tf_data.get("last_rsi_sent"):
                lv = rsi_levels
                stars = get_signal_stars("rsi")
                direction = "покупку" if rsi_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по RSI для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${lv['price']:.2f}\n"
                msg += f"🛑 SL: ${lv['sl']:.2f}\n"
                msg += f"🎯 TP1: ${lv['tp1']:.2f} (1:1)\n"
                msg += f"🎯 TP2: ${lv['tp2']:.2f} (1:2)\n"
                msg += f"📊 RSI: {lv['rsi']:.1f}"
                await send_to_chat(context, msg)
                tf_data["last_rsi_sent"] = rsi_signal
                record_signal_event(name, tf, "rsi", rsi_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'])
            # EMA (20/50)
            if ema_signal and ema_levels and ema_signal != tf_data.get("last_ema_sent"):
                lv = ema_levels
                stars = get_signal_stars("ema")
                direction = "покупку" if ema_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по EMA (20/50) для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${lv['price']:.2f}\n"
                msg += f"🛑 SL: ${lv['sl']:.2f} (ATR×{SL_MULT})\n"
                msg += f"🎯 TP1: ${lv['tp1']:.2f} (ATR×{TP1_MULT})\n"
                msg += f"🎯 TP2: ${lv['tp2']:.2f} (ATR×{TP2_MULT})\n"
                msg += f"📊 ATR: {lv['atr']:.2f}\n"
                msg += f"📊 EMA20: {cur_fast:.2f}, EMA50: {cur_slow:.2f}\n"
                msg += f"🔹 Действие: {ema_signal}"
                await send_to_chat(context, msg)
                tf_data["last_ema_sent"] = ema_signal
                record_signal_event(name, tf, "ema", ema_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'])
            # Combined
            if combined_signal and combined_levels and combined_signal != tf_data.get("last_combined_sent"):
                lv = combined_levels
                stars = get_signal_stars("combined")
                direction = "покупку" if combined_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по RSI+EMA для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${lv['price']:.2f}\n"
                msg += f"🛑 SL: ${lv['sl']:.2f}\n"
                msg += f"🎯 TP1: ${lv['tp1']:.2f} (1:1)\n"
                msg += f"🎯 TP2: ${lv['tp2']:.2f} (1:2)\n"
                msg += f"📊 RSI: {lv['rsi']:.1f}\n"
                msg += f"🔹 EMA20: {cur_fast:.2f}, EMA50: {cur_slow:.2f}"
                await send_to_chat(context, msg)
                tf_data["last_combined_sent"] = combined_signal
                record_signal_event(name, tf, "combined", combined_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'])
            # FAST EMA
            if fast_signal and fast_levels and fast_signal != tf_data.get("last_fast_ema_sent"):
                lv = fast_levels
                stars = get_signal_stars("fast_ema")
                direction = "покупку" if fast_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по FAST EMA (3/10) для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${lv['price']:.2f}\n"
                msg += f"🛑 SL: ${lv['sl']:.2f} (ATR×{SL_MULT})\n"
                msg += f"🎯 TP1: ${lv['tp1']:.2f} (ATR×{TP1_MULT})\n"
                msg += f"🎯 TP2: ${lv['tp2']:.2f} (ATR×{TP2_MULT})\n"
                msg += f"🎯 TP3: ${lv['tp3']:.2f} (ATR×{TP3_MULT})\n"
                msg += f"📊 ATR: {lv['atr']:.2f}\n"
                msg += f"📊 EMA3: {cur_fast3:.2f}, EMA10: {cur_slow10:.2f}"
                await send_to_chat(context, msg)
                tf_data["last_fast_ema_sent"] = fast_signal
                record_signal_event(name, tf, "fast_ema", fast_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'], lv['tp3'])

# === Автоматическая проверка ===
async def check_and_send_signal(context: ContextTypes.DEFAULT_TYPE):
    print("⏰ Запущена автоматическая проверка...")
    if CHANNEL_ID is None and chat_id is None:
        return
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

            # --- RSI ---
            if rsi_signal and rsi_levels and rsi_signal != tf_data.get("last_rsi_sent"):
                lv = rsi_levels
                stars = get_signal_stars("rsi")
                direction = "покупку" if rsi_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по RSI для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${lv['price']:.2f}\n"
                msg += f"🛑 SL: ${lv['sl']:.2f}\n"
                msg += f"🎯 TP1: ${lv['tp1']:.2f} (1:1)\n"
                msg += f"🎯 TP2: ${lv['tp2']:.2f} (1:2)\n"
                msg += f"📊 RSI: {lv['rsi']:.1f}"
                await send_to_chat(context, msg)
                tf_data["last_rsi_sent"] = rsi_signal
                record_signal_event(name, tf, "rsi", rsi_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'])

            # --- EMA (20/50) ---
            if ema_signal and ema_levels and ema_signal != tf_data.get("last_ema_sent"):
                lv = ema_levels
                stars = get_signal_stars("ema")
                direction = "покупку" if ema_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по EMA (20/50) для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${lv['price']:.2f}\n"
                msg += f"🛑 SL: ${lv['sl']:.2f} (ATR×{SL_MULT})\n"
                msg += f"🎯 TP1: ${lv['tp1']:.2f} (ATR×{TP1_MULT})\n"
                msg += f"🎯 TP2: ${lv['tp2']:.2f} (ATR×{TP2_MULT})\n"
                msg += f"📊 ATR: ${lv['atr']:.2f}\n"
                msg += f"📊 EMA20: {cur_fast:.2f}, EMA50: {cur_slow:.2f}\n"
                msg += f"🔹 Действие: {ema_signal}"
                await send_to_chat(context, msg)
                tf_data["last_ema_sent"] = ema_signal
                record_signal_event(name, tf, "ema", ema_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'])

            # --- Combined ---
            if combined_signal and combined_levels and combined_signal != tf_data.get("last_combined_sent"):
                lv = combined_levels
                stars = get_signal_stars("combined")
                direction = "покупку" if combined_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по RSI+EMA для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${lv['price']:.2f}\n"
                msg += f"🛑 SL: ${lv['sl']:.2f}\n"
                msg += f"🎯 TP1: ${lv['tp1']:.2f} (1:1)\n"
                msg += f"🎯 TP2: ${lv['tp2']:.2f} (1:2)\n"
                msg += f"📊 RSI: {lv['rsi']:.1f}\n"
                msg += f"🔹 EMA20: {cur_fast:.2f}, EMA50: {cur_slow:.2f}"
                await send_to_chat(context, msg)
                tf_data["last_combined_sent"] = combined_signal
                record_signal_event(name, tf, "combined", combined_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'])

            # --- FAST EMA ---
            if fast_signal and fast_levels and fast_signal != tf_data.get("last_fast_ema_sent"):
                lv = fast_levels
                stars = get_signal_stars("fast_ema")
                direction = "покупку" if fast_signal == "BUY" else "продажу"
                msg = f"{stars} 📢 Сигнал на {direction} по FAST EMA (3/10) для {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${lv['price']:.2f}\n"
                msg += f"🛑 SL: ${lv['sl']:.2f} (ATR×{SL_MULT})\n"
                msg += f"🎯 TP1: ${lv['tp1']:.2f} (ATR×{TP1_MULT})\n"
                msg += f"🎯 TP2: ${lv['tp2']:.2f} (ATR×{TP2_MULT})\n"
                msg += f"🎯 TP3: ${lv['tp3']:.2f} (ATR×{TP3_MULT})\n"
                msg += f"📊 ATR: ${lv['atr']:.2f}\n"
                msg += f"📊 EMA3: {cur_fast3:.2f}, EMA10: {cur_slow10:.2f}"
                await send_to_chat(context, msg)
                tf_data["last_fast_ema_sent"] = fast_signal
                record_signal_event(name, tf, "fast_ema", fast_signal, lv['price'], lv['sl'], lv['tp1'], lv['tp2'], lv['tp3'])

            # --- TP/SL уведомления ---
            if tf_data["entry_price"] is not None and tf_data["signal_type"]:
                if tf_data["sl_hit"] and not tf_data.get("sl_notified"):
                    msg = f"❌ Стоп-лосс сработал по {name} [{tf}]\nВход: ${tf_data['entry_price']:.2f}\nSL: ${tf_data['sl']:.2f}"
                    await send_to_chat(context, msg)
                    tf_data["sl_notified"] = True
                if tf_data["tp1_hit"] and not tf_data.get("tp1_notified"):
                    msg = f"✅ TP1 достигнут по {name} [{tf}]\nВход: ${tf_data['entry_price']:.2f}\nTP1: ${tf_data['tp1']:.2f}"
                    await send_to_chat(context, msg)
                    tf_data["tp1_notified"] = True
                if tf_data["tp2_hit"] and not tf_data.get("tp2_notified"):
                    msg = f"✅ TP2 достигнут по {name} [{tf}]\nВход: ${tf_data['entry_price']:.2f}\nTP2: ${tf_data['tp2']:.2f}"
                    await send_to_chat(context, msg)
                    tf_data["tp2_notified"] = True
                if tf_data["tp3_hit"] and not tf_data.get("tp3_notified"):
                    msg = f"✅ TP3 достигнут по {name} [{tf}]\nВход: ${tf_data['entry_price']:.2f}\nTP3: ${tf_data['tp3']:.2f}"
                    await send_to_chat(context, msg)
                    tf_data["tp3_notified"] = True

# === Планировщик ===
def start_scheduler(context: ContextTypes.DEFAULT_TYPE):
    if context.job_queue is None:
        print("⚠️ JobQueue не доступен. Установите apscheduler.")
        return
    for job in context.job_queue.jobs():
        job.schedule_removal()
    context.job_queue.run_repeating(check_and_send_signal, interval=60, first=10)
    context.job_queue.run_daily(daily_report_task, time=dt_time(hour=22, minute=0), days=tuple(range(7)))
    context.job_queue.run_daily(weekly_report_task, time=dt_time(hour=19, minute=0), days=(6,))
    print("📅 Планировщик запущен (проверка каждую минуту, отчёты: ежедневно в 22:00, по воскресеньям в 19:00)")

# === Запуск ===
def run_bot():
    print("🤖 Бот запускается...")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gold", gold))
    app.add_handler(CommandHandler("btc", btc))
    app.add_handler(CommandHandler("eth", eth))
    app.add_handler(CommandHandler("sol", sol))
    app.add_handler(CommandHandler("crypto", crypto))
    app.add_handler(CommandHandler("status", status))

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
    print("✅ Бот готов, запускаем поллинг...")

    # Запускаем планировщик внутри обработчика start, но чтобы не ждать /start, можно сразу вызвать
    # Создадим контекст и вызовем start_scheduler. Для этого нужно получить app.
    # Но проще всего использовать обработчик /start. Поэтому пользователь должен отправить /start один раз.
    # Если хотите автоматический запуск без /start – используйте фоновый цикл (был в предыдущей версии).
    # Пока оставляем как есть.

    app.run_polling(drop_pending_updates=True)

def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()

if __name__ == "__main__":
    main()
