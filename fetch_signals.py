"""fetch_signals.py (v5) - 장 마감 확정 종가 기준 신호 계산
daily.json : 매일 읽는 경량 파일(+역배열·RSI≤30 종목 목록, 지수 지연 시 ETF 대용 추정) / weekly.json : 토요일 심층 점검용(+벤치마크)
수량·평단 등 개인 포트폴리오 정보는 포함하지 않음 (공개 저장소용)"""
import json
import sys
import time
from datetime import datetime
from io import StringIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

KR = {  # 이름: 코드 ('.KS/.KQ' 포함 시 그대로, 없으면 자동 판별)
    "현대백화점": "069960", "후성": "093370", "한화시스템": "272210",
    "대한광통신": "010170.KQ", "한국금융지주": "071050", "현대차": "005380",
    "삼성전자": "005930", "SKT": "017670", "농산물H": "271060", "태양광ESS": "457990",
    "조선TOP3+": "466920", "필수소비재": "266410", "e커머스": "322400",
    "우주항공": "421320", "미국AI전력": "487230", "조선TOP10": "494670",
    "미국은행": "0013P0", "양자컴퓨팅": "0023A0", "휴머노이드": "0035T0",
    "차이나로봇": "0053L0", "코리아원자력": "0091P0", "미국우주항공": "0131V0",
    "SMR원자력": "0155M0", "2차전지소재": "461950", "차이나반도체(관심)": "396520",
}
US = {
    "알파벳": "GOOGL", "IBM": "IBM", "타이슨": "TSN", "테슬라": "TSLA",
    "키사이트": "KEYS", "퀀텀컴": "QUBT", "뉴스케일": "SMR", "로켓랩": "RKLB",
    "인플렉션": "INFQ", "MOO": "MOO", "KORU": "KORU",
}
MACRO = {  # 이름: (티커, 시장)   G = 24시간 자산(뉴욕 17시 기준)
    "USD/KRW": ("KRW=X", "G"), "DXY": ("DX-Y.NYB", "G"), "WTI": ("CL=F", "G"),
    "VIX": ("^VIX", "US"), "S&P500": ("^GSPC", "US"), "NASDAQ": ("^IXIC", "US"),
    "KOSPI": ("^KS11", "KR"), "KOSDAQ": ("^KQ11", "KR"), "닛케이225": ("^N225", "JP"),
    "금": ("GC=F", "G"), "구리": ("HG=F", "G"),
}
PROXY = {"KOSPI": ("069500.KS", "KR"), "KOSDAQ": ("229200.KS", "KR"),
         "닛케이225": ("1321.T", "JP")}   # 야후 지수 일봉이 하루 늦을 때 대용 ETF
CLOSE = {"KR": ("Asia/Seoul", 15, 40), "JP": ("Asia/Tokyo", 15, 40),
         "US": ("America/New_York", 16, 15), "G": ("America/New_York", 17, 0)}
TH = dict(day_up=5, day_down=-5, d5=10, d20=20, gap=10, gap_hot=15,
          streak=3, vol=2.0, bigdrop=-3)          # 경고 기준 (필요시 조정)
FIELDS_W = ["c", "d5", "d20", "ma20", "ma60", "ma120", "g20", "rsi", "vx", "al",
            "st", "sup", "res", "lo60", "hi60", "lo120", "hi120", "n"]


# ───────── 유틸 ─────────
def num(x):
    return None if x is None or pd.isna(x) else float(x)


def rnd(x, nd=2):
    x = num(x)
    if x is None:
        return None
    v = round(x, nd)
    return int(v) if v == int(v) else v


def pct(a, b):
    a, b = num(a), num(b)
    if a is None or b in (None, 0):
        return None
    return rnd((a / b - 1) * 100, 1)


def hist(ticker, retries=2, period="9mo"):
    for _ in range(retries + 1):
        try:
            df = yf.Ticker(ticker).history(period=period, auto_adjust=False)
            df = df.dropna(subset=["Close"])
            if len(df):
                return df
        except Exception:
            pass
        time.sleep(1)
    return None


