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

# === Список активов и их последние сигналы ===
ASSETS = {
    "GOLD":   {"symbol": "XAUT-USDT", "last_signal": None, "last_levels": None},
    "BTC":    {"symbol": "BTC-USDT",  "last_signal": None, "last_levels": None},
    "ETH":    {"symbol": "ETH-USDT",  "last_signal": None, "last_levels": None},
    "SOL":    {"symbol": "SOL-USDT",  "last_signal": None, "last_levels": None},
}

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
TIMEFRAME = "15m"
LOOKBACK = 50
BARS_FOR_LEVELS = 10

# === Функция получения цены для любого символа ===
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

# === Функция получения свечей для любого символа ===
def get_klines(symbol, interval="15m", limit=100):
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
            # Убираем строки с NaN (если есть)
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            return df
        else:
            return None
    except Exception as e:
        print(f"❌ Ошибка klines {symbol}: {e}")
        return None

# === Расчёт RSI и баров для любого символа ===
def get_rsi_and_bars(symbol, retries=3, base_delay=5):
    for attempt in range(retries):
        try:
            df = get_klines(symbol, interval=TIMEFRAME, limit=LOOKBACK)
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

            return current_rsi, prev_rsi, high, low
        except Exception as e:
            print(f"Ошибка RSI {symbol} (попытка {attempt+1}): {e}")
            time.sleep(base_delay * (attempt + 1))
    return None, None, None, None

# === Расчёт уровней ===
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

# === Проверка сигнала для конкретного актива ===
def check_signal(asset_name):
    asset = ASSETS[asset_name]
    symbol = asset["symbol"]
    price = get_current_price(symbol)
    if price is None:
        return None, None, None, None, None, None

    current_rsi, prev_rsi, high, low = get_rsi_and_bars(symbol)
    if current_rsi is None or prev_rsi is None:
        return None, None, None, None, None, None

    signal = None
    if prev_rsi < RSI_OVERSOLD and current_rsi >= RSI_OVERSOLD:
        signal = "BUY"
    elif prev_rsi > RSI_OVERBOUGHT and current_rsi <= RSI_OVERBOUGHT:
        signal = "SELL"

    if signal and signal != asset["last_signal"]:
        asset["last_signal"] = signal
        sl, tp1, tp2 = calculate_levels(price, high, low, signal)
        asset["last_levels"] = {
            'price': price,
            'sl': sl,
            'tp1': tp1,
            'tp2': tp2,
            'rsi': current_rsi,
            'prev_rsi': prev_rsi
        }
        return signal, price, current_rsi, sl, tp1, tp2
    return None, price, current_rsi, None, None, None

# === Команда /start ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "👋 Бот запущен!\n"
        "Отслеживаю: GOLD (XAUT), BTC, ETH, SOL.\n"
        "Команды:\n"
        "/gold – цена и RSI золота\n"
        "/btc, /eth, /sol – по монете\n"
        "/crypto – сводка по всем активам\n"
        "/status – последний сигнал (золото)\n"
        "Сигналы приходят автоматически при пересечении RSI уровней 30/70."
    )
    start_scheduler(context)

# === Команда для каждого актива ===
async def asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, asset_name):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"⏳ Загружаю данные по {asset_name}...")
    await asyncio.sleep(random.uniform(0.5, 1.5))
    signal, price, rsi, sl, tp1, tp2 = check_signal(asset_name)
    asset = ASSETS[asset_name]
    if price is not None and rsi is not None:
        msg = f"💰 {asset_name} ({asset['symbol']}): ${price:.2f}\n📊 RSI (14): {rsi:.1f}"
        if asset["last_signal"]:
            msg += f"\n📌 Последний сигнал: {asset['last_signal']}"
            if asset["last_levels"]:
                lv = asset["last_levels"]
                msg += f"\n--- Уровни ---"
                msg += f"\nВход: ${lv['price']:.2f}"
                msg += f"\n🛑 Стоп-лосс: ${lv['sl']:.2f}"
                msg += f"\n🎯 TP1: ${lv['tp1']:.2f} (1:1)"
                msg += f"\n🎯 TP2: ${lv['tp2']:.2f} (1:2)"
        else:
            msg += "\n📌 Последний сигнал: Нет сигнала"
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text(f"❌ Не удалось получить данные по {asset_name}. Попробуйте позже.")

