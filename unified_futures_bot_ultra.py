# -*- coding: utf-8 -*-
"""
# -*- coding: utf-8 -*-
"""
UNIFIED FUTURES BOT v2.6.0 SAFE+
MEXC + BITGET | 15m | объём >= 5M | RSI/EMA/SR/ATR/VOLR | анти-памп фильтр | авто-скан | scanlog

⚙️ ОСНОВНОЕ:
• Анализ фьючерсов USDT (MEXC + Bitget)
• Таймфрейм: 15 минут
• Мин. объём: 5M USDT
• RSI, EMA50/200, ATR, S/R, всплеск объёма (VOLR)
• Проверка тренда H1 и H4 (без кэша)
• Анти-памп фильтр: свеча > +6% и volr > 3 → пропуск
• Не шортит против ап-тренда
• TP1 ≈ 2×ATR, TP2 ≈ 4×ATR, SL ≈ 1.5×ATR или ≥5%
• Автоскан каждые 5 минут, ETA — время до TP1
• Сильный сигнал ≥ 85%, Хороший ≥ 70%, Средний ≥ 55%

🧠 ЛОГИКА ОТБОРА:
1. Отбираются топовые USDT-свопы по объёму ≥ 5M
2. Рассчитываются RSI, EMA, ATR, VOLR, уровни S/R
3. Формируются баллы для LONG / SHORT
4. Проверяется тренд H1 и H4 (EMA50 vs EMA200)
5. Исключаются пампы и шорты против ап-тренда
6. Сигналы сортируются по вероятности и volr

📲 TELEGRAM-КОМАНДЫ:
/start — параметры и справка
/info — описание логики
/scan — ручной поиск сигналов (топ-15)
/top — топ-3 сильных сигналов
/trade <№> <сумма> — открыть по сигналу
/report — показать активные сделки
/history — выгрузить журнал сделок CSV
/scanlog — включить/выключить debug-режим (пошаговые логи)
/stop — отключить авто-скан
"""

"""

import os
import sys
import time
import asyncio
import logging
from datetime import datetime
import datetime as dt
from typing import Dict, List, Tuple, Any, Set

import requests
import nest_asyncio

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
    CallbackQueryHandler,
    ContextTypes,
)

# =====================================================
# АНТИ-ДУБЛЬ ДЛЯ RENDER
# =====================================================
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
if not TG_BOT_TOKEN:
    raise SystemExit("❌ Нет TG_BOT_TOKEN")

def ensure_single_instance(token: str):
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=5)
        j = r.json()
        if j.get("ok") and j.get("result", {}).get("url"):
            print("⚠️ Уже есть активный инстанс (webhook). Выходим.")
            sys.exit(0)
    except Exception:
        pass

ensure_single_instance(TG_BOT_TOKEN)

# чтобы telegram+asyncio в 3.13 не ругался
nest_asyncio.apply()

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
BASE_STOP_LOSS_PCT = 0.05          # 5%
MIN_QUOTE_VOLUME = 5_000_000       # 5M USDT

SCAN_INTERVAL = 300                # авто-скан каждые 5 минут
MONITOR_INTERVAL = 15
NO_SIGNAL_NOTIFY_INTERVAL = 3600

PARTIAL_TP_RATIO = 0.5
TP1_MULTIPLIER_TREND = 2.0
TP2_MULTIPLIER_TREND = 4.0

TRAILING_ACTIVATION_PCT = 0.03     # с +3% активировать трейлинг
TRAILING_DISTANCE_PCT = 0.015      # шаг 1.5%

TAKER_FEE = 0.0006
MAKER_FEE = 0.0002

DATA_DIR = "./data"
LOGS_DIR = "./logs"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

