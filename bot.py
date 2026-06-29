import os, time, threading, random, asyncio, requests, json, uuid, feedparser, hmac, hashlib, urllib.parse
import pandas as pd
import numpy as np
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from datetime import time as dt_time, datetime, timedelta, timezone
from collections import defaultdict
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------- Конфигурация ----------
TOKEN = os.environ.get('TOKEN')
if not TOKEN:
    raise ValueError("TOKEN environment variable not set")

CHANNEL_ID = os.environ.get('CHANNEL_ID')
SEND_TO_CHANNEL = True

GIGACHAT_AUTH_KEY = os.environ.get('GIGACHAT_AUTH_KEY')
GIGACHAT_SCOPE = "GIGACHAT_API_PERS"

BYBIT_API_KEY = os.environ.get('BYBIT_API_KEY', '')
BYBIT_API_SECRET = os.environ.get('BYBIT_API_SECRET', '')

MSK = timezone(timedelta(hours=3))

app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    return "Bot is running!", 200

def run_flask():
    app_flask.run(host='0.0.0.0', port=10000)

chat_id = None
signal_history = []
active_signals = {}
gigachat_token = None
gigachat_token_expires = 0
news_sentiment = {}

ASSET_TIMEFRAMES = {
    "GOLD": ["5m", "15m"],
    "BTC":  ["15m", "1h"],
    "ETH":  ["15m", "1h"],
    "SOL":  ["15m", "1h"],
}

GOLD_SYMBOL = "XAUT-USDT"

ASSETS = {
    "GOLD": {"symbol": GOLD_SYMBOL},
    "BTC":  {"symbol": "BTC-USDT"},
    "ETH":  {"symbol": "ETH-USDT"},
    "SOL":  {"symbol": "SOL-USDT"},
}

for name, asset in ASSETS.items():
    active_signals[name] = {}
    for tf in ASSET_TIMEFRAMES[name]:
        active_signals[name][tf] = []

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
LOOKBACK = 50
EMA_FAST = 20
EMA_SLOW = 50
EMA_FAST_FAST = 3
EMA_SLOW_FAST = 10

ATR_MULTIPLIERS = {
    "5m":  {"SL": 1.2, "TP1": 1.5, "TP2": 2.0, "TP3": 3.0},
    "15m": {"SL": 1.5, "TP1": 2.0, "TP2": 3.0, "TP3": 5.0},
    "1h":  {"SL": 2.0, "TP1": 3.0, "TP2": 5.0, "TP3": 8.0},
}

def safe_format(value, format_spec=":.2f"):
    try:
        if value is None:
            return "0.00"
        num = float(value)
        if np.isnan(num) or not np.isfinite(num):
            return "0.00"
        return f"{num:{format_spec}}"
    except (ValueError, TypeError):
        return str(value)

def get_signal_stars(signal_type):
    return {"rsi": "⭐⭐", "ema": "⭐⭐", "combined": "⭐⭐⭐", "fast_ema": "⭐"}.get(signal_type, "")

# ---------- Bybit TradFi ----------
def bybit_sign_request(params):
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    param_str = urllib.parse.urlencode(sorted(params.items()))
    sign_str = f"{timestamp}{BYBIT_API_KEY}{recv_window}{param_str}"
    signature = hmac.new(
        bytes(BYBIT_API_SECRET, "utf-8"),
        bytes(sign_str, "utf-8"),
        hashlib.sha256
    ).hexdigest()
    return {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": recv_window,
    }

def check_bybit_tradfi():
    global GOLD_SYMBOL
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        print("⚠️ Ключи Bybit не заданы, GOLD будет работать через BingX")
        return False
    try:
        params = {"category": "tradfi", "symbol": "XAUUSDT+"}
        headers = bybit_sign_request(params)
        url = "https://api.bybit.com/v5/market/tickers"
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code == 403:
            print("❌ Bybit заблокировал запрос (403). TradFi недоступен.")
            return False
        data = resp.json()
        if data.get("retCode") == 0 and data.get("result", {}).get("list"):
            price = float(data["result"]["list"][0]["lastPrice"])
            print(f"✅ Доступ к Bybit TradFi подтверждён. Цена XAUUSDT+: ${price:.2f}")
            GOLD_SYMBOL = "XAUUSDT+"
            ASSETS["GOLD"]["symbol"] = GOLD_SYMBOL
            return True
        else:
            print(f"❌ Bybit вернул ошибку: {data.get('retMsg', 'Unknown error')}")
            return False
    except Exception as e:
        print(f"❌ Исключение при проверке Bybit TradFi: {type(e).__name__}: {e}")
        return False

