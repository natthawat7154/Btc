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