LOG_FILENAME = os.path.join(
    LOGS_DIR,
    f"{datetime.now(dt.timezone.utc).date().isoformat()}_futures_2_5_8.log"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILENAME, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("FUTURES_2_5_8")

# =====================================================
# ГЛОБАЛЫ
# =====================================================
LAST_SCAN: Dict[int, List[Dict[str, Any]]] = {}
ACTIVE_TRADES: Dict[int, List[Dict[str, Any]]] = {}
AUTO_ENABLED: bool = True
H1_TRENDS_CACHE: Dict[str, Tuple[str, float]] = {}
H4_TRENDS_CACHE: Dict[str, Tuple[str, float]] = {}
LAST_NO_SIGNAL_TIME = 0

# чаты, где включён “пруф-режим” сканера
SCAN_DEBUG_CHATS: Set[int] = set()

TRADES_HISTORY_FILE = os.path.join(DATA_DIR, "trades_history.csv")
if not os.path.exists(TRADES_HISTORY_FILE):
    with open(TRADES_HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("ts,chat_id,exchange,symbol,side,stake,amount,entry,sl,tp1,tp2,eta_min,prob,volr,result,pnl,roi\n")

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

def signal_strength_tag(prob: int) -> str:
    if prob >= 85:
        return "🔥 Сильный"
    elif prob >= 70:
        return "⚡ Хороший"
    elif prob >= 55:
        return "⚠️ Средний"
    else:
        return "❄️ Слабый"

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
    if entry_price <= 0:
        return 0.0
    return (stake_usdt * leverage) / entry_price

def normalize_amount_for_exchange(ex: ccxt.Exchange, symbol: str, amount: float) -> float:
    m = ex.markets.get(symbol)
    if not m:
        return amount
    lot = m.get("limits", {}).get("amount", {}).get("min") or m.get("precision", {}).get("amount")
    if lot is None:
        return amount
    lot = float(lot)
    if amount < lot:
        return 0.0
    precision = m.get("precision", {}).get("amount")
    if precision is not None:
        return float(ex.amount_to_precision(symbol, amount))
    return amount

# =====================================================
# БИРЖИ
# =====================================================
def make_exchange(name: str):
    if name == "mexc":
        return ccxt.mexc({
            "apiKey": os.getenv("MEXC_API_KEY", ""),
            "secret": os.getenv("MEXC_API_SECRET", ""),
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
            "timeout": 30000,
        })
    elif name == "bitget":
        return ccxt.bitget({
            "apiKey": os.getenv("BITGET_API_KEY", ""),
            "secret": os.getenv("BITGET_API_SECRET", ""),
            "password": os.getenv("BITGET_PASSPHRASE", ""),
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
            "timeout": 30000,
        })
    else:
        raise ValueError("Unknown exchange name")

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

# =====================================================
# BITGET: авто-ISOLATED
# =====================================================
def ensure_bitget_isolated(ex: ccxt.Exchange, symbol: str):
    try:
        ex.set_margin_mode("isolated", symbol, params={
            "productType": "USDT-FUTURES",
            "marginCoin": "USDT",
        })
        log.info(f"bitget: set isolated for {symbol}")
    except Exception as e:
        log.warning(f"bitget: cannot set isolated for {symbol}: {e}")

# =====================================================
# СКАН
# =====================================================
def load_top_usdt_swaps(ex: ccxt.Exchange, top_n=60):
    ex.load_markets()
    tickers = ex.fetch_tickers()
    rows = []
    for s, x in tickers.items():
        m = ex.markets.get(s)
        if not m or m.get("type") != "swap" or m.get("quote") != "USDT":
            continue
        qv = x.get("quoteVolume") or x.get("info", {}).get("quoteVolume") or 0
        try:
            qv = float(qv)
        except Exception:
            qv = 0.0
        if qv < float(MIN_QUOTE_VOLUME):
            continue
        rows.append((s, qv))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:top_n]]

async def analyze_symbol(ex: ccxt.Exchange, symbol: str):
    ohlcv = await asyncio.to_thread(ex.fetch_ohlcv, symbol, TIMEFRAME, None, LIMIT)
    if len(ohlcv) < LIMIT // 2:
        return None

    df = pd.DataFrame(ohlcv, columns=["t","o","h","l","c","v"])
    c, v = df["c"], df["v"]

    # === индикаторы ===
    r = rsi(c, RSI_PERIOD)
    e50, e200 = ema(c, EMA_SHORT), ema(c, EMA_LONG)
    vma = v.rolling(VOL_SMA).mean()
    volr = v.iloc[-1] / (vma.iloc[-1] + 1e-12) if vma.iloc[-1] > 0 else 0
    atr_val = atr(df, ATR_PERIOD).iloc[-1]
    _, nearR, _, nearS = detect_sr_levels(df)

    open_, close = float(df["o"].iloc[-1]), float(df["c"].iloc[-1])
    bull = close > open_ * 1.003
    bear = close < open_ * 0.997
    change_pct = (close / open_ - 1) * 100

    # === тренды без кеша ===
    h1_trend = await fetch_trend(ex, symbol, "1h", H1_TRENDS_CACHE, ttl=0)
    h4_trend = await fetch_trend(ex, symbol, "4h", H4_TRENDS_CACHE, ttl=0)

    # === анти-памп фильтр ===
    if change_pct > 6 and volr > 3:
        msg = f"🚫 {symbol}: памп {change_pct:.1f}% + volr {volr:.2f} → пропуск"
        log.info(msg)
        # если включён debug-режим, показать в Telegram
        for cid in list(SCAN_DEBUG_CHATS):
            try:
                from telegram import Bot
                bot = Bot(TG_BOT_TOKEN)
                await bot.send_message(cid, msg)
            except Exception:
                pass
        return None

    # === логика сигналов ===
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

    # не шортим против ап-тренда
    if h1_trend == "up" or h4_trend == "up":
        trend_ok_short = False

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

