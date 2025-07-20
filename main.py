import ccxt
import time
import requests
from datetime import datetime, timedelta
import logging
import threading
import json
import os
import calendar
import sys
import math

# ==============================================================================
# 1. ตั้งค่าพื้นฐาน (CONFIGURATION)
# ==============================================================================

# --- API Keys & Credentials (ดึงจาก Environment Variables เพื่อความปลอดภัย) ---
API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_BINANCE_API_KEY_HERE_FOR_LOCAL_TESTING')
SECRET = os.getenv('BINANCE_SECRET', 'YOUR_BINANCE_SECRET_HERE_FOR_LOCAL_TESTING')

# --- Trade Parameters ---
SYMBOL = 'BTC/USDT:USDT' # *** แก้ไขเป็น 'BTC/USDT' (ไม่มี :USDT) ***
TIMEFRAME = '15m'
LEVERAGE = 34 # *** ใช้ Leverage 34x ตามโค้ดที่คุณบอกว่าทำงานได้ดี ***
TP_DISTANCE_POINTS = 501 # Take Profit ระยะ 501 จุด
SL_DISTANCE_POINTS = 999 # Stop Loss เริ่มต้น ระยะ 999 จุด (จากราคาเข้า)

# --- Trailing Stop Loss Parameters (2 Steps) ---
# สำหรับ Long Position: (ราคาวิ่งขึ้น)
# Trigger 1: เมื่อราคากำไรถึง X จุด (จากราคาเข้า)
# SL ใหม่ 1: SL จะไปอยู่ที่ (ราคาเข้า + TRAIL_SL_STEP1_NEW_SL_POINTS_LONG)
TRAIL_SL_STEP1_TRIGGER_LONG_POINTS = 300 # ราคากำไร 300 จุด จากราคาเข้า
TRAIL_SL_STEP1_NEW_SL_POINTS_LONG = -400 # SL ใหม่ที่ ราคาเข้า - 400 จุด

# Trigger 2: เมื่อราคากำไรถึง Y จุด (จากราคาเข้า)
# SL ใหม่ 2: SL จะไปอยู่ที่ (ราคาเข้า + TRAIL_SL_STEP2_NEW_SL_POINTS_LONG)
TRAIL_SL_STEP2_TRIGGER_LONG_POINTS = 400 # ราคากำไร 400 จุด จากราคาเข้า
TRAIL_SL_STEP2_NEW_SL_POINTS_LONG = 100 # SL ใหม่ที่ ราคาเข้า + 100 จุด (กันทุน+กำไร)

# สำหรับ Short Position: (ราคาวิ่งลง) - **ต้องเพิ่ม Logic ใน monitor_position() หากต้องการเทรด Short**
# TRAIL_SL_STEP1_TRIGGER_SHORT_POINTS = 300 # ราคากำไร 300 จุด (ราคาลง 300)
# TRAIL_SL_STEP1_NEW_SL_POINTS_SHORT = 400  # SL ใหม่ที่ ราคาเข้า + 400 จุด (SL อยู่เหนือราคาเข้า)

# TRAIL_SL_STEP2_TRIGGER_SHORT_POINTS = 400 # ราคากำไร 400 จุด (ราคาลง 400)
# TRAIL_SL_STEP2_NEW_SL_POINTS_SHORT = -100 # SL ใหม่ที่ ราคาเข้า - 100 จุด (กันทุน+กำไร)

CROSS_THRESHOLD_POINTS = 1 # EMA Cross Threshold (ไม่ได้ใช้ในการทดลองนี้)

# --- Risk Management ---
MARGIN_BUFFER_USDT = 5 # Margin Buffer (USDT)
TARGET_POSITION_SIZE_FACTOR = 0.8 # *** ใช้ 0.8 (80%) ของ Equity ที่ใช้ได้ทั้งหมด ***

# --- Order Confirmation & Stability ---
CONFIRMATION_RETRIES = 15 # จำนวนครั้งที่พยายามยืนยันโพซิชัน
CONFIRMATION_SLEEP = 5 # หน่วงเวลาระหว่างการยืนยัน (วินาที)
TP_SL_BE_PRICE_TOLERANCE_PERCENT = 0.005 # เปอร์เซ็นต์ความคลาดเคลื่อนในการระบุสาเหตุการปิดออเดอร์

# --- Telegram Notification Settings ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN_HERE_FOR_LOCAL_TESTING')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE_FOR_LOCAL_TESTING')

