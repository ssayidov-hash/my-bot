import nest_asyncio
nest_asyncio.apply()  # критично для Render / py 3.13 / telegram async

# -*- coding: utf-8 -*-
"""
unified_futures_bot_ultra_v2.5.4_SAFE.py
MEXC + Bitget | 24/7 | x5 | сигналы по тренду | /scan | /top | /trade <№> <сумма>
+ кнопки BUY/EST и в авто-скане
+ активный мониторинг SL/TP/trailing
+ попытка выставить TP/SL на Bitget
"""

import os
import sys
import asyncio
import logging
import time
from datetime import datetime
import datetime as dt
from typing import Dict, List, Tuple, Any

import requests
import ccxt
import pandas as pd
import numpy as np

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# =====================================================
# ENV + анти-дубль
# =====================================================
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")

if not TG_BOT_TOKEN:
    raise SystemExit("❗ Нужно задать TG_BOT_TOKEN в переменных окружения")

def ensure_single_instance(token: str):
    """если вебхук у бота включён — значит уже есть живой инстанс, выходим"""
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=8)
        data = resp.json()
        if data.get("ok") and data.get("result", {}).get("url"):
            print("⚠️ Duplicate instance detected — shutting down.", flush=True)
            sys.exit(0)
    except Exception as e:
        print(f"Webhook check failed: {e}", flush=True)

ensure_single_instance(TG_BOT_TOKEN)

# =====================================================
# ПАРАМЕТРЫ
# =====================================================
TIMEFRAME = "15m"
LIMIT = 300

RSI_PERIOD = 14
RSI_OVERBOUGHT = 82.0
RSI_OVERSOLD = 18.0

EMA_SHORT = 50
EMA_LONG = 200
VOL_SMA = 20
ATR_PERIOD = 14

LEVERAGE = 5

# было 5%, ты попросил 3.5%
BASE_STOP_LOSS_PCT = 0.035          # 3.5% — безопаснее, чем 5 при частом входе
MIN_QUOTE_VOLUME = 5_000_000        # 5M USDT
SCAN_INTERVAL = 300                 # авто-скан раз в 5 мин
TRAILING_ACTIVATION_PCT = 0.03      # включаем трейлинг при +3%
TRAILING_DISTANCE_PCT = 0.015       # расстояние 1.5%
NO_SIGNAL_NOTIFY_INTERVAL = 3600

PARTIAL_TP_RATIO = 0.5
TP1_MULTIPLIER_TREND = 2.0
TP2_MULTIPLIER_TREND = 4.0

# комиссии
TAKER_FEE = 0.0006
MAKER_FEE = 0.0002

# папки
BASE_DIR = os.getcwd()
LOG_DIR = os.path.join(BASE_DIR, "logs")
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