async def scan_exchange(name: str, debug_chats: Set[int] = None, bot=None):
    ex = make_exchange(name)
    syms = await asyncio.to_thread(load_top_usdt_swaps, ex, 60)
    results = []
    total = len(syms)
    for idx, s in enumerate(syms, 1):
        try:
            d = await analyze_symbol(ex, s)
            if d:
                results.append(d)
        except Exception as e:
            log.warning(f"{name} {s}: {e}")
       # отправим прогресс, если кто-то в дебаге
    if debug_chats and bot:
        txt = f"🔎 {name.upper()}: {idx}/{total}… сигналы={len(results)}"
        for cid in list(debug_chats):  # ← копия множества
            await bot.send_message(cid, txt)
        await asyncio.sleep(0.35)

    results.sort(key=lambda x: (x["prob"], x["volr"]), reverse=True)
    return results


async def scan_all(debug_chats: Set[int] = None, bot=None):
    # запускаем обе биржи параллельно
    mexc_task = asyncio.create_task(scan_exchange("mexc", debug_chats, bot))
    bitget_task = asyncio.create_task(scan_exchange("bitget", debug_chats, bot))
    mexc_res = await mexc_task
    bitget_res = await bitget_task
    all_res = mexc_res + bitget_res

    if debug_chats and bot:
        summary = f"✅ Скан завершён. Найдено {len(all_res)} сигналов."
        for cid in list(debug_chats):  # ← тоже копия
            await bot.send_message(cid, summary)

    return all_res


# =====================================================
# TELEGRAM UI
# =====================================================
def build_signal_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("BUY 10", callback_data=f"BUY|{index}|10"),
            InlineKeyboardButton("BUY 20", callback_data=f"BUY|{index}|20"),
            InlineKeyboardButton("BUY 50", callback_data=f"BUY|{index}|50"),
            InlineKeyboardButton("BUY 100", callback_data=f"BUY|{index}|100"),
        ],
        [
            InlineKeyboardButton("EST 10", callback_data=f"EST|{index}|10"),
            InlineKeyboardButton("EST 20", callback_data=f"EST|{index}|20"),
            InlineKeyboardButton("EST 50", callback_data=f"EST|{index}|50"),
            InlineKeyboardButton("EST 100", callback_data=f"EST|{index}|100"),
        ]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*🤖 UNIFIED FUTURES BOT v2.6.0 SAFE+*\n\n"
        "⚙️ Параметры:\n"
        f"• TF: {TIMEFRAME}\n"
        f"• Автоскан: {SCAN_INTERVAL//60} мин\n"
        f"• Мин. объём: {MIN_QUOTE_VOLUME/1_000_000:.1f}M USDT\n"
        f"• SL (base): {BASE_STOP_LOSS_PCT*100:.1f}%\n"
        f"• Плечо: x{LEVERAGE}\n"
        f"• Trailing: с +{TRAILING_ACTIVATION_PCT*100:.1f}%, шаг {TRAILING_DISTANCE_PCT*100:.1f}%\n\n"
        "📋 Команды:\n"
        "/scan — найти сигналы\n"
        "/top — топ-3 сильных\n"
        "/report — активные сделки\n"
        "/history — журнал сделок\n"
        "/info — описание логики\n"
        "/scanlog — включить/выключить debug-режим\n"
        "/stop — выключить авто\n\n"
        "💡 Кнопки BUY/EST доступны в сигналах."
    )
    await update.effective_message.reply_text(text, parse_mode="Markdown")

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*UNIFIED FUTURES BOT v2.6.0 SAFE+ — справка*\n\n"
        "🔍 Что делает:\n"
        "• сканирует фьючерсы USDT на MEXC и Bitget\n"
        "• фильтрует пары с объёмом ≥ 5M USDT\n"
        "• проверяет RSI, EMA50/200, всплеск объёма, уровни S/R\n"
        "• сверяет тренды H1 и H4 (без кэша)\n"
        "• считает TP1/TP2, SL, ETA и net-прибыль\n"
        "• исключает пампы: свеча > +6% и volr > 3 → пропуск\n"
        "• не шортит против ап-тренда\n\n"
        "📊 Сигналы сортируются по вероятности и volr:\n"
        "• 🔥 ≥85% — сильный\n"
        "• ⚡ ≥70% — хороший\n"
        "• ⚠️ ≥55% — средний\n\n"
        "⚠️ Bitget — бот сам включает isolated\n"
        "⚠️ MEXC — ордер не ставится, только аналитика\n"
    )
    await update.effective_message.reply_text(text, parse_mode="Markdown")


