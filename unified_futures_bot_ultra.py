# -*- coding: utf-8 -*-
"""
unified_futures_bot_ultra_v22.py — RENDER.COM УЛУЧШЕННАЯ ВЕРСИЯ
MEXC + Bitget | 24/7 | x5 | RSI/EMA/ATR | Только по тренду | Быстрые сигналы (до 30 мин)
"""

import os
import asyncio
import logging
import time
from datetime import datetime
import datetime as dt
from typing import Dict, List, Tuple

import ccxt
import pandas as pd
import numpy as np
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
)

# ================== ENV VARS (RENDER) ==================
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = int(os.getenv("TG_CHAT_ID", "0") or "0")
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")

if not all([TG_BOT_TOKEN, MEXC_API_KEY, BITGET_API_KEY, BITGET_PASSPHRASE]):
    raise SystemExit("ОШИБКА: Добавьте TG_BOT_TOKEN, MEXC_*, BITGET_*, BITGET_PASSPHRASE в Environment Variables!")

# ================== НАСТРОЙКИ ==================
TIMEFRAME = "15m"
LIMIT = 300
RSI_PERIOD = 14
RSI_OVERBOUGHT = 82.0   # усиленный фильтр
RSI_OVERSOLD = 18.0
EMA_SHORT, EMA_LONG, VOL_SMA = 50, 200, 20
ATR_PERIOD = 14
LEVERAGE = 5
BASE_STOP_LOSS_PCT = 0.05   # шире для коротких сделок (20–30 мин)
MIN_QUOTE_VOLUME = 5_000_000  # отсекаем "мертвые" пары
SCAN_INTERVAL = 300
MONITOR_INTERVAL = 30
NO_SIGNAL_NOTIFY_INTERVAL = 3600

PARTIAL_TP_RATIO = 0.5
TP1_MULTIPLIER_TREND = 2.0
TP2_MULTIPLIER_TREND = 4.0
TP1_MULTIPLIER_FLAT = 1.0
TP2_MULTIPLIER_FLAT = 2.0

TRAILING_ACTIVATION_PCT = 0.015
TRAILING_DISTANCE_PCT = 0.015

# ================== ЛОГИ ==================
os.makedirs("logs", exist_ok=True)
LOG_FILENAME = f"logs/{datetime.now(dt.timezone.utc).date().isoformat()}_v22.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILENAME, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("UNIFIED-V22")

# ================== ГЛОБАЛЬНЫЕ СТРУКТУРЫ ==================
LAST_SCAN: Dict[int, List[Tuple]] = {}
ACTIVE_TRADES: Dict[int, List[Dict]] = {}
AUTO_ENABLED = True
H1_TRENDS_CACHE: Dict[str, Tuple[str, float]] = {}
LAST_NO_SIGNAL_TIME = 0

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
def ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def rsi(s: pd.Series, p: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).ewm(span=p, adjust=False).mean()
    l = -d.clip(upper=0).ewm(span=p, adjust=False).mean()
    rs = g / (l + 1e-12)
    return 100 - 100 / (1 + rs)