def get_bybit_price(symbol):
    try:
        params = {"category": "tradfi", "symbol": symbol}
        headers = bybit_sign_request(params)
        url = "https://api.bybit.com/v5/market/tickers"
        resp = requests.get(url, params=params, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("retCode") == 0:
                return float(data["result"]["list"][0]["lastPrice"])
    except:
        pass
    return None

# ---------- GigaChat ----------
async def get_gigachat_token(force=False):
    global gigachat_token, gigachat_token_expires
    if not force and gigachat_token and time.time() < gigachat_token_expires:
        return gigachat_token
    if not GIGACHAT_AUTH_KEY:
        return None
    try:
        url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        headers = {
            "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded"
        }
        data = {"scope": GIGACHAT_SCOPE}
        response = requests.post(url, headers=headers, data=data, verify=False, timeout=10)
        if response.status_code == 200:
            result = response.json()
            gigachat_token = result.get("access_token")
            expires_at = result.get("expires_at", time.time() + 1800)
            gigachat_token_expires = expires_at - 60
            return gigachat_token
    except Exception as e:
        print(f"❌ Ошибка токена GigaChat: {e}")
    return None

async def ask_gigachat(prompt):
    token = await get_gigachat_token()
    if not token:
        return None
    try:
        url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "model": "GigaChat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3, "max_tokens": 500
        }
        response = requests.post(url, headers=headers, json=payload, verify=False, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"]["content"].strip()
        elif response.status_code == 401:
            new_token = await get_gigachat_token(force=True)
            if new_token:
                headers["Authorization"] = f"Bearer {new_token}"
                response = requests.post(url, headers=headers, json=payload, verify=False, timeout=15)
                if response.status_code == 200:
                    data = response.json()
                    if "choices" in data and len(data["choices"]) > 0:
                        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"❌ GigaChat error: {e}")
    return None

# ---------- Новости ----------
def fetch_news(asset):
    rss_urls = {
        "GOLD": "https://ru.investing.com/rss/news_295.rss",
        "BTC": "https://cointelegraph.com/rss",
        "ETH": "https://cointelegraph.com/rss",
        "SOL": "https://cointelegraph.com/rss",
    }
    url = rss_urls.get(asset)
    if not url:
        return ""
    try:
        feed = feedparser.parse(url)
        entries = feed.entries[:10]
        titles = [entry.title for entry in entries if hasattr(entry, 'title')]
        return " ".join(titles) if titles else ""
    except:
        return ""

async def analyze_news_with_gigachat(asset, news_text):
    if not news_text:
        return "Новостей нет."
    prompt = f"""
Проанализируй новости по активу {asset} за последние часы. Новости:
{news_text}

Дай краткую оценку (1–2 предложения):
- общее настроение (бычье/медвежье/нейтральное)
- ключевые события
- влияние на цену в ближайшие часы
"""
    return await ask_gigachat(prompt)

async def update_news_sentiment(context: ContextTypes.DEFAULT_TYPE):
    global news_sentiment
    try:
        print("📰 Обновление новостного фона...")
        for asset in ASSETS:
            news_text = fetch_news(asset)
            if news_text:
                analysis = await analyze_news_with_gigachat(asset, news_text)
                news_sentiment[asset] = analysis
                print(f"📰 {asset}: {analysis[:100]}...")
            else:
                news_sentiment[asset] = "Новостей не найдено."
        print("✅ Новостной фон обновлён")
    except Exception as e:
        print(f"❌ Ошибка в update_news_sentiment: {e}")

# ---------- AI анализ ----------
async def get_ai_analysis(asset_name, signal_type, signal, price, rsi, ema_fast=None, ema_slow=None,
                          atr=None, volume=None, avg_volume=None, vwap=None, higher_trend=None):
    if not GIGACHAT_AUTH_KEY:
        return None
    direction = "покупку" if signal == "BUY" else "продажу"
    price_str = safe_format(price)
    rsi_str = f"{float(rsi):.1f}" if rsi is not None else "N/A"
    ema_text = ""
    if ema_fast is not None and ema_slow is not None:
        ema_text = f"EMA{EMA_FAST}: {safe_format(ema_fast)}, EMA{EMA_SLOW}: {safe_format(ema_slow)}"
    atr_str = safe_format(atr) if atr else ""
    vol_str = ""
    if volume is not None and avg_volume is not None and vwap is not None:
        vol_str = (f"Объём свечи: {float(volume):.0f} | "
                   f"Средний объём (50): {float(avg_volume):.0f} | "
                   f"VWAP (50): ${float(vwap):.2f}\n")
    trend_text = f"Тренд на старшем ТФ: {higher_trend}" if higher_trend else ""
    news_text = news_sentiment.get(asset_name, "Новостной фон не оценён.")

    prompt = f"""
Ты – опытный трейдер по золоту и криптовалютам. Оцени сигнал и учти новостной фон.

Актив: {asset_name}
Тип сигнала: {signal_type} (сигнал на {direction})
Цена: ${price_str}
RSI (14): {rsi_str}
{ema_text}
{atr_str}
{vol_str}
{trend_text}
Новостной фон (последние часы): {news_text}

Ответь кратко, строго в формате:
1. Оценка ситуации (одно предложение).
2. Риск (одно предложение).
3. Рекомендация: BUY/SELL/HOLD с пояснением.
4. Оценка силы сигнала: напиши "✅ СИЛЬНЫЙ СИГНАЛ" или "⚠️ СЛАБЫЙ СИГНАЛ"
"""
    try:
        return await ask_gigachat(prompt)
    except Exception as e:
        print(f"❌ Ошибка AI: {e}")
        return None

# ---------- Рыночные данные ----------
def get_current_price(symbol):
    if GOLD_SYMBOL == "XAUUSDT+" and symbol == "XAUUSDT+":
        return get_bybit_price(symbol)
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
        df.rename(columns={'open': 'Open', 'close': 'Close', 'high': 'High', 'low': 'Low',
                           'volume': 'Volume', 'time': 'Timestamp'}, inplace=True)
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df.dropna(subset=["Open", "High", "Low", "Close"])
    except:
        return None