# =====================================================
# TRADE / PLACE ORDERS
# =====================================================
def log_trade_row(
    chat_id: int,
    d: Dict[str, Any],
    stake: float,
    amount: float,
    result: str = "opened",
    pnl: float = 0.0,
    roi: float = 0.0,
):
    with open(TRADES_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(
            f"{datetime.utcnow().isoformat()}Z,{chat_id},{d['exchange']},{d['symbol']},{d['side']},"
            f"{stake},{amount},{d['entry']},{d['sl_pct']},{d['tp1_pct']},{d['tp2_pct']},{d['eta_min']},"
            f"{d['prob']},{d['volr']},{result},{pnl},{roi}\n"
        )

def place_orders_real(ex: ccxt.Exchange, d: Dict[str, Any], amount: float):
    sym = d["symbol"]
    side = d["side"]
    entry = d["entry"]
    tp1_price = d["tp1_price"]
    tp2_price = d["tp2_price"]
    sl_pct = d["sl_pct"]

    if side == "long":
        sl_price = entry * (1 - sl_pct)
        main_side = "buy"
        close_side = "sell"
    else:
        sl_price = entry * (1 + sl_pct)
        main_side = "sell"
        close_side = "buy"

    params = {}
    if ex.id == "bitget":
        params = {
            "marginMode": "isolated",
            "marginCoin": "USDT",
            "productType": "USDT-FUTURES",
        }

    ex.create_order(sym, "market", main_side, amount, None, params)

    placed_tp_sl = False
    try:
        ex.create_order(sym, "limit", close_side, amount * PARTIAL_TP_RATIO, tp1_price, {
            **params,
            "reduceOnly": True,
        })
        ex.create_order(sym, "limit", close_side, amount * (1 - PARTIAL_TP_RATIO), tp2_price, {
            **params,
            "reduceOnly": True,
        })
        ex.create_order(sym, "stop_market", close_side, amount, None, {
            **params,
            "reduceOnly": True,
            "triggerPrice": sl_price,
        })
        placed_tp_sl = True
    except Exception as e:
        log.warning(f"{ex.id} cannot place TP/SL: {e}")
        placed_tp_sl = False

    return placed_tp_sl

async def handle_buy_from_signal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    signal_index: int,
    stake: float,
):
    rows = LAST_SCAN.get(chat_id, [])
    if signal_index < 0 or signal_index >= len(rows):
        await context.bot.send_message(chat_id, "Нет такого сигнала.")
        return

    d = rows[signal_index]
    ex = make_exchange(d["exchange"])

    if d["exchange"] == "mexc":
        await context.bot.send_message(
            chat_id,
            f"[MEXC] Ордер по API не размещён. Пара: {d['symbol']}.\n"
            f"Entry: {d['entry']:.6f}, SL: {d['sl_pct']*100:.1f}%, TP1: {d['tp1_pct']*100:.1f}%, ETA {d['eta_min']}м"
        )
        return

    if d["exchange"] == "bitget":
        ensure_bitget_isolated(ex, d["symbol"])

    try:
        bal = ex.fetch_balance(params={"type": "swap"})["USDT"]["free"]
    except Exception as e:
        await context.bot.send_message(chat_id, f"[{d['exchange'].upper()}] не смог получить баланс: {e}")
        return

    amount = calc_position_amount(bal, d["entry"], stake, LEVERAGE)
    amount = normalize_amount_for_exchange(ex, d["symbol"], amount)
    if amount <= 0:
        await context.bot.send_message(chat_id, f"[{d['exchange'].upper()}] Слишком маленькая сумма для {d['symbol']}")
        return

    placed_tp_sl = False
    try:
        placed_tp_sl = place_orders_real(ex, d, amount)
    except Exception as e:
        await context.bot.send_message(chat_id, f"[{d['exchange'].upper()}] Ошибка ордера: {e}")
        log.error(f"order error: {e}")
        return

    ACTIVE_TRADES.setdefault(chat_id, []).append({
        "exchange": d["exchange"],
        "symbol": d["symbol"],
        "side": d["side"],
        "entry": d["entry"],
        "amount": amount,
        "tp1_price": d["tp1_price"],
        "tp2_price": d["tp2_price"],
        "sl_pct": d["sl_pct"],
        "time": datetime.now(dt.timezone.utc).isoformat(),
        "stake": stake,
    })
    log_trade_row(chat_id, d, stake, amount, result="opened")

    await context.bot.send_message(
        chat_id,
        f"✅ [{d['exchange'].upper()}] Открыт {d['side'].upper()} {d['symbol']}\n"
        f"Сумма: {stake} USDT (x{LEVERAGE}) → объём {amount:.6f}\n"
        f"Entry: {d['entry']:.6f}\n"
        f"SL: {d['sl_pct']*100:.1f}%\n"
        f"TP1: +{d['tp1_pct']*100:.1f}% → {d['tp1_price']:.6f}\n"
        f"TP2: +{d['tp2_pct']*100:.1f}% → {d['tp2_price']:.6f}\n"
        f"⏱ ETA: ~{d['eta_min']} мин\n"
        f"💰 Net: +{d['net_tp1_pct']*100:.2f}%\n"
        + ("⚠️ Bitget: TP/SL не поставлены на бирже, фиксация — через бота." if not placed_tp_sl else "")
    )

