# -*- coding: utf-8 -*-
"""
unified_futures_bot_ultra2.5.0_analytics.py
БАЗА: unified_futures_bot_ultra2.4.2 (твоя текущая)
ДОБАВЛЕНО:
- лог всех сделок в /mnt/data/trades_history.csv
- запись открытия и закрытия (TP1/TP2/SL/Not placed)
- /stats — показать win-rate и средний PnL
- /history — прислать CSV
Остальное НЕ трогал.
"""

import os, asyncio, logging, time
import requests, sys
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
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

import nest_asyncio
nest_asyncio.apply()

# ============ ПУТИ ============
DATA_DIR = "/mnt/data"
os.makedirs(DATA_DIR, exist_ok=True)
TRADES_CSV_PATH = os.path.join(DATA_DIR, "trades_history.csv")

# ============ АНТИ-ДУБЛЬ ============
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
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
ensure_single_instance(TG_BOT_TOKEN)

# ============ API KEYS ============
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")

if not all([TG_BOT_TOKEN, MEXC_API_KEY, BITGET_API_KEY, BITGET_PASSPHRASE]):
    raise SystemExit("Нужно задать TG_BOT_TOKEN, MEXC_*, BITGET_*, BITGET_PASSPHRASE!")

# ============ ПАРАМЕТРЫ ============
TIMEFRAME, LIMIT = "15m", 300

RSI_PERIOD = 14
RSI_OVERBOUGHT = 82.0
RSI_OVERSOLD = 18.0

EMA_SHORT, EMA_LONG = 50, 200
VOL_SMA = 20
ATR_PERIOD = 14

LEVERAGE = 5
BASE_STOP_LOSS_PCT = 0.05
MIN_QUOTE_VOLUME = 5_000_000
SCAN_INTERVAL = 300
NO_SIGNAL_NOTIFY_INTERVAL = 3600

PARTIAL_TP_RATIO = 0.5
TP1_MULTIPLIER_TREND = 2.0
TP2_MULTIPLIER_TREND = 4.0

TRAILING_PCT_MULTIPLIER = 1.5
TRAILING_ACTIVATION_MULTIPLIER = 1.2
TRAILING_MIN_PROB = 80

TAKER_FEE = 0.0006
MAKER_FEE = 0.0002

SIGNAL_TTL = 1800  # 30 min

# ============ ЛОГИ ============
os.makedirs("logs", exist_ok=True)
LOG_FILENAME = f"logs/{datetime.now(dt.timezone.utc).date().isoformat()}_v25.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILENAME, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("V25")

# ============ ГЛОБАЛЫ ============
LAST_SCAN: Dict[int, List[Tuple]] = {}
ACTIVE_TRADES: Dict[int, List[Dict[str, Any]]] = {}
H1_TRENDS_CACHE: Dict[str, Tuple[str, float]] = {}
H4_TRENDS_CACHE: Dict[str, Tuple[str, float]] = {}
AUTO_ENABLED = True
LAST_NO_SIGNAL_TIME = 0

# сигнал_id -> {signal, timestamp}
SIGNAL_STORE: Dict[str, Dict[str, Any]] = {}
LAST_EST_AMOUNT: Dict[int, float] = {}
PENDING_CONFIRMS: Dict[str, Tuple[str, float]] = {}

# ============ УТИЛЫ ============
def ensure_trades_csv():
    """Создаёт CSV, если его нет."""
    if not os.path.exists(TRADES_CSV_PATH):
        import csv
        with open(TRADES_CSV_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "timestamp_utc",
                "chat_id",
                "exchange",
                "symbol",
                "side",
                "entry",
                "sl",
                "tp1",
                "tp2",
                "stake_usdt",
                "leverage",
                "result",           # OPEN / TP1 / TP2 / SL / CLOSED / FAIL
                "profit_pct",
                "profit_usdt",
                "source",           # manual-index / manual-symbol / inline / auto
            ])