def get_rsi_and_bars(symbol, interval):
    df = get_klines(symbol, interval, limit=LOOKBACK)
    if df is None or len(df) < 2:
        return None, None, None, None
    close = df['Close']
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
    rs = gain / loss
    rsi = 100 - (100/(1+rs))
    current_rsi = rsi.iloc[-1]
    prev_rsi = rsi.iloc[-2] if len(rsi) > 1 else current_rsi
    return current_rsi, prev_rsi, df.tail(10)['High'], df.tail(10)['Low']

def get_ema_cross(symbol, interval, fast, slow):
    df = get_klines(symbol, interval, limit=LOOKBACK)
    if df is None or len(df) < slow:
        return None, None, None, None, None
    close = df['Close'].values
    ema_fast = np.zeros_like(close); ema_slow = np.zeros_like(close)
    alpha_fast = 2/(fast+1); alpha_slow = 2/(slow+1)
    ema_fast[0] = close[0]; ema_slow[0] = close[0]
    for i in range(1, len(close)):
        ema_fast[i] = alpha_fast*close[i] + (1-alpha_fast)*ema_fast[i-1]
        ema_slow[i] = alpha_slow*close[i] + (1-alpha_slow)*ema_slow[i-1]
    cur_fast = ema_fast[-1]; cur_slow = ema_slow[-1]
    prev_fast = ema_fast[-2]; prev_slow = ema_slow[-2]
    signal = None
    if prev_fast <= prev_slow and cur_fast > cur_slow:
        signal = "BUY"
    elif prev_fast >= prev_slow and cur_fast < cur_slow:
        signal = "SELL"
    return signal, cur_fast, cur_slow, prev_fast, prev_slow

def get_atr_value(symbol, interval):
    df = get_klines(symbol, interval, limit=LOOKBACK)
    if df is None or len(df) < 14:
        return None
    high = df['High'].values; low = df['Low'].values; close = df['Close'].values
    tr = np.maximum(high-low, np.maximum(abs(high-np.roll(close,1)), abs(low-np.roll(close,1))))
    tr[0] = high[0]-low[0]
    atr = np.zeros_like(tr)
    atr[:14] = np.mean(tr[:14])
    for i in range(14, len(tr)):
        atr[i] = (atr[i-1]*13 + tr[i])/14
    return atr[-1]

def get_trend_direction(symbol, base_interval, check_interval, fast=20, slow=50):
    df = get_klines(symbol, interval=check_interval, limit=LOOKBACK)
    if df is None or len(df) < slow:
        return None
    close = df['Close'].values
    ema_fast = np.zeros_like(close); ema_slow = np.zeros_like(close)
    alpha_fast = 2/(fast+1); alpha_slow = 2/(slow+1)
    ema_fast[0] = close[0]; ema_slow[0] = close[0]
    for i in range(1, len(close)):
        ema_fast[i] = alpha_fast*close[i] + (1-alpha_fast)*ema_fast[i-1]
        ema_slow[i] = alpha_slow*close[i] + (1-alpha_slow)*ema_slow[i-1]
    if ema_fast[-1] > ema_slow[-1]: return "UP"
    elif ema_fast[-1] < ema_slow[-1]: return "DOWN"
    return None

def calculate_atr_levels(price, atr, signal_type, tf):
    mult = ATR_MULTIPLIERS.get(tf, {"SL": 1.5, "TP1": 2.0, "TP2": 3.0, "TP3": 5.0})
    if signal_type == "BUY":
        sl = price - atr * mult["SL"]
        tp1 = price + atr * mult["TP1"]
        tp2 = price + atr * mult["TP2"]
        tp3 = price + atr * mult["TP3"]
    else:
        sl = price + atr * mult["SL"]
        tp1 = price - atr * mult["TP1"]
        tp2 = price - atr * mult["TP2"]
        tp3 = price - atr * mult["TP3"]
    return {'price': round(price,2), 'sl': round(sl,2), 'tp1': round(tp1,2),
            'tp2': round(tp2,2), 'tp3': round(tp3,2), 'atr': round(atr,2)}

def create_signal_dict(asset_name, tf, signal_type, signal, levels, ai_analysis=None):
    return {
        'timestamp': datetime.now(timezone.utc),
        'asset': asset_name,
        'tf': tf,
        'type': signal_type,
        'signal': signal,
        'levels': levels.copy(),
        'tp1_hit': False, 'tp2_hit': False, 'tp3_hit': False,
        'sl_hit': False, 'closed': False,
        'ai_analysis': ai_analysis,
        'adjusted_by_ai': False
    }

def add_active_signal(asset_name, tf, signal_dict):
    if asset_name not in active_signals:
        active_signals[asset_name] = {}
    if tf not in active_signals[asset_name]:
        active_signals[asset_name][tf] = []
    active_signals[asset_name][tf].append(signal_dict)

