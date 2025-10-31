# -*- coding: utf-8 -*-
"""
unified_futures_bot_v23_1_final.py
MEXC + Bitget | 24/7 | x5 | сигналы по тренду | /scan | /top | /trade <№> <сумма>
"""

import os, asyncio, logging, time
# === АНТИ-ДУБЛЬ ЗАЩИТА ПЕРЕД СТАРТОМ БОТА ===
import requests, sys

def ensure_single_instance(token: str):
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=10)
        data = resp.json()
        if data.get("ok") and data.get("result", {}).get("url"):
            print("⚠️ Duplicate instance detected — shutting down.", flush=True)
            logging.error("⚠️ Duplicate instance detected — shutting down.")
            sys.exit(0)
    except Exception as e:
        logging.warning(f"Webhook check failed: {e}")

ensure_single_instance(os.getenv("TG_BOT_TOKEN", ""))
# === КОНЕЦ АНТИ-ДУБЛЬ ===
from datetime import datetime
import datetime as dt
from typing import Dict, List, Tuple, Any

import ccxt, pandas as pd, numpy as np
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ================== ENV ==================
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")

if not all([TG_BOT_TOKEN, MEXC_API_KEY, BITGET_API_KEY, BITGET_PASSPHRASE]):
    raise SystemExit("Нужно задать TG_BOT_TOKEN, MEXC_*, BITGET_*, BITGET_PASSPHRASE!")

# ================== ПАРАМЕТРЫ ==================
TIMEFRAME, LIMIT = "15m", 300
RSI_PERIOD, RSI_OVERBOUGHT, RSI_OVERSOLD = 14, 82.0, 18.0
EMA_SHORT, EMA_LONG, VOL_SMA, ATR_PERIOD = 50, 200, 20, 14
LEVERAGE, BASE_STOP_LOSS_PCT = 5, 0.05
MIN_QUOTE_VOLUME, SCAN_INTERVAL = 5_000_000, 300
MONITOR_INTERVAL, NO_SIGNAL_NOTIFY_INTERVAL = 30, 3600
PARTIAL_TP_RATIO, TP1_MULTIPLIER_TREND, TP2_MULTIPLIER_TREND = 0.5, 2.0, 4.0
TRAILING_ACTIVATION_PCT, TRAILING_DISTANCE_PCT = 0.015, 0.015
TAKER_FEE, MAKER_FEE = 0.0006, 0.0002

# ================== ЛОГИ ==================
os.makedirs("logs", exist_ok=True)
LOG_FILENAME = f"logs/{datetime.now(dt.timezone.utc).date().isoformat()}_v23_1.log"
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILENAME, encoding="utf-8"), logging.StreamHandler()])
log = logging.getLogger("V23_1")

# ================== ГЛОБАЛЫ ==================
LAST_SCAN: Dict[int, List[Tuple]] = {}
ACTIVE_TRADES: Dict[int, List[Dict[str, Any]]] = {}
AUTO_ENABLED = True
H1_TRENDS_CACHE: Dict[str, Tuple[str, float]] = {}
H4_TRENDS_CACHE: Dict[str, Tuple[str, float]] = {}
LAST_NO_SIGNAL_TIME = 0

# ================== ВСПОМОГАТЕЛЬНЫЕ ==================
def ema(s: pd.Series, p: int): return s.ewm(span=p, adjust=False).mean()

def rsi(s: pd.Series, p=14):
    d = s.diff(); g = d.clip(lower=0).ewm(span=p, adjust=False).mean()
    l = -d.clip(upper=0).ewm(span=p, adjust=False).mean()
    rs = g / (l + 1e-12); return 100 - 100 / (1 + rs)

