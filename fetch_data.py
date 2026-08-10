import yfinance as yf
import json
from datetime import datetime

result = {"updated_at": datetime.now().isoformat(), "holdings": {}, "indices": {}, "fx": {}}

def fetch(ticker, period="5d"):
    data = yf.Ticker(ticker).history(period=period)
    data = data.reset_index()
    data["Date"] = data["Date"].astype(str)
    rows = data[["Date", "Close", "Volume"]].tail(3).to_dict(orient="records")
    labeled = {}
    labels = ["latest", "prev", "prev2"]
    for i, row in enumerate(reversed(rows)):
        if i < len(labels):
            labeled[labels[i]] = {
                "date": row["Date"],
                "close": row["Close"],
                "volume": row["Volume"]
            }
    return labeled

us_tickers = ["GOOGL", "IBM", "TSLA", "KEYS", "RKLB"]
jp_tickers = ["1306.T", "1631.T"]
kr_tickers = {
    "SK텔레콤": "017670.KS",
    "현대차": "005380.KS",
    "ACE미국SMR원자력TOP10": "0155M0.KS",
    "RISE미국은행TOP10": "0013P0.KS",
    "PLUS글로벌휴머노이드로봇액티브": "0035T0.KS",
    "SOL미국양자컴퓨팅TOP10": "0023A0.KS",
    "SOL조선TOP3플러스": "466920.KS",
    "PLUS태양광ESS": "457990.KS",
    "TIGER차이나반도체FACTSET": "396520.KS",
    "TIGER차이나휴머노이드로봇": "0053L0.KS",
}

for ticker in us_tickers + jp_tickers:
    try:
        result["holdings"][ticker] = fetch(ticker)
    except Exception as e:
        result["holdings"][ticker] = {"error": str(e)}

for name, ticker in kr_tickers.items():
    try:
        result["holdings"][name] = fetch(ticker)
    except Exception as e:
        result["holdings"][name] = {"error": str(e), "ticker": ticker}

indices = {"코스피": "^KS11", "코스닥": "^KQ11", "나스닥": "^IXIC", "S&P500": "^GSPC", "닛케이225": "^N225"}
for name, ticker in indices.items():
    try:
        result["indices"][name] = fetch(ticker)
    except Exception as e:
        result["indices"][name] = {"error": str(e)}

fx = {"원달러": "KRW=X", "엔달러": "JPY=X"}
for name, ticker in fx.items():
    try:
        result["fx"][name] = fetch(ticker)
    except Exception as e:
        result["fx"][name] = {"error": str(e)}

with open("data.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, default=str, indent=2)

print("완료")
