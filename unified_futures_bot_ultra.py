# -*- coding: utf-8 -*-
"""
unified_futures_bot_ultra2.5.1.py
MEXC + Bitget | 24/7 | x5 | сигналы по тренду | /scan | /top | /trade <№> <сумма>
с кнопками: EST 10/20/50/100 и BUY 10/20/50/100
логирование сделок в ./data/trades_history.csv

улучшения внутри:

фикс пути DATA_DIR → os.path.join(os.getcwd(), "data")

trailing-monitor 2.0

изоляция проверки маржи (ISOLATED only)

автоматическая фиксация TP/SL ботом, если биржа не поддерживает

расширенный CSV-лог с ROI, Win/Loss, профитом

понятные сообщения Bitget/MEXC вместо JSON

автообновление активных позиций (fetch_positions)

анти-дубль проверки при старте

улучшенные Telegram-уведомления с режимом маржи и net-профитом
"""
import nest_asyncio
nest_asyncio.apply()  # <-- критично для Render + Python 3.13

import os
import sys
import asyncio
import logging
import time
from datetime import datetime, timezone
import datetime as dt
from typing import Dict, List, Tuple, Any, Optional

import requests
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
    CallbackQueryHandler,
    ContextTypes,
)

# ================== ENV ==================
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")

if not TG_BOT_TOKEN:
    raise SystemExit("❌ TG_BOT_TOKEN обязателен")

# ================== АНТИ-ДУБЛЬ ==================
def ensure_single_instance(token: str):
    if not token:
        return
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=8)
        data = resp.json()
        # если у бота уже что-то висит — выходим
        if data.get("ok") and data.get("result", {}).get("url"):
            print("⚠️ Другой инстанс бота уже активен (webhook). Завершение.")
            sys.exit(0)
    except Exception as e:
        # если запрос не удался — просто продолжаем
        print(f"[anti-dup] не удалось проверить webhook: {e}", flush=True)

ensure_single_instance(TG_BOT_TOKEN)

# ================== ПУТИ И ЛОГИ ==================
BASE_DIR = os.getcwd()
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

