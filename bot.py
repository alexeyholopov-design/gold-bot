import os, time, threading, random, asyncio, requests, json, uuid, feedparser, hmac, hashlib, urllib.parse
import pandas as pd
import numpy as np
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from datetime import time as dt_time, datetime, timedelta, timezone
from collections import defaultdict
import urllib3
import logging
from bs4 import BeautifulSoup
import re

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------- Логирование ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ---------- Конфигурация ----------
TOKEN = os.environ.get('TOKEN')
if not TOKEN:
    logger.error("TOKEN environment variable not set")
    raise ValueError("TOKEN environment variable not set")

CHANNEL_ID = os.environ.get('CHANNEL_ID')
SEND_TO_CHANNEL = True
ENABLE_GOLD_1M = False

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
telegram_app = None

ASSET_TIMEFRAMES = {
    "GOLD": ["5m", "15m"],
    "BTC":  ["15m", "1h"],
    "ETH":  ["15m", "1h"],
    "SOL":  ["15m", "1h"],
}

GOLD_SYMBOL = "XAUT-USDT"
ASSETS = {
    "GOLD": {"symbol": GOLD_SYMBOL, "tag": "#XAU"},
    "BTC":  {"symbol": "BTC-USDT", "tag": "#BTC"},
    "ETH":  {"symbol": "ETH-USDT", "tag": "#ETH"},
    "SOL":  {"symbol": "SOL-USDT", "tag": "#SOL"},
}

for name, asset in ASSETS.items():
    active_signals[name] = {}
    for tf in ASSET_TIMEFRAMES[name]:
        active_signals[name][tf] = []

RSI_PERIOD = 14; RSI_OVERBOUGHT = 70; RSI_OVERSOLD = 30
LOOKBACK = 50
EMA_FAST = 20; EMA_SLOW = 50; EMA_FAST_FAST = 3; EMA_SLOW_FAST = 10

ATR_MULTIPLIERS = {
    "1m":  {"SL": 1.7, "TP1": 2.3, "TP2": 3.5, "TP3": 0},
    "5m":  {"SL": 1.2, "TP1": 1.5, "TP2": 2.0, "TP3": 3.0},
    "15m": {"SL": 1.5, "TP1": 2.0, "TP2": 3.0, "TP3": 5.0},
    "1h":  {"SL": 2.0, "TP1": 3.0, "TP2": 5.0, "TP3": 8.0},
}

def safe_format(value, format_spec=":.2f"):
    try:
        if value is None: return "0.00"
        num = float(value)
        if np.isnan(num) or not np.isfinite(num): return "0.00"
        return f"{num:{format_spec}}"
    except: return str(value)

def get_signal_stars(signal_type):
    return {"rsi": "⭐⭐", "ema": "⭐⭐", "combined": "⭐⭐⭐", "fast_ema": "⭐"}.get(signal_type, "")

# ---------- Bybit TradFi ----------
def bybit_sign_request(params):
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    param_str = urllib.parse.urlencode(sorted(params.items()))
    sign_str = f"{timestamp}{BYBIT_API_KEY}{recv_window}{param_str}"
    signature = hmac.new(bytes(BYBIT_API_SECRET, "utf-8"), bytes(sign_str, "utf-8"), hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": recv_window,
    }

def check_bybit_tradfi():
    global GOLD_SYMBOL
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        logger.warning("⚠️ Ключи Bybit не заданы, GOLD будет работать через BingX")
        return False
    try:
        params = {"category": "tradfi", "symbol": "XAUUSDT+"}
        headers = bybit_sign_request(params)
        url = "https://api.bybit.com/v5/market/tickers"
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code == 403:
            logger.error("❌ Bybit заблокировал запрос (403). TradFi недоступен.")
            return False
        data = resp.json()
        if data.get("retCode") == 0 and data.get("result", {}).get("list"):
            price = float(data["result"]["list"][0]["lastPrice"])
            logger.info(f"✅ Доступ к Bybit TradFi подтверждён. Цена XAUUSDT+: ${price:.2f}")
            GOLD_SYMBOL = "XAUUSDT+"
            ASSETS["GOLD"]["symbol"] = GOLD_SYMBOL
            return True
        else:
            logger.error(f"❌ Bybit вернул ошибку: {data.get('retMsg', 'Unknown error')}")
            return False
    except Exception as e:
        logger.error(f"❌ Исключение при проверке Bybit TradFi: {e}")
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
    except: pass
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
        logger.error(f"❌ Ошибка токена GigaChat: {e}")
    return None