# --- Files & Paths ---
STATS_FILE = 'trading_stats.json' # ควรเปลี่ยนเป็น '/data/trading_stats.json' หากใช้ Railway Volume

# --- Bot Timing ---
MAIN_LOOP_SLEEP_SECONDS = 30 # ลดเวลา Main Loop เพื่อให้เห็นผลเร็วขึ้นในการทดสอบ
ERROR_RETRY_SLEEP_SECONDS = 60
MONTHLY_REPORT_DAY = 20
MONTHLY_REPORT_HOUR = 0
MONTHLY_REPORT_MINUTE = 5

# ==============================================================================
# 2. การตั้งค่า Logging
# ==============================================================================
logging.basicConfig(
    level=logging.INFO, # *** ตั้งค่าเป็น INFO สำหรับการใช้งานปกติ ***
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
for handler in logging.root.handlers:
    if isinstance(handler, logging.StreamHandler):
        handler.flush = lambda: sys.stdout.flush()

logger = logging.getLogger(__name__)


# ==============================================================================
# 3. ตัวแปรสถานะการเทรด (GLOBAL TRADE STATE VARIABLES)
# ==============================================================================
current_position_details = None # เก็บข้อมูลโพซิชันปัจจุบัน (side, contracts, entry_price, sl_step, tp_price, sl_price, initial_sl_price)
portfolio_balance = 0.0
last_monthly_report_date = None
initial_balance = 0.0
last_ema_position_status = None

# ==============================================================================
# 4. โครงสร้างข้อมูลสถิติ (STATISTICS DATA STRUCTURE)
# ==============================================================================
monthly_stats = {
    'month_year': None,
    'tp_count': 0,
    'sl_count': 0,
    'total_pnl': 0.0,
    'trades': [],
    'last_report_month_year': None,
    'last_ema_cross_signal': None,
    'last_ema_position_status': None
}

# ==============================================================================
# 5. การตั้งค่า Exchange (CCXT EXCHANGE SETUP)
# ==============================================================================
exchange = None
market_info = None

def setup_exchange():
    global exchange, market_info
    try:
        if not API_KEY or API_KEY == 'YOUR_BINANCE_API_KEY_HERE_F
# ==============================================================================
# 10. ฟังก์ชันช่วยสำหรับการคำนวณและตรวจสอบออเดอร์
# ==============================================================================

def round_to_precision(value: float, precision_type: str) -> float:
    """ปัดค่าให้เป็นไปตาม Precision ที่ Exchange กำหนด"""
    if market_info and 'precision' in market_info and precision_type in market_info['precision']:
        # *** แก้ไขบรรทัดนี้: เปลี่ยน exchange.ROUND เป็น ccxt.ROUND ***
        return float(exchange.decimal_to_precision(value, ccxt.ROUND, market_info['precision'][precision_type]))
    else:
        logger.warning(f"⚠️ ไม่พบ Precision สำหรับ '{precision_type}'. ใช้ round() ปกติ.")
        return round(value, 8)

def calculate_order_details(available_usdt: float, price: float) -> tuple[float, float]:
    """
    คำนวณจำนวนสัญญาที่จะเปิดและ Margin ที่ต้องใช้ โดยพิจารณาจาก Exchange Limits
    และเปอร์เซ็นต์ของเงินทุนในพอร์ตที่ต้องการใช้
    """
    if price <= 0 or LEVERAGE <= 0 or TARGET_POSITION_SIZE_FACTOR <= 0:
        logger.error("Error: Price, leverage, and target_position_size_factor must be positive.")
        return (0, 0)

    if not market_info:
        logger.error(f"❌ Could not retrieve market info for {SYMBOL}. Please ensure setup_exchange ran successfully.")
        return (0, 0)

    try:
        amount_step = market_info['limits']['amount']['step']
        min_exchange_amount = market_info['limits']['amount']['min']
        max_exchange_amount = market_info['limits']['amount']['max']
        min_notional_exchange = market_info['limits']['cost']['min']

    except KeyError as e:
        logger.critical(f"❌ Error accessing market limits for {SYMBOL}: Missing key {e}. Exiting.", exc_info=True)
        send_telegram(f"⛔️ Critical Error: Cannot parse market limits for {SYMBOL}.\nDetails: {e}")
        return (0, 0)
    except (TypeError, ValueError) as e:
        logger.critical(f"❌ Error parsing market limits for {SYMBOL}: Invalid value {e}. Exiting.", exc_info=True)
        send_telegram(f"⛔️ Critical Error: Cannot parse market limits for {SYMBOL}.\nDetails: {e}")
        return (0, 0)

    # *** ส่วนที่แก้ไขเพื่อใช้เปอร์เซ็นต์ของทุนทั้งหมด (80%) ***
    # 1. หัก Margin Buffer ออกจากทุนทั้งหมดเพื่อหาเงินที่สามารถนำไปลงทุนได้
    investable_capital = available_usdt - MARGIN_BUFFER_USDT
    if investable_capital <= 0:
        logger.warning(f"❌ Available capital ({available_usdt:,.2f}) is not enough after deducting margin buffer ({MARGIN_BUFFER_USDT:,.2f}) for investment.")
        return (0, 0)

    # 2. คำนวณ Notional Value เป้าหมายจากสัดส่วนของ Investable Capital * Leverage
    # เช่น ถ้า investable_capital = 50 USDT, TARGET_POSITION_SIZE_FACTOR = 0.8, LEVERAGE = 34
    # target_notional_for_order_raw = 50 * 0.8 * 34 = 1360 USDT
    target_notional_for_order_raw = investable_capital * TARGET_POSITION_SIZE_FACTOR * LEVERAGE

    # 3. ต้องไม่ต่ำกว่าค่าขั้นต่ำของ Exchange (Min Notional Value และ Min Exchange Amount)
    min_notional_from_min_amount = min_exchange_amount * price
    target_notional_for_order = max(target_notional_for_order_raw, min_notional_exchange, min_notional_from_min_amount)

    # 4. หาก Notional Value ที่คำนวณจากเปอร์เซ็นต์ต่ำกว่า Minimum Notional ของ Exchange
    # ให้ใช้ Minimum Notional ของ Exchange แทน และแจ้งเตือน
    if target_notional_for_order_raw < min_notional_exchange:
        logger.info(f"ℹ️ Calculated notional from percentage ({target_notional_for_order_raw:,.2f}) is below exchange minimum ({min_notional_exchange:,.2f}). Will attempt to open at exchange minimum ({target_notional_for_order:,.2f}).")

    # *** สิ้นสุดส่วนแก้ไข ***

    contracts_raw = target_notional_for_order / price
    contracts_to_open = float(exchange.amount_to_precision(SYMBOL, contracts_raw))

    contracts_to_open = max(contracts_to_open, min_exchange_amount)
    contracts_to_open = min(contracts_to_open, max_exchange_amount)

    actual_notional_after_precision = contracts_to_open * price
    required_margin = actual_notional_after_precision / LEVERAGE

    if contracts_to_open == 0:
        logger.warning(f"⚠️ Calculated contracts to open is 0 after all adjustments. (Target notional: {target_notional_for_order:,.2f} USDT, Current price: {price:,.2f}, Min exchange amount: {min_exchange_amount:.8f}). This means calculated size is too small or rounded to zero.")
        return (0, 0)

    if available_usdt < required_margin + MARGIN_BUFFER_USDT:
        logger.error(f"❌ Margin not sufficient. Available: {available_usdt:,.2f}, Required: {required_margin:,.2f} (for trade) + {MARGIN_BUFFER_USDT} (Buffer) = {required_margin + MARGIN_BUFFER_USDT:,.2f} USDT.")
        return (0, 0)

    logger.info(f"💡 Order Calculation Result: Contracts: {contracts_to_open:,.8f}, Target Notional: {target_notional_for_order:,.2f}, Actual Notional: {actual_notional_after_precision:,.2f}, Req Margin: {required_margin:,.2f} USDT")
    return (contracts_to_open, required_margin)
# ==============================================================================
# 12. ฟังก์ชันตั้งค่า TP/SL/กันทุน (ปรับปรุงสำหรับ Trailing SL)
# ==============================================================================

def cancel_all_open_tp_sl_orders():
    """ยกเลิกคำสั่ง TP/SL ที่ค้างอยู่สำหรับ Symbol ปัจจุบันบน Binance Futures."""
    logger.info(f"⏳ Checking for and canceling existing TP/SL orders for {SYMBOL}...")
    try:
        open_orders = exchange.fetch_open_orders(SYMBOL)

        canceled_count = 0
        for order in open_orders:
            if (order['status'] == 'open' or order['status'] == 'pending') and \
               (order.get('reduceOnly', False) == True or \
                order['type'] in ['TAKE_PROFIT_MARKET', 'STOP_MARKET', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT']):
                try:
                    exchange.cancel_order(order['id'], SYMBOL)
                    logger.info(f"✅ Canceled old TP/SL order: ID {order['id']}, Type: {order['type']}, Side: {order['side']}, Price: {order.get('stopPrice') or order.get('price')}")
                    canceled_count += 1
                except ccxt.OrderNotFound:
                    logger.info(f"💡 Order {order['id']} not found or already canceled/filled. No action needed.")
                except ccxt.BaseError as e:
                    logger.warning(f"❌ Failed to cancel order {order['id']}: {str(e)}")

        if canceled_count == 0:
            logger.info("No old TP/SL orders found to cancel.")
        else:
            logger.info(f"✓ Successfully canceled {canceled_count} old TP/SL orders.")

    except ccxt.NetworkError as e:
        logger.error(f"❌ Network error while fetching/canceling open orders: {e}")
        send_telegram(f"⛔️ API Error: ไม่สามารถยกเลิก TP/SL เก่าได้ (Network)\nรายละเอียด: {e}")
    except ccxt.ExchangeError as e:
        logger.error(f"❌ Exchange error while fetching/canceling open orders: {e}")
        send_telegram(f"⛔️ API Error: ไม่สามารถยกเลิก TP/SL เก่าได้ (Exchange)\nรายละเอียด: {e}")
    except Exception as e:
        logger.error(f"❌ An unexpected error occurred while canceling orders: {e}", exc_info=True)
        send_telegram(f"⛔️ Unexpected Error: ไม่สามารถยกเลิก TP/SL เก่าได้\nรายละเอียด: {e}")


def set_tpsl_for_position(direction: str, amount: float, current_sl_price: float, current_tp_price: float) -> bool:
    """
    ตั้ง Take Profit และ Stop Loss สำหรับโพซิชัน.
    จะยกเลิก TP/SL ที่เปิดอยู่ก่อนเสมอแล้วตั้งใหม่
    """
    if not amount or amount <= 0:
        logger.error("❌ ไม่สามารถตั้ง TP/SL ได้: ขนาดโพซิชันเป็น 0 หรือไม่ถูกต้อง.")
        return False

    cancel_all_open_tp_sl_orders()
    time.sleep(1)

    market_info_precision_price = 'price' # market_info['precision']['price']

    tp_price_formatted = round_to_precision(current_tp_price, market_info_precision_price)
    sl_price_formatted = round_to_precision(current_sl_price, market_info_precision_price)

    logger.info(f"🎯 กำลังตั้ง TP: {tp_price_formatted:,.2f} | 🛑 กำลังตั้ง SL: {sl_price_formatted:,.2f} สำหรับ {direction.upper()}")

    try:
        tp_sl_side = 'sell' if direction == 'long' else 'buy'

        tp_order = exchange.create_order(
            symbol=SYMBOL,
            type='TAKE_PROFIT_MARKET',
            side=tp_sl_side,
            amount=amount,
            price=None,
            params={
                'stopPrice': tp_price_formatted,
                'reduceOnly': True,
            }
        )
        logger.info(f"✅ Take Profit order placed: ID → {tp_order.get('id', 'N/A')}")

        sl_order = exchange.create_order(
            symbol=SYMBOL,
            type='STOP_MARKET',
            side=tp_sl_side,
            amount=amount,
            price=None,
            params={
                'stopPrice': sl_price_formatted,
                'reduceOnly': True,
            }
        )
        logger.info(f"✅ Stop Loss order placed: ID → {sl_order.get('id', 'N/A')}")

        return True

    except ccxt.BaseError as e:
        logger.error(f"❌ Error setting TP/SL: {str(e)}", exc_info=True)
        send_telegram(f"⛔️ API Error (TP/SL): {e.args[0] if e.args else str(e)}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error setting TP/SL: {e}", exc_info=True)
        send_telegram(f"⛔️ Unexpected Error (TP/SL): {e}")
        return False


# ==============================================================================
# 13. ฟังก์ชันตรวจสอบสถานะและบริหารโพซิชัน (MONITORING FUNCTIONS)
# ==============================================================================

def monitor_position(current_market_price: float):
    global current_position_details, last_ema_position_status, monthly_stats

    logger.info(f"🔄 กำลังตรวจสอบสถานะโพซิชัน (Current Price: {current_market_price:,.2f})")

    pos_info_from_exchange = get_current_position()

    # 1. ตรวจสอบว่าโพซิชันถูกปิดแล้ว (Exchange ไม่มี แต่บอทยังมีข้อมูล)
    if not pos_info_from_exchange and current_p