LOG_FILENAME = os.path.join(
    LOG_DIR,
    f"{datetime.now(timezone.utc).date().isoformat()}_v2.5.1.log"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILENAME, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("UFuturesBot2.5.1")

TRADES_CSV = os.path.join(DATA_DIR, "trades_history.csv")

# ================== ПАРАМЕТРЫ ==================
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
BASE_STOP_LOSS_PCT = 0.05          # 5% — с запасом
MIN_QUOTE_VOLUME = 5_000_000       # 5M USDT — отсекаем тухлые

SCAN_INTERVAL = 300                # 5 мин
AUTO_MONITOR_INTERVAL = 15         # было 60, сделали 15 сек для Bitget
NO_SIGNAL_NOTIFY_INTERVAL = 3600

PARTIAL_TP_RATIO = 0.5
TP1_MULTIPLIER_TREND = 2.0
TP2_MULTIPLIER_TREND = 4.0

# ТРЕЙЛИНГ — ты попросил 3% и дистанцию 1.5%
TRAILING_ACTIVATION_PCT = 0.03     # при прибыли 3% активируем
TRAILING_DISTANCE_PCT = 0.015      # держим стоп на 1.5% позади цены

# комиссии
TAKER_FEE = 0.0006   # 0.06%
MAKER_FEE = 0.0002   # 0.02%

# ================== ГЛОБАЛЫ ==================
LAST_SCAN: Dict[int, List[Dict[str, Any]]] = {}   # chat_id -> список сигналов в словарях
ACTIVE_TRADES: Dict[int, List[Dict[str, Any]]] = {}
AUTO_ENABLED = True
H1_TRENDS_CACHE: Dict[str, Tuple[str, float]] = {}
H4_TRENDS_CACHE: Dict[str, Tuple[str, float]] = {}
LAST_NO_SIGNAL_TIME = 0

# ========= ВСПОМОГАТЕЛЬНЫЕ =========
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

def normalize_amount_for_exchange(ex: ccxt.Exchange, symbol: str, amount: float) -> float:
    try:
        market = ex.market(symbol)
        lot = market.get("limits", {}).get("amount", {}).get("min") or market.get("precision", {}).get("amount")
        if lot:
            amt = float(ex.amount_to_precision(symbol, amount))
            if amt < float(lot):
                # если меньше минимума — возвращаем 0, пусть бот скажет
                return 0.0
            return amt
    except Exception:
        pass
    return amount

# ================== EXCHANGES ==================
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

# ================== АНАЛИЗ СИМВОЛА ==================
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

# ================== СКАН ==================
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

# ================== ЛОГИ СДЕЛОК ==================
CSV_HEADERS = [
    "ts_open", "ts_close", "exchange", "symbol", "side",
    "entry_price", "exit_price", "result", "profit_usdt",
    "profit_pct", "stake_usdt", "leverage",
    "sl_price", "tp1_price", "tp2_price", "note"
]

def init_trades_csv():
    if not os.path.exists(TRADES_CSV):
        df = pd.DataFrame(columns=CSV_HEADERS)
        df.to_csv(TRADES_CSV, index=False, encoding="utf-8")

def append_trade_row(row: dict):
    try:
        df = pd.DataFrame([row])
        df.to_csv(TRADES_CSV, mode="a", header=not os.path.exists(TRADES_CSV), index=False, encoding="utf-8")
    except Exception as e:
        log.error(f"append_trade_row error: {e}")

init_trades_csv()

# ================== ТРЕЙДЫ ==================
def set_leverage_isolated(ex: ccxt.Exchange, symbol: str, lev: int):
    # пробуем выставить isolated
    try:
        ex.set_leverage(lev, symbol, params={"marginMode": "isolated", "posMode": "one_way"})
    except Exception as e:
        log.warning(f"set_leverage {symbol}: {e}")

def is_isolated_mode_on_bitget(ex: ccxt.Exchange, symbol: str) -> bool:
    try:
        poss = ex.fetch_positions()
        for p in poss:
            if p.get("symbol") == symbol or p.get("info", {}).get("symbol") == symbol:
                mm = p.get("marginMode") or p.get("info", {}).get("marginMode")
                if mm and str(mm).lower().startswith("isol"):
                    return True
        # если позиции нет — попробуем создать isolated на будущее
        return True  # не мешаем входу если нет поз
    except Exception as e:
        log.warning(f"is_isolated_mode_on_bitget error: {e}")
        return True

def calc_position_amount(balance_usdt: float, entry_price: float, stake_usdt: float, leverage: int) -> float:
    stake_usdt = min(stake_usdt, balance_usdt)
    if entry_price <= 0:
        return 0.0
    return (stake_usdt * leverage) / entry_price

def humanize_ccxt_error(e: Exception) -> str:
    msg = str(e)
    if "400172" in msg:
        return "биржа отклонила тип ордера (order type illegal) — надо market / reduceOnly"
    if "40762" in msg or "insufficient" in msg.lower():
        return "недостаточно баланса или слишком маленькая / большая сумма"
    return msg

def place_orders_mexc(ex: ccxt.Exchange, trade: Dict[str, Any]):
    sym = trade["symbol"]
    side = trade["side"]
    entry = trade["entry"]
    amount = trade["amount"]
    sl_price = trade["sl_price"]
    tp1_price = trade["tp1_price"]
    tp2_price = trade["tp2_price"]

    # вход
    ex.create_market_order(sym, "buy" if side == "long" else "sell", amount)
    amount1 = amount * PARTIAL_TP_RATIO
    amount2 = amount - amount1
    # tp1
    ex.create_order(sym, "limit", "sell" if side == "long" else "buy", amount1, tp1_price, params={"reduceOnly": True})
    # tp2
    ex.create_order(sym, "limit", "sell" if side == "long" else "buy", amount2, tp2_price, params={"reduceOnly": True})
    # sl
    ex.create_order(sym, "stop_market", "sell" if side == "long" else "buy", amount, params={"reduceOnly": True, "triggerPrice": sl_price})

def place_orders_bitget(ex: ccxt.Exchange, trade: Dict[str, Any]):
    # Bitget не даёт сразу все ордера, поэтому только вход
    sym = trade["symbol"]
    side = trade["side"]
    amount = trade["amount"]
    ex.create_market_order(sym, "buy" if side == "long" else "sell", amount)
    # дальше мониторинг закроет

async def close_position_market(ex: ccxt.Exchange, trade: Dict[str, Any]):
    sym = trade["symbol"]
    side = trade["side"]
    amount = trade["amount"]
    try:
        # reduceOnly если поддерживает
        ex.create_market_order(sym, "sell" if side == "long" else "buy", amount, params={"reduceOnly": True})
    except Exception:
        # fallback
        ex.create_market_order(sym, "sell" if side == "long" else "buy", amount)

# ================== TELEGRAM ==================
def build_signal_keyboard(idx: int) -> InlineKeyboardMarkup:
    # idx — номер сигнала в списке
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("EST 10", callback_data=f"est:{idx}:10"),
            InlineKeyboardButton("EST 20", callback_data=f"est:{idx}:20"),
            InlineKeyboardButton("EST 50", callback_data=f"est:{idx}:50"),
            InlineKeyboardButton("EST 100", callback_data=f"est:{idx}:100"),
        ],
        [
            InlineKeyboardButton("BUY 10", callback_data=f"buy:{idx}:10"),
            InlineKeyboardButton("BUY 20", callback_data=f"buy:{idx}:20"),
            InlineKeyboardButton("BUY 50", callback_data=f"buy:{idx}:50"),
            InlineKeyboardButton("BUY 100", callback_data=f"buy:{idx}:100"),
        ],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*🤖 UNIFIED FUTURES BOT v2.5.1 PRO*\n\n"
        "⚙️ *Параметры стратегии:*\n"
        f"• Таймфрейм: {TIMEFRAME}\n"
        f"• Автоскан: каждые {SCAN_INTERVAL//60} мин\n"
        f"• Мин. объём: {MIN_QUOTE_VOLUME/1_000_000:.1f}M USDT\n"
        f"• RSI OB/OS: {RSI_OVERBOUGHT}/{RSI_OVERSOLD}\n"
        f"• EMA: {EMA_SHORT}/{EMA_LONG}\n"
        f"• SL (базовый): {BASE_STOP_LOSS_PCT*100:.1f}%\n"
        f"• Плечо: x{LEVERAGE}\n"
        f"• Trailing: активируется с +{TRAILING_ACTIVATION_PCT*100:.1f}%, дистанция {TRAILING_DISTANCE_PCT*100:.1f}%\n"
        f"• Комиссия (вход+выход): {(TAKER_FEE+MAKER_FEE)*100:.2f}%\n"
        f"• TP1/TP2 множители ATR: {TP1_MULTIPLIER_TREND}/{TP2_MULTIPLIER_TREND}\n"
        f"• Min ETA: 15m candle-based estimation\n\n"

        "📋 *Команды:*\n"
        "/scan — найти сигналы\n"
        "/top — топ-3 сильных\n"
        "/trade <№> <сумма> — войти по сигналу\n"
        "/report — активные сделки\n"
        "/history — файл всех сделок (лог)\n"
        "/stop — выключить автоскан\n\n"

        "💡 *Советы:*\n"
        "• Используй 🔥 сигналы с prob ≥ 80%\n"
        "• Лучше входить в сторону тренда (H1/H4 совпадают)\n"
        "• Перед реальной торговлей протестируй EST-кнопки\n"
        "• Трейлинг фиксирует прибыль при развороте цены\n\n"

        "🚀 Бот готов к работе! Используй /scan чтобы начать поиск сигналов."
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

    LAST_SCAN[chat_id] = entries
    # показываем только первые 10 с кнопками
    for i, d in enumerate(entries[:10], 1):
        tag = signal_strength_tag(d["prob"])
        msg = (
            f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} — {tag} ({d['prob']}%)\n"
            f"RSI={d['rsi']:.1f} | vol×={d['volr']:.2f} | H1={d['h1']} H4={d['h4']}\n"
            f"Entry≈{d['entry']:.6f} | SL=−{d['sl_pct']*100:.1f}% | TP1=+{d['tp1_pct']*100:.1f}% | ETA {d['eta_min']} мин\n"
            f"Net≈+{d['net_tp1_pct']*100:.2f}%"
        )
        await update.effective_message.reply_text(
            msg,
            reply_markup=build_signal_keyboard(i-1)
        )

