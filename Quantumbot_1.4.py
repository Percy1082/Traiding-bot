import os
import sys
import asyncio
import pandas as pd
import pandas_ta as ta
from binance import AsyncClient
import httpx
from functools import wraps
import logging
from logging.handlers import RotatingFileHandler
import json
from datetime import datetime
from time import time
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackContext
from dotenv import load_dotenv
import feedparser  # Thêm ở đầu file
from pathlib import Path  # <-- THÊM DÒNG NÀY

load_dotenv()  # Load biến môi trường từ .env

# ====================== PHẦN BỔ SUNG ======================
def check_single_instance():
    """Ngăn chạy nhiều instance cùng lúc"""
    pid_file = "quantumbot.pid"
    if os.path.exists(pid_file):
        with open(pid_file, "r") as f:
            old_pid = f.read().strip()
            try:
                # Kiểm tra trên Linux/Unix
                if os.path.exists(f"/proc/{old_pid}"):
                    print("⚠️ Bot đang chạy! Hãy tắt process trước.")
                    sys.exit(1)
            except:
                # Fallback cho Windows
                import psutil
                if psutil.pid_exists(int(old_pid)):
                    print("⚠️ Bot đang chạy! Hãy tắt process trước.")
                    sys.exit(1)
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

# ====================== MICROCONFIG ======================
class MicroConfig:
    API_KEY = os.getenv('BINANCE_API_KEY')
    API_SECRET = os.getenv('BINANCE_API_SECRET')
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    
    # Risk Management
    BASE_RISK = 0.02         # Tăng từ 1% lên 2%
    RISK_GROWTH_FACTOR = 0.005
    MAX_RISK = 0.15          # Tăng risk tối đa
    MIN_RISK = 0.005
    DAILY_TARGET = 0.08      # Mục tiêu 8%/ngày
    DAILY_MAX_LOSS = 0.03    # Giảm max loss
    
    # Chiến lược
    ATR_MULTIPLIER = 1.8     # Giảm khoảng cách stop loss
    TRAILING_STOP_RATIO = 0.015  # Trailing stop 1.5%
    
    # Các cấu hình khác
    AUTO_SAVE_INTERVAL = 300  # Tự động lưu trạng thái mỗi 300 giây
    NEWS_CHECK_INTERVAL = 600  # Kiểm tra tin tức mỗi 600 giây
    
    # Logging
    LOG_FILE = 'quantumbot.log'
    LOG_LEVEL = logging.DEBUG

# ====================== DYNAMIC CONFIG ======================
class DynamicConfig:
    def __init__(self):
        self.base_risk = MicroConfig.BASE_RISK
        self.atr_multiplier = MicroConfig.ATR_MULTIPLIER
        self.rsi_overbought = 75  # Điều chỉnh ngưỡng RSI
        self.rsi_oversold = 25    # Tăng độ nhạy

# ====================== MICROTRADER CLASS ======================
class MicroTrader:
    def __init__(self):
        self.client = None
        self.leverage_data = {}
        self.active_orders = {}
        self.logger = logging.getLogger(self.__class__.__name__)
        self.initial_equity = 16.74
        self.telegram_manager = TelegramManager(self)
        self.dynamic_config = self.telegram_manager.dynamic_config
        self.valid_symbols_cache = {"data": None, "timestamp": 0}

    async def initialize(self):  # <-- PHƯƠNG THỨC NÀY ĐÃ TỒN TẠI
        if self.client:
            return
        self.client = await AsyncClient.create(
            api_key=MicroConfig.API_KEY,
            api_secret=MicroConfig.API_SECRET,
            requests_params={'timeout': 30}
        )

    @staticmethod
    def retry_api(max_retries=3, delay=2):
        def decorator(func):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                for _ in range(max_retries):
                    try:
                        return await func(*args, **kwargs)
                    except Exception as e:
                        print(f"Retrying {func.__name__}: {str(e)}")
                        await asyncio.sleep(delay)
                raise Exception("API request failed")
            return wrapper
        return decorator

    @retry_api()
    async def _get_real_equity(self):
        """Lấy số dư ví Futures"""
        account = await self.client.futures_account()
        return float(account['totalWalletBalance'])

    async def get_high_volume_symbols(self):
        """Lọc symbol có volume 24h > $5M và spread < 0.1%"""
        symbols = []
        tickers = await self.client.futures_ticker()
        for ticker in tickers:
            if "USDT" in ticker['symbol']:
                spread = (float(ticker['highPrice']) - float(ticker['lowPrice'])) / float(ticker['lowPrice'])
                if float(ticker['quoteVolume']) > 5_000_000 and spread < 0.001:
                    symbols.append(ticker['symbol'])
        return symbols[:20]  # Giới hạn 20 symbol

