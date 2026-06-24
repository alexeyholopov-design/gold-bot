import os
import time
import threading
import random
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from datetime import time as dt_time
import yfinance as yf
import pandas as pd
import numpy as np

# === ТОКЕН ===
TOKEN = os.environ.get('TOKEN')
if not TOKEN:
    raise ValueError("TOKEN environment variable not set")

# === Flask для Render ===
app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    return "Bot is running!", 200

def run_flask():
    app_flask.run(host='0.0.0.0', port=10000)

# === Бот ===
chat_id = None
last_signal = None  # 'BUY' или 'SELL' или None

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
TIMEFRAME = "15m"
LOOKBACK = 100  # количество свечей для расчёта

def get_rsi(ticker_symbol="GC=F", period=RSI_PERIOD, lookback=LOOKBACK):
    """Загружает данные и рассчитывает RSI."""
    try:
        ticker = yf.Ticker(ticker_symbol)
        # Загружаем последние 5 дней с интервалом 15 минут
        data = ticker.history(period="5d", interval=TIMEFRAME)
        if data.empty or len(data) < 2:
            print("Нет данных или слишком мало свечей")
            return None, None, None
        
        # Берём последние lookback свечей
        df = data.tail(lookback)
        if len(df) < 2:
            print("Недостаточно свечей для расчёта")
            return None, None, None
        
        close = df['Close']
        price = close.iloc[-1]
        
        # RSI
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        current_rsi = rsi.iloc[-1]
        prev_rsi = rsi.iloc[-2] if len(rsi) > 1 else current_rsi
        return price, current_rsi, prev_rsi
    except Exception as e:
        print(f"Ошибка RSI: {e}")
        return None, None, None

def check_signal():
    """Проверяет, нужно ли отправить сигнал."""
    global last_signal
    price, current_rsi, prev_rsi = get_rsi()
    if price is None or current_rsi is None:
        return None, None, None
    
    signal = None
    # Пересечение снизу вверх (oversold -> норма)
    if prev_rsi is not None and prev_rsi < RSI_OVERSOLD and current_rsi >= RSI_OVERSOLD:
        signal = "BUY"
    # Пересечение сверху вниз (overbought -> норма)
    elif prev_rsi is not None and prev_rsi > RSI_OVERBOUGHT and current_rsi <= RSI_OVERBOUGHT:
        signal = "SELL"
    
    if signal and signal != last_signal:
        last_signal = signal
        return signal, price, current_rsi
    return None, price, current_rsi

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "👋 Бот торговых сигналов запущен!\n"
        "Анализирую золото (GC=F) на 15-минутных свечах.\n"
        "Сигналы:\n"
        "📈 BUY  – когда RSI выходит из зоны перепроданности (<30)\n"
        "📉 SELL – когда RSI выходит из зоны перекупленности (>70)\n\n"
        "Команды:\n"
        "/gold – цена и RSI\n"
        "/status – последний сигнал"
    )
    start_scheduler(context)

async def gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id, last_signal
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Загружаю данные...")
    # Добавляем случайную задержку, чтобы не бить по API слишком часто
    time.sleep(random.uniform(1, 3))
    price, current_rsi, _ = get_rsi()
    if price is not None and current_rsi is not None:
        signal_text = last_signal if last_signal else "Нет сигнала"
        await update.message.reply_text(
            f"💰 Золото: ${price:.2f}\n"
            f"📊 RSI (14): {current_rsi:.1f}\n"
            f"📌 Последний сигнал: {signal_text}"
        )
    else:
        await update.message.reply_text("❌ Не удалось получить данные. Попробуйте позже.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_signal
    await update.message.reply_text(
        f"📌 Последний сигнал: {last_signal if last_signal else 'Нет сигнала'}"
    )

async def check_and_send_signal(context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    if chat_id is None:
        return
    # Небольшая задержка между проверками
    await asyncio.sleep(random.uniform(0.5, 1.5))
    signal, price, rsi = check_signal()
    if signal and price is not None and rsi is not None:
        emoji = "📈" if signal == "BUY" else "📉"
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{emoji} ТОРГОВЫЙ СИГНАЛ (15 мин)\n"
                 f"Тип: {signal}\n"
                 f"Цена: ${price:.2f}\n"
                 f"RSI (14): {rsi:.1f}\n"
                 f"Уровень: {'перепроданность' if signal == 'BUY' else 'перекупленность'}"
        )

async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    global chat_id, last_signal
    if chat_id is None:
        return
    price, current_rsi, _ = get_rsi()
    if price is not None and current_rsi is not None:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📊 ПЛАНОВЫЙ ОТЧЁТ (15 мин)\n"
                 f"💰 Золото: ${price:.2f}\n"
                 f"📊 RSI (14): {current_rsi:.1f}\n"
                 f"📌 Последний сигнал: {last_signal if last_signal else 'Нет сигнала'}"
        )

def start_scheduler(context: ContextTypes.DEFAULT_TYPE):
    if context.job_queue is None:
        return
    for job in context.job_queue.jobs():
        job.schedule_removal()
    # Проверяем каждые 5 минут
    context.job_queue.run_repeating(check_and_send_signal, interval=300, first=10)
    # Ежедневные отчёты
    context.job_queue.run_daily(daily_report, time=dt_time(hour=12, minute=0), days=tuple(range(7)))
    context.job_queue.run_daily(daily_report, time=dt_time(hour=18, minute=0), days=tuple(range(7)))

def run_bot():
    print("🤖 Бот запускается...")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gold", gold))
    app.add_handler(CommandHandler("status", status))
    print("✅ Бот готов, запускаем поллинг...")
    # Добавляем drop_pending_updates=True, чтобы сбросить старые обновления и избежать конфликта
    app.run_polling(drop_pending_updates=True)

def main():
    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()

if __name__ == "__main__":
    main()
