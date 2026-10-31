"""fetch_signals.py (v16) - 장 마감 확정 종가 기준 신호 계산
daily.json : 매일 읽는 경량 파일 / weekly.json : 토요일 심층 점검용
지수는 공식·주요 출처의 실제 종가만 사용(ETF 대용·추정 금지), 못 구하면 null + pending 표시
수량·평단 등 개인 포트폴리오 정보는 포함하지 않음 (공개 저장소용)"""
import json
import re
import warnings
warnings.filterwarnings("ignore")   # yfinance/pandas 경고로 로그가 길어지는 것 방지
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
    "SMR원자력": "0155M0", "미국10년국채H": "0091C0", "2차전지소재": "461950", "차이나반도체(관심)": "396520",
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
    "금": ("GC=F", "G"), "구리": ("HG=F", "G"), "Brent": ("BZ=F", "G"),
}
PROBES = {  # 시장 breadth·KORU 점검용 (보유 아님)
    "DOW": ("^DJI", "US"), "Russell2000": ("^RUT", "US"), "SOX": ("^SOX", "US"),
    "EWY(MSCI Korea)": ("EWY", "US"), "SK하이닉스": ("000660.KS", "KR"),
    "KODEX반도체": ("091160.KS", "KR"), "TOPIX(1306)": ("1306.T", "JP"),
}
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


UA = {"User-Agent": "Mozilla/5.0 (compatible; stock-data-relay)"}
TIMEOUT = 10          # 외부 요청 1회 제한(초)


def http_text(url, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as e:
            print("요청 실패", url[:70], i + 1, e)
            time.sleep(1 + i)
    return None


def treasury():
    """미 재무부 Daily Par Yield Curve → {y2,y10,y30,real10} Series. 실패 시 None"""
    year = datetime.now(ZoneInfo("America/New_York")).year

    def load(kind, y, cols):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               f"daily-treasury-rates.csv/{y}/all?type={kind}&field_tdr_date_value={y}&page&_format=csv")
        txt = http_text(url)
        if not txt or not txt.replace('"', "").lstrip().lower().startswith("date"):
            return None
        df = pd.read_csv(StringIO(txt))
        df.columns = [str(c).strip().lower() for c in df.columns]
        if not set(["date"] + cols) <= set(df.columns):
            return None
        out = df[["date"] + cols].copy()
        out["date"] = pd.to_datetime(out["date"], format="%m/%d/%Y", errors="coerce")
        for c in cols:
            out[c] = pd.to_numeric(out[c], errors="coerce")
        return out.dropna(subset=["date"])

    def table(kind, cols):
        cur = load(kind, year, cols)
        if cur is None:
            return None
        if len(cur) < 30:                       # 연초에는 전년도 데이터와 합침
            prev = load(kind, year - 1, cols)
            if prev is not None:
                cur = pd.concat([cur, prev])
        return cur.drop_duplicates("date").sort_values("date").set_index("date")

    nom = table("daily_treasury_yield_curve", ["2 yr", "10 yr", "30 yr"])
    if nom is None:
        return None
    out = {k: nom[c].dropna().tail(60) for k, c in (("y2", "2 yr"), ("y10", "10 yr"), ("y30", "30 yr"))}
    if len(out["y2"]) < 25 or len(out["y10"]) < 25:
        return None
    out["real10"] = None
    try:
        rl = table("daily_treasury_real_yield_curve", ["10 yr"])
        if rl is not None:
            out["real10"] = rl["10 yr"].dropna().tail(60)
    except Exception as e:
        print("실질금리 실패", e)
    return out


def fred(series, tries=2, keep=60):
    """FRED CSV (보조 출처). 실패 시 None"""
    txt = http_text(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}", tries)
    try:
        if not txt or not txt.lstrip()[:20].lower().startswith(("date", "observation_date")):
            raise ValueError("CSV 아님")
        df = pd.read_csv(StringIO(txt))
        s = pd.to_numeric(df.iloc[:, 1], errors="coerce")
        s.index = pd.to_datetime(df.iloc[:, 0])
        s = s.dropna().tail(keep)
        if len(s) >= 25:
            return s
        raise ValueError("데이터 부족")
    except Exception as e:
        print("FRED 실패", series, e)
        return None


