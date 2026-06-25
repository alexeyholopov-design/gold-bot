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
from datetime import time as dt_time

TOKEN = "8538708990:AAFC3rk1Z82IP5q5DJCsg2bU9z70uvFBalI"

app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    return "Bot is running!", 200

def run_flask():
    app_flask.run(host='0.0.0.0', port=10000)

chat_id = None

# === Конфигурация активов и таймфреймов ===
TIMEFRAMES = ["5m", "15m"]

# Для каждого актива и каждого ТФ храним все данные
ASSETS = {
    "GOLD": {"symbol": "XAUT-USDT", "data": {}},
    "BTC":  {"symbol": "BTC-USDT",  "data": {}},
    "ETH":  {"symbol": "ETH-USDT",  "data": {}},
    "SOL":  {"symbol": "SOL-USDT",  "data": {}},
}

# Инициализируем структуру для каждого ТФ
for asset in ASSETS:
    for tf in TIMEFRAMES:
        ASSETS[asset]["data"][tf] = {
            "last_rsi_signal": None,
            "last_rsi_levels": None,
            "last_rsi_sent": None,
            "last_ema_signal": None,
            "last_ema_price": None,
            "last_ema_sent": None,
            "last_combined_signal": None,
            "last_combined_levels": None,
            "last_combined_sent": None,
            # Для отслеживания TP/SL
            "entry_price": None,
            "sl": None,
            "tp1": None,
            "tp2": None,
            "tp1_hit": False,
            "tp2_hit": False,
            "sl_hit": False,
            "signal_type": None,   # 'rsi' или 'combined' (только для них есть уровни)
        }

# Параметры индикаторов (общие)
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
LOOKBACK = 50
BARS_FOR_LEVELS = 10
EMA_FAST = 20
EMA_SLOW = 50

# === Функции работы с BingX ===
def get_current_price(symbol):
    try:
        url = "https://open-api.bingx.com/openApi/swap/v2/quote/price"
        params = {"symbol": symbol}
        response = requests.get(url, params=params, timeout=5)
        if response.status_code != 200:
            print(f"❌ HTTP {response.status_code} для {symbol}")
            return None
        data = response.json()
        if data.get("code") == 0:
            price = float(data["data"]["price"])
            print(f"💰 {symbol}: ${price:.2f}")
            return price
        else:
            print(f"❌ {symbol} ошибка: {data.get('msg')}")
            return None
    except Exception as e:
        print(f"❌ Ошибка {symbol}: {e}")
        return None

def get_klines(symbol, interval, limit=100):
    try:
        url = "https://open-api.bingx.com/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        response = requests.get(url, params=params, timeout=5)
        if response.status_code != 200:
            return None
        data = response.json()
        if data.get("code") == 0:
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
        else:
            return None
    except Exception as e:
        print(f"❌ Ошибка klines {symbol}: {e}")
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

def get_ema_cross(symbol, interval):
    df = get_klines(symbol, interval=interval, limit=LOOKBACK)
    if df is None or len(df) < EMA_SLOW:
        return None, None, None, None, None
    close = df['Close'].values
    ema_fast = ema_indicator(close, EMA_FAST)
    ema_slow = ema_indicator(close, EMA_SLOW)
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
    return sl, tp1, tp2