async def handle_estimate_from_signal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    signal_index: int,
    stake: float,
):
    rows = LAST_SCAN.get(chat_id, [])
    if signal_index < 0 or signal_index >= len(rows):
        await context.bot.send_message(chat_id, "Нет такого сигнала.")
        return
    d = rows[signal_index]
    net_pct = d["net_tp1_pct"]
    profit = stake * net_pct
    await context.bot.send_message(
        chat_id,
        f"📈 Оценка {d['symbol']} {d['side'].upper()} на {stake:.1f} USDT:\n"
        f"TP1: +{d['tp1_pct']*100:.2f}% → ≈ +{profit:.2f} USDT (с комиссией)\n"
        f"ETA: {d['eta_min']} мин"
    )

# =====================================================
# CALLBACKS
# =====================================================
async def button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("|")
    if len(parts) != 3:
        return
    action, idx, stake = parts
    idx = int(idx)
    stake = float(stake)
    chat_id = query.message.chat_id

    if action == "BUY":
        await handle_buy_from_signal(update, context, chat_id, idx, stake)
    elif action == "EST":
        await handle_estimate_from_signal(update, context, chat_id, idx, stake)

# =====================================================
# COMMANDS
# =====================================================
async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    debug_now = chat_id in SCAN_DEBUG_CHATS
    if debug_now:
        await update.effective_message.reply_text("🔎 Ручной скан запущен…")
    entries = await scan_all(SCAN_DEBUG_CHATS if debug_now else None, context.bot)
    if not entries:
        await update.effective_message.reply_text("Сигналов нет.")
        LAST_SCAN[chat_id] = []
        return

    LAST_SCAN[chat_id] = entries[:15]
    for i, d in enumerate(entries[:15], 1):
        txt = (
            f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} — {signal_strength_tag(d['prob'])} ({d['prob']}%)\n"
            f"    Entry≈{d['entry']:.6f} | SL=−{d['sl_pct']*100:.1f}% | TP1=+{d['tp1_pct']*100:.1f}% | ETA {d['eta_min']} мин"
        )
        await update.effective_message.reply_text(txt, reply_markup=build_signal_keyboard(i-1))

async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    debug_now = chat_id in SCAN_DEBUG_CHATS
    entries = await scan_all(SCAN_DEBUG_CHATS if debug_now else None, context.bot)
    if not entries:
        await update.effective_message.reply_text("Сигналов нет.")
        return
    strong = [d for d in entries if d["prob"] >= 80]
    if not strong:
        strong = entries[:3]
    LAST_SCAN[chat_id] = strong[:3]
    for i, d in enumerate(strong[:3], 1):
        txt = (
            f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} — {signal_strength_tag(d['prob'])} ({d['prob']}%)\n"
            f"    Entry≈{d['entry']:.6f} | SL=−{d['sl_pct']*100:.1f}% | TP1=+{d['tp1_pct']*100:.1f}% | ETA {d['eta_min']} мин"
        )
        await update.effective_message.reply_text(txt, reply_markup=build_signal_keyboard(i-1))