def get_yields():
    """금리: 1순위 재무부(2Y/10Y/30Y, 실질 10Y), 2순위 FRED. 반환 (dict|None, 출처)"""
    t = treasury()
    if t is not None:
        print("금리 출처: 재무부")
        return t, "treasury"
    y10 = fred("DGS10")
    y2 = fred("DGS2") if y10 is not None else None
    if y10 is not None and y2 is not None:
        print("금리 출처: FRED")
        return {"y2": y2, "y10": y10, "y30": fred("DGS30", 1), "real10": None}, "fred"
    return None, None


def tsy_long(kind, cols, years=6):
    """재무부 CSV를 최근 N개 연도 이어붙여 장기 시계열(DataFrame)로. 실패 시 None"""
    y0 = datetime.now(ZoneInfo("America/New_York")).year
    parts = []
    for y in range(y0, y0 - years, -1):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               f"daily-treasury-rates.csv/{y}/all?type={kind}&field_tdr_date_value={y}&page&_format=csv")
        txt = http_text(url, 2)
        if not txt or not txt.replace('"', "").lstrip().lower().startswith("date"):
            continue
        df = pd.read_csv(StringIO(txt))
        df.columns = [str(c).strip().lower() for c in df.columns]
        if not set(["date"] + cols) <= set(df.columns):
            continue
        d = df[["date"] + cols].copy()
        d["date"] = pd.to_datetime(d["date"], format="%m/%d/%Y", errors="coerce")
        for c in cols:
            d[c] = pd.to_numeric(d[c], errors="coerce")
        parts.append(d.dropna(subset=["date"]))
    if not parts:
        return None
    return pd.concat(parts).drop_duplicates("date").sort_values("date").set_index("date")


def pct_rank(series, window=1260):
    """마지막 값이 최근 window(약 5년) 안에서 차지하는 백분위(0~100)"""
    h = series.dropna().tail(window)
    if len(h) < 250:
        return None
    return round(float((h.iloc[:-1] <= h.iloc[-1]).mean() * 100), 1)