def append_trade_row(
    chat_id: int,
    exchange: str,
    symbol: str,
    side: str,
    entry: float,
    sl: float,
    tp1: float,
    tp2: float,
    stake_usdt: float,
    result: str,
    profit_pct: float,
    profit_usdt: float,
    source: str,
):
    ensure_trades_csv()
    import csv
    with open(TRADES_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            datetime.utcnow().isoformat(),
            chat_id,
            exchange,
            symbol,
            side,
            f"{entry:.10f}",
            f"{sl:.10f}",
            f"{tp1:.10f}",
            f"{tp2:.10f}",
            f"{stake_usdt:.2f}",
            LEVERAGE,
            result,
            f"{profit_pct:.6f}",
            f"{profit_usdt:.6f}",
            source,
        ])

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

def estimate_profit_usdt(stake_usdt: float, tp_pct: float) -> float:
    position = stake_usdt * LEVERAGE
    gross = position * tp_pct
    fees = position * (TAKER_FEE + MAKER_FEE)
    net = gross - fees
    return max(0.0, net)

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

    trailing_pct = max(0.01, TRAILING_PCT_MULTIPLIER * atr_val / close)
    trailing_activation = tp1_pct * TRAILING_ACTIVATION_MULTIPLIER

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
        "trailing_pct": trailing_pct,
        "trailing_activation": trailing_activation,
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

def normalize_amount_for_exchange(exchange_id: str, symbol: str, amount: float) -> float:
    if exchange_id == "mexc":
        return max(1, int(math.ceil(amount)))
    else:
        return float(f"{amount:.6f}")

def set_leverage_isolated(ex: ccxt.Exchange, symbol: str, lev: int):
    try:
        ex.set_leverage(lev, symbol, params={"marginMode": "isolated", "posMode": "one_way"})
    except Exception as e:
        log.warning(f"set_leverage {symbol}: {e}")

def place_orders(ex: ccxt.Exchange, trade: Dict[str, Any]):
    sym = trade["symbol"]
    side = trade["side"]
    amount = trade["amount"]
    sl_price = trade["sl_price"]
    tp1_price = trade["tp1_price"]
    tp2_price = trade["tp2_price"]

    entry_order = ex.create_market_order(sym, "buy" if side == "long" else "sell", amount)

    if ex.id == "bitget":
        log.info(f"[BITGET] Entry done {sym} {side} amount={amount}. TP/SL не ставим на бирже.")
        return {"tp_sl_placed": False, "order_ids": {}}

    amount1 = amount * PARTIAL_TP_RATIO
    amount2 = amount - amount1
    tp1_order = ex.create_order(sym, "limit", "sell" if side == "long" else "buy", amount1, tp1_price, params={"reduceOnly": True})
    tp2_order = ex.create_order(sym, "limit", "sell" if side == "long" else "buy", amount2, tp2_price, params={"reduceOnly": True})
    sl_order = ex.create_order(sym, "stop_market", "sell" if side == "long" else "buy", amount, params={"reduceOnly": True, "triggerPrice": sl_price})
    return {
        "tp_sl_placed": True,
        "order_ids": {
            "entry": entry_order.get("id"),
            "tp1": tp1_order.get("id"),
            "tp2": tp2_order.get("id"),
            "sl": sl_order.get("id"),
        }
    }

# ====== INLINE ======
def build_signal_keyboard(signal_id: str):
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
    row_close = [InlineKeyboardButton("CLOSE", callback_data=f"CLOSE|{signal_id}")]
    return InlineKeyboardMarkup([row_buy, row_est, row_close])

def build_confirm_keyboard(query_id: str):
    return InlineKeyboardMarkup([[InlineKeyboardButton("Yes", callback_data=f"CONFIRM|{query_id}|YES"),
                                  InlineKeyboardButton("No", callback_data=f"CONFIRM|{query_id}|NO")]])

