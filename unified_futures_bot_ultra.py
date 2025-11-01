# -*- coding: utf-8 -*-
"""
unified_futures_bot_v24_smarttrade.py
MEXC + Bitget | 24/7 | x5 | сигналы по тренду
/scan | /top | /trade <№> <сумма> | /trade <symbol> <side> <amount>
+ inline-кнопки BUY/EST
"""

import os, asyncio, logging, time
# === АНТИ-ДУБЛЬ ЗАЩИТА ПЕРЕД СТАРТОМ БОТА ===
import requests, sys

def ensure_single_instance(token: str):
    if not token:
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=10)
        data = r.json()
        if data.get("ok") and data.get("result", {}).get("url"):
            print("⚠️ Duplicate instance detected — shutting down.", flush=True)
            logging.error("⚠️ Duplicate instance detected — shutting down.")
            sys.exit(0)
    except Exception as e:
        logging.warning(f"Webhook check failed: {e}")

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
ensure_single_instance(TG_BOT_TOKEN)
# === КОНЕЦ АНТИ-ДУБЛЬ ===

import math
from datetime import datetime
import datetime as dt
from typing import Dict, List, Tuple, Any

import ccxt
import pandas as pd
import numpy as np

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

import nest_asyncio
nest_asyncio.apply()

# ================== ENV МАРКЕТЫ ==================
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")

if not all([TG_BOT_TOKEN, MEXC_API_KEY, BITGET_API_KEY, BITGET_PASSPHRASE]):
    raise SystemExit("Нужно задать TG_BOT_TOKEN, MEXC_*, BITGET_*, BITGET_PASSPHRASE!")

# ================== ПАРАМЕТРЫ ==================
TIMEFRAME, LIMIT = "15m", 300

RSI_PERIOD = 14
RSI_OVERBOUGHT = 82.0
RSI_OVERSOLD = 18.0

EMA_SHORT, EMA_LONG = 50, 200
VOL_SMA = 20
ATR_PERIOD = 14

LEVERAGE = 5
BASE_STOP_LOSS_PCT = 0.05        # 5% под ожидание 20–30 мин
MIN_QUOTE_VOLUME = 5_000_000     # 5M USDT — только живые
SCAN_INTERVAL = 300              # 5 мин
NO_SIGNAL_NOTIFY_INTERVAL = 3600

PARTIAL_TP_RATIO = 0.5
TP1_MULTIPLIER_TREND = 2.0
TP2_MULTIPLIER_TREND = 4.0

# комиссии (чуть с запасом)
TAKER_FEE = 0.0006
MAKER_FEE = 0.0002

# ================== ЛОГИ ==================
os.makedirs("logs", exist_ok=True)
LOG_FILENAME = f"logs/{datetime.now(dt.timezone.utc).date().isoformat()}_v24.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILENAME, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("V24")

# ================== ГЛОБАЛЫ ==================
# chat_id -> list of signals (сокращенная форма по /scan | /top)
LAST_SCAN: Dict[int, List[Tuple]] = {}
# список реально открытых сделок
ACTIVE_TRADES: Dict[int, List[Dict[str, Any]]] = {}
# кеш трендов
H1_TRENDS_CACHE: Dict[str, Tuple[str, float]] = {}
H4_TRENDS_CACHE: Dict[str, Tuple[str, float]] = {}
AUTO_ENABLED = True
LAST_NO_SIGNAL_TIME = 0
# для inline-кнопок: сигнал_id -> полный сигнал
SIGNAL_STORE: Dict[str, Dict[str, Any]] = {}
# чат -> последняя оценка через EST
LAST_EST_AMOUNT: Dict[int, float] = {}

# ================== ВСПОМОГАТЕЛЬНЫЕ ==================
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
        df["h"] - df["l"],
        (df["h"] - df["c"].shift()).abs(),
        (df["l"] - df["c"].shift()).abs()
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