# ---------- ИСПРАВЛЕННАЯ ПРОВЕРКА УРОВНЕЙ (TP1/TP2/TP3 не закрывают сделку) ----------
async def check_signal_levels(bot, signal_dict):
    levels = signal_dict['levels']
    if signal_dict['closed']:
        return
    asset_name = signal_dict['asset']
    symbol = ASSETS[asset_name]['symbol']
    price = get_current_price(symbol)
    if price is None:
        return

    is_buy = signal_dict['signal'] == 'BUY'

    if not signal_dict['sl_hit']:
        sl_hit = (is_buy and price <= levels['sl']) or (not is_buy and price >= levels['sl'])
        if sl_hit:
            signal_dict['sl_hit'] = True
            signal_dict['closed'] = True
            msg = (f"❌ Стоп-лосс сработал по {asset_name} [{signal_dict['tf']}] ({signal_dict['type']})\n"
                   f"Вход: ${levels['price']:.2f}\nSL: ${levels['sl']:.2f}")
            await send_to_chat(FakeContext(bot), msg)
            print(f"✅ Отправлено уведомление о SL для {asset_name} {signal_dict['tf']}")
            return

    if not signal_dict['sl_hit']:
        # TP1
        if not signal_dict['tp1_hit']:
            tp1_hit = (is_buy and price >= levels['tp1']) or (not is_buy and price <= levels['tp1'])
            if tp1_hit:
                signal_dict['tp1_hit'] = True
                signal_dict['levels']['sl'] = levels['price']
                msg = (f"✅ TP1 достигнут по {asset_name} [{signal_dict['tf']}] ({signal_dict['type']})\n"
                       f"Вход: ${levels['price']:.2f}\nTP1: ${levels['tp1']:.2f}\n"
                       f"🔒 Стоп перенесён в безубыток")
                await send_to_chat(FakeContext(bot), msg)
                print(f"✅ Отправлено уведомление о TP1 (безубыток) для {asset_name} {signal_dict['tf']}")
        else:
            # После TP1 проверяем TP2 и TP3
            if not signal_dict['tp2_hit']:
                tp2_hit = (is_buy and price >= levels['tp2']) or (not is_buy and price <= levels['tp2'])
                if tp2_hit:
                    signal_dict['tp2_hit'] = True
                    msg = (f"✅ TP2 достигнут по {asset_name} [{signal_dict['tf']}] ({signal_dict['type']})\n"
                           f"Вход: ${levels['price']:.2f}\nTP2: ${levels['tp2']:.2f}")
                    await send_to_chat(FakeContext(bot), msg)
                    print(f"✅ Отправлено уведомление о TP2 для {asset_name} {signal_dict['tf']}")
            if not signal_dict['tp3_hit']:
                tp3_hit = (is_buy and price >= levels['tp3']) or (not is_buy and price <= levels['tp3'])
                if tp3_hit:
                    signal_dict['tp3_hit'] = True
                    msg = (f"✅ TP3 достигнут по {asset_name} [{signal_dict['tf']}] ({signal_dict['type']})\n"
                           f"Вход: ${levels['price']:.2f}\nTP3: ${levels['tp3']:.2f}")
                    await send_to_chat(FakeContext(bot), msg)
                    print(f"✅ Отправлено уведомление о TP3 для {asset_name} {signal_dict['tf']}")

async def check_all_active_signals(bot):
    for asset_name, tf_dict in active_signals.items():
        for tf, signals in tf_dict.items():
            for sig in signals[:]:
                await check_signal_levels(bot, sig)
                if sig['closed']:
                    signal_history.append(sig)
                    signals.remove(sig)

def has_open_signal(asset_name, tf, signal_type, direction):
    sigs = active_signals.get(asset_name, {}).get(tf, [])
    for s in sigs:
        if not s['closed'] and s['type'] == signal_type and s['signal'] == direction:
            return True
    return False

# ---------- ИСПРАВЛЕННЫЙ ВЫВОД ОБЪЁМА И VWAP (короткие числа) ----------
async def handle_new_signal(asset_name, tf, signal_type, signal, price, rsi=None, ema_fast=None, ema_slow=None,
                            cur_fast3=None, cur_slow10=None, atr=None, volume=None, avg_volume=None, vwap=None,
                            higher_trend=None, context=None):
    if has_open_signal(asset_name, tf, signal_type, signal):
        return
    levels = calculate_atr_levels(price, atr, signal, tf)
    ai_analysis = await get_ai_analysis(asset_name, signal_type, signal, price, rsi,
                                        ema_fast=ema_fast, ema_slow=ema_slow, atr=atr,
                                        volume=volume, avg_volume=avg_volume, vwap=vwap,
                                        higher_trend=higher_trend)
    signal_dict = create_signal_dict(asset_name, tf, signal_type, signal, levels, ai_analysis)
    add_active_signal(asset_name, tf, signal_dict)

    stars = get_signal_stars(signal_type)
    direction = "покупку" if signal == "BUY" else "продажу"
    symbol = ASSETS[asset_name]['symbol']
    msg = f"{stars} 📢 Сигнал на {direction} по {signal_type.upper()} для {asset_name} ({symbol}) [{tf}]\n"
    msg += f"💰 Вход: ${levels['price']:.2f}\n"
    msg += f"🛑 SL: ${levels['sl']:.2f} (ATR×{ATR_MULTIPLIERS.get(tf, {}).get('SL', '?')})\n"
    msg += f"🎯 TP1: ${levels['tp1']:.2f} (ATR×{ATR_MULTIPLIERS.get(tf, {}).get('TP1', '?')})\n"
    msg += f"🎯 TP2: ${levels['tp2']:.2f} (ATR×{ATR_MULTIPLIERS.get(tf, {}).get('TP2', '?')})\n"
    msg += f"🎯 TP3: ${levels['tp3']:.2f} (ATR×{ATR_MULTIPLIERS.get(tf, {}).get('TP3', '?')})\n"
    if rsi is not None:
        msg += f"📊 RSI: {float(rsi):.1f}\n"
    if ema_fast is not None:
        msg += f"📊 EMA: {float(ema_fast):.2f} / {float(ema_slow):.2f}\n"
    if cur_fast3 is not None:
        msg += f"📊 EMA(3/10): {float(cur_fast3):.2f} / {float(cur_slow10):.2f}\n"
    if volume is not None:
        msg += f"📊 Объём: {float(volume):.0f} | Средний: {float(avg_volume):.0f} | VWAP: ${float(vwap):.2f}\n"
    if ai_analysis:
        msg += f"\n🧠 {ai_analysis}"
    await send_to_chat(context, msg)