def treasury_monitor():
    """미국채 진입환경 3요인 모니터(G안): 5년 상대가치 + 실질금리 + BE + 20일 모멘텀 + 변동성 상한.
    매수 신호가 아니라 상태 표시. 계산은 전부 여기서 끝내고 루틴에는 결과만 넘긴다."""
    nom = tsy_long("daily_treasury_yield_curve", ["10 yr", "30 yr"])
    real = tsy_long("daily_treasury_real_yield_curve", ["10 yr"])
    src = "treasury"
    if nom is None or nom["10 yr"].dropna().shape[0] < 250:
        y10, y30 = fred("DGS10", 2, 1400), fred("DGS30", 1, 1400)
        r10 = fred("DFII10", 1, 1400)
        if y10 is None or y30 is None:
            return None
        nom = pd.DataFrame({"10 yr": y10, "30 yr": y30})
        real = pd.DataFrame({"10 yr": r10}) if r10 is not None else None
        src = "fred"
    d = nom.dropna(subset=["10 yr"]).copy()
    d["rl"] = real["10 yr"] if real is not None else float("nan")
    d["be"] = d["10 yr"] - d["rl"]
    y10, y30 = d["10 yr"], d["30 yr"].dropna()
    if len(y30) < 250:
        return None
    rl, be = d["rl"].dropna(), d["be"].dropna()
    vol = y10.diff().rolling(60).std()
    spr = (d["30 yr"] - d["10 yr"]).dropna()

    def ch(s, n=20):
        s = s.dropna()
        return round(float(s.iloc[-1] - s.iloc[-1 - n]), 3) if len(s) > n else None

    o = {"asof": str(y10.index[-1].date()), "src": src,
         "y10": round(float(y10.iloc[-1]), 3), "y30": round(float(y30.iloc[-1]), 3),
         "y10_pct_5y": pct_rank(y10), "y30_pct_5y": pct_rank(y30),
         "real10": round(float(rl.iloc[-1]), 3) if len(rl) else None,
         "real10_pct_5y": pct_rank(rl) if len(rl) else None,
         "be10": round(float(be.iloc[-1]), 3) if len(be) else None,
         "y10_chg20": ch(y10), "y30_chg20": ch(y30), "be10_chg20": ch(be) if len(be) else None,
         "y10_vol60": round(float(vol.iloc[-1]), 4) if vol.notna().any() else None,
         "y10_vol_pct_5y": pct_rank(vol), "spr30_10": round(float(spr.iloc[-1]), 3),
         "spr30_10_pct_5y": pct_rank(spr)}

    def score(pv, pr, rpct, bech, chg_, long_spr=None):
        if pv is None or chg_ is None:
            return None
        sc = 2 if pv >= 80 else 1 if pv >= 60 else 0 if pv >= 40 else -1
        if rpct is not None:
            sc += 1 if rpct >= 75 else -1 if rpct < 40 else 0
        if bech is not None:
            sc += 0 if bech <= 0.05 else -1 if bech <= 0.15 else -2
        sc += 1 if chg_ <= 0 else 0 if chg_ <= 0.25 else -1 if chg_ <= 0.50 else -2
        if long_spr is not None:
            sc += 0 if long_spr >= 70 else -1 if long_spr >= 30 else -2
        return sc

    def state(sc, vol_hi):
        if sc is None:
            return "unknown"
        st = "green" if sc >= 3 else "yellow" if sc >= 1 else "orange" if sc >= -1 else "red"
        if vol_hi and st == "green":
            st = "yellow"                    # 변동성 상위 10%면 최대 노랑
        return st

    vol_hi = o["y10_vol_pct_5y"] is not None and o["y10_vol_pct_5y"] >= 90
    o["score_10y"] = score(o["y10_pct_5y"], None, o["real10_pct_5y"], o["be10_chg20"], o["y10_chg20"])
    o["score_30y"] = score(o["y30_pct_5y"], None, o["real10_pct_5y"], o["be10_chg20"], o["y30_chg20"],
                           o["spr30_10_pct_5y"] if o["spr30_10_pct_5y"] is not None else 50)
    o["state_10y"], o["state_30y"] = state(o["score_10y"], vol_hi), state(o["score_30y"], vol_hi)
    tags = []
    if o["real10_pct_5y"] is not None and o["real10_pct_5y"] >= 75:
        tags.append("REAL_YIELD_HIGH")
    if o["be10_chg20"] is not None and o["be10_chg20"] > 0.15:
        tags.append("INFLATION_REPRICING")
    if o["y10_chg20"] is not None and o["y10_chg20"] > 0.50:
        tags.append("RATES_RISING_FAST")
    if vol_hi:
        tags.append("HIGH_RATE_VOL")
    if o["spr30_10_pct_5y"] is not None and o["spr30_10_pct_5y"] < 30:
        tags.append("LONG_END_POOR_COMPENSATION")
    o["tags"] = tags or ["NORMAL"]
    o["dq"] = "ok" if (real is not None and o["real10_pct_5y"] is not None) else "no-real-yield"
    return o


def by_day(s):
    s = s.copy()
    s.index = pd.to_datetime(s.index.date)
    return s[~s.index.duplicated(keep="last")]


def naver_index(code):
    """네이버 금융 일별 지수 시세(KRX 종가 반영). 25행 미만이면 None"""
    pat = re.compile(r'<td class="date">\s*(\d{4}\.\d{2}\.\d{2})\s*</td>\s*'
                     r'<td class="number_1">\s*([\d,]+\.\d+)\s*</td>')
    rows = {}
    for page in range(1, 8):
        txt = http_text(f"https://finance.naver.com/sise/sise_index_day.naver?code={code}&page={page}", 2)
        if not txt:
            break
        found = pat.findall(txt)
        if not found and page == 1:
            print("네이버 HTML 형식 불일치:", code, len(txt), repr(txt[:160]))
        for d_, v in found:
            rows[pd.Timestamp(d_.replace(".", "-"))] = float(v.replace(",", ""))
        if len(rows) >= 25:
            break
    return pd.Series(rows).sort_index() if len(rows) >= 25 else None