async def gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asset_cmd(update, context, "GOLD")

async def btc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asset_cmd(update, context, "BTC")

async def eth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asset_cmd(update, context, "ETH")

async def sol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asset_cmd(update, context, "SOL")

# === Сводка по всем активам ===
async def crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Загружаю сводку по всем активам...")
    await asyncio.sleep(1)
    msg = "📊 СВОДКА ПО АКТИВАМ (RSI 15м):\n\n"
    for name in ASSETS:
        symbol = ASSETS[name]["symbol"]
        price = get_current_price(symbol)
        rsi, _, _, _ = get_rsi_and_bars(symbol)
        if price is not None and rsi is not None:
            last_sig = ASSETS[name]["last_signal"] or "Нет"
            msg += f"**{name}** ({symbol}): ${price:.2f}  |  RSI: {rsi:.1f}  |  Сигнал: {last_sig}\n"
        else:
            msg += f"**{name}**: данные недоступны\n"
    await update.message.reply_text(msg)

# === Статус последнего сигнала (золото) ===
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asset = ASSETS["GOLD"]
    if asset["last_signal"] and asset["last_levels"]:
        lv = asset["last_levels"]
        msg = f"📌 Последний сигнал по GOLD: {asset['last_signal']}\n"
        msg += f"💰 Вход: ${lv['price']:.2f}\n"
        msg += f"🛑 Стоп-лосс: ${lv['sl']:.2f}\n"
        msg += f"🎯 TP1: ${lv['tp1']:.2f} (1:1)\n"
        msg += f"🎯 TP2: ${lv['tp2']:.2f} (1:2)\n"
        msg += f"📊 RSI на момент: {lv['rsi']:.1f}"
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("Нет активного сигнала по GOLD.")

# === Автоматическая проверка для всех активов ===
async def check_and_send_signal(context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    if chat_id is None:
        return
    for name in ASSETS:
        print(f"🔍 Проверка {name}...")
        signal, price, rsi, sl, tp1, tp2 = check_signal(name)
        if signal and price is not None and rsi is not None:
            emoji = "📈" if signal == "BUY" else "📉"
            msg = f"{emoji} ПОЛУЧЕН СИГНАЛ НА {name} ({ASSETS[name]['symbol']}) (15 мин)\n\n"
            msg += f"💰 Вход: ${price:.2f}\n"
            msg += f"🛑 Стоп-лосс: ${sl:.2f}\n"
            msg += f"🎯 TP1: ${tp1:.2f} (1:1)\n"
            msg += f"🎯 TP2: ${tp2:.2f} (1:2)\n"
            msg += f"📊 RSI (14): {rsi:.1f}"
            await context.bot.send_message(chat_id=chat_id, text=msg)

# === Плановый отчёт по всем активам ===
async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    if chat_id is None:
        return
    msg = "📊 ПЛАНОВЫЙ ОТЧЁТ (15 мин)\n\n"
    for name in ASSETS:
        symbol = ASSETS[name]["symbol"]
        price = get_current_price(symbol)
        rsi, _, _, _ = get_rsi_and_bars(symbol)
        if price is not None and rsi is not None:
            last_sig = ASSETS[name]["last_signal"] or "Нет"
            msg += f"**{name}** ({symbol}): ${price:.2f}  |  RSI: {rsi:.1f}  |  Сигнал: {last_sig}\n"
        else:
            msg += f"**{name}**: данные недоступны\n"
    await context.bot.send_message(chat_id=chat_id, text=msg)

# === Планировщик ===
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
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gold", gold))
    app.add_handler(CommandHandler("btc", btc))
    app.add_handler(CommandHandler("eth", eth))
    app.add_handler(CommandHandler("sol", sol))
    app.add_handler(CommandHandler("crypto", crypto))
    app.add_handler(CommandHandler("status", status))

    # Тестовый запуск для проверки
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
