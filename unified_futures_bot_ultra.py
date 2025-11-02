# -*- coding: utf-8 -*-
"""
unified_futures_bot_ultra.py
v2.5.6 SAFE
MEXC + Bitget | 24/7 | x5 | кнопки BUY/EST | лог сделок
"""

import os
import sys
import asyncio
import logging
import time
from datetime import datetime
import datetime as dt
from typing import Dict, List, Tuple, Any, Optional

import ccxt
import pandas as pd
import numpy as np

# telegram
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# nest_asyncio для Render/Python 3.13
import nest_asyncio
nest_asyncio.apply()

# =====================================================
# ENV
# =====================================================
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")

if not TG_BOT_TOKEN:
    raise SystemExit("TG_BOT_TOKEN is required")

# =====================================================
# anti-double (чтобы второй процесс на Render не лез в polling)
# =====================================================
def ensure_single_instance(token: str):
    import requests
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getWebhookInfo",
            timeout=5,
        )
        data = r.json()
        # если есть URL вебхука — бот где-то уже живёт → выходим
        if data.get("ok") and data.get("result", {}).get("url"):
            print("⚠️ Bot already has webhook, exit to avoid double polling", flush=True)
            sys.exit(0)
    except Exception as e:
        # молча продолжаем — лучше пусть работает
        print(f"[anti-double] warning: {e}", flush=True)


ensure_single_instance(TG_BOT_TOKEN)

