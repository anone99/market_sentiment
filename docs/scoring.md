# Danger-score specification (S&P 500 / Dow drawdown-risk monitor)

This defines how several indicators are combined into a single 0–100
"danger score". The daily job and `market_risk.py` follow this spec.

> **Disclaimer**: educational heuristic, not investment advice. Thresholds are
> rule-of-thumb values and do not guarantee or predict market behaviour.

## Design

- Single indicators give too many false signals, so **5–6 categories are
  combined with weights**.
- Valuation (PE / CAPE) measures *how* stretched the market is but is a poor
  *timing* tool, so it is weighted low.
- Volatility, credit and breadth tend to move early, so they are weighted
  higher for earlier warning.
- Each indicator is normalised to a 0–100 "danger contribution", then the
  categories are combined as a weighted average. Missing categories are dropped
  and the remaining weights are renormalised to sum to 1.

## Categories and weights

| Category | Weight | Indicators |
|----------|--------|------------|
| Valuation | 15% | Shiller CAPE; forward/trailing PE deviation from long-run mean |
| Trend | 20% | 200-day MA deviation; drawdown from 52-week high; below 200-day MA |
| Volatility | 20% | VIX level; VIX term structure (VIX vs VIX3M); SKEW; VVIX |
| Sentiment | 15% | Put/Call ratio (index-scale, SPY chain); CNN Fear & Greed |
| Credit/Macro | 20% | Yield curve (10Y-2Y, 10Y-3M); HY & IG OAS; NFCI; Sahm rule |
| Breadth | 10% | Equal-weight ÷ cap-weight ratio (RSP/SPY); % of stocks above 200d MA |

`score = Σ(category_danger × weight)`, renormalised over available categories.

## Normalisation

### Valuation (mean of the parts below)
Shiller CAPE (long-run median ≈ 17; record ≈ 44 in Dec 1999):

| CAPE | Danger |
|------|--------|
| < 20 | 0 |
| 20–25 | 20 |
| 25–30 | 40 |
| 30–35 | 60 |
| 35–40 | 80 |
| > 40 | 100 |

Forward-PE deviation from its 10-year mean adds +10 per +10% (capped at 100).

**Excess CAPE Yield (ECY)** — Shiller's rate-aware improvement on CAPE, since CAPE
ignores the interest-rate / inflation regime. `ECY = (1 / CAPE) − real 10y yield`,
where the real 10y yield is FRED `DFII10` (10-year TIPS). Higher ECY = equities
cheaper vs bonds = lower danger:

| ECY (%) | Danger |
|---------|--------|
| ≥ 4 | 10 |
| 3–4 | 25 |
| 2–3 | 45 |
| 1–2 | 65 |
| 0–1 | 80 |
| < 0 | 95 |

When ECY is available it is the **primary** valuation signal (weight 0.6), blended
with the CAPE/PE danger (0.4): `valuation = 0.6·ECY_danger + 0.4·CAPE_PE_danger`.
If ECY is unavailable, valuation falls back to the CAPE/PE mean.

### Trend (max of the following)
- **200-day MA deviation** `dev`: >+12% → 60; +8..12% → 40; −3..+8% → 10;
  −3..−7% → 55; −7..−12% → 75; < −12% → 90.
- **Below 200-day MA**: floor of 70.
- **Drawdown from 52-week high**: <3% → 0; 3–7% → 30; 7–10% → 55;
  10–20% → 75; >20% → 95.

### Volatility
VIX level: <13 → 10; 13–17 → 20; 17–20 → 35; 20–25 → 50; 25–30 → 65;
30–40 → 85; >40 → 100. Then **+25** if VIX > VIX3M (backwardation),
**+5** if SKEW ≥ 150, **+5** if VVIX ≥ 110 (capped at 100).

### Sentiment (mean of the parts)
- **Put/Call, Cboe all-market total** (manual entry only, see below):
  <0.7 → 60; 0.7–0.9 → 25; 0.9–1.1 → 15; 1.1–1.3 → 40; >1.3 → 70.
