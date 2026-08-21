#!/usr/bin/env python3
"""US equity drawdown-risk monitor — composite danger score.

Following the spec in docs/scoring.md, this fetches indicators from free
public sources and computes a 0-100 composite "danger score". The scoring
logic uses only the standard library; chart generation (optional) uses
matplotlib.

Data sources:
    - FRED : yield-curve spreads / HY & IG OAS / NFCI / Sahm rule / VIX
             (uses the API when FRED_API_KEY is set, otherwise the public CSV)
    - Yahoo: S&P 500 & Dow prices, VIX3M, SKEW, VVIX, RSP/SPY (breadth proxy)
    - multpl: Shiller CAPE
    - CNN  : Fear & Greed (best effort; may be unreachable from CI runners)

Network note:
    Some environments block direct access to FRED / Yahoo / CNN. Run this on
    GitHub Actions (or any host that can reach those sites), or use --manual.

Usage:
    python market_risk.py                    # auto-fetch -> text
    python market_risk.py --report md         # auto-fetch -> Markdown (issue body)
    python market_risk.py --json              # JSON output
    python market_risk.py --alert             # intraday threshold check (JSON)
    python market_risk.py --charts DIR        # write chart PNGs into DIR
    python market_risk.py --manual            # enter values by hand

Disclaimer: educational heuristic, NOT investment advice.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import http.cookiejar
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from html import unescape

# CBOE's CDN and CNN's dataviz host both reject non-browser clients, so send a
# realistic browser header set rather than a bespoke agent string.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 25
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()

# Long-run reference levels used as chart baselines / context.
CAPE_LONG_RUN_MEAN = 17.0
VIX_LONG_RUN_AVG = 19.5


# ---------------------------------------------------------------------------
# Low-level fetch helpers
# ---------------------------------------------------------------------------
def _get(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={**BROWSER_HEADERS, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def fetch_fred(series_id: str) -> tuple[float | None, str | None]:
    """Latest (value, date) for a FRED series. API if key set, else CSV."""
    try:
        if FRED_API_KEY:
            url = (f"https://api.stlouisfed.org/fred/series/observations"
                   f"?series_id={series_id}&api_key={FRED_API_KEY}"
                   f"&file_type=json&sort_order=desc&limit=8")
            for o in json.loads(_get(url))["observations"]:
                if o["value"] not in (".", ""):
                    return float(o["value"]), o["date"]
        else:
            raw = _get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}")
            for line in reversed(raw.decode("utf-8").strip().splitlines()[1:]):
                date, _, val = line.partition(",")
                if val.strip() not in (".", ""):
                    return float(val.strip()), date.strip()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] FRED {series_id}: {e}", file=sys.stderr)
    return None, None


def fetch_yahoo_series(symbol: str, rng: str = "1y") -> tuple[list[int], list[float]]:
    """Return (timestamps, closes) daily series from Yahoo chart API."""
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?range={rng}&interval=1d")
        res = json.loads(_get(url))["chart"]["result"][0]
        ts = res.get("timestamp", [])
        closes = res["indicators"]["quote"][0]["close"]
        pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
        return [t for t, _ in pairs], [c for _, c in pairs]
    except Exception as e:  # noqa: BLE001
        print(f"[warn] Yahoo {symbol}: {e}", file=sys.stderr)
        return [], []


def fetch_yahoo_closes(symbol: str, rng: str = "1y") -> list[float]:
    return fetch_yahoo_series(symbol, rng)[1]


def fetch_yahoo_quote(symbol: str) -> dict | None:
    """Return {last, prev} from Yahoo chart meta (for intraday change %)."""
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?range=5d&interval=1d")
        meta = json.loads(_get(url))["chart"]["result"][0]["meta"]
        last = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if last is not None:
            return {"last": float(last),
                    "prev": float(prev) if prev is not None else None}
    except Exception as e:  # noqa: BLE001
        print(f"[warn] Yahoo quote {symbol}: {e}", file=sys.stderr)
    return None


def fetch_cape() -> float | None:
    """Current Shiller CAPE from multpl (scraped)."""
    try:
        html = _get("https://www.multpl.com/shiller-pe").decode("utf-8", "ignore")
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
        m = re.search(r"Current Shiller PE Ratio[:\s]*([0-9]+(?:\.[0-9]+)?)", text)
        if m:
            return float(m.group(1))
        print("[warn] CAPE(multpl): pattern not found", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] CAPE(multpl): {e}", file=sys.stderr)
    return None


def fetch_cape_history(max_points: int = 240) -> list[tuple[str, float]]:
    """Monthly Shiller CAPE history from multpl (best effort). Newest last."""
    try:
        page = _get("https://www.multpl.com/shiller-pe/table/by-month").decode("utf-8", "ignore")
        # Rows look like: <td>Aug 20, 2026</td><td> &#x2002; 41.79 </td> -- the
        # EN-SPACE entity sits between date and value, so parse the cells
        # rather than matching across the gap.
        rows = re.findall(r"<td>([^<]+)</td>\s*<td>(.*?)</td>", page, re.S)
        pts = []
        for d, v in rows:
            m = re.search(r"-?[0-9]+\.[0-9]+", unescape(v))
            if m:
                pts.append((unescape(d).strip(), float(m.group(0))))
        pts.reverse()  # page is newest-first -> make oldest-first
        return pts[-max_points:]
    except Exception as e:  # noqa: BLE001
        print(f"[warn] CAPE history(multpl): {e}", file=sys.stderr)
    return []


def fetch_cnn_fng() -> dict | None:
    try:
        # cnn.com's dataviz host no longer resolves; the live one is cnn.io.
        d = json.loads(_get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata"))
        out = {"score": float(d["fear_and_greed"]["score"]),
               "rating": d["fear_and_greed"]["rating"]}
        for k, v in d.items():
            if isinstance(v, dict) and "score" in v and k != "fear_and_greed":
                out[k] = v.get("score")
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[warn] CNN F&G: {e}", file=sys.stderr)
    return None


def fetch_spy_put_call() -> float | None:
    """Put/call *volume* ratio for SPY's front-month option chain (Yahoo).

    Cboe's own total put/call archive stopped updating on 2019-10-04, so the
    live ratio is computed from SPY's nearest-expiry chain instead. Yahoo's
    options endpoint needs a session cookie plus a crumb token. Note this is
    an index/ETF ratio, which is structurally more put-heavy than Cboe's
    all-market total -- score it with PUT_CALL_INDEX_TABLE, not the total-scale
    thresholds.
    """
    try:
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def _open(url):
            return opener.open(urllib.request.Request(url, headers=BROWSER_HEADERS),
                               timeout=TIMEOUT).read()

        try:
            _open("https://fc.yahoo.com")   # sets the session cookie
        except Exception:
            pass                            # this endpoint 404s but still sets it
        crumb = _open("https://query1.finance.yahoo.com/v1/test/getcrumb").decode().strip()
        if not crumb:
            print("[warn] SPY put/call: empty crumb", file=sys.stderr)
            return None
        d = json.loads(_open("https://query2.finance.yahoo.com/v7/finance/options/SPY"
                             f"?crumb={urllib.parse.quote(crumb)}"))
        chain = d["optionChain"]["result"][0]["options"][0]
        calls = sum(c.get("volume") or 0 for c in chain["calls"])
        puts = sum(p.get("volume") or 0 for p in chain["puts"])
        if calls <= 0:
            print("[warn] SPY put/call: zero call volume", file=sys.stderr)
            return None
        return round(puts / calls, 3)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] SPY put/call: {e}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Normalisation (keep in sync with docs/scoring.md)
# ---------------------------------------------------------------------------
def _bucket(value, table, default=0):
    for hi, danger in table:
        if value < hi:
            return danger
    return default


def _ecy_danger(ecy):
    # Excess CAPE Yield (Shiller): (1/CAPE) - real 10y yield.
    # Higher = equities cheaper vs bonds = lower danger.
    if ecy >= 4:
        return 10
    if ecy >= 3:
        return 25
    if ecy >= 2:
        return 45
    if ecy >= 1:
        return 65
    if ecy >= 0:
        return 80
    return 95


def score_valuation(cape, pe_dev_pct, ecy=None):
    parts = []
    if cape is not None:
        parts.append(_bucket(cape, [(20, 0), (25, 20), (30, 40), (35, 60), (40, 80)], 100))
    if pe_dev_pct is not None:
        parts.append(min(100, max(0, pe_dev_pct)))
    cape_pe = sum(parts) / len(parts) if parts else None
    if ecy is not None:
        # ECY is the improved, rate-aware measure -> weight it higher (0.6).
        return 0.6 * _ecy_danger(ecy) + 0.4 * cape_pe if cape_pe is not None else float(_ecy_danger(ecy))
    return cape_pe if cape_pe is not None else 0.0


def score_trend(dev200, drawdown_pct, below_ma200):
    cands = []
    if dev200 is not None:
        if dev200 > 12:
            cands.append(60)
        elif dev200 > 8:
            cands.append(40)
        elif dev200 >= -3:
            cands.append(10)
        elif dev200 >= -7:
            cands.append(55)
        elif dev200 >= -12:
            cands.append(75)
        else:
            cands.append(90)
    if below_ma200:
        cands.append(70)
    if drawdown_pct is not None:
        cands.append(_bucket(drawdown_pct, [(3, 0), (7, 30), (10, 55), (20, 75)], 95))
    return max(cands) if cands else 0.0


def score_volatility(vix, vix3m, skew=None, vvix=None):
    if vix is None:
        return 0.0
    base = _bucket(vix, [(13, 10), (17, 20), (20, 35), (25, 50), (30, 65), (40, 85)], 100)
    if vix3m is not None and vix > vix3m:
        base += 25
    if skew is not None and skew >= 150:
        base += 5
    if vvix is not None and vvix >= 110:
        base += 5
    return float(min(100, base))


# Cboe all-market total put/call thresholds (docs/scoring.md).
PUT_CALL_TOTAL_TABLE = [(0.7, 60), (0.9, 25), (1.1, 15), (1.3, 40)]
# Index/ETF option chains are structurally more put-heavy, so the same danger
# levels sit at higher ratios. These cut-points are the percentile-matched
# equivalents of the total-scale ones, derived from Cboe's own totalpc.csv vs
# indexpc.csv archives (3,253 shared sessions, 2006-11-01..2019-10-04):
#   total 0.7 = 2.3rd pct -> index 0.71    total 1.1 = 83.4th pct -> index 1.47
#   total 0.9 = 39.2nd pct -> index 1.09   total 1.3 = 97.0th pct -> index 1.84
PUT_CALL_INDEX_TABLE = [(0.71, 60), (1.09, 25), (1.47, 15), (1.84, 40)]


def _put_call_danger(ratio, table):
    for cut, danger in table:
        if ratio < cut:
            return danger
    return 70


def score_sentiment(put_call, fng, put_call_index=None):
    parts = []
    if put_call is not None:
        parts.append(_put_call_danger(put_call, PUT_CALL_TOTAL_TABLE))
    elif put_call_index is not None:
        parts.append(_put_call_danger(put_call_index, PUT_CALL_INDEX_TABLE))
    if fng is not None:
        if fng > 80:
            parts.append(70)
        elif fng > 60:
            parts.append(45)
        elif fng >= 40:
            parts.append(20)
        elif fng >= 20:
            parts.append(45)
        else:
            parts.append(65)
    return sum(parts) / len(parts) if parts else 0.0


def _spread_danger(spread):
    if spread > 0.5:
        return 10
    if spread >= 0:
        return 30
    if spread >= -0.3:
        return 60
    return 80


def score_credit(t10y2y, t10y3m, hy_oas, ig_oas=None, nfci=None, sahm=None):
    cands = []
    for s in (t10y2y, t10y3m):
        if s is not None:
            cands.append(_spread_danger(s))
    if hy_oas is not None:
        cands.append(_bucket(hy_oas, [(3.5, 10), (4.5, 30), (5.5, 50), (7, 70)], 90))
    if ig_oas is not None:
        cands.append(_bucket(ig_oas, [(1.0, 10), (1.3, 30), (1.7, 50), (2.2, 70)], 90))
    if nfci is not None:
        cands.append(_bucket(nfci, [(-0.5, 10), (0, 20), (0.3, 45), (0.7, 70)], 90))
    if sahm is not None and sahm >= 0.5:
        cands.append(75)
    return max(cands) if cands else 0.0


def score_breadth(pct_above_200ma, ew_cw_dev=None):
    if pct_above_200ma is not None:
        return _bucket(100 - pct_above_200ma, [(40, 10), (50, 25), (60, 45), (70, 65)], 85)
    if ew_cw_dev is not None:
        # equal-weight / cap-weight ratio deviation from its 200-day mean (%).
        # More negative => narrow leadership => higher danger.
        if ew_cw_dev > 1:
            return 10
        if ew_cw_dev > -1:
            return 25
        if ew_cw_dev > -3:
            return 45
        if ew_cw_dev > -5:
            return 65
        return 85
    return 0.0


WEIGHTS = {"valuation": 0.15, "trend": 0.20, "volatility": 0.20,
           "sentiment": 0.15, "credit": 0.20, "breadth": 0.10}
LABELS = {"valuation": "Valuation", "trend": "Trend",
          "volatility": "Volatility", "sentiment": "Sentiment",
          "credit": "Credit/Macro", "breadth": "Breadth"}


@dataclass
class Inputs:
    cape: float | None = None
    pe_dev_pct: float | None = None
    real10y: float | None = None   # 10Y real yield (FRED DFII10)
    ecy: float | None = None       # Excess CAPE Yield = 1/CAPE - real10y
    dev200: float | None = None
    drawdown_pct: float | None = None
    below_ma200: bool | None = None
    vix: float | None = None
    vix3m: float | None = None
    skew: float | None = None
    vvix: float | None = None
    put_call: float | None = None        # Cboe all-market total ratio (manual)
    put_call_index: float | None = None  # SPY front-month chain ratio (auto)
    fng: float | None = None
    t10y2y: float | None = None
    t10y3m: float | None = None
    hy_oas: float | None = None
    ig_oas: float | None = None
    nfci: float | None = None
    sahm: float | None = None
    pct_above_200ma: float | None = None
    ew_cw_dev: float | None = None  # equal-weight/cap-weight ratio dev (breadth proxy)


def _available(inp: Inputs) -> dict[str, bool]:
    return {
        "valuation": inp.cape is not None or inp.pe_dev_pct is not None or inp.ecy is not None,
        "trend": inp.dev200 is not None or inp.drawdown_pct is not None,
        "volatility": inp.vix is not None,
        "sentiment": any(x is not None for x in
                         (inp.put_call, inp.put_call_index, inp.fng)),
        "credit": any(x is not None for x in
                      (inp.t10y2y, inp.t10y3m, inp.hy_oas, inp.ig_oas, inp.nfci, inp.sahm)),
        "breadth": inp.pct_above_200ma is not None or inp.ew_cw_dev is not None,
    }


def tier(score: float) -> str:
    if score < 20:
        return "Calm 🟢"
    if score < 40:
        return "Mild caution 🟢🟡"
    if score < 60:
        return "Caution 🟡"
    if score < 80:
        return "Elevated 🟠"
    return "Danger 🔴"


def compute(inp: Inputs) -> dict:
    cats = {
        "valuation": score_valuation(inp.cape, inp.pe_dev_pct, inp.ecy),
        "trend": score_trend(inp.dev200, inp.drawdown_pct, inp.below_ma200),
        "volatility": score_volatility(inp.vix, inp.vix3m, inp.skew, inp.vvix),
        "sentiment": score_sentiment(inp.put_call, inp.fng, inp.put_call_index),
        "credit": score_credit(inp.t10y2y, inp.t10y3m, inp.hy_oas,
                               inp.ig_oas, inp.nfci, inp.sahm),
        "breadth": score_breadth(inp.pct_above_200ma, inp.ew_cw_dev),
    }
    avail = _available(inp)
    wsum = sum(WEIGHTS[k] for k in cats if avail[k]) or 1.0
    total = round(sum(cats[k] * WEIGHTS[k] for k in cats if avail[k]) / wsum, 1)
    return {"score": total, "tier": tier(total), "categories": cats,
            "available": avail, "inputs": asdict(inp)}


# ---------------------------------------------------------------------------
# Auto collection
# ---------------------------------------------------------------------------
def collect_auto() -> tuple[Inputs, dict]:
    inp, dates = Inputs(), {}
    _, sp = fetch_yahoo_series("%5EGSPC")
    if len(sp) >= 200:
        price = sp[-1]
        ma200 = sum(sp[-200:]) / 200
        inp.dev200 = round((price - ma200) / ma200 * 100, 2)
        inp.below_ma200 = price < ma200
        hi = max(sp[-252:]) if len(sp) >= 252 else max(sp)
        inp.drawdown_pct = round((hi - price) / hi * 100, 2)
    for sym, attr in (("%5EVIX", "vix"), ("%5EVIX3M", "vix3m"),
                      ("%5ESKEW", "skew"), ("%5EVVIX", "vvix")):
        s = fetch_yahoo_closes(sym, "1mo")
        if s:
            setattr(inp, attr, round(s[-1], 2))
    # breadth proxy: equal-weight (RSP) / cap-weight (SPY) ratio vs its 200d mean
    rsp = fetch_yahoo_closes("RSP")
    spy = fetch_yahoo_closes("SPY")
    if len(rsp) >= 200 and len(spy) >= 200:
        n = min(len(rsp), len(spy))
        ratio = [a / b for a, b in zip(rsp[-n:], spy[-n:])]
        ma = sum(ratio[-200:]) / 200
        inp.ew_cw_dev = round((ratio[-1] - ma) / ma * 100, 2)
    for series, attr in (("T10Y2Y", "t10y2y"), ("T10Y3M", "t10y3m"),
                         ("BAMLH0A0HYM2", "hy_oas"), ("BAMLC0A0CM", "ig_oas"),
                         ("NFCI", "nfci"), ("SAHMREALTIME", "sahm"),
                         ("DFII10", "real10y")):
        val, d = fetch_fred(series)
        setattr(inp, attr, val)
        if d:
            dates[attr] = d
    inp.cape = fetch_cape()
    # Excess CAPE Yield (Shiller): earnings yield minus the real 10y yield.
    if inp.cape and inp.real10y is not None:
        inp.ecy = round(100.0 / inp.cape - inp.real10y, 2)
    inp.put_call_index = fetch_spy_put_call()
    fng = fetch_cnn_fng()
    if fng:
        inp.fng = fng["score"]  # CNN put_call_options is a 0-100 index, not a ratio
    return inp, dates


def collect_manual() -> tuple[Inputs, dict]:
    inp = Inputs()
    prompts = [
        ("cape", "Shiller CAPE (e.g. 42): "),
        ("real10y", "10Y real yield %, FRED DFII10 (optional, e.g. 1.9): "),
        ("dev200", "S&P500 200-day MA deviation % (e.g. 3.5): "),
        ("drawdown_pct", "Drawdown from 52w high % (e.g. 1.2): "),
        ("vix", "VIX (e.g. 14.7): "),
        ("vix3m", "VIX3M (optional): "),
        ("skew", "CBOE SKEW (optional): "),
        ("vvix", "VVIX (optional): "),
        ("put_call", "Put/Call ratio, CBOE total (e.g. 0.9): "),
        ("fng", "CNN Fear&Greed 0-100 (e.g. 62): "),
        ("t10y2y", "10Y-2Y spread % (e.g. 0.4): "),
        ("t10y3m", "10Y-3M spread % (optional): "),
        ("hy_oas", "HY OAS spread % (e.g. 3.2): "),
        ("ig_oas", "IG OAS spread % (optional): "),
        ("nfci", "Chicago Fed NFCI (optional): "),
        ("sahm", "Sahm rule value (optional): "),
        ("pct_above_200ma", "% of stocks above 200d MA (optional): "),
    ]
    for field_name, msg in prompts:
        raw = input(msg).strip()
        if raw:
            setattr(inp, field_name, float(raw))
    if inp.dev200 is not None:
        inp.below_ma200 = inp.dev200 < 0
    if inp.cape and inp.real10y is not None:
        inp.ecy = round(100.0 / inp.cape - inp.real10y, 2)
    return inp, {}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _top_flags(result: dict, n: int = 3):
    avail = result["available"]
    items = [(LABELS[k], v) for k, v in result["categories"].items() if avail[k]]
    return sorted(items, key=lambda x: -x[1])[:n]


def render_text(result: dict) -> str:
    lines = [f"Danger score {result['score']} / 100 ({result['tier']})", "",
             "By category:"]
    for k, v in sorted(result["categories"].items(), key=lambda x: -x[1]):
        na = "" if result["available"][k] else "  (N/A, excluded)"
        lines.append(f"  {LABELS[k]:<12} {v:5.1f}  (weight {int(WEIGHTS[k]*100)}%){na}")
    lines += ["", "Educational heuristic, not investment advice."]
    return "\n".join(lines)


def _jp_summary(result: dict) -> str:
    """One-line Japanese summary for quick reading."""
    jp_tier = {"Calm 🟢": "平常🟢", "Mild caution 🟢🟡": "やや注意🟢🟡",
               "Caution 🟡": "注意🟡", "Elevated 🟠": "警戒🟠", "Danger 🔴": "危険🔴"}
    flags = "、".join(n for n, _ in _top_flags(result))
    return f"🇯🇵 危険度 {result['score']}/100（{jp_tier.get(result['tier'], result['tier'])}）｜上位フラグ: {flags}"


def render_markdown(result: dict, dates: dict, asof: str, charts: list[str] | None = None) -> str:
    inp = result["inputs"]
    L = [f"> {_jp_summary(result)}", "",
         f"## US market danger score — **{result['score']} / 100** ({result['tier']})  ·  {asof}", ""]
    L += ["### Top flags"]
    for name, v in _top_flags(result):
        L.append(f"- **{name}**: {v:.0f}")
    if charts:
        L += ["", "### Charts"]
        for c in charts:
            L.append(f"![{c}]({c})")
    L += ["", "### By category", "", "| Category | Danger | Weight |", "|---|---|---|"]
    for k, v in sorted(result["categories"].items(), key=lambda x: -x[1]):
        na = " (N/A)" if not result["available"][k] else ""
        L.append(f"| {LABELS[k]} | {v:.1f}{na} | {int(WEIGHTS[k]*100)}% |")
    L += ["", "### Indicators", "", "| Indicator | Value | As of |", "|---|---|---|"]
    rows = [
        ("Shiller CAPE", inp["cape"], None),
        ("10Y real yield % (DFII10)", inp["real10y"], dates.get("real10y")),
        ("Excess CAPE Yield (ECY) %", inp["ecy"], None),
        ("S&P500 dev from 200d MA %", inp["dev200"], None),
        ("Drawdown from 52w high %", inp["drawdown_pct"], None),
        ("VIX", inp["vix"], None), ("VIX3M", inp["vix3m"], None),
        ("SKEW", inp["skew"], None), ("VVIX", inp["vvix"], None),
        ("Put/Call (Cboe total)", inp["put_call"], None),
        ("Put/Call (SPY front-month)", inp["put_call_index"], None),
        ("Fear & Greed", inp["fng"], None),
        ("10Y-2Y", inp["t10y2y"], dates.get("t10y2y")),
        ("10Y-3M", inp["t10y3m"], dates.get("t10y3m")),
        ("HY OAS %", inp["hy_oas"], dates.get("hy_oas")),
        ("IG OAS %", inp["ig_oas"], dates.get("ig_oas")),
        ("NFCI", inp["nfci"], dates.get("nfci")),
        ("Sahm rule", inp["sahm"], dates.get("sahm")),
        ("% above 200d MA", inp["pct_above_200ma"], None),
        ("Equal/Cap-weight dev % (breadth)", inp["ew_cw_dev"], None),
    ]
    for name, val, d in rows:
        L.append(f"| {name} | {'—' if val is None else val} | {d or ''} |")
    L += ["", "_Educational heuristic, not investment advice._"]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Intraday alert
# ---------------------------------------------------------------------------
def run_alert(asof: str) -> dict:
    qv = fetch_yahoo_quote("%5EVIX")
    qs = fetch_yahoo_quote("%5EGSPC")
    q3 = fetch_yahoo_quote("%5EVIX3M")
    inp, dates = collect_auto()
    result = compute(inp)

    fired, tags = [], []
    if qv:
        if qv["last"] >= 20:
            fired.append(f"VIX {qv['last']:.1f} (>20)")
            tags.append("VIX>20")
        elif qv["prev"] and (qv["last"] - qv["prev"]) / qv["prev"] * 100 >= 20:
            pct = (qv["last"] - qv["prev"]) / qv["prev"] * 100
            fired.append(f"VIX spike +{pct:.0f}% vs prior close ({qv['last']:.1f})")
            tags.append("VIX spike")
    if qs and qs["prev"]:
        chg = (qs["last"] - qs["prev"]) / qs["prev"] * 100
        if chg <= -2:
            fired.append(f"S&P500 intraday {chg:.1f}% vs prior close")
            tags.append("SPX drop")
    if qv and q3 and qv["last"] > q3["last"]:
        fired.append(f"VIX term-structure inverted (VIX {qv['last']:.1f} > VIX3M {q3['last']:.1f})")
        tags.append("backwardation")
    if result["score"] >= 60:
        fired.append(f"Danger score {result['score']} ({result['tier']})")
        tags.append("score>=60")

    if not fired:
        return {"fired": False}
    body = f"> {_jp_summary(result)}\n\n## ⚠️ Intraday alert — {asof}\n\n### Triggered\n"
    body += "".join(f"- {f}\n" for f in fired)
    body += "\n" + render_markdown(result, dates, asof)
    return {"fired": True, "signature": " / ".join(tags), "body": body}


# ---------------------------------------------------------------------------
# Charts (matplotlib; optional)
# ---------------------------------------------------------------------------
def generate_charts(out_dir: str, history_csv: str | None = None) -> list[str]:
    """Write chart PNGs into out_dir; return the list of filenames written."""
    import datetime as _dt
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []

    def _save(fig, name):
        path = os.path.join(out_dir, name)
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)
        written.append(name)

    def _dates(ts):
        return [_dt.datetime.utcfromtimestamp(t) for t in ts]

    # 1) S&P 500 with 50/200-day moving averages
    ts, sp = fetch_yahoo_series("%5EGSPC", "1y")
    if len(sp) >= 200:
        d = _dates(ts)
        ma50 = [sum(sp[max(0, i - 49):i + 1]) / len(sp[max(0, i - 49):i + 1]) for i in range(len(sp))]
        ma200 = [sum(sp[max(0, i - 199):i + 1]) / len(sp[max(0, i - 199):i + 1]) for i in range(len(sp))]
        fig, ax = plt.subplots(figsize=(8, 3.2))
        ax.plot(d, sp, label="S&P 500", lw=1.4)
        ax.plot(d, ma50, label="50-day MA", lw=1.0, alpha=0.9)
        ax.plot(d, ma200, label="200-day MA", lw=1.0, alpha=0.9)
        ax.set_title("S&P 500 with moving averages")
        ax.legend(loc="best", fontsize=8)
        ax.grid(alpha=0.25)
        _save(fig, "sp500_ma.png")

    # 2) VIX with long-run average line
    tsv, vix = fetch_yahoo_series("%5EVIX", "1y")
    if vix:
        fig, ax = plt.subplots(figsize=(8, 3.2))
        ax.plot(_dates(tsv), vix, label="VIX", lw=1.3, color="#c0392b")
        ax.axhline(VIX_LONG_RUN_AVG, ls="--", lw=1.0, color="gray",
                   label=f"Long-run avg ≈ {VIX_LONG_RUN_AVG}")
        ax.set_title("VIX vs long-run average")
        ax.legend(loc="best", fontsize=8)
        ax.grid(alpha=0.25)
        _save(fig, "vix_avg.png")

    # 3) Shiller CAPE history with long-run mean line
    cape_hist = fetch_cape_history()
    if len(cape_hist) >= 12:
        try:
            xs = [_dt.datetime.strptime(d, "%b %d, %Y") for d, _ in cape_hist]
        except ValueError:
            xs = list(range(len(cape_hist)))
        ys = [v for _, v in cape_hist]
        fig, ax = plt.subplots(figsize=(8, 3.2))
        ax.plot(xs, ys, label="Shiller CAPE", lw=1.3, color="#8e44ad")
        ax.axhline(CAPE_LONG_RUN_MEAN, ls="--", lw=1.0, color="gray",
                   label=f"Long-run mean ≈ {CAPE_LONG_RUN_MEAN}")
        ax.set_title("Shiller CAPE vs long-run mean")
        ax.legend(loc="best", fontsize=8)
        ax.grid(alpha=0.25)
        _save(fig, "cape_mean.png")

    # 4) Danger score trend from accumulated history CSV
    if history_csv and os.path.exists(history_csv):
        import datetime as _dt2
        pts = []
        for line in open(history_csv, encoding="utf-8").read().splitlines()[1:]:
            parts = line.split(",")
            if len(parts) >= 2:
                try:
                    pts.append((_dt2.datetime.strptime(parts[0], "%Y-%m-%d"), float(parts[1])))
                except ValueError:
                    continue
        if len(pts) >= 2:
            fig, ax = plt.subplots(figsize=(8, 3.2))
            ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", ms=3, lw=1.3)
            for lo, hi, col in [(0, 20, "#2ecc71"), (20, 40, "#a9dfbf"),
                                (40, 60, "#f1c40f"), (60, 80, "#e67e22"), (80, 100, "#e74c3c")]:
                ax.axhspan(lo, hi, color=col, alpha=0.10)
            ax.set_ylim(0, 100)
            ax.set_title("Composite danger score trend")
            ax.grid(alpha=0.25)
            _save(fig, "score_trend.png")

    return written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="US equity drawdown-risk danger score")
    ap.add_argument("--manual", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--report", choices=["text", "md"], default="text")
    ap.add_argument("--alert", action="store_true", help="intraday threshold check (JSON)")
    ap.add_argument("--charts", metavar="DIR", help="write chart PNGs into DIR")
    ap.add_argument("--history", metavar="CSV", help="score-history CSV for the trend chart")
    ap.add_argument("--asof", default="", help="report date label")
    args = ap.parse_args()

    if args.alert:
        print(json.dumps(run_alert(args.asof or "now"), ensure_ascii=False))
        return

    if args.charts:
        names = generate_charts(args.charts, args.history)
        print("charts written:", ", ".join(names) if names else "(none)")
        return

    inp, dates = collect_manual() if args.manual else collect_auto()
    result = compute(inp)
    if args.json:
        print(json.dumps({**result, "dates": dates}, ensure_ascii=False, indent=2))
    elif args.report == "md":
        charts = None
        if args.charts:
            charts = generate_charts(args.charts, args.history)
        print(render_markdown(result, dates, args.asof or "today", charts))
    else:
        print(render_text(result))


if __name__ == "__main__":
    main()