async def check_and_send_signal(context: ContextTypes.DEFAULT_TYPE):
    print("⏰ Автоматическая проверка запущена")
    if CHANNEL_ID is None and chat_id is None:
        print("⚠️ Нет получателей")
        return
    for name, asset in ASSETS.items():
        symbol = asset['symbol']
        for tf in ASSET_TIMEFRAMES[name]:
            price = get_current_price(symbol)
            if price is None:
                continue
            try:
                df_vol = get_klines(symbol, tf, limit=50)
                avg_volume = None
                vwap = None
                volume_now = None
                if df_vol is not None and len(df_vol) >= 2:
                    volume_now = df_vol['Volume'].iloc[-1]
                    if pd.notna(volume_now):
                        avg_volume = df_vol['Volume'].mean()
                        typical_price = (df_vol['High'] + df_vol['Low'] + df_vol['Close']) / 3
                        if df_vol['Volume'].sum() > 0:
                            vwap = (typical_price * df_vol['Volume']).sum() / df_vol['Volume'].sum()
                        else:
                            vwap = price

                if volume_now is not None and avg_volume is not None and avg_volume > 0:
                    if volume_now < avg_volume * 0.8:
                        print(f"ℹ️ Сигнал для {name} {tf} пропущен: объём {volume_now:.0f} < средний {avg_volume:.0f}")
                        continue

                if tf != "5m" and vwap is not None and not np.isnan(vwap):
                    pass

                current_rsi, prev_rsi, _, _ = get_rsi_and_bars(symbol, tf)
                atr = get_atr_value(symbol, tf)

                rsi_signal = None
                if current_rsi is not None and prev_rsi is not None and atr is not None:
                    if prev_rsi < RSI_OVERSOLD and current_rsi >= RSI_OVERSOLD:
                        rsi_signal = "BUY"
                    elif prev_rsi > RSI_OVERBOUGHT and current_rsi <= RSI_OVERBOUGHT:
                        rsi_signal = "SELL"
                    if rsi_signal:
                        if tf != "5m" and vwap is not None and not np.isnan(vwap):
                            if (rsi_signal == "BUY" and price <= vwap) or (rsi_signal == "SELL" and price >= vwap):
                                print(f"ℹ️ Сигнал {rsi_signal} для {name} {tf} пропущен: VWAP={vwap:.2f}, цена={price:.2f}")
                                continue
                        await handle_new_signal(name, tf, "rsi", rsi_signal, price, rsi=current_rsi, atr=atr,
                                                volume=volume_now, avg_volume=avg_volume, vwap=vwap, context=context)

                ema_signal, cur_fast, cur_slow, _, _ = get_ema_cross(symbol, tf, EMA_FAST, EMA_SLOW)
                if ema_signal and atr is not None:
                    if tf != "5m" and vwap is not None and not np.isnan(vwap):
                        if (ema_signal == "BUY" and price <= vwap) or (ema_signal == "SELL" and price >= vwap):
                            print(f"ℹ️ Сигнал {ema_signal} для {name} {tf} пропущен: VWAP={vwap:.2f}, цена={price:.2f}")
                            continue
                    await handle_new_signal(name, tf, "ema", ema_signal, price,
                                            ema_fast=cur_fast, ema_slow=cur_slow, atr=atr,
                                            volume=volume_now, avg_volume=avg_volume, vwap=vwap, context=context)

                combined_signal = None
                if rsi_signal and ema_signal and atr is not None:
                    if rsi_signal == "BUY" and cur_fast > cur_slow:
                        combined_signal = "BUY"
                    elif rsi_signal == "SELL" and cur_fast < cur_slow:
                        combined_signal = "SELL"
                    if combined_signal:
                        if tf != "5m" and vwap is not None and not np.isnan(vwap):
                            if (combined_signal == "BUY" and price <= vwap) or (combined_signal == "SELL" and price >= vwap):
                                print(f"ℹ️ Сигнал {combined_signal} для {name} {tf} пропущен: VWAP={vwap:.2f}, цена={price:.2f}")
                                continue
                        await handle_new_signal(name, tf, "combined", combined_signal, price,
                                                rsi=current_rsi, ema_fast=cur_fast, ema_slow=cur_slow, atr=atr,
                                                volume=volume_now, avg_volume=avg_volume, vwap=vwap, context=context)

                fast_cross, cur_fast3, cur_slow10, _, _ = get_ema_cross(symbol, tf, EMA_FAST_FAST, EMA_SLOW_FAST)
                if fast_cross and atr is not None:
                    higher_tf = "1h" if tf == "15m" else "15m" if tf != "1h" else None
                    trend_ok = True
                    if higher_tf:
                        trend = get_trend_direction(symbol, tf, higher_tf)
                        if (fast_cross == "BUY" and trend != "UP") or (fast_cross == "SELL" and trend != "DOWN"):
                            trend_ok = False
                    if trend_ok:
                        if tf != "5m" and vwap is not None and not np.isnan(vwap):
                            if (fast_cross == "BUY" and price <= vwap) or (fast_cross == "SELL" and price >= vwap):
                                print(f"ℹ️ Сигнал {fast_cross} для {name} {tf} пропущен: VWAP={vwap:.2f}, цена={price:.2f}")
                                continue
                        await handle_new_signal(name, tf, "fast_ema", fast_cross, price,
                                                cur_fast3=cur_fast3, cur_slow10=cur_slow10, atr=atr,
                                                volume=volume_now, avg_volume=avg_volume, vwap=vwap,
                                                higher_trend=trend if higher_tf else None, context=context)
            except Exception as e:
                print(f"❌ Ошибка в check_and_send_signal для {name} {tf}: {e}")
    await check_all_active_signals(context.bot)

