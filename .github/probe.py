import json, urllib.request
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
H = {"User-Agent": UA,
     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
     "Accept-Language": "en-US,en;q=0.9",
     "Referer": "https://www.cboe.com/",
     "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
     "Sec-Fetch-Site": "same-origin", "Upgrade-Insecure-Requests": "1"}
CANDS = [
 ("CNN .io F&G",     "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"),
 ("CBOE totalpc",    "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/totalpc.csv"),
 ("CBOE equitypc",   "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/equitypc.csv"),
 ("CBOE indexpc",    "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/indexpc.csv"),
 ("CBOE vix csv",    "https://cdn.cboe.com/api/global/us_indices/daily_prices/_VIX_History.csv"),
 ("Nasdaq SPY opt",  "https://api.nasdaq.com/api/quote/SPY/option-chain?assetclass=etf&limit=10"),
 ("AAII sentiment",  "https://www.aaii.com/sentimentsurvey/sent_results"),
]
for name, url in CANDS:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=20) as r:
            body = r.read()
        print(f"[{r.status}] {len(body):>9}B  {name:<16} {body[:200].decode('utf-8','ignore')!r}")
        if name == "CNN .io F&G":
            d = json.loads(body)
            print("     F&G score:", d["fear_and_greed"]["score"], "| rating:", d["fear_and_greed"]["rating"])
            print("     put_call_options:", d.get("put_call_options", {}).get("score"))
    except Exception as e:
        print(f"[ERR]           -  {name:<16} {type(e).__name__}: {e}")
# Yahoo options via cookie+crumb
try:
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.open(urllib.request.Request("https://fc.yahoo.com", headers={"User-Agent": UA}), timeout=15)
except Exception as e:
    print("cookie step:", type(e).__name__, e)
try:
    crumb = op.open(urllib.request.Request(
        "https://query1.finance.yahoo.com/v1/test/getcrumb",
        headers={"User-Agent": UA}), timeout=15).read().decode()
    print("crumb:", repr(crumb[:20]))
    u = f"https://query2.finance.yahoo.com/v7/finance/options/SPY?crumb={crumb}"
    d = json.loads(op.open(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=20).read())
    ch = d["optionChain"]["result"][0]["options"][0]
    cv = sum(c.get("volume") or 0 for c in ch["calls"]); pv = sum(p.get("volume") or 0 for p in ch["puts"])
    print(f"Yahoo SPY nearest expiry: calls={cv} puts={pv} P/C={round(pv/cv,3) if cv else None}")
except Exception as e:
    print("yahoo crumb path:", type(e).__name__, e)
