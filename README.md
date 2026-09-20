# CRYPTO SCALP BOT V5.2 — STRUCTURAL SL

Based on V5.1 POI/LIQUIDITY. Detection logic is unchanged.

Changes in V5.2:
- Structural SL uses the real invalidation area:
  - LONG: below the lowest of sweep low / POI low / IMB low, with buffer.
  - SHORT: above the highest of sweep high / POI high / IMB high, with buffer.
- Signal is skipped when structural SL risk exceeds MAX_RISK_PCT (default 2.50%).

Preserved:
- TP1 0.50%
- TP2 0.70%
- 30x default leverage
- 5m context
- closed 1m candles
- SSL/BSL sweep -> POI -> reaction -> displacement -> CHoCH/BOS -> IMB
- anti-chase 0.25%
- confirmed Telegram messages only

Important: wider structural SL is not a reason to increase capital risk. Position size should be adjusted separately.
