import requests
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from google.colab import files
from datetime import datetime, timedelta, timezone

# ---------- Конфигурация (как в реальном боте) ----------
ASSETS = {
    "GOLD": "XAUT-USDT",
    "BTC":  "BTC-USDT",
    "ETH":  "ETH-USDT",
    "SOL":  "SOL-USDT",
}
TIMEFRAMES = {
    "GOLD": ["5m", "15m"],
    "BTC":  ["15m", "1h"],
    "ETH":  ["15m", "1h"],
    "SOL":  ["15m", "1h"],
}
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

MIN_ATR = {
    "GOLD": {"5m": 2.5, "15m": 3.0},
    "BTC":  {"15m": 90, "1h": 200},
    "ETH":  {"15m": 4.5, "1h": 10},
    "SOL":  {"15m": 0.5, "1h": 1.3},
}

MSK = timezone(timedelta(hours=3))

# ---------- Загрузка данных ----------
def get_klines(symbol, interval, limit=500):
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=15)
    if r.status_code != 200:
        return None
    data = r.json()
    if data.get("code") != 0:
        return None
    df = pd.DataFrame(data["data"])
    df.rename(columns={'open':'Open','close':'Close','high':'High','low':'Low','volume':'Volume','time':'Timestamp'}, inplace=True)
    for c in ["Open","High","Low","Close"]:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
    df.sort_values('Timestamp', inplace=True)
    df.set_index('Timestamp', inplace=True)
    return df.dropna(subset=["Open","High","Low","Close"])

# ---------- Индикаторы ----------
def compute_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100/(1+rs))

def ema(close, period):
    return close.ewm(span=period, adjust=False).mean()

def atr(df, period=14):
    high, low, close = df['High'], df['Low'], df['Close']
    tr = np.maximum(high-low, np.maximum(abs(high-close.shift()), abs(low-close.shift())))
    return tr.ewm(span=period, adjust=False).mean()

# ---------- Проверка дня недели (МСК) ----------
def is_weekend(ts):
    if ts.tzinfo is None:
        ts = ts.tz_localize('UTC')
    msk_time = ts.astimezone(MSK)
    return msk_time.weekday() >= 5

# ---------- Симуляция сделки (с фильтрами объёма и VWAP) ----------
def simulate_trades(df, signals_df, asset_name, tf, sig_type):
    trades = []
    for idx, row in signals_df.iterrows():
        if is_weekend(idx):
            continue

        atr_val = row['ATR14']
        min_atr = MIN_ATR.get(asset_name, {}).get(tf)
        if min_atr is not None and (atr_val is None or atr_val < min_atr):
            continue

        # Фильтр объёма
        volume_now = row['Volume']
        avg_volume = row['Avg_Volume']
        if volume_now is not None and avg_volume is not None:
            if volume_now < avg_volume * 0.8:
                continue

        # Фильтр VWAP (только для ТФ ≥ 15m)
        if tf != "5m":
            vwap = row.get('VWAP', None)
            if vwap is not None:
                if row['direction'] == 'BUY' and row['entry_price'] <= vwap:
                    continue
                if row['direction'] == 'SELL' and row['entry_price'] >= vwap:
                    continue

        direction = row['direction']
        price = row['entry_price']
        mult = ATR_MULTIPLIERS.get(tf)
        if direction == 'BUY':
            sl = price - atr_val * mult['SL']
            tp1 = price + atr_val * mult['TP1']
            tp2 = price + atr_val * mult['TP2']
            tp3 = price + atr_val * mult['TP3']
        else:
            sl = price + atr_val * mult['SL']
            tp1 = price - atr_val * mult['TP1']
            tp2 = price - atr_val * mult['TP2']
            tp3 = price - atr_val * mult['TP3']
        be = price

        future = df.loc[idx:].iloc[1:]
        if len(future) == 0:
            continue

        tp1_hit = False
        sl_hit = False
        be_hit_after_tp1 = False
        tp2_hit = False
        tp3_hit = False
        result = None
        partial_profit = 0

        for t, frow in future.iterrows():
            high, low = frow['High'], frow['Low']
            if not tp1_hit:
                if (direction == 'BUY' and low <= sl) or (direction == 'SELL' and high >= sl):
                    sl_hit = True
                    result = 'SL'
                    break
                if (direction == 'BUY' and high >= tp1) or (direction == 'SELL' and low <= tp1):
                    tp1_hit = True
                    partial_profit = 1.0 * abs(tp1 - price)
                    sl = be
            else:
                if (direction == 'BUY' and low <= sl) or (direction == 'SELL' and high >= sl):
                    be_hit_after_tp1 = True
                    result = 'TP1_FULL'
                    break
                if (direction == 'BUY' and high >= tp2) or (direction == 'SELL' and low <= tp2):
                    tp2_hit = True
                if (direction == 'BUY' and high >= tp3) or (direction == 'SELL' and low <= tp3):
                    tp3_hit = True

        if result is None:
            if tp3_hit and sig_type == 'fast_ema':
                result = 'TP3'
            elif tp2_hit:
                result = 'TP2'
            elif tp1_hit:
                result = 'OPEN'
            else:
                result = 'OPEN'

        if result == 'SL':
            pnl_pts = -abs(sl - price)
        elif result == 'TP1_FULL':
            pnl_pts = partial_profit
        elif result == 'TP2':
            pnl_pts = partial_profit
        elif result == 'TP3':
            pnl_pts = partial_profit
        else:
            pnl_pts = 0

        trades.append({
            'asset': asset_name, 'tf': tf, 'type': sig_type, 'direction': direction,
            'entry_time': idx, 'entry_price': price,
            'sl': sl, 'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
            'result': result, 'tp1_hit': tp1_hit, 'tp2_hit': tp2_hit,
            'tp3_hit': tp3_hit, 'sl_hit': sl_hit, 'be_hit': be_hit_after_tp1,
            'pnl_pts': pnl_pts
        })
    return trades