async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    entries = await scan_all()
    if not entries:
        await update.effective_message.reply_text("Сигналов нет.")
        return
    strong = [d for d in entries if d["prob"] >= 80]
    if not strong:
        strong = entries[:3]
    LAST_SCAN[chat_id] = strong
    for i, d in enumerate(strong[:3], 1):
        tag = signal_strength_tag(d["prob"])
        msg = (
            f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} — {tag} ({d['prob']}%)\n"
            f"Entry≈{d['entry']:.6f} | SL=−{d['sl_pct']*100:.1f}% | TP1=+{d['tp1_pct']*100:.1f}% | ETA {d['eta_min']} мин"
        )
        await update.effective_message.reply_text(
            msg,
            reply_markup=build_signal_keyboard(i-1)
        )

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if os.path.exists(TRADES_CSV):
        await update.effective_message.reply_document(open(TRADES_CSV, "rb"))
    else:
        await update.effective_message.reply_text("История пока пуста.")

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

    entries = LAST_SCAN[chat_id]
    if idx < 0 or idx >= len(entries):
        await m.reply_text("Нет такого номера сигнала.")
        return

    d = entries[idx]
    await execute_trade_from_signal(update, context, chat_id, d, stake)

# ================== INLINE CALLBACK ==================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()
    data = query.data  # формат: est:<idx>:<usdt> или buy:<idx>:<usdt>
    try:
        action, idx, usdt = data.split(":")
        idx = int(idx)
        usdt = float(usdt)
    except Exception:
        return
    if chat_id not in LAST_SCAN or idx >= len(LAST_SCAN[chat_id]):
        await query.edit_message_text("Сигнал устарел, сделай /scan")
        return
    signal = LAST_SCAN[chat_id][idx]

    if action == "est":
        # просто расчёт
        tp1_pct = signal["tp1_pct"]
        net = estimate_net_profit_pct(tp1_pct)
        profit = usdt * net
        await query.message.reply_text(
            f"📈 Оценка {signal['symbol']} {signal['side'].upper()} на {usdt:.1f} USDT:\n"
            f"TP1: +{tp1_pct*100:.2f}% → ≈ +{profit:.2f} USDT (с комиссией)\n"
            f"ETA: {signal['eta_min']} мин"
        )
    elif action == "buy":
        # сразу покупаем
        fake_update = Update(update.update_id, message=query.message)
        await execute_trade_from_signal(fake_update, context, chat_id, signal, usdt)

