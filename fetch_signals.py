"""보유종목 신호(이평선/RSI/이격/거래량)와 매크로를 계산해 signals.json으로 저장.
수량·평단 등 개인 포트폴리오 정보는 포함하지 않음 (공개 저장소용)."""
import json
import sys
import time
from datetime import datetime
from io import StringIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

# ───────── 종목 설정 ─────────
KR = {  # 이름: 코드 (코스피/코스닥 자동 판별). None이면 missing에 기록
    "현대백화점": "069960", "후성": "093370", "한화시스템": "272210",
    "대한광통신": "010170", "한국금융지주": "071050", "현대자동차": "005380",
    "삼성전자": "005930", "SK텔레콤": "017670",
    "KODEX 3대농산물선물(H)": "271060", "PLUS 태양광&ESS": "457990",
    "SOL 조선TOP3플러스": "466920", "KODEX 필수소비재": "266410",
    "HANARO e커머스": "322400", "PLUS 우주항공": "421320",
    "KODEX 미국AI전력핵심인프라": "487230", "TIGER 조선TOP10": "494670",
    "RISE 미국은행TOP10": "0013P0", "SOL 미국양자컴퓨팅TOP10": "0023A0",
    "PLUS 글로벌휴머노이드로봇액티브": "0035T0", "TIGER 차이나휴머노이드로봇": "0053L0",
    "TIGER 코리아원자력": "0091P0", "1Q 미국우주항공테크": "0131V0",
    "ACE 미국SMR원자력TOP10": "0155M0",
    "KODEX 2차전지핵심소재10": None,          # TODO: 코드 확인 후 입력
    "TIGER 차이나반도체FACTSET(관심)": "396520",
}
US = {
    "알파벳": "GOOGL", "IBM": "IBM", "타이슨푸드": "TSN", "테슬라": "TSLA",
    "키사이트": "KEYS", "퀀텀컴퓨팅": "QUBT", "뉴스케일파워": "SMR",
    "로켓랩": "RKLB", "인플렉션": "INFQ", "VanEck Agribusiness": "MOO", "KORU": "KORU",
}
MACRO = {  # 이름: (티커, 시장)  시장 None = 24시간 거래 자산
    "USD/KRW": ("KRW=X", None), "DXY": ("DX-Y.NYB", None), "WTI": ("CL=F", None),
    "VIX": ("^VIX", "US"), "S&P500": ("^GSPC", "US"), "NASDAQ": ("^IXIC", "US"),
    "KOSPI": ("^KS11", "KR"), "KOSDAQ": ("^KQ11", "KR"), "닛케이225": ("^N225", "JP"),
    "금": ("GC=F", None), "구리": ("HG=F", None),
}
CLOSE = {"KR": ("Asia/Seoul", 15, 40), "JP": ("Asia/Tokyo", 15, 40),
         "US": ("America/New_York", 16, 15)}
FIELDS = ["c", "p", "d1", "d5", "d20", "ma20", "ma60", "ma120", "g20",
          "rsi", "vx", "al", "st", "lo20", "hi60", "dt"]
TH = dict(day_up=5, day_down=-5, d5=10, d20=20, gap=10, gap_hot=15,
          streak=3, vol=2.0, bigdrop=-3)   # 경고 기준 (필요시 조정)


def r2(x):
    return None if x is None or pd.isna(x) else round(float(x), 2)


def pct(a, b):
    if a is None or b is None or b == 0 or pd.isna(a) or pd.isna(b):
        return None
    return round((float(a) / float(b) - 1) * 100, 2)


def hist(ticker, retries=2):
    for _ in range(retries + 1):
        try:
            df = yf.Ticker(ticker).history(period="9mo", auto_adjust=False)
            df = df.dropna(subset=["Close"])
            if len(df):
                return df
        except Exception:
            pass
        time.sleep(1)
    return None


def hist_kr(code):
    for suf in (".KS", ".KQ"):
        df = hist(code + suf, retries=1)
        if df is not None:
            return df
    return None


def drop_partial(df, mkt):
    """장 마감 전 미완성 봉 제거"""
    if mkt not in CLOSE or df is None or df.empty:
        return df
    tz, h, m = CLOSE[mkt]
    now = datetime.now(ZoneInfo(tz))
    if df.index[-1].date() == now.date() and (now.hour, now.minute) < (h, m):
        return df.iloc[:-1]
    return df


