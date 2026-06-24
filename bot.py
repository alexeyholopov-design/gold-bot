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

# === НОВЫЙ ТОКЕН (вставьте сюда ваш, полученный от @BotFather) ===
TOKEN = "8538708990:AAFC3rk1Z82IP5q5DJCsg2bU9z70uvFBalI"

if not TOKEN:
    raise ValueError("TOKEN environment variable not set")

app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    return "Bot is running!", 200

def run_flask():
    app_flask.run(host='0.0.0.0', port=10000)

chat_id = None
last_signal = None
last_levels = None

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
TIMEFRAME = "15"
LOOKBACK = 50
BARS_FOR_LEVELS = 10

# === Функции для работы с Bybit (спот XAUTUSDT) ===
def get_current_price():
    try:
        url = "https://api.bybit.com/v5/market/tickers"
        params = {"category": "spot", "symbol": "XAUTUSDT"}
        response = requests.get(url, params=params, timeout=10)
        if response.status_code != 200:
            print(f"HTTP error: {response.status_code}")
            return None
        data = response.json()
        if data["retCode"] == 0:
            price = float(data["result"]["list"][0]["lastPrice"])
            print(f"💰 Bybit XAUTUSDT: ${price:.2f}")
            return price
        else:
            print(f"Ошибка Bybit: {data['retMsg']}")
            return None
    except Exception as e:
        print(f"Ошибка запроса к Bybit: {e}")
        return None

def get_klines(symbol="XAUTUSDT", interval="15", limit=100):
    try:
        url = "https://api.bybit.com/v5/market/kline"
        params = {"category": "spot", "symbol": symbol, "interval": interval, "limit": limit}
        response = requests.get(url, params=params, timeout=10)
        if response.status_code != 200:
            print(f"HTTP error: {response.status_code}")
            return None
        data = response.json()
        if data["retCode"] == 0:
            candles = data["result"]["list"]
            df = pd.DataFrame(candles, columns=["Open", "High", "Low", "Close", "Volume", "Turnover", "Timestamp"])
            df[["Open", "High", "Low", "Close"]] = df[["Open", "High", "Low", "Close"]].astype(float)
            return df
        else:
            print(f"Ошибка Kline Bybit: {data['retMsg']}")
            return None
    except Exception as e:
        print(f"Ошибка запроса Kline к Bybit: {e}")
        return None

def get_rsi_and_bars(ticker_symbol="XAUTUSDT", retries=3, base_delay=5):
    for attempt in range(retries):
        try:
            df = get_klines(symbol=ticker_symbol, interval=TIMEFRAME, limit=LOOKBACK)
            if df is None or len(df) < 2:
                time.sleep(base_delay * (attempt + 1))
                continue

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

            print(f"RSI: {current_rsi:.1f}, high_max: {high.max():.2f}, low_min: {low.min():.2f}")
            return current_rsi, prev_rsi, high, low
        except Exception as e:
            print(f"Ошибка (попытка {attempt+1}): {e}")
            time.sleep(base_delay * (attempt + 1))
    return None, None, None, None

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

def check_signal():
    global last_signal, last_levels
    price = get_current_price()
    if price is None:
        return None, None, None
    current_rsi, prev_rsi, high, low = get_rsi_and_bars()
    if current_rsi is None or prev_rsi is None:
        return None, None, None

    signal = None
    if prev_rsi < RSI_OVERSOLD and current_rsi >= RSI_OVERSOLD:
        signal = "BUY"
    elif prev_rsi > RSI_OVERBOUGHT and current_rsi <= RSI_OVERBOUGHT:
        signal = "SELL"

    if signal and signal != last_signal:
        last_signal = signal
        sl, tp1, tp2 = calculate_levels(price, high, low, signal)
        last_levels = {
            'price': price,
            'sl': sl,
            'tp1': tp1,
            'tp2': tp2,
            'rsi': current_rsi,
            'prev_rsi': prev_rsi
        }
        return signal, price, current_rsi, sl, tp1, tp2
    return None, price, current_rsi, None, None, None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "👋 Бот торговых сигналов запущен!\n"
        "Анализирую золото (XAUTUSDT спот) с Bybit на 15-минутных свечах.\n"
        "Сигналы:\n"
        "📈 BUY  – когда RSI выходит из зоны перепроданности (<30)\n"
        "📉 SELL – когда RSI выходит из зоны перекупленности (>70)\n\n"
        "К каждому сигналу прилагаются уровни входа, стоп-лосс и тейк-профиты.\n\n"
        "Команды:\n"
        "/gold – цена и RSI\n"
        "/status – последний сигнал и уровни"
    )
    start_scheduler(context)