def make_exchange(name: str):
    if name == "mexc":
        return ccxt.mexc({
            "apiKey": MEXC_API_KEY,
            "secret": MEXC_API_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
            "timeout": 30000,
        })
    elif name == "bitget":
        return ccxt.bitget({
            "apiKey": BITGET_API_KEY,
            "secret": BITGET_API_SECRET,
            "password": BITGET_PASSPHRASE,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
            "timeout": 30000,
        })
    else:
        raise ValueError("Unknown exchange")

async def fetch_trend(ex: ccxt.Exchange, symbol: str, tf: str, cache: Dict[str, Tuple[str, float]], ttl: int = 3600) -> str:
    now = time.time()
    key = f"{ex.id}:{symbol}:{tf}"
    if key in cache and now - cache[key][1] < ttl:
        return cache[key][0]
    try:
        ohlcv = await asyncio.to_thread(ex.fetch_ohlcv, symbol, tf, None, 200)
        df = pd.DataFrame(ohlcv, columns=["ts","o","h","l","c","v"])
        e50, e200 = ema(df["c"], 50), ema(df["c"], 200)
        trend = "up" if e50.iloc[-1] > e200.iloc[-1] else "down" if e50.iloc[-1] < e200.iloc[-1] else "flat"
        cache[key] = (trend, now)
        return trend
    except Exception:
        return "flat"

def estimate_time_to_tp(entry: float, tp_price: float, atr_val: float, tf_minutes: int = 15) -> int:
    dist = abs(tp_price - entry)
    if atr_val <= 0:
        return tf_minutes
    candles = max(1, dist / atr_val)
    return int(candles * tf_minutes)

def estimate_net_profit_pct(tp_pct: float) -> float:
    total_fee = TAKER_FEE + MAKER_FEE
    return tp_pct - total_fee

def signal_strength_tag(prob: int) -> str:
    if prob >= 85:
        return "🔥 Сильный"
    elif prob >= 70:
        return "⚡ Хороший"
    elif prob >= 55:
        return "⚠️ Средний"
    else:
        return "❄️ Слабый"

def load_top_usdt_swaps(ex: ccxt.Exchange, top_n=60):
    ex.load_markets()
    tickers = ex.fetch_tickers()
    rows = []
    for s, x in tickers.items():
        m = ex.markets.get(s)
        if not m or m.get("type") != "swap" or m.get("quote") != "USDT":
            continue
        qv = x.get("quoteVolume") or x.get("info", {}).get("quoteVolume") or 0.0
        if qv < MIN_QUOTE_VOLUME:
            continue
        rows.append((s, float(qv)))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s,_ in rows[:top_n]]

async def analyze_symbol(ex: ccxt.Exchange, symbol: str):
    ohlcv = await asyncio.to_thread(ex.fetch_ohlcv, symbol, TIMEFRAME, None, LIMIT)
    if len(ohlcv) < LIMIT // 2:
        return None
    df = pd.DataFrame(ohlcv, columns=["t","o","h","l","c","v"])
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

    h1_trend = await fetch_trend(ex, symbol, "1h", H1_TRENDS_CACHE)
    h4_trend = await fetch_trend(ex, symbol, "4h", H4_TRENDS_CACHE)

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

    trend_ok_long = (lo >= 3 and h1_trend == "up" and h4_trend in ("up","flat"))
    trend_ok_short = (sh >= 3 and h1_trend == "down" and h4_trend in ("down","flat"))

    tp1_pct = max(0.02, TP1_MULTIPLIER_TREND * atr_val / close)
    tp2_pct = max(0.04, TP2_MULTIPLIER_TREND * atr_val / close)
    tp1_price = entry_price * (1 + tp1_pct) if trend_ok_long else entry_price * (1 - tp1_pct)
    tp2_price = entry_price * (1 + tp2_pct) if trend_ok_long else entry_price * (1 - tp2_pct)

    eta_min = estimate_time_to_tp(entry_price, tp1_price, atr_val, 15)

    score = 0
    if trend_ok_long: score += lo
    if trend_ok_short: score += sh
    if volr >= 2.5: score += 1
    if h1_trend == h4_trend and h1_trend != "flat": score += 1
    prob = min(100, 50 + score * 8)

    side = None
    if trend_ok_long:
        side = "long"
    elif trend_ok_short:
        side = "short"
    else:
        return None

    net_tp1_pct = estimate_net_profit_pct(tp1_pct)

    return {
        "exchange": ex.id,
        "symbol": symbol,
        "side": side,
        "rsi": float(r.iloc[-1]),
        "volr": float(volr),
        "score": score,
        "prob": prob,
        "h1": h1_trend,
        "h4": h4_trend,
        "entry": entry_price,
        "sl_pct": sl_pct,
        "tp1_pct": tp1_pct,
        "tp2_pct": tp2_pct,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "eta_min": eta_min,
        "atr": float(atr_val),
        "note": "Near S" if nearS else "Near R" if nearR else "",
        "net_tp1_pct": net_tp1_pct,
    }

