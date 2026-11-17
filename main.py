from fastapi import FastAPI, HTTPException, Request
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi.middleware.cors import CORSMiddleware
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
import os
import time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastapi.responses import JSONResponse
from typing import Optional

load_dotenv()

DATABASE_URL = os.environ.get('DATABASE_URL')
TINKOFF_TOKEN = os.environ.get('TINKOFF_TOKEN')

app = FastAPI()

# ============================================================
# БАЗА ДАННЫХ (PostgreSQL на Neon)
# ============================================================
def get_db_connection():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL не настроен")
    for attempt in range(3):
        try:
            return psycopg2.connect(
                DATABASE_URL,
                connect_timeout=10,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=5
            )
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
# КЭШ FIGI
# ============================================================
_FIGI_CACHE: dict = {}

def resolve_figi(ticker: str, headers: dict) -> Optional[str]:
    """Резолвит FIGI для тикера с кэшем в памяти (classCode == TQBR)."""
    if ticker in _FIGI_CACHE:
        return _FIGI_CACHE[ticker]
    url = ("https://invest-public-api.tbank.ru/rest/"
           "tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument")
    resp = requests.post(
        url, headers=headers,
        json={"query": ticker, "instrumentKind": "INSTRUMENT_TYPE_SHARE"},
        timeout=30, verify=False
    )
    for inst in resp.json().get("instruments", []):
        if inst.get("ticker") == ticker and inst.get("classCode") == "TQBR":
            _FIGI_CACHE[ticker] = inst["figi"]
            return inst["figi"]
    return None

CANDLE_URL = ("https://invest-public-api.tbank.ru/rest/"
              "tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles")

# ============================================================
# БЕЗОПАСНАЯ ЗАПИСЬ (переподключение при InterfaceError)
# ============================================================
def safe_execute(cursor, conn, values):
    """Пишет values в candles. При обрыве соединения — переподключается и повторяет."""
    query = ("INSERT INTO candles (ticker, time, close) VALUES %s "
             "ON CONFLICT (ticker, time) DO NOTHING")
    try:
        execute_values(cursor, query, values)
        conn.commit()
        return conn, cursor
    except psycopg2.InterfaceError:
        print("!!! Соединение с базой умерло, переподключаемся...")
        try:
            conn.close()
        except Exception:
            pass
        conn = get_db_connection()
        cursor = conn.cursor()
        execute_values(cursor, query, values)
        conn.commit()
        return conn, cursor

# ============================================================
# ОБНОВЛЕНИЕ БАЗЫ СВЕЧЕЙ (PostgreSQL)
# ============================================================
@app.post("/candles/update")
async def update_candles(request: Request):
    body = await request.json()
    tickers = body.get("tickers", [])
    days_back = body.get("days_back")

    if not tickers:
        raise HTTPException(status_code=400, detail="Список тикеров пуст")
    if not TINKOFF_TOKEN:
        raise HTTPException(status_code=500, detail="TINKOFF_TOKEN не настроен")

    headers = {"Authorization": f"Bearer {TINKOFF_TOKEN}", "Content-Type": "application/json"}
    results = {}

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка подключения к базе: {e}")

    for ticker in tickers:
        try:
            figi = resolve_figi(ticker, headers)
            if not figi:
                results[ticker] = {"status": "error", "message": "FIGI не найден"}
                continue

            if days_back:
                to_date = datetime.now(timezone.utc)
                from_date = to_date - timedelta(days=days_back)
            else:
                cursor.execute("SELECT MAX(time) FROM candles WHERE ticker = %s", (ticker,))
                last_time = cursor.fetchone()[0]
                if last_time:
                    from_date = (datetime.fromisoformat(last_time.replace('Z', '+00:00'))
                                 + timedelta(minutes=5))
                else:
                    from_date = datetime.now(timezone.utc) - timedelta(days=365)
                to_date = datetime.now(timezone.utc)

            new_candles = 0
            cur = from_date

            while cur < to_date:
                nxt = min(cur + timedelta(days=7), to_date)
                payload = {
                    "figi": figi,
                    "from": cur.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to": nxt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "interval": "CANDLE_INTERVAL_5_MIN",
                    "limit": 1000,
                }
                try:
                    candle_resp = requests.post(
                        CANDLE_URL, headers=headers, json=payload,
                        timeout=60, verify=False
                    )
                    candles = candle_resp.json().get("candles", [])
                    print(f"[{ticker}] {cur.date()} — {nxt.date()}: получено {len(candles)} свечей")

                    if candles:
                        values = [(ticker, c["time"], parse_price(c.get("close"))) for c in candles]
                        conn, cursor = safe_execute(cursor, conn, values)
                        new_candles += len(values)
                except Exception as e:
                    print(f"[{ticker}] ОШИБКА на {cur.date()}: {e}")
                    results[ticker] = {"status": "error", "message": f"Ошибка записи: {str(e)}"}
                    break
                cur = nxt

            if ticker not in results:
                results[ticker] = {"status": "ok", "new_candles": new_candles}

        except Exception as e:
            results[ticker] = {"status": "error", "message": str(e)}

    try:
        cursor.close()
        conn.close()
    except Exception:
        pass
    return {"updated": results}

# ============================================================
# ЧТЕНИЕ СВЕЧЕЙ ИЗ БАЗЫ (PostgreSQL)
# ============================================================
@app.get("/candles/{ticker}")
def get_candles(ticker: str, days: int = 120, interval: str = "5m"):
    ticker = ticker.upper().strip()
    from_date = datetime.now(timezone.utc) - timedelta(days=days)

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute(
        "SELECT time, close FROM candles WHERE ticker = %s AND time >= %s ORDER BY time",
        (ticker, from_date.isoformat().replace('+00:00', 'Z'))
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail=f"Нет данных для {ticker}")

    if interval == "1h":
        # Ресемплинг 5-минуток в часы: берём последнюю close в каждом часе
        hourly = {}
        for r in rows:
            # time в формате "2026-11-13T10:05:00Z" — берём часть до минут
            hour_key = r["time"][:13] + ":00:00Z"   # "2026-11-13T10:00:00Z"
            hourly[hour_key] = r["close"]           # последняя в часе перезапишет
        candles = [{"time": k, "close": v} for k, v in sorted(hourly.items())]
    else:
        candles = [{"time": r["time"], "close": r["close"]} for r in rows]

    return {
        "ticker": ticker,
        "interval": interval,
        "count": len(candles),
        "candles": candles
    }