# ---------- Отчёты ----------
def get_moscow_time():
    return datetime.now(timezone.utc) + timedelta(hours=3)

def calculate_stats(signals):
    total = len(signals)
    if total == 0:
        return None
    tp1_count = sum(1 for s in signals if s['tp1_hit'])
    tp2_count = sum(1 for s in signals if s['tp2_hit'])
    tp3_count = sum(1 for s in signals if s['tp3_hit'])
    sl_count = sum(1 for s in signals if s['sl_hit'])
    closed = tp1_count + tp2_count + tp3_count + sl_count
    success_rate = (tp1_count / closed * 100) if closed > 0 else 0
    return {
        'total': total, 'tp1': tp1_count, 'tp2': tp2_count,
        'tp3': tp3_count, 'sl': sl_count, 'closed': closed,
        'success_rate': success_rate
    }

def generate_insights(stats_by_asset, stats_by_type):
    insights = []
    best, worst = [], []
    for asset, st in stats_by_asset.items():
        if st['closed'] > 0:
            best.append((asset, st['success_rate'], st['tp1'], st['closed']))
            worst.append((asset, st['success_rate'], st['tp1'], st['closed']))
    best.sort(key=lambda x: x[1], reverse=True)
    worst.sort(key=lambda x: x[1])
    if best:
        top3 = best[:3]
        insights.append("🏆 Самые эффективные инструменты (по % успеха):")
        for a, rate, tp1, closed in top3:
            insights.append(f"{a} – {rate:.1f}% ({tp1}/{closed})")
    if worst:
        bottom3 = worst[:3]
        insights.append("📉 Самые неэффективные:")
        for a, rate, tp1, closed in bottom3:
            insights.append(f"{a} – {rate:.1f}% ({tp1}/{closed})")
    for typ, st in stats_by_type.items():
        if st['closed'] >= 3:
            if st['success_rate'] == 100:
                insights.append(f"💡 {typ.upper()} показал 100% попаданий ({st['tp1']} из {st['closed']}), но выборка мала.")
            elif st['success_rate'] == 0:
                insights.append(f"💡 {typ.upper()} показал 0% успеха – стоит пересмотреть логику.")
    return insights

async def generate_daily_report():
    now = get_moscow_time()
    yesterday = now - timedelta(days=1)
    signals = [s for s in signal_history if s['timestamp'].replace(tzinfo=timezone.utc) + timedelta(hours=3) >= yesterday]
    return format_report(signals, f"📊 Ежедневный отчёт за {now.strftime('%d.%m.%Y')}")

async def generate_weekly_report():
    now = get_moscow_time()
    week_ago = now - timedelta(days=7)
    signals = [s for s in signal_history if s['timestamp'].replace(tzinfo=timezone.utc) + timedelta(hours=3) >= week_ago]
    return format_report(signals, f"📊 Воскресный отчёт за неделю ({ (now-timedelta(days=7)).strftime('%d.%m')} - {now.strftime('%d.%m.%Y')})")

async def generate_today_report():
    now = get_moscow_time()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    signals = [s for s in signal_history if s['timestamp'].replace(tzinfo=timezone.utc) + timedelta(hours=3) >= today_start]
    return format_report(signals, f"📊 Отчёт за сегодня ({now.strftime('%d.%m.%Y')})")

def format_report(signals, title):
    if not signals:
        return f"{title}\n\nСигналов не было."
    total = len(signals)
    stats_by_asset = defaultdict(list)
    stats_by_type = defaultdict(list)
    for s in signals:
        if not s['closed']:
            continue
        stats_by_asset[s['asset']].append(s)
        stats_by_type[s['type']].append(s)
    all_stats = calculate_stats([s for s in signals if s['closed']])
    lines = [title, ""]
    if all_stats:
        lines.append(f"Всего сигналов: {all_stats['total']}")
        lines.append(f"Успешных (TP1 достигнут): {all_stats['tp1']}")
        lines.append(f"Общая успешность: ~{all_stats['success_rate']:.1f}%")
        if all_stats['tp2'] == 0 and all_stats['tp3'] == 0:
            lines.append("TP2 и TP3 не достигнуты ни разу.")
        lines.append("")
    asset_stats = {}
    for asset, lst in stats_by_asset.items():
        st = calculate_stats(lst)
        if st:
            asset_stats[asset] = st
    lines.append("📌 По инструментам:")
    for asset, st in sorted(asset_stats.items()):
        lines.append(f"{asset}: всего {st['total']}, TP1: {st['tp1']}, успешность {st['success_rate']:.1f}%")
    lines.append("")
    type_stats = {}
    for typ, lst in stats_by_type.items():
        st = calculate_stats(lst)
        if st:
            type_stats[typ] = st
    lines.append("🔹 По типам сигналов:")
    for typ, st in sorted(type_stats.items()):
        lines.append(f"{typ.upper()}: всего {st['total']}, TP1: {st['tp1']}, успешность {st['success_rate']:.1f}%")
    lines.append("")
    insights = generate_insights(asset_stats, type_stats)
    if insights:
        lines.append("💡 Заметка:")
        lines.extend(insights)
    return "\n".join(lines)