def atr(df: pd.DataFrame, p=14):
    tr = pd.concat([df["h"]-df["l"], (df["h"]-df["c"].shift()).abs(), (df["l"]-df["c"].shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()

def find_pivots(series, left=2, right=2, mode="high"):
    piv=[]; 
    for i in range(left, len(series)-right):
        v=series.iloc[i]
        if mode=="high" and all(v>series.iloc[i-j-1] for j in range(left)) and all(v>series.iloc[i+j+1] for j in range(right)): piv.append(i)
        if mode=="low" and all(v<series.iloc[i-j-1] for j in range(left)) and all(v<series.iloc[i+j+1] for j in range(right)): piv.append(i)
    return piv

def detect_sr_levels(df, tol_factor=1.0, min_touch=3, left=2):
    h,l,close=df["h"].values,df["l"].values,float(df["c"].iloc[-1])
    atr_val=atr(df,ATR_PERIOD).iloc[-1]; tol=tol_factor*(atr_val/close) if close>0 else 0.003
    ph,pl=find_pivots(df["h"],left,left,"high"),find_pivots(df["l"],left,left,"low")
    res_levels=[(h[i],np.sum(np.abs(h-h[i])/h[i]<tol)) for i in ph]
    sup_levels=[(l[i],np.sum(np.abs(l-l[i])/l[i]<tol)) for i in pl]
    res=max((x for x,cnt in res_levels if cnt>=min_touch),default=0)
    sup=min((x for x,cnt in sup_levels if cnt>=min_touch),default=0)
    nearR=abs(close-res)/res<tol if res else False; nearS=abs(close-sup)/sup<tol if sup else False
    return res,nearR,sup,nearS

def make_exchange(name:str):
    if name=="mexc":
        return ccxt.mexc({"apiKey":MEXC_API_KEY,"secret":MEXC_API_SECRET,"enableRateLimit":True,"options":{"defaultType":"swap"},"timeout":30000})
    elif name=="bitget":
        return ccxt.bitget({"apiKey":BITGET_API_KEY,"secret":BITGET_API_SECRET,"password":BITGET_PASSPHRASE,"enableRateLimit":True,"options":{"defaultType":"swap"},"timeout":30000})
    else: raise ValueError("Unknown exchange")

async def fetch_trend(ex,sym,tf,cache,ttl=3600):
    now=time.time(); key=f"{ex.id}:{sym}:{tf}"
    if key in cache and now-cache[key][1]<ttl: return cache[key][0]
    try:
        ohlcv=await asyncio.to_thread(ex.fetch_ohlcv,sym,tf,None,200)
        df=pd.DataFrame(ohlcv,columns=["ts","o","h","l","c","v"])
        e50,e200=ema(df["c"],50),ema(df["c"],200)
        trend="up" if e50.iloc[-1]>e200.iloc[-1] else "down" if e50.iloc[-1]<e200.iloc[-1] else "flat"
        cache[key]=(trend,now); return trend
    except: return "flat"

def estimate_time_to_tp(entry,tp,atr_val,tfm=15):
    dist=abs(tp-entry); 
    return int(max(1,dist/atr_val)*tfm) if atr_val>0 else tfm

def estimate_net_profit_pct(tp_pct): return tp_pct-(TAKER_FEE+MAKER_FEE)
def calc_position_amount(bal,entry,stake,lev): return (min(stake,bal)*lev)/entry
def signal_strength_tag(prob):
    if prob>=85:return"🔥 Сильный"
    if prob>=70:return"⚡ Хороший"
    if prob>=55:return"⚠️ Средний"
    return"❄️ Слабый"

# ================== СКАН ==================
def load_top_usdt_swaps(ex:ccxt.Exchange,top_n=60):
    ex.load_markets(); t=ex.fetch_tickers(); rows=[]
    for s,x in t.items():
        m=ex.markets.get(s)
        if not m or m.get("type")!="swap" or m.get("quote")!="USDT":continue
        qv=x.get("quoteVolume") or x.get("info",{}).get("quoteVolume") or 0.0
        if qv<MIN_QUOTE_VOLUME:continue
        rows.append((s,float(qv)))
    rows.sort(key=lambda x:x[1],reverse=True)
    return [s for s,_ in rows[:top_n]]

async def analyze_symbol(ex,sym):
    ohlcv=await asyncio.to_thread(ex.fetch_ohlcv,sym,TIMEFRAME,None,LIMIT)
    if len(ohlcv)<LIMIT//2:return
    df=pd.DataFrame(ohlcv,columns=["t","o","h","l","c","v"])
    c,v=df["c"],df["v"]
    r=rsi(c,RSI_PERIOD); e50,e200=ema(c,EMA_SHORT),ema(c,EMA_LONG)
    vma=v.rolling(VOL_SMA).mean(); volr=v.iloc[-1]/(vma.iloc[-1]+1e-12) if vma.iloc[-1]>0 else 0
    atr_val=atr(df,ATR_PERIOD).iloc[-1]; _,nR,_,nS=detect_sr_levels(df)
    open_,close=float(df["o"].iloc[-1]),float(df["c"].iloc[-1])
    bull,bear=close>open_*1.003,close<open_*0.997
    h1,h4=await fetch_trend(ex,sym,"1h",H1_TRENDS_CACHE),await fetch_trend(ex,sym,"4h",H4_TRENDS_CACHE)
    sh=lo=0
    if r.iloc[-1]>=RSI_OVERBOUGHT:sh+=1
    if e50.iloc[-1]<e200.iloc[-1]and c.iloc[-1]<e50.iloc[-1]:sh+=1
    if volr>=2:sh+=1
    if nR:sh+=1
    if bear:sh+=1
    if r.iloc[-1]<=RSI_OVERSOLD:lo+=1
    if e50.iloc[-1]>e200.iloc[-1]and c.iloc[-1]>e50.iloc[-1]:lo+=1
    if volr>=2:lo+=1
    if nS:lo+=1
    if bull:lo+=1
    entry=close; sl_pct=max(BASE_STOP_LOSS_PCT,1.5*atr_val/close)
    ok_long,ok_short=(lo>=3 and h1=="up" and h4 in("up","flat")),(sh>=3 and h1=="down" and h4 in("down","flat"))
    tp1=max(0.02,TP1_MULTIPLIER_TREND*atr_val/close); tp2=max(0.04,TP2_MULTIPLIER_TREND*atr_val/close)
    tp1p=entry*(1+tp1) if ok_long else entry*(1-tp1)
    tp2p=entry*(1+tp2) if ok_long else entry*(1-tp2)
    eta=estimate_time_to_tp(entry,tp1p,atr_val,15)
    score=(lo if ok_long else 0)+(sh if ok_short else 0)
    if volr>=2.5:score+=1
    if h1==h4 and h1!="flat":score+=1
    prob=min(100,50+score*8)
    side="long" if ok_long else "short" if ok_short else None
    if not side:return
    return dict(exchange=ex.id,symbol=sym,side=side,rsi=float(r.iloc[-1]),volr=float(volr),
        prob=prob,h1=h1,h4=h4,entry=entry,sl_pct=sl_pct,tp1_pct=tp1,tp2_pct=tp2,
        tp1_price=tp1p,tp2_price=tp2p,eta_min=eta,atr=float(atr_val),
        note="Near S" if nS else "Near R" if nR else "",net_tp1_pct=estimate_net_profit_pct(tp1))

async def scan_exchange(name:str):
    ex=make_exchange(name)
    syms=await asyncio.to_thread(load_top_usdt_swaps,ex,60)
    results=[]
    for s in syms:
        try:
            d=await analyze_symbol(ex,s)
            if d:results.append(d)
        except Exception as e:log.warning(f"{name} {s}: {e}")
        await asyncio.sleep(0.4)
    results.sort(key=lambda x:(x["prob"],x["volr"]),reverse=True)
    return results

async def scan_all():
    m,b=await asyncio.gather(scan_exchange("mexc"),scan_exchange("bitget"))
    return m+b

# ================== ТРЕЙДЫ ==================
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
    ex.create_market_order(sym, "buy" if side == "long" else "sell", amount)
    a1 = amount * PARTIAL_TP_RATIO; a2 = amount - a1
    ex.create_order(sym, "limit", "sell" if side == "long" else "buy", a1, tp1_price, params={"reduceOnly": True})
    ex.create_order(sym, "limit", "sell" if side == "long" else "buy", a2, tp2_price, params={"reduceOnly": True})
    ex.create_order(sym, "stop_market", "sell" if side == "long" else "buy", amount, params={"reduceOnly": True, "triggerPrice": sl_price})

# ================== TELEGRAM ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*UNIFIED FUTURES BOT v23.1 FINAL*\n\n"
        f"⚙️ TF: {TIMEFRAME}\nАвтоскан: {SCAN_INTERVAL//60} мин\n"
        f"Мин. объём: {MIN_QUOTE_VOLUME/1_000_000:.1f} M USDT\nRSI OB/OS: {RSI_OVERBOUGHT}/{RSI_OVERSOLD}\n"
        f"EMA: {EMA_SHORT}/{EMA_LONG}\nSL base: {BASE_STOP_LOSS_PCT*100:.1f}% | x{LEVERAGE}\n\n"
        "📋 Команды:\n/scan — поиск сигналов\n/top — топ-3\n"
        "/trade <№> <сумма> — войти по сигналу\n/report — отчёт\n/stop — остановить авто"
    )
    await update.effective_message.reply_text(text, parse_mode="Markdown")

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.effective_message.reply_text("Сканирую MEXC + Bitget…")
    entries = await scan_all()
    if not entries:
        await update.effective_message.reply_text("Сигналов нет.")
        LAST_SCAN[chat_id] = []
        return
    LAST_SCAN[chat_id] = []
    lines = []
    for i, d in enumerate(entries, 1):
        tag = signal_strength_tag(d["prob"])
        line = (
            f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} — {tag} ({d['prob']}%)\n"
            f"    RSI={d['rsi']:.1f} | vol×={d['volr']:.2f} | H1={d['h1']} H4={d['h4']}\n"
            f"    Entry≈{d['entry']:.6f} | SL=−{d['sl_pct']*100:.1f}% | TP1=+{d['tp1_pct']*100:.1f}% | ETA {d['eta_min']} мин\n"
        )
        lines.append(line)
        LAST_SCAN[chat_id].append((
            d["symbol"], d["side"], d["exchange"],
            d["entry"], d["sl_pct"], d["tp1_pct"], d["tp2_pct"],
            d["tp1_price"], d["tp2_price"], d["eta_min"], d["prob"], d["volr"], d["rsi"]
        ))
        if i >= 15: break
    await update.effective_message.reply_text("Сигналы:\n" + "\n".join(lines))

async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    entries = await scan_all()
    if not entries:
        await update.effective_message.reply_text("Сигналов нет.")
        return
    strong = [d for d in entries if d["prob"] >= 80]
    if not strong: strong = entries[:3]
    lines = []
    LAST_SCAN[chat_id] = []
    for i, d in enumerate(strong[:3], 1):
        tag = signal_strength_tag(d["prob"])
        line = (
            f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} — {tag} ({d['prob']}%)\n"
            f"    Entry≈{d['entry']:.6f} | SL=−{d['sl_pct']*100:.1f}% | TP1=+{d['tp1_pct']*100:.1f}% | ETA {d['eta_min']} мин\n"
        )
        lines.append(line)
        LAST_SCAN[chat_id].append((
            d["symbol"], d["side"], d["exchange"],
            d["entry"], d["sl_pct"], d["tp1_pct"], d["tp2_pct"],
            d["tp1_price"], d["tp2_price"], d["eta_min"], d["prob"], d["volr"], d["rsi"]
        ))
    await update.effective_message.reply_text("ТОП сигналы:\n" + "\n".join(lines))

