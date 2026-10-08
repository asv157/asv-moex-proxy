from fastapi import FastAPI, HTTPException
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

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
    