async def scan_exchange(name: str):
    ex = make_exchange(name)
    syms = await asyncio.to_thread(load_top_usdt_swaps, ex, 60)
    results = []
    for s in syms:
        try:
            data = await analyze_symbol(ex, s)
            if data:
                results.append(data)
        except Exception as e:
            log.warning(f"{name} {s}: {e}")
        await asyncio.sleep(0.35)
    results.sort(key=lambda x: (x["prob"], x["volr"]), reverse=True)
    return results

async def scan_all():
    mexc_task = asyncio.create_task(scan_exchange("mexc"))
    bitget_task = asyncio.create_task(scan_exchange("bitget"))
    mexc_res, bitget_res = await asyncio.gather(mexc_task, bitget_task)
    return mexc_res + bitget_res

# ================== РАСЧЁТ ПРОФИТА ==================
def estimate_profit_usdt(stake_usdt: float, tp_pct: float) -> float:
    # stake -> позиция с плечом
    position = stake_usdt * LEVERAGE
    gross = position * tp_pct
    fees = position * (TAKER_FEE + MAKER_FEE)
    net = gross - fees
    return max(0.0, net)

def normalize_amount_for_exchange(exchange_id: str, symbol: str, amount: float) -> float:
    if exchange_id == "mexc":
        # mexc часто требует целое кол-во
        return max(1, int(math.ceil(amount)))
    else:
        # bitget, как правило, принимает float
        return float(f"{amount:.6f}")

# ================== ТРЕЙД ==================
def set_leverage_isolated(ex: ccxt.Exchange, symbol: str, lev: int):
    try:
        ex.set_leverage(lev, symbol, params={"marginMode": "isolated", "posMode": "one_way"})
    except Exception as e:
        log.warning(f"set_leverage {symbol}: {e}")

def place_orders(ex: ccxt.Exchange, trade: Dict[str, Any]):
    sym = trade["symbol"]
    side = trade["side"]
    entry = trade["entry"]
    amount = trade["amount"]
    sl_price = trade["sl_price"]
    tp1_price = trade["tp1_price"]
    tp2_price = trade["tp2_price"]

    # вход
    ex.create_market_order(sym, "buy" if side == "long" else "sell", amount)

    if ex.id == "bitget":
        # на bitget не ставим tp/sl, только логируем
        log.info(f"[BITGET] Entry done {sym} {side} amount={amount}. TP1={tp1_price}, TP2={tp2_price}, SL={sl_price} (not placed)")
        return {"tp_sl_placed": False}

    # для MEXC ставим как раньше
    amount1 = amount * PARTIAL_TP_RATIO
    amount2 = amount - amount1
    ex.create_order(sym, "limit", "sell" if side == "long" else "buy", amount1, tp1_price, params={"reduceOnly": True})
    ex.create_order(sym, "limit", "sell" if side == "long" else "buy", amount2, tp2_price, params={"reduceOnly": True})
    ex.create_order(sym, "stop_market", "sell" if side == "long" else "buy", amount, params={"reduceOnly": True, "triggerPrice": sl_price})
    return {"tp_sl_placed": True}