async def ask_gigachat(prompt):
    token = await get_gigachat_token()
    if not token: return None
    try:
        url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {"model": "GigaChat", "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 300}
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
        logger.error(f"❌ GigaChat error: {e}")
    return None

# ---------- НОВОСТИ ----------
def fetch_news(asset):
    if asset == "GOLD":
        try:
            feed = feedparser.parse("https://www.kitco.com/rss/")
            entries = feed.entries[:5]
            titles = [entry.title for entry in entries if hasattr(entry, 'title')]
            if titles:
                logger.info(f"📰 GOLD: Kitco RSS — {len(titles)} заголовков")
                return " ".join(titles)
        except Exception as e:
            logger.warning(f"⚠️ Kitco RSS ошибка: {e}")

        try:
            google_url = "https://news.google.com/rss/search?q=gold+price&hl=en-US&gl=US&ceid=US:en"
            feed = feedparser.parse(google_url)
            entries = feed.entries[:5]
            titles = [entry.title for entry in entries if hasattr(entry, 'title')]
            if titles:
                logger.info(f"📰 GOLD: Google News — {len(titles)} заголовков")
                return " ".join(titles)
        except Exception as e:
            logger.warning(f"⚠️ Google News RSS ошибка: {e}")

        try:
            resp = requests.get("https://www.kitco.com", timeout=10)
            soup = BeautifulSoup(resp.text, 'html.parser')
            items = soup.select('a.article-title, h3.title, .news-title a')
            titles = [el.get_text(strip=True) for el in items[:5]]
            if titles:
                logger.info(f"📰 GOLD: Kitco HTML парсинг — {len(titles)} заголовков")
                return " ".join(titles)
        except Exception as e:
            logger.warning(f"⚠️ Kitco парсинг ошибка: {e}")

        logger.warning("⚠️ Все источники для GOLD вернули пустой результат")
        return ""

    rss_urls = {
        "BTC": "https://cointelegraph.com/rss/tag/bitcoin",
        "ETH": "https://cointelegraph.com/rss/tag/ethereum",
        "SOL": "https://cointelegraph.com/rss/tag/solana",
    }
    url = rss_urls.get(asset)
    if not url:
        return ""
    try:
        feed = feedparser.parse(url)
        entries = feed.entries[:5]
        titles = [entry.title for entry in entries if hasattr(entry, 'title')]
        if titles:
            logger.info(f"📰 {asset}: Cointelegraph — {len(titles)} заголовков")
            return " ".join(titles)
    except Exception as e:
        logger.warning(f"⚠️ {asset} RSS ошибка: {e}")
    return ""

async def analyze_news_with_gigachat(asset, news_text):
    if not news_text: return "Новостей нет."
    prompt = (f"Проанализируй новости по активу {asset}. Новости:\n{news_text}\n\n"
              "Дай КРАТКУЮ оценку на русском языке (1-2 предложения): общее настроение, ключевое событие, влияние на цену в ближайшие часы.")
    return await ask_gigachat(prompt)

async def update_news_sentiment(context: ContextTypes.DEFAULT_TYPE = None):
    global news_sentiment
    try:
        logger.info("📰 Обновление новостного фона...")
        for asset in ASSETS:
            news_text = fetch_news(asset)
            if news_text:
                analysis = await analyze_news_with_gigachat(asset, news_text)
                news_sentiment[asset] = analysis if analysis else "Анализ недоступен"
                logger.info(f"📰 {asset}: {news_sentiment[asset][:100]}...")
            else:
                news_sentiment[asset] = "Новостей не найдено."
        logger.info("✅ Новостной фон обновлён")
    except Exception as e:
        logger.error(f"❌ Ошибка в update_news_sentiment: {e}", exc_info=True)

# ---------- ТЕХНИЧЕСКИЙ ПАСПОРТ ----------
def get_technical_passport(asset_name, tf, signal, price,
                           rsi=None, ema_fast=None, ema_slow=None, atr=None,
                           volume=None, avg_volume=None, vwap=None, higher_trend=None):
    """
    Собирает краткий технический «паспорт» для передачи в GigaChat.
    """
    lines = []
    # Тренд старшего ТФ
    if higher_trend:
        trend_label = "восходящий" if higher_trend == "UP" else "нисходящий"
        lines.append(f"Тренд старшего ТФ: {trend_label}")
    else:
        lines.append("Тренд старшего ТФ: не определён")

    # RSI
    if rsi is not None:
        try:
            rsi_val = float(rsi)
            if rsi_val > 70:
                zone = "перекупленность"
            elif rsi_val < 30:
                zone = "перепроданность"
            else:
                zone = "нейтрально"
            lines.append(f"RSI(14): {rsi_val:.1f} ({zone})")
        except:
            lines.append(f"RSI(14): {rsi}")

    # EMA
    if ema_fast is not None and ema_slow is not None:
        try:
            ema_f = float(ema_fast)
            ema_s = float(ema_slow)
            if ema_f > ema_s:
                lines.append(f"EMA: EMA{EMA_FAST}>{EMA_SLOW} (бычье пересечение)")
            else:
                lines.append(f"EMA: EMA{EMA_FAST}<{EMA_SLOW} (медвежье)")
        except:
            lines.append(f"EMA: {ema_fast}/{ema_slow}")

    # VWAP
    if vwap is not None and not (isinstance(vwap, float) and np.isnan(vwap)):
        try:
            vwap_f = float(vwap)
            if price > vwap_f:
                lines.append(f"Цена выше VWAP ({vwap_f:.2f})")
            else:
                lines.append(f"Цена ниже VWAP ({vwap_f:.2f})")
        except:
            pass

    # ATR и волатильность
    if atr is not None:
        try:
            atr_f = float(atr)
            atr_pct = atr_f / float(price) * 100
            if atr_pct < 0.05:
                vol = "низкая"
            elif atr_pct < 0.1:
                vol = "средняя"
            else:
                vol = "высокая"
            lines.append(f"ATR: {atr_f:.2f} ({atr_pct:.2f}% от цены, волатильность {vol})")
        except:
            lines.append(f"ATR: {atr}")

    # Объём
    if volume is not None and avg_volume is not None:
        try:
            vol_now = float(volume)
            vol_avg = float(avg_volume)
            if vol_avg > 0:
                ratio = vol_now / vol_avg
                if ratio > 1.5:
                    vol_desc = "повышенный"
                elif ratio < 0.5:
                    vol_desc = "пониженный"
                else:
                    vol_desc = "средний"
                lines.append(f"Объём: {ratio:.1f}x от среднего ({vol_desc})")
        except:
            pass

    # Свечной паттерн (только для младших ТФ)
    if tf in ["1m", "5m"]:
        try:
            symbol = ASSETS[asset_name]["symbol"]
            df = get_klines(symbol, tf, limit=5)
            if df is not None and len(df) >= 2:
                last = df.iloc[-1]
                prev = df.iloc[-2]
                body = abs(last['Close'] - last['Open'])
                upper_shadow = last['High'] - max(last['Close'], last['Open'])
                lower_shadow = min(last['Close'], last['Open']) - last['Low']
                total_range = last['High'] - last['Low'] if last['High'] != last['Low'] else 0.0001
                # Пинбар
                if (lower_shadow > 2*body and upper_shadow < body) or (upper_shadow > 2*body and lower_shadow < body):
                    lines.append("Свечной паттерн: пинбар")
                # Поглощение
                elif (last['Close'] > last['Open'] and prev['Close'] < prev['Open'] and
                      last['Close'] > prev['Open'] and last['Open'] < prev['Close']):
                    lines.append("Свечной паттерн: бычье поглощение")
                elif (last['Close'] < last['Open'] and prev['Close'] > prev['Open'] and
                      last['Open'] > prev['Close'] and last['Close'] < prev['Open']):
                    lines.append("Свечной паттерн: медвежье поглощение")
        except:
            pass

    return "\n".join(lines) if lines else "Технический паспорт недоступен"

# ---------- AI анализ сигналов ----------
async def get_ai_analysis(asset_name, signal_type, signal, price, rsi, ema_fast=None, ema_slow=None,
                          atr=None, volume=None, avg_volume=None, vwap=None, higher_trend=None, tf=None):
    if not GIGACHAT_AUTH_KEY: return None
    direction = "покупку" if signal == "BUY" else "продажу"
    price_str = safe_format(price)

    # Собираем технический паспорт
    passport = get_technical_passport(
        asset_name, tf, signal, price,
        rsi=rsi, ema_fast=ema_fast, ema_slow=ema_slow, atr=atr,
        volume=volume, avg_volume=avg_volume, vwap=vwap, higher_trend=higher_trend
    )

    news_text = news_sentiment.get(asset_name, "Новостной фон не оценён.")

    prompt = f"""
Ты – опытный трейдер. Дай КРАТКУЮ оценку (2-3 предложения) на русском языке, учитывая технический паспорт и новости.

Актив: {asset_name} [{tf}]
Сигнал: {signal_type} на {direction}
Цена: ${price_str}

Технический паспорт:
{passport}

Новостной фон: {news_text}

Формат ответа (строго):
1. Оценка ситуации: ...
2. Риск: ...
3. Рекомендация: BUY/SELL/HOLD
4. Сила сигнала: ✅ СИЛЬНЫЙ или ⚠️ СЛАБЫЙ
"""
    try: return await ask_gigachat(prompt)
    except Exception as e:
        logger.error(f"❌ Ошибка AI: {e}")
        return None

# ---------- Рыночные данные ----------
def get_current_price(symbol):
    if GOLD_SYMBOL == "XAUUSDT+" and symbol == "XAUUSDT+":
        return get_bybit_price(symbol)
    try:
        url = "https://open-api.bingx.com/openApi/swap/v2/quote/price"
        params = {"symbol": symbol}
        response = requests.get(url, params=params, timeout=5)
        if response.status_code != 200: return None
        data = response.json()
        if data.get("code") == 0: return float(data["data"]["price"])
        return None
    except: return None

def get_klines(symbol, interval, limit=100):
    try:
        url = "https://open-api.bingx.com/openApi/swap/v2/quote/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        response = requests.get(url, params=params, timeout=5)
        if response.status_code != 200: return None
        data = response.json()
        if data.get("code") != 0: return None
        candles = data["data"]
        df = pd.DataFrame(candles)
        df.rename(columns={'open':'Open','close':'Close','high':'High','low':'Low','volume':'Volume','time':'Timestamp'}, inplace=True)
        for col in ["Open","High","Low","Close","Volume"]:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df.dropna(subset=["Open","High","Low","Close"])
    except: return None

def get_rsi_and_bars(symbol, interval):
    df = get_klines(symbol, interval, limit=LOOKBACK)
    if df is None or len(df) < 2: return None, None, None, None
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
    if df is None or len(df) < slow: return None, None, None, None, None
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
    if prev_fast <= prev_slow and cur_fast > cur_slow: signal = "BUY"
    elif prev_fast >= prev_slow and cur_fast < cur_slow: signal = "SELL"
    return signal, cur_fast, cur_slow, prev_fast, prev_slow

def get_atr_value(symbol, interval):
    df = get_klines(symbol, interval, limit=LOOKBACK)
    if df is None or len(df) < 14: return None
    high = df['High'].values; low = df['Low'].values; close = df['Close'].values
    tr = np.maximum(high-low, np.maximum(abs(high-np.roll(close,1)), abs(low-np.roll(close,1))))
    tr[0] = high[0]-low[0]
    atr = np.zeros_like(tr); atr[:14] = np.mean(tr[:14])
    for i in range(14, len(tr)): atr[i] = (atr[i-1]*13 + tr[i])/14
    return atr[-1]

def get_atr_stats(symbol, interval):
    """Возвращает текущий ATR и средний ATR за 14 свечей (для динамических множителей)."""
    df = get_klines(symbol, interval, limit=64)
    if df is None or len(df) < 15: return None, None
    high = df['High'].values; low = df['Low'].values; close = df['Close'].values
    tr = np.maximum(high-low, np.maximum(abs(high-np.roll(close,1)), abs(low-np.roll(close,1))))
    tr[0] = high[0]-low[0]
    atr = np.zeros_like(tr); atr[:14] = np.mean(tr[:14])
    for i in range(14, len(tr)): atr[i] = (atr[i-1]*13 + tr[i])/14
    current_atr = atr[-1]
    avg_atr14 = np.mean(atr[-14:]) if len(atr) >= 14 else current_atr
    return current_atr, avg_atr14

def get_ema_value(symbol, interval, period):
    df = get_klines(symbol, interval, limit=LOOKBACK)
    if df is None or len(df) < period: return None
    close = df['Close'].values
    ema = np.zeros_like(close)
    alpha = 2/(period+1)
    ema[0] = close[0]
    for i in range(1, len(close)):
        ema[i] = alpha*close[i] + (1-alpha)*ema[i-1]
    return ema[-1]

def get_trend_direction(symbol, base_interval, check_interval, fast=20, slow=50):
    df = get_klines(symbol, interval=check_interval, limit=LOOKBACK)
    if df is None or len(df) < slow: return None
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

def calculate_atr_levels(price, atr, signal_type, tf, custom_mult=None):
    mult = custom_mult if custom_mult else ATR_MULTIPLIERS.get(tf, {"SL": 1.5, "TP1": 2.0, "TP2": 3.0, "TP3": 5.0})
    if signal_type == "BUY":
        sl = price - atr * mult["SL"]
        tp1 = price + atr * mult["TP1"]
        tp2 = price + atr * mult["TP2"]
        tp3 = price + atr * mult.get("TP3", 0)
    else:
        sl = price + atr * mult["SL"]
        tp1 = price - atr * mult["TP1"]
        tp2 = price - atr * mult["TP2"]
        tp3 = price - atr * mult.get("TP3", 0)
    return {'price': round(price,2), 'sl': round(sl,2), 'tp1': round(tp1,2),
            'tp2': round(tp2,2), 'tp3': round(tp3,2) if tp3 != 0 else 0, 'atr': round(atr,2)}

def create_signal_dict(asset_name, tf, signal_type, signal, levels, ai_analysis=None):
    return {
        'timestamp': datetime.now(timezone.utc),
        'asset': asset_name, 'tf': tf, 'type': signal_type, 'signal': signal,
        'levels': levels.copy(),
        'tp1_hit': False, 'tp2_hit': False, 'tp3_hit': False,
        'sl_hit': False, 'closed': False,
        'ai_analysis': ai_analysis, 'adjusted_by_ai': False
    }

def add_active_signal(asset_name, tf, signal_dict):
    if asset_name not in active_signals: active_signals[asset_name] = {}
    if tf not in active_signals[asset_name]: active_signals[asset_name][tf] = []
    active_signals[asset_name][tf].append(signal_dict)

# ---------- Проверка уровней ----------
async def check_signal_levels(bot, signal_dict):
    levels = signal_dict['levels']
    if signal_dict['closed']: return
    asset_name = signal_dict['asset']
    symbol = ASSETS[asset_name]['symbol']
    price = get_current_price(symbol)
    if price is None: return

    is_buy = signal_dict['signal'] == 'BUY'
    now = datetime.now(timezone.utc)
    minutes = (now - signal_dict['timestamp']).total_seconds() / 60
    tag = ASSETS[asset_name]['tag']

    if not signal_dict['sl_hit']:
        sl_hit = (is_buy and price <= levels['sl']) or (not is_buy and price >= levels['sl'])
        if sl_hit:
            signal_dict['sl_hit'] = True
            if not signal_dict['tp1_hit']:
                signal_dict['closed'] = True
                msg = (f"{tag} ❌ Стоп-лосс сработал по {asset_name} [{signal_dict['tf']}] ({signal_dict['type']})\n"
                       f"Вход: ${levels['price']:.2f}\nSL: ${levels['sl']:.2f}\n"
                       f"⏱ Время сделки: {minutes:.1f} мин.")
                await send_to_chat(FakeContext(bot), msg)
            else:
                signal_dict['closed'] = True
                msg = (f"{tag} 🔒 Безубыток по {asset_name} [{signal_dict['tf']}] ({signal_dict['type']})\n"
                       f"Вход: ${levels['price']:.2f}\nСтоп: ${levels['sl']:.2f}\n"
                       f"⏱ Время сделки: {minutes:.1f} мин.")
                await send_to_chat(FakeContext(bot), msg)
            logger.info(f"✅ Завершение сделки для {asset_name} {signal_dict['tf']}")
            return

    if not signal_dict['sl_hit']:
        if not signal_dict['tp1_hit']:
            tp1_hit = (is_buy and price >= levels['tp1']) or (not is_buy and price <= levels['tp1'])
            if tp1_hit:
                signal_dict['tp1_hit'] = True
                signal_dict['levels']['sl'] = levels['price']
                msg = (f"{tag} ✅ TP1 достигнут по {asset_name} [{signal_dict['tf']}] ({signal_dict['type']})\n"
                       f"Вход: ${levels['price']:.2f}\nTP1: ${levels['tp1']:.2f}\n"
                       f"🔒 Стоп перенесён в безубыток\n⏱ Время сделки: {minutes:.1f} мин.")
                await send_to_chat(FakeContext(bot), msg)
                logger.info(f"✅ TP1 (безубыток) для {asset_name} {signal_dict['tf']}")
        else:
            if not signal_dict['tp2_hit']:
                tp2_hit = (is_buy and price >= levels['tp2']) or (not is_buy and price <= levels['tp2'])
                if tp2_hit:
                    signal_dict['tp2_hit'] = True
                    close_trade = signal_dict['type'] != 'fast_ema'
                    if close_trade:
                        signal_dict['closed'] = True
                        close_msg = " (сделка завершена)"
                    else:
                        close_msg = ""
                    msg = (f"{tag} ✅ TP2 достигнут по {asset_name} [{signal_dict['tf']}] ({signal_dict['type']})\n"
                           f"Вход: ${levels['price']:.2f}\nTP2: ${levels['tp2']:.2f}{close_msg}\n"
                           f"⏱ Время сделки: {minutes:.1f} мин.")
                    await send_to_chat(FakeContext(bot), msg)
                    logger.info(f"✅ TP2 для {asset_name} {signal_dict['tf']}")
            if not signal_dict['tp3_hit'] and not signal_dict['closed'] and levels.get('tp3', 0) != 0:
                tp3_hit = (is_buy and price >= levels['tp3']) or (not is_buy and price <= levels['tp3'])
                if tp3_hit:
                    signal_dict['tp3_hit'] = True
                    signal_dict['closed'] = True
                    msg = (f"{tag} ✅ TP3 достигнут по {asset_name} [{signal_dict['tf']}] ({signal_dict['type']})\n"
                           f"Вход: ${levels['price']:.2f}\nTP3: ${levels['tp3']:.2f} (сделка завершена)\n"
                           f"⏱ Время сделки: {minutes:.1f} мин.")
                    await send_to_chat(FakeContext(bot), msg)
                    logger.info(f"✅ TP3 для {asset_name} {signal_dict['tf']}")

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

# ---------- Отправка нового сигнала ----------
async def handle_new_signal(asset_name, tf, signal_type, signal, price, rsi=None, ema_fast=None, ema_slow=None,
                            cur_fast3=None, cur_slow10=None, atr=None, volume=None, avg_volume=None, vwap=None,
                            higher_trend=None, context=None, simple_message=False, range_entry=None):
    if has_open_signal(asset_name, tf, signal_type, signal): return
    levels = calculate_atr_levels(price, atr, signal, tf)

    # AI-анализ теперь для всех сигналов
    ai_analysis = await get_ai_analysis(
        asset_name, signal_type, signal, price, rsi,
        ema_fast=ema_fast, ema_slow=ema_slow, atr=atr,
        volume=volume, avg_volume=avg_volume, vwap=vwap,
        higher_trend=higher_trend, tf=tf
    )
    signal_dict = create_signal_dict(asset_name, tf, signal_type, signal, levels, ai_analysis)
    add_active_signal(asset_name, tf, signal_dict)

    stars = get_signal_stars(signal_type)
    direction = "покупку" if signal == "BUY" else "продажу"
    symbol = ASSETS[asset_name]['symbol']
    tag = ASSETS[asset_name]['tag']

    if simple_message:
        entry_from = range_entry['from']
        entry_to = range_entry['to']
        msg = f"{tag} ⭐ 📢 Входим в {direction.upper()} {asset_name} [{tf}]\n"
        msg += f"🔹 Диапазон входа: ${entry_from:.2f} – ${entry_to:.2f}\n"
        msg += f"🛑 Стоп-лосс: ${levels['sl']:.2f}\n"
        msg += f"🎯 Тейк-профит 1: ${levels['tp1']:.2f}\n"
        msg += f"🎯 Тейк-профит 2: ${levels['tp2']:.2f}"
    else:
        msg = f"{tag} {stars} 📢 Сигнал на {direction} по {signal_type.upper()} для {asset_name} ({symbol}) [{tf}]\n"
        msg += f"💰 Вход: ${levels['price']:.2f}\n"
        msg += f"🛑 SL: ${levels['sl']:.2f} (ATR×{ATR_MULTIPLIERS.get(tf, {}).get('SL', '?')})\n"
        msg += f"🎯 TP1: ${levels['tp1']:.2f} (ATR×{ATR_MULTIPLIERS.get(tf, {}).get('TP1', '?')})\n"
        msg += f"🎯 TP2: ${levels['tp2']:.2f} (ATR×{ATR_MULTIPLIERS.get(tf, {}).get('TP2', '?')})\n"
        if levels.get('tp3', 0) != 0:
            msg += f"🎯 TP3: ${levels['tp3']:.2f} (ATR×{ATR_MULTIPLIERS.get(tf, {}).get('TP3', '?')})\n"
        if rsi is not None: msg += f"📊 RSI: {float(rsi):.1f}\n"
        if ema_fast is not None: msg += f"📊 EMA: {float(ema_fast):.2f} / {float(ema_slow):.2f}\n"
        if cur_fast3 is not None: msg += f"📊 EMA(3/10): {float(cur_fast3):.2f} / {float(cur_slow10):.2f}\n"
        if volume is not None: msg += f"📊 Объём: {float(volume):.0f} | Средний: {float(avg_volume):.0f} | VWAP: ${float(vwap):.2f}\n"

    if ai_analysis:
        msg += f"\n🧠 {ai_analysis}"
    await send_to_chat(context, msg)

# ---------- Основная логика ----------
async def check_and_send_signal(context: ContextTypes.DEFAULT_TYPE):
    logger.info("⏰ Автоматическая проверка запущена")
    if CHANNEL_ID is None and chat_id is None:
        logger.warning("⚠️ Нет получателей")
        return

    now_msk = get_moscow_time()
    if now_msk.hour >= 23 or now_msk.weekday() in (5, 6):
        logger.info("⏸️ Генерация сигналов приостановлена (время/выходной)")
    else:
        for name, asset in ASSETS.items():
            symbol = asset['symbol']
            for tf in ASSET_TIMEFRAMES[name]:
                price = get_current_price(symbol)
                if price is None: continue
                try:
                    df_vol = get_klines(symbol, tf, limit=50)
                    avg_volume = None; vwap = None; volume_now = None
                    if df_vol is not None and len(df_vol) >= 2:
                        volume_now = df_vol['Volume'].iloc[-1]
                        if pd.notna(volume_now):
                            avg_volume = df_vol['Volume'].mean()
                            typical_price = (df_vol['High'] + df_vol['Low'] + df_vol['Close']) / 3
                            if df_vol['Volume'].sum() > 0:
                                vwap = (typical_price * df_vol['Volume']).sum() / df_vol['Volume'].sum()
                            else: vwap = price

                    vol_threshold = 0.6 if tf in ["15m", "1h"] else 0.8
                    if volume_now is not None and avg_volume is not None and avg_volume > 0:
                        if volume_now < avg_volume * vol_threshold:
                            logger.info(f"ℹ️ Сигнал для {name} {tf} пропущен: объём {volume_now:.0f} < средний {avg_volume:.0f}")
                            continue

                    if tf != "5m" and vwap is not None and not np.isnan(vwap): pass

                    current_rsi, prev_rsi, _, _ = get_rsi_and_bars(symbol, tf)
                    atr = get_atr_value(symbol, tf)

                    # EMA
                    ema_signal, cur_fast, cur_slow, _, _ = get_ema_cross(symbol, tf, EMA_FAST, EMA_SLOW)
                    if ema_signal and atr is not None:
                        if tf != "5m" and vwap is not None and not np.isnan(vwap):
                            if (ema_signal == "BUY" and price <= vwap) or (ema_signal == "SELL" and price >= vwap):
                                logger.info(f"ℹ️ EMA {ema_signal} для {name} {tf} пропущен: VWAP")
                                continue
                        await handle_new_signal(name, tf, "ema", ema_signal, price,
                                                ema_fast=cur_fast, ema_slow=cur_slow, atr=atr,
                                                volume=volume_now, avg_volume=avg_volume, vwap=vwap, context=context)

                    # Combined
                    rsi_signal = None
                    if current_rsi is not None and prev_rsi is not None:
                        if prev_rsi < RSI_OVERSOLD and current_rsi >= RSI_OVERSOLD: rsi_signal = "BUY"
                        elif prev_rsi > RSI_OVERBOUGHT and current_rsi <= RSI_OVERBOUGHT: rsi_signal = "SELL"
                    if rsi_signal and ema_signal and atr is not None:
                        if rsi_signal == "BUY" and cur_fast > cur_slow: combined_signal = "BUY"
                        elif rsi_signal == "SELL" and cur_fast < cur_slow: combined_signal = "SELL"
                        else: combined_signal = None
                        if combined_signal:
                            if tf != "5m" and vwap is not None and not np.isnan(vwap):
                                if (combined_signal == "BUY" and price <= vwap) or (combined_signal == "SELL" and price >= vwap):
                                    logger.info(f"ℹ️ Combined для {name} {tf} пропущен: VWAP")
                                    continue
                            await handle_new_signal(name, tf, "combined", combined_signal, price,
                                                    rsi=current_rsi, ema_fast=cur_fast, ema_slow=cur_slow, atr=atr,
                                                    volume=volume_now, avg_volume=avg_volume, vwap=vwap, context=context)

                    # FAST EMA
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
                                    logger.info(f"ℹ️ FAST_EMA для {name} {tf} пропущен: VWAP")
                                    continue
                            await handle_new_signal(name, tf, "fast_ema", fast_cross, price,
                                                    cur_fast3=cur_fast3, cur_slow10=cur_slow10, atr=atr,
                                                    volume=volume_now, avg_volume=avg_volume, vwap=vwap,
                                                    higher_trend=trend if higher_tf else None, context=context)
                except Exception as e:
                    logger.error(f"❌ Ошибка в check_and_send_signal для {name} {tf}: {e}")

            # === GOLD 1m FAST_EMA ===
            if name == "GOLD" and ENABLE_GOLD_1M:
                try:
                    fast_cross_1m, cur_fast3_1m, cur_slow10_1m, _, _ = get_ema_cross(symbol, "1m", EMA_FAST_FAST, EMA_SLOW_FAST)
                    if fast_cross_1m:
                        atr_1m = get_atr_value(symbol, "1m")
                        if atr_1m is None: continue
                        if atr_1m < price * 0.0005:
                            logger.info(f"ℹ️ GOLD 1m FAST_EMA пропущен: низкая волатильность (ATR {atr_1m:.2f})")
                            continue

                        ema50_1m = get_ema_value(symbol, "1m", 50)
                        if ema50_1m is not None:
                            if fast_cross_1m == "BUY" and price <= ema50_1m:
                                logger.info(f"ℹ️ GOLD 1m FAST_EMA BUY пропущен: цена ниже EMA50")
                                continue
                            if fast_cross_1m == "SELL" and price >= ema50_1m:
                                logger.info(f"ℹ️ GOLD 1m FAST_EMA SELL пропущен: цена выше EMA50")
                                continue

                        df_1m = get_klines(symbol, "1m", limit=5)
                        if df_1m is not None and len(df_1m) >= 2:
                            prev_high = df_1m['High'].iloc[-2]
                            prev_low = df_1m['Low'].iloc[-2]
                            current_close = df_1m['Close'].iloc[-1]
                            if fast_cross_1m == "BUY" and current_close <= prev_high:
                                logger.info(f"ℹ️ GOLD 1m FAST_EMA BUY пропущен: закрытие не выше предыдущего High")
                                continue
                            if fast_cross_1m == "SELL" and current_close >= prev_low:
                                logger.info(f"ℹ️ GOLD 1m FAST_EMA SELL пропущен: закрытие не ниже предыдущего Low")
                                continue

                        current_atr, avg_atr14 = get_atr_stats(symbol, "1m")
                        mult = ATR_MULTIPLIERS["1m"].copy()
                        if current_atr and avg_atr14 and avg_atr14 > 0:
                            if current_atr > avg_atr14 * 1.3:
                                mult = {k: v*1.2 for k, v in mult.items()}
                            elif current_atr < avg_atr14 * 0.7:
                                mult = {k: v*0.8 for k, v in mult.items()}
                        levels = calculate_atr_levels(price, atr_1m, fast_cross_1m, "1m", custom_mult=mult)

                        df_range = get_klines(symbol, "1m", limit=10)
                        if df_range is not None and len(df_range) >= 2:
                            min10 = df_range['Low'].min()
                            max10 = df_range['High'].max()
                            if fast_cross_1m == "BUY":
                                range_from = min10
                                range_to = price
                                levels['sl'] = round(min10 - atr_1m * 0.5, 2)
                            else:
                                range_from = price
                                range_to = max10
                                levels['sl'] = round(max10 + atr_1m * 0.5, 2)

                            trend_5m = get_trend_direction(symbol, "1m", "5m")
                            await handle_new_signal(
                                "GOLD", "1m", "fast_ema", fast_cross_1m, price,
                                atr=atr_1m, higher_trend=trend_5m, context=context,
                                simple_message=True,
                                range_entry={'from': range_from, 'to': range_to}
                            )
                except Exception as e:
                    logger.error(f"❌ Ошибка в GOLD 1m: {e}")

    await check_all_active_signals(context.bot)

# ---------- Отчёты ----------
def get_moscow_time():
    return datetime.now(timezone.utc) + timedelta(hours=3)

def calculate_stats(signals):
    total = len(signals)
    if total == 0: return None
    tp1_count = sum(1 for s in signals if s['tp1_hit'])
    tp2_count = sum(1 for s in signals if s['tp2_hit'])
    tp3_count = sum(1 for s in signals if s['tp3_hit'])
    true_sl = sum(1 for s in signals if s['sl_hit'] and not (s['tp1_hit'] or s['tp2_hit'] or s['tp3_hit']))
    closed = tp1_count + true_sl
    success_rate = (tp1_count / closed * 100) if closed > 0 else 0
    return {
        'total': total, 'tp1': tp1_count, 'tp2': tp2_count, 'tp3': tp3_count,
        'sl': true_sl, 'closed': closed, 'success_rate': success_rate
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
    signals = [s for s in signal_history if (s['tp1_hit'] or s['sl_hit']) and s['timestamp'].replace(tzinfo=timezone.utc) + timedelta(hours=3) >= yesterday]
    return format_report(signals, f"📊 Ежедневный отчёт за {now.strftime('%d.%m.%Y')}")

async def generate_weekly_report():
    now = get_moscow_time()
    week_ago = now - timedelta(days=7)
    signals = [s for s in signal_history if (s['tp1_hit'] or s['sl_hit']) and s['timestamp'].replace(tzinfo=timezone.utc) + timedelta(hours=3) >= week_ago]
    return format_report(signals, f"📊 Воскресный отчёт за неделю ({ (now-timedelta(days=7)).strftime('%d.%m')} - {now.strftime('%d.%m.%Y')})")

async def generate_today_report():
    now = get_moscow_time()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    signals = [s for s in signal_history if (s['tp1_hit'] or s['sl_hit']) and s['timestamp'].replace(tzinfo=timezone.utc) + timedelta(hours=3) >= today_start]
    return format_report(signals, f"📊 Отчёт за сегодня ({now.strftime('%d.%m.%Y')})")

def format_report(signals, title):
    if not signals: return f"{title}\n\nСигналов не было."

    stats_by_asset = defaultdict(list)
    stats_by_type = defaultdict(list)
    for s in signals:
        stats_by_asset[s['asset']].append(s)
        stats_by_type[s['type']].append(s)

    all_stats = calculate_stats(signals)
    lines = [title, ""]
    if all_stats:
        lines.append(f"Всего сигналов: {all_stats['total']}")
        lines.append(f"TP1: {all_stats['tp1']}  |  TP2: {all_stats['tp2']}  |  TP3: {all_stats['tp3']}")
        lines.append(f"SL (без TP): {all_stats['sl']}")
        lines.append(f"Общая успешность: ~{all_stats['success_rate']:.1f}%")
        lines.append("")

    asset_stats = {}
    for asset, lst in stats_by_asset.items():
        st = calculate_stats(lst)
        if st: asset_stats[asset] = st
    lines.append("📌 По инструментам:")
    for asset, st in sorted(asset_stats.items()):
        lines.append(f"{asset}: всего {st['total']}, TP1: {st['tp1']}, успешность {st['success_rate']:.1f}%")
    lines.append("")

    type_stats = {}
    for typ, lst in stats_by_type.items():
        st = calculate_stats(lst)
        if st: type_stats[typ] = st
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
    def __init__(self, bot): self.bot = bot

async def send_to_chat(context, text):
    try:
        if CHANNEL_ID is not None and SEND_TO_CHANNEL:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=text)
        if chat_id is not None:
            await context.bot.send_message(chat_id=chat_id, text=text)
        if CHANNEL_ID is None and chat_id is None:
            logger.warning("⚠️ Нет получателя для сообщения")
    except Exception as e:
        logger.error(f"❌ Ошибка в send_to_chat: {e}")

async def channel_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SEND_TO_CHANNEL
    SEND_TO_CHANNEL = True
    await update.message.reply_text("✅ Отправка в канал включена.")

async def channel_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SEND_TO_CHANNEL
    SEND_TO_CHANNEL = False
    await update.message.reply_text("⏸️ Отправка в канал приостановлена.")

# ---------- Команды GOLD 1m ----------
async def gold_1m_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ENABLE_GOLD_1M
    ENABLE_GOLD_1M = True
    await update.message.reply_text("✅ GOLD 1m включён. Сигналы FAST_EMA по тренду 5m.")

async def gold_1m_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ENABLE_GOLD_1M
    ENABLE_GOLD_1M = False
    await update.message.reply_text("⏸️ GOLD 1m выключен.")

# ---------- ПАРСИНГ ИНВЕСТИНГА ----------
INVESTING_API_URL = "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData"
INVESTING_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded",
}