def hist_kr(code):
    if "." in code:
        return hist(code, retries=1)
    for suf in (".KS", ".KQ"):
        df = hist(code + suf, retries=1)
        if df is not None:
            return df
    return None


def drop_partial(df, mkt):
    """마감 전·미래 날짜 봉 제거 → 확정 종가만 남김"""
    if df is None or df.empty or mkt not in CLOSE:
        return df
    tz, h, m = CLOSE[mkt]
    now = datetime.now(ZoneInfo(tz))
    while len(df):
        d = df.index[-1].date()
        if d > now.date() or (d == now.date() and (now.hour, now.minute) < (h, m)):
            df = df.iloc[:-1]
        else:
            break
    return df


def sma(c, w, back=0):
    s = c.iloc[:len(c) - back]
    return float(s.tail(w).mean()) if len(s) >= w else None


def rsi_series(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def align(p, m20, m60, m120):
    if None in (m20, m60, m120):
        return None
    if p > m20 > m60 > m120:
        return "정"
    if p < m20 < m60 < m120:
        return "역"
    return "혼"


def prior_ext(c, w, fn):
    s = c.iloc[-1 - w:-1]          # 오늘 제외 직전 w일
    return rnd(fn(s)) if len(s) >= 10 else None


# ───────── 종목 분석 ─────────
def analyze(df, mkt):
    df = drop_partial(df, mkt)
    if df is None or len(df) < 3:
        return None
    c = df["Close"].astype(float)
    v = df["Volume"].astype(float)
    n = len(c)
    last, prev = float(c.iloc[-1]), float(c.iloc[-2])

    ma = {w: sma(c, w) for w in (20, 60, 120)}
    mp = {w: sma(c, w, 1) for w in (20, 60, 120)}
    rs = rsi_series(c)
    st = 0
    for i in range(n - 1, 0, -1):
        if c.iloc[i] > c.iloc[i - 1]:
            st += 1
        else:
            break
    base = v.iloc[-21:-1].mean() if n >= 21 else None
    vx = float(v.iloc[-1] / base) if base and base > 0 else None
    lo = {w: prior_ext(c, w, min) for w in (20, 60, 120)}
    hi = {w: prior_ext(c, w, max) for w in (20, 60, 120)}
    levels = {"20일선": ma[20], "60일선": ma[60], "120일선": ma[120],
              "20일저점": lo[20], "60일저점": lo[60], "120일저점": lo[120],
              "20일고점": hi[20], "60일고점": hi[60], "120일고점": hi[120]}
    below = [(rnd(x), k) for k, x in levels.items() if x is not None and x < last]
    above = [(rnd(x), k) for k, x in levels.items() if x is not None and x > last]
    return {
        "c": rnd(last), "p": rnd(prev), "d1": pct(last, prev),
        "d5": pct(last, c.iloc[-6]) if n > 5 else None,
        "d20": pct(last, c.iloc[-21]) if n > 20 else None,
        "ma20": rnd(ma[20]), "ma60": rnd(ma[60]), "ma120": rnd(ma[120]),
        "mp20": mp[20], "mp60": mp[60], "mp120": mp[120],
        "g20": pct(last, ma[20]),
        "rsi": rnd(rs.iloc[-1], 1), "rsi_prev": rnd(rs.iloc[-2], 1),
        "vx": rnd(vx, 1),
        "al": align(last, ma[20], ma[60], ma[120]),
        "al_prev": align(prev, mp[20], mp[60], mp[120]),
        "st": st,
        "sup": list(max(below)) if below else None,   # [가격, 레벨명], None=지지없음(신저가권)
        "res": list(min(above)) if above else None,
        "lo20p": lo[20], "lo60": lo[60], "hi60": hi[60], "lo120": lo[120], "hi120": hi[120],
        "n": n, "dt": str(df.index[-1].date()),
    }


def event_flags(m):
    """상태가 아니라 '이벤트'만 경고 (매일 같은 경고 반복 방지)"""
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
    r, rp = m["rsi"], m["rsi_prev"]
    if r is not None and rp is not None:
        if r <= 30 < rp:
            f.append("RSI30↓진입")
        if r >= 70 > rp:
            f.append("RSI70↑진입")
    for w in (20, 60, 120):
        a, b = m[f"ma{w}"], m[f"mp{w}"]
        if a is not None and b is not None and m["p"] >= b and m["c"] < a:
            f.append(f"{w}일선이탈")
    # 저점 이탈은 가장 심각한 것 하나만
    if m["n"] > 120 and m["lo120"] is not None and m["c"] < m["lo120"]:
        f.append("120일저점이탈(신저가권)")
    elif m["n"] > 60 and m["lo60"] is not None and m["c"] < m["lo60"]:
        f.append("60일저점이탈")
    elif m["lo20p"] is not None and m["c"] < m["lo20p"]:
        f.append("20일저점이탈")
    if m["al"] == "역" and m["al_prev"] != "역":
        f.append("역배열진입")
    if m["al"] == "정" and m["al_prev"] != "정":
        f.append("정배열진입")
    return f


# ───────── 매크로 ─────────
def chg(c):
    c = c.dropna()
    if len(c) < 2:
        return None, None
    last = float(c.iloc[-1])
    out = [rnd(last)] + [pct(last, c.iloc[-1 - k]) if len(c) > k else None for k in (1, 5, 21)]
    return out, str(c.index[-1].date())


def chg_abs(c):
    c = c.dropna()
    if len(c) < 2:
        return None, None
    last = float(c.iloc[-1])
    out = [rnd(last)] + [rnd(last - float(c.iloc[-1 - k])) if len(c) > k else None
                         for k in (1, 5, 21)]
    return out, str(c.index[-1].date())


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


def fix_stale(macro, dts, ms):
    """야후 지수 일봉이 종목 일봉보다 늦으면(예: KOSPI 전일값) 대용 ETF로 추정, 실패 시 None"""
    ref = {"KR": ms.get("삼성전자", {}).get("dt"), "US": ms.get("알파벳", {}).get("dt")}
    ref["JP"] = ref["KR"]
    stale, proxied = [], {}
    for name, (t, mkt) in MACRO.items():
        r, d0, old = ref.get(mkt), dts.get(name), macro.get(name)
        if not (r and d0 and old and d0 < r):
            continue
        stale.append(name)
        macro[name] = None
        px = PROXY.get(name)
        if not px:
            continue
        etf = drop_partial(hist(px[0]), px[1])
        if etf is None or etf.empty or str(etf.index[-1].date()) != r:
            continue
        c = etf["Close"].astype(float)
        base = c[[str(i.date()) == d0 for i in c.index]]
        out, _ = chg(c)
        if out is None or not len(base):
            continue
        out[0] = rnd(old[0] * float(c.iloc[-1]) / float(base.iloc[-1]), 2)
        macro.pop(name, None)
        macro[name + "(추정)"] = out
        proxied[name] = px[0]
        dts[name] = r
    return ref, stale, proxied


def dump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def ref_returns(s):
    """일별 종가 Series → 직전월말·전년말 종가 대비 MTD/YTD(%)"""
    s = by_day(s.dropna())
    if len(s) < 2:
        return None
    last_d = s.index[-1]
    out = {"asof": str(last_d.date()), "mtd": None, "ytd": None}
    for key, cut in (("mtd", pd.Timestamp(last_d.year, last_d.month, 1)),
                     ("ytd", pd.Timestamp(last_d.year, 1, 1))):
        prior = s[s.index < cut]
        if len(prior):
            out[key] = pct(s.iloc[-1], prior.iloc[-1])
    return out


def bench():
    """벤치마크 MTD/YTD: KOSPI(원화), S&P500(달러), S&P500(원화환산)"""
    out = {}
    try:
        k = drop_partial(hist("^KS11", period="1y"), "KR")
        sp = drop_partial(hist("^GSPC", period="1y"), "US")
        fx = drop_partial(hist("KRW=X", period="1y"), "G")
        if k is not None and not k.empty:
            out["KOSPI"] = ref_returns(k["Close"])
        if sp is not None and not sp.empty:
            out["S&P500_USD"] = ref_returns(sp["Close"])
            if fx is not None and not fx.empty:
                kr = (by_day(sp["Close"]) * by_day(fx["Close"])).dropna()
                out["S&P500_KRW"] = ref_returns(kr)
    except Exception as e:
        print("bench 실패", e)
    return out


# ───────── 메인 ─────────
def main():
    ms, missing = {}, []

    def add(name, df, mkt):
        m = analyze(df, mkt)
        if m is None:
            missing.append(name)
        else:
            ms[name] = m

    for name, code in KR.items():
        add(name, hist_kr(code), "KR")
    for name, t in US.items():
        add(name, hist(t), "US")

    if len(missing) > 6:
        print("누락 과다:", missing)
        sys.exit(1)          # 이전 정상 파일 유지

    macro, dts = {}, {}
    for name, (t, mkt) in MACRO.items():
        df = drop_partial(hist(t), mkt)
        if df is None or df.empty:
            macro[name] = None
            continue
        macro[name], dts[name] = chg(df["Close"])

    krw, jpy = drop_partial(hist("KRW=X"), "G"), drop_partial(hist("JPY=X"), "G")
    if krw is not None and jpy is not None:
        s = (by_day(krw["Close"]) / by_day(jpy["Close"]) * 100).dropna()
        macro["JPY/KRW(100엔)"], _ = chg(s)

    t10 = drop_partial(hist("^TNX"), "G")           # 10Y: 당일 종가
    if t10 is not None and not t10.empty:
        s = t10["Close"].astype(float)
        if s.iloc[-1] > 20:                          # 구버전 ×10 표기 방어
            s = s / 10
        macro["미국10Y"], dts["UST10"] = chg_abs(s)
    y10, y2 = fred("DGS10"), fred("DGS2")            # 2Y·스프레드: FRED(지연 가능)
    if y2 is not None:
        macro["미국2Y(FRED)"], dts["FRED"] = chg_abs(y2)
    if y10 is not None and y2 is not None:
        macro["10Y-2Y(FRED)"], _ = chg_abs((y10 - y2).dropna())

    ref, stale, proxied = fix_stale(macro, dts, ms)

    alerts = []
    for name, m in ms.items():
        f = event_flags(m)
        if f:
            alerts.append([name, f, [m["d5"], m["d20"], m["g20"], m["rsi"], m["vx"], m["sup"], m["res"]]])
    rev = [k for k, m in ms.items() if m["al"] == "역"]
    rsi30 = [k for k, m in ms.items() if m["rsi"] is not None and m["rsi"] <= 30]
    state = {"n": len(ms),
             "정배열": sum(1 for m in ms.values() if m["al"] == "정"),
             "역배열": len(rev),
             "RSI≤30": len(rsi30)}
    asof = {"KR": ref.get("KR") or dts.get("KOSPI"), "JP": dts.get("닛케이225"), "US": dts.get("S&P500"),
            "FX": dts.get("USD/KRW"), "UST10": dts.get("UST10"), "FRED": dts.get("FRED")}
    gen = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")

    dump("daily.json", {
        "gen": gen, "asof": asof,
        "stocks": {k: [m["c"], m["p"], m["d1"]] for k, m in ms.items()},
        "alerts": alerts, "state": state, "rev": rev, "rsi30": rsi30,
        "macro": macro, "stale": stale, "proxy": proxied, "missing": missing,
    })
    dump("weekly.json", {
        "gen": gen, "asof": asof, "fields": FIELDS_W,
        "stocks": {k: [m[f] for f in FIELDS_W] for k, m in ms.items()},
        "bench": bench(),
    })
    print("완료", len(ms), "종목, 누락:", missing, "알림:", len(alerts))


if __name__ == "__main__":
    main()