async def execute_trade_from_signal(
    update_or_bot,
    chat_id: int,
    signal: Dict[str, Any],
    stake_usdt: float,
    reason: str = "manual"
):
    ex = make_exchange(signal["exchange"])
    # баланс
    try:
        bal = ex.fetch_balance(params={"type": "swap"})
        free_usdt = bal["USDT"]["free"]
    except Exception as e:
        text = f"[{signal['exchange'].upper()}] Не смог получить баланс: {e}"
        if hasattr(update_or_bot, "send_message"):
            await update_or_bot.send_message(chat_id, text)
        else:
            await update_or_bot.effective_message.reply_text(text)
        return

    if stake_usdt > free_usdt:
        txt = f"Недостаточно средств: нужно {stake_usdt} USDT, доступно {free_usdt:.2f} USDT"
        if hasattr(update_or_bot, "send_message"):
            await update_or_bot.send_message(chat_id, txt)
        else:
            await update_or_bot.effective_message.reply_text(txt)
        return

    entry = signal["entry"]
    side = signal["side"]
    sym = signal["symbol"]
    sl_pct = signal["sl_pct"]
    tp1_pct = signal["tp1_pct"]
    tp2_pct = signal["tp2_pct"]
    tp1_price = signal["tp1_price"]
    tp2_price = signal["tp2_price"]
    eta_min = signal["eta_min"]

    raw_amount = (stake_usdt * LEVERAGE) / entry
    amount = normalize_amount_for_exchange(signal["exchange"], sym, raw_amount)
    if amount <= 0:
        if hasattr(update_or_bot, "send_message"):
            await update_or_bot.send_message(chat_id, "Не удалось рассчитать объём.")
        else:
            await update_or_bot.effective_message.reply_text("Не удалось рассчитать объём.")
        return

    set_leverage_isolated(ex, sym, LEVERAGE)

    if side == "long":
        sl_price = entry * (1 - sl_pct)
    else:
        sl_price = entry * (1 + sl_pct)

    trade = {
        "symbol": sym,
        "side": side,
        "entry": entry,
        "amount": amount,
        "sl_price": sl_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
    }

    try:
        res = place_orders(ex, trade)
    except Exception as e:
        msg = f"[{signal['exchange'].upper()}] Ошибка ордеров: {e}"
        if hasattr(update_or_bot, "send_message"):
            await update_or_bot.send_message(chat_id, msg)
        else:
            await update_or_bot.effective_message.reply_text(msg)
        log.error(e)
        return

    # записываем только если вход реально был
    ACTIVE_TRADES.setdefault(chat_id, []).append({
        "symbol": sym,
        "side": side,
        "entry": entry,
        "amount": amount,
        "exchange": signal["exchange"],
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "sl_price": sl_price,
        "time": datetime.now(dt.timezone.utc),
        "stake": stake_usdt,
        "source": reason,
    })

    net_pct = estimate_net_profit_pct(tp1_pct)
    net_usdt = estimate_profit_usdt(stake_usdt, tp1_pct)

    txt = (
        f"✅ [{signal['exchange'].upper()}] Открыт {side.upper()} {sym}\n"
        f"Сумма: {stake_usdt} USDT (x{LEVERAGE}) → объём {amount}\n"
        f"Entry: {entry:.6f}\n"
        f"SL: {sl_price:.6f} (−{sl_pct*100:.1f}%)\n"
        f"TP1: {tp1_price:.6f} (+{tp1_pct*100:.1f}%)\n"
        f"TP2: {tp2_price:.6f} (+{tp2_pct*100:.1f}%)\n"
        f"⏱ ETA: ~{eta_min} мин\n"
        f"💰 Net: +{net_pct*100:.2f}% ≈ +{net_usdt:.2f} USDT\n"
    )
    if signal["exchange"] == "bitget" and not res.get("tp_sl_placed", False):
        txt += "⚠️ Bitget: TP/SL не поставлены на бирже, фиксация — через бота.\n"

    if hasattr(update_or_bot, "send_message"):
        await update_or_bot.send_message(chat_id, txt)
    else:
        await update_or_bot.effective_message.reply_text(txt)