# =====================================================
# PATHS / LOGS
# =====================================================
BASE_DIR = os.getcwd()
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILENAME = os.path.join(
    LOG_DIR,
    f"{datetime.now(dt.timezone.utc).date().isoformat()}_v2.5.6.log"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILENAME, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("UFuturesBot")

TRADES_CSV = os.path.join(DATA_DIR, "trades.csv")
if not os.path.exists(TRADES_CSV):
    with open(TRADES_CSV, "w", encoding="utf-8") as f:
        f.write("ts,exchange,symbol,side,entry,sl,tp1,tp2,stake,amount,eta,prob,volr,rsi,result,pnl\n")

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
BASE_STOP_LOSS_PCT = 0.05           # 5%
MIN_QUOTE_VOLUME = 5_000_000        # 5M USDT

SCAN_INTERVAL = 300                 # 5 минут
AUTO_MONITOR_INTERVAL = 12          # сек — мониторинг открытых
NO_SIGNAL_NOTIFY_INTERVAL = 3600

TRAILING_ACTIVATION_PCT = 0.03      # 3%
TRAILING_DISTANCE_PCT = 0.015       # 1.5%

TAKER_FEE = 0.0006
MAKER_FEE = 0.0002

# =====================================================
# ГЛОБАЛЫ
# =====================================================
LAST_SCAN: Dict[int, List[Tuple]] = {}
ACTIVE_TRADES: Dict[int, List[Dict[str, Any]]] = {}
AUTO_ENABLED = True
LAST_NO_SIGNAL_TIME = 0

H1_TRENDS_CACHE: Dict[str, Tuple[str, float]] = {}
H4_TRENDS_CACHE: Dict[str, Tuple[str, float]] = {}

# =====================================================
# HELPERS
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

# =====================================================
# EXCHANGES
# =====================================================
def make_exchange(name: str) -> ccxt.Exchange:
    if name == "mexc":
        return ccxt.mexc({
            "apiKey": MEXC_API_KEY,
            "secret": MEXC_API_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
    elif name == "bitget":
        return ccxt.bitget({
            "apiKey": BITGET_API_KEY,
            "secret": BITGET_API_SECRET,
            "password": BITGET_PASSPHRASE,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
    else:
        raise ValueError("unknown exchange")

# =====================================================
# TREND CACHE
# =====================================================
async def fetch_trend(ex: ccxt.Exchange, symbol: str, tf: str,
                      cache: Dict[str, Tuple[str, float]], ttl: int = 3600) -> str:
    now = time.time()
    key = f"{ex.id}:{symbol}:{tf}"
    if key in cache and now - cache[key][1] < ttl:
        return cache[key][0]
    try:
        ohlcv = await asyncio.to_thread(ex.fetch_ohlcv, symbol, tf, None, 200)
        df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
        e50, e200 = ema(df["c"], 50), ema(df["c"], 200)
        trend = "up" if e50.iloc[-1] > e200.iloc[-1] else "down" if e50.iloc[-1] < e200.iloc[-1] else "flat"
        cache[key] = (trend, now)
        return trend
    except Exception:
        return "flat"

# =====================================================
# MARKET SCAN HELPERS
# =====================================================
def safe_float(x) -> float:
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(str(x))
    except Exception:
        return 0.0

def load_top_usdt_swaps(ex: ccxt.Exchange, top_n=60) -> List[str]:
    ex.load_markets()
    tickers = ex.fetch_tickers()
    rows = []
    for s, t in tickers.items():
        m = ex.markets.get(s)
        if not m:
            continue
        if m.get("type") != "swap":
            continue
        if m.get("quote") != "USDT":
            continue
        qv = (
            t.get("quoteVolume")
            or t.get("info", {}).get("quoteVolume")
            or t.get("info", {}).get("amount")
            or 0
        )
        qv = safe_float(qv)
        if qv < MIN_QUOTE_VOLUME:
            continue
        rows.append((s, qv))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:top_n]]

async def analyze_symbol(ex: ccxt.Exchange, symbol: str):
    ohlcv = await asyncio.to_thread(ex.fetch_ohlcv, symbol, TIMEFRAME, None, LIMIT)
    if len(ohlcv) < 50:
        return None
    df = pd.DataFrame(ohlcv, columns=["t","o","h","l","c","v"])
    c, v = df["c"], df["v"]

    r = rsi(c, RSI_PERIOD)
    e50, e200 = ema(c, EMA_SHORT), ema(c, EMA_LONG)
    vma = v.rolling(VOL_SMA).mean()
    volr = v.iloc[-1] / (vma.iloc[-1] + 1e-12) if vma.iloc[-1] > 0 else 0.0
    atr_val = atr(df, ATR_PERIOD).iloc[-1]
    _, nearR, _, nearS = detect_sr_levels(df)

    open_, close = float(df["o"].iloc[-1]), float(df["c"].iloc[-1])
    bull = close > open_ * 1.003
    bear = close < open_ * 0.997

    h1_t = await fetch_trend(ex, symbol, "1h", H1_TRENDS_CACHE)
    h4_t = await fetch_trend(ex, symbol, "4h", H4_TRENDS_CACHE)

    sh = 0
    if r.iloc[-1] >= RSI_OVERBOUGHT: sh += 1
    if e50.iloc[-1] < e200.iloc[-1] and c.iloc[-1] < e50.iloc[-1]: sh += 1
    if volr >= 2.0: sh += 1
    if nearR: sh += 1
    if bear: sh += 1

    lo = 0
    if r.iloc[-1] <= RSI_OVERSOLD: lo += 1
    if e50.iloc[-1] > e200.iloc[-1] and c.iloc[-1] > e50.iloc[-1]: lo += 1
    if volr >= 2.0: lo += 1
    if nearS: lo += 1
    if bull: lo += 1

    entry_price = close
    sl_pct = max(BASE_STOP_LOSS_PCT, 1.5 * atr_val / close)

    trend_ok_long = (lo >= 3 and h1_t == "up" and h4_t in ("up", "flat"))
    trend_ok_short = (sh >= 3 and h1_t == "down" and h4_t in ("down", "flat"))

    tp1_pct = max(0.02, 2.0 * atr_val / close)
    tp2_pct = max(0.04, 4.0 * atr_val / close)

    if trend_ok_long:
        tp1_price = entry_price * (1 + tp1_pct)
        tp2_price = entry_price * (1 + tp2_pct)
    else:
        tp1_price = entry_price * (1 - tp1_pct)
        tp2_price = entry_price * (1 - tp2_pct)

    eta_min = estimate_time_to_tp(entry_price, tp1_price, atr_val, 15)

    score = 0
    if trend_ok_long: score += lo
    if trend_ok_short: score += sh
    if volr >= 2.5: score += 1
    if h1_t == h4_t and h1_t != "flat": score += 1
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
        "h1": h1_t,
        "h4": h4_t,
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
    out = []
    for s in syms:
        try:
            d = await analyze_symbol(ex, s)
            if d:
                out.append(d)
        except Exception as e:
            log.warning(f"{name} {s}: {e}")
        await asyncio.sleep(0.25)
    out.sort(key=lambda x: (x["prob"], x["volr"]), reverse=True)
    return out

async def scan_all():
    mexc_task = asyncio.create_task(scan_exchange("mexc"))
    bitget_task = asyncio.create_task(scan_exchange("bitget"))
    mexc_res = await mexc_task
    bitget_res = await bitget_task
    return mexc_res + bitget_res

# =====================================================
# QUANTITY NORMALIZATION
# =====================================================
def normalize_amount_for_exchange(ex: ccxt.Exchange, symbol: str, amount: float) -> float:
    try:
        m = ex.market(symbol)
        min_amt = safe_float(m.get("limits", {}).get("amount", {}).get("min"))
        step = safe_float(m.get("precision", {}).get("amount"))
        if min_amt and amount < min_amt:
            amount = min_amt
        # округление до кол-ва знаков
        if step:
            # step = кол-во знаков
            amount = float(f"{amount:.{step}f}")
        return amount
    except Exception:
        return amount

def calc_position_amount(balance_usdt: float, entry_price: float, stake_usdt: float, leverage: int) -> float:
    stake_usdt = min(stake_usdt, balance_usdt)
    return (stake_usdt * leverage) / entry_price if entry_price > 0 else 0.0

# =====================================================
# BITGET/MEXC SAFE HELPERS
# =====================================================
def set_leverage_isolated(ex: ccxt.Exchange, symbol: str, side: str, lev: int):
    # для mexc надо позицию/типы — предупреждаем и выходим
    if ex.id == "mexc":
        try:
            ex.set_leverage(
                lev,
                symbol,
                params={
                    "openType": 1,  # 1 isolated / 2 cross
                    "positionType": 1 if side == "long" else 2,
                },
            )
        except Exception as e:
            log.warning(f"set_leverage {symbol}: {e}")
        return
    # bitget
    try:
        ex.set_leverage(lev, symbol, params={"marginMode": "isolated", "holdSide": side})
    except Exception as e:
        log.warning(f"set_leverage {symbol}: {e}")

def is_isolated_mode_on_bitget(ex: ccxt.Exchange, symbol: str) -> bool:
    try:
        pos = ex.fetch_positions([symbol])
        for p in pos:
            if p.get("symbol") == symbol:
                mm = p.get("marginMode") or p.get("info", {}).get("marginMode")
                return str(mm).lower() == "isolated"
    except Exception:
        pass
    return False

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
        ],
    ])

