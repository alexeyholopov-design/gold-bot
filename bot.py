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

# === Конфигурация для каждого актива ===
ASSET_CONFIG = {
    "GOLD": {
        "symbol": "XAUT-USDT",
        "enabled": True,                  # отправлять сигналы?
        "rsi_period": 14,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "atr_period": 14,
        "atr_sl_mult": 1.5,
        "atr_tp_mult": 2.0,
        "use_trend_filter": True,
        "trend_ma_period": 50,
    },
    "BTC": {
        "symbol": "BTC-USDT",
        "enabled": False,                 # пока отключаем, т.к. не приносит прибыль
        "rsi_period": 14,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "atr_period": 14,
        "atr_sl_mult": 1.5,
        "atr_tp_mult": 2.0,
        "use_trend_filter": True,
        "trend_ma_period": 50,
    },
    "ETH": {
        "symbol": "ETH-USDT",
        "enabled": False,
        "rsi_period": 14,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "atr_period": 14,
        "atr_sl_mult": 1.5,
        "atr_tp_mult": 2.0,
        "use_trend_filter": True,
        "trend_ma_period": 50,
    },
    "SOL": {
        "symbol": "SOL-USDT",
        "enabled": True,
        "rsi_period": 14,
        "rsi_oversold": 20,
        "rsi_overbought": 75,
        "atr_period": 14,
        "atr_sl_mult": 1.0,
        "atr_tp_mult": 3.0,
        "use_trend_filter": False,
        "trend_ma_period": 50,
    },
}

# === Хранилище последних сигналов и уровней для каждого актива ===
last_signals = {name: {"signal": None, "levels": None} for name in ASSET_CONFIG}

TIMEFRAME = "15m"
LOOKBACK = 50
BARS_FOR_LEVELS = 10

# === Функции получения данных от BingX ===
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
        else:
            return None
    except Exception as e:
        print(f"Ошибка цены {symbol}: {e}")
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
        print(f"Ошибка klines {symbol}: {e}")
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
        avg_gain[i] = (avg_gain[i-1] * (period-1) + gain[i]) / period
        avg_loss[i] = (avg_loss[i-1] * (period-1) + loss[i]) / period
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def atr_indicator(high, low, close, period=14):
    high = np.asarray(high)
    low = np.asarray(low)
    close = np.asarray(close)
    tr = np.maximum(high - low, np.maximum(abs(high - np.roll(close, 1)), abs(low - np.roll(close, 1))))
    tr[0] = high[0] - low[0]
    atr = np.zeros_like(tr)
    atr[:period] = np.mean(tr[:period])
    for i in range(period, len(tr)):
        atr[i] = (atr[i-1] * (period-1) + tr[i]) / period
    return atr

def ma_indicator(close, period=50):
    close = np.asarray(close)
    ma = np.zeros_like(close)
    for i in range(period, len(close)):
        ma[i] = np.mean(close[i-period:i])
    ma[:period] = np.nan
    return ma

def get_rsi_atr_ma(symbol, config):
    try:
        df = get_klines(symbol, interval=TIMEFRAME, limit=LOOKBACK)
        if df is None or len(df) < 2:
            return None, None, None, None
        close = df['Close']
        high = df['High']
        low = df['Low']
        rsi = rsi_indicator(close, config['rsi_period'])
        atr = atr_indicator(high, low, close, config['atr_period'])
        ma = ma_indicator(close, config['trend_ma_period'])
        return rsi[-1], rsi[-2] if len(rsi) > 1 else rsi[-1], atr[-1], ma[-1]
    except Exception as e:
        print(f"Ошибка индикаторов {symbol}: {e}")
        return None, None, None, None

def calculate_levels(price, high, low, signal_type, sl_mult, tp_mult):
    if signal_type == "BUY":
        sl = low.min() - sl_mult * (high.max() - low.min())  # упрощённо, но можно и по ATR
        tp1 = price + (price - sl)
        tp2 = price + 2 * (price - sl)
    else:
        sl = high.max() + sl_mult * (high.max() - low.min())
        tp1 = price - (sl - price)
        tp2 = price - 2 * (sl - price)
    return sl, tp1, tp2

def check_signal_for_asset(asset_name):
    config = ASSET_CONFIG[asset_name]
    symbol = config['symbol']
    if not config['enabled']:
        return None, None, None, None, None, None

    price = get_current_price(symbol)
    if price is None:
        return None, None, None, None, None, None

    current_rsi, prev_rsi, atr, ma = get_rsi_atr_ma(symbol, config)
    if current_rsi is None:
        return None, None, None, None, None, None

    # Определяем сигнал
    signal = None
    if prev_rsi < config['rsi_oversold'] and current_rsi >= config['rsi_oversold']:
        if not config['use_trend_filter'] or price > ma:
            signal = "BUY"
    elif prev_rsi > config['rsi_overbought'] and current_rsi <= config['rsi_overbought']:
        if not config['use_trend_filter'] or price < ma:
            signal = "SELL"

    if signal is None:
        return None, price, current_rsi, None, None, None

    # Рассчитываем уровни на основе ATR
    sl = price - (config['atr_sl_mult'] * atr) if signal == "BUY" else price + (config['atr_sl_mult'] * atr)
    tp1 = price + (config['atr_tp_mult'] * atr) if signal == "BUY" else price - (config['atr_tp_mult'] * atr)
    # Для TP2 используем удвоенный TP1 (можно настроить)
    tp2 = price + 2 * (config['atr_tp_mult'] * atr) if signal == "BUY" else price - 2 * (config['atr_tp_mult'] * atr)

    # Проверяем, изменился ли сигнал (чтобы не спамить)
    last = last_signals[asset_name]
    if last['signal'] != signal:
        last_signals[asset_name]['signal'] = signal
        last_signals[asset_name]['levels'] = {
            'price': price,
            'sl': sl,
            'tp1': tp1,
            'tp2': tp2,
            'rsi': current_rsi,
            'atr': atr
        }
        return signal, price, current_rsi, sl, tp1, tp2
    else:
        return None, price, current_rsi, None, None, None