# ----------------------------- CẤU HÌNH LOGGING -----------------------------
def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(MicroConfig.LOG_LEVEL)
    
    file_handler = RotatingFileHandler(
        MicroConfig.LOG_FILE,
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(file_formatter)
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    
    return logger

# ----------------------------- TRADING ENGINE -----------------------------
class TradingEngine(MicroTrader):
    def __init__(self):
        super().__init__()
        self.semaphore = asyncio.Semaphore(10)
        self.leverage = 3
        self.risk_level = 0

    async def execute_strategy(self):
        """Phiên bản tốc độ cao"""
        while True:
            try:
                valid_symbols = await self.get_high_volume_symbols()  # Lọc symbol volume cao
                tasks = [self.process_symbol(symbol) for symbol in valid_symbols]
                await asyncio.gather(*tasks)
                await asyncio.sleep(20)  # Giảm từ 60s -> 20s
            except Exception as e:
                self.logger.error(f"Lỗi chiến lược: {str(e)}")
                await asyncio.sleep(10)

    async def monitor_positions(self):
        """Theo dõi và cập nhật trailing stop loss"""
        while True:
            try:
                positions = await self.client.futures_position_information()
                for p in positions:
                    symbol = p['symbol']
                    position_amt = float(p['positionAmt'])
                    if position_amt != 0:
                        mark_price = float(p['markPrice'])
                        await self.update_trailing_stop(symbol, mark_price)
                await asyncio.sleep(30)
            except Exception as e:
                self.logger.error(f"Lỗi giám sát vị thế: {str(e)}")

    async def process_symbol(self, symbol):
        try:
            # Lấy dữ liệu 3 khung: 1m, 5m, 15m
            klines_1m = await self.client.futures_klines(symbol=symbol, interval='1m', limit=100)
            klines_5m = await self.client.futures_klines(symbol=symbol, interval='5m', limit=100)
            
            # Thêm logic phân tích đa khung
            df_1m = self._create_dataframe(klines_1m)
            df_5m = self._create_dataframe(klines_5m)
            
            # Điều kiện mua khi cả 2 khung đồng thuận
            buy_condition = (
                (df_1m['RSI'].iloc[-1] < 40) &
                (df_5m['EMA_12'].iloc[-1] > df_5m['EMA_26'].iloc[-1]) &
                (df_1m['volume'].iloc[-1] > df_1m['volume'].rolling(20).mean().iloc[-1])
            )
            
            if buy_condition:
                await self._place_order(symbol, 'BUY', df_1m['ATR'].iloc[-1])
        except Exception as e:
            self.logger.error(f"Lỗi xử lý {symbol}: {str(e)}")

    def _create_dataframe(self, klines):
        """Tạo DataFrame từ klines và tính các chỉ báo"""
        df = pd.DataFrame(klines, columns=[
            'timestamp','open','high','low','close','volume',
            'ignore','quote_volume','trades','taker_buy_base','taker_buy_quote','ignore2'
        ])
        # Tính toán các chỉ báo khác (như RSI, EMA_12, EMA_50, ATR, ...)
        # Ví dụ:
        df['close'] = df['close'].astype(float)
        df['RSI'] = ta.rsi(df['close'], length=14)
        df['EMA_12'] = ta.ema(df['close'], length=12)
        df['EMA_50'] = ta.ema(df['close'], length=50)
        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        return df

    async def emergency_stop(self):
        """Dừng khẩn cấp toàn bộ giao dịch"""
        try:
            # Hủy tất cả lệnh đang chờ
            open_orders = await self.client.futures_get_open_orders()
            for order in open_orders:
                await self.client.futures_cancel_order(
                    symbol=order['symbol'],
                    orderId=order['orderId']
                )

            # Đóng tất cả vị thế
            positions = await self.client.futures_position_information()
            for pos in positions:
                if float(pos['positionAmt']) != 0:
                    await self.client.futures_create_order(
                        symbol=pos['symbol'],
                        side='SELL' if float(pos['positionAmt']) > 0 else 'BUY',
                        type='MARKET',
                        quantity=abs(float(pos['positionAmt'])),
                        reduceOnly=True
                    )

            self.logger.info("🛑 Đã dừng khẩn cấp toàn bộ giao dịch")
            await self.send_telegram_message("🚨 TẤT CẢ LỆNH ĐÃ ĐƯỢC ĐÓNG KHẨN CẤP")

        except Exception as e:
            self.logger.error(f"Lỗi khi dừng khẩn cấp: {str(e)}")
            raise

    async def _place_order(self, symbol: str, side: str, atr: float):
        try:
            equity = await self._get_real_equity()
            price = float((await self.client.futures_mark_price(symbol=symbol))['markPrice'])
            
            # Tính toán risk theo ATR
            risk_amount = equity * self.dynamic_config.base_risk
            stop_loss = price - (atr * self.dynamic_config.atr_multiplier) if side == 'BUY' else price + (atr * self.dynamic_config.atr_multiplier)
            
            # Tính khối lượng chính xác
            qty = (risk_amount / abs(price - stop_loss))
            qty = round(qty, self.get_symbol_precision(symbol))  # Làm tròn theo quy tắc symbol
            
            # Đặt lệnh với 2 TP
            await self.client.futures_create_order(
                symbol=symbol,
                side=side,
                type='MARKET',
                quantity=qty
            )
            
            # TP1: 50% lệnh ở RR 1:1.5
            await self.client.futures_create_order(
                symbol=symbol,
                side='SELL' if side == 'BUY' else 'BUY',
                type='TAKE_PROFIT_MARKET',
                stopPrice=round(price + (atr * 1.5), 3),
                quantity=qty * 0.5
            )
            
            # TP2: 50% còn lại dùng trailing stop
            await self.client.futures_create_order(
                symbol=symbol,
                side='SELL' if side == 'BUY' else 'BUY',
                type='TRAILING_STOP_MARKET',
                callbackRate=self.dynamic_config.TRAILING_STOP_RATIO * 100,
                quantity=qty * 0.5
            )
            
        except Exception as e:
            self.logger.error(f"Lỗi đặt lệnh {symbol}: {str(e)}")

# ====================== TELEGRAMMANAGER CLASS ======================
class TelegramManager:
    def __init__(self, trader_instance):
        self.trader = trader_instance
        self.application = Application.builder().token(MicroConfig.TELEGRAM_TOKEN).build()
        self.dynamic_config = DynamicConfig()
        self.logger = logging.getLogger("telegram_manager")
        self._register_handlers()  # Tách phần đăng ký lệnh
        self.auto_mode = False  # Thêm thuộc tính mới
    
    async def start_bot(self, update: Update, context: CallbackContext):
        """Phương thức bị thiếu - Kích hoạt chế độ tự động"""
        self.auto_mode = True
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🤖 CHẾ ĐỘ TỰ ĐỘNG ĐÃ ĐƯỢC KÍCH HOẠT"
        )

    async def stop_bot(self, update: Update, context: CallbackContext):
        """Dừng toàn bộ hoạt động"""
        self.auto_mode = False
        try:
            await self.trader.emergency_stop()  # <-- Đã được triển khai
        except Exception as e:
            self.logger.error(f"Lỗi khi dừng bot: {str(e)}")
        
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🛑 BOT ĐÃ DỪNG HOẠT ĐỘNG"
        )
    
    def _register_handlers(self):
        """Tách riêng phần đăng ký lệnh"""
        handlers = [
            CommandHandler('set_risk', self.set_risk),
            CommandHandler('set_atr', self.set_atr),
            CommandHandler('set_rsi', self.set_rsi),
            CommandHandler('status', self.get_status),
            CommandHandler('start', self.start_bot),
            CommandHandler('stop', self.stop_bot),
            CommandHandler('profit', self.get_profit),  # Thêm lệnh /profit
        ]
        for handler in handlers:
            self.application.add_handler(handler)
    
    async def start(self):
        """Khởi động bot Telegram an toàn"""
        await self.application.bot.delete_webhook()  # QUAN TRỌNG: Xóa webhook cũ
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
        self.logger.info("Bot Telegram đã khởi động")
    
    async def stop(self):
        """Dừng bot Telegram đúng cách"""
        await self.application.updater.stop()
        await self.application.stop()
        await self.application.shutdown()
        self.logger.info("Bot Telegram đã dừng")
    
    async def set_risk(self, update: Update, context: CallbackContext):
        try:
            new_risk = float(context.args[0])
            if 0 < new_risk <= MicroConfig.MAX_RISK:  # Sử dụng MAX_RISK mới
                self.dynamic_config.base_risk = new_risk
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"✅ Đã cập nhật BASE_RISK: {new_risk}"
                )
            else:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"⚠️ Giá trị risk phải nằm trong khoảng (0, {MicroConfig.MAX_RISK}]"
                )
        except Exception as e:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Lệnh không hợp lệ. Ví dụ: /set_risk 0.1"
            )
    
    async def set_atr(self, update: Update, context: CallbackContext):
        """Xử lý lệnh /set_atr"""
        try:
            new_atr = float(context.args[0])
            if new_atr > 0:
                self.dynamic_config.atr_multiplier = new_atr
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"✅ Đã cập nhật ATR_MULTIPLIER: {new_atr}"
                )
        except Exception:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Lệnh không hợp lệ. Ví dụ: /set_atr 3.0"
            )
    
    async def set_rsi(self, update: Update, context: CallbackContext):
        """Xử lý lệnh /set_rsi để cập nhật RSI thresholds"""
        try:
            overbought = int(context.args[0])
            oversold = int(context.args[1])
            if 50 < overbought < 90 and 10 < oversold < 50:
                self.dynamic_config.rsi_overbought = overbought
                self.dynamic_config.rsi_oversold = oversold
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"✅ Đã cập nhật RSI: {overbought}/{oversold}"
                )
        except Exception:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Lệnh không hợp lệ. Ví dụ: /set_rsi 80 20"
            )
    
    async def get_status(self, update: Update, context: CallbackContext):
        """Gửi trạng thái bot"""
        status_text = (
            f"📊 Trạng thái:\n"
            f"- BASE_RISK: {self.dynamic_config.base_risk}\n"
            f"- ATR_MULTIPLIER: {self.dynamic_config.atr_multiplier}\n"
            f"- RSI (Overbought/Oversold): {self.dynamic_config.rsi_overbought}/{self.dynamic_config.rsi_oversold}"
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=status_text
        )
    
    async def get_profit(self, update: Update, context: CallbackContext):
        """Lệnh /profit để xem lợi nhuận tức thời"""
        equity = await self.trader._get_real_equity()
        profit = equity - self.trader.initial_equity
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"📈 Lợi nhuận hiện tại: {profit:.2f} USD ({profit/self.trader.initial_equity*100:.1f}%)"
        )