async def gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id, last_signal, last_levels
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Загружаю данные...")
    await asyncio.sleep(random.uniform(0.5, 1.5))
    price = get_current_price()
    current_rsi, prev_rsi, _, _ = get_rsi_and_bars()
    if price is not None and current_rsi is not None:
        signal_text = last_signal if last_signal else "Нет сигнала"
        msg = f"💰 Золото (XAUTUSDT): ${price:.2f}\n📊 RSI (14): {current_rsi:.1f}\n📌 Последний сигнал: {signal_text}"
        if last_levels and last_signal:
            msg += f"\n\n--- Уровни (последний сигнал {last_signal}) ---"
            msg += f"\nВход: ${last_levels['price']:.2f}"
            msg += f"\n🛑 Стоп-лосс: ${last_levels['sl']:.2f}"
            msg += f"\n🎯 TP1: ${last_levels['tp1']:.2f} (1:1)"
            msg += f"\n🎯 TP2: ${last_levels['tp2']:.2f} (1:2)"
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("❌ Не удалось получить данные. Попробуйте позже.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_signal, last_levels
    if last_signal and last_levels:
        msg = f"📌 Последний сигнал: {last_signal}\n"
        msg += f"💰 Цена входа: ${last_levels['price']:.2f}\n"
        msg += f"🛑 Стоп-лосс: ${last_levels['sl']:.2f}\n"
        msg += f"🎯 TP1: ${last_levels['tp1']:.2f} (1:1)\n"
        msg += f"🎯 TP2: ${last_levels['tp2']:.2f} (1:2)\n"
        msg += f"📊 RSI на момент сигнала: {last_levels['rsi']:.1f}"
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("Нет активного сигнала.")

async def check_and_send_signal(context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    if chat_id is None:
        return
    print("🔍 Проверка сигнала...")
    signal, price, rsi, sl, tp1, tp2 = check_signal()
    if signal and price is not None and rsi is not None:
        emoji = "📈" if signal == "BUY" else "📉"
        msg = f"{emoji} ПОЛУЧЕН СИГНАЛ НА {signal} (15 мин)\n\n"
        msg += f"💰 Вход: ${price:.2f}\n"
        msg += f"🛑 Стоп-лосс: ${sl:.2f}\n"
        msg += f"🎯 TP1: ${tp1:.2f} (1:1)\n"
        msg += f"🎯 TP2: ${tp2:.2f} (1:2)\n"
        msg += f"📊 RSI (14): {rsi:.1f}"
        await context.bot.send_message(chat_id=chat_id, text=msg)

async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    global chat_id, last_signal, last_levels
    if chat_id is None:
        return
    price = get_current_price()
    current_rsi, _, _, _ = get_rsi_and_bars()
    if price is not None and current_rsi is not None:
        msg = f"📊 ПЛАНОВЫЙ ОТЧЁТ (15 мин)\n"
        msg += f"💰 Золото (XAUTUSDT): ${price:.2f}\n"
        msg += f"📊 RSI (14): {current_rsi:.1f}\n"
        msg += f"📌 Последний сигнал: {last_signal if last_signal else 'Нет сигнала'}"
        if last_levels and last_signal:
            msg += f"\n--- Уровни последнего сигнала ---"
            msg += f"\nВход: ${last_levels['price']:.2f}"
            msg += f"\n🛑 Стоп-лосс: ${last_levels['sl']:.2f}"
            msg += f"\n🎯 TP1: ${last_levels['tp1']:.2f} (1:1)"
            msg += f"\n🎯 TP2: ${last_levels['tp2']:.2f} (1:2)"
        await context.bot.send_message(chat_id=chat_id, text=msg)

def start_scheduler(context: ContextTypes.DEFAULT_TYPE):
    if context.job_queue is None:
        print("⚠️ JobQueue не доступен. Установите apscheduler.")
        return
    for job in context.job_queue.jobs():
        job.schedule_removal()
    context.job_queue.run_repeating(check_and_send_signal, interval=900, first=10)
    context.job_queue.run_daily(daily_report, time=dt_time(hour=12, minute=0), days=tuple(range(7)))
    context.job_queue.run_daily(daily_report, time=dt_time(hour=18, minute=0), days=tuple(range(7)))
    print("📅 Планировщик запущен (проверка каждые 15 минут)")

def run_bot():
    print("🤖 Бот запускается...")
    app = Application.builder().token(TOKEN).build()
    
    # Принудительный сброс вебхука и удаление висящих обновлений
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(app.bot.delete_webhook(drop_pending_updates=True))
        print("✅ Вебхук сброшен, pending updates удалены")
    except Exception as e:
        print(f"⚠️ Не удалось сбросить вебхук: {e}")
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gold", gold))
    app.add_handler(CommandHandler("status", status))
    print("✅ Бот готов, запускаем поллинг...")
    app.run_polling(drop_pending_updates=True)

def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()

if __name__ == "__main__":
    main()