class FakeContext:
    def __init__(self, bot):
        self.bot = bot

async def send_to_chat(context, text):
    try:
        if CHANNEL_ID is not None and SEND_TO_CHANNEL:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=text)
        if chat_id is not None:
            await context.bot.send_message(chat_id=chat_id, text=text)
        if CHANNEL_ID is None and chat_id is None:
            print("⚠️ Нет получателя для сообщения")
    except Exception as e:
        print(f"❌ Ошибка в send_to_chat: {e}")

async def channel_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SEND_TO_CHANNEL
    SEND_TO_CHANNEL = True
    await update.message.reply_text("✅ Отправка в канал включена.")

async def channel_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SEND_TO_CHANNEL
    SEND_TO_CHANNEL = False
    await update.message.reply_text("⏸️ Отправка в канал приостановлена.")

async def start_scheduler(app):
    job_queue = app.job_queue
    if job_queue is None:
        print("⚠️ JobQueue не доступен.")
        return
    for job in job_queue.jobs():
        job.schedule_removal()
    job_queue.run_repeating(check_and_send_signal, interval=60, first=10)
    job_queue.run_daily(lambda ctx: asyncio.create_task(daily_report_job(ctx)),
                        time=dt_time(hour=21, minute=0, tzinfo=MSK), days=tuple(range(7)))
    job_queue.run_daily(lambda ctx: asyncio.create_task(weekly_report_job(ctx)),
                        time=dt_time(hour=18, minute=0, tzinfo=MSK), days=(6,))
    job_queue.run_daily(send_morning_report, time=dt_time(hour=10, minute=0, tzinfo=MSK), days=tuple(range(7)))
    job_queue.run_repeating(update_news_sentiment, interval=3600, first=30)
    print("📅 Планировщик запущен")

async def daily_report_job(context):
    print("📊 Запущена задача daily_report")
    report = await generate_daily_report()
    await send_to_chat(context, report)

async def weekly_report_job(context):
    print("📊 Запущена задача weekly_report")
    report = await generate_weekly_report()
    await send_to_chat(context, report)

async def send_morning_report(context: ContextTypes.DEFAULT_TYPE):
    print("📊 Формирование утреннего обзора...")
    msg = "🌅 **Утренний обзор рынка**\n\n"
    for name, asset in ASSETS.items():
        symbol = asset['symbol']
        price = get_current_price(symbol)
        rsi, _, _, _ = get_rsi_and_bars(symbol, "15m")
        if price is not None and rsi is not None:
            msg += f"**{name}** ({symbol}): ${float(price):.2f}  |  RSI(14): {float(rsi):.1f}\n"
        else:
            msg += f"**{name}**: данные недоступны\n"
    msg += "\n📰 **Новостной фон:**\n"
    for asset_name in ASSETS:
        sentiment = news_sentiment.get(asset_name, "Нет данных")
        msg += f"**{asset_name}**: {sentiment}\n"
    await send_to_chat(context, msg)
    print("✅ Утренний обзор отправлен")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    status_msg = "включена" if SEND_TO_CHANNEL else "приостановлена"
    gold_source = "Bybit TradFi" if GOLD_SYMBOL == "XAUUSDT+" else "BingX (возможно расхождение ~$10)"
    await update.message.reply_text(
        "👋 Бот запущен!\n"
        f"Отслеживаю: GOLD ({gold_source}), BTC, ETH, SOL.\n"
        "Таймфреймы: GOLD (5м, 15м), крипта (15м, 1ч).\n"
        "⭐ FAST EMA | ⭐⭐ RSI/EMA | ⭐⭐⭐ Combined\n"
        "📰 Новости каждый час. Утренний обзор в 10:00 МСК.\n"
        f"📢 Отправка в канал: {status_msg}\n\n"
        "Команды:\n"
        "/gold, /btc, /eth, /sol – цена и активные сигналы\n"
        "/crypto – сводка\n"
        "/status – активные сигналы\n"
        "/today – отчёт за сегодня\n"
        "/ai {актив} – AI-анализ\n"
        "/channel_on – включить канал\n"
        "/channel_off – приостановить канал"
    )
    msg = "📌 Активные сигналы:\n"
    for name in ASSETS:
        for tf in ASSET_TIMEFRAMES[name]:
            sigs = active_signals.get(name, {}).get(tf, [])
            for s in sigs:
                if not s['closed']:
                    direction = "BUY" if s['signal'] == "BUY" else "SELL"
                    msg += f"{name} {tf} {s['type']}: {direction} (вход {s['levels']['price']:.2f})\n"
    if msg == "📌 Активные сигналы:\n":
        msg += "Нет активных сигналов."
    await update.message.reply_text(msg)