# ----------------------------- EnhancedMarketAnalyzer -----------------------------
class EnhancedMarketAnalyzer:
    def __init__(self, sentiment_analyzer):  # Thêm tham số
        self.news_sources = {
            'cointelegraph': "https://cointelegraph.com/rss",
            'cryptodaily': "https://cryptodaily.co.uk/feed",
            'bitcoin_news': "https://news.bitcoin.com/feed/"
        }
        self.sentiment_analyzer = sentiment_analyzer  # Lưu sentiment analyzer

    async def get_market_sentiment(self, news_texts: list):
        """Phân tích sentiment bằng AI"""
        scores = []
        for text in news_texts[:5]:  # Giới hạn 5 tin
            result = self.sentiment_analyzer(text[:512])[0]  # Sử dụng sentiment_analyzer đã truyền vào
            score = result['score'] if result['label'] == 'POSITIVE' else -result['score']
            scores.append(score)
        return sum(scores) / len(scores)

# ====================== SMART TRADING ENGINE ======================
class SmartTradingEngine(TradingEngine):
    def __init__(self):
        super().__init__()
        from transformers import pipeline
        self.sentiment_analyzer = pipeline('sentiment-analysis')  # Khởi tạo sentiment analyzer
        self.market_analyzer = EnhancedMarketAnalyzer(self.sentiment_analyzer)  # Truyền vào EnhancedMarketAnalyzer

    async def execute_strategy(self):
        """Phiên bản nâng cao"""
        while True:
            market_state = await self.market_analyzer.get_market_sentiment()
            self._adjust_leverage(market_state['risk_level'])
            await self._batch_process_symbols()
            await asyncio.sleep(60)

    def _adjust_leverage(self, risk_level):
        """Tự động điều chỉnh đòn bẩy (THÊM PHƯƠNG THỨC NÀY)"""
        if risk_level > 7:
            self.leverage = 1
            self.dynamic_config.base_risk = MicroConfig.MIN_RISK
        elif risk_level > 5:
            self.leverage = 2
            self.dynamic_config.base_risk *= 0.7
        else:
            self.leverage = 3
            self.dynamic_config.base_risk = min(
                self.dynamic_config.base_risk * 1.1,
                MicroConfig.MAX_RISK
            )

    async def _place_order(self, symbol: str, side: str, atr: float):
        """Phiên bản nâng cao với chốt lời từng phần"""
        try:
            equity = await self._get_real_equity()
            price = float((await self.client.futures_mark_price(symbol=symbol))['markPrice'])
            
            # Tính toán risk theo ATR
            risk_amount = equity * self.dynamic_config.base_risk
            stop_loss = price - (atr * self.dynamic_config.atr_multiplier) if side == 'BUY' else price + (atr * self.dynamic_config.atr_multiplier)
            
            # Tính khối lượng chính xác
            qty = (risk_amount / abs(price - stop_loss))
            qty = round(qty, self.get_symbol_precision(symbol))  # Làm tròn theo quy tắc symbol
            
            # Đặt lệnh với 2 TP
            await self.client.futures_create_order(
                symbol=symbol,
                side=side,
                type='MARKET',
                quantity=qty
            )
            
            # TP1: 50% lệnh ở RR 1:1.5
            await self.client.futures_create_order(
                symbol=symbol,
                side='SELL' if side == 'BUY' else 'BUY',
                type='TAKE_PROFIT_MARKET',
                stopPrice=round(price + (atr * 1.5), 3),
                quantity=qty * 0.5
            )
            
            # TP2: 50% còn lại dùng trailing stop
            await self.client.futures_create_order(
                symbol=symbol,
                side='SELL' if side == 'BUY' else 'BUY',
                type='TRAILING_STOP_MARKET',
                callbackRate=self.dynamic_config.TRAILING_STOP_RATIO * 100,
                quantity=qty * 0.5
            )
            
        except Exception as e:
            self.logger.error(f"Lỗi đặt lệnh cho {symbol}: {str(e)}")

    def _adjust_risk_based_on_volatility(self, atr):
        """Tự động điều chỉnh risk theo ATR"""
        volatility_factor = atr / 0.03  # Giả sử 3% là mức volatility chuẩn
        self.dynamic_config.base_risk = min(
            MicroConfig.BASE_RISK * volatility_factor,
            MicroConfig.MAX_RISK
        )