def naver_mobile(code):
    """네이버 모바일 증권 JSON(보조). 날짜·종가만 읽는다. 실패 시 None"""
    import json as _json
    txt = http_text(f"https://m.stock.naver.com/api/index/{code}/price?pageSize=60&page=1", 2)
    if not txt:
        return None
    try:
        data = _json.loads(txt)
        if isinstance(data, dict):
            data = data.get("priceInfos") or data.get("items") or data.get("data") or []
        rows = {}
        for it in data:
            d_ = str(it.get("localTradedAt") or it.get("tradedAt") or "")[:10]
            v = it.get("closePrice") or it.get("close")
            if d_ and v is not None:
                rows[pd.Timestamp(d_)] = float(str(v).replace(",", ""))
        if len(rows) >= 25:
            return pd.Series(rows).sort_index()
        print("네이버 모바일 행 부족:", code, len(rows))
    except Exception as e:
        print("네이버 모바일 해석 실패:", code, e, repr(txt[:120]))
    return None


def nikkei_official():
    """닛케이 공식(indexes.nikkei.co.jp) 일별 종가 CSV"""
    txt = http_text("https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_daily_en.csv", 2)
    if not txt:
        return None
    rows = {}
    for line in txt.splitlines():
        p = [x.strip().strip('"') for x in line.lstrip("\ufeff").split(",")]
        if len(p) >= 5:
            try:
                rows[pd.Timestamp(datetime.strptime(p[0], "%Y/%m/%d"))] = float(p[4].replace(",", ""))
            except ValueError:
                continue
    return pd.Series(rows).sort_index().tail(60) if len(rows) >= 25 else None


def cboe_vix():
    """CBOE 공식 VIX 일별 종가 CSV"""
    txt = http_text("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv", 2)
    if not txt:
        return None
    df = pd.read_csv(StringIO(txt))
    df.columns = [str(c).strip().lower() for c in df.columns]
    if not {"date", "close"} <= set(df.columns):
        return None
    s = pd.Series(pd.to_numeric(df["close"], errors="coerce").values,
                  index=pd.to_datetime(df["date"], format="%m/%d/%Y", errors="coerce"))
    s = s[s.index.notna()].dropna().sort_index().tail(60)
    return s if len(s) >= 25 else None


def stooq(sym):
    """Stooq 일별 종가 CSV (보조 출처)"""
    txt = http_text(f"https://stooq.com/q/d/l/?s={sym}&i=d", 2)
    if not txt or not txt.lstrip().lower().startswith("date"):
        return None
    df = pd.read_csv(StringIO(txt))
    df.columns = [str(c).strip().lower() for c in df.columns]
    if not {"date", "close"} <= set(df.columns):
        return None
    s = pd.Series(pd.to_numeric(df["close"], errors="coerce").values,
                  index=pd.to_datetime(df["date"], errors="coerce"))
    s = s[s.index.notna()].dropna().sort_index().tail(60)
    return s if len(s) >= 25 else None


IDX_SRC = {   # 우선순위: 공식/주요 한국·일본·CBOE → Stooq → (같은 날짜면) 야후
    "KOSPI": [("naver-m", lambda: naver_mobile("KOSPI")), ("stooq", lambda: stooq("^kospi"))],
    "KOSDAQ": [("naver-m", lambda: naver_mobile("KOSDAQ")), ("stooq", lambda: stooq("^kosdaq"))],
    "닛케이225": [("nikkei", nikkei_official), ("stooq", lambda: stooq("^nkx"))],
    "VIX": [("cboe", cboe_vix), ("stooq", lambda: stooq("^vix"))],
}


