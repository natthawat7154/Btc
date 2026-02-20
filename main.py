# -*- coding: utf-8 -*-
# main.py
# Binance Futures – Nadaraya-Watson Envelope + MACD Confirm (TF ย่อย)
# ปรับ: TP buffer, BE reason, BE via MACD option, EMA on/off, simplified daily report
# + TP ใช้ Mid (เปิด/ปิดได้)
# + สรุปรายเดือนสะสมทั้งเดือน

import ccxt, time, json, math, logging, os, requests
from datetime import datetime

# ============================================================
# CONFIG (ปรับได้)
# ============================================================

API_KEY = os.getenv("BINANCE_API_KEY", "YOUR_BINANCE_API_KEY")
SECRET  = os.getenv("BINANCE_SECRET",    "YOUR_BINANCE_SECRET")

SYMBOL = "BTC/USDT:USDT"
TIMEFRAME = "30m"
MACD_TF = "5m"                         # TF ใช้สำหรับ entry confirm (pending)
MACD_ENABLED = False

# --- EMA toggle (ถ้าปิด จะไม่ใช้ EMA เป็นเงื่อนไข trend) ---
EMA_ENABLED = False
EMA_FAST = 50
EMA_SLOW = 100

# --- Breakeven via MACD experimental ---
USE_BREAKEVEN_MACD = False              # ถ้า True: ใช้ MACD บน BREAKEVEN_MACD_TF เพื่อตัดสินการกันทุน/ปิด
BREAKEVEN_MACD_TF = "5m"               # TF สำหรับตรวจ MACD เพื่อ BE/close (ทดลอง)
USE_REPAINT = True

LEVERAGE = 10
POSITION_MARGIN_FRACTION = 0.7

# Nadaraya params
NW_BANDWIDTH = 8.0
NW_MULT = 4
NW_FACTOR = 1.5
UPDATE_FRACTION = 0.50

# Risk / TP / SL
TP_BUFFER = 300                        # ใช้เมื่อ USE_MID_AS_TP = False
SL_DISTANCE = 2000
USE_BREAKEVEN = True
BREAKEVEN_OFFSET = 250

# 👉 เพิ่ม: เลือกโหมด TP
USE_MID_AS_TP = True    # True = TP ที่ mid / False = TP ที่ upper/lower - buffer

# Monthly report (สะสมทั้งเดือน ส่งวันที่ 25)
DAILY_REPORT_HH = 23
DAILY_REPORT_MM = 59
STATS_FILE = "daily_pnl.json"
REPORT_SENT_FILE = "daily_report_sent.txt"

LOOP_SEC = 10
LOG_LEVEL = logging.INFO

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

# ============================================================
# Logging & Telegram
# ============================================================
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("main")

def tg(msg):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("YOUR"):
        return
    try:
        requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            params={"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        )
    except:
        pass

# ============================================================
# Exchange Setup
# ============================================================
def setup_exchange():
    ex = ccxt.binance({
        "apiKey": API_KEY,
        "secret": SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "future"}
    })
    ex.load_markets()
    try:
        ex.set_leverage(LEVERAGE, SYMBOL)
    except Exception as e:
        log.warning(f"set_leverage warn: {e}")
    return ex

# ============================================================
# Indicators
# ============================================================
def ema(series, period):
    if len(series) < period:
        return None
    k = 2 / (period + 1)
    e = sum(series[:period]) / period
    for v in series[period:]:
        e = v * k + e * (1 - k)
    return e

def nwe_luxalgo_repaint(closes, h=NW_BANDWIDTH, mult=NW_MULT, factor=NW_FACTOR):
    n = len(closes)
    if n < 200:
        return None, None, None
    win = min(499, n - 1)
    coefs = [math.exp(-(i * i) / (2 * (h ** 2))) for i in range(win)]
    den = sum(coefs)
    num = sum(closes[-1 - j] * coefs[j] for j in range(win))
    mean = num / den
    win_s = int(h * 10)
    win_s = min(win_s, win - 1)
    diffs = [abs(closes[-1 - i] - closes[-1 - i - 1]) for i in range(1, win_s)]
    mae = (sum(diffs) / len(diffs)) * mult * factor if diffs else 0.0
    return mean + mae, mean - mae, mean

def macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal + 5:
        return None
    kf = 2 / (fast + 1)
    e = sum(closes[:fast]) / fast
    ef = [None]*(fast-1) + [e]
    for v in closes[fast:]:
        e = v*kf + e*(1-kf)
        ef.append(e)
    ks = 2 / (slow + 1)
    e = sum(closes[:slow]) / slow
    es = [None]*(slow-1) + [e]
    for v in closes[slow:]:
        e = v*ks + e*(1-ks)
        es.append(e)
    dif = []
    for a,b in zip(ef, es):
        if a is not None and b is not None:
            dif.append(a-b)
        else:
            dif.append(None)
    dif_clean = [x for x in dif if x is not None]
    if len(dif_clean) < signal+5:
        return None
    ks2 = 2/(signal+1)
    e = sum(dif_clean[:signal]) / signal
    dea = [None]*(signal-1) + [e]
    for v in dif_clean[signal:]:
        e = v*ks2 + e*(1-ks2)
        dea.append(e)
    return dif_clean[-2], dif_clean[-1], dea[-2], dea[-1]

def macd_up(dp,dn,ep,en):   # ตัดขึ้น
    return dp <= ep and dn > en

def macd_down(dp,dn,ep,en): # ตัดลง
    return dp >= ep and dn < en

# ============================================================
# Position sizing
# ============================================================
def free_usdt(ex):
    bal = ex.fetch_balance({"type":"future"})
    return float((bal.get("USDT") or {}).get("free") or 0.0)

def order_size(ex, price):
    free = free_usdt(ex)
    margin = free * POSITION_MARGIN_FRACTION
    notional = margin * LEVERAGE
    qty = notional / price if price > 0 else 0
    try:
        return float(ex.amount_to_precision(SYMBOL, qty))
    except:
        return round(qty, 3)

# ============================================================
# Monthly Stats (แทน Daily Stats เดิม)
# ============================================================
def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            return json.load(open(STATS_FILE,"r"))
        except:
            pass
    return {
        "month": datetime.now().strftime("%Y-%m"),
        "pnl": 0.0,
        "trades": []   # list of dict: {time, side, entry, exit, pnl, reason}
    }

def save_stats(s):
    json.dump(s, open(STATS_FILE,"w"), indent=2)

def has_sent_today():
    if not os.path.exists(REPORT_SENT_FILE):
        return False
    d = open(REPORT_SENT_FILE).read().strip()
    return d == datetime.now().strftime("%Y-%m")

def mark_sent_today():
    open(REPORT_SENT_FILE,"w").write(datetime.now().strftime("%Y-%m"))

def reset_report_if_new_month(stats):
    this_month = datetime.now().strftime("%Y-%m")
    if stats.get("month") != this_month:
        stats["month"] = this_month
        stats["pnl"] = 0.0
        stats["trades"] = []
        save_stats(stats)
        open(REPORT_SENT_FILE,"w").write("")

def try_send_monthly_report(stats):
    now = datetime.now()
    if not (now.day == 25 and now.hour == DAILY_REPORT_HH and now.minute == DAILY_REPORT_MM):
        return

    if has_sent_today():
        return

    tp_count = sum(1 for t in stats["trades"] if t.get("reason","").startswith("TP"))
    sl_count = sum(1 for t in stats["trades"] if t.get("reason","") == "SL")
    be_count = sum(1 for t in stats["trades"] if t.get("reason","") == "BE")
    total_pnl = stats["pnl"]

    lines = [
        f"📊 รายงานประจำเดือน {stats['month']}",
        f"TP: {tp_count}   SL: {sl_count}   BE: {be_count}",
        f"Σ PnL เดือนนี้: {total_pnl:+.2f} USDT"
    ]
    tg("\n".join(lines))
    mark_sent_today()
    log.info("📨 Monthly report sent.")

