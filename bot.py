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

# === Список активов и их последние сигналы (RSI и EMA) ===
ASSETS = {
    "GOLD": {
        "symbol": "XAUT-USDT",
        "last_rsi_signal": None,
        "last_rsi_levels": None,
        "last_ema_signal": None,
        "last_ema_price": None
    },
    "BTC": {
        "symbol": "BTC-USDT",
        "last_rsi_signal": None,
        "last_rsi_levels": None,
        "last_ema_signal": None,
        "last_ema_price": None
    },
    "ETH": {
        "symbol": "ETH-USDT",
        "last_rsi_signal": None,
        "last_rsi_levels": None,
        "last_ema_signal": None,
        "last_ema_price": None
    },
    "SOL": {
        "symbol": "SOL-USDT",
        "last_rsi_signal": None,
        "last_rsi_levels": None,
        "last_ema_signal": None,
        "last_ema_price": None
    },
}

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
TIMEFRAME = "15m"
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

def get_rsi_and_bars(symbol):
    df = get_klines(symbol, interval=TIMEFRAME, limit=LOOKBACK)
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

def get_ema_cross(symbol):
    """Возвращает текущие значения EMA20, EMA50, предыдущие значения и сигнал пересечения."""
    df = get_klines(symbol, interval=TIMEFRAME, limit=LOOKBACK)
    if df is None or len(df) < EMA_SLOW:
        return None, None, None, None
    close = df['Close'].values
    ema_fast = ema_indicator(close, EMA_FAST)
    ema_slow = ema_indicator(close, EMA_SLOW)
    # Текущие значения
    cur_fast = ema_fast[-1]
    cur_slow = ema_slow[-1]
    # Предыдущие значения
    prev_fast = ema_fast[-2] if len(ema_fast) > 1 else cur_fast
    prev_slow = ema_slow[-2] if len(ema_slow) > 1 else cur_slow
    signal = None
    # Быстрое пересекает медленную снизу вверх (BUY)
    if prev_fast <= prev_slow and cur_fast > cur_slow:
        signal = "BUY"
    # Быстрое пересекает медленную сверху вниз (SELL)
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

def check_signal(asset_name):
    asset = ASSETS[asset_name]
    symbol = asset["symbol"]
    price = get_current_price(symbol)
    if price is None:
        return None, None, None, None, None, None, None, None

    # --- RSI сигнал ---
    current_rsi, prev_rsi, high, low = get_rsi_and_bars(symbol)
    rsi_signal = None
    rsi_levels = None
    if current_rsi is not None and prev_rsi is not None:
        if prev_rsi < RSI_OVERSOLD and current_rsi >= RSI_OVERSOLD:
            rsi_signal = "BUY"
        elif prev_rsi > RSI_OVERBOUGHT and current_rsi <= RSI_OVERBOUGHT:
            rsi_signal = "SELL"
        if rsi_signal and rsi_signal != asset["last_rsi_signal"]:
            sl, tp1, tp2 = calculate_levels(price, high, low, rsi_signal)
            rsi_levels = {
                'price': price,
                'sl': sl,
                'tp1': tp1,
                'tp2': tp2,
                'rsi': current_rsi
            }
            asset["last_rsi_signal"] = rsi_signal
            asset["last_rsi_levels"] = rsi_levels
        else:
            # Если сигнал не изменился, не обновляем уровни
            pass

    # --- EMA сигнал ---
    ema_signal, cur_fast, cur_slow, prev_fast, prev_slow = get_ema_cross(symbol)
    if ema_signal and ema_signal != asset["last_ema_signal"]:
        asset["last_ema_signal"] = ema_signal
        asset["last_ema_price"] = price
        # EMA сигнал без уровней (только цена)
    else:
        ema_signal = None  # не отправляем повторный сигнал

    # Возвращаем оба сигнала и вспомогательные данные
    return (rsi_signal, rsi_levels, current_rsi,
            ema_signal, price, cur_fast, cur_slow)

# === Команды ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "👋 Бот запущен!\n"
        "Отслеживаю: GOLD (XAUT), BTC, ETH, SOL.\n"
        "Сигналы двух типов:\n"
        "📈 RSI сигнал – при пересечении 30/70 (с уровнями SL/TP1/TP2)\n"
        "📉 EMA сигнал – кроссовер EMA20/EMA50 (без уровней)\n\n"
        "Команды:\n"
        "/gold, /btc, /eth, /sol – цена, RSI и оба сигнала\n"
        "/crypto – сводка по всем активам\n"
        "/status – последние RSI и EMA сигналы по золоту"
    )
    start_scheduler(context)

