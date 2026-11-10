from fastapi import FastAPI, HTTPException, Request
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi.middleware.cors import CORSMiddleware
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
import os
import time

app = FastAPI()

# ============================================================
# БАЗА ДАННЫХ (PostgreSQL на Neon)
# ============================================================
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL не настроен")
    # Повторные попытки подключения (если Neon «спит»)
    for attempt in range(3):
        try:
            return psycopg2.connect(DATABASE_URL, connect_timeout=10)
        except Exception as e:
            if attempt == 2:
                raise e
            time.sleep(2)

def init_db():
    conn = get_db_connection()
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
    cursor.close()
    conn.close()

# Инициализация при старте
try:
    init_db()
    print("База данных инициализирована")
except Exception as e:
    print(f"Ошибка инициализации базы: {e}")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# СТАРЫЕ ЭНДПОИНТЫ (Мосбиржа)
# ============================================================
@app.get("/")
def read_root():
    return {"message": "MOEX Proxy is running"}

@app.get("/stock/{ticker}")
def get_stock_price(ticker: str):
    ticker = ticker.upper().strip()
    url = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/{ticker}.json?iss.meta=off&iss.only=marketdata"
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        columns = data['marketdata']['columns']
        rows = data['marketdata']['data']
        
        if not rows:
            raise HTTPException(status_code=404, detail=f"Нет данных для тикера {ticker}")
        
        try:
            price_index = columns.index('LAST')
            price = rows[0][price_index]
        except (ValueError, IndexError):
            price = None
            
        if price is None:
            raise HTTPException(status_code=404, detail=f"Цена для {ticker} недоступна")
            
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
    
    raise HTTPException(status_code=404, detail="shortname не найден")

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

# ============================================================
# ПРОКСИ Т-БАНКА
# ============================================================
TINKOFF_TOKEN = os.environ.get('TINKOFF_TOKEN')

@app.post("/tinkoff/{endpoint:path}")
async def tinkoff_proxy(endpoint: str, request: Request):
    if not TINKOFF_TOKEN:
        raise HTTPException(status_code=500, detail="TINKOFF_TOKEN не настроен")
    url = f"https://invest-public-api.tbank.ru/rest/{endpoint}"
    headers = {"Authorization": f"Bearer {TINKOFF_TOKEN}", "Content-Type": "application/json"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
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
# ОБНОВЛЕНИЕ БАЗЫ СВЕЧЕЙ (PostgreSQL, по месяцам)
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
    
    # Подключение к базе (один раз на все тикеры)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка подключения к базе: {e}")
    
    for ticker in tickers:
        try:
            # 1. FIGI
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
            
            # 2. Последняя дата в базе
            cursor.execute("SELECT MAX(time) FROM candles WHERE ticker = %s", (ticker,))
            last_time = cursor.fetchone()[0]
            
            if last_time:
                from_date = datetime.fromisoformat(last_time.replace('Z', '+00:00')) + timedelta(minutes=1)
            else:
                from_date = datetime(2026, 1, 1)
            
            to_date = datetime.now()
            
            # 3. Загружаем по МЕСЯЦАМ (чтобы не перегружать память)
            new_candles = 0
            current_start = from_date
            
            while current_start <= to_date:
                current_end = min(current_start + timedelta(days=30), to_date)
                
                candle_url = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles"
                payload = {
                    "figi": figi,
                    "from": current_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to": current_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "interval": "CANDLE_INTERVAL_5_MIN"
                }
                
                try:
                    candle_resp = requests.post(candle_url, headers=headers, json=payload, timeout=60, verify=False)
                    candle_data = candle_resp.json()
                    candles = candle_data.get("candles", [])
                    
                    print(f"[{ticker}] {current_start.date()} — {current_end.date()}: получено {len(candles)} свечей")
                    
                    values = [(ticker, c["time"], parse_price(c.get("close"))) for c in candles]
                    if values:
                        cursor.executemany(
                            "INSERT INTO candles (ticker, time, close) VALUES (%s, %s, %s) ON CONFLICT (ticker, time) DO NOTHING",
                            values
                        )
                        new_candles += len(values)
                    
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    print(f"[{ticker}] ОШИБКА на {current_start.date()}: {e}")  # <--- ВОТ ЭТО
                
                current_start = current_end + timedelta(seconds=1)
            
            results[ticker] = {"status": "ok", "new_candles": new_candles}
            
        except Exception as e:
            conn.rollback()
            results[ticker] = {"status": "error", "message": str(e)}
    
    cursor.close()
    conn.close()
    return {"updated": results}

# ============================================================
# ЧТЕНИЕ СВЕЧЕЙ ИЗ БАЗЫ (PostgreSQL)
# ============================================================
@app.get("/candles/{ticker}")
def get_candles(ticker: str, days: int = 120):
    ticker = ticker.upper().strip()
    from_date = datetime.now() - timedelta(days=days)
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute(
        "SELECT time, close FROM candles WHERE ticker = %s AND time >= %s ORDER BY time",
        (ticker, from_date.isoformat())
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    
    if not rows:
        raise HTTPException(status_code=404, detail=f"Нет данных для {ticker}")
    
    return {
        "ticker": ticker,
        "count": len(rows),
        "candles": [{"time": r["time"], "close": r["close"]} for r in rows]
    }