def check_signal(asset_name, interval):
    asset = ASSETS[asset_name]
    symbol = asset["symbol"]
    tf_data = asset["data"][interval]
    price = get_current_price(symbol)
    if price is None:
        return (None, None, None, None, None, None, None, None, None, None)

    # --- RSI ---
    current_rsi, prev_rsi, high, low = get_rsi_and_bars(symbol, interval)
    rsi_signal = None
    rsi_levels = None
    if current_rsi is not None and prev_rsi is not None:
        if prev_rsi < RSI_OVERSOLD and current_rsi >= RSI_OVERSOLD:
            rsi_signal = "BUY"
        elif prev_rsi > RSI_OVERBOUGHT and current_rsi <= RSI_OVERBOUGHT:
            rsi_signal = "SELL"
        if rsi_signal and rsi_signal != tf_data["last_rsi_signal"]:
            sl, tp1, tp2 = calculate_levels(price, high, low, rsi_signal)
            rsi_levels = {
                'price': price,
                'sl': sl,
                'tp1': tp1,
                'tp2': tp2,
                'rsi': current_rsi
            }
            tf_data["last_rsi_signal"] = rsi_signal
            tf_data["last_rsi_levels"] = rsi_levels
            # Сбрасываем флаги выполнения при новом сигнале
            tf_data["entry_price"] = price
            tf_data["sl"] = sl
            tf_data["tp1"] = tp1
            tf_data["tp2"] = tp2
            tf_data["tp1_hit"] = False
            tf_data["tp2_hit"] = False
            tf_data["sl_hit"] = False
            tf_data["signal_type"] = "rsi"

    # --- EMA ---
    ema_signal, cur_fast, cur_slow, _, _ = get_ema_cross(symbol, interval)
    if ema_signal and ema_signal != tf_data["last_ema_signal"]:
        tf_data["last_ema_signal"] = ema_signal
        tf_data["last_ema_price"] = price
    else:
        ema_signal = None

    # --- Combined (RSI + EMA) ---
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
                sl, tp1, tp2 = calculate_levels(price, high, low, combined_signal)
                combined_levels = {
                    'price': price,
                    'sl': sl,
                    'tp1': tp1,
                    'tp2': tp2,
                    'rsi': current_rsi
                }
            tf_data["last_combined_signal"] = combined_signal
            tf_data["last_combined_levels"] = combined_levels
            # Обновляем уровни и для комбинированного
            if not rsi_levels:  # если RSI не дал уровней, берём из combined
                tf_data["entry_price"] = price
                tf_data["sl"] = sl
                tf_data["tp1"] = tp1
                tf_data["tp2"] = tp2
                tf_data["tp1_hit"] = False
                tf_data["tp2_hit"] = False
                tf_data["sl_hit"] = False
                tf_data["signal_type"] = "combined"
        else:
            combined_signal = None

    # --- Проверка достижения TP/SL (если есть активный сигнал) ---
    if tf_data["entry_price"] is not None and tf_data["signal_type"]:
        # Проверяем только если есть открытая позиция (сигнал был, но не закрыт)
        # Используем текущую цену для проверки
        if tf_data["sl"] is not None and not tf_data["sl_hit"]:
            if tf_data["signal_type"] == "BUY" and price <= tf_data["sl"]:
                tf_data["sl_hit"] = True
                # Отправим уведомление отдельно
            elif tf_data["signal_type"] == "SELL" and price >= tf_data["sl"]:
                tf_data["sl_hit"] = True
        if tf_data["tp1"] is not None and not tf_data["tp1_hit"]:
            if tf_data["signal_type"] == "BUY" and price >= tf_data["tp1"]:
                tf_data["tp1_hit"] = True
            elif tf_data["signal_type"] == "SELL" and price <= tf_data["tp1"]:
                tf_data["tp1_hit"] = True
        if tf_data["tp2"] is not None and not tf_data["tp2_hit"]:
            if tf_data["signal_type"] == "BUY" and price >= tf_data["tp2"]:
                tf_data["tp2_hit"] = True
            elif tf_data["signal_type"] == "SELL" and price <= tf_data["tp2"]:
                tf_data["tp2_hit"] = True

    return (rsi_signal, rsi_levels, current_rsi,
            ema_signal, price, cur_fast, cur_slow,
            combined_signal, combined_levels, interval)

