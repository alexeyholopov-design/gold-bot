import os
# Очищаем все переменные прокси ДО импорта
for var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
    os.environ.pop(var, None)

# Теперь безопасно импортируем
import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from datetime import time as dt_time

# === ТОКЕН ===
TOKEN = "7765279031:AAGAAZ66s0e-wG3GcfOYHeDzko-ZvWXZpWo"

chat_id = None

# --- Команда /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "👋 Бот запущен! Я буду присылать сигналы по золоту.\n"
        "Команда /gold - узнать цену сейчас."
    )
    start_scheduler(context)

# --- Команда /gold ---
async def gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Загружаю цену...")
    price, change = get_price()
    if price:
        await update.message.reply_text(f"💰 Золото: ${price:.2f}\n📊 Изменение за сутки: {change:.2f}%")
    else:
        await update.message.reply_text("❌ Не удалось получить цену (проверьте интернет).")

# --- Получение цены ---
def get_price():
    try:
        ticker = yf.Ticker("GC=F")
        data = ticker.history(period="2d")
        if len(data) >= 2:
            now = data['Close'].iloc[-1]
            prev = data['Close'].iloc[-2]
            change = ((now - prev) / prev) * 100
            return now, change
        elif len(data) == 1:
            return data['Close'].iloc[-1], 0.0
        else:
            return None, None
    except Exception as e:
        print("Ошибка получения цены:", e)
        return None, None

# --- Проверка резких движений (каждые 5 минут) ---
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
            text=f"🔔 СИГНАЛ ТРЕВОГИ!\n"
                 f"Цена изменилась на {change:.2f}%\n"
                 f"Сейчас: ${price:.2f}\n"
                 f"{direction}"
        )

# --- Плановый отчёт (в 12:00 и 18:00) ---
async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    if chat_id is None:
        return
    price, change = get_price()
    if price:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📊 ПЛАНОВЫЙ ОТЧЁТ\n"
                 f"💰 Золото: ${price:.2f}\n"
                 f"📈 Изменение за сутки: {change:.2f}%"
        )

# --- Настройка расписания ---
def start_scheduler(context: ContextTypes.DEFAULT_TYPE):
    if context.job_queue is None:
        return
    for job in context.job_queue.jobs():
        job.schedule_removal()
    context.job_queue.run_repeating(check_price_movement, interval=300, first=10)
    context.job_queue.run_daily(daily_report, time=dt_time(hour=12, minute=0), days=tuple(range(7)))
    context.job_queue.run_daily(daily_report, time=dt_time(hour=18, minute=0), days=tuple(range(7)))

# --- Запуск бота (без кастомного клиента) ---
def main():
    print("🤖 Бот запускается...")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gold", gold))
    print("✅ Бот готов, запускаем поллинг...")
    app.run_polling()

if __name__ == "__main__":
    main()