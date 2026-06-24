import os
import time
import threading
import random
import asyncio
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from datetime import time as dt_time
import yfinance as yf
import pandas as pd
import numpy as np

TOKEN = os.environ.get('TOKEN')
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
last_levels = None  # словарь для хранения последних уровней

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
TIMEFRAME = "15m"
LOOKBACK = 50  # для расчёта RSI
BARS_FOR_LEVELS = 10  # количество свечей для поиска минимума/максимума

def get_current_price(ticker_symbol="XAUUSD=X"):
    try:
        ticker = yf.Ticker(ticker_symbol)
        data = ticker.history(period="1d", interval="1m")
        if not data.empty:
            return data['Close'].iloc[-1]
        return None
    except Exception as e:
        print(f"Ошибка цены: {e}")
        return None

def get_rsi_and_bars(ticker_symbol="XAUUSD=X", retries=3, base_delay=5):
    """Возвращает current_rsi, prev_rsi и последние BARS_FOR_LEVELS свечей (High, Low)."""
    for attempt in range(retries):
        try:
            ticker = yf.Ticker(ticker_symbol)
            data = ticker.history(period="5d", interval=TIMEFRAME)
            if data.empty or len(data) < 2:
                time.sleep(base_delay * (attempt + 1))
                continue
            
            df = data.tail(LOOKBACK)
            if len(df) < 2:
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
            
            # Берём последние BARS_FOR_LEVELS свечей для расчёта уровней
            bars = df.tail(BARS_FOR_LEVELS)
            high = bars['High']
            low = bars['Low']
            
            print(f"RSI: {current_rsi:.1f}, high_max: {high.max():.2f}, low_min: {low.min():.2f}")
            return current_rsi, prev_rsi, high, low
        except Exception as e:
            print(f"Ошибка (попытка {attempt+1}): {e}")
            if "Rate limited" in str(e):
                time.sleep(base_delay * (attempt + 1) * 2)
            else:
                time.sleep(base_delay * (attempt + 1))
    return None, None, None, None

def calculate_levels(price, high, low, signal_type):
    """Рассчитывает SL, TP1, TP2 на основе последних свечей."""
    # Отступ в долларах для выхода за уровень
    buffer = 2.0  # для золота 2 доллара
    # Минимальный стоп (чтобы не было слишком маленьких)
    min_stop = 5.0
    
    if signal_type == "BUY":
        # Стоп ниже последнего минимума минус буфер
        sl = low.min() - buffer
        # Если стоп слишком близко к цене, расширяем до минимума
        if price - sl < min_stop:
            sl = price - min_stop
        # TP1 = цена + (цена - SL) т.е. 1:1
        tp1 = price + (price - sl)
        # TP2 = цена + 2 * (цена - SL) т.е. 1:2
        tp2 = price + 2 * (price - sl)
    else:  # SELL
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
    elif signal == last_signal:
        # Если сигнал тот же, не отправляем повторно, но можно обновить уровни, если они устарели
        # (для простоты оставим старые уровни)
        pass
    return None, price, current_rsi, None, None, None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "👋 Бот торговых сигналов запущен!\n"
        "Анализирую золото (XAUUSD) на 15-минутных свечах.\n"
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
        msg = f"💰 Золото (спот): ${price:.2f}\n📊 RSI (14): {current_rsi:.1f}\n📌 Последний сигнал: {signal_text}"
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
        msg += f"💰 Золото (спот): ${price:.2f}\n"
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
