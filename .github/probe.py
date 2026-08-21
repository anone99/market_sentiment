import json, urllib.request, urllib.error
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
CANDS = [
 ("CNN F&G",            "https://production.dataviz.cnn.com/index/fearandgreed/graphdata"),
 ("Yahoo opt SPY",      "https://query2.finance.yahoo.com/v7/finance/options/SPY"),
 ("Yahoo opt SPY q1",   "https://query1.finance.yahoo.com/v7/finance/options/SPY"),
 ("CBOE stats page",    "https://www.cboe.com/us/options/market_statistics/daily/"),
 ("CBOE cdn vix csv",   "https://cdn.cboe.com/api/global/us_indices/daily_prices/_VIX_History.csv"),
 ("CBOE mkt-stat json", "https://www.cboe.com/us/options/market_statistics/daily/api/?dt=2026-08-20"),
 ("CBOE hist pc csv",   "https://cdn.cboe.com/data/us/options/market_statistics/historical_data/total_pc.csv"),
 ("stooq spy",          "https://stooq.com/q/d/l/?s=spy.us&i=d"),
 ("multpl by-month",    "https://www.multpl.com/shiller-pe/table/by-month"),
]
for name, url in CANDS:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
        snip = body[:220].decode("utf-8", "ignore").replace("\n", " ")
        print(f"[{r.status}] {len(body):>8}B  {name:<20} {snip}")
        if name == "CNN F&G":
            d = json.loads(body)
            print("    CNN keys:", ", ".join(sorted(d.keys())))
            print("    put_call_options:", json.dumps(d.get("put_call_options", {}).get("score")))
            print("    fear_and_greed:", json.dumps(d.get("fear_and_greed")))
        if name.startswith("Yahoo opt"):
            d = json.loads(body)
            ch = d["optionChain"]["result"][0]["options"][0]
            cv = sum(c.get("volume") or 0 for c in ch["calls"])
            pv = sum(p.get("volume") or 0 for p in ch["puts"])
            print(f"    SPY nearest-expiry vol: calls={cv} puts={pv} P/C={pv/cv if cv else None}")
    except Exception as e:
        print(f"[ERR]           -  {name:<20} {type(e).__name__}: {e}")