async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id; m = update.effective_message
    if chat_id not in LAST_SCAN or not LAST_SCAN[chat_id]:
        await m.reply_text("Сначала /scan или /top."); return
    if len(context.args) < 2:
        await m.reply_text("Формат: /trade <номер> <сумма>\nПример: /trade 2 40"); return
    try:
        idx = int(context.args[0]) - 1; stake = float(context.args[1])
    except ValueError:
        await m.reply_text("Номер и сумма должны быть числами."); return
    rows = LAST_SCAN[chat_id]
    if idx < 0 or idx >= len(rows): await m.reply_text("Нет такого номера."); return
    sym, side, exname, entry, sl_pct, tp1_pct, tp2_pct, tp1_price, tp2_price, eta, prob, volr, rsi_val = rows[idx]
    ex = make_exchange(exname)
    try: bal = ex.fetch_balance(params={"type": "swap"})["USDT"]["free"]
    except Exception as e: await m.reply_text(f"[{exname.upper()}] Баланс не получен: {e}"); return
    amount = calc_position_amount(bal, entry, stake, LEVERAGE)
    if amount <= 0: await m.reply_text("Недостаточно баланса."); return
    set_leverage_isolated(ex, sym, LEVERAGE)
    sl_price = entry*(1-sl_pct) if side=="long" else entry*(1+sl_pct)
    trade = dict(symbol=sym, side=side, entry=entry, amount=amount, sl_price=sl_price, tp1_price=tp1_price, tp2_price=tp2_price)
    try: place_orders(ex, trade)
    except Exception as e: await m.reply_text(f"[{exname.upper()}] Ошибка ордеров: {e}"); log.error(e); return
    ACTIVE_TRADES.setdefault(chat_id, []).append(dict(symbol=sym, side=side, entry=entry, amount=amount,
        exchange=exname, tp1_price=tp1_price, tp2_price=tp2_price, sl_price=sl_price, time=datetime.now(dt.timezone.utc), stake=stake))
    net_pct = estimate_net_profit_pct(tp1_pct)
    await m.reply_text(
        f"✅ [{exname.upper()}] {side.upper()} {sym}\n{signal_strength_tag(prob)} ({prob}%)\n"
        f"Сумма: {stake} USDT (x{LEVERAGE}) → объём {amount:.4f}\nEntry: {entry:.6f}\n"
        f"SL: {sl_price:.6f} (−{sl_pct*100:.1f}%)\nTP1: {tp1_price:.6f}\nTP2: {tp2_price:.6f}\n"
        f"ETA: ~{eta} мин\n💰 Net +{net_pct*100:.2f}%"
    )