# ================== INLINE КНОПКИ ==================
def build_signal_keyboard(signal_id: str):
    # две строки: BUY и EST
    row_buy = [
        InlineKeyboardButton("BUY 10", callback_data=f"BUY|{signal_id}|10"),
        InlineKeyboardButton("BUY 20", callback_data=f"BUY|{signal_id}|20"),
        InlineKeyboardButton("BUY 50", callback_data=f"BUY|{signal_id}|50"),
        InlineKeyboardButton("BUY 100", callback_data=f"BUY|{signal_id}|100"),
    ]
    row_est = [
        InlineKeyboardButton("EST 10", callback_data=f"EST|{signal_id}|10"),
        InlineKeyboardButton("EST 20", callback_data=f"EST|{signal_id}|20"),
        InlineKeyboardButton("EST 50", callback_data=f"EST|{signal_id}|50"),
        InlineKeyboardButton("EST 100", callback_data=f"EST|{signal_id}|100"),
    ]
    return InlineKeyboardMarkup([row_buy, row_est])

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # e.g. "BUY|<id>|10"
    parts = data.split("|")
    if len(parts) != 3:
        return
    action, signal_id, amt_str = parts
    chat_id = query.message.chat_id
    amount_usdt = float(amt_str)

    signal = SIGNAL_STORE.get(signal_id)
    if not signal:
        await query.edit_message_text("Сигнал устарел. Сделай /scan.")
        return

    if action == "EST":
        # расчёт прибыли
        tp1_pct = signal["tp1_pct"]
        net_usdt = estimate_profit_usdt(amount_usdt, tp1_pct)
        txt = (
            f"📈 Оценка {signal['symbol']} {signal['side'].upper()} на {amount_usdt} USDT:\n"
            f"TP1: +{tp1_pct*100:.2f}% → ≈ +{net_usdt:.2f} USDT (с комиссией)\n"
            f"ETA: {signal['eta_min']} мин\n"
        )
        LAST_EST_AMOUNT[chat_id] = amount_usdt
        await query.message.reply_text(txt)
        return

    if action == "BUY":
        # если до этого был EST — в принципе не важно, что жали раньше, как ты и сказал
        await execute_trade_from_signal(context.bot, chat_id, signal, amount_usdt, reason="inline")
        return