- **Put/Call, index/ETF scale** (used by the automated run): <0.71 → 60;
  0.71–1.09 → 25; 1.09–1.47 → 15; 1.47–1.84 → 40; >1.84 → 70.
- Fear & Greed: >80 → 70; 60–80 → 45; 40–60 → 20; 20–40 → 45; <20 → 65.

Extreme greed (complacency at tops) and extreme fear (already selling off) both
raise danger.

**Why two put/call scales.** Cboe's own put/call archives
(`totalpc.csv` / `equitypc.csv` / `indexpc.csv`) stopped updating on
**2019-10-04**, and no free source publishes the current all-market total. The
automated run therefore computes the ratio from SPY's nearest-expiry option
chain. Index/ETF options are structurally more put-heavy than the all-market
total (hedging demand), so applying the total-scale thresholds to them would
systematically overstate danger — SPY at 1.38 would score 70 ("fear") when it
is in fact near its own median.

The index-scale cut-points above are the **percentile-matched equivalents** of
the total-scale ones, derived from Cboe's two archives over the 3,253 sessions
they share (2006-11-01 … 2019-10-04):

| Total cut | Percentile in `totalpc` | Same percentile in `indexpc` |
|-----------|-------------------------|------------------------------|
| 0.70 | 2.3rd | 0.71 |
| 0.90 | 39.2nd | 1.09 |
| 1.10 | 83.4th | 1.47 |
| 1.30 | 97.0th | 1.84 |

For reference, medians over that window were 0.93 (total) vs 1.16 (index).
Because the mapping is calibrated on pre-2019 data, it does not account for the
later growth of 0DTE volume — treat the index-scale ratio as the coarser of the
two signals.

### Credit/Macro (max of the following)
- **10Y-2Y** and **10Y-3M** spreads: >0.5 → 10; 0..0.5 → 30; −0.3..0 → 60;
  < −0.3 → 80.
- **HY OAS**: <3.5 → 10; 3.5–4.5 → 30; 4.5–5.5 → 50; 5.5–7 → 70; >7 → 90.
- **IG OAS**: <1.0 → 10; 1.0–1.3 → 30; 1.3–1.7 → 50; 1.7–2.2 → 70; >2.2 → 90.
- **NFCI** (Chicago Fed): <−0.5 → 10; −0.5..0 → 20; 0..0.3 → 45; 0.3..0.7 → 70;
  >0.7 → 90 (positive = tighter than average).
- **Sahm rule** ≥ 0.5 → recession signal, danger ≥ 75.

### Breadth
- **% of stocks above 200d MA**: >60% → 10; 50–60% → 25; 40–50% → 45;
  30–40% → 65; <30% → 85.
- If unavailable, use the **equal-weight ÷ cap-weight ratio** (RSP/SPY)
  deviation from its 200-day mean: >+1% → 10; −1..+1% → 25; −3..−1% → 45;
  −5..−3% → 65; < −5% → 85. A falling ratio = a few megacaps carrying the
  index = narrow, weak breadth.

## Tiers

| Score | Tier |
|-------|------|
| 0–20 | Calm 🟢 |
| 20–40 | Mild caution 🟢🟡 |
| 40–60 | Caution 🟡 |
| 60–80 | Elevated 🟠 |
| 80–100 | Danger 🔴 |

## Data sources

FRED (rates, HY/IG OAS, NFCI, Sahm, VIX), Yahoo Finance (prices, VIX3M, SKEW,
VVIX, RSP, SPY, SPY option chain for put/call), multpl (Shiller CAPE + monthly
history), CNN (Fear & Greed).

Two source quirks the fetchers work around:

- CNN's dataviz host moved from `production.dataviz.cnn.com` (now NXDOMAIN) to
  `production.dataviz.cnn.io`.
- multpl, CNN and Yahoo's options endpoint all reject non-browser clients, so
  requests carry a browser `User-Agent`/`Accept-Language`; the Yahoo options
  endpoint additionally needs a session cookie plus a crumb token.
