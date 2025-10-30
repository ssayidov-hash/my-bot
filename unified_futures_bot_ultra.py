# -*- coding: utf-8 -*-
"""
unified_futures_bot_ultra.py — RENDER.COM READY
MEXC + Bitget | 24/7 | Без ключей в коде
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
from ccxt.base.errors import AuthenticationError, InvalidOrder
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
)

# ================== КЛЮЧИ ИЗ ENV VARS (RENDER) ==================
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = int(os.getenv("TG_CHAT_ID", "0") or "0")
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")

# Проверка ключей
if not all([TG_BOT_TOKEN, MEXC_API_KEY, BITGET_API_KEY]):
    raise SystemExit("ОШИБКА: Добавьте TG_BOT_TOKEN, MEXC_API_KEY, BITGET_API_KEY в Environment Variables!")

# ================== НАСТРОЙКИ ==================
TIMEFRAME = "15m"
LIMIT = 300
RSI_PERIOD = 14
RSI_OVERBOUGHT = 80.0
RSI_OVERSOLD = 20.0
EMA_SHORT, EMA_LONG, VOL_SMA = 50, 200, 20
ATR_PERIOD = 14
LEVERAGE = 5
BASE_STOP_LOSS_PCT = 0.03
MIN_QUOTE_VOLUME = 1_000_000
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
if not os.path.exists("logs"):
    os.makedirs("logs")
LOG_FILENAME = f"logs/{datetime.now(dt.timezone.utc).date().isoformat()}_unified.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILENAME, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("UNIFIED-ULTRA")

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

def score_label(sc: int) -> str:
    return {0:"Обычный",1:"Обычный",2:"Нормальный",3:"Хороший"}.get(sc,"Отличный")

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
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
            "timeout": 30000,
        })

def load_top_usdt_swaps(ex: ccxt.Exchange, exchange_name: str, top_n=50):
    ex.load_markets()
    t = ex.fetch_tickers()
    rows = []
    for s, x in t.items():
        m = ex.markets.get(s)
        if not m or m.get("type") != "swap" or m.get("quote") != "USDT":
            continue
        qv = x.get("quoteVolume") or x.get("info", {}).get("quoteVolume") or 0.0
        if qv < MIN_QUOTE_VOLUME:
            continue
        rows.append((s, float(qv)))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:top_n]]

async def fetch_h1_trend(ex: ccxt.Exchange, symbol: str) -> str:
    now = time.time()
    cache_key = ex.id + symbol
    if cache_key in H1_TRENDS_CACHE and now - H1_TRENDS_CACHE[cache_key][1] < 3600:
        return H1_TRENDS_CACHE[cache_key][0]
    try:
        ohlcv = await asyncio.to_thread(ex.fetch_ohlcv, symbol, "1h", None, 200)
        df = pd.DataFrame(ohlcv, columns=["ts","o","h","l","c","v"])
        c = df["c"]
        e50, e200 = ema(c, 50), ema(c, 200)
        trend = "up" if e50.iloc[-1] > e200.iloc[-1] else "down" if e50.iloc[-1] < e200.iloc[-1] else "flat"
        H1_TRENDS_CACHE[cache_key] = (trend, now)
        return trend
    except Exception:
        return "flat"

def set_leverage_isolated(ex: ccxt.Exchange, symbol: str, lev: int):
    try:
        ex.set_leverage(lev, symbol, params={"marginMode":"isolated","posMode":"one_way"})
    except Exception as e:
        log.warning(e)

# ================== СКАНЕР ==================
async def scan_exchange(exchange_name: str):
    ex = make_exchange(exchange_name)
    syms = await asyncio.to_thread(load_top_usdt_swaps, ex, exchange_name, 50)
    entries = []
    for s in syms:
        try:
            result = await process_symbol_with_retry(ex, s)
            entries.extend(result)
        except Exception as e:
            log.warning(f"{exchange_name} {s}: {e}")
        await asyncio.sleep(0.6)
    entries.sort(key=lambda x: x[4], reverse=True)
    return entries

async def scan_all():
    mexc_task = asyncio.create_task(scan_exchange("mexc"))
    bitget_task = asyncio.create_task(scan_exchange("bitget"))
    mexc_entries = await mexc_task
    bitget_entries = await bitget_task
    return mexc_entries + bitget_entries

async def process_symbol_with_retry(ex, s, max_retries=3):
    for attempt in range(max_retries):
        try:
            ohlcv = await asyncio.to_thread(ex.fetch_ohlcv, s, TIMEFRAME, None, LIMIT)
            if len(ohlcv) < LIMIT // 2:
                return []
            df = pd.DataFrame(ohlcv, columns=["t","o","h","l","c","v"])
            c, v = df["c"], df["v"]
            r = rsi(c, RSI_PERIOD)
            e50, e200 = ema(c, EMA_SHORT), ema(c, EMA_LONG)
            vma = v.rolling(VOL_SMA).mean()
            volr = v.iloc[-1] / (vma.iloc[-1] + 1e-12) if vma.iloc[-1] > 0 else 0
            atr_val = atr(df, ATR_PERIOD).iloc[-1]
            _, nearR, _, nearS = detect_sr_levels(df)
            open_, close = float(df["o"].iloc[-1]), float(df["c"].iloc[-1])
            bull = close > open_ * 1.002
            bear = close < open_ * 0.998
            trend = await fetch_h1_trend(ex, s)

            sh, lo = 0, 0
            if r.iloc[-1] >= RSI_OVERBOUGHT: sh += 1
            if e50.iloc[-1] < e200.iloc[-1] and c.iloc[-1] < e50.iloc[-1]: sh += 1
            if volr >= 1.5: sh += 1
            if nearR: sh += 1
            if bear: sh += 1

            if r.iloc[-1] <= RSI_OVERSOLD: lo += 1
            if e50.iloc[-1] > e200.iloc[-1] and c.iloc[-1] > e50.iloc[-1]: lo += 1
            if volr >= 1.5: lo += 1
            if nearS: lo += 1
            if bull: lo += 1

            entry_price = close
            sl_pct = max(BASE_STOP_LOSS_PCT, 1.5 * atr_val / close)

            if trend in ("up", "down"):
                tp1_pct = max(0.02, TP1_MULTIPLIER_TREND * atr_val / close)
                tp2_pct = max(0.04, TP2_MULTIPLIER_TREND * atr_val / close)
            else:
                tp1_pct = max(0.015, TP1_MULTIPLIER_FLAT * atr_val / close)
                tp2_pct = max(0.03, TP2_MULTIPLIER_FLAT * atr_val / close)

            entries = []
            if lo >= 3 and trend in ("up", "flat"):
                entries.append(("long", s, float(r.iloc[-1]), float(volr), lo,
                                "Near Support" if nearS else "", ex.id, trend,
                                entry_price, sl_pct, tp1_pct, tp2_pct))
            if sh >= 3 and trend in ("down", "flat"):
                entries.append(("short", s, float(r.iloc[-1]), float(volr), sh,
                                "Near Resistance" if nearR else "", ex.id, trend,
                                entry_price, sl_pct, tp1_pct, tp2_pct))
            return entries
        except Exception as e:
            if "510" in str(e) or "频率" in str(e):
                wait = 2 ** attempt
                log.warning(f"Rate limit {s}, retry {attempt+1}/{max_retries} after {wait}s")
                await asyncio.sleep(wait)
            else:
                log.warning(f"{s}: {e}")
                return []
    return []

# ================== ТРЕЙЛИНГ И КОМАНДЫ (как раньше) ==================
# ... (весь остальной код из предыдущей версии, только с [exchange.upper()])

# ================== MAIN ==================
async def main():
    app = ApplicationBuilder().token(TG_BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("trade", trade))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("auto", auto_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))

    log.info("UNIFIED ULTRA BOT STARTED ON RENDER")
    print("BOT ЗАПУЩЕН НА RENDER.COM | 24/7")

    asyncio.create_task(auto_scan_loop(app))
    asyncio.create_task(trailing_monitor(app))
    await app.run_polling()

if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()

    asyncio.get_event_loop().run_until_complete(main())