# =====================================================
# COMMANDS
# =====================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*🤖 UNIFIED FUTURES BOT v2.5.6 SAFE*\n\n"
        "⚙️ Параметры:\n"
        f"• TF: {TIMEFRAME}\n"
        f"• Автоскан: {SCAN_INTERVAL//60} мин\n"
        f"• Мин. объём: {MIN_QUOTE_VOLUME/1_000_000:.1f}M USDT\n"
        f"• SL (base): {BASE_STOP_LOSS_PCT*100:.1f}%\n"
        f"• Плечо: x{LEVERAGE}\n"
        f"• Trailing: с +{TRAILING_ACTIVATION_PCT*100:.1f}%, шаг {TRAILING_DISTANCE_PCT*100:.1f}%\n\n"
        "📋 Команды:\n"
        "/scan — найти сигналы (с кнопками)\n"
        "/top — топ-3 сильных (с кнопками)\n"
        "/trade <№> <сумма> — войти по сигналу из /scan\n"
        "/trade <SYM> <side> <сумма> — принудительно\n"
        "/report — активные сделки\n"
        "/history — файл сделок\n"
        "/stop — выключить авто\n"
        "💡 Кнопки BUY/EST приходят и в авто-сигналах."
    )
    await update.effective_message.reply_text(text, parse_mode="Markdown")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.effective_message.reply_text("Сканирую биржи...")
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
            f"Entry≈{d['entry']:.6f} | SL=−{d['sl_pct']*100:.1f}% | TP1=+{d['tp1_pct']*100:.1f}% "
            f"| ETA {d['eta_min']} мин | vol×={d['volr']:.2f} | H1={d['h1']} H4={d['h4']}"
        )
        kbd = build_signal_keyboard(i-1)
        await update.effective_message.reply_text(text, reply_markup=kbd)
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
    strong = [d for d in entries if d["prob"] >= 80] or entries[:3]
    LAST_SCAN[chat_id] = []
    for i, d in enumerate(strong[:3], 1):
        txt = (
            f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} — {signal_strength_tag(d['prob'])} ({d['prob']}%)\n"
            f"Entry≈{d['entry']:.6f} | SL=−{d['sl_pct']*100:.1f}% | TP1=+{d['tp1_pct']*100:.1f}% | ETA {d['eta_min']} мин"
        )
        kbd = build_signal_keyboard(i-1)
        await update.effective_message.reply_text(txt, reply_markup=kbd)
        LAST_SCAN[chat_id].append((
            d["symbol"], d["side"], d["exchange"],
            d["entry"], d["sl_pct"], d["tp1_pct"], d["tp2_pct"],
            d["tp1_price"], d["tp2_price"], d["eta_min"], d["prob"], d["volr"], d["rsi"]
        ))

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

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(TRADES_CSV):
        await update.effective_message.reply_text("История пока пустая.")
        return
    await update.effective_message.reply_document(InputFile(TRADES_CSV), filename="trades.csv")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AUTO_ENABLED
    AUTO_ENABLED = False
    await update.effective_message.reply_text("Автоскан отключён.")

