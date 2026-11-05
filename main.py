from fastapi import FastAPI, HTTPException, Request
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi.middleware.cors import CORSMiddleware
import sqlite3
from datetime import datetime, timedelta

app = FastAPI()
# ============================================================
# БАЗА ДАННЫХ ДЛЯ СВЕЧЕЙ
# ============================================================
DB_FILE = "candles.db"

def init_db():
    """Создаёт таблицу, если её нет."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            ticker TEXT,
            time TEXT,
            close REAL,
            PRIMARY KEY (ticker, time)
        )
    """)
    conn.commit()
    conn.close()


# Разрешаем запросы откуда угодно (чтобы Google Таблицы могли к нам обращаться)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"message": "MOEX Proxy is running"}

@app.get("/stock/{ticker}")
def get_stock_price(ticker: str):
    """
    Принимает тикер, идёт в API Мосбиржи и возвращает цену.
    """
    ticker = ticker.upper().strip()
    
    # URL для получения последней цены с основной торговой площадки (TQBR)
    # Мы используем параметр iss.only, чтобы получить только нужный блок marketdata
    url = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/{ticker}.json?iss.meta=off&iss.only=marketdata"
    
    try:
        # Делаем запрос от имени нашего сервера (с российским IP, если хостинг в РФ, или просто не Google)
        # Добавляем User-Agent, чтобы Мосбиржа не думала, что мы бот
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        
        # Парсим сложную структуру JSON от Мосбиржи
        columns = data['marketdata']['columns']
        rows = data['marketdata']['data']
        
        if not rows:
            raise HTTPException(status_code=404, detail=f"Нет данных для тикера {ticker}")
        
        # Находим индекс колонки с ценой (LAST) и другими полями
        try:
            price_index = columns.index('LAST')
            # Цена может быть None, если торги не идут
            price = rows[0][price_index]
        except (ValueError, IndexError):
            price = None
            
        if price is None:
            # Если LAST пустой, пробуем взять цену закрытия или другую
            raise HTTPException(status_code=404, detail=f"Цена для {ticker} недоступна (торги не идут или неверный тикер)")
            
        return {"ticker": ticker, "price": price}
        
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Ошибка при запросе к Мосбирже: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка: {str(e)}")
        
@app.get("/name/{ticker}")
def get_stock_name(ticker: str):
    ticker = ticker.upper().strip()
    url = f"https://iss.moex.com/iss/securities/{ticker}.json?iss.meta=off"
    
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    
    data = response.json()
    
    if 'description' not in data:
        raise HTTPException(status_code=404, detail="Нет блока description")
    
    columns = data['description']['columns']
    rows = data['description']['data']
    
    name_idx = columns.index('name')
    value_idx = columns.index('value')
    
    for row in rows:
        if row[name_idx] == 'SHORTNAME':
            return {"ticker": ticker, "name": row[value_idx]}
    
    # Если shortname не найден — вернём все поля для отладки
    debug_fields = [row[name_idx] for row in rows]
    raise HTTPException(status_code=404, detail=f"shortname не найден. Доступные поля: {debug_fields}")

init_db()

@app.get("/stocks")
def get_stocks_prices(tickers: str):
    if not tickers:
        raise HTTPException(status_code=400, detail="Параметр tickers обязателен")
    
    ticker_list = [t.strip().upper() for t in tickers.split(',') if t.strip()]
    
    if not ticker_list:
        raise HTTPException(status_code=400, detail="Пустой список тикеров")
    
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    def fetch_price(ticker):
        try:
            # Используем Session для переиспользования соединения
            with requests.Session() as session:
                session.headers.update(headers)
                url = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/{ticker}.json?iss.meta=off&iss.only=marketdata"
                response = session.get(url, timeout=5)
                
                if response.status_code != 200:
                    return ticker, None
                
                data = response.json()
                columns = data['marketdata']['columns']
                rows = data['marketdata']['data']
                
                if not rows:
                    return ticker, None
                
                try:
                    price_index = columns.index('LAST')
                    return ticker, rows[0][price_index]
                except (ValueError, IndexError):
                    return ticker, None
                    
        except Exception:
            return ticker, None
    
    result = {}
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(fetch_price, t): t for t in ticker_list}
        
        for future in as_completed(futures):
            ticker, price = future.result()
            result[ticker] = price
    
    return result
    
    return result
    
import os

# Токен Т-Банка берём из переменной окружения Render
TINKOFF_TOKEN = os.environ.get('TINKOFF_TOKEN')