# ====== EXECUTE TRADE ======
async def execute_trade_from_signal(
    update_or_bot,
    chat_id: int,
    signal: Dict[str, Any],
    stake_usdt: float,
    reason: str = "manual"
):
    ex = make_exchange(signal["exchange"])
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
        "use_trailing": signal["prob"] >= TRAILING_MIN_PROB,
        "trailing_pct": signal.get("trailing_pct", 0.01),
        "trailing_activation": signal.get("trailing_activation", 0.02),
    }

    try:
        res = place_orders(ex, trade)
        trade["order_ids"] = res.get("order_ids", {})
    except Exception as e:
        msg = f"[{signal['exchange'].upper()}] Ошибка ордеров: {e}"
        if hasattr(update_or_bot, "send_message"):
            await update_or_bot.send_message(chat_id, msg)
        else:
            await update_or_bot.effective_message.reply_text(msg)
        log.error(e)

        # ЛОГ неуспешной попытки
        append_trade_row(
            chat_id,
            signal["exchange"],
            sym,
            side,
            entry,
            sl_price,
            tp1_price,
            tp2_price,
            stake_usdt,
            result="FAIL",
            profit_pct=0.0,
            profit_usdt=0.0,
            source=reason,
        )
        return

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
        "use_trailing": trade["use_trailing"],
        "trailing_pct": trade["trailing_pct"],
        "trailing_activation": trade["trailing_activation"],
        "order_ids": trade.get("order_ids", {}),
    })

    net_pct = estimate_net_profit_pct(tp1_pct)
    net_usdt = estimate_profit_usdt(stake_usdt, tp1_pct)

    # ЛОГ ОТКРЫТИЯ
    append_trade_row(
        chat_id,
        signal["exchange"],
        sym,
        side,
        entry,
        sl_price,
        tp1_price,
        tp2_price,
        stake_usdt,
        result="OPEN",
        profit_pct=0.0,
        profit_usdt=0.0,
        source=reason,
    )

    txt = (
        f"✅ [{signal['exchange'].upper()}] Открыт {side.upper()} {sym}\n"
        f"Сумма: {stake_usdt} USDT (x{LEVERAGE}) → объём {amount}\n"
        f"Entry: {entry:.6f}\n"
        f"SL: {sl_price:.6f} (−{sl_pct*100:.1f}%)\n"
        f"TP1: {tp1_price:.6f} (+{tp1_pct*100:.1f}%)\n"
        f"TP2: {tp2_price:.6f} (+{tp2_pct*100:.1f}%)\n"
        f"⏱ Ожидание: ~{eta_min} мин\n"
        f"💰 Net (TP1): +{net_pct*100:.2f}% ≈ +{net_usdt:.2f} USDT\n"
    )
    if trade["use_trailing"]:
        txt += f"📈 Trailing: {trade['trailing_pct']*100:.2f}% after +{trade['trailing_activation']*100:.2f}%\n"
    if signal["exchange"] == "bitget" and not res.get("tp_sl_placed", False):
        txt += "⚠️ Bitget: TP/SL не поставлены на бирже, фиксация — через бота.\n"

    if hasattr(update_or_bot, "send_message"):
        await update_or_bot.send_message(chat_id, txt)
    else:
        await update_or_bot.effective_message.reply_text(txt)

# ====== CALLBACK ======
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("|")
    chat_id = query.message.chat_id

    if len(parts) == 3 and parts[0] in ("BUY", "EST", "CLOSE"):
        action, signal_id, amt_str = parts
        if action in ("BUY", "EST"):
            try:
                amount_usdt = float(amt_str)
            except ValueError:
                return
        else:
            amount_usdt = 0.0

        signal_data = SIGNAL_STORE.get(signal_id)
        if not signal_data or time.time() - signal_data["timestamp"] > SIGNAL_TTL:
            await query.edit_message_text("Сигнал устарел. Сделай /scan.")
            SIGNAL_STORE.pop(signal_id, None)
            return

        signal = signal_data["signal"]

        if action == "EST":
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
            query_id = f"{chat_id}:{int(time.time())}"
            PENDING_CONFIRMS[query_id] = (signal_id, amount_usdt)
            txt = f"Подтверди BUY {amount_usdt} USDT для {signal['symbol']} {signal['side'].upper()}?"
            await query.message.reply_text(txt, reply_markup=build_confirm_keyboard(query_id))
            return

        if action == "CLOSE":
            await query.edit_message_reply_markup(None)
            SIGNAL_STORE.pop(signal_id, None)
            return

    elif len(parts) == 3 and parts[0] == "CONFIRM":
        _, query_id, choice = parts
        pending = PENDING_CONFIRMS.pop(query_id, None)
        if not pending:
            await query.edit_message_text("Подтверждение устарело.")
            return
        signal_id, amount_usdt = pending
        signal_data = SIGNAL_STORE.get(signal_id)
        if not signal_data:
            await query.edit_message_text("Сигнал устарел.")
            return
        signal = signal_data["signal"]

        if choice == "YES":
            await execute_trade_from_signal(context.bot, chat_id, signal, amount_usdt, reason="inline")
            await query.edit_message_text("Подтверждено! Сделка открыта.")
        else:
            await query.edit_message_text("Отменено.")
        return