# ====================== ACCOUNTGUARD CLASS ======================
class AccountGuard(SmartTradingEngine):
    def __init__(self):
        super().__init__()  # <-- THÊM DÒNG NÀY ĐỂ KẾ THỪA ĐÚNG

    async def load_state(self):
        """Khôi phục trạng thái từ file"""
        state_file = Path('bot_state.json')
        if state_file.exists():
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                    self.active_orders = state.get('active_orders', {})
                    self.logger.info("✅ Đã khôi phục trạng thái từ file")
            except json.JSONDecodeError:
                self.logger.warning("File trạng thái bị lỗi, bỏ qua")
            except Exception as e:
                self.logger.error(f"Lỗi khi load state: {str(e)}")
    
    async def auto_save_state(self):
        """Tự động lưu trạng thái định kỳ"""
        while True:
            try:
                await asyncio.sleep(MicroConfig.AUTO_SAVE_INTERVAL)
                state = {
                    'active_orders': self.active_orders,
                    'equity': await self._get_real_equity(),
                    'timestamp': datetime.now().isoformat()
                }
                with open('bot_state.json', 'w', encoding='utf-8') as f:
                    json.dump(state, f, indent=2)
                self.logger.debug("💾 Đã lưu trạng thái hệ thống")
            except Exception as e:
                self.logger.error(f"Lỗi lưu trạng thái: {str(e)}")
    
    async def auto_system_check(self):
        """Tự động kiểm tra hệ thống định kỳ"""
        while True:
            await self.check_emergency_conditions(self.client)
            await asyncio.sleep(MicroConfig.NEWS_CHECK_INTERVAL)
    
    async def run(self):
        check_single_instance()
        await self.load_state()
        await self.initialize()  # <-- PHƯƠNG THỨC initialize() ĐƯỢC KẾ THỪA TỪ MicroTrader
        await self.telegram_manager.start()
        
        try:
            main_tasks = [
                asyncio.create_task(self.execute_strategy()),
                asyncio.create_task(self.monitor_positions()),  # ĐÃ ĐƯỢC KẾ THỪA
                asyncio.create_task(self.auto_system_check()),
                asyncio.create_task(self.auto_save_state())
            ]
            
            while True:
                now = datetime.now()
                # Reset initial_equity vào 00:00 hàng ngày
                if now.hour == 0 and now.minute == 0 and now.second < 5:
                    self.initial_equity = await self._get_real_equity()
                    self.logger.info(f"🔄 Đã reset initial equity: {self.initial_equity}")
                    await self.send_telegram_message(f"🔄 Reset mục tiêu ngày mới: {self.initial_equity}")
                    await asyncio.sleep(5)  # Tránh reset nhiều lần
                
                equity = await self._get_real_equity()
                self.logger.info(f"Lợi nhuận hiện tại: {equity - self.initial_equity}")
                
                # Kiểm tra điều kiện hàng ngày
                if equity >= self.initial_equity * (1 + MicroConfig.DAILY_TARGET):
                    await self.send_telegram_message("🎯 Đạt mục tiêu hàng ngày!")
                    self.initial_equity = equity  # Cập nhật lại cho ngày tiếp theo
                elif equity <= self.initial_equity * (1 - MicroConfig.DAILY_MAX_LOSS):
                    await self.send_telegram_message("🛑 Dừng bot do thua lỗ!")
                    break
                
                await asyncio.sleep(60)  # Kiểm tra mỗi phút
            
            for task in main_tasks:
                task.cancel()
        
        except Exception as e:
            self.logger.critical(f"Lỗi nghiêm trọng: {str(e)}")
        finally:
            await self.telegram_manager.stop()
            await self.shutdown()
            os.remove("quantumbot.pid")

    async def shutdown(self):
        if self.client:
            await self.client.close_connection()
        self.logger.info("Đã đóng kết nối Binance.")

# ====================== MAIN ======================
async def main():
    setup_logging()
    bot = AccountGuard()
    await bot.run()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())