# === Команды (обновлены с учётом ТФ) ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "👋 Бот запущен!\n"
        "Отслеживаю: GOLD (XAUT), BTC, ETH, SOL.\n"
        "Таймфреймы: 5м и 15м.\n"
        "Три типа сигналов для каждого ТФ:\n"
        "🔹 RSI – пересечение 30/70 (с уровнями SL/TP1/TP2)\n"
        "🔸 EMA – кроссовер EMA20/EMA50 (без уровней)\n"
        "🔹 RSI+EMA – комбинированный (RSI + тренд по EMA) – с уровнями\n\n"
        "Уведомления о достижении TP1, TP2 и SL приходят автоматически.\n\n"
        "Команды:\n"
        "/gold, /btc, /eth, /sol – цена, RSI и все сигналы для обоих ТФ\n"
        "/crypto – сводка по всем активам (последние сигналы)\n"
        "/status – последние сигналы по GOLD (оба ТФ)"
    )
    start_scheduler(context)

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
        # Получаем сигналы для этого ТФ
        (rsi_signal, rsi_levels, current_rsi,
         ema_signal, _, cur_fast, cur_slow,
         combined_signal, combined_levels, _) = check_signal(asset_name, tf)
        tf_data = asset["data"][tf]
        
        msg += f"⏱ {tf}\n"
        msg += f"📊 RSI: {current_rsi:.1f}\n" if current_rsi else "📊 RSI: —\n"
        # RSI
        if tf_data["last_rsi_signal"]:
            lv = tf_data["last_rsi_levels"]
            msg += f"🔹 RSI: {tf_data['last_rsi_signal']}"
            if lv:
                msg += f" | Вход: {lv['price']:.2f} | SL: {lv['sl']:.2f} | TP1: {lv['tp1']:.2f} | TP2: {lv['tp2']:.2f}"
            msg += "\n"
        else:
            msg += "🔹 RSI: Нет\n"
        # EMA
        if tf_data["last_ema_signal"]:
            msg += f"🔸 EMA: {tf_data['last_ema_signal']} (EMA{EMA_FAST}: {cur_fast:.2f}, EMA{EMA_SLOW}: {cur_slow:.2f})\n"
        else:
            msg += "🔸 EMA: Нет\n"
        # Combined
        if tf_data["last_combined_signal"]:
            lv = tf_data["last_combined_levels"]
            msg += f"🔹 RSI+EMA: {tf_data['last_combined_signal']}"
            if lv:
                msg += f" | Вход: {lv['price']:.2f} | SL: {lv['sl']:.2f} | TP1: {lv['tp1']:.2f} | TP2: {lv['tp2']:.2f}"
            msg += "\n"
        else:
            msg += "🔹 RSI+EMA: Нет\n"
        # Статус TP/SL
        if tf_data["entry_price"] is not None:
            msg += f"📌 Позиция: {tf_data['signal_type']} | Вход: {tf_data['entry_price']:.2f}"
            if tf_data["sl_hit"]:
                msg += " | ❌ SL сработал"
            else:
                if tf_data["tp1_hit"]:
                    msg += " | ✅ TP1 достигнут"
                if tf_data["tp2_hit"]:
                    msg += " | ✅ TP2 достигнут"
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
            msg += f"  {tf}: RSI={rsi_sig}, EMA={ema_sig}, RSI+EMA={comb_sig}\n"
        msg += "\n"
    await update.message.reply_text(msg)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asset = ASSETS["GOLD"]
    msg = "📌 ПОСЛЕДНИЕ СИГНАЛЫ ПО GOLD:\n\n"
    for tf in TIMEFRAMES:
        tf_data = asset["data"][tf]
        msg += f"⏱ {tf}\n"
        msg += f"RSI: {tf_data['last_rsi_signal'] or 'Нет'}\n"
        msg += f"EMA: {tf_data['last_ema_signal'] or 'Нет'}\n"
        msg += f"RSI+EMA: {tf_data['last_combined_signal'] or 'Нет'}\n"
        if tf_data["entry_price"] is not None:
            msg += f"Позиция: {tf_data['signal_type']} | Вход: {tf_data['entry_price']:.2f}"
            if tf_data["sl_hit"]:
                msg += " | SL сработал"
            elif tf_data["tp1_hit"] and tf_data["tp2_hit"]:
                msg += " | TP1 и TP2 достигнуты"
            elif tf_data["tp1_hit"]:
                msg += " | TP1 достигнут"
            msg += "\n"
        msg += "\n"
    await update.message.reply_text(msg)