# ================== TELEGRAM КОМАНДЫ ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*UNIFIED FUTURES BOT v24 SMART-TRADE*\n\n"
        "⚙️ Параметры:\n"
        f"• TF: {TIMEFRAME}\n"
        f"• Автоскан: {SCAN_INTERVAL//60} мин\n"
        f"• Мин. объём: {MIN_QUOTE_VOLUME/1_000_000:.1f}M USDT\n"
        f"• RSI OB/OS: {RSI_OVERBOUGHT}/{RSI_OVERSOLD}\n"
        f"• EMA: {EMA_SHORT}/{EMA_LONG}\n"
        f"• SL (base): {BASE_STOP_LOSS_PCT*100:.1f}%\n"
        f"• Плечо: x{LEVERAGE}\n"
        f"• Комиссия: taker {TAKER_FEE*100:.2f}%, maker {MAKER_FEE*100:.2f}%\n\n"
        "📋 Команды:\n"
        "/scan — найти сигналы\n"
        "/top — топ-3 сильных\n"
        "/trade <№> <сумма> — войти по сигналу из /scan\n"
        "/trade <SYM> <side> <сумма> — войти принудительно (пример: /trade ZEN/USDT long 20)\n"
        "/report — активные сделки\n"
        "/stop — выключить авто\n"
    )
    await update.effective_message.reply_text(text, parse_mode="Markdown")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.effective_message.reply_text("Сканирую MEXC + Bitget...")
    entries = await scan_all()
    if not entries:
        await update.effective_message.reply_text("Сигналов нет.")
        LAST_SCAN[chat_id] = []
        return

    LAST_SCAN[chat_id] = []
    SIGNAL_STORE.clear()
    for i, d in enumerate(entries, 1):
        signal_id = f"{chat_id}:{i}:{int(time.time())}"
        SIGNAL_STORE[signal_id] = d
        LAST_SCAN[chat_id].append((
            d["symbol"], d["side"], d["exchange"],
            d["entry"], d["sl_pct"], d["tp1_pct"], d["tp2_pct"],
            d["tp1_price"], d["tp2_price"], d["eta_min"], d["prob"], d["volr"], d["rsi"]
        ))
        tag = signal_strength_tag(d["prob"])
        text = (
            f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} — {tag} ({d['prob']}%)\n"
            f"RSI={d['rsi']:.1f} | vol×={d['volr']:.2f} | H1={d['h1']} H4={d['h4']}\n"
            f"Entry≈{d['entry']:.6f} | SL=−{d['sl_pct']*100:.1f}% | TP1=+{d['tp1_pct']*100:.1f}% | ETA {d['eta_min']} мин\n"
        )
        await update.effective_message.reply_text(text, reply_markup=build_signal_keyboard(signal_id))
        if i >= 15:
            break

async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    entries = await scan_all()
    if not entries:
        await update.effective_message.reply_text("Сигналов нет.")
        return
    strong = [d for d in entries if d["prob"] >= 80]
    if not strong:
        strong = entries[:3]
    LAST_SCAN[chat_id] = []
    SIGNAL_STORE.clear()
    for i, d in enumerate(strong[:3], 1):
        signal_id = f"{chat_id}:{i}:{int(time.time())}"
        SIGNAL_STORE[signal_id] = d
        LAST_SCAN[chat_id].append((
            d["symbol"], d["side"], d["exchange"],
            d["entry"], d["sl_pct"], d["tp1_pct"], d["tp2_pct"],
            d["tp1_price"], d["tp2_price"], d["eta_min"], d["prob"], d["volr"], d["rsi"]
        ))
        tag = signal_strength_tag(d["prob"])
        text = (
            f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} — {tag} ({d['prob']}%)\n"
            f"Entry≈{d['entry']:.6f} | SL=−{d['sl_pct']*100:.1f}% | TP1=+{d['tp1_pct']*100:.1f}% | ETA {d['eta_min']} мин\n"
        )
        await update.effective_message.reply_text(text, reply_markup=build_signal_keyboard(signal_id))