# =====================================================
# TRADE EXECUTION
# =====================================================
def log_trade_row(d: Dict[str, Any]):
    with open(TRADES_CSV, "a", encoding="utf-8") as f:
        f.write(
            f"{datetime.utcnow().isoformat()},{d.get('exchange')},{d.get('symbol')},{d.get('side')},"
            f"{d.get('entry')},{d.get('sl_price')},{d.get('tp1_price')},{d.get('tp2_price')},"
            f"{d.get('stake')},{d.get('amount')},{d.get('eta')},{d.get('prob')},{d.get('volr')},"
            f"{d.get('rsi')},open,0\n"
        )

async def place_trade_by_row(
    update: Optional[Update],
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    row: Tuple,
    stake: float,
):
    (sym, side, exchange, entry, sl_pct, tp1_pct, tp2_pct,
     tp1_price, tp2_price, eta_min, prob, volr, rsi_val) = row

    ex = make_exchange(exchange)
    try:
        bal = ex.fetch_balance(params={"type": "swap"})["USDT"]["free"]
    except Exception as e:
        if update:
            await update.effective_message.reply_text(f"[{exchange.upper()}] Баланс не получен: {e}")
        return

    amount = calc_position_amount(bal, entry, stake, LEVERAGE)
    amount = normalize_amount_for_exchange(ex, sym, amount)
    if amount <= 0:
        if update:
            await update.effective_message.reply_text(f"[{exchange.upper()}] Слишком маленькая сумма для {sym}")
        return

    # bitget — проверка isolated
    if exchange == "bitget":
        if not is_isolated_mode_on_bitget(ex, sym):
            if update:
                await update.effective_message.reply_text("⚠️ Bitget сейчас в CROSS. Включи ISOLATED вручную.")
            return

    set_leverage_isolated(ex, sym, side, LEVERAGE)

    if side == "long":
        sl_price = entry * (1 - sl_pct)
    else:
        sl_price = entry * (1 + sl_pct)

    # создаём рыночный
    try:
        order = ex.create_market_order(
            sym,
            "buy" if side == "long" else "sell",
            amount
        )
    except Exception as e:
        if update:
            await update.effective_message.reply_text(f"[{exchange.upper()}] Ошибка ордера: {e}")
        log.error(f"order error: {e}")
        return

    # Bitget часто не даёт повесить reduceOnly сразу — монитрим софтом
    ACTIVE_TRADES.setdefault(chat_id, []).append({
        "exchange": exchange,
        "symbol": sym,
        "side": side,
        "entry": entry,
        "amount": amount,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "sl_price": sl_price,
        "created": datetime.utcnow().isoformat(),
        "chat_id": chat_id,
    })

    log_trade_row({
        "exchange": exchange,
        "symbol": sym,
        "side": side,
        "entry": entry,
        "sl_price": sl_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "stake": stake,
        "amount": amount,
        "eta": eta_min,
        "prob": prob,
        "volr": volr,
        "rsi": rsi_val,
    })

    net_pct = estimate_net_profit_pct(tp1_pct)

    if update:
        await update.effective_message.reply_text(
            f"✅ [{exchange.upper()}] Открыт {side.upper()} {sym}\n"
            f"Сумма: {stake} USDT (x{LEVERAGE}) → объём {amount:.4f}\n"
            f"Entry: {entry:.6f}\n"
            f"SL: {sl_price:.6f} (−{sl_pct*100:.1f}%)\n"
            f"TP1: {tp1_price:.6f} (+{tp1_pct*100:.1f}%)\n"
            f"TP2: {tp2_price:.6f} (+{tp2_pct*100:.1f}%)\n"
            f"⏱ ETA: ~{eta_min} мин\n"
            f"💰 Net: +{net_pct*100:.2f}%\n"
            f"📈 Trailing: {sl_pct*100:.2f}% after {(1+TRAILING_ACTIVATION_PCT)*100:.2f}%\n"
            f"⚠️ Bitget: TP/SL не поставлены на бирже, фиксация — через бота."
        )