# ---------- Основной цикл ----------
all_trades = []
for asset_name, symbol in ASSETS.items():
    for tf in TIMEFRAMES[asset_name]:
        print(f"Скачиваю данные {asset_name} {tf}...")
        df = get_klines(symbol, tf, limit=1000)
        if df is None or len(df) < 100:
            print(f"  Ошибка загрузки {asset_name} {tf}, пропускаю")
            continue
        print(f"  Рассчитываю индикаторы для {tf}...")
        df['RSI'] = compute_rsi(df['Close'], RSI_PERIOD)
        df['EMA20'] = ema(df['Close'], EMA_FAST)
        df['EMA50'] = ema(df['Close'], EMA_SLOW)
        df['EMA3'] = ema(df['Close'], EMA_FAST_FAST)
        df['EMA10'] = ema(df['Close'], EMA_SLOW_FAST)
        df['ATR14'] = atr(df, 14)

        # Объём и VWAP (как в боте)
        df['Avg_Volume'] = df['Volume'].rolling(50).mean()
        typical_price = (df['High'] + df['Low'] + df['Close']) / 3
        df['VWAP'] = (typical_price * df['Volume']).rolling(50).sum() / df['Volume'].rolling(50).sum()

        df['RSI_prev'] = df['RSI'].shift(1)
        df['EMA20_prev'] = df['EMA20'].shift(1)
        df['EMA50_prev'] = df['EMA50'].shift(1)
        df['EMA3_prev'] = df['EMA3'].shift(1)
        df['EMA10_prev'] = df['EMA10'].shift(1)

        rsi_buy = (df['RSI_prev'] < RSI_OVERSOLD) & (df['RSI'] >= RSI_OVERSOLD)
        rsi_sell = (df['RSI_prev'] > RSI_OVERBOUGHT) & (df['RSI'] <= RSI_OVERBOUGHT)
        ema_buy = (df['EMA20_prev'] <= df['EMA50_prev']) & (df['EMA20'] > df['EMA50'])
        ema_sell = (df['EMA20_prev'] >= df['EMA50_prev']) & (df['EMA20'] < df['EMA50'])
        combined_buy = rsi_buy & ema_buy
        combined_sell = rsi_sell & ema_sell
        fast_buy = (df['EMA3_prev'] <= df['EMA10_prev']) & (df['EMA3'] > df['EMA10'])
        fast_sell = (df['EMA3_prev'] >= df['EMA10_prev']) & (df['EMA3'] < df['EMA10'])

        # Для GOLD оставляем все 4 сигнала, для остальных только Combined и FAST_EMA
        if asset_name == "GOLD":
            signal_list = [
                ('rsi', rsi_buy, rsi_sell),
                ('ema', ema_buy, ema_sell),
                ('combined', combined_buy, combined_sell),
                ('fast_ema', fast_buy, fast_sell),
            ]
        else:
            signal_list = [
                ('combined', combined_buy, combined_sell),
                ('fast_ema', fast_buy, fast_sell),
            ]

        for sig_type, buy_mask, sell_mask in signal_list:
            for idx in df[buy_mask].index:
                row = {
                    'direction': 'BUY',
                    'entry_price': df.loc[idx, 'Close'],
                    'ATR14': df.loc[idx, 'ATR14'],
                    'Volume': df.loc[idx, 'Volume'],
                    'Avg_Volume': df.loc[idx, 'Avg_Volume'],
                    'VWAP': df.loc[idx, 'VWAP']
                }
                signals_df = pd.DataFrame([row], index=[idx])
                trades = simulate_trades(df, signals_df, asset_name, tf, sig_type)
                all_trades.extend(trades)
            for idx in df[sell_mask].index:
                row = {
                    'direction': 'SELL',
                    'entry_price': df.loc[idx, 'Close'],
                    'ATR14': df.loc[idx, 'ATR14'],
                    'Volume': df.loc[idx, 'Volume'],
                    'Avg_Volume': df.loc[idx, 'Avg_Volume'],
                    'VWAP': df.loc[idx, 'VWAP']
                }
                signals_df = pd.DataFrame([row], index=[idx])
                trades = simulate_trades(df, signals_df, asset_name, tf, sig_type)
                all_trades.extend(trades)