def rsi14(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    v = 100 - 100 / (1 + up / dn)
    return None if len(c) <= n or pd.isna(v.iloc[-1]) else float(v.iloc[-1])


def analyze(df, mkt):
    df = drop_partial(df, mkt)
    if df is None or len(df) < 2:
        return None
    c, v = df["Close"].astype(float), df["Volume"].astype(float)
    n, last = len(c), float(c.iloc[-1])

    def ret(k):
        return pct(last, c.iloc[-1 - k]) if n > k else None

    def ma(w):
        return float(c.tail(w).mean()) if n >= w else None

    m20, m60, m120 = ma(20), ma(60), ma(120)
    st = 0
    for i in range(n - 1, 0, -1):
        if c.iloc[i] > c.iloc[i - 1]:
            st += 1
        else:
            break
    base = v.iloc[-21:-1].mean() if n >= 21 else None
    vx = float(v.iloc[-1] / base) if base and base > 0 else None
    al = None
    if None not in (m20, m60, m120):
        al = "정" if last > m20 > m60 > m120 else "역" if last < m20 < m60 < m120 else "혼"
    return {
        "c": r2(last), "p": r2(c.iloc[-2]), "d1": ret(1), "d5": ret(5), "d20": ret(20),
        "ma20": r2(m20), "ma60": r2(m60), "ma120": r2(m120), "g20": pct(last, m20),
        "rsi": r2(rsi14(c)), "vx": r2(vx), "al": al, "st": st,
        "lo20": r2(c.tail(20).min()), "hi60": r2(c.tail(60).max()),
        "dt": str(df.index[-1].date()),
    }


def flags(m):
    f = []
    d1, d5, d20, g, vx = m["d1"], m["d5"], m["d20"], m["g20"], m["vx"]
    if d1 is not None and d1 >= TH["day_up"]:
        f.append("급등일")
    if d1 is not None and d1 <= TH["day_down"]:
        f.append("급락일")
    if m["st"] >= TH["streak"]:
        f.append(f"연속상승{m['st']}일")
    if d5 is not None and d5 >= TH["d5"]:
        f.append("5일급등(#2)")
    if d20 is not None and d20 >= TH["d20"]:
        f.append("20일급등")
    if g is not None and g >= TH["gap_hot"]:
        f.append("이격+15%↑(#17)")
    elif g is not None and g >= TH["gap"]:
        f.append("이격+10%↑(#17)")
    if vx and vx >= TH["vol"] and d1 is not None and d1 <= TH["bigdrop"]:
        f.append("거래량급증+하락")
    if m["rsi"] is not None and m["rsi"] >= 70:
        f.append("RSI≥70")
    if m["rsi"] is not None and m["rsi"] <= 30:
        f.append("RSI≤30")
    if m["al"] == "역":
        f.append("역배열")
    return f


def chg(c):
    c = c.dropna()
    if len(c) < 2:
        return None
    last = float(c.iloc[-1])
    return [r2(last)] + [pct(last, c.iloc[-1 - k]) if len(c) > k else None for k in (1, 5, 21)]


def chg_abs(c):
    c = c.dropna()
    if len(c) < 2:
        return None
    last = float(c.iloc[-1])
    return [round(last, 3)] + [round(last - float(c.iloc[-1 - k]), 3) if len(c) > k else None
                               for k in (1, 5, 21)]


def fred(series):
    try:
        r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}", timeout=20)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        s = pd.to_numeric(df.iloc[:, 1], errors="coerce")
        s.index = pd.to_datetime(df.iloc[:, 0])
        return s.dropna().tail(60)
    except Exception as e:
        print("FRED 실패", series, e)
        return None


def by_day(s):
    s = s.copy()
    s.index = pd.to_datetime(s.index.date)
    return s[~s.index.duplicated(keep="last")]


def main():
    stocks, alerts, missing = {}, [], []

    def add(name, df, mkt):
        m = analyze(df, mkt)
        if m is None:
            missing.append(name)
            return
        stocks[name] = [m[k] for k in FIELDS]
        f = flags(m)
        if f:
            alerts.append([name, f])

    for name, code in KR.items():
        if not code:
            missing.append(f"{name}(코드 미입력)")
            continue
        add(name, hist_kr(code), "KR")
    for name, t in US.items():
        add(name, hist(t), "US")

    if len(missing) > 6:
        print("누락 과다:", missing)
        sys.exit(1)          # 이전 정상 파일을 덮어쓰지 않도록 실패 처리

    macro = {}
    for name, (t, mkt) in MACRO.items():
        df = drop_partial(hist(t), mkt)
        macro[name] = chg(df["Close"]) if df is not None else None

    krw, jpy = hist("KRW=X"), hist("JPY=X")
    if krw is not None and jpy is not None:
        macro["JPY/KRW(100엔)"] = chg((by_day(krw["Close"]) / by_day(jpy["Close"]) * 100).dropna())

    y10, y2 = fred("DGS10"), fred("DGS2")
    if y10 is not None:
        macro["미국10Y(%,Δ%p)"] = chg_abs(y10)
    if y2 is not None:
        macro["미국2Y(%,Δ%p)"] = chg_abs(y2)
    if y10 is not None and y2 is not None:
        macro["10Y-2Y(%p)"] = chg_abs((y10 - y2).dropna())

    result = {
        "generated_kst": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M"),
        "fields": FIELDS,
        "note": "stocks=[c종가,p전일,d1/d5/d20 등락%,ma20/60/120,g20 20일선이격%,rsi,vx 거래량/20일평균,"
                "al 정/역/혼,st 연속상승일,lo20 20일저점,hi60 60일고점,dt 기준일]. "
                "macro=[현재,1일,5일,21일] (금리는 Δ%p). null=산출불가",
        "stocks": stocks, "alerts": alerts, "macro": macro, "missing": missing,
    }
    with open("signals.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    print("완료", len(stocks), "종목, 누락:", missing)


if __name__ == "__main__":
    main()
