from fastapi import FastAPI, HTTPException
import requests
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
    """
    Принимает тикер, идёт в API Мосбиржи и возвращает краткое название компании.
    """
    ticker = ticker.upper().strip()
    
    # URL для получения справочной информации по бумаге
    url = f"https://iss.moex.com/iss/securities/{ticker}.json?iss.meta=off"
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        
        # Парсим блок 'description', где лежит shortname
        columns = data['description']['columns']
        rows = data['description']['data']
        
        if not rows:
            raise HTTPException(status_code=404, detail=f"Нет данных для тикера {ticker}")
        
        # Ищем строку, где в поле 'name' записано 'shortname'
        try:
            name_idx = columns.index('name')
            value_idx = columns.index('value')
            for row in rows:
                if row[name_idx] == 'shortname':
                    return {"ticker": ticker, "name": row[value_idx]}
        except (ValueError, IndexError):
            pass
            
        raise HTTPException(status_code=404, detail=f"Название для {ticker} не найдено")
        
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Ошибка при запросе к Мосбирже: {str(e)}")