async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = update.effective_message

    # два формата:
    # 1) /trade <№> <сумма>
    # 2) /trade <symbol> <side> <amount>
    if not context.args:
        await m.reply_text("Формат: /trade <№> <сумма> или /trade <symbol> <side> <amount>")
        return

    # формат 2
    if len(context.args) >= 3 and "/" in context.args[0]:
        symbol = context.args[0].upper()
        side = context.args[1].lower()
        try:
            stake = float(context.args[2])
        except ValueError:
            await m.reply_text("Сумма должна быть числом.")
            return

        # надо подтащить последнюю аналитику по этому символу
        # пробуем найти в SIGNAL_STORE
        sig = None
        for s in SIGNAL_STORE.values():
            if s["symbol"].upper() == symbol and s["side"] == side:
                sig = s
                break
        if not sig:
            # если нет — быстро проанализируем только этот символ на обеих биржах
            found = None
            for ex_name in ("mexc", "bitget"):
                ex = make_exchange(ex_name)
                try:
                    d = await analyze_symbol(ex, symbol)
                    if d and d["side"] == side:
                        found = d
                        break
                except Exception as e:
                    log.warning(f"analyze single {ex_name} {symbol}: {e}")
            if not found:
                await m.reply_text("Не найден свежий сигнал по этому символу.")
                return
            sig = found

        await execute_trade_from_signal(update, chat_id, sig, stake, reason="manual-symbol")
        return

    # формат 1
    if len(context.args) < 2:
        await m.reply_text("Формат: /trade <№> <сумма>")
        return
    try:
        idx = int(context.args[0]) - 1
        stake = float(context.args[1])
    except ValueError:
        await m.reply_text("Номер и сумма должны быть числами.")
        return

    rows = LAST_SCAN.get(chat_id, [])
    if not rows or idx < 0 or idx >= len(rows):
        await m.reply_text("Нет такого номера сигнала. Сделай /scan.")
        return

    sym, side, exchange, entry, sl_pct, tp1_pct, tp2_pct, tp1_price, tp2_price, eta_min, prob, volr, rsi_val = rows[idx]
    sig = {
        "exchange": exchange,
        "symbol": sym,
        "side": side,
        "entry": entry,
        "sl_pct": sl_pct,
        "tp1_pct": tp1_pct,
        "tp2_pct": tp2_pct,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "eta_min": eta_min,
        "prob": prob,
        "volr": volr,
        "rsi": rsi_val,
    }
    await execute_trade_from_signal(update, chat_id, sig, stake, reason="manual-index")

async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    trades = ACTIVE_TRADES.get(chat_id, [])
    if not trades:
        await update.effective_message.reply_text("Нет активных сделок.")
        return
    lines = ["Активные сделки:"]
    for i, t in enumerate(trades, 1):
        dt_open = t["time"].strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"{i}. [{t['exchange'].upper()}] {t['side'].upper()} {t['symbol']} @ {t['entry']:.6f} | "
            f"SL {t['sl_price']:.6f} | TP1 {t['tp1_price']:.6f} | {dt_open}"
        )
    await update.effective_message.reply_text("\n".join(lines))

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AUTO_ENABLED
    AUTO_ENABLED = False
    await update.effective_message.reply_text("Автоскан отключён.")

# ================== ФОН ==================
async def auto_scan_loop(app):
    global LAST_NO_SIGNAL_TIME
    while True:
        if AUTO_ENABLED:
            try:
                entries = await scan_all()
                now = time.time()
                if entries:
                    LAST_NO_SIGNAL_TIME = now
                    # рассылаем только тем, кто уже что-то делал
                    for chat_id in LAST_SCAN.keys():
                        text_lines = [f"📊 Автосигналы {datetime.utcnow().strftime('%H:%M:%S')} UTC:"]
                        for i, d in enumerate(entries[:5], 1):
                            tag = signal_strength_tag(d["prob"])
                            text_lines.append(
                                f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} {tag} ETA {d['eta_min']}м"
                            )
                        await app.bot.send_message(chat_id, "\n".join(text_lines))
                else:
                    if now - LAST_NO_SIGNAL_TIME >= NO_SIGNAL_NOTIFY_INTERVAL:
                        for chat_id in LAST_SCAN.keys():
                            await app.bot.send_message(chat_id, "Сигналов нет.")
                        LAST_NO_SIGNAL_TIME = now
                log.info("auto_scan tick OK")
            except Exception as e:
                log.error(f"auto_scan_loop: {e}")
        await asyncio.sleep(SCAN_INTERVAL)

# ================== MAIN ==================
async def main():
    print("🚀 MAIN INIT START", flush=True)
    app = Application.builder().token(TG_BOT_TOKEN).build()
    print("✅ Application initialized", flush=True)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("trade", trade_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))

    log.info("UNIFIED FUTURES BOT v24 SMART-TRADE STARTED")
    print("BOT ЗАПУЩЕН НА RENDER.COM | 24/7", flush=True)

    asyncio.create_task(auto_scan_loop(app))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
