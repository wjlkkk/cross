"""Main arbitrage trading bot for StandX and Lighter exchanges."""
import asyncio
import signal
import logging
import os
import sys
import time
import requests
import traceback
from decimal import Decimal
from typing import Tuple
from datetime import datetime
import pytz

# Lighter 客户端
from lighter.signer_client import SignerClient

# StandX 客户端
try:
    from exchanges.standx import StandXClient
except ImportError:
    # 后备导入，适配不同的运行环境
    import sys
    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
    from exchanges.standx import StandXClient

from .data_logger import DataLogger
from .order_book_manager import OrderBookManager
from .websocket_manager import WebSocketManagerWrapper
from .order_manager import OrderManager
from .standx_position_tracker import StandXPositionTracker
from .dynamic_threshold import DynamicThresholdCalculator


class Config:
    """Simple config class to wrap dictionary."""
    def __init__(self, config_dict):
        for key, value in config_dict.items():
            setattr(self, key, value)


class StandxArb:
    """
    Arbitrage trading bot: 
    - Maker (Post-Only) orders on StandX
    - Taker (Market) orders on Lighter
    """

    def __init__(self, ticker: str, order_quantity: Decimal,
                 fill_timeout: int = 5, max_position: Decimal = Decimal('0'),
                 long_ex_threshold: Decimal = Decimal('100'),
                 short_ex_threshold: Decimal = Decimal('100'),
                 robot_id: str = None):
        """Initialize the arbitrage trading bot."""
        self.ticker = ticker
        self.order_quantity = order_quantity
        self.fill_timeout = fill_timeout
        self.max_position = max_position
        self.stop_flag = False
        self._cleanup_done = False
        self.robot_id = robot_id or f"standx_{ticker.lower()}"  # 使用传入的 robot_id 或默认值

        self.long_ex_threshold = long_ex_threshold
        self.short_ex_threshold = short_ex_threshold

        # Dynamic threshold configuration
        self.use_dynamic_threshold = os.getenv('USE_DYNAMIC_THRESHOLD', 'false').lower() == 'true'
        dynamic_window = int(os.getenv('DYNAMIC_THRESHOLD_WINDOW', '1000'))
        dynamic_interval = int(os.getenv('DYNAMIC_THRESHOLD_UPDATE_INTERVAL', '300'))
        dynamic_min = Decimal(os.getenv('DYNAMIC_THRESHOLD_MIN', '1.0'))
        dynamic_max = Decimal(os.getenv('DYNAMIC_THRESHOLD_MAX', '20.0'))
        dynamic_percentile = float(os.getenv('DYNAMIC_THRESHOLD_PERCENTILE', '0.70'))

        self.dynamic_threshold = DynamicThresholdCalculator(
            window_size=dynamic_window,
            update_interval=dynamic_interval,
            min_threshold=dynamic_min,
            max_threshold=dynamic_max,
            percentile=dynamic_percentile,
        )

        # Setup logger
        self._setup_logger()

        # Initialize modules
        # exchange="standx" 用于区分日志 CSV
        self.data_logger = DataLogger(exchange="standx", ticker=ticker, logger=self.logger, robot_id=self.robot_id)
        self.order_book_manager = OrderBookManager(self.logger)
        self.ws_manager = WebSocketManagerWrapper(self.order_book_manager, self.logger)
        self.order_manager = OrderManager(self.order_book_manager, self.logger)

        # Initialize clients (will be set later)
        self.standx_client = None
        self.lighter_client = None

        # Configuration
        self.lighter_base_url = "https://mainnet.zklighter.elliot.ai"
        self.account_index = int(os.getenv('LIGHTER_ACCOUNT_INDEX', 0))
        self.api_key_index = int(os.getenv('LIGHTER_API_KEY_INDEX', 0))
        
        # StandX Config
        self.standx_private_key = os.getenv('STANDX_PRIVATE_KEY')
        self.standx_base_url = os.getenv('STANDX_BASE_URL', 'https://perps.standx.com')
        self.standx_auth_url = os.getenv('STANDX_AUTH_URL', 'https://api.standx.com')

        # Contract/market info
        self.standx_symbol = ticker + "-USD" # 假设 StandX 格式为 BTC-USD
        self.standx_tick_size = Decimal("0.1") # 默认值，初始化时会尝试更新
        self.lighter_market_index = None
        self.base_amount_multiplier = None
        self.price_multiplier = None
        self.tick_size = None

        # Position tracker
        self.position_tracker = None

        # BBO logging control
        self.last_bbo_log_time = None
        self.last_status_log_time = None
        self.bbo_log_interval = 1800  # 半小时打印一次状态

        # Price tolerance
        self.price_tolerance_pct = Decimal('0.05')

        # Current active order tracking (to filter stale order updates)
        self.current_order_id = None

        # Setup callbacks
        self._setup_callbacks()

    def _setup_logger(self):
        """Setup logging configuration with simplified console output."""
        os.makedirs("logs", exist_ok=True)
        # 使用 robot_id 命名日志文件，确保每个机器人有独立的日志
        self.log_filename = f"logs/{self.robot_id}_arb_log.txt"

        self.logger = logging.getLogger(f"arbi_{self.robot_id}")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()

        # Disable verbose logging
        logging.getLogger('urllib3').setLevel(logging.WARNING)
        logging.getLogger('requests').setLevel(logging.WARNING)
        logging.getLogger('websockets').setLevel(logging.WARNING)

        # File handler (detailed logging)
        file_handler = logging.FileHandler(self.log_filename)
        file_handler.setLevel(logging.INFO)
        
        # Buffer optimization
        if hasattr(file_handler, 'stream') and hasattr(file_handler.stream, 'reconfigure'):
            try:
                file_handler.stream.reconfigure(buffering=65536)
            except Exception:
                pass

        # Console handler (simplified logging)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)

        # File formatter (detailed for debugging)
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')
        
        # Console formatter (simplified - only show prices and orders)
        def simplify_log_message(record):
            """Simplify log message for console - only show prices and orders."""
            msg = record.getMessage()
            
            # Keep only price/order related messages
            keywords = ['price', 'bid', 'ask', 'BBO', 'order', 'fill', 'position', '📊', '💰', '✅', '❌', '🚀', '🛑']
            if not any(kw.lower() in msg.lower() for kw in keywords):
                return None  # Suppress this message
            
            # Simplify timestamp
            import time as time_module
            beijing_ts = time_module.time() + 28800
            beijing_time = time_module.strftime("%H:%M:%S", time_module.gmtime(beijing_ts))
            
            # Add emoji prefix based on content
            if 'BBO' in msg or 'bid' in msg.lower() or 'ask' in msg.lower():
                return f"[{beijing_time}] 📊 {msg}"
            elif 'order' in msg.lower() or 'fill' in msg.lower():
                return f"[{beijing_time}] 💰 {msg}"
            elif 'position' in msg.lower():
                return f"[{beijing_time}] 📈 {msg}"
            else:
                return f"[{beijing_time}] {msg}"
        
        class SimplifiedFormatter(logging.Formatter):
            def format(self, record):
                simplified = simplify_log_message(record)
                if simplified is None:
                    return ""  # Suppress message
                return simplified

        file_handler.setFormatter(file_formatter)
        console_handler.setFormatter(SimplifiedFormatter())

        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
        self.logger.propagate = False

    def _setup_callbacks(self):
        """Setup callback functions for order updates."""
        # Lighter 回调通过 WS Manager 处理
        self.ws_manager.set_callbacks(
            on_lighter_order_filled=self._handle_lighter_order_filled,
            # StandX 的 WS 回调直接绑定在 StandXClient 上，此处无需设置
            on_edgex_order_update=None 
        )
        self.order_manager.set_callbacks(
            on_order_filled=self._handle_lighter_order_filled
        )

    def _handle_lighter_order_filled(self, order_data: dict):
        """Handle Lighter order fill."""
        try:
            if "avg_filled_price" not in order_data:
                filled_quote = Decimal(order_data.get("filled_quote_amount", 0))
                filled_base = Decimal(order_data.get("filled_base_amount", 0))
                if filled_base > 0:
                    order_data["avg_filled_price"] = filled_quote / filled_base
                else:
                    self.logger.error("❌ Cannot calculate avg price: filled_base_amount is 0")
                    return

            if order_data.get("is_ask") or order_data.get("side") == "SELL":
                order_data["side"] = "SHORT"
                order_type = "OPEN"
                if self.position_tracker:
                    filled_amount = Decimal(str(order_data.get("filled_base_amount", 0)))
                    self.position_tracker.update_lighter_position(-filled_amount)
            else:
                order_data["side"] = "LONG"
                order_type = "CLOSE"
                if self.position_tracker:
                    filled_amount = Decimal(str(order_data.get("filled_base_amount", 0)))
                    self.position_tracker.update_lighter_position(filled_amount)

            client_order_index = order_data.get("client_order_id", "UNKNOWN")
            filled_base_amount = order_data.get("filled_base_amount", 0)
            avg_filled_price = order_data.get("avg_filled_price", 0)

            self.logger.info(
                f"[{client_order_index}] [{order_type}] [Lighter] [FILLED]: "
                f"{filled_base_amount} @ {avg_filled_price}")

            self.data_logger.log_trade_to_csv(
                exchange='lighter',
                side=order_data['side'],
                price=str(avg_filled_price),
                quantity=str(filled_base_amount)
            )

            self.order_manager.lighter_order_filled = True
            self.order_manager.order_execution_complete = True

        except Exception as e:
            self.logger.error(f"Error handling Lighter order result: {e}")
            traceback.print_exc()

    def _handle_standx_order_update(self, order: dict):
        """
        Handle StandX order update from WebSocket.
        Triggered by StandXClient.
        """
        try:
            # 打印关键订单信息
            self.logger.info(
                f"📥 [StandX WS] Order: id={order.get('cl_ord_id', '')[:8]}... "
                f"side={order.get('side')} qty={order.get('qty')} "
                f"fill={order.get('fill_qty')}@{order.get('fill_avg_price')} "
                f"status={order.get('status')}"
            )

            # 过滤合约 - 支持两种字段名格式
            contract_id = order.get('contract_id') or order.get('symbol')
            if contract_id and contract_id != self.standx_symbol:
                return

            # 支持驼峰和下划线两种字段名格式 (StandX API 返回下划线格式)
            order_id = order.get('cl_ord_id') or order.get('order_id') or order.get('orderId') or order.get('clOrdId')
            status = (order.get('status') or order.get('orderStatus') or '').upper()
            side = (order.get('side') or '').lower()
            # StandX 使用 fill_qty 表示成交数量
            filled_size = Decimal(str(order.get('fill_qty') or order.get('filled_qty') or order.get('filled_size') or order.get('filledSize') or order.get('filledQty') or '0'))
            size = Decimal(str(order.get('qty') or order.get('size') or '0'))
            # StandX 使用 fill_avg_price 表示成交均价
            price = order.get('fill_avg_price') or order.get('price') or order.get('avg_price') or order.get('avgPrice') or '0'

            # Determine Order Type (Open/Close) logic
            if side == 'buy':
                order_type = "OPEN"
            else:
                order_type = "CLOSE"

            if status == 'CANCELED' and filled_size > 0:
                status = 'FILLED'

            # 模拟 EdgeX 的状态更新逻辑给 OrderManager
            # OrderManager 用 update_standx_order_status 记录状态
            self.order_manager.update_edgex_order_status(status)

            # 只处理当前活跃订单的成交，忽略旧订单的延迟通知
            if status == 'FILLED' and filled_size > 0:
                if self.current_order_id and order_id != self.current_order_id:
                    self.logger.warning(
                        f"⚠️ [Stale Order] Ignoring fill for old order {order_id}, current={self.current_order_id}")
                    # 仍然更新持仓跟踪，但不触发对冲
                    if self.position_tracker:
                        if side == 'buy':
                            self.position_tracker.update_standx_position(filled_size)
                        else:
                            self.position_tracker.update_standx_position(-filled_size)
                    return

                self.logger.info(
                    f"✅ [StandX Filled] {side.upper()} {filled_size} @ {price} (id={order_id})")

                if self.position_tracker:
                    if side == 'buy':
                        self.position_tracker.update_standx_position(filled_size)
                    else:
                        self.position_tracker.update_standx_position(-filled_size)

                self.logger.info(
                    f"[{order_id}] [{order_type}] [StandX] [{status}]: {filled_size} @ {price}")

                if filled_size > 0.0001:
                    self.data_logger.log_trade_to_csv(
                        exchange='standx',
                        side=side,
                        price=str(price),
                        quantity=str(filled_size)
                    )

                # 触发 Lighter 对冲
                self.logger.info(
                    f"🔄 [Trigger Hedge] StandX {side} filled, preparing Lighter hedge order...")

                # 触发对冲逻辑
                self.order_manager.handle_edgex_order_update({
                    'order_id': order_id,
                    'side': side,
                    'status': status,
                    'size': size,
                    'price': price,
                    'contract_id': self.standx_symbol,
                    'filled_size': filled_size
                })
            
            elif status == 'OPEN':
                self.logger.info(f"[{order_id}] [{order_type}] [StandX] [{status}]: {size} @ {price}")

        except Exception as e:
            self.logger.error(f"Error handling StandX order update: {e}")
            traceback.print_exc()

    def shutdown(self, signum=None, frame=None):
        if self.stop_flag: return
        self.stop_flag = True
        self.logger.info("\n🛑 Stopping...")

        try:
            if self.ws_manager: self.ws_manager.shutdown()
        except: pass

        try:
            if self.data_logger: self.data_logger.close()
        except: pass
        
        # Async cleanup will be handled in run()

    async def _async_cleanup(self):
        if self._cleanup_done: return
        self._cleanup_done = True

        try:
            if self.standx_client:
                await self.standx_client.disconnect()
                self.logger.info("🔌 StandX client closed")
        except Exception as e:
            self.logger.error(f"Error closing StandX client: {e}")

    def setup_signal_handlers(self):
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

    def initialize_lighter_client(self):
        if self.lighter_client is None:
            api_key_private_key = os.getenv('API_KEY_PRIVATE_KEY')
            if not api_key_private_key:
                raise Exception("API_KEY_PRIVATE_KEY not set")

            api_private_keys = {self.api_key_index: api_key_private_key}
            self.lighter_client = SignerClient(
                url=self.lighter_base_url,
                account_index=self.account_index,
                api_private_keys=api_private_keys,
            )
            if err := self.lighter_client.check_client():
                raise Exception(f"Lighter CheckClient error: {err}")

            self.logger.info("✅ Lighter client initialized")
        return self.lighter_client

    def initialize_standx_client(self):
        if not self.standx_private_key:
            raise ValueError("STANDX_PRIVATE_KEY must be set")

        config = {
            "private_key": self.standx_private_key,
            "chain": "solana",
            "base_url": self.standx_base_url,
            "auth_url": self.standx_auth_url,
            "symbol": self.standx_symbol,
            "tick_size": self.standx_tick_size
        }
        
        self.standx_client = StandXClient(config)
        # 绑定 WS 回调
        self.standx_client.setup_order_update_handler(self._handle_standx_order_update)
        
        self.logger.info("✅ StandX client initialized")
        return self.standx_client

    def get_lighter_market_config(self) -> Tuple[int, int, int, Decimal]:
        url = f"{self.lighter_base_url}/api/v1/orderBooks"
        try:
            response = requests.get(url, headers={"accept": "application/json"}, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            for market in data.get("order_books", []):
                if market["symbol"] == self.ticker:
                    price_multiplier = pow(10, market["supported_price_decimals"])
                    return (market["market_id"],
                            pow(10, market["supported_size_decimals"]),
                            price_multiplier,
                            Decimal("1") / (Decimal("10") ** market["supported_price_decimals"]))
            raise Exception(f"Ticker {self.ticker} not found on Lighter")
        except Exception as e:
            self.logger.error(f"⚠️ Error getting market config: {e}")
            raise

    async def trading_loop(self):
        """Main trading loop."""
        self.logger.info(f"🚀 Starting StandX arbitrage bot for {self.ticker}")

        try:
            self.initialize_lighter_client()
            self.initialize_standx_client()
            
            # Connect StandX (Login + WS)
            await self.standx_client.connect()

            # Lighter Config
            (self.lighter_market_index, self.base_amount_multiplier,
             self.price_multiplier, self.tick_size) = self.get_lighter_market_config()

            # Try to update StandX Tick Size
            try:
                ticker_info = self.standx_client.get_ticker(self.standx_symbol)
                # 简单的 tick size 推断或 hardcode
                # self.standx_tick_size = ... 
                pass 
            except:
                pass

            self.logger.info(f"Infoloaded - SX: {self.standx_symbol}, Lighter ID: {self.lighter_market_index}")

        except Exception as e:
            self.logger.error(f"❌ Failed to initialize: {e}")
            traceback.print_exc()
            return

        # Initialize position tracker
        self.position_tracker = StandXPositionTracker(
            self.ticker,
            self.standx_client,
            self.standx_symbol,
            self.lighter_base_url,
            self.account_index,
            self.logger
        )

        # Configure modules
        # OrderManager 同样需要兼容 StandXClient
        self.order_manager.set_edgex_config(
            self.standx_client, self.standx_symbol, self.standx_tick_size)
        self.order_manager.set_lighter_config(
            self.lighter_client, self.lighter_market_index,
            self.base_amount_multiplier, self.price_multiplier, self.tick_size)

        # Start Lighter WS (StandX WS started in connect())
        self.ws_manager.set_lighter_config(
            self.lighter_client, self.lighter_market_index, self.account_index)
        self.ws_manager.start_lighter_websocket()

        await asyncio.sleep(5)

        # Initial positions
        # StandX 策略使用 standx_client 获取持仓
        self.position_tracker.standx_position = await self.standx_client.get_account_positions()
        self.position_tracker.lighter_position = await self.position_tracker.get_lighter_position()

        self.logger.info(f"📍 Starting main trading loop! st pos:{self.position_tracker.standx_position}, lt pos: {self.position_tracker.lighter_position}")

        while not self.stop_flag:
            # 1. Fetch StandX BBO
            try:
                # 使用 StandXClient 的 get_ticker 获取价格
                ticker_data = self.standx_client.get_ticker(self.standx_symbol)
                ex_best_bid = Decimal(str(ticker_data.get('bid_price') or 0))
                ex_best_ask = Decimal(str(ticker_data.get('ask_price') or 0))
                # self.logger.info(f"StandX BBO: {ex_best_bid}/{ex_best_ask}")
                if ex_best_bid <= 0 or ex_best_ask <= 0:
                    # self.logger.warning("StandX BBO not ready")
                    await asyncio.sleep(0.5)
                    continue
            except Exception as e:
                self.logger.error(f"Error fetching StandX BBO: {e}")
                await asyncio.sleep(0.5)
                continue

            # 2. Fetch Lighter BBO
            lighter_bid, lighter_ask = self.order_book_manager.get_lighter_bbo()
            # self.logger.info(f"Lighter BBO: {lighter_bid}/{lighter_ask}")
        
            # 3. Strategy Logic
            long_ex = False
            short_ex = False

            # Calculate spreads
            long_spread = (lighter_bid - ex_best_bid) if (lighter_bid and ex_best_bid) else Decimal('0')
            short_spread = (ex_best_ask - lighter_ask) if (ex_best_ask and lighter_ask) else Decimal('0')

            # Add spread observation to dynamic threshold calculator
            if lighter_bid and ex_best_bid and lighter_ask and ex_best_ask:
                self.dynamic_threshold.add_spread_observation(long_spread, short_spread)

            # Get current thresholds (dynamic or fixed)
            if self.use_dynamic_threshold:
                long_threshold, short_threshold = self.dynamic_threshold.get_thresholds()
            else:
                long_threshold = self.long_ex_threshold
                short_threshold = self.short_ex_threshold

            # Logic: Buy StandX (Maker), Sell Lighter (Taker)
            if (lighter_bid and ex_best_bid and long_spread > long_threshold):
                long_ex = True

            # Logic: Sell StandX (Maker), Buy Lighter (Taker)
            elif (ex_best_ask and lighter_ask and short_spread > short_threshold):
                short_ex = True

            # Logging
            current_time = time.time()
            if (long_ex or short_ex or
                self.last_status_log_time is None or
                (current_time - self.last_status_log_time >= self.bbo_log_interval)):

                threshold_mode = "dynamic" if self.use_dynamic_threshold else "fixed"
                self.logger.info(
                    f"📊 ST: {ex_best_bid}/{ex_best_ask} | LT: {lighter_bid}/{lighter_ask} | "
                    f"L_Spr: {long_spread:.2f} | S_Spr: {short_spread:.2f} | "
                    f"Th({threshold_mode}): {long_threshold:.2f}/{short_threshold:.2f} | "
                    f"Pos: ST={self.position_tracker.get_current_standx_position()} LT={self.position_tracker.lighter_position}"
                )
                self.last_status_log_time = current_time

            if self.stop_flag: 
                self.logger.info("🛑 Stop flag detected, exiting trading loop")
                break

            # Execute Trades
            current_position = self.position_tracker.get_current_standx_position()

            if long_ex:
                if current_position < self.max_position:
                    self.logger.info(f"🚀 OPPORTUNITY: Long StandX (Spread: {long_spread:.2f} > Th: {long_threshold:.2f})")
                    # 做多 StandX: 挂买单 @ Ask 附近
                    await self._execute_trade('buy', ex_best_ask, lighter_bid)
                else:
                    self.logger.info("⚠️ Max Long Position Reached")
                    self.last_status_log_time = current_time
                    await asyncio.sleep(1)

            elif short_ex:
                if current_position > -1 * self.max_position:
                    self.logger.info(f"🚀 OPPORTUNITY: Short StandX (Spread: {short_spread:.2f} > Th: {short_threshold:.2f})")
                    # 做空 StandX: 挂卖单 @ Bid 附近
                    await self._execute_trade('sell', ex_best_bid, lighter_ask)
                else:
                    self.logger.info("⚠️ Max Short Position Reached")
                    self.last_status_log_time = current_time
                    await asyncio.sleep(1)
            else:
                await asyncio.sleep(0.05)

    async def _execute_trade(self, side: str, expected_price: Decimal, hedge_price: Decimal):
        """Execute trade pair (StandX Maker -> Lighter Taker)."""
        self.order_manager.order_execution_complete = False
        self.order_manager.waiting_for_lighter_fill = False
        self.current_order_id = None  # Reset at start

        try:
            self.logger.info(f"1️⃣ Placing StandX {side.upper()} Order...")
            
            # 价格微调：尝试做 Maker
            if side == 'buy':
                price = expected_price - self.standx_tick_size
            else:
                price = expected_price + self.standx_tick_size
            
            # 使用 StandXClient 下单
            # BaseExchangeClient 接口参数: contract_id, quantity, direction, price (optional)
            direction = 'long' if side == 'buy' else 'short'

            # 调用 place_open_order 并传入计算好的 price
            res = await self.standx_client.place_open_order(
                self.standx_symbol,
                self.order_quantity,
                direction,
                price  # 传入限价单价格
            )
            
            if not res.success:
                self.logger.error(f"❌ StandX Order Failed: {res.error_message}")
                return

            self.logger.info(f"✅ StandX Order Placed: {res.order_id}")

            # 设置当前订单ID，用于过滤旧订单的延迟成交通知
            self.current_order_id = res.order_id

            # 等待成交 (WS 回调会更新 order_manager.waiting_for_lighter_fill)
            wait_start = time.time()
            while not self.order_manager.waiting_for_lighter_fill and not self.stop_flag:
                await asyncio.sleep(0.01)
                if time.time() - wait_start > self.fill_timeout:
                    self.logger.warning("⏳ StandX Order Timeout, Cancelling...")
                    cancel_result = await self.standx_client.cancel_order(res.order_id)

                    # 等待取消确认或成交确认 (最多等待3秒)
                    cancel_wait_start = time.time()
                    while time.time() - cancel_wait_start < 3.0:
                        await asyncio.sleep(0.1)
                        # 如果在等待取消期间订单成交了，需要继续对冲
                        if self.order_manager.waiting_for_lighter_fill:
                            self.logger.info("📥 Order filled during cancel wait, proceeding to hedge...")
                            break

                    # 如果取消期间没有成交，直接返回
                    if not self.order_manager.waiting_for_lighter_fill:
                        self.logger.info("✅ Order cancelled successfully, no fill detected")
                        self.current_order_id = None  # Clear order ID
                        return

            # 执行对冲
            if self.order_manager.waiting_for_lighter_fill:
                self.logger.info("2️⃣ Placing Lighter Hedge Order...")
                await self.order_manager.place_lighter_market_order(
                    self.order_manager.current_lighter_side,
                    self.order_manager.current_lighter_quantity,
                    self.order_manager.current_lighter_price,
                    self.stop_flag
                )
                self.current_order_id = None  # Clear after hedge complete

        except Exception as e:
            self.logger.error(f"Trade Execution Error: {e}")
            traceback.print_exc()

    async def run(self):
        """Run the arbitrage bot."""
        self.setup_signal_handlers()

        try:
            await self.trading_loop()
        except KeyboardInterrupt:
            self.logger.info("\n🛑 Received interrupt signal...")
        except asyncio.CancelledError:
            self.logger.info("\n🛑 Task cancelled...")
        finally:
            self.logger.info("🔄 Cleaning up...")
            self.shutdown()
            try:
                await asyncio.wait_for(self._async_cleanup(), timeout=5.0)
            except asyncio.TimeoutError:
                self.logger.warning("⚠️ Cleanup timeout, forcing exit")
            except Exception as e:
                self.logger.error(f"Error during cleanup: {e}")