LOG_FILENAME = os.path.join(
    LOG_DIR,
    f"{datetime.now(dt.timezone.utc).date().isoformat()}_v2.5.4_SAFE.log"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILENAME, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("UFutures2.5.4")

# =====================================================
# ГЛОБАЛЫ
# =====================================================
LAST_SCAN: Dict[int, List[Tuple]] = {}      # chat_id -> list(signals)
ACTIVE_TRADES: Dict[int, List[Dict[str, Any]]] = {}
AUTO_ENABLED = True
H1_TRENDS_CACHE: Dict[str, Tuple[str, float]] = {}
H4_TRENDS_CACHE: Dict[str, Tuple[str, float]] = {}
LAST_NO_SIGNAL_TIME = 0
LAST_AUTO_SENT: Dict[int, str] = {}         # чат -> текст последнего авто сообщения
TRADES_CSV = os.path.join(DATA_DIR, "trades_history.csv")

APP = None  # сюда сохраним app из main, чтобы trailing_monitor мог слать сообщения

# =====================================================
# ВСПОМОГАТЕЛЬНЫЕ
# =====================================================
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

def make_exchange(exchange_name: str):
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

def calc_position_amount(balance_usdt: float, entry_price: float, stake_usdt: float, leverage: int) -> float:
    stake_usdt = min(stake_usdt, balance_usdt)
    return (stake_usdt * leverage) / entry_price

def signal_strength_tag(prob: int) -> str:
    if prob >= 85:
        return "🔥 Сильный"
    elif prob >= 70:
        return "⚡ Хороший"
    elif prob >= 55:
        return "⚠️ Средний"
    else:
        return "❄️ Слабый"

def normalize_amount_for_exchange(ex: ccxt.Exchange, symbol: str, amount: float) -> float:
    try:
        return float(ex.amount_to_precision(symbol, amount))
    except Exception:
        return amount

def is_isolated_mode_on_bitget(ex: ccxt.Exchange, symbol: str) -> bool:
    try:
        pos = ex.fetch_positions([symbol])
        for p in pos:
            mm = p.get("info", {}).get("marginMode") or p.get("marginMode")
            if mm and mm.lower() == "isolated":
                return True
        return False
    except Exception:
        return True  # не блокируем если не смогли прочитать

def set_leverage_isolated(ex: ccxt.Exchange, symbol: str, lev: int, side: str = "long"):
    try:
        if ex.id == "mexc":
            ex.set_leverage(
                lev, symbol,
                params={
                    "openType": 1,  # isolated
                    "positionType": 1 if side == "long" else 2,
                }
            )
        else:
            ex.set_leverage(
                lev, symbol,
                params={
                    "marginMode": "isolated",
                    "posMode": "one_way",
                }
            )
    except Exception as e:
        log.warning(f"set_leverage {symbol}: {e}")

def append_trade_row(row: Dict[str, Any]):
    file_exists = os.path.exists(TRADES_CSV)
    df_row = pd.DataFrame([row])
    if file_exists:
        df_row.to_csv(TRADES_CSV, mode="a", header=False, index=False)
    else:
        df_row.to_csv(TRADES_CSV, mode="w", header=True, index=False)

# =====================================================
# АНАЛИТИКА
# =====================================================
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
    if trend_ok_long:
        score += lo
    if trend_ok_short:
        score += sh
    if volr >= 2.5:
        score += 1
    if h1_trend == h4_trend and h1_trend != "flat":
        score += 1
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
    mexc_res = await mexc_task
    bitget_res = await bitget_task
    return mexc_res + bitget_res

# =====================================================
# TELEGRAM UI
# =====================================================
def build_signal_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"BUY 10", callback_data=f"BUY|{index}|10"),
            InlineKeyboardButton(f"BUY 20", callback_data=f"BUY|{index}|20"),
            InlineKeyboardButton(f"BUY 50", callback_data=f"BUY|{index}|50"),
            InlineKeyboardButton(f"BUY 100", callback_data=f"BUY|{index}|100"),
        ],
        [
            InlineKeyboardButton(f"EST 10", callback_data=f"EST|{index}|10"),
            InlineKeyboardButton(f"EST 20", callback_data=f"EST|{index}|20"),
            InlineKeyboardButton(f"EST 50", callback_data=f"EST|{index}|50"),
            InlineKeyboardButton(f"EST 100", callback_data=f"EST|{index}|100"),
        ]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*🤖 UNIFIED FUTURES BOT v2.5.4 SAFE*\n\n"
        "⚙️ Параметры:\n"
        f"• TF: {TIMEFRAME}\n"
        f"• Автоскан: {SCAN_INTERVAL//60} мин\n"
        f"• Мин. объём: {MIN_QUOTE_VOLUME/1_000_000:.1f}M USDT\n"
        f"• SL (min): {BASE_STOP_LOSS_PCT*100:.1f}%\n"
        f"• Плечо: x{LEVERAGE}\n"
        f"• Trailing: с +{TRAILING_ACTIVATION_PCT*100:.1f}%, шаг {TRAILING_DISTANCE_PCT*100:.1f}%\n\n"
        "📋 Команды:\n"
        "/scan — найти сигналы\n"
        "/top — топ-3 сильных\n"
        "/trade <№> <сумма> — войти по сигналу\n"
        "/report — активные сделки\n"
        "/history — файл сделок\n"
        "/stop — выключить авто\n"
        "💡 Кнопки BUY/EST есть и в авто-сигналах."
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
    for i, d in enumerate(entries[:15], 1):
        tag = signal_strength_tag(d["prob"])
        text = (
            f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} — {tag} ({d['prob']}%)\n"
            f"RSI={d['rsi']:.1f} | vol×={d['volr']:.2f} | H1={d['h1']} H4={d['h4']}\n"
            f"Entry≈{d['entry']:.6f} | SL=−{d['sl_pct']*100:.1f}% | TP1=+{d['tp1_pct']*100:.1f}% | ETA {d['eta_min']} мин"
        )
        await update.effective_message.reply_text(
            text,
            reply_markup=build_signal_keyboard(i),
            parse_mode=None,
        )
        LAST_SCAN[chat_id].append((
            d["symbol"], d["side"], d["exchange"],
            d["entry"], d["sl_pct"], d["tp1_pct"], d["tp2_pct"],
            d["tp1_price"], d["tp2_price"], d["eta_min"], d["prob"], d["volr"], d["rsi"]
        ))

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
    for i, d in enumerate(strong[:3], 1):
        text = (
            f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} — {signal_strength_tag(d['prob'])} ({d['prob']}%)\n"
            f"Entry≈{d['entry']:.6f} | SL=−{d['sl_pct']*100:.1f}% | TP1=+{d['tp1_pct']*100:.1f}% | ETA {d['eta_min']} мин"
        )
        await update.effective_message.reply_text(
            text,
            reply_markup=build_signal_keyboard(i),
        )
        LAST_SCAN[chat_id].append((
            d["symbol"], d["side"], d["exchange"],
            d["entry"], d["sl_pct"], d["tp1_pct"], d["tp2_pct"],
            d["tp1_price"], d["tp2_price"], d["eta_min"], d["prob"], d["volr"], d["rsi"]
        ))

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if os.path.exists(TRADES_CSV):
        await update.effective_message.reply_document(open(TRADES_CSV, "rb"))
    else:
        await update.effective_message.reply_text("История пока пустая.")

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