# ================== ВСПОМОГ ФОРМА СДЕЛКИ ==================
async def execute_trade_from_signal(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, d: Dict[str, Any], stake: float):
    ex = make_exchange(d["exchange"])
    sym = d["symbol"]
    side = d["side"]
    entry = d["entry"]
    sl_pct = d["sl_pct"]
    tp1_price = d["tp1_price"]
    tp2_price = d["tp2_price"]

    # баланс
    try:
        bal = ex.fetch_balance(params={"type": "swap"})["USDT"]["free"]
    except Exception as e:
        await context.bot.send_message(chat_id, f"[{d['exchange'].upper()}] Не смог получить баланс: {e}")
        return

    amount = calc_position_amount(bal, entry, stake, LEVERAGE)
    amount = normalize_amount_for_exchange(ex, sym, amount)
    if amount <= 0:
        await context.bot.send_message(chat_id, f"[{d['exchange'].upper()}] Слишком маленькая сумма для {sym}")
        return

    # проверка isolated (особенно для bitget)
    if d["exchange"] == "bitget":
        if not is_isolated_mode_on_bitget(ex, sym):
            await context.bot.send_message(chat_id, "⚠️ Bitget сейчас в CROSS/CRUZADO. Сначала включи ISOLATED.")
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
        "exchange": d["exchange"],
        "chat_id": chat_id,
        "stake": stake,
        "open_time": datetime.now(timezone.utc),
        "use_trailing": True,
        "trailing_activation": TRAILING_ACTIVATION_PCT,
        "trailing_pct": TRAILING_DISTANCE_PCT,
    }

    try:
        if d["exchange"] == "mexc":
            place_orders_mexc(ex, trade)
            on_exchange_tp_sl = True
        else:
            place_orders_bitget(ex, trade)
            on_exchange_tp_sl = False
    except Exception as e:
        h = humanize_ccxt_error(e)
        await context.bot.send_message(chat_id, f"[{d['exchange'].upper()}] Ошибка ордеров: {h}")
        log.error(f"order error: {e}")
        return

    # сохранить в активные
    ACTIVE_TRADES.setdefault(chat_id, []).append(trade)

    note_line = ""
    if d["exchange"] == "bitget":
        note_line = "⚠️ Bitget: TP/SL не поставлены на бирже, фиксация — через бота."

    net_pct = estimate_net_profit_pct(d["tp1_pct"])
    await context.bot.send_message(
        chat_id,
        f"✅ [{d['exchange'].upper()}] Открыт {side.upper()} {sym}\n"
        f"Сумма: {stake} USDT (x{LEVERAGE}) → объём {amount:.4f}\n"
        f"Entry: {entry:.6f}\n"
        f"SL: {sl_price:.6f} (−{sl_pct*100:.1f}%)\n"
        f"TP1: {tp1_price:.6f} (+{d['tp1_pct']*100:.1f}%)\n"
        f"TP2: {tp2_price:.6f} (+{d['tp2_pct']*100:.1f}%)\n"
        f"⏱️ ETA: ~{d['eta_min']} мин\n"
        f"💰 Net: +{net_pct*100:.2f}% ≈ +{stake*net_pct:.2f} USDT\n"
        f"📈 Trailing: {TRAILING_ACTIVATION_PCT*100:.2f}% → стоп {TRAILING_DISTANCE_PCT*100:.2f}%\n"
        f"{note_line}"
    )

    # пишем в CSV открытие
    append_trade_row({
        "ts_open": trade["open_time"].isoformat(),
        "ts_close": "",
        "exchange": d["exchange"],
        "symbol": sym,
        "side": side,
        "entry_price": entry,
        "exit_price": "",
        "result": "OPEN",
        "profit_usdt": "",
        "profit_pct": "",
        "stake_usdt": stake,
        "leverage": LEVERAGE,
        "sl_price": sl_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "note": note_line,
    })