# =====================================================
# CALLBACKS (кнопки)
# =====================================================
async def button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # "BUY|idx|10" или "EST|idx|10"
    try:
        action, idx_str, stake_str = data.split("|")
        idx = int(idx_str)
        stake = float(stake_str)
    except Exception:
        return
    chat_id = query.message.chat_id
    rows = LAST_SCAN.get(chat_id, [])
    if not rows or idx < 0 or idx >= len(rows):
        await query.edit_message_text("Сигнал устарел. Сделай /scan.")
        return
    row = rows[idx]

    if action == "EST":
        # просто считаем и показываем
        (_, _, exchange, entry, sl_pct, tp1_pct, _, tp1_price, _, eta_min, prob, volr, rsi_val) = row
        net_pct = estimate_net_profit_pct(tp1_pct)
        profit = stake * net_pct
        txt = (
            f"📈 Оценка {row[0]} {row[1].upper()} на {stake} USDT:\n"
            f"TP1: +{tp1_pct*100:.2f}% → ≈ +{profit:.2f} USDT (с комиссией)\n"
            f"ETA: {eta_min} мин\n"
            f"Биржа: {exchange.upper()} | vol×={volr:.2f} | prob={prob}%"
        )
        await query.message.reply_text(txt)
        return

    if action == "BUY":
        fake_update = Update(
            update.update_id,
            message=None,
            callback_query=update.callback_query,
        )
        await place_trade_by_row(fake_update, context, chat_id, row, stake)