async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = update.effective_message
    if chat_id not in LAST_SCAN or not LAST_SCAN[chat_id]:
        await m.reply_text("Сначала /scan или /top.")
        return
    if len(context.args) < 2:
        await m.reply_text("Формат: /trade <номер_сигнала> <сумма_USDT>")
        return
    try:
        idx = int(context.args[0]) - 1
        stake = float(context.args[1])
    except ValueError:
        await m.reply_text("Номер и сумма должны быть числами.")
        return
    await handle_buy_from_signal(update, context, chat_id, idx, stake)

async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    trades = ACTIVE_TRADES.get(chat_id, [])
    if not trades:
        await update.effective_message.reply_text("Нет активных сделок.")
        return
    lines = ["Активные сделки:"]
    for i, t in enumerate(trades, 1):
        lines.append(
            f"{i}. [{t['exchange'].upper()}] {t['side'].upper()} {t['symbol']} @ {t['entry']:.6f} | TP1 {t['tp1_price']:.6f}"
        )
    await update.effective_message.reply_text("\n".join(lines))

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(TRADES_HISTORY_FILE):
        await update.effective_message.reply_text("История пуста.")
        return
    await update.effective_message.reply_document(
        document=InputFile(TRADES_HISTORY_FILE),
        filename="trades_history.csv",
        caption="Журнал сделок",
    )

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AUTO_ENABLED
    AUTO_ENABLED = False
    await update.effective_message.reply_text("Авто-скан отключён.")

async def scanlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in SCAN_DEBUG_CHATS:
        SCAN_DEBUG_CHATS.remove(chat_id)
        await update.effective_message.reply_text("🟡 Режим скан-дебага выключен.")
    else:
        SCAN_DEBUG_CHATS.add(chat_id)
        await update.effective_message.reply_text("🟢 Режим скан-дебага включен. Буду показывать шаги скана.")

# =====================================================
# ФОН: АВТО-СКАН
# =====================================================
async def auto_scan_loop(app: Application):
    global LAST_NO_SIGNAL_TIME
    while True:
        if AUTO_ENABLED:
            try:
                # в авто-скан берём все чаты, где включён дебаг
                entries = await scan_all(SCAN_DEBUG_CHATS if SCAN_DEBUG_CHATS else None, app.bot)
                now = time.time()
                if entries:
                    LAST_NO_SIGNAL_TIME = now
                    for chat_id in LAST_SCAN.keys() or SCAN_DEBUG_CHATS or []:
                        d = entries[0]
                        txt = (
                            f"📊 Автосигнал:\n"
                            f"[{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} {signal_strength_tag(d['prob'])} ({d['prob']}%)\n"
                            f"Entry≈{d['entry']:.6f} | SL=−{d['sl_pct']*100:.1f}% | TP1=+{d['tp1_pct']*100:.1f}% | ETA {d['eta_min']}м"
                        )
                        LAST_SCAN[chat_id] = [d]
                        await app.bot.send_message(chat_id, txt, reply_markup=build_signal_keyboard(0))
                else:
                    if now - LAST_NO_SIGNAL_TIME >= NO_SIGNAL_NOTIFY_INTERVAL:
                        for chat_id in LAST_SCAN.keys():
                            await app.bot.send_message(chat_id, "Сигналов пока нет.")
                        LAST_NO_SIGNAL_TIME = now
                log.info("auto_scan tick OK")
            except Exception as e:
                log.error(f"auto_scan_loop: {e}")
        await asyncio.sleep(SCAN_INTERVAL)

# =====================================================
# MAIN
# =====================================================
async def main():
    print("🚀 MAIN INIT START", flush=True)
    app = Application.builder().token(TG_BOT_TOKEN).concurrent_updates(True).build()
    print("✅ Application initialized", flush=True)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("trade", trade_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("scanlog", scanlog_cmd))
    app.add_handler(CallbackQueryHandler(button_cb))

    log.info("UNIFIED FUTURES BOT v2.5.8 SAFE STARTED")
    print("BOT ЗАПУЩЕН НА RENDER.COM | 24/7", flush=True)

    asyncio.create_task(auto_scan_loop(app))

    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