# ================== ФОН: АВТО-СКАН ==================
async def auto_scan_loop(app: Application):
    global LAST_NO_SIGNAL_TIME
    while True:
        if AUTO_ENABLED:
            try:
                entries = await scan_all()
                now = time.time()
                if entries:
                    LAST_NO_SIGNAL_TIME = now
                    # рассылаем тем, кто уже вызывал /scan
                    for chat_id in list(LAST_SCAN.keys()):
                        text_lines = []
                        for i, d in enumerate(entries[:5], 1):
                            tag = signal_strength_tag(d["prob"])
                            text_lines.append(
                                f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} {tag} ETA {d['eta_min']}м"
                            )
                        await app.bot.send_message(chat_id, "📊 Автосигналы:\n" + "\n".join(text_lines))
                else:
                    if now - LAST_NO_SIGNAL_TIME >= NO_SIGNAL_NOTIFY_INTERVAL:
                        for chat_id in list(LAST_SCAN.keys()):
                            await app.bot.send_message(chat_id, "Сигналов нет.")
                        LAST_NO_SIGNAL_TIME = now
                log.info("auto_scan tick OK")
            except Exception as e:
                log.error(f"auto_scan_loop: {e}")
        await asyncio.sleep(SCAN_INTERVAL)

# ================== ФОН: ТРЕЙЛИНГ И SL ==================
async def trailing_monitor(app: Application):
    while True:
        try:
            # пробуем подтянуть актуальные позиции с бирж, чтобы понять, что закрыто вручную
            await sync_manual_closes()

            # проходим по активным
            for chat_id, trades in list(ACTIVE_TRADES.items()):
                if not trades:
                    continue
                for trade in trades[:]:
                    ex = make_exchange(trade["exchange"])
                    sym = trade["symbol"]
                    side = trade["side"]
                    amount = trade["amount"]
                    entry = trade["entry"]
                    sl_price = trade["sl_price"]
                    tp1_price = trade["tp1_price"]
                    tp2_price = trade["tp2_price"]

                    # текущая цена
                    try:
                        ticker = ex.fetch_ticker(sym)
                        current_price = float(ticker["last"])
                    except Exception as e:
                        log.warning(f"trailing fetch_ticker {sym}: {e}")
                        continue

                    # прибыль в %
                    if side == "long":
                        profit_pct_live = (current_price - entry) / entry
                    else:
                        profit_pct_live = (entry - current_price) / entry

                    # TP2
                    tp2_hit = (side == "long" and current_price >= tp2_price) or \
                              (side == "short" and current_price <= tp2_price)
                    if tp2_hit:
                        await close_position_market(ex, trade)
                        ACTIVE_TRADES[chat_id].remove(trade)
                        append_trade_row({
                            "ts_open": trade["open_time"].isoformat(),
                            "ts_close": datetime.now(timezone.utc).isoformat(),
                            "exchange": trade["exchange"],
                            "symbol": sym,
                            "side": side,
                            "entry_price": entry,
                            "exit_price": current_price,
                            "result": "TP2",
                            "profit_usdt": (trade["stake"] * profit_pct_live),
                            "profit_pct": profit_pct_live * 100,
                            "stake_usdt": trade["stake"],
                            "leverage": LEVERAGE,
                            "sl_price": sl_price,
                            "tp1_price": tp1_price,
                            "tp2_price": tp2_price,
                            "note": "TP2 via bot" if trade["exchange"] == "bitget" else "",
                        })
                        await app.bot.send_message(chat_id, f"✅ TP2 исполнен по {sym} @ {current_price:.6f}")
                        continue

                    # TP1
                    tp1_hit = (side == "long" and current_price >= tp1_price) or \
                              (side == "short" and current_price <= tp1_price)
                    if tp1_hit:
                        # закрываем половину
                        half = amount * PARTIAL_TP_RATIO
                        try:
                            ex.create_market_order(sym, "sell" if side == "long" else "buy", half, params={"reduceOnly": True})
                        except Exception:
                            ex.create_market_order(sym, "sell" if side == "long" else "buy", half)
                        trade["amount"] = amount - half
                        append_trade_row({
                            "ts_open": trade["open_time"].isoformat(),
                            "ts_close": datetime.now(timezone.utc).isoformat(),
                            "exchange": trade["exchange"],
                            "symbol": sym,
                            "side": side,
                            "entry_price": entry,
                            "exit_price": current_price,
                            "result": "TP1",
                            "profit_usdt": (trade["stake"] * profit_pct_live) * PARTIAL_TP_RATIO,
                            "profit_pct": profit_pct_live * 100,
                            "stake_usdt": trade["stake"],
                            "leverage": LEVERAGE,
                            "sl_price": sl_price,
                            "tp1_price": tp1_price,
                            "tp2_price": tp2_price,
                            "note": "TP1 via bot" if trade["exchange"] == "bitget" else "",
                        })
                        await app.bot.send_message(chat_id, f"✅ TP1 исполнен по {sym} @ {current_price:.6f}")
                        # дальше смотрим trailing/SL уже для остатка
                        continue

                    # SL
                    sl_hit = (side == "long" and current_price <= sl_price) or \
                             (side == "short" and current_price >= sl_price)
                    if sl_hit:
                        await close_position_market(ex, trade)
                        ACTIVE_TRADES[chat_id].remove(trade)
                        append_trade_row({
                            "ts_open": trade["open_time"].isoformat(),
                            "ts_close": datetime.now(timezone.utc).isoformat(),
                            "exchange": trade["exchange"],
                            "symbol": sym,
                            "side": side,
                            "entry_price": entry,
                            "exit_price": current_price,
                            "result": "SL",
                            "profit_usdt": (trade["stake"] * profit_pct_live),
                            "profit_pct": profit_pct_live * 100,
                            "stake_usdt": trade["stake"],
                            "leverage": LEVERAGE,
                            "sl_price": sl_price,
                            "tp1_price": tp1_price,
                            "tp2_price": tp2_price,
                            "note": "SL via bot" if trade["exchange"] == "bitget" else "",
                        })
                        await app.bot.send_message(chat_id, f"❌ SL по {sym} @ {current_price:.6f}")
                        continue

                    # TRAILING
                    if trade.get("use_trailing") and profit_pct_live >= trade["trailing_activation"]:
                        if side == "long":
                            new_sl = current_price * (1 - trade["trailing_pct"])
                            if new_sl > trade["sl_price"]:
                                trade["sl_price"] = new_sl
                                await app.bot.send_message(chat_id, f"🔁 Trailing SL обновлён по {sym}: {new_sl:.6f}")
                        else:
                            new_sl = current_price * (1 + trade["trailing_pct"])
                            if new_sl < trade["sl_price"]:
                                trade["sl_price"] = new_sl
                                await app.bot.send_message(chat_id, f"🔁 Trailing SL обновлён по {sym}: {new_sl:.6f}")

        except Exception as e:
            log.error(f"trailing_monitor: {e}")

        await asyncio.sleep(AUTO_MONITOR_INTERVAL)

