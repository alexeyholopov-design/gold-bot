import os
import asyncio
import threading
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from datetime import time as dt_time
import yfinance as yf

# === ТОКЕН из переменной окружения ===
TOKEN = os.environ.get('TOKEN')
if not TOKEN:
    raise ValueError("TOKEN environment variable not set")

# === Flask веб-сервер для Render ===
app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    return "Bot is running!", 200

def run_flask():
    app_flask.run(host='0.0.0.0', port=10000)

# === Telegram бот ===
chat_id = None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text("👋 Бот запущен! Команда /gold - цена.")
    start_scheduler(context)

async def gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Загружаю цену...")
    price, change = get_price()
    if price:
        await update.message.reply_text(f"💰 Золото: ${price:.2f}\n📊 Изменение за сутки: {change:.2f}%")
    else:
        await update.message.reply_text("❌ Не удалось получить цену.")

def get_price():
    try:
        ticker = yf.Ticker("GC=F")
        data = ticker.history(period="2d")
        if len(data) >= 2:
            now = data['Close'].iloc[-1]
            prev = data['Close'].iloc[-2]
            change = ((now - prev) / prev) * 100
            return now, change
        return None, None
    except:
        return None, None

async def check_price_movement(context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    if chat_id is None:
        return
    price, change = get_price()
    if price is None:
        return
    if abs(change) > 0.8:
        direction = "🚀 РАСТЁТ" if change > 0 else "📉 ПАДАЕТ"
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔔 СИГНАЛ ТРЕВОГИ!\nЦена изменилась на {change:.2f}%\nСейчас: ${price:.2f}\n{direction}"
        )

async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    if chat_id is None:
        return
    price, change = get_price()
    if price:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📊 ПЛАНОВЫЙ ОТЧЁТ\n💰 Золото: ${price:.2f}\n📈 Изменение за сутки: {change:.2f}%"
        )

def start_scheduler(context: ContextTypes.DEFAULT_TYPE):
    if context.job_queue is None:
        return
    for job in context.job_queue.jobs():
        job.schedule_removal()
    context.job_queue.run_repeating(check_price_movement, interval=300, first=10)
    context.job_queue.run_daily(daily_report, time=dt_time(hour=12, minute=0), days=tuple(range(7)))
    context.job_queue.run_daily(daily_report, time=dt_time(hour=18, minute=0), days=tuple(range(7)))

def run_bot():
    print("🤖 Бот запускается...")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gold", gold))
    print("✅ Бот готов, запускаем поллинг...")
    app.run_polling()

# === Главная функция: запускаем бота и Flask параллельно ===
def main():
    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    # Запускаем бота в основном потоке
    run_bot()

if __name__ == "__main__":
    main()