import yfinance as yf
from pykrx import stock
import json
from datetime import datetime, timedelta

result = {"updated_at": datetime.now().isoformat(), "holdings": {}, "indices": {}, "fx": {}}

# 미국 종목
us_tickers = ["GOOGL", "IBM", "TSLA", "KEYS", "RKLB"]
for ticker in us_tickers:
    try:
        data = yf.Ticker(ticker).history(period="60d")
        result["holdings"][ticker] = data.tail(3).reset_index().to_dict(orient="records")
    except Exception as e:
        result["holdings"][ticker] = {"error": str(e)}

# 일본 ETF
jp_tickers = ["1306.T", "1631.T"]
for ticker in jp_tickers:
    try:
        data = yf.Ticker(ticker).history(period="60d")
        result["holdings"][ticker] = data.tail(3).reset_index().to_dict(orient="records")
    except Exception as e:
        result["holdings"][ticker] = {"error": str(e)}

# 한국 종목/ETF (종목코드)
kr_tickers = {
    "SK텔레콤": "017670",
    "현대차": "005380",
    "ACE미국SMR원자력TOP10": "0900XX",   # 실제 코드 확인 필요
    "RISE미국은행TOP10": "0900XX",       # 실제 코드 확인 필요
    "PLUS글로벌휴머노이드로봇액티브": "0900XX",  # 실제 코드 확인 필요
    "SOL미국양자컴퓨팅TOP10": "0900XX",   # 실제 코드 확인 필요
    "SOL조선TOP3플러스": "0900XX",        # 실제 코드 확인 필요
    "PLUS태양광ESS": "0900XX",           # 실제 코드 확인 필요
    "TIGER차이나반도체FACTSET": "0900XX", # 실제 코드 확인 필요
    "TIGER차이나휴머노이드로봇": "0900XX", # 실제 코드 확인 필요
}
for name, code in kr_tickers.items():
    try:
        df = stock.get_market_ohlcv_by_date(
            (datetime.now()-timedelta(days=15)).strftime("%Y%m%d"),
            datetime.now().strftime("%Y%m%d"), code)
        result["holdings"][name] = df.tail(3).reset_index().to_dict(orient="records")
    except Exception as e:
        result["holdings"][name] = {"error": str(e), "code": code}

# 지수
indices = {"코스피": "KS11", "코스닥": "KQ11"}
for name, code in indices.items():
    try:
        df = stock.get_index_ohlcv_by_date(
            (datetime.now()-timedelta(days=10)).strftime("%Y%m%d"),
            datetime.now().strftime("%Y%m%d"), code)
        result["indices"][name] = df.tail(3).reset_index().to_dict(orient="records")
    except Exception as e:
        result["indices"][name] = {"error": str(e)}

for idx_name, idx_ticker in [("나스닥","^IXIC"),("S&P500","^GSPC"),("닛케이225","^N225")]:
    try:
        data = yf.Ticker(idx_ticker).history(period="5d")
        result["indices"][idx_name] = data.tail(3).reset_index().to_dict(orient="records")
    except Exception as e:
        result["indices"][idx_name] = {"error": str(e)}

# 환율
for fx_name, fx_ticker in [("원달러","KRW=X"),("엔달러","JPY=X")]:
    try:
        data = yf.Ticker(fx_ticker).history(period="5d")
        result["fx"][fx_name] = data.tail(3).reset_index().to_dict(orient="records")
    except Exception as e:
        result["fx"][fx_name] = {"error": str(e)}

with open("data.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, default=str, indent=2)

print("완료:", json.dumps(result, ensure_ascii=False)[:200])