def pick_index(name, ysr, ref):
    """기준일(ref) 이상의 실제 종가 시계열을 고른다. 없으면 (None, None) = 종가 확인 대기"""
    y = by_day(ysr.astype(float).dropna()) if ysr is not None and len(ysr) else None
    for label, fn in IDX_SRC[name]:
        try:
            s = fn()
        except Exception as e:
            print("지수 조회 오류", name, label, e)
            s = None
        if s is None or s.empty:
            continue
        s = by_day(s.astype(float).dropna())
        s = s[[str(i.date()) <= ref for i in s.index]]   # 장중 실행 시 기준일 이후(오늘 진행 중) 값 제외
        if s.empty:
            continue
        if y is not None:                       # 교차검증: 이미 확정된 날(최신일 제외)의 값이 야후와 1% 넘게 다르면 기각
            common = s.index.intersection(y.index)
            if len(common) >= 2:
                d_ = common[-2]
                if abs(s[d_] / y[d_] - 1) > (0.02 if name == "닛케이225" else 0.01):   # 야후 ^N225는 공식 종가와 상시 0.5~1.4% 어긋나 닛케이만 2% 허용
                    print("지수 교차검증 불일치", name, label, str(d_.date()), s[d_], y[d_])
                    continue
            if len(common) and common[-1] == s.index[-1] and abs(s[common[-1]] / y[common[-1]] - 1) > 0.01:
                print("최신일 값이 야후와 다름(공식 출처 채택)", name, label, s.iloc[-1], y[common[-1]])
                print("DBG", name, label, "공식 최근3", [(str(i.date()), round(v, 2)) for i, v in s.tail(3).items()], "야후 최근3", [(str(i.date()), round(v, 2)) for i, v in y.tail(3).items()])
        if str(s.index[-1].date()) >= ref:
            return label, s
        print("지수 날짜 부족", name, label, "최신", str(s.index[-1].date()), "필요", ref)
    if y is not None and len(y):
        y = y[[str(i.date()) <= ref for i in y.index]]
    if y is not None and len(y) and str(y.index[-1].date()) >= ref:
        return "yahoo", y
    return None, None


def apply_indices(macro, dts, ysr, ms):
    """KOSPI·KOSDAQ·닛케이·VIX를 공식/주요 출처의 실제 종가로 교체 (추정·대용 없음)"""
    ref = {"KR": ms.get("삼성전자", {}).get("dt"), "US": ms.get("알파벳", {}).get("dt")}
    # 일본 기준일: 한국 기준일을 강제하지 않고, 야후 닛케이의 최신 확정일(없으면 한국 기준일)을 쓴다 (공식 CSV 갱신 지연으로 인한 불필요한 대기 방지)
    yn = ysr.get("닛케이225")
    try:
        ref["JP"] = str(by_day(yn.astype(float).dropna()).index[-1].date()) if yn is not None and len(yn) else ref["KR"]
    except Exception:
        ref["JP"] = ref["KR"]
    mk = {"KOSPI": "KR", "KOSDAQ": "KR", "닛케이225": "JP", "VIX": "US"}
    src, pending = {}, []
    for name, m in mk.items():
        r = ref.get(m)
        if not r:
            continue
        label, s = pick_index(name, ysr.get(name), r)
        if s is None:
            macro[name] = None
            pending.append(name)
            continue
        macro[name], dts[name] = chg(s)
        src[name] = label
    return ref, src, pending