# =====================================================
# ТОРГОВЛЯ (команда)
# =====================================================
async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = update.effective_message
    if chat_id not in LAST_SCAN or not LAST_SCAN[chat_id]:
        await m.reply_text("Сначала /scan или /top.")
        return
    if len(context.args) < 2:
        await m.reply_text("Формат: /trade <номер_сигнала> <сумма_USDT>\nНапр: /trade 2 40")
        return
    try:
        idx = int(context.args[0]) - 1
        stake = float(context.args[1])
    except ValueError:
        await m.reply_text("Номер и сумма должны быть числами.")
        return

    rows = LAST_SCAN[chat_id]
    if idx < 0 or idx >= len(rows):
        await m.reply_text("Нет такого номера сигнала.")
        return

    sym, side, exchange, entry, sl_pct, tp1_pct, tp2_pct, tp1_price, tp2_price, eta_min, prob, volr, rsi_val = rows[idx]
    ex = make_exchange(exchange)

    # MEXC — сигналы только руками
    if exchange == "mexc":
        await m.reply_text("⚠️ MEXC фьючерсы сейчас не даём через API. Сигнал есть — выставь руками.")
        return

    try:
        bal = ex.fetch_balance(params={"type": "swap"})["USDT"]["free"]
    except Exception as e:
        await m.reply_text(f"[{exchange.upper()}] Не смог получить баланс: {e}")
        return

    amount = calc_position_amount(bal, entry, stake, LEVERAGE)
    amount = normalize_amount_for_exchange(ex, sym, amount)
    if amount <= 0:
        await m.reply_text(f"[{exchange.upper()}] Слишком маленькая сумма для {sym}")
        return

    # bitget: проверяем isolated
    if exchange == "bitget":
        if not is_isolated_mode_on_bitget(ex, sym):
            await m.reply_text("⚠️ Bitget сейчас в CROSS/CRUZADO. Сначала включи ISOLATED.")
            return

    # проверка минимального объёма
    try:
        market = ex.market(sym)
        min_amt = market.get("limits", {}).get("amount", {}).get("min", 0)
        if min_amt and amount < min_amt:
            log.warning(f"amount {amount:.4f} < min {min_amt:.4f}, adjusted for {sym}")
            amount = min_amt
    except Exception as e:
        log.warning(f"min amount check failed for {sym}: {e}")

    set_leverage_isolated(ex, sym, LEVERAGE, side=side)

    # SL/TP уровни
    if side == "long":
        sl_price = entry * (1 - sl_pct)
    else:
        sl_price = entry * (1 + sl_pct)

    # 1) открываем позицию
    try:
        ex.create_market_order(sym, "buy" if side == "long" else "sell", amount)
    except Exception as e:
        await m.reply_text(f"[{exchange.upper()}] Ошибка ордеров: {e}")
        log.error(f"order error: {e}")
        return

    # 2) пытаемся сразу поставить TP и SL на бирже (bitget)
    placed_on_exchange = False
    if exchange == "bitget":
        try:
            # TP1
            ex.create_order(
                sym,
                type="limit",
                side="sell" if side == "long" else "buy",
                amount=amount * PARTIAL_TP_RATIO,
                price=tp1_price,
                params={
                    "reduceOnly": True,
                    "marginMode": "isolated",
                }
            )
            # SL
            ex.create_order(
                sym,
                type="stop_market",
                side="sell" if side == "long" else "buy",
                amount=amount,
                params={
                    "reduceOnly": True,
                    "triggerPrice": sl_price,
                    "marginMode": "isolated",
                }
            )
            placed_on_exchange = True
        except Exception as e:
            log.warning(f"Bitget TP/SL on-exchange skipped: {e}")
            placed_on_exchange = False

    # 3) сохраняем сделку для мониторинга
    ACTIVE_TRADES.setdefault(chat_id, []).append({
        "symbol": sym,
        "side": side,
        "entry": entry,
        "amount": amount,
        "exchange": exchange,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "sl_price": sl_price,
        "time": datetime.now(dt.timezone.utc),
        "stake": stake,
        "trailing_on": False,
        "placed_on_exchange": placed_on_exchange,
    })

    net_pct = estimate_net_profit_pct(tp1_pct)
    append_trade_row({
        "ts": datetime.now(dt.timezone.utc).isoformat(),
        "chat_id": chat_id,
        "exchange": exchange,
        "symbol": sym,
        "side": side,
        "entry": entry,
        "amount": amount,
        "stake": stake,
        "tp1": tp1_price,
        "tp2": tp2_price,
        "sl": sl_price,
        "prob": prob,
        "eta_min": eta_min,
        "reason": "OPEN",
    })

    await m.reply_text(
        f"✅ [{exchange.upper()}] Открыт {side.upper()} {sym}\n"
        f"Сумма: {stake} USDT (x{LEVERAGE}) → объём {amount:.4f}\n"
        f"Entry: {entry:.6f}\n"
        f"SL: {sl_price:.6f} (−{sl_pct*100:.1f}%)\n"
        f"TP1: {tp1_price:.6f} (+{tp1_pct*100:.1f}%)\n"
        f"TP2: {tp2_price:.6f} (+{tp2_pct*100:.1f}%)\n"
        f"⏱️ ETA: ~{eta_min} мин\n"
        f"💰 Net: +{net_pct*100:.2f}%\n"
        + (
            "✅ TP/SL выставлены на бирже.\n"
            if placed_on_exchange else
            "⚠️ Bitget: TP/SL не поставлены на бирже, фиксация — через бота.\n"
        )
    )