# =====================================================
# /trade <…>
# =====================================================
async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # 1) /trade <№> <сумма> — по сигналу
    if len(context.args) == 2 and context.args[0].isdigit():
        if chat_id not in LAST_SCAN or not LAST_SCAN[chat_id]:
            await update.effective_message.reply_text("Сначала /scan или /top.")
            return
        idx = int(context.args[0]) - 1
        stake = float(context.args[1])
        rows = LAST_SCAN[chat_id]
        if idx < 0 or idx >= len(rows):
            await update.effective_message.reply_text("Нет такого номера сигнала. Сделай /scan.")
            return
        row = rows[idx]
        await place_trade_by_row(update, context, chat_id, row, stake)
        return

    # 2) /trade <SYM> <side> <сумма>
    if len(context.args) == 3:
        sym = context.args[0].upper()
        side = context.args[1].lower()
        stake = float(context.args[2])
        # возьмём BITGET по умолчанию
        ex = make_exchange("bitget")
        try:
            bal = ex.fetch_balance(params={"type": "swap"})["USDT"]["free"]
        except Exception as e:
            await update.effective_message.reply_text(f"[BITGET] Баланс не получен: {e}")
            return
        ticker = ex.fetch_ticker(sym)
        entry = safe_float(ticker.get("last") or ticker.get("close") or 0)
        if entry <= 0:
            await update.effective_message.reply_text("Не смог взять цену.")
            return
        amount = calc_position_amount(bal, entry, stake, LEVERAGE)
        amount = normalize_amount_for_exchange(ex, sym, amount)
        if amount <= 0:
            await update.effective_message.reply_text("Слишком маленькая сумма.")
            return
        set_leverage_isolated(ex, sym, side, LEVERAGE)
        sl_pct = BASE_STOP_LOSS_PCT
        if side == "long":
            sl_price = entry * (1 - sl_pct)
        else:
            sl_price = entry * (1 + sl_pct)
        tp1_price = entry * (1 + 0.02) if side == "long" else entry * (1 - 0.02)

        try:
            ex.create_market_order(sym, "buy" if side == "long" else "sell", amount)
        except Exception as e:
            await update.effective_message.reply_text(f"[BITGET] Ошибка ордеров: {e}")
            return

        ACTIVE_TRADES.setdefault(chat_id, []).append({
            "exchange": "bitget",
            "symbol": sym,
            "side": side,
            "entry": entry,
            "amount": amount,
            "tp1_price": tp1_price,
            "tp2_price": 0,
            "sl_price": sl_price,
            "created": datetime.utcnow().isoformat(),
            "chat_id": chat_id,
        })

        await update.effective_message.reply_text(
            f"✅ [BITGET] Открыт {side.upper()} {sym}\n"
            f"Сумма: {stake} USDT (x{LEVERAGE}) → объём {amount:.4f}\n"
            f"Entry: {entry:.6f}\n"
            f"SL: {sl_price:.6f}\n"
            f"TP1: {tp1_price:.6f}\n"
            "⚠️ TP/SL держит бот, не биржа."
        )
        return

    await update.effective_message.reply_text("Форматы:\n/trade 2 40\n/trade ZEN/USDT:USDT long 20")