async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    trades = ACTIVE_TRADES.get(chat_id, [])
    if not trades:
        await update.effective_message.reply_text("Нет активных сделок."); return
    lines = ["Активные сделки:"]
    for i, t in enumerate(trades, 1):
        lines.append(f"{i}. [{t['exchange'].upper()}] {t['side'].upper()} {t['symbol']} @ {t['entry']:.6f} | SL {t['sl_price']:.6f} | TP1 {t['tp1_price']:.6f}")
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
                entries=await scan_all()
                now=time.time()
                if entries:
                    LAST_NO_SIGNAL_TIME=now
                    for chat in LAST_SCAN.keys():
                        text=[f"📊 Автосигналы {datetime.utcnow().strftime('%H:%M:%S')} UTC:"]
                        for i,d in enumerate(entries[:5],1):
                            tag=signal_strength_tag(d["prob"])
                            text.append(f"{i}. [{d['exchange'].upper()}] {d['side'].upper()} {d['symbol']} {tag} ETA {d['eta_min']}м")
                        await app.bot.send_message(chat, "\n".join(text))
                else:
                    if now-LAST_NO_SIGNAL_TIME>=NO_SIGNAL_NOTIFY_INTERVAL:
                        for chat in LAST_SCAN.keys():
                            await app.bot.send_message(chat, "Сигналов нет.")
                        LAST_NO_SIGNAL_TIME=now
                log.info("auto_scan tick OK")
            except Exception as e: log.error(f"auto_scan_loop: {e}")
        await asyncio.sleep(SCAN_INTERVAL)

# ================== MAIN ==================
import nest_asyncio
nest_asyncio.apply()  # <-- критично для Render + Python 3.13

async def main():
    print("🚀 MAIN INIT START", flush=True)

    app = Application.builder().token(TG_BOT_TOKEN).build()
    print("✅ Application initialized", flush=True)

    # обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("trade", trade_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    log.info("UNIFIED FUTURES BOT v23.1 FINAL STARTED")
    print("BOT ЗАПУЩЕН НА RENDER.COM | 24/7", flush=True)

    # фоновая задача
    asyncio.create_task(auto_scan_loop(app))

    # правильный запуск без закрытия event loop
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    await asyncio.Event().wait()  # держим процесс живым


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())