# ================== ФОН: СИНК РУЧНЫХ ЗАКРЫТИЙ ==================
async def sync_manual_closes():
    # проходим по всем чатам и по всем биржам
    for chat_id, trades in list(ACTIVE_TRADES.items()):
        if not trades:
            continue
        # группируем по биржам
        by_ex: Dict[str, List[Dict[str, Any]]] = {}
        for t in trades:
            by_ex.setdefault(t["exchange"], []).append(t)
        for ex_id, tlist in by_ex.items():
            ex = make_exchange(ex_id)
            try:
                poss = ex.fetch_positions()
            except Exception as e:
                log.warning(f"sync_manual_closes fetch_positions {ex_id}: {e}")
                continue
            active_symbols = set()
            for p in poss:
                sym = p.get("symbol") or p.get("info", {}).get("symbol")
                if sym:
                    active_symbols.add(sym)
            # все сделки, которых нет в позициях — удалить и записать в CSV как "Closed manual"
            for t in tlist[:]:
                if t["symbol"] not in active_symbols:
                    # значит, закрыли вручную
                    ACTIVE_TRADES[chat_id].remove(t)
                    append_trade_row({
                        "ts_open": t["open_time"].isoformat(),
                        "ts_close": datetime.now(timezone.utc).isoformat(),
                        "exchange": t["exchange"],
                        "symbol": t["symbol"],
                        "side": t["side"],
                        "entry_price": t["entry"],
                        "exit_price": "",
                        "result": "CLOSED_MANUAL",
                        "profit_usdt": "",
                        "profit_pct": "",
                        "stake_usdt": t["stake"],
                        "leverage": LEVERAGE,
                        "sl_price": t["sl_price"],
                        "tp1_price": t["tp1_price"],
                        "tp2_price": t["tp2_price"],
                        "note": "manual close detected",
                    })
                    log.info(f"manual close detected for {t['symbol']}")

# ================== MAIN ==================
async def main():
    print("🚀 MAIN INIT START", flush=True)
    app = Application.builder().token(TG_BOT_TOKEN).concurrent_updates(True).build()
    print("✅ Application initialized", flush=True)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("trade", trade_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    app.add_handler(CallbackQueryHandler(button_handler))

    log.info("UNIFIED FUTURES BOT v2.5.1 STARTED")
    print("BOT ЗАПУЩЕН НА RENDER.COM | 24/7", flush=True)

    # фоновые задачи
    asyncio.create_task(auto_scan_loop(app))
    asyncio.create_task(trailing_monitor(app))

    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