# ====== КОМАНДЫ ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*UNIFIED FUTURES BOT v25 ANALYTICS*\n\n"
        "⚙️ Параметры:\n"
        f"• TF: {TIMEFRAME}\n"
        f"• Автоскан: {SCAN_INTERVAL//60} мин\n"
        f"• Мин. объём: {MIN_QUOTE_VOLUME/1_000_000:.1f}M USDT\n"
        f"• RSI OB/OS: {RSI_OVERBOUGHT}/{RSI_OVERSOLD}\n"
        f"• EMA: {EMA_SHORT}/{EMA_LONG}\n"
        f"• SL (base): {BASE_STOP_LOСС_PCT*100:.1f}%\n"
        f"• Плечо: x{LEVERAGE}\n"
        "📋 Команды:\n"
        "/scan — найти сигналы\n"
        "/top — топ-3 сильных\n"
        "/trade <№> <сумма>\n"
        "/trade <symbol> <side> <сумма>\n"
        "/report — активные сделки\n"
        "/stats — статистика по CSV\n"
        "/history — прислать CSV\n"
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
    now = time.time()
    to_remove = [k for k, v in SIGNAL_STORE.items() if now - v["timestamp"] > SIGNAL_TTL]
    for k in to_remove:
        del SIGNAL_STORE[k]

    for i, d in enumerate(entries, 1):
        signal_id = f"{chat_id}:{i}:{int(now)}"
        SIGNAL_STORE[signal_id] = {"signal": d, "timestamp": now}
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
    now = time.time()
    to_remove = [k for k, v in SIGNAL_STORE.items() if now - v["timestamp"] > SIGNAL_TTL]
    for k in to_remove:
        del SIGNAL_STORE[k]

    for i, d in enumerate(strong[:3], 1):
        signal_id = f"{chat_id}:{i}:{int(now)}"
        SIGNAL_STORE[signal_id] = {"signal": d, "timestamp": now}
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

    if not context.args:
        await m.reply_text("Формат: /trade <№> <сумма> или /trade <symbol> <side> <amount>")
        return

    # режим: /trade ZEN/USDT long 20
    if len(context.args) >= 3 and "/" in context.args[0]:
        symbol = context.args[0].upper()
        side = context.args[1].lower()
        try:
            stake = float(context.args[2])
        except ValueError:
            await m.reply_text("Сумма должна быть числом.")
            return

        sig = None
        for sd in SIGNAL_STORE.values():
            s = sd["signal"]
            if s["symbol"].upper() == symbol and s["side"] == side:
                sig = s
                break
        if not sig:
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

    # режим: /trade 1 20
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
        "trailing_pct": max(0.01, TRAILING_PCT_MULTIPLIER * (tp1_pct / TP1_MULTIPLIER_TREND)),
        "trailing_activation": tp1_pct * TRAILING_ACTIVATION_MULTIPLIER,
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
        lines.append(
            f"{i}. [{t['exchange'].upper()}] {t['side'].upper()} {t['symbol']} @ {t['entry']:.6f} | SL {t['sl_price']:.6f} | TP1 {t['tp1_price']:.6f}"
        )
    await update.effective_message.reply_text("\n".join(lines))

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AUTO_ENABLED
    AUTO_ENABLED = False
    await update.effective_message.reply_text("Автоскан отключён.")