def _convert_to_24h_and_shift(time_str: str) -> str:
    if not time_str:
        return time_str
    time_str = time_str.strip()
    match = re.match(r'(\d{1,2}):(\d{2})\s*(AM|PM)', time_str, re.IGNORECASE)
    if match:
        hour = int(match.group(1))
        minute = match.group(2)
        ampm = match.group(3).upper()
        if ampm == 'PM' and hour != 12:
            hour += 12
        elif ampm == 'AM' and hour == 12:
            hour = 0
        hour24 = hour
    else:
        parts = time_str.split(':')
        try:
            hour24 = int(parts[0])
            minute = parts[1]
        except (ValueError, IndexError):
            return time_str
        else:
            minute = parts[1]

    shifted_hour = (hour24 + 13) % 24
    return f"{shifted_hour:02d}:{minute}"

def parse_investing_html(html_string):
    soup = BeautifulSoup(html_string, 'html.parser')
    events = []
    for row in soup.find_all('tr', id=lambda x: x and x.startswith('eventRowId_')):
        try:
            cols = row.find_all('td')
            if len(cols) < 8:
                continue
            raw_time = cols[0].get_text(strip=True)
            time_24 = _convert_to_24h_and_shift(raw_time)
            currency = cols[1].get_text(strip=True)
            name = cols[3].get_text(strip=True) if cols[3].get_text(strip=True) else cols[2].get_text(strip=True)
            if not name:
                name = cols[2].get_text(strip=True)
            actual = cols[4].get_text(strip=True) if len(cols) > 4 else ''
            forecast = cols[5].get_text(strip=True) if len(cols) > 5 else ''
            previous = cols[6].get_text(strip=True) if len(cols) > 6 else ''

            events.append({
                'time': time_24,
                'currency': currency,
                'event': name,
                'actual': actual if actual != '' else None,
                'forecast': forecast,
                'previous': previous
            })
        except Exception as e:
            logger.warning(f"⚠️ Ошибка парсинга строки Investing: {e}")
    return events

