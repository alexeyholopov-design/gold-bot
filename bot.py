def check_bybit_tradfi():
    global GOLD_SYMBOL
    print("🔑 [DEBUG] check_bybit_tradfi вызвана")
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        print("⚠️ Ключи Bybit не заданы, GOLD будет работать через BingX")
        return False
    try:
        params = {"category": "tradfi", "symbol": "XAUUSDT+"}
        headers = bybit_sign_request(params)
        url = "https://api.bybit.com/v5/market/tickers"
        print(f"🔑 [DEBUG] Запрос к {url} с params={params}")
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        print(f"🔑 [DEBUG] HTTP статус: {resp.status_code}")
        print(f"🔑 [DEBUG] Сырой ответ: {resp.text[:500]}")
        if resp.status_code != 200:
            print(f"❌ HTTP ошибка {resp.status_code}: {resp.text[:200]}")
            return False
        data = resp.json()
        print(f"🔑 [DEBUG] JSON ответ: {data}")
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