# =====================================================
# CALLBACK (кнопки BUY/EST)
# =====================================================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # BUY|idx|amount  или EST|idx|amount
    chat_id = query.message.chat_id
    if chat_id not in LAST_SCAN or not LAST_SCAN[chat_id]:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    try:
        action, idx, amt = data.split("|")
        idx = int(idx) - 1
        stake = float(amt)
    except Exception:
        return

    rows = LAST_SCAN[chat_id]
    if idx < 0 or idx >= len(rows):
        await query.message.reply_text("Сигнал уже устарел, сделай /scan")
        return

    sym, side, exchange, entry, sl_pct, tp1_pct, tp2_pct, tp1_price, tp2_price, eta_min, prob, volr, rsi_val = rows[idx]
    ex = make_exchange(exchange)

    # это оценка
    if action == "EST":
        net_pct = estimate_net_profit_pct(tp1_pct)
        profit = stake * net_pct
        await query.message.reply_text(
            f"📈 Оценка {sym} {side.upper()} на {stake} USDT:\n"
            f"TP1: +{tp1_pct*100:.2f}% → ≈ +{profit:.2f} USDT (с комиссией)\n"
            f"ETA: {eta_min} мин"
        )
        log.info(f"[EST] chat={chat_id} {sym} {side} stake={stake}")
        return

    # это BUY
    if exchange == "mexc":
        await query.message.reply_text("⚠️ MEXC фьючерсы по API не ставим. Сигнал смотри и ставь руками.")
        return

    try:
        bal = ex.fetch_balance(params={"type": "swap"})["USDT"]["free"]
    except Exception as e:
        await query.message.reply_text(f"[{exchange.upper()}] Не смог получить баланс: {e}")
        return

    amount = calc_position_amount(bal, entry, stake, LEVERAGE)
    amount = normalize_amount_for_exchange(ex, sym, amount)
    if amount <= 0:
        await query.message.reply_text(f"[{exchange.upper()}] Слишком маленькая сумма для {sym}")
        return

    if exchange == "bitget":
        if not is_isolated_mode_on_bitget(ex, sym):
            await query.message.reply_text("⚠️ Bitget сейчас в CROSS/CRUZADO. Сначала включи ISOLATED.")
            return

    # min amount
    try:
        market = ex.market(sym)
        min_amt = market.get("limits", {}).get("amount", {}).get("min", 0)
        if min_amt and amount < min_amt:
            log.warning(f"[BTN] amount {amount:.4f} < min {min_amt:.4f}, adjusted for {sym}")
            amount = min_amt
    except Exception as e:
        log.warning(f"[BTN] min amount check failed for {sym}: {e}")

    set_leverage_isolated(ex, sym, LEVERAGE, side=side)

    if side == "long":
        sl_price = entry * (1 - sl_pct)
    else:
        sl_price = entry * (1 + sl_pct)

    # вход
    try:
        ex.create_market_order(sym, "buy" if side == "long" else "sell", amount)
    except Exception as e:
        await query.message.reply_text(f"[{exchange.upper()}] Ошибка ордеров: {e}")
        log.error(f"[BTN] order error: {e}")
        return

    placed_on_exchange = False
    if exchange == "bitget":
        try:
            # TP
            ex.create_order(
                sym,
                type="limit",
                side="sell" if side == "long" else "buy",
                amount=amount * PARTIAL_TP_RATIO,
                price=tp1_price,
                params={
                    "reduceOnly": True,
                    "marginMode": "isolated",
                }
            )
            # SL
            ex.create_order(
                sym,
                type="stop_market",
                side="sell" if side == "long" else "buy",
                amount=amount,
                params={
                    "reduceOnly": True,
                    "triggerPrice": sl_price,
                    "marginMode": "isolated",
                }
            )
            placed_on_exchange = True
        except Exception as e:
            log.warning(f"[BTN] Bitget TP/SL on-exchange skipped: {e}")
            placed_on_exchange = False

    ACTIVE_TRADES.setdefault(chat_id, []).append({
        "symbol": sym,
        "side": side,
        "entry": entry,
        "amount": amount,
        "exchange": exchange,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "sl_price": sl_price,
        "time": datetime.now(dt.timezone.utc),
        "stake": stake,
        "trailing_on": False,
        "placed_on_exchange": placed_on_exchange,
    })

    append_trade_row({
        "ts": datetime.now(dt.timezone.utc).isoformat(),
        "chat_id": chat_id,
        "exchange": exchange,
        "symbol": sym,
        "side": side,
        "entry": entry,
        "amount": amount,
        "stake": stake,
        "tp1": tp1_price,
        "tp2": tp2_price,
        "sl": sl_price,
        "prob": prob,
        "eta_min": eta_min,
        "reason": "OPEN_BTN",
    })

    await query.message.reply_text(
        f"✅ [{exchange.upper()}] Открыт {side.upper()} {sym} (кнопка BUY {stake} USDT)\n"
        f"Entry: {entry:.6f} | SL: {sl_price:.6f} | TP1: {tp1_price:.6f}\n"
        + (
            "✅ TP/SL выставлены на бирже."
            if placed_on_exchange else
            "⚠️ Bitget: TP/SL не поставлены на бирже, фиксация — через бота."
        )
    )
    log.info(f"[BTN BUY] chat={chat_id} {exchange} {sym} {side} stake={stake}")