print(f"Всего сделок сгенерировано: {len(all_trades)}")

if all_trades:
    tdf = pd.DataFrame(all_trades)
    closed = tdf[tdf['result'] != 'OPEN']
    if len(closed) > 0:
        total = len(closed)
        sl = len(closed[closed['result'] == 'SL'])
        tp1_full = len(closed[closed['result'] == 'TP1_FULL'])
        tp2 = len(closed[closed['result'] == 'TP2'])
        tp3 = len(closed[closed['result'] == 'TP3'])
        win = tp1_full + tp2 + tp3
        win_rate = win / total * 100
        total_pnl = closed['pnl_pts'].sum()

        print(f"\n===== РЕЗУЛЬТАТЫ (с фильтрами объёма и VWAP) =====")
        print(f"Всего закрытых сделок: {total}")
        print(f"Убыток (SL): {sl}")
        print(f"TP1 полная: {tp1_full}")
        print(f"TP2: {tp2}")
        print(f"TP3: {tp3}")
        print(f"Успешных (TP1+TP2+TP3): {win} ({win_rate:.1f}%)")
        print(f"Суммарный PnL в пунктах: {total_pnl:.2f}")

        fig, axes = plt.subplots(2, 2, figsize=(12, 10))
        by_asset = closed.groupby('asset')['result'].value_counts().unstack().fillna(0)
        by_asset.plot(kind='bar', stacked=True, ax=axes[0,0], title='По активам')
        by_type = closed.groupby('type')['result'].value_counts().unstack().fillna(0)
        by_type.plot(kind='bar', stacked=True, ax=axes[0,1], title='По типам сигналов')
        by_tf = closed.groupby('tf')['result'].value_counts().unstack().fillna(0)
        by_tf.plot(kind='bar', stacked=True, ax=axes[1,0], title='По ТФ')
        axes[1,1].pie([sl, tp1_full, tp2, tp3], labels=['SL','TP1','TP2','TP3'], autopct='%1.1f%%')
        axes[1,1].set_title('Итоги')
        plt.tight_layout()
        plt.show()

        csv_name = 'backtest_volume_vwap.csv'
        tdf.to_csv(csv_name, index=False)
        print(f"📁 Файл {csv_name} готов, скачиваю...")
        files.download(csv_name)
    else:
        print("Нет завершённых сделок.")
else:
    print("Нет данных для анализа.")