async def asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, asset_name):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"⏳ Загружаю данные по {asset_name}...")
    await asyncio.sleep(random.uniform(0.5, 1.5))
    
    rsi_signal, rsi_levels, current_rsi, ema_signal, price, cur_fast, cur_slow = check_signal(asset_name)
    asset = ASSETS[asset_name]
    if price is None or current_rsi is None:
        await update.message.reply_text(f"❌ Не удалось получить данные по {asset_name}. Попробуйте позже.")
        return

    msg = f"💰 {asset_name} ({asset['symbol']}): ${price:.2f}\n"
    msg += f"📊 RSI (14): {current_rsi:.1f}\n"

    # RSI сигнал
    if asset["last_rsi_signal"]:
        lv = asset["last_rsi_levels"]
        msg += f"\n🔹 RSI сигнал: {asset['last_rsi_signal']}"
        if lv:
            msg += f"\n   Вход: ${lv['price']:.2f}"
            msg += f"\n   🛑 SL: ${lv['sl']:.2f}"
            msg += f"\n   🎯 TP1: ${lv['tp1']:.2f} (1:1)"
            msg += f"\n   🎯 TP2: ${lv['tp2']:.2f} (1:2)"
    else:
        msg += "\n🔹 RSI сигнал: Нет"

    # EMA сигнал
    if asset["last_ema_signal"]:
        msg += f"\n🔸 EMA сигнал ({EMA_FAST}/{EMA_SLOW}): {asset['last_ema_signal']}"
        msg += f"\n   EMA{EMA_FAST}: {cur_fast:.2f}, EMA{EMA_SLOW}: {cur_slow:.2f}"
    else:
        msg += f"\n🔸 EMA сигнал: Нет"

    await update.message.reply_text(msg)

async def gold(update: Update, context): await asset_cmd(update, context, "GOLD")
async def btc(update: Update, context): await asset_cmd(update, context, "BTC")
async def eth(update: Update, context): await asset_cmd(update, context, "ETH")
async def sol(update: Update, context): await asset_cmd(update, context, "SOL")

async def crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Загружаю сводку по всем активам...")
    await asyncio.sleep(1)
    msg = "📊 СВОДКА ПО АКТИВАМ (15м):\n\n"
    for name in ASSETS:
        symbol = ASSETS[name]["symbol"]
        price = get_current_price(symbol)
        rsi, _, _, _ = get_rsi_and_bars(symbol)
        if price is not None and rsi is not None:
            rsi_sig = ASSETS[name]["last_rsi_signal"] or "Нет"
            ema_sig = ASSETS[name]["last_ema_signal"] or "Нет"
            msg += f"**{name}** ({symbol}): ${price:.2f}  |  RSI: {rsi:.1f}  |  RSI сигнал: {rsi_sig}  |  EMA сигнал: {ema_sig}\n"
        else:
            msg += f"**{name}**: данные недоступны\n"
    await update.message.reply_text(msg)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asset = ASSETS["GOLD"]
    msg = "📌 ПОСЛЕДНИЕ СИГНАЛЫ ПО GOLD:\n"
    if asset["last_rsi_signal"]:
        lv = asset["last_rsi_levels"]
        msg += f"\n🔹 RSI: {asset['last_rsi_signal']} @ ${lv['price']:.2f}, SL: ${lv['sl']:.2f}, TP1: ${lv['tp1']:.2f}, TP2: ${lv['tp2']:.2f}"
    else:
        msg += "\n🔹 RSI: нет сигнала"
    if asset["last_ema_signal"]:
        msg += f"\n🔸 EMA: {asset['last_ema_signal']} @ ${asset['last_ema_price']:.2f}"
    else:
        msg += "\n🔸 EMA: нет сигнала"
    await update.message.reply_text(msg)

async def check_and_send_signal(context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    if chat_id is None:
        return
    for name in ASSETS:
        print(f"🔍 Проверка {name}...")
        rsi_signal, rsi_levels, current_rsi, ema_signal, price, cur_fast, cur_slow = check_signal(name)
        # Отправляем RSI сигнал, если он новый и есть уровни
        if rsi_signal and rsi_levels:
            lv = rsi_levels
            emoji = "📈" if rsi_signal == "BUY" else "📉"
            msg = f"{emoji} RSI СИГНАЛ НА {name} ({ASSETS[name]['symbol']}) (15м)\n"
            msg += f"💰 Вход: ${lv['price']:.2f}\n"
            msg += f"🛑 SL: ${lv['sl']:.2f}\n"
            msg += f"🎯 TP1: ${lv['tp1']:.2f} (1:1)\n"
            msg += f"🎯 TP2: ${lv['tp2']:.2f} (1:2)\n"
            msg += f"📊 RSI: {lv['rsi']:.1f}"
            await context.bot.send_message(chat_id=chat_id, text=msg)
        # Отправляем EMA сигнал, если он новый
        if ema_signal:
            emoji = "📈" if ema_signal == "BUY" else "📉"
            msg = f"{emoji} EMA СИГНАЛ НА {name} ({ASSETS[name]['symbol']}) (15м)\n"
            msg += f"💰 Цена: ${price:.2f}\n"
            msg += f"📊 EMA{EMA_FAST}: {cur_fast:.2f}, EMA{EMA_SLOW}: {cur_slow:.2f}\n"
            msg += f"🔹 Действие: {ema_signal}"
            await context.bot.send_message(chat_id=chat_id, text=msg)

async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    if chat_id is None:
        return
    msg = "📊 ПЛАНОВЫЙ ОТЧЁТ (15м)\n\n"
    for name in ASSETS:
        symbol = ASSETS[name]["symbol"]
        price = get_current_price(symbol)
        rsi, _, _, _ = get_rsi_and_bars(symbol)
        if price is not None and rsi is not None:
            rsi_sig = ASSETS[name]["last_rsi_signal"] or "Нет"
            ema_sig = ASSETS[name]["last_ema_signal"] or "Нет"
            msg += f"**{name}** ({symbol}): ${price:.2f}  |  RSI: {rsi:.1f}  |  RSI: {rsi_sig}  |  EMA: {ema_sig}\n"
        else:
            msg += f"**{name}**: данные недоступны\n"
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