# =====================================================
# ФОН: авто-скан
# =====================================================
async def auto_scan_loop(app):
    global LAST_NO_SIGNAL_TIME, LAST_AUTO_SENT
    while True:
        if AUTO_ENABLED:
            try:
                entries = await scan_all()
                now = time.time()
                if entries:
                    for chat_id in list(LAST_SCAN.keys()):
                        top5 = entries[:5]
                        text_parts = []
                        last_sig_cache = []
                        for i, d in enumerate(top5, 1):
                            line = (
                                f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} "
                                f"{signal_strength_tag(d['prob'])} "
                            )
                            # поправим скобку
                            line = (
                                f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} "
                                f"{signal_strength_tag(d['prob'])} | "
                                f"Entry {d['entry']:.6f} | SL {d['sl_pct']*100:.1f}% | TP1 {d['tp1_pct']*100:.1f}% | ETA {d['eta_min']}м"
                            )
                            text_parts.append(line)
                            last_sig_cache.append((
                                d["symbol"], d["side"], d["exchange"],
                                d["entry"], d["sl_pct"], d["tp1_pct"], d["tp2_pct"],
                                d["tp1_price"], d["tp2_price"], d["eta_min"], d["prob"], d["volr"], d["rsi"]
                            ))

                        full_text = "📊 Автосигналы:\n" + "\n".join(text_parts)

                        # анти-дубль
                        if LAST_AUTO_SENT.get(chat_id) == full_text:
                            continue
                        LAST_AUTO_SENT[chat_id] = full_text

                        LAST_SCAN[chat_id] = last_sig_cache

                        kb = build_signal_keyboard(1)
                        await app.bot.send_message(chat_id, full_text, reply_markup=kb)

                    LAST_NO_SIGNAL_TIME = now
                else:
                    if now - LAST_NO_SIGNAL_TIME >= NO_SIGNAL_NOTIFY_INTERVAL:
                        for chat_id in list(LAST_SCAN.keys()):
                            await app.bot.send_message(chat_id, "Автоскан: пока сигналов нет.")
                        LAST_NO_SIGNAL_TIME = now
                log.info("auto_scan tick OK")
            except Exception as e:
                log.error(f"auto_scan_loop: {e}")
        await asyncio.sleep(SCAN_INTERVAL)