# =====================================================
# AUTO SCAN LOOP
# =====================================================
async def auto_scan_loop(app: Application):
    global LAST_NO_SIGNAL_TIME
    while True:
        if AUTO_ENABLED:
            try:
                entries = await scan_all()
                now = time.time()
                if entries:
                    LAST_NO_SIGNAL_TIME = now
                    # рассылаем только тем, кто уже что-то запускал
                    for chat_id in list(LAST_SCAN.keys()):
                        top5 = entries[:5]
                        for i, d in enumerate(top5, 1):
                            txt = (
                                f"📊 Автосигналы {datetime.utcnow().strftime('%H:%M:%S')} UTC:\n"
                                f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} {signal_strength_tag(d['prob'])} ETA {d['eta_min']}м"
                            )
                            kbd = build_signal_keyboard(i-1)
                            # обновим LAST_SCAN тоже
                            LAST_SCAN[chat_id] = []
                            for dd in entries[:15]:
                                LAST_SCAN[chat_id].append((
                                    dd["symbol"], dd["side"], dd["exchange"],
                                    dd["entry"], dd["sl_pct"], dd["tp1_pct"], dd["tp2_pct"],
                                    dd["tp1_price"], dd["tp2_price"], dd["eta_min"],
                                    dd["prob"], dd["volr"], dd["rsi"]
                                ))
                            await app.bot.send_message(chat_id, txt, reply_markup=kbd)
                else:
                    if now - LAST_NO_SIGNAL_TIME >= NO_SIGNAL_NOTIFY_INTERVAL:
                        for chat_id in list(LAST_SCAN.keys()):
                            await app.bot.send_message(chat_id, "Сигналов нет.")
                        LAST_NO_SIGNAL_TIME = now
                log.info("auto_scan tick OK")
            except Exception as e:
                log.error(f"auto_scan_loop: {e}")
        await asyncio.sleep(SCAN_INTERVAL)

# =====================================================
# SOFT MONITOR (SL/TP)
# =====================================================
async def soft_monitor_loop(app: Application):
    while True:
        try:
            for chat_id, trades in list(ACTIVE_TRADES.items()):
                if not trades:
                    continue
                new_list = []
                for t in trades:
                    ex = make_exchange(t["exchange"])
                    try:
                        ticker = ex.fetch_ticker(t["symbol"])
                        last = safe_float(ticker.get("last") or ticker.get("close") or 0)
                    except Exception:
                        new_list.append(t)
                        continue
                    side = t["side"]
                    sl_price = t["sl_price"]
                    tp1_price = t["tp1_price"]
                    closed = False
                    if side == "long":
                        if last <= sl_price:
                            await app.bot.send_message(chat_id, f"⛔ SL сработал по {t['symbol']} @ {last}")
                            closed = True
                        elif last >= tp1_price:
                            await app.bot.send_message(chat_id, f"✅ TP1 сработал по {t['symbol']} @ {last}")
                            closed = True
                    else:
                        if last >= sl_price:
                            await app.bot.send_message(chat_id, f"⛔ SL сработал по {t['symbol']} @ {last}")
                            closed = True
                        elif last <= tp1_price:
                            await app.bot.send_message(chat_id, f"✅ TP1 сработал по {t['symbol']} @ {last}")
                            closed = True
                    if not closed:
                        new_list.append(t)
                ACTIVE_TRADES[chat_id] = new_list
        except Exception as e:
            log.error(f"soft_monitor_loop: {e}")
        await asyncio.sleep(AUTO_MONITOR_INTERVAL)

# =====================================================
# MAIN
# =====================================================
async def main():
    print("🚀 MAIN INIT START", flush=True)
    app = ApplicationBuilder().token(TG_BOT_TOKEN).concurrent_updates(True).build()
    print("✅ Application initialized", flush=True)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("trade", trade_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CallbackQueryHandler(button_cb))

    log.info("UNIFIED FUTURES BOT v2.5.6 STARTED")
    print("BOT ЗАПУЩЕН НА RENDER.COM | 24/7", flush=True)

    asyncio.create_task(auto_scan_loop(app))
    asyncio.create_task(soft_monitor_loop(app))

    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    # на Render иногда уже есть loop — используем существующий
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except RuntimeError:
        # если "loop already running" — просто добавим таску
        asyncio.ensure_future(main())
        loop.run_forever()