async def asset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, asset_name):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"⏳ Загружаю данные по {asset_name}...")
    await asyncio.sleep(random.uniform(0.5, 1.5))
    symbol = ASSETS[asset_name]['symbol']
    msg = f"💰 {asset_name} ({symbol})\n"
    price = get_current_price(symbol)
    if price is None:
        await update.message.reply_text(f"❌ Не удалось получить цену для {asset_name}")
        return
    msg += f"Цена: ${float(price):.2f}\n\n"
    for tf in ASSET_TIMEFRAMES[asset_name]:
        msg += f"⏱ {tf}\n"
        sigs = active_signals.get(asset_name, {}).get(tf, [])
        if not sigs:
            msg += "  Нет активных сигналов.\n"
        else:
            for s in sigs:
                if s['closed']: continue
                direction = "покупку" if s['signal'] == "BUY" else "продажу"
                stars = get_signal_stars(s['type'])
                msg += f"  {stars} {s['type'].upper()} на {direction}\n"
                msg += f"    Вход: {s['levels']['price']:.2f} | SL: {s['levels']['sl']:.2f} | TP1: {s['levels']['tp1']:.2f}\n"
        msg += "\n"
    await update.message.reply_text(msg)

async def gold(update, context): await asset_cmd(update, context, "GOLD")
async def btc(update, context): await asset_cmd(update, context, "BTC")
async def eth(update, context): await asset_cmd(update, context, "ETH")
async def sol(update, context): await asset_cmd(update, context, "SOL")

async def crypto(update, context):
    msg = "📊 СВОДКА ПО АКТИВАМ (активные сигналы):\n\n"
    for name in ASSETS:
        msg += f"**{name}** ({ASSETS[name]['symbol']})\n"
        for tf in ASSET_TIMEFRAMES[name]:
            sigs = [s for s in active_signals.get(name, {}).get(tf, []) if not s['closed']]
            if sigs:
                signals_str = ", ".join(f"{s['type']}:{s['signal']}" for s in sigs)
                msg += f"  {tf}: {signals_str}\n"
            else:
                msg += f"  {tf}: нет\n"
    await update.message.reply_text(msg)

async def status(update, context):
    msg = "📌 АКТИВНЫЕ СИГНАЛЫ:\n\n"
    for name in ASSETS:
        for tf in ASSET_TIMEFRAMES[name]:
            sigs = [s for s in active_signals.get(name, {}).get(tf, []) if not s['closed']]
            for s in sigs:
                msg += f"{name} {tf} {s['type']}: {s['signal']} (вход {s['levels']['price']:.2f})\n"
    if msg == "📌 АКТИВНЫЕ СИГНАЛЫ:\n\n":
        msg += "Нет активных сигналов."
    await update.message.reply_text(msg)

async def today_report(update, context):
    report = await generate_today_report()
    await update.message.reply_text(report)

async def ai_command(update, context):
    if not context.args:
        await update.message.reply_text("Укажите актив: /ai BTC")
        return
    asset_name = context.args[0].upper()
    if asset_name not in ASSETS:
        await update.message.reply_text("Доступны: GOLD, BTC, ETH, SOL")
        return
    last_signal = None
    for tf in reversed(ASSET_TIMEFRAMES[asset_name]):
        sigs = [s for s in active_signals.get(asset_name, {}).get(tf, []) if not s['closed']]
        if sigs:
            last_signal = sigs[-1]
            break
    if not last_signal:
        await update.message.reply_text("Нет активных сигналов для анализа.")
        return
    analysis = last_signal.get('ai_analysis')
    if analysis:
        await update.message.reply_text(f"🧠 Анализ для {asset_name} ({last_signal['tf']}):\n\n{analysis}")
    else:
        await update.message.reply_text("AI-анализ отсутствует.")

async def post_init(app):
    await start_scheduler(app)

def run_bot():
    print("🤖 Бот запускается...")
    if GIGACHAT_AUTH_KEY:
        print("🧠 GigaChat AI включён")
    else:
        print("⚠️ GigaChat AI отключён")

    print("🔑 Проверяю доступ к Bybit TradFi...")
    result = check_bybit_tradfi()
    print(f"ℹ️ Результат проверки Bybit: {result}, GOLD_SYMBOL={GOLD_SYMBOL}")

    print("📋 Конфигурация таймфреймов:")
    for asset, tfs in ASSET_TIMEFRAMES.items():
        print(f"  {asset}: {tfs}")
    print(f"ℹ️ GOLD источник: {GOLD_SYMBOL}")

    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gold", gold))
    app.add_handler(CommandHandler("btc", btc))
    app.add_handler(CommandHandler("eth", eth))
    app.add_handler(CommandHandler("sol", sol))
    app.add_handler(CommandHandler("crypto", crypto))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("today", today_report))
    app.add_handler(CommandHandler("ai", ai_command))
    app.add_handler(CommandHandler("channel_on", channel_on))
    app.add_handler(CommandHandler("channel_off", channel_off))
    print("✅ Бот готов")
    app.run_polling(drop_pending_updates=True)

def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()

if __name__ == "__main__":
    main()