# =====================================================
# ФОН: трейлинг / SL / TP контроль
# =====================================================
async def trailing_monitor():
    global ACTIVE_TRADES, APP
    while True:
        # если ещё не инициализировали app — ждём
        if APP is None:
            await asyncio.sleep(2)
            continue

        for chat_id, trades in list(ACTIVE_TRADES.items()):
            new_trades = []
            for t in trades:
                ex = make_exchange(t["exchange"])
                sym = t["symbol"]
                side = t["side"]
                entry = t["entry"]
                sl_price = t["sl_price"]
                tp1_price = t["tp1_price"]
                amount = t["amount"]

                # получаем текущую цену
                try:
                    ticker = ex.fetch_ticker(sym)
                    last = float(ticker["last"])
                except Exception as e:
                    log.warning(f"trailing_monitor: fetch_ticker fail {sym}: {e}")
                    new_trades.append(t)
                    continue

                # текущий PNL в %
                if side == "long":
                    pnl_pct = (last - entry) / entry
                else:
                    pnl_pct = (entry - last) / entry

                # SL hit?
                sl_hit = (side == "long" and last <= sl_price) or (side == "short" and last >= sl_price)
                # TP1 hit?
                tp1_hit = (side == "long" and last >= tp1_price) or (side == "short" and last <= tp1_price)

                # 1) сработал SL — закрываем всё
                if sl_hit:
                    try:
                        ex.create_market_order(sym, "sell" if side == "long" else "buy", amount)
                    except Exception as e:
                        log.error(f"SL close fail {sym}: {e}")
                    # телега
                    try:
                        roi_pct = pnl_pct * 100
                        await APP.bot.send_message(
                            chat_id,
                            f"⚠️ Закрыл {sym} по SL @ {last:.6f} ({roi_pct:.2f}%)"
                        )
                    except Exception as e:
                        log.warning(f"send sl msg fail: {e}")
                    # лог в CSV
                    append_trade_row({
                        "ts": datetime.now(dt.timezone.utc).isoformat(),
                        "chat_id": chat_id,
                        "exchange": t["exchange"],
                        "symbol": sym,
                        "side": side,
                        "entry": entry,
                        "exit": last,
                        "roi_pct": pnl_pct * 100,
                        "reason": "SL",
                    })
                    continue  # не оставляем в списке

                # 2) сработал TP1 — закрываем половину и включаем трейлинг
                if tp1_hit:
                    close_amt = amount * PARTIAL_TP_RATIO
                    rest_amt = amount - close_amt
                    try:
                        ex.create_market_order(sym, "sell" if side == "long" else "buy", close_amt)
                    except Exception as e:
                        log.error(f"TP1 close fail {sym}: {e}")

                    try:
                        await APP.bot.send_message(
                            chat_id,
                            f"✅ TP1 по {sym} @ {last:.6f} (+{pnl_pct*100:.2f}%)"
                        )
                    except Exception as e:
                        log.warning(f"send tp1 msg fail: {e}")

                    append_trade_row({
                        "ts": datetime.now(dt.timezone.utc).isoformat(),
                        "chat_id": chat_id,
                        "exchange": t["exchange"],
                        "symbol": sym,
                        "side": side,
                        "entry": entry,
                        "exit": last,
                        "roi_pct": pnl_pct * 100,
                        "reason": "TP1",
                    })

                    # остаток — под трейлинг
                    if rest_amt > 0:
                        t["amount"] = rest_amt
                        t["trailing_on"] = True
                        if side == "long":
                            t["sl_price"] = last * (1 - TRAILING_DISTANCE_PCT)
                        else:
                            t["sl_price"] = last * (1 + TRAILING_DISTANCE_PCT)
                        new_trades.append(t)
                    continue

                # 3) трейлинг уже включён?
                if t.get("trailing_on"):
                    if side == "long":
                        target_sl = last * (1 - TRAILING_DISTANCE_PCT)
                        if target_sl > t["sl_price"]:
                            t["sl_price"] = target_sl
                            try:
                                await APP.bot.send_message(
                                    chat_id,
                                    f"📊 Trailing: подтянул SL по {sym} → {t['sl_price']:.6f}"
                                )
                            except Exception as e:
                                log.warning(f"send trailing msg fail: {e}")
                    else:
                        target_sl = last * (1 + TRAILING_DISTANCE_PCT)
                        if target_sl < t["sl_price"]:
                            t["sl_price"] = target_sl
                            try:
                                await APP.bot.send_message(
                                    chat_id,
                                    f"📊 Trailing: подтянул SL по {sym} → {t['sl_price']:.6f}"
                                )
                            except Exception as e:
                                log.warning(f"send trailing msg fail: {e}")

                    new_trades.append(t)
                    continue

                # 4) если прибыль >= 3% — включаем трейлинг
                if pnl_pct >= TRAILING_ACTIVATION_PCT:
                    t["trailing_on"] = True
                    if side == "long":
                        t["sl_price"] = last * (1 - TRAILING_DISTANCE_PCT)
                    else:
                        t["sl_price"] = last * (1 + TRAILING_DISTANCE_PCT)
                    try:
                        await APP.bot.send_message(
                            chat_id,
                            f"📈 Trailing включён по {sym}. SL → {t['sl_price']:.6f}"
                        )
                    except Exception as e:
                        log.warning(f"send trailing-on msg fail: {e}")
                    new_trades.append(t)
                    continue

                # иначе оставляем
                new_trades.append(t)

            ACTIVE_TRADES[chat_id] = new_trades

        await asyncio.sleep(15)

# =====================================================
# MAIN
# =====================================================
async def main():
    global APP
    print("🚀 MAIN INIT START", flush=True)
    app = ApplicationBuilder().token(TG_BOT_TOKEN).concurrent_updates(True).build()
    APP = app  # сохранили глобально для trailing_monitor
    print("✅ Application initialized", flush=True)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("trade", trade_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))

    log.info("UNIFIED FUTURES BOT v2.5.4 SAFE STARTED")
    print("BOT ЗАПУЩЕН НА RENDER.COM | 24/7", flush=True)

    asyncio.create_task(auto_scan_loop(app))
    asyncio.create_task(trailing_monitor())

    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