# ====== СТАТИСТИКА ======
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not os.path.exists(TRADES_CSV_PATH):
        await update.effective_message.reply_text("История сделок пока пустая.")
        return
    import csv
    total = 0
    wins = 0
    loses = 0
    profit_sum = 0.0
    with open(TRADES_CSV_PATH, "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            if str(row["chat_id"]) != str(chat_id):
                continue
            if row["result"] == "OPEN":
                # пока не закрыта
                continue
            total += 1
            p_usdt = float(row["profit_usdt"])
            profit_sum += p_usdt
            if row["result"] in ("TP1", "TP2", "CLOSED"):
                wins += 1
            elif row["result"] in ("SL", "FAIL"):
                loses += 1
    if total == 0:
        await update.effective_message.reply_text("Пока нет закрытых сделок для статистики.")
        return
    win_rate = wins / total * 100
    avg_profit = profit_sum / total
    txt = (
        f"📊 Статистика по сделкам:\n"
        f"Всего закрытых: {total}\n"
        f"В плюс: {wins} ({win_rate:.1f}%)\n"
        f"В минус/ошибка: {loses}\n"
        f"Средний результат: {avg_profit:.2f} USDT на сделку\n"
        f"Цель: 60–65% — у тебя {'OK' if win_rate >= 60 else 'пока ниже цели'}\n"
    )
    await update.effective_message.reply_text(txt)

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(TRADES_CSV_PATH):
        await update.effective_message.reply_text("История пока пустая.")
        return
    await update.effective_message.reply_document(
        document=InputFile(TRADES_CSV_PATH),
        filename="trades_history.csv",
        caption="История сделок"
    )

# ====== ФОН: авто + трейлинг ======
async def auto_scan_loop(app):
    global LAST_NO_SIGNAL_TIME
    while True:
        if AUTO_ENABLED:
            try:
                entries = await scan_all()
                now = time.time()
                if entries:
                    LAST_NO_SIGNAL_TIME = now
                    # чистим
                    to_remove = [k for k, v in SIGNAL_STORE.items() if now - v["timestamp"] > SIGNAL_TTL]
                    for k in to_remove:
                        SIGNAL_STORE.pop(k, None)
                    for chat_id in list(LAST_SCAN.keys()):
                        LAST_SCAN[chat_id] = []
                        await app.bot.send_message(
                            chat_id,
                            f"📊 Автосигналы {datetime.utcnow().strftime('%H:%M:%S')} UTC:"
                        )
                        for i, d in enumerate(entries[:5], 1):
                            signal_id = f"auto:{chat_id}:{i}:{int(now)}"
                            SIGNAL_STORE[signal_id] = {"signal": d, "timestamp": now}
                            LAST_SCAN[chat_id].append((
                                d["symbol"], d["side"], d["exchange"],
                                d["entry"], d["sl_pct"], d["tp1_pct"], d["tp2_pct"],
                                d["tp1_price"], d["tp2_price"], d["eta_min"], d["prob"], d["volr"], d["rsi"]
                            ))
                            tag = signal_strength_tag(d["prob"])
                            text = (
                                f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} {tag} "
                                f"ETA {d['eta_min']}м\n"
                                f"Entry≈{d['entry']:.6f} | SL=−{d['sl_pct']*100:.1f}% | TP1=+{d['tp1_pct']*100:.1f}%"
                            )
                            await app.bot.send_message(
                                chat_id,
                                text,
                                reply_markup=build_signal_keyboard(signal_id)
                            )
                    log.info("auto_scan tick OK (with inline)")
                else:
                    if now - LAST_NO_SIGNAL_TIME >= NO_SIGNAL_NOTIFY_INTERVAL:
                        for chat_id in LAST_SCAN.keys():
                            await app.bot.send_message(chat_id, "Сигналов нет.")
                        LAST_NO_SIGNAL_TIME = now
            except Exception as e:
                log.error(f"auto_scan_loop: {e}")
        await asyncio.sleep(SCAN_INTERVAL)