def probes():
    """breadth·KORU 점검용 보조 지표: 이름: [종가, 1일%, 5일%, 기준일]"""
    out = {}
    for name, (t, mkt) in PROBES.items():
        try:
            df = drop_partial(hist(t, retries=1), mkt)
            if df is None or df.empty:
                continue
            c = df["Close"].astype(float)
            out[name] = [rnd(c.iloc[-1]), pct(c.iloc[-1], c.iloc[-2]) if len(c) > 1 else None,
                         pct(c.iloc[-1], c.iloc[-6]) if len(c) > 5 else None, str(c.index[-1].date())]
        except Exception as e:
            print("probe 실패", name, e)
    return out


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

    macro, dts, ysr = {}, {}, {}
    for name, (t, mkt) in MACRO.items():
        df = drop_partial(hist(t), mkt)
        if df is None or df.empty:
            macro[name] = None
            continue
        ysr[name] = df["Close"]
        if MACRO[name][1] == "G":      # 진단: 24시간 자산의 마지막 3개 봉(날짜·종가)을 로그에 남긴다
            print("DBG", name, [(str(i), round(float(v), 3)) for i, v in df["Close"].tail(3).items()])
        macro[name], dts[name] = chg(df["Close"])

    krw, jpy = drop_partial(hist("KRW=X"), "G"), drop_partial(hist("JPY=X"), "G")
    if krw is not None and jpy is not None:
        s = (by_day(krw["Close"]) / by_day(jpy["Close"]) * 100).dropna()
        macro["JPY/KRW(100엔)"], _ = chg(s)

    ref, idx_src, pending = apply_indices(macro, dts, ysr, ms)

    t10 = drop_partial(hist("^TNX"), "G")           # 시장금리(참고용). 재무부 CMT와 한 시계열로 섞지 않음
    if t10 is not None and not t10.empty:
        s = t10["Close"].astype(float)
        if s.iloc[-1] > 20:                          # 구버전 ×10 표기 방어
            s = s / 10
        macro["미국10Y(시장)"], dts["UST10"] = chg_abs(s)

    ydat, ysrc = get_yields()                        # 공식 Daily Treasury Par Yield (재무부 → FRED)
    if ydat is not None:
        y10, y2, y30 = ydat["y10"], ydat["y2"], ydat.get("y30")
        macro["미국10Y"], dts["TSY"] = chg_abs(y10)
        macro["미국2Y"], _ = chg_abs(y2)
        if y30 is not None and len(y30) >= 25:
            macro["미국30Y"], _ = chg_abs(y30)
        macro["10Y-2Y"], _ = chg_abs((y10 - y2).dropna())
        if ydat.get("real10") is not None:
            be = (y10 - ydat["real10"]).dropna()
            if len(be) >= 25:
                macro["10Y BE"], _ = chg_abs(be)
    else:                                            # 실패 시 직전 파일(v8 형식)의 값을 기준일과 함께 재사용
        try:
            with open("daily.json", encoding="utf-8") as f:
                prev = json.load(f)
            if prev.get("tsy") in ("treasury", "fred", "prev-reuse"):
                for k in ("미국10Y", "미국2Y", "미국30Y", "10Y-2Y", "10Y BE"):
                    if prev.get("macro", {}).get(k):
                        macro[k] = prev["macro"][k]
                dts["TSY"] = prev.get("asof", {}).get("TSY")
                ysrc = "prev-reuse"
                print("금리 직전 값 재사용")
        except Exception as e:
            print("금리 직전 값 재사용 실패", e)

    try:
        tmon = treasury_monitor()
    except Exception as e:
        print("국채 모니터 실패", e)
        tmon = None
    if tmon is None:                                  # 실패 시 직전 값을 기준일과 함께 재사용
        try:
            with open("daily.json", encoding="utf-8") as f:
                tmon = json.load(f).get("tmon")
            if tmon:
                tmon["src"] = "prev-reuse"
        except Exception:
            tmon = None

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
    asof = {"KR": ref.get("KR"), "JP": dts.get("닛케이225") or ref.get("JP"), "US": dts.get("S&P500"),
            "FX": dts.get("USD/KRW"), "TSY": dts.get("TSY"), "UST10": dts.get("UST10")}
    idx = {k: [dts.get(k), v] for k, v in idx_src.items()}
    focus = {"KORU": {f: ms["KORU"][f] for f in ("c", "d1", "d5", "d20", "g20", "rsi", "vx", "al",
                                                  "ma20", "ma60", "ma120", "sup", "res", "st")}} if "KORU" in ms else {}
    gen = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")

    dump("daily.json", {
        "gen": gen, "asof": asof,
        "stocks": {k: [m["c"], m["p"], m["d1"]] for k, m in ms.items()},
        "alerts": alerts, "state": state, "rev": rev, "rsi30": rsi30,
        "macro": macro, "idx": idx, "pending": pending, "tsy": ysrc,
        "breadth": probes(), "focus": focus, "missing": missing,
        "lv": {k: [m["ma20"], m["ma60"], m["lo20p"], m["lo60"], m["d5"], m["d20"], m["g20"], m["rsi"], m["vx"], m["al"]] for k, m in ms.items()},
        "tmon": tmon,
    })
    dump("weekly.json", {
        "gen": gen, "asof": asof, "fields": FIELDS_W,
        "stocks": {k: [m[f] for f in FIELDS_W] for k, m in ms.items()},
        "bench": bench(),
    })
    print("완료", len(ms), "종목, 누락:", missing, "알림:", len(alerts))


if __name__ == "__main__":
    main()