# === Команды Telegram ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "👋 Бот запущен!\n"
        "Активы: GOLD, BTC, ETH, SOL (настроены индивидуально).\n"
        "Команды:\n"
        "/gold, /btc, /eth, /sol – цена и RSI\n"
        "/crypto – сводка по всем активам\n"
        "/status – последний сигнал для золота\n"
        "Сигналы приходят автоматически при пересечении RSI."
    )
    start_scheduler(context)

async def asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, asset_name):
    global chat_id
    chat_id = update.effective_chat.id
    config = ASSET_CONFIG[asset_name]
    await update.message.reply_text(f"⏳ Загружаю данные по {asset_name}...")
    await asyncio.sleep(1)
    price = get_current_price(config['symbol'])
    rsi, _, _, _ = get_rsi_atr_ma(config['symbol'], config)  # нам нужен только current_rsi
    # Для отображения последнего сигнала
    last = last_signals[asset_name]
    if price is not None and rsi is not None:
        msg = f"💰 {asset_name} ({config['symbol']}): ${price:.2f}\n📊 RSI (14): {rsi:.1f}"
        if last['signal']:
            msg += f"\n📌 Последний сигнал: {last['signal']}"
            if last['levels']:
                lv = last['levels']
                msg += f"\n--- Уровни ---\nВход: ${lv['price']:.2f}\n🛑 Стоп: ${lv['sl']:.2f}\n🎯 TP1: ${lv['tp1']:.2f}\n🎯 TP2: ${lv['tp2']:.2f}"
        else:
            msg += "\n📌 Сигналов пока нет"
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text(f"❌ Не удалось получить данные по {asset_name}.")

async def gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asset_cmd(update, context, "GOLD")
async def btc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asset_cmd(update, context, "BTC")
async def eth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asset_cmd(update, context, "ETH")
async def sol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asset_cmd(update, context, "SOL")

async def crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Загружаю сводку...")
    msg = "📊 СВОДКА ПО АКТИВАМ\n\n"
    for name, config in ASSET_CONFIG.items():
        price = get_current_price(config['symbol'])
        rsi, _, _, _ = get_rsi_atr_ma(config['symbol'], config)
        if price is not None and rsi is not None:
            last = last_signals[name]
            sig = last['signal'] if last['signal'] else "Нет"
            msg += f"**{name}**: ${price:.2f}  |  RSI: {rsi:.1f}  |  Сигнал: {sig}\n"
        else:
            msg += f"**{name}**: данные недоступны\n"
    await update.message.reply_text(msg)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # показываем последний сигнал по золоту (для обратной совместимости)
    await asset_cmd(update, context, "GOLD")

async def check_and_send_signal(context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    if chat_id is None:
        return
    for name in ASSET_CONFIG:
        if not ASSET_CONFIG[name]['enabled']:
            continue
        signal, price, rsi, sl, tp1, tp2 = check_signal_for_asset(name)
        if signal and price is not None:
            emoji = "📈" if signal == "BUY" else "📉"
            msg = f"{emoji} СИГНАЛ {name} ({ASSET_CONFIG[name]['symbol']}) (15 мин)\n\n"
            msg += f"💰 Вход: ${price:.2f}\n"
            msg += f"🛑 Стоп: ${sl:.2f}\n"
            msg += f"🎯 TP1: ${tp1:.2f}\n"
            msg += f"🎯 TP2: ${tp2:.2f}\n"
            msg += f"📊 RSI (14): {rsi:.1f}"
            await context.bot.send_message(chat_id=chat_id, text=msg)

async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    if chat_id is None:
        return
    msg = "📊 ПЛАНОВЫЙ ОТЧЁТ (15 мин)\n\n"
    for name, config in ASSET_CONFIG.items():
        price = get_current_price(config['symbol'])
        rsi, _, _, _ = get_rsi_atr_ma(config['symbol'], config)
        if price is not None and rsi is not None:
            last = last_signals[name]
            sig = last['signal'] if last['signal'] else "Нет"
            msg += f"**{name}**: ${price:.2f}  |  RSI: {rsi:.1f}  |  Сигнал: {sig}\n"
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
    print("✅ Бот готов, запускаем поллинг...")
    app.run_polling(drop_pending_updates=True)

def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()

if __name__ == "__main__":
    main()