def get_investing_high_impact_events():
    try:
        today_str = get_moscow_time().strftime("%Y-%m-%d")
        payload = {
            "dateFrom": today_str,
            "dateTo": today_str,
            "importance[]": "3",
            "timeZone": "3",
            "currentTab": "custom",
        }
        resp = requests.post(INVESTING_API_URL, headers=INVESTING_HEADERS, data=payload, timeout=10)
        if not resp.ok:
            logger.error(f"📅 Investing.com статус: {resp.status_code}")
            return None
        data = resp.json()
        html_str = data.get('data') if isinstance(data, dict) else data
        if not html_str or not isinstance(html_str, str):
            logger.info("📅 Investing.com: пустой HTML")
            return None

        events = parse_investing_html(html_str)
        if not events:
            logger.info(f"📅 Investing.com: не найдено событий ★★★ на {today_str}")
            return None

        lines = []
        for ev in events:
            line = f"🕒 {ev['time']} | {ev['currency']} | {ev['event']}"
            if ev['forecast']:
                line += f" | Прогноз: {ev['forecast']}"
            if ev['previous']:
                line += f" | Предыдущее: {ev['previous']}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"❌ Ошибка получения календаря Investing.com: {e}")
        return None

notified_events = set()

async def check_investing_events_and_notify(context: ContextTypes.DEFAULT_TYPE):
    global notified_events
    try:
        today_str = get_moscow_time().strftime("%Y-%m-%d")
        payload = {
            "dateFrom": today_str,
            "dateTo": today_str,
            "importance[]": "3",
            "timeZone": "3",
            "currentTab": "custom",
        }
        resp = requests.post(INVESTING_API_URL, headers=INVESTING_HEADERS, data=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        html_str = data.get('data') if isinstance(data, dict) else data
        if not html_str:
            return
        events = parse_investing_html(html_str)
        for ev in events:
            actual = ev.get('actual')
            if not actual:
                continue
            event_id = f"{today_str}_{ev['time']}_{ev['currency']}_{ev['event']}"
            if event_id in notified_events:
                continue
            notified_events.add(event_id)

            msg_lines = [
                "📅 **Результат важного события (Investing.com ★★★)**",
                f"🕒 {ev['time']} МСК",
                f"💱 {ev['currency']} | {ev['event']}",
                f"📊 Прогноз: {ev['forecast'] if ev['forecast'] else '—'}",
                f"📌 Предыдущее: {ev['previous'] if ev['previous'] else '—'}",
                f"✅ Факт: {actual}"
            ]

            if GIGACHAT_AUTH_KEY:
                prompt = (
                    f"Проанализируй влияние опубликованного экономического события на рынки золота и криптовалют.\n"
                    f"Событие: {ev['event']}\n"
                    f"Валюта: {ev['currency']}\n"
                    f"Прогноз: {ev['forecast']}\n"
                    f"Предыдущее: {ev['previous']}\n"
                    f"Фактическое значение: {actual}\n\n"
                    "Опиши кратко на русском языке (2-3 предложения): как это событие может повлиять на цену золота и криптовалюты в ближайшие часы. "
                    "Укажи, какие активы (GOLD, BTC, ETH, SOL) могут быть затронуты больше всего."
                )
                impact = await ask_gigachat(prompt)
                if impact:
                    msg_lines.append(f"\n🧠 **Влияние на рынок:**\n{impact}")

            full_msg = "\n".join(msg_lines)
            logger.info(f"📢 Отправка результата события: {event_id}")
            await send_to_chat(context, full_msg)

    except Exception as e:
        logger.error(f"❌ Ошибка в check_investing_events_and_notify: {e}")

# ---------- Утренний обзор ----------
async def send_morning_report(context=None):
    logger.info("📊 Формирование утреннего обзора...")
    if context is None and telegram_app:
        context = FakeContext(telegram_app.bot)
    elif context is None:
        logger.error("❌ Нет контекста для отправки утреннего обзора")
        return

    await update_news_sentiment(context)

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

    investing_events = get_investing_high_impact_events()
    if investing_events:
        msg += "\n📅 **Важные события (Investing.com ★★★):**\n"
        msg += investing_events + "\n"
    else:
        msg += "\n📅 Важных событий (Investing.com ★★★) на сегодня нет.\n"

    await send_to_chat(context, msg)
    logger.info("✅ Утренний обзор отправлен")

async def morning_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Формирую утренний обзор...")
    await send_morning_report(context)

# ---------- Планировщик ----------
async def start_scheduler(app):
    job_queue = app.job_queue
    if job_queue is None:
        logger.warning("⚠️ JobQueue не доступен.")
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
    job_queue.run_repeating(check_investing_events_and_notify, interval=600, first=120)
    logger.info("📅 Планировщик запущен")

async def daily_report_job(context):
    logger.info("📊 Запущена задача daily_report")
    report = await generate_daily_report()
    await send_to_chat(context, report)

async def weekly_report_job(context):
    logger.info("📊 Запущена задача weekly_report")
    report = await generate_weekly_report()
    await send_to_chat(context, report)

# ---------- Команды бота ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    status_msg = "включена" if SEND_TO_CHANNEL else "приостановлена"
    gold_source = "Bybit TradFi" if GOLD_SYMBOL == "XAUUSDT+" else "BingX (возможно расхождение ~$10)"
    await update.message.reply_text(
        "👋 Бот запущен!\n"
        f"Отслеживаю: GOLD ({gold_source}), BTC, ETH, SOL.\n"
        "Таймфреймы: GOLD (5м, 15м), крипта (15м, 1ч).\n"
        "⭐ FAST EMA | ⭐⭐ EMA | ⭐⭐⭐ Combined (RSI+EMA)\n"
        "📰 Новости каждый час. Утренний обзор в 10:00 МСК.\n"
        f"📢 Отправка в канал: {status_msg}\n\n"
        "Команды:\n"
        "/gold, /btc, /eth, /sol – цена и активные сигналы\n"
        "/crypto – сводка\n"
        "/status – активные сигналы\n"
        "/today – отчёт за сегодня\n"
        "/week – отчёт за неделю\n"
        "/ai {актив} – AI-анализ\n"
        "/morning – утренний обзор\n"
        "/gold_1m_on – включить GOLD 1m\n"
        "/gold_1m_off – выключить GOLD 1m\n"
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
    if msg == "📌 Активные сигналы:\n": msg += "Нет активных сигналов."
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
        if not sigs: msg += "  Нет активных сигналов.\n"
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
            else: msg += f"  {tf}: нет\n"
    await update.message.reply_text(msg)

async def status(update, context):
    msg = "📌 АКТИВНЫЕ СИГНАЛЫ:\n\n"
    for name in ASSETS:
        for tf in ASSET_TIMEFRAMES[name]:
            sigs = [s for s in active_signals.get(name, {}).get(tf, []) if not s['closed']]
            for s in sigs:
                msg += f"{name} {tf} {s['type']}: {s['signal']} (вход {s['levels']['price']:.2f})\n"
    if msg == "📌 АКТИВНЫЕ СИГНАЛЫ:\n\n": msg += "Нет активных сигналов."
    await update.message.reply_text(msg)

async def today_report(update, context):
    report = await generate_today_report()
    await update.message.reply_text(report)

async def week_report(update, context):
    await update.message.reply_text("⏳ Формирую недельный отчёт...")
    report = await generate_weekly_report()
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

# ---------- Запуск ----------
async def post_init(app):
    await start_scheduler(app)
    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    if 6 <= now_msk.hour <= 12:
        try:
            await send_morning_report(FakeContext(app.bot))
        except Exception as e:
            logger.error(f"❌ Ошибка стартового обзора: {e}")

def run_bot():
    global telegram_app
    logger.info("🤖 Бот запускается...")
    if GIGACHAT_AUTH_KEY:
        logger.info("🧠 GigaChat AI включён")
    else:
        logger.warning("⚠️ GigaChat AI отключён")

    logger.info("🔑 Проверяю доступ к Bybit TradFi...")
    result = check_bybit_tradfi()
    logger.info(f"ℹ️ Результат проверки Bybit: {result}, GOLD_SYMBOL={GOLD_SYMBOL}")

    logger.info("📋 Конфигурация таймфреймов:")
    for asset, tfs in ASSET_TIMEFRAMES.items():
        logger.info(f"  {asset}: {tfs}")
    logger.info(f"ℹ️ GOLD источник: {GOLD_SYMBOL}")

    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gold", gold))
    app.add_handler(CommandHandler("btc", btc))
    app.add_handler(CommandHandler("eth", eth))
    app.add_handler(CommandHandler("sol", sol))
    app.add_handler(CommandHandler("crypto", crypto))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("today", today_report))
    app.add_handler(CommandHandler("week", week_report))
    app.add_handler(CommandHandler("ai", ai_command))
    app.add_handler(CommandHandler("morning", morning_command))
    app.add_handler(CommandHandler("gold_1m_on", gold_1m_on))
    app.add_handler(CommandHandler("gold_1m_off", gold_1m_off))
    app.add_handler(CommandHandler("channel_on", channel_on))
    app.add_handler(CommandHandler("channel_off", channel_off))

    telegram_app = app
    logger.info("✅ Бот готов")
    app.run_polling(drop_pending_updates=True)

def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()

if __name__ == "__main__":
    main()