@app.post("/tinkoff/{endpoint:path}")
async def tinkoff_proxy(endpoint: str, request: Request):
    """
    Прокси для API Т-Банка.
    Принимает путь и JSON, добавляет токен и отправляет в Т-Банк.
    """
    if not TINKOFF_TOKEN:
        raise HTTPException(status_code=500, detail="TINKOFF_TOKEN не настроен на сервере")

    # Формируем полный URL к API Т-Банка
    url = f"https://invest-public-api.tbank.ru/rest/{endpoint}"

    headers = {
        "Authorization": f"Bearer {TINKOFF_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        # Получаем тело запроса от Google Apps Script
        body = await request.json()
    except Exception:
        body = {}

    try:
        # Отключаем проверку SSL (verify=False) — прокси работает в доверенном окружении
        resp = requests.post(url, headers=headers, json=body, timeout=30, verify=False)
        return resp.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Ошибка запроса к Т-Банку: {str(e)}")

# ============================================================
# УНИВЕРСАЛЬНЫЙ ПАРСЕР ЦЕНЫ
# ============================================================
def parse_price(val):
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except:
            return 0.0
    if isinstance(val, dict):
        units = float(val.get('units', 0))
        nano = float(val.get('nano', 0))
        return units + nano / 1e9
    return 0.0

# ============================================================
# ОБНОВЛЕНИЕ БАЗЫ СВЕЧЕЙ
# ============================================================
@app.post("/candles/update")
async def update_candles(request: Request):
    body = await request.json()
    tickers = body.get("tickers", [])
    
    if not tickers:
        raise HTTPException(status_code=400, detail="Список тикеров пуст")
    
    if not TINKOFF_TOKEN:
        raise HTTPException(status_code=500, detail="TINKOFF_TOKEN не настроен")
    
    headers = {"Authorization": f"Bearer {TINKOFF_TOKEN}", "Content-Type": "application/json"}
    results = {}
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    for ticker in tickers:
        try:
            # FIGI
            url = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument"
            resp = requests.post(url, headers=headers, json={"query": ticker, "instrumentKind": "INSTRUMENT_TYPE_SHARE"}, timeout=30, verify=False)
            data = resp.json()
            
            figi = None
            for inst in data.get("instruments", []):
                if inst.get("ticker") == ticker and inst.get("classCode") == "TQBR":
                    figi = inst.get("figi")
                    break
            
            if not figi:
                results[ticker] = {"status": "error", "message": "FIGI не найден"}
                continue
            
            # Последняя дата в базе
            cursor.execute("SELECT MAX(time) FROM candles WHERE ticker = ?", (ticker,))
            last_time = cursor.fetchone()[0]
            
            if last_time:
                from_date = datetime.fromisoformat(last_time.replace('Z', '+00:00')) + timedelta(minutes=1)
            else:
                from_date = datetime(2026, 1, 1)
            
            to_date = datetime.now()
            new_candles = 0
            current_start = from_date
            
            while current_start <= to_date:
                current_end = min(current_start + timedelta(days=7), to_date)
                
                candle_url = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles"
                payload = {
                    "figi": figi,
                    "from": current_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to": current_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "interval": "CANDLE_INTERVAL_5_MIN"
                }
                
                try:
                    candle_resp = requests.post(candle_url, headers=headers, json=payload, timeout=30, verify=False)
                    candle_data = candle_resp.json()
                    candles = candle_data.get("candles", [])
                    
                    for c in candles:
                        close_price = parse_price(c.get("close"))
                        cursor.execute(
                            "INSERT OR IGNORE INTO candles (ticker, time, close) VALUES (?, ?, ?)",
                            (ticker, c["time"], close_price)
                        )
                        new_candles += 1
                except Exception as e:
                    pass
                
                current_start = current_end + timedelta(seconds=1)
            
            conn.commit()
            results[ticker] = {"status": "ok", "new_candles": new_candles}
            
        except Exception as e:
            results[ticker] = {"status": "error", "message": str(e)}
    
    conn.close()
    return {"updated": results}


@app.get("/candles/{ticker}")
def get_candles(ticker: str, days: int = 120):
    ticker = ticker.upper().strip()
    from_date = datetime.now() - timedelta(days=days)
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT time, close FROM candles WHERE ticker = ? AND time >= ? ORDER BY time",
        (ticker, from_date.isoformat())
    )
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        raise HTTPException(status_code=404, detail=f"Нет данных для {ticker}")
    
    return {
        "ticker": ticker,
        "count": len(rows),
        "candles": [{"time": r[0], "close": r[1]} for r in rows]
    }