# ============================================================
# Main Loop
# ============================================================
def main():
    ex = setup_exchange()
    log.info(f"✅ Started Binance Futures NW Bot ({TIMEFRAME}, MACD={MACD_TF}, USE_MID_AS_TP={USE_MID_AS_TP})")

    stats = load_stats()

    position = None          # {"side","qty","entry","sl","tp"}
    sl_lock = False
    pending = None           # {"side","touch_price","lower","upper","mid","ts"}

    last_nw_update = 0
    upper = lower = mid = None

    while True:
        try:
            reset_report_if_new_month(stats)
            try_send_monthly_report(stats)

            # TF main
            candles = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=600)
            closes = [c[4] for c in candles]
            last_close = closes[-1]

            # EMA Trend (if enabled)
            e_fast = ema(closes, EMA_FAST) if EMA_ENABLED else None
            e_slow = ema(closes, EMA_SLOW) if EMA_ENABLED else None
            if EMA_ENABLED and (e_fast is None or e_slow is None):
                time.sleep(LOOP_SEC); continue
            trend = "BUY" if EMA_ENABLED and e_fast > e_slow else "SELL" if EMA_ENABLED else None

            # NW freeze
            now_ts = time.time()
            if "m" in TIMEFRAME:
                tf_minutes = int(TIMEFRAME.replace("m",""))
            else:
                tf_minutes = int(TIMEFRAME.replace("h","")) * 60
            freeze_sec = tf_minutes * 60 * UPDATE_FRACTION

            if upper is None or now_ts - last_nw_update > freeze_sec:
                u,l,m = nwe_luxalgo_repaint(closes)
                if u is None:
                    log.info("[DEBUG] NW not ready"); time.sleep(LOOP_SEC); continue
                upper,lower,mid = u,l,m
                last_nw_update = now_ts
                log.info(f"[DEBUG] NW updated: U={upper:.2f}, L={lower:.2f}, M={mid:.2f}")
            else:
                log.info("[DEBUG] Using previous NW band (frozen)")

            # MACD small TF for entry confirm
            macd_side_ok = None
            if MACD_ENABLED:
                small = ex.fetch_ohlcv(SYMBOL, MACD_TF, limit=200)
                mcloses = [c[4] for c in small[:-1]]
                mac = macd(mcloses)
                if mac:
                    dp,dn,ep,en = mac
                    if EMA_ENABLED:
                        macd_side_ok = macd_up(dp,dn,ep,en) if trend=="BUY" else macd_down(dp,dn,ep,en)
                    else:
                        macd_side_ok = True

            # Read live position on exchange
            try:
                pos_list = ex.fetch_positions([SYMBOL])
                amt = 0.0; live_side = None
                for p in pos_list:
                    if p.get("symbol") == SYMBOL and float(p.get("contracts") or 0) != 0:
                        amt = float(p["contracts"])
                        live_side = p["side"]
                        break
            except:
                amt=0.0; live_side=None

            if amt == 0 and position is not None:
                log.info("⚠ Position disappeared on exchange, reset local state.")
                position = None

            # ---------------- MANAGE OPEN POSITION ----------------
            if position and amt > 0:
                last_price = ex.fetch_ticker(SYMBOL)["last"]
                side = position["side"]
                entry = position["entry"]
                sl = position["sl"]
                tp = position.get("tp")

                # --- Breakeven via MACD experimental (if enabled) ---
                if USE_BREAKEVEN_MACD:
                    try:
                        small_be = ex.fetch_ohlcv(SYMBOL, BREAKEVEN_MACD_TF, limit=200)
                        be_closes = [c[4] for c in small_be[:-1]]
                        mac_be = macd(be_closes)
                        if mac_be:
                            dp,dn,ep,en = mac_be
                            if side == "long" and macd_down(dp,dn,ep,en):
                                unreal = (last_close - entry) * position["qty"]
                                if unreal > 0:
                                    if position["sl"] < entry + BREAKEVEN_OFFSET:
                                        position["sl"] = entry + BREAKEVEN_OFFSET
                                else:
                                    pnl = (last_price-entry)*position["qty"]
                                    stats["pnl"] += pnl
                                    stats["trades"].append({"time": datetime.now().strftime("%H:%M:%S"),"side":"LONG","entry": entry,"exit": last_price,"pnl": pnl,"reason": "SL"})
                                    ex.create_market_order(SYMBOL,"sell",position["qty"],params={"reduceOnly":True})
                                    position=None; sl_lock=True; save_stats(stats); time.sleep(LOOP_SEC); continue
                            if side == "short" and macd_up(dp,dn,ep,en):
                                unreal = (entry-last_close) * position["qty"]
                                if unreal > 0:
                                    if position["sl"] > entry - BREAKEVEN_OFFSET:
                                        position["sl"] = entry - BREAKEVEN_OFFSET
                                else:
                                    pnl = (entry-last_price)*position["qty"]
                                    stats["pnl"] += pnl
                                    stats["trades"].append({"time": datetime.now().strftime("%H:%M:%S"),"side":"SHORT","entry": entry,"exit": last_price,"pnl": pnl,"reason": "SL"})
                                    ex.create_market_order(SYMBOL,"buy",position["qty"],params={"reduceOnly":True})
                                    position=None; sl_lock=True; save_stats(stats); time.sleep(LOOP_SEC); continue
                    except Exception as e:
                        log.debug(f"[BE_MACD] fetch error: {e}")

                # --- SL Touch ---
                if side=="long" and last_price <= sl:
                    reason = "BE" if abs(sl - (entry + BREAKEVEN_OFFSET)) < 1e-6 else "SL"
                    pnl = (last_price-entry)*position["qty"]
                    stats["pnl"] += pnl
                    stats["trades"].append({"time": datetime.now().strftime("%H:%M:%S"),"side":"LONG","entry": entry,"exit": last_price,"pnl": pnl,"reason": reason})
                    ex.create_market_order(SYMBOL,"sell",position["qty"],params={"reduceOnly":True})
                    tg(f"🚨 LONG {reason} {entry:.2f}->{last_price:.2f} PnL={pnl:+.2f}")
                    position=None; sl_lock=True; save_stats(stats); time.sleep(LOOP_SEC); continue

                if side=="short" and last_price >= sl:
                    reason = "BE" if abs(sl - (entry - BREAKEVEN_OFFSET)) < 1e-6 else "SL"
                    pnl = (entry-last_price)*position["qty"]
                    stats["pnl"] += pnl
                    stats["trades"].append({"time": datetime.now().strftime("%H:%M:%S"),"side":"SHORT","entry": entry,"exit": last_price,"pnl": pnl,"reason": reason})
                    ex.create_market_order(SYMBOL,"buy",position["qty"],params={"reduceOnly":True})
                    tg(f"🚨 SHORT {reason} {entry:.2f}->{last_price:.2f} PnL={pnl:+.2f}")
                    position=None; sl_lock=True; save_stats(stats); time.sleep(LOOP_SEC); continue

                # --- TP ---
                if tp is not None:
                    if side=="long" and last_price >= tp:
                        pnl = (last_price-entry)*position["qty"]
                        stats["pnl"] += pnl
                        stats["trades"].append({"time": datetime.now().strftime("%H:%M:%S"),"side":"LONG","entry": entry,"exit": last_price,"pnl": pnl,"reason": "TP_mid" if USE_MID_AS_TP else "TP_upper"})
                        ex.create_market_order(SYMBOL,"sell",position["qty"],params={"reduceOnly":True})
                        tg(f"✅ LONG TP @ {last_price:.2f} PnL={pnl:+.2f}")
                        position=None; save_stats(stats); time.sleep(LOOP_SEC); continue

                    if side=="short" and last_price <= tp:
                        pnl = (entry-last_price)*position["qty"]
                        stats["pnl"] += pnl
                        stats["trades"].append({"time": datetime.now().strftime("%H:%M:%S"),"side":"SHORT","entry": entry,"exit": last_price,"pnl": pnl,"reason": "TP_mid" if USE_MID_AS_TP else "TP_lower"})
                        ex.create_market_order(SYMBOL,"buy",position["qty"],params={"reduceOnly":True})
                        tg(f"✅ SHORT TP @ {last_price:.2f} PnL={pnl:+.2f}")
                        position=None; save_stats(stats); time.sleep(LOOP_SEC); continue

                # --- EMA flip TP mid ---
                if EMA_ENABLED:
                    trend_now = "BUY" if e_fast > e_slow else "SELL"
                    if side=="long" and trend_now=="SELL" and last_price <= mid:
                        pnl = (last_price-entry)*position["qty"]
                        stats["pnl"] += pnl
                        stats["trades"].append({"time": datetime.now().strftime("%H:%M:%S"),"side":"LONG","entry": entry,"exit": last_price,"pnl": pnl,"reason": "TP_mid_trend_flip"})
                        ex.create_market_order(SYMBOL,"sell",position["qty"],params={"reduceOnly":True})
                        position=None; save_stats(stats); time.sleep(LOOP_SEC); continue

                    if side=="short" and trend_now=="BUY" and last_price >= mid:
                        pnl = (entry-last_price)*position["qty"]
                        stats["pnl"] += pnl
                        stats["trades"].append({"time": datetime.now().strftime("%H:%M:%S"),"side":"SHORT","entry": entry,"exit": last_price,"pnl": pnl,"reason": "TP_mid_trend_flip"})
                        ex.create_market_order(SYMBOL,"buy",position["qty"],params={"reduceOnly":True})
                        position=None; save_stats(stats); time.sleep(LOOP_SEC); continue

                # --- Breakeven via mid ---
                if not USE_BREAKEVEN_MACD and USE_BREAKEVEN and not sl_lock:
                    if side=="long" and last_close > mid and position["sl"] < entry + BREAKEVEN_OFFSET:
                        position["sl"] = entry + BREAKEVEN_OFFSET
                    if side=="short" and last_close < mid and position["sl"] > entry - BREAKEVEN_OFFSET:
                        position["sl"] = entry - BREAKEVEN_OFFSET

                save_stats(stats)
                time.sleep(LOOP_SEC)
                continue

            # ---------------- NO POSITION ----------------
            if sl_lock:
                if (last_close > mid) or (last_close < mid):
                    sl_lock=False
                    log.info("🔓 SL Lock released")
                time.sleep(LOOP_SEC)
                continue

            # 1) pending from touch -> wait MACD confirm
            if MACD_ENABLED and pending is not None:
                last_price = ex.fetch_ticker(SYMBOL)["last"]
                side = pending["side"]
                p_lower = pending["lower"]
                p_upper = pending["upper"]
                p_mid   = pending["mid"]

                if macd_side_ok:
                    if not EMA_ENABLED:
                        small = ex.fetch_ohlcv(SYMBOL, MACD_TF, limit=200)
                        mcloses = [c[4] for c in small[:-1]]
                        mac = macd(mcloses)
                        if mac:
                            dp,dn,ep,en = mac
                            if side=="long" and not macd_up(dp,dn,ep,en):
                                pending=None; save_stats(stats); time.sleep(LOOP_SEC); continue
                            if side=="short" and not macd_down(dp,dn,ep,en):
                                pending=None; save_stats(stats); time.sleep(LOOP_SEC); continue

                    if side=="long":
                        if last_price < p_lower or last_price > p_mid:
                            pending=None
                        else:
                            qty = order_size(ex, last_price)
                            ex.create_market_order(SYMBOL,"buy",qty)
                            tp_val = p_mid if USE_MID_AS_TP else (p_upper - TP_BUFFER)
                            position={"side":"long","qty":qty,"entry":last_price,"sl":last_price-SL_DISTANCE,"tp":tp_val}
                            pending=None
                    else:
                        if last_price > p_upper or last_price < p_mid:
                            pending=None
                        else:
                            qty = order_size(ex, last_price)
                            ex.create_market_order(SYMBOL,"sell",qty)
                            tp_val = p_mid if USE_MID_AS_TP else (p_lower + TP_BUFFER)
                            position={"side":"short","qty":qty,"entry":last_price,"sl":last_price+SL_DISTANCE,"tp":tp_val}
                            pending=None

                save_stats(stats)
                time.sleep(LOOP_SEC)
                continue

            # 2) no pending -> detect new NW touch
            if (not EMA_ENABLED or (EMA_ENABLED and trend=="BUY")) and last_close <= lower:
                last_price = ex.fetch_ticker(SYMBOL)["last"]
                if MACD_ENABLED:
                    pending = {"side":"long","touch_price": last_price,"lower": lower,"upper": upper,"mid": mid,"ts": now_ts}
                else:
                    qty = order_size(ex, last_price)
                    tp_val = mid if USE_MID_AS_TP else (upper - TP_BUFFER)
                    ex.create_market_order(SYMBOL,"buy",qty)
                    position={"side":"long","qty":qty,"entry":last_price,"sl":last_price-SL_DISTANCE,"tp":tp_val}
                save_stats(stats); time.sleep(LOOP_SEC); continue

            if (not EMA_ENABLED or (EMA_ENABLED and trend=="SELL")) and last_close >= upper:
                last_price = ex.fetch_ticker(SYMBOL)["last"]
                if MACD_ENABLED:
                    pending = {"side":"short","touch_price": last_price,"lower": lower,"upper": upper,"mid": mid,"ts": now_ts}
                else:
                    qty = order_size(ex, last_price)
                    tp_val = mid if USE_MID_AS_TP else (lower + TP_BUFFER)
                    ex.create_market_order(SYMBOL,"sell",qty)
                    position={"side":"short","qty":qty,"entry":last_price,"sl":last_price+SL_DISTANCE,"tp":tp_val}
                save_stats(stats); time.sleep(LOOP_SEC); continue

            save_stats(stats)
            time.sleep(LOOP_SEC)

        except Exception as e:
            log.exception(f"loop error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()
