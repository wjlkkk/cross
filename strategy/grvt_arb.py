"""Main arbitrage trading bot for GRVT and Lighter exchanges."""
import asyncio
import signal
import logging
import os
import sys
import time
import traceback
import requests
from decimal import Decimal
from typing import Tuple
from datetime import datetime
import pytz

from lighter.signer_client import SignerClient
from lighter import OrderApi, ApiClient, Configuration
from edgex_sdk import Client, WebSocketManager

from .data_logger import DataLogger
from .order_book_manager import OrderBookManager
from .websocket_manager import WebSocketManagerWrapper
from .order_manager import OrderManager
from .position_tracker import PositionTracker
from .dynamic_threshold import DynamicThresholdCalculator
from exchanges.grvt import GrvtClient


class Config:
    """Simple config class to wrap dictionary."""
    def __init__(self, config_dict):
        for key, value in config_dict.items():
            setattr(self, key, value)


class LighterClient:
    """Lighter exchange client for arbitrage trading."""
    
    def __init__(self, ticker: str, logger: logging.Logger):
        """Initialize Lighter client."""
        self.ticker = ticker
        self.logger = logger
        
        # Credentials
        self.api_key_private_key = os.getenv('API_KEY_PRIVATE_KEY')
        self.account_index = int(os.getenv('LIGHTER_ACCOUNT_INDEX', '0'))
        self.api_key_index = int(os.getenv('LIGHTER_API_KEY_INDEX', '0'))
        self.base_url = os.getenv('LIGHTER_BASE_URL', 'https://mainnet.zklighter.elliot.ai')
        
        if not self.api_key_private_key:
            raise ValueError("API_KEY_PRIVATE_KEY must be set in environment variables")
        
        # Client instances
        self.lighter_client = None
        self.api_client = None
        self.order_api = None
        
        # Market configuration
        self.market_id = None
        self.base_amount_multiplier = None
        self.price_multiplier = None
        
        # State
        self.current_order = None
        self.current_order_client_id = None
        
        self.logger.info("LighterClient initialized")
    
    async def connect(self):
        """Connect to Lighter."""
        try:
            # Initialize API client
            self.api_client = ApiClient(configuration=Configuration(host=self.base_url))
            self.order_api = OrderApi(self.api_client)
            
            # Initialize SignerClient
            api_private_keys = {self.api_key_index: self.api_key_private_key}
            self.lighter_client = SignerClient(
                url=self.base_url,
                private_key=self.api_key_private_key,
                account_index=self.account_index,
                api_key_index=self.api_key_index,
            )
            
            # Check client
            err = self.lighter_client.check_client()
            if err is not None:
                raise Exception(f"CheckClient error: {err}")
            
            self.logger.info("Lighter client connected successfully")
            
        except Exception as e:
            self.logger.error(f"Failed to connect to Lighter: {e}")
            raise
    
    async def get_market_config(self) -> Tuple[int, int, int]:
        """Get market configuration for ticker."""
        try:
            order_books = await self.order_api.order_books()
            
            for market in order_books.order_books:
                if market.symbol == self.ticker:
                    self.market_id = market.market_id
                    self.base_amount_multiplier = pow(10, market.supported_size_decimals)
                    self.price_multiplier = pow(10, market.supported_price_decimals)
                    
                    self.logger.info(f"Market config: {self.ticker} -> ID={self.market_id}, "
                                   f"Base mult={self.base_amount_multiplier}, Price mult={self.price_multiplier}")
                    
                    return self.market_id, self.base_amount_multiplier, self.price_multiplier
            
            raise Exception(f"Ticker {self.ticker} not found in Lighter markets")
            
        except Exception as e:
            self.logger.error(f"Error getting market config: {e}")
            raise
    
    async def fetch_bbo_prices(self) -> Tuple[Decimal, Decimal]:
        """Fetch best bid/ask prices from Lighter API."""
        try:
            # Use account API to get order book details
            order_book_details = await self.order_api.order_book_details(market_id=self.market_id)
            
            if order_book_details and order_book_details.order_book_details:
                details = order_book_details.order_book_details[0]
                
                # Get best bid and ask from order book
                bids = details.bids if hasattr(details, 'bids') else []
                asks = details.asks if hasattr(details, 'asks') else []
                
                if bids and asks:
                    best_bid = Decimal(str(bids[0].price))
                    best_ask = Decimal(str(asks[0].price))
                    
                    if best_bid >= best_ask:
                        self.logger.warning(f"Invalid BBO: bid {best_bid} >= ask {best_ask}")
                        return Decimal('0'), Decimal('0')
                    
                    return best_bid, best_ask
            
            self.logger.warning("No bids/asks in order book")
            return Decimal('0'), Decimal('0')
            
        except Exception as e:
            self.logger.error(f"Error fetching BBO: {e}")
            return Decimal('0'), Decimal('0')
    
    async def place_order(self, side: str, quantity: Decimal, price: Decimal, 
                          is_ask: bool, order_type: str = "limit",
                          time_in_force: str = "gtc") -> Tuple[bool, str]:
        """Place an order with Lighter."""
        try:
            client_order_index = int(time.time() * 1000) % 1000000
            self.current_order_client_id = client_order_index
            
            # Convert to raw values
            base_amount = int(quantity * self.base_amount_multiplier)
            raw_price = int(price * self.price_multiplier)
            
            # Determine time in force
            if time_in_force == "ioc":
                tif = self.lighter_client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
            elif time_in_force == "gtc":
                tif = self.lighter_client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
            else:
                tif = self.lighter_client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
            
            # Create order
            create_order, tx_hash, error = await self.lighter_client.create_order(
                market_index=self.market_id,
                client_order_index=client_order_index,
                base_amount=base_amount,
                price=raw_price,
                is_ask=is_ask,
                order_type=self.lighter_client.ORDER_TYPE_LIMIT,
                time_in_force=tif,
                reduce_only=False,
                trigger_price=0,
            )
            
            if error is not None:
                self.logger.error(f"Order error: {error}")
                return False, str(error)
            
            self.logger.info(f"Order placed: {side.upper()} {quantity} @ {price}, tx={tx_hash[:20] if tx_hash else 'N/A'}...")
            return True, str(client_order_index)
            
        except Exception as e:
            self.logger.error(f"Error placing order: {e}")
            return False, str(e)
    
    async def get_active_orders(self) -> list:
        """Get active orders."""
        try:
            auth_token, error = self.lighter_client.create_auth_token_with_expiry()
            if error:
                self.logger.error(f"Auth token error: {error}")
                return []
            
            orders_response = await self.order_api.account_active_orders(
                account_index=self.account_index,
                market_id=self.market_id,
                auth=auth_token
            )
            
            if orders_response and orders_response.orders:
                return orders_response.orders
            return []
            
        except Exception as e:
            self.logger.error(f"Error getting active orders: {e}")
            return []
    
    async def get_positions(self) -> list:
        """Get account positions."""
        try:
            account_api = lighter.AccountApi(self.api_client)
            account_data = await account_api.account(
                by="index", 
                value=str(self.account_index)
            )
            
            if account_data and account_data.accounts:
                return account_data.accounts[0].positions
            return []
            
        except Exception as e:
            self.logger.error(f"Error getting positions: {e}")
            return []
    
    async def get_position_size(self) -> Decimal:
        """Get net position size for current ticker."""
        positions = await self.get_positions()
        
        for pos in positions:
            if pos.market_id == self.market_id:
                return Decimal(str(pos.position))
        
        return Decimal('0')
    
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order."""
        try:
            cancel_order, tx_hash, error = await self.lighter_client.cancel_order(
                market_index=self.market_id,
                order_index=int(order_id)
            )
            
            if error is not None:
                self.logger.error(f"Cancel error: {error}")
                return False
            
            self.logger.info(f"Order {order_id} cancelled")
            return True
            
        except Exception as e:
            self.logger.error(f"Error cancelling order: {e}")
            return False


class GrvtArb:
    """Arbitrage trading bot: makes post-only orders on GRVT, and market orders on Lighter."""

    def __init__(self, ticker: str, order_quantity: Decimal,
                 fill_timeout: int = 5, max_position: Decimal = Decimal('0'),
                 long_ex_threshold: Decimal = Decimal('10'),
                 short_ex_threshold: Decimal = Decimal('10')):
        """Initialize the arbitrage trading bot."""
        self.ticker = ticker
        self.order_quantity = order_quantity
        self.fill_timeout = fill_timeout
        self.max_position = max_position
        self.stop_flag = False
        self._cleanup_done = False

        self.long_ex_threshold = long_ex_threshold
        self.short_ex_threshold = short_ex_threshold

        # Setup logger
        self._setup_logger()

        # Initialize modules
        self.data_logger = DataLogger(exchange="grvt", ticker=ticker, logger=self.logger)
        self.order_book_manager = OrderBookManager(self.logger)
        self.ws_manager = WebSocketManagerWrapper(self.order_book_manager, self.logger)
        self.order_manager = OrderManager(self.order_book_manager, self.logger)

        # Initialize dynamic threshold calculator
        dynamic_window = int(os.getenv('DYNAMIC_THRESHOLD_WINDOW', '1000'))
        dynamic_interval = int(os.getenv('DYNAMIC_THRESHOLD_UPDATE_INTERVAL', '300'))
        dynamic_min = Decimal(os.getenv('DYNAMIC_THRESHOLD_MIN', '1.0'))
        dynamic_max = Decimal(os.getenv('DYNAMIC_THRESHOLD_MAX', '10.0'))
        dynamic_percentile = float(os.getenv('DYNAMIC_THRESHOLD_PERCENTILE', '0.70'))
        
        self.dynamic_threshold = DynamicThresholdCalculator(
            window_size=dynamic_window,
            update_interval=dynamic_interval,
            min_threshold=dynamic_min,
            max_threshold=dynamic_max,
            percentile=dynamic_percentile,
            logger=self.logger
        )

        # Initialize clients
        self.grvt_client = None
        self.lighter_client = None

        # Contract/market info
        self.grvt_contract_id = None
        self.grvt_tick_size = None
        self.lighter_market_id = None

        # Position tracker
        self.position_tracker = None
        
        # Statistics
        self.loop_count = 0
        self.last_price_log_time = 0

    def _setup_logger(self):
        """Setup logger with file and console output."""
        self.logger = logging.getLogger(f"grvt_{self.ticker}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers = []
        os.makedirs('logs', exist_ok=True)
        fh = logging.FileHandler(f'logs/grvt_{self.ticker}_log.txt')
        fh.setLevel(logging.DEBUG)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)
        self.logger.addHandler(fh)
        self.logger.addHandler(ch)

    async def initialize_clients(self):
        """Initialize exchange clients."""
        self.logger.info("Initializing GRVT client...")
        
        # Initialize GRVT client
        grvt_config = Config({
            'ticker': self.ticker,
            'tick_size': self.grvt_tick_size or Decimal('1'),
            'quantity': self.order_quantity,
            'contract_id': self.grvt_contract_id or f"{self.ticker}_USDT_Perp",
            'direction': 'buy',
            'close_order_side': 'sell'
        })
        
        try:
            self.grvt_client = GrvtClient(grvt_config)
            self.logger.info("GRVT client initialized successfully")
        except Exception as e:
            self.logger.error(f"Failed to initialize GRVT client: {e}")
            raise

        self.logger.info("Initializing Lighter client...")
        
        # Initialize Lighter client
        try:
            self.lighter_client = LighterClient(self.ticker, self.logger)
            await self.lighter_client.connect()
            self.logger.info("Lighter client initialized successfully")
            
            # Get market config
            lighter_config = await self.lighter_client.get_market_config()
            self.lighter_market_id = lighter_config[0]
            
            # Set config for order manager
            self.order_manager.set_lighter_config(
                self.lighter_client.lighter_client,
                self.lighter_market_id,
                lighter_config[1],  # base_amount_multiplier
                lighter_config[2],  # price_multiplier
                Decimal('0.1')      # tick_size (placeholder)
            )
            
        except Exception as e:
            self.logger.error(f"Failed to initialize Lighter client: {e}")
            raise

        # Get GRVT contract info
        try:
            contract_id, tick_size = await self.grvt_client.get_contract_attributes()
            self.grvt_contract_id = contract_id
            self.grvt_tick_size = tick_size
            self.logger.info(f"GRVT contract: {contract_id}, tick size: {tick_size}")
        except Exception as e:
            self.logger.warning(f"Could not get GRVT contract info: {e}")
            self.grvt_contract_id = f"{self.ticker}_USDT_Perp"
            self.grvt_tick_size = Decimal('1')

        # Initialize position tracker
        self.position_tracker = PositionTracker(
            ticker=self.ticker,
            grvt_client=self.grvt_client,
            grvt_contract_id=self.grvt_contract_id,
            lighter_base_url=self.lighter_client.base_url,
            account_index=self.lighter_client.account_index,
            logger=self.logger
        )

    async def connect(self):
        """Connect to exchanges."""
        self.logger.info("Connecting to exchanges...")
        
        # Connect to GRVT WebSocket
        try:
            await self.grvt_client.connect()
            self.logger.info("GRVT WebSocket connected")
        except Exception as e:
            self.logger.error(f"Failed to connect to GRVT WebSocket: {e}")
            raise

        # Setup Lighter WebSocket
        try:
            # Set Lighter config
            self.ws_manager.set_lighter_config(
                self.lighter_client.lighter_client,
                self.lighter_market_id,
                self.lighter_client.account_index
            )
            # Start Lighter WebSocket task
            self.ws_manager.start_lighter_websocket()
            self.logger.info("Lighter WebSocket task started")
            
            # Wait for Lighter order book data
            self.logger.info("⏳ Waiting for Lighter order book data...")
            timeout = 10
            start_time = time.time()
            while not self.order_book_manager.lighter_order_book_ready and not self.stop_flag:
                if time.time() - start_time > timeout:
                    self.logger.warning(f"⚠️ Timeout waiting for Lighter order book data after {timeout}s")
                    break
                await asyncio.sleep(0.5)
            
            if self.order_book_manager.lighter_order_book_ready:
                self.logger.info("✅ Lighter order book data received")
        except Exception as e:
            self.logger.error(f"Failed to setup Lighter WebSocket: {e}")
            raise

    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown."""
        def signal_handler(signum, frame):
            self.logger.info(f"Received signal {signum}, initiating shutdown...")
            self.stop_flag = True
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    async def emergency_close_all(self):
        """Emergency close all positions."""
        self.logger.warning("EMERGENCY CLOSE: Starting emergency close procedure...")
        
        try:
            if self.position_tracker is None:
                self.logger.warning("Position tracker not initialized, skipping emergency close")
                return
            
            grvt_position = await self.position_tracker.get_grvt_position()
            lighter_position = await self.position_tracker.get_lighter_position()
            
            net_position = grvt_position + lighter_position
            
            if net_position != 0:
                self.logger.warning(f"Net position detected: {net_position}")
                self.logger.warning("Attempting to close positions...")
                # Add emergency close logic here
                
        except Exception as e:
            self.logger.error(f"Error during emergency close: {e}")

    async def cleanup(self):
        """Cleanup resources."""
        if self._cleanup_done:
            return
            
        self._cleanup_done = True
        self.logger.info("Cleaning up resources...")
        
        try:
            if self.grvt_client:
                await self.grvt_client.disconnect()
                self.logger.info("GRVT client disconnected")
        except Exception as e:
            self.logger.error(f"Error disconnecting GRVT client: {e}")
        
        try:
            if self.ws_manager and self.ws_manager.lighter_ws_task:
                self.ws_manager.lighter_ws_task.cancel()
                self.logger.info("Lighter WebSocket task cancelled")
        except Exception as e:
            self.logger.error(f"Error cancelling Lighter WebSocket task: {e}")
        
        self.logger.info("Cleanup completed")

    async def trading_loop(self):
        """Main trading loop."""
        self.logger.info("Starting trading loop...")
        
        while not self.stop_flag:
            try:
                self.loop_count += 1
                
                # Get GRVT BBO
                grvt_bid = self.order_book_manager.grvt_best_bid
                grvt_ask = self.order_book_manager.grvt_best_ask
                
                if not grvt_bid or not grvt_ask:
                    try:
                        grvt_bid, grvt_ask = await self.grvt_client.fetch_bbo_prices(self.grvt_contract_id)
                        if grvt_bid and grvt_ask:
                            self.order_book_manager.update_grvt_order_book(
                                [[grvt_bid, self.order_quantity]], 
                                [[grvt_ask, self.order_quantity]]
                            )
                            self.logger.debug(f"📊 [REST] GRVT BBO: {grvt_bid} / {grvt_ask}")
                    except Exception as e:
                        self.logger.debug(f"Failed to fetch GRVT BBO: {e}")
                        await asyncio.sleep(0.1)
                        continue
                
                # Get Lighter BBO (WebSocket first, then REST API)
                lighter_bid = self.order_book_manager.lighter_best_bid
                lighter_ask = self.order_book_manager.lighter_best_ask
                
                if not lighter_bid or not lighter_ask:
                    # Try REST API as fallback
                    try:
                        lighter_bid, lighter_ask = await self.lighter_client.fetch_bbo_prices()
                        if lighter_bid and lighter_ask:
                            self.logger.debug(f"📊 [REST] Lighter BBO: {lighter_bid} / {lighter_ask}")
                    except Exception as e:
                        self.logger.debug(f"Failed to fetch Lighter BBO: {e}")
                
                # Skip if data is not ready
                if not all([grvt_bid, grvt_ask, lighter_bid, lighter_ask]):
                    await asyncio.sleep(0.1)
                    continue
                
                # Calculate spread
                long_spread = lighter_bid - grvt_ask
                short_spread = grvt_bid - lighter_ask
                
                # Log prices periodically (every 5 seconds)
                current_time = time.time()
                if current_time - self.last_price_log_time > 5:
                    self.logger.info(
                        f"📊 Prices: GRVT {grvt_bid:.1f}/{grvt_ask:.1f} | "
                        f"Lighter {lighter_bid:.1f}/{lighter_ask:.1f} | "
                        f"Spread: L={long_spread:.2f}, S={short_spread:.2f}"
                    )
                    self.last_price_log_time = current_time
                
                # Get current threshold
                long_threshold = self.dynamic_threshold.get_long_threshold()
                short_threshold = self.dynamic_threshold.get_short_threshold()
                
                # Get current positions (GRVT + Lighter)
                net_position = self.position_tracker.get_grvt_lighter_net_position()
                
                # Check max position
                if abs(net_position) >= self.max_position:
                    self.logger.debug(f"Max position reached: {net_position}")
                    await asyncio.sleep(1)
                    continue
                
                # Long arbitrage: Lighter bid > GRVT ask + threshold
                if long_spread > long_threshold and net_position > -self.max_position:
                    self.logger.info(f"🚀 Long arbitrage: spread={long_spread:.2f} > threshold={long_threshold}")
                    
                    success = await self.order_manager.execute_grvt_long_arbitrage(
                        grvt_client=self.grvt_client,
                        lighter_client=self.lighter_client.lighter_client,
                        contract_id=self.grvt_contract_id,
                        quantity=self.order_quantity,
                        grvt_ask=grvt_ask,
                        fill_timeout=self.fill_timeout,
                        stop_flag=self.stop_flag
                    )
                    
                    if success:
                        self.logger.info("✅ Long arbitrage executed successfully")
                
                # Short arbitrage: GRVT bid > Lighter ask + threshold
                elif short_spread > short_threshold and net_position < self.max_position:
                    self.logger.info(f"🚀 Short arbitrage: spread={short_spread:.2f} > threshold={short_threshold}")
                    
                    success = await self.order_manager.execute_grvt_short_arbitrage(
                        grvt_client=self.grvt_client,
                        lighter_client=self.lighter_client.lighter_client,
                        contract_id=self.grvt_contract_id,
                        quantity=self.order_quantity,
                        grvt_bid=grvt_bid,
                        fill_timeout=self.fill_timeout,
                        stop_flag=self.stop_flag
                    )
                    
                    if success:
                        self.logger.info("✅ Short arbitrage executed successfully")
                
                # Update dynamic threshold with new spread observation
                self.dynamic_threshold.add_spread_observation(long_spread, short_spread)
                
                # Sleep to avoid tight loop
                await asyncio.sleep(0.1)
                
            except Exception as e:
                self.logger.error(f"Error in trading loop: {e}")
                await asyncio.sleep(1)

    async def run(self):
        """Main run method."""
        self.logger.info("=" * 60)
        self.logger.info(f"Starting GRVT-Lighter Arbitrage Bot for {self.ticker}")
        self.logger.info(f"Order quantity: {self.order_quantity}")
        self.logger.info(f"Max position: {self.max_position}")
        self.logger.info("=" * 60)
        
        try:
            # Initialize clients
            await self.initialize_clients()
            
            # Connect to exchanges
            await self.connect()
            
            # Setup signal handlers
            self._setup_signal_handlers()
            
            # Wait for WebSocket data to be ready
            self.logger.info("Waiting for order book data...")
            await asyncio.sleep(3)
            
            # Start trading loop
            await self.trading_loop()
            
        except asyncio.CancelledError:
            self.logger.info("Task cancelled")
        except KeyboardInterrupt:
            self.logger.info("Keyboard interrupt received")
        except Exception as e:
            self.logger.error(f"Fatal error: {e}")
            self.logger.error(traceback.format_exc())
        finally:
            self.logger.info("Initiating shutdown sequence...")
            self.stop_flag = True
            
            # Emergency close
            await self.emergency_close_all()
            
            # Cleanup
            await self.cleanup()
            
            self.logger.info("Shutdown complete")