def atr(df: pd.DataFrame, p: int = 14) -> pd.Series:
    tr = pd.concat([
        df['h'] - df['l'],
        (df['h'] - df['c'].shift()).abs(),
        (df['l'] - df['c'].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()

def find_pivots(series: pd.Series, left=2, right=2, mode="high"):
    piv = []
    for i in range(left, len(series) - right):
        v = series.iloc[i]
        if mode == "high" and all(v > series.iloc[i - j - 1] for j in range(left)) and all(v > series.iloc[i + j + 1] for j in range(right)):
            piv.append(i)
        if mode == "low" and all(v < series.iloc[i - j - 1] for j in range(left)) and all(v < series.iloc[i + j + 1] for j in range(right)):
            piv.append(i)
    return piv

def detect_sr_levels(df, tol_factor=1.0, min_touch=3, left=2):
    h, l, close = df["h"].values, df["l"].values, float(df["c"].iloc[-1])
    atr_val = atr(df, ATR_PERIOD).iloc[-1]
    tol = tol_factor * (atr_val / close) if close > 0 else 0.003
    ph = find_pivots(df["h"], left, left, "high")
    pl = find_pivots(df["l"], left, left, "low")
    res_levels = [(h[i], np.sum(np.abs(h - h[i]) / h[i] < tol)) for i in ph]
    sup_levels = [(l[i], np.sum(np.abs(l - l[i]) / l[i] < tol)) for i in pl]
    res = max((x for x, cnt in res_levels if cnt >= min_touch), default=0)
    sup = min((x for x, cnt in sup_levels if cnt >= min_touch), default=0)
    nearR = abs(close - res) / res < tol if res else False
    nearS = abs(close - sup) / sup < tol if sup else False
    return res, nearR, sup, nearS

def make_exchange(exchange_name):
    if exchange_name == "mexc":
        return ccxt.mexc({
            "apiKey": MEXC_API_KEY,
            "secret": MEXC_API_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
            "timeout": 30000,
        })
    elif exchange_name == "bitget":
        return ccxt.bitget({
            "apiKey": BITGET_API_KEY,
            "secret": BITGET_API_SECRET,
            "password": BITGET_PASSPHRASE,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
            "timeout": 30000,
        })

def set_leverage_isolated(ex: ccxt.Exchange, symbol: str, lev: int):
    try:
        ex.set_leverage(lev, symbol, params={"marginMode": "isolated", "posMode": "one_way"})
    except Exception as e:
        log.warning(e)

async def fetch_h1_trend(ex: ccxt.Exchange, symbol: str) -> str:
    now = time.time()
    cache_key = ex.id + symbol
    if cache_key in H1_TRENDS_CACHE and now - H1_TRENDS_CACHE[cache_key][1] < 3600:
        return H1_TRENDS_CACHE[cache_key][0]
    try:
        ohlcv = await asyncio.to_thread(ex.fetch_ohlcv, symbol, "1h", None, 200)
        df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
        e50, e200 = ema(df["c"], 50), ema(df["c"], 200)
        trend = "up" if e50.iloc[-1] > e200.iloc[-1] else "down" if e50.iloc[-1] < e200.iloc[-1] else "flat"
        H1_TRENDS_CACHE[cache_key] = (trend, now)
        return trend
    except Exception:
        return "flat"

# ================== СКАНЕР ==================
async def process_symbol_with_retry(ex, s, max_retries=3):
    for attempt in range(max_retries):
        try:
            ohlcv = await asyncio.to_thread(ex.fetch_ohlcv, s, TIMEFRAME, None, LIMIT)
            if len(ohlcv) < LIMIT // 2:
                return []

            df = pd.DataFrame(ohlcv, columns=["t", "o", "h", "l", "c", "v"])
            c, v = df["c"], df["v"]
            r = rsi(c, RSI_PERIOD)
            e50, e200 = ema(c, EMA_SHORT), ema(c, EMA_LONG)
            vma = v.rolling(VOL_SMA).mean()
            volr = v.iloc[-1] / (vma.iloc[-1] + 1e-12) if vma.iloc[-1] > 0 else 0
            atr_val = atr(df, ATR_PERIOD).iloc[-1]
            _, nearR, _, nearS = detect_sr_levels(df)

            open_, close = float(df["o"].iloc[-1]), float(df["c"].iloc[-1])
            bull = close > open_ * 1.003
            bear = close < open_ * 0.997
            trend = await fetch_h1_trend(ex, s)

            sh, lo = 0, 0
            if r.iloc[-1] >= RSI_OVERBOUGHT: sh += 1
            if e50.iloc[-1] < e200.iloc[-1] and c.iloc[-1] < e50.iloc[-1]: sh += 1
            if volr >= 2.0: sh += 1
            if nearR: sh += 1
            if bear: sh += 1

            if r.iloc[-1] <= RSI_OVERSOLD: lo += 1
            if e50.iloc[-1] > e200.iloc[-1] and c.iloc[-1] > e50.iloc[-1]: lo += 1
            if volr >= 2.0: lo += 1
            if nearS: lo += 1
            if bull: lo += 1

            entry_price = close
            sl_pct = max(BASE_STOP_LOSS_PCT, 1.5 * atr_val / close)

            tp1_pct = max(0.02, TP1_MULTIPLIER_TREND * atr_val / close)
            tp2_pct = max(0.04, TP2_MULTIPLIER_TREND * atr_val / close)

            entries = []
            if lo >= 3 and trend == "up":
                entries.append(("long", s, float(r.iloc[-1]), float(volr), lo, "Near Support" if nearS else "", ex.id, trend, entry_price, sl_pct, tp1_pct, tp2_pct))
            if sh >= 3 and trend == "down":
                entries.append(("short", s, float(r.iloc[-1]), float(volr), sh, "Near Resistance" if nearR else "", ex.id, trend, entry_price, sl_pct, tp1_pct, tp2_pct))

            return entries
        except Exception as e:
            if "510" in str(e) or "频率" in str(e):
                await asyncio.sleep(2 ** attempt)
            else:
                log.warning(f"{s}: {e}")
                return []
    return []

# ================== AUTO SCAN ==================
async def auto_scan_loop(app):
    global LAST_NO_SIGNAL_TIME
    while True:
        if AUTO_ENABLED:
            try:
                entries = []
                for exch in ("mexc", "bitget"):
                    ex = make_exchange(exch)
                    syms = await asyncio.to_thread(lambda: [s for s, _ in ex.fetch_tickers().items()])
                    for s in syms[:50]:
                        entries.extend(await process_symbol_with_retry(ex, s))
                        await asyncio.sleep(0.5)

                now = time.time()
                if entries:
                    LAST_NO_SIGNAL_TIME = now
                    msg = "\n".join(f"{i}. [{ex.upper()}] {sd.upper()} {sm} (score {sc})" for i, (sd, sm, _, _, sc, _, ex, _, _, _, _, _) in enumerate(entries, 1))
                    for chat_id in LAST_SCAN.keys():
                        await app.bot.send_message(chat_id, f"Сигналы:\n{msg}")
                else:
                    if now - LAST_NO_SIGNAL_TIME >= NO_SIGNAL_NOTIFY_INTERVAL:
                        for chat_id in LAST_SCAN.keys():
                            await app.bot.send_message(chat_id, "Сигналов нет.")
                        LAST_NO_SIGNAL_TIME = now

                # поддержка активности для Render (пинг в лог)
                log.info("auto_scan tick OK")

            except Exception as e:
                log.error(f"Auto-scan error: {e}")
        await asyncio.sleep(SCAN_INTERVAL)

# ================== MAIN ==================
async def main():
    app = ApplicationBuilder().token(TG_BOT_TOKEN).concurrent_updates(True).build()
    log.info("UNIFIED V22 STARTED")
    print("BOT ЗАПУЩЕН НА RENDER.COM | 24/7")

    asyncio.create_task(auto_scan_loop(app))
    await app.run_polling()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())