# === Автоматическая проверка и отправка сигналов + уведомления о TP/SL ===
async def check_and_send_signal(context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    if chat_id is None:
        return
    for name in ASSETS:
        asset = ASSETS[name]
        symbol = asset["symbol"]
        for tf in TIMEFRAMES:
            print(f"🔍 Проверка {name} {tf}...")
            (rsi_signal, rsi_levels, current_rsi,
             ema_signal, price, cur_fast, cur_slow,
             combined_signal, combined_levels, _) = check_signal(name, tf)
            tf_data = asset["data"][tf]

            # --- Отправка новых сигналов ---
            if rsi_signal and rsi_levels and rsi_signal != tf_data.get("last_rsi_sent"):
                lv = rsi_levels
                emoji = "📈" if rsi_signal == "BUY" else "📉"
                msg = f"{emoji} RSI СИГНАЛ НА {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${lv['price']:.2f}\n"
                msg += f"🛑 SL: ${lv['sl']:.2f}\n"
                msg += f"🎯 TP1: ${lv['tp1']:.2f} (1:1)\n"
                msg += f"🎯 TP2: ${lv['tp2']:.2f} (1:2)\n"
                msg += f"📊 RSI: {lv['rsi']:.1f}"
                await context.bot.send_message(chat_id=chat_id, text=msg)
                tf_data["last_rsi_sent"] = rsi_signal

            if ema_signal and ema_signal != tf_data.get("last_ema_sent"):
                emoji = "📈" if ema_signal == "BUY" else "📉"
                msg = f"{emoji} EMA СИГНАЛ НА {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Цена: ${price:.2f}\n"
                msg += f"📊 EMA{EMA_FAST}: {cur_fast:.2f}, EMA{EMA_SLOW}: {cur_slow:.2f}\n"
                msg += f"🔹 Действие: {ema_signal}"
                await context.bot.send_message(chat_id=chat_id, text=msg)
                tf_data["last_ema_sent"] = ema_signal

            if combined_signal and combined_levels and combined_signal != tf_data.get("last_combined_sent"):
                lv = combined_levels
                emoji = "📈" if combined_signal == "BUY" else "📉"
                msg = f"{emoji} RSI+EMA СИГНАЛ НА {name} ({symbol}) [{tf}]\n"
                msg += f"💰 Вход: ${lv['price']:.2f}\n"
                msg += f"🛑 SL: ${lv['sl']:.2f}\n"
                msg += f"🎯 TP1: ${lv['tp1']:.2f} (1:1)\n"
                msg += f"🎯 TP2: ${lv['tp2']:.2f} (1:2)\n"
                msg += f"📊 RSI: {lv['rsi']:.1f}"
                msg += f"\n🔹 EMA{EMA_FAST}: {cur_fast:.2f}, EMA{EMA_SLOW}: {cur_slow:.2f}"
                await context.bot.send_message(chat_id=chat_id, text=msg)
                tf_data["last_combined_sent"] = combined_signal

            # --- Уведомления о TP/SL (срабатывают один раз) ---
            if tf_data["entry_price"] is not None and tf_data["signal_type"]:
                # Проверяем только если есть открытая позиция (entry_price задан)
                if tf_data["sl_hit"] and not tf_data.get("sl_notified"):
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"❌ СТОП-ЛОСС СРАБОТАЛ НА {name} [{tf}]\n"
                             f"Вход: ${tf_data['entry_price']:.2f}\n"
                             f"SL: ${tf_data['sl']:.2f}"
                    )
                    tf_data["sl_notified"] = True
                if tf_data["tp1_hit"] and not tf_data.get("tp1_notified"):
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ TP1 ДОСТИГНУТ НА {name} [{tf}]\n"
                             f"Вход: ${tf_data['entry_price']:.2f}\n"
                             f"TP1: ${tf_data['tp1']:.2f}"
                    )
                    tf_data["tp1_notified"] = True
                if tf_data["tp2_hit"] and not tf_data.get("tp2_notified"):
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ TP2 ДОСТИГНУТ НА {name} [{tf}]\n"
                             f"Вход: ${tf_data['entry_price']:.2f}\n"
                             f"TP2: ${tf_data['tp2']:.2f}"
                    )
                    tf_data["tp2_notified"] = True

# === Плановый отчёт ===
async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    if chat_id is None:
        return
    msg = "📊 ПЛАНОВЫЙ ОТЧЁТ\n\n"
    for name in ASSETS:
        asset = ASSETS[name]
        msg += f"**{name}** ({asset['symbol']})\n"
        for tf in TIMEFRAMES:
            tf_data = asset["data"][tf]
            rsi_sig = tf_data["last_rsi_signal"] or "Нет"
            ema_sig = tf_data["last_ema_signal"] or "Нет"
            comb_sig = tf_data["last_combined_signal"] or "Нет"
            msg += f"  {tf}: RSI={rsi_sig}, EMA={ema_sig}, RSI+EMA={comb_sig}\n"
        msg += "\n"
    await context.bot.send_message(chat_id=chat_id, text=msg)

def start_scheduler(context: ContextTypes.DEFAULT_TYPE):
    if context.job_queue is None:
        print("⚠️ JobQueue не доступен. Установите apscheduler.")
        return
    for job in context.job_queue.jobs():
        job.schedule_removal()
    # Проверка каждые 5 минут (для 5-минутных сигналов)
    context.job_queue.run_repeating(check_and_send_signal, interval=300, first=10)
    # Ежедневные отчёты
    context.job_queue.run_daily(daily_report, time=dt_time(hour=12, minute=0), days=tuple(range(7)))
    context.job_queue.run_daily(daily_report, time=dt_time(hour=18, minute=0), days=tuple(range(7)))
    print("📅 Планировщик запущен (проверка каждые 5 минут)")

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
    print("✅ Бот готов, запускаем поллинг...")
    app.run_polling(drop_pending_updates=True)

def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()

if __name__ == "__main__":
    main()