async def trailing_monitor(app):
    while True:
        for chat_id, trades in ACTIVE_TRADES.items():
            for trade in trades[:]:
                ex = make_exchange(trade["exchange"])
                try:
                    ticker = ex.fetch_ticker(trade["symbol"])
                    current_price = float(ticker["last"])
                    side = trade["side"]
                    entry = trade["entry"]

                    close_side = "sell" if side == "long" else "buy"

                    # TP2
                    if (side == "long" and current_price >= trade["tp2_price"]) or (side == "short" and current_price <= trade["tp2_price"]):
                        ex.create_market_order(trade["symbol"], close_side, trade["amount"], params={"reduceOnly": True})
                        trades.remove(trade)

                        # лог TP2
                        profit_pct = abs(current_price - entry) / entry
                        profit_usdt = estimate_profit_usdt(trade["stake"], profit_pct)
                        append_trade_row(
                            chat_id,
                            trade["exchange"],
                            trade["symbol"],
                            trade["side"],
                            entry,
                            trade["sl_price"],
                            trade["tp1_price"],
                            trade["tp2_price"],
                            trade["stake"],
                            result="TP2",
                            profit_pct=profit_pct,
                            profit_usdt=profit_usdt,
                            source=trade.get("source", "auto"),
                        )

                        await app.bot.send_message(chat_id, f"Closed by TP2: {trade['symbol']} @ {current_price:.6f}")
                        continue

                    # TP1
                    if (side == "long" and current_price >= trade["tp1_price"]) or (side == "short" and current_price <= trade["tp1_price"]):
                        partial_amt = trade["amount"] * PARTIAL_TP_RATIO
                        ex.create_market_order(trade["symbol"], close_side, partial_amt, params={"reduceOnly": True})
                        trade["amount"] -= partial_amt

                        profit_pct = abs(trade["tp1_price"] - entry) / entry
                        profit_usdt = estimate_profit_usdt(trade["stake"], profit_pct)
                        append_trade_row(
                            chat_id,
                            trade["exchange"],
                            trade["symbol"],
                            trade["side"],
                            entry,
                            trade["sl_price"],
                            trade["tp1_price"],
                            trade["tp2_price"],
                            trade["stake"],
                            result="TP1",
                            profit_pct=profit_pct,
                            profit_usdt=profit_usdt,
                            source=trade.get("source", "auto"),
                        )

                        await app.bot.send_message(chat_id, f"Partial close TP1: {trade['symbol']} @ {current_price:.6f}")
                        # не continue — пусть trailing обновится
                    # SL
                    if (side == "long" and current_price <= trade["sl_price"]) or (side == "short" and current_price >= trade["sl_price"]):
                        ex.create_market_order(trade["symbol"], close_side, trade["amount"], params={"reduceOnly": True})
                        trades.remove(trade)

                        loss_pct = -abs(trade["sl_price"] - entry) / entry
                        loss_usdt = estimate_profit_usdt(trade["stake"], abs(loss_pct)) * -1

                        append_trade_row(
                            chat_id,
                            trade["exchange"],
                            trade["symbol"],
                            trade["side"],
                            entry,
                            trade["sl_price"],
                            trade["tp1_price"],
                            trade["tp2_price"],
                            trade["stake"],
                            result="SL",
                            profit_pct=loss_pct,
                            profit_usdt=loss_usdt,
                            source=trade.get("source", "auto"),
                        )

                        await app.bot.send_message(chat_id, f"Closed by SL: {trade['symbol']} @ {current_price:.6f}")
                        continue

                    # Trailing
                    profit_pct_live = (current_price - entry) / entry if side == "long" else (entry - current_price) / entry
                    if trade.get("use_trailing") and profit_pct_live >= trade["trailing_activation"]:
                        new_sl = current_price * (1 - trade["trailing_pct"]) if side == "long" else current_price * (1 + trade["trailing_pct"])
                        if (side == "long" and new_sl > trade["sl_price"]) or (side == "short" and new_sl < trade["sl_price"]):
                            old_sl = trade["sl_price"]
                            trade["sl_price"] = new_sl
                            if ex.id == "mexc" and "sl" in trade.get("order_ids", {}):
                                try:
                                    ex.cancel_order(trade["order_ids"]["sl"], trade["symbol"])
                                    sl_order = ex.create_order(
                                        trade["symbol"], "stop_market", close_side, trade["amount"],
                                        params={"reduceOnly": True, "triggerPrice": new_sl}
                                    )
                                    trade["order_ids"]["sl"] = sl_order.get("id")
                                except Exception as e:
                                    log.warning(f"Trailing SL re-place {trade['symbol']}: {e}")
                            await app.bot.send_message(chat_id, f"Trailing SL updated: {old_sl:.6f} → {new_sl:.6f}")
                except Exception as e:
                    log.warning(f"Trailing monitor: {e}")
        await asyncio.sleep(60)

# ====== MAIN ======
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
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))

    log.info("UNIFIED FUTURES BOT v25 ANALYTICS STARTED")
    print("BOT ЗАПУЩЕН НА RENDER.COM | 24/7", flush=True)

    asyncio.create_task(auto_scan_loop(app))
    asyncio.create_task(trailing_monitor(app))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
