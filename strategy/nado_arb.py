"""
Nado + Lighter Cross-Exchange Arbitrage Strategy.

This module implements an arbitrage strategy that:
- Buys on Nado (taker) and sells on Lighter (maker) for long opportunities
- Sells on Nado (taker) and buys on Lighter (maker) for short opportunities
"""
import asyncio
import logging
import sys
import time
import traceback
from decimal import Decimal
from typing import Optional

# Add parent directory to path for imports
sys.path.insert(0, str(__file__).rsplit('/', 2)[0])

from strategy.order_manager import OrderManager
from strategy.order_book_manager import OrderBookManager
from strategy.position_tracker import PositionTracker
from strategy.websocket_manager import WebSocketManagerWrapper
from strategy.data_logger import DataLogger
from strategy.dynamic_threshold import DynamicThresholdCalculator
from strategy.nado_client import NadoClient
from strategy.trade_statistics import TradeStatistics


class NadoArb:
    """Nado + Lighter Arbitrage Strategy.
    
    This class implements cross-exchange arbitrage between Nado and Lighter:
    - Long opportunity: Buy on Nado, Sell on Lighter
    - Short opportunity: Sell on Nado, Buy on Lighter
    """
    
    # Default configuration
    DEFAULT_LONG_EX_THRESHOLD = Decimal('10')
    DEFAULT_SHORT_EX_THRESHOLD = Decimal('10')
    
    # Position limits
    DEFAULT_MAX_POSITION = Decimal('0')
    
    # Close thresholds (for closing existing positions with minimal profit)
    DEFAULT_CLOSE_THRESHOLD_MULTIPLIER = Decimal('0.5')
    DEFAULT_MIN_CLOSE_SPREAD = Decimal('1')
    
    # Price tolerance
    DEFAULT_PRICE_TOLERANCE_PCT = Decimal('0.05')
    
    def __init__(self, ticker: str, order_quantity: Decimal,
                 fill_timeout: int = 5, max_position: Decimal = Decimal('0'),
                 long_ex_threshold: Decimal = Decimal('10'),
                 short_ex_threshold: Decimal = Decimal('10'),
                 robot_id: str = None):
        """Initialize the Nado arbitrage strategy.
        
        Args:
            ticker: Trading pair symbol (e.g., 'BTC')
            order_quantity: Order size for each trade
            fill_timeout: Timeout for order fills (seconds)
            max_position: Maximum position size
            long_ex_threshold: Long opportunity threshold
            short_ex_threshold: Short opportunity threshold
            robot_id: Unique robot identifier
        """
        self.ticker = ticker.upper()
        self.order_quantity = order_quantity
        self.fill_timeout = fill_timeout
        self.max_position = abs(max_position)
        self.long_ex_threshold = abs(long_ex_threshold)
        self.short_ex_threshold = abs(short_ex_threshold)
        self.robot_id = robot_id or f"nado_{ticker.lower()}"
        
        # Execution lock to prevent concurrent arbitrage execution
        self.is_executing = False
        self.execution_lock = asyncio.Lock()
        
        # Initialize components
        self.logger = self._setup_logger()
        self.order_book_manager = OrderBookManager(self.logger)
        
        # Initialize trade statistics
        self.trade_statistics = TradeStatistics(
            robot_id=self.robot_id,
            ticker=self.ticker,
            logger=self.logger
        )
        
        self.order_manager = OrderManager(
            self.order_book_manager, 
            self.logger,
            trade_statistics=self.trade_statistics
        )
        self.position_tracker = None  # Will be initialized in _setup_clients
        self.ws_manager = WebSocketManagerWrapper(self.order_book_manager, self.logger)
        
        # Nado client
        self.nado_client: Optional[NadoClient] = None
        self.nado_product_id: Optional[int] = None
        self.nado_tick_size: Optional[Decimal] = None
        
        # Lighter configuration (similar to edgex_arb.py)
        import os
        self.lighter_base_url = os.getenv('LIGHTER_BASE_URL', 'https://mainnet.zklighter.elliot.ai')
        self.account_index = int(os.getenv('LIGHTER_ACCOUNT_INDEX', '692775'))
        self.api_key_index = int(os.getenv('LIGHTER_API_KEY_INDEX', '0'))
        self.lighter_client = None
        
        # Lighter market config (will be set during initialization)
        self.lighter_market_index = None
        self.base_amount_multiplier = None
        self.price_multiplier = None
        self.tick_size = None
        
        # Data logger
        self.data_logger = DataLogger(
            exchange="nado",
            ticker=self.ticker,
            logger=self.logger,
            robot_id=self.robot_id
        )
        
        # Dynamic threshold (optional)
        self.use_dynamic_threshold = False
        self.dynamic_threshold = None
        
        # Stop flag
        self.stop_flag = False
        
        # Position tracking
        self.position_open_time: Optional[float] = None
        
        # Time-based close configuration
        self.enable_time_based_close = True
        self.hold_time_threshold_hours = Decimal('0.02')  # ~1.2 minutes
        self.stage_1_multiplier = Decimal('0.5')
        self.stage_2_hours = Decimal('0.08')  # ~5 minutes
        self.stage_2_multiplier = Decimal('0.3')
        self.stage_3_hours = Decimal('0.17')  # ~10 minutes
        self.stage_3_multiplier = Decimal('0.1')
        self.aggressive_close_threshold = Decimal('1')  # Force close if spread > 1
        
        # Close threshold configuration
        self.close_threshold_multiplier = self.DEFAULT_CLOSE_THRESHOLD_MULTIPLIER
        self.min_close_spread = self.DEFAULT_MIN_CLOSE_SPREAD
        
        # Price tolerance
        self.price_tolerance_pct = self.DEFAULT_PRICE_TOLERANCE_PCT
        
        # Position sync control
        self.last_position_sync_time = None
        self.position_sync_interval = 60
        
        # Heartbeat log control
        self.last_heartbeat_time = None
        self.heartbeat_interval = 60
        
        # BBO logging control
        self.last_bbo_log_time = None
        self.bbo_log_interval = 3600
        
        # Opportunity logging control
        self.last_opportunity_key = None
        self.opportunity_log_interval = 5
        
        # Strategy initialized
    
    def _setup_logger(self):
        """Configure logging for the strategy."""
        logger = logging.getLogger(f"arbi_{self.robot_id}")
        
        # Clear existing handlers
        logger.handlers.clear()
        
        # Create logs directory if needed
        import os
        os.makedirs('logs', exist_ok=True)
        
        # File handler
        log_file = f"logs/{self.robot_id}_arb_log.txt"
        file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        
        # Formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        logger.setLevel(logging.DEBUG)
        
        return logger
    
    def initialize_lighter_client(self):
        """Initialize the Lighter client (same as edgex_arb.py)."""
        if self.lighter_client is None:
            import os
            from lighter.signer_client import SignerClient
            
            api_key_private_key = os.getenv('API_KEY_PRIVATE_KEY')
            if not api_key_private_key:
                raise Exception("API_KEY_PRIVATE_KEY environment variable not set")

            # Create api_private_keys dictionary with the index as key
            api_private_keys = {self.api_key_index: api_key_private_key}

            self.lighter_client = SignerClient(
                url=self.lighter_base_url,
                account_index=self.account_index,
                api_private_keys=api_private_keys,
            )

            err = self.lighter_client.check_client()
            if err is not None:
                raise Exception(f"CheckClient error: {err}")

            # Lighter client initialized
        return self.lighter_client
    
    def get_lighter_market_config(self):
        """Get Lighter market configuration (same as edgex_arb.py)."""
        import requests
        url = f"{self.lighter_base_url}/api/v1/orderBooks"
        headers = {"accept": "application/json"}

        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()

            if not response.text.strip():
                raise Exception("Empty response from Lighter API")

            data = response.json()

            if "order_books" not in data:
                raise Exception("Unexpected response format")

            for market in data["order_books"]:
                if market["symbol"] == self.ticker:
                    price_multiplier = pow(10, market["supported_price_decimals"])
                    return (market["market_id"],
                            pow(10, market["supported_size_decimals"]),
                            price_multiplier,
                            Decimal("1") / (Decimal("10") ** market["supported_price_decimals"]))
            raise Exception(f"Ticker {self.ticker} not found")

        except Exception as e:
            self.logger.error(f"⚠️ Error getting market config: {e}")
            raise
    
    async def initialize(self):
        """Initialize Nado client and Lighter connections."""
        try:
            # ========== Initialize Lighter first (same as edgex_arb.py) ==========
            self.initialize_lighter_client()
            
            # Get Lighter market config
            (self.lighter_market_index, self.base_amount_multiplier,
             self.price_multiplier, self.tick_size) = self.get_lighter_market_config()
            
            # Contract info loaded
            
            # Configure Lighter in modules
            self.order_manager.set_lighter_config(
                self.lighter_client, self.lighter_market_index,
                self.base_amount_multiplier, self.price_multiplier, self.tick_size,
                self.lighter_base_url
            )
            
            self.ws_manager.set_lighter_config(
                self.lighter_client, self.lighter_market_index, self.account_index
            )
            
            # ========== Initialize Nado client ==========
            
            # Initialize Nado client
            self.nado_client = NadoClient()
            
            # Get product info for the ticker
            # Nado uses "BTC-PERP" format for perpetual contracts
            nado_ticker = self.ticker
            if self.ticker == 'BTC':
                nado_ticker = 'BTC-PERP'
            elif self.ticker == 'ETH':
                nado_ticker = 'ETH-PERP'
            
            product_info = await self.nado_client.get_product_info(nado_ticker)
            if product_info:
                self.nado_product_id = product_info.get('id')
                # tickSize from API is in Wei format (x18), convert to USD
                tick_size_wei = Decimal(str(product_info.get('tickSize', '100000000000000000')))  # Default: 0.1 in Wei
                self.nado_tick_size = tick_size_wei / Decimal('1e18')
                # Nado product info loaded
            else:
                # Fallback: use default product ID for BTC
                self.nado_product_id = 2  # BTC-PERP default
                self.nado_tick_size = Decimal('0.1')
                self.logger.warning(f"⚠️ Could not find product info for {self.ticker} ({nado_ticker}), using defaults (ID=2 for BTC-PERP)")
            
            # Configure Nado in order manager
            self.order_manager.set_nado_config(
                client=self.nado_client,
                product_id=self.nado_product_id,
                tick_size=self.nado_tick_size
            )
            
            # Configure WebSocket manager
            self.ws_manager.set_nado_config(
                product_id=self.nado_product_id,
                wallet_address=self.nado_client.wallet_address
            )
            
            # Initialize position tracker
            self.position_tracker = PositionTracker(
                ticker=self.ticker,
                logger=self.logger,
                nado_client=self.nado_client,
                nado_product_id=self.nado_product_id,
                lighter_base_url=self.lighter_base_url,
                account_index=self.account_index
            )
            
            # Set callbacks
            self.order_manager.set_callbacks(
                on_order_filled=self._handle_order_filled
            )
            
            self.ws_manager.set_callbacks(
                on_lighter_order_filled=self.order_manager.handle_lighter_order_filled,
                on_lighter_order_canceled=self._handle_lighter_order_canceled
            )
            
            # All clients initialized
            
        except Exception as e:
            self.logger.error(f"❌ Failed to initialize: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            raise
    
    async def start(self):
        """Start the arbitrage strategy."""
        try:
            # Initialize
            await self.initialize()
            
            # Start WebSocket connections
            
            # Run main loop
            await self.run()
            
        except Exception as e:
            self.logger.error(f"❌ Error starting strategy: {e}")
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            raise
        finally:
            self.shutdown()
    
    async def run(self):
        """Main trading loop."""
        try:
            while not self.stop_flag:
                try:
                    await self.trading_loop()
                except Exception as e:
                    self.logger.error(f"❌ Trading loop error: {e}")
                    self.logger.error(f"Traceback: {traceback.format_exc()}")
                    await asyncio.sleep(1)
                    
        except KeyboardInterrupt:
            self.logger.info("\n🛑 Received interrupt signal...")
        except asyncio.CancelledError:
            self.logger.info("\n🛑 Task cancelled...")
        except Exception as e:
            self.logger.error(f"❌ Unhandled exception in run(): {e}")
            self.logger.error(f"Full traceback: {traceback.format_exc()}")
        finally:
            self.logger.info("🔄 Cleaning up...")
            self.shutdown()
    
    async def trading_loop(self):
        """Main trading loop logic."""
        # Initialize clients first
        try:
            await self.initialize()
        except Exception as e:
            self.logger.error(f"❌ Failed to initialize: {e}")
            return
        
        # Start Lighter WebSocket connection first (for order book data)
        # Start WebSocket connections
        try:
            self.ws_manager.start_lighter_websocket()
        except Exception as e:
            pass
        
        self.ws_manager.start_nado_websocket()
        
        # Wait for order book data
        wait_start = time.time()
        max_wait = 30  # 30 seconds max wait
        
        while (not self.order_book_manager.lighter_order_book_ready and 
               not self.stop_flag and
               time.time() - wait_start < max_wait):
            await asyncio.sleep(0.1)
        
        # Order book ready check (silent)
        
        # Initial position sync
        await self._sync_positions()
        
        # Main loop
        while not self.stop_flag:
            current_time = time.time()
            
            # Check for stop
            if self.stop_flag:
                break
            
            # Check position sync
            if (self.last_position_sync_time is None or 
                current_time - self.last_position_sync_time >= self.position_sync_interval):
                await self._sync_positions()
            
            # Get current position
            current_position = self.position_tracker.get_current_nado_position()
            
            # Calculate thresholds for closing
            if current_position != 0:
                close_multiplier, min_close_spread, stage_name = self._get_time_based_close_thresholds(
                    self.short_ex_threshold
                )
                long_close_threshold = max(self.long_ex_threshold * close_multiplier, min_close_spread)
                short_close_threshold = max(self.short_ex_threshold * close_multiplier, min_close_spread)
            else:
                long_close_threshold = self.long_ex_threshold
                short_close_threshold = self.short_ex_threshold
            
            # Get BBO prices
            nado_bid, nado_ask = await self._get_nado_bbo()
            lighter_bid, lighter_ask = self._get_lighter_bbo()
            
            if not (nado_bid and nado_ask and lighter_bid and lighter_ask):
                await asyncio.sleep(0.1)
                continue
            
            # Calculate spreads
            long_spread = lighter_bid - nado_ask  # Long: Buy Nado, Sell Lighter
            short_spread = nado_bid - lighter_ask  # Short: Sell Nado, Buy Lighter
            
            # Check long opportunity (buy Nado, sell Lighter)
            # Allow if: current_position < max_position (can open long or add to existing long)
            if long_spread > long_close_threshold:
                if current_position < self.max_position:
                    # Double-check position before executing (with lock)
                    async with self.execution_lock:
                        if self.is_executing:
                            await asyncio.sleep(0.01)
                            continue
                        
                        # Re-check position after acquiring lock (refresh from exchange for accuracy)
                        await self._sync_positions()
                        current_position_check = self.position_tracker.get_current_nado_position()
                        
                        if current_position_check >= self.max_position:
                            self.logger.info(
                                f"📊 [OPPORTUNITY SKIPPED] Long Nado - Position limit reached! "
                                f"Position={current_position_check}/{self.max_position}"
                            )
                            await asyncio.sleep(0.01)
                            continue
                        
                        self.is_executing = True
                    
                    try:
                        self.logger.info(
                            f"🔍 [Long Opportunity] Spread={long_spread:.2f} > Threshold={long_close_threshold:.2f}, "
                            f"Position={current_position_check}/{self.max_position}"
                        )
                        await self._execute_long_trade(nado_ask, lighter_bid)
                    finally:
                        self.is_executing = False
                else:
                    self.logger.debug(
                        f"⏸️ [Long Skipped] Spread={long_spread:.2f} > Threshold={long_close_threshold:.2f}, "
                        f"but Position={current_position} >= Max={self.max_position}"
                    )
            
            # Check short opportunity (sell Nado, buy Lighter)
            # Allow if: current_position > -max_position (can open short or add to existing short)
            if short_spread > short_close_threshold:
                if current_position > -self.max_position:
                    # Double-check position before executing (with lock)
                    async with self.execution_lock:
                        if self.is_executing:
                            await asyncio.sleep(0.01)
                            continue
                        
                        # Re-check position after acquiring lock (refresh from exchange for accuracy)
                        await self._sync_positions()
                        current_position_check = self.position_tracker.get_current_nado_position()
                        
                        if current_position_check <= -self.max_position:
                            self.logger.info(
                                f"📊 [OPPORTUNITY SKIPPED] Short Nado - Position limit reached! "
                                f"Position={current_position_check}/{-self.max_position}"
                            )
                            await asyncio.sleep(0.01)
                            continue
                        
                        self.is_executing = True
                    
                    try:
                        self.logger.info(
                            f"🔍 [Short Opportunity] Spread={short_spread:.2f} > Threshold={short_close_threshold:.2f}, "
                            f"Position={current_position_check}/{-self.max_position}"
                        )
                        await self._execute_short_trade(nado_bid, lighter_ask)
                    finally:
                        self.is_executing = False
                else:
                    self.logger.debug(
                        f"⏸️ [Short Skipped] Spread={short_spread:.2f} > Threshold={short_close_threshold:.2f}, "
                        f"but Position={current_position} <= Max={-self.max_position}"
                    )
            
            # Heartbeat log (simplified)
            if (self.last_heartbeat_time is None or 
                current_time - self.last_heartbeat_time >= self.heartbeat_interval):
                self.logger.info(
                    f"Nado: {nado_bid:.1f}/{nado_ask:.1f} | "
                    f"Lighter: {lighter_bid:.1f}/{lighter_ask:.1f} | "
                    f"L={float(long_spread):.2f} S={float(short_spread):.2f} | "
                    f"Pos: {current_position}"
                )
                self.last_heartbeat_time = current_time
            
            await asyncio.sleep(0.01)
    
    async def _get_nado_bbo(self) -> tuple:
        """Get Nado best bid/ask prices."""
        # Try WebSocket data first
        nado_bbo = self.order_book_manager.get_nado_bbo()
        if nado_bbo[0] and nado_bbo[1]:
            return nado_bbo
        
        # Fallback to REST API
        try:
            return await self.order_manager.fetch_nado_bbo_prices()
        except Exception as e:
            self.logger.warning(f"⚠️ Failed to fetch Nado BBO: {e}")
            return None, None
    
    def _get_lighter_bbo(self) -> tuple:
        """Get Lighter best bid/ask prices (with REST API fallback)."""
        # Try WebSocket data first
        lighter_bbo = self.order_book_manager.get_lighter_bbo()
        if lighter_bbo[0] and lighter_bbo[1]:
            return lighter_bbo
        
        # Fallback to REST API
        try:
            return self.order_manager.fetch_lighter_bbo_from_rest()
        except Exception as e:
            self.logger.debug(f"⚠️ Failed to fetch Lighter BBO (will retry): {e}")
            return None, None
    
    async def _sync_positions(self):
        """Synchronize cached positions with actual positions."""
        try:
            # Get actual positions from exchanges
            actual_nado_pos = await self.position_tracker.get_nado_position()
            actual_lighter_pos = await self.position_tracker.get_lighter_position()
            
            # Get cached positions
            cached_nado_pos = self.position_tracker.get_current_nado_position()
            cached_lighter_pos = self.position_tracker.get_current_lighter_position()
            
            # Check for mismatch
            nado_diff = abs(actual_nado_pos - cached_nado_pos)
            lighter_diff = abs(actual_lighter_pos - cached_lighter_pos)
            
            if nado_diff > Decimal('0.01') or lighter_diff > Decimal('0.01'):
                self.position_tracker.nado_position = actual_nado_pos
                self.position_tracker.lighter_position = actual_lighter_pos
            
            self.last_position_sync_time = time.time()
            
        except Exception as e:
            self.logger.error(f"❌ Position sync failed: {e}")
    
    async def _execute_long_trade(self, expected_nado_ask: Decimal, expected_lighter_bid: Decimal):
        """Execute long arbitrage: Buy on Nado (taker), Sell on Lighter (maker).
        
        Args:
            expected_nado_ask: Expected Nado ask price
            expected_lighter_bid: Expected Lighter bid price
        """
        # Check price tolerance
        current_nado_ask = (await self._get_nado_bbo())[1]
        if current_nado_ask:
            price_change = abs((current_nado_ask - expected_nado_ask) / expected_nado_ask * 100)
            if price_change > float(self.price_tolerance_pct):
                return
        
        # Execute arbitrage using order manager
        success = await self.order_manager.execute_nado_long_arbitrage(
            nado_client=self.nado_client,
            lighter_client=self.lighter_client,
            product_id=self.nado_product_id,
            quantity=self.order_quantity,
            nado_ask=current_nado_ask or expected_nado_ask,
            fill_timeout=self.fill_timeout,
            stop_flag=self.stop_flag
        )
        
        if success:
            self.position_open_time = time.time()
    
    async def _execute_short_trade(self, expected_nado_bid: Decimal, expected_lighter_ask: Decimal):
        """Execute short arbitrage: Sell on Nado (taker), Buy on Lighter (maker).
        
        Args:
            expected_nado_bid: Expected Nado bid price
            expected_lighter_ask: Expected Lighter ask price
        """
        # Check price tolerance
        current_nado_bid = (await self._get_nado_bbo())[0]
        if current_nado_bid:
            price_change = abs((current_nado_bid - expected_nado_bid) / expected_nado_bid * 100)
            if price_change > float(self.price_tolerance_pct):
                return
        
        # Execute arbitrage using order manager
        success = await self.order_manager.execute_nado_short_arbitrage(
            nado_client=self.nado_client,
            lighter_client=self.lighter_client,
            product_id=self.nado_product_id,
            quantity=self.order_quantity,
            nado_bid=current_nado_bid or expected_nado_bid,
            fill_timeout=self.fill_timeout,
            stop_flag=self.stop_flag
        )
        
        if success:
            self.position_open_time = time.time()
    
    def _get_time_based_close_thresholds(self, base_threshold: Decimal):
        """Calculate time-based close thresholds.
        
        Returns:
            (multiplier, min_spread, stage_name)
        """
        if not self.position_open_time:
            return Decimal('1'), self.min_close_spread, "default"
        
        holding_hours = (time.time() - self.position_open_time) / 3600
        
        if holding_hours < float(self.hold_time_threshold_hours):
            return self.close_threshold_multiplier, self.min_close_spread, "default"
        elif holding_hours < float(self.stage_2_hours):
            return self.stage_1_multiplier, self.min_close_spread, "stage1"
        elif holding_hours < float(self.stage_3_hours):
            return self.stage_2_multiplier, self.min_close_spread, "stage2"
        else:
            return self.stage_3_multiplier, self.aggressive_close_threshold, "stage3"
    
    def _handle_order_filled(self, order_data: dict):
        """Handle order fill callback."""
        try:
            # Order filled
            
            # Update data logger
            self.data_logger.log_trade_to_csv(
                exchange="nado",
                side=order_data.get('side', 'unknown'),
                quantity=Decimal(str(order_data.get('filled_base_amount', 0))),
                price=Decimal(str(order_data.get('avg_filled_price', 0)))
            )
            
        except Exception as e:
            self.logger.error(f"Error handling order filled: {e}")
    
    def _handle_lighter_order_canceled(self, order_data: dict):
        """Handle Lighter order canceled callback (e.g., margin-not-allowed)."""
        try:
            client_order_id = order_data.get('client_order_id', 'UNKNOWN')
            status = order_data.get('status', 'UNKNOWN')
            self.logger.error(
                f"❌ [Lighter Order Canceled] Order {client_order_id} was canceled with status '{status}'. "
                f"This will cause position imbalance if Nado order was already filled!")
            
            # Set canceled flag in order manager
            if self.order_manager:
                self.order_manager.lighter_order_canceled = True
                self.order_manager.lighter_order_filled = False
                self.order_manager.waiting_for_lighter_fill = False
                self.order_manager.order_execution_complete = False
            
        except Exception as e:
            self.logger.error(f"Error handling Lighter order canceled: {e}")
    
    def shutdown(self):
        """Shutdown the strategy."""
        self.stop_flag = True
        self.logger.info("🛑 Shutting down NadoArb strategy...")
        
        # Close data logger
        if hasattr(self, 'data_logger'):
            self.data_logger.close()
        
        # Shutdown WebSocket
        if hasattr(self, 'ws_manager'):
            self.ws_manager.shutdown()
        
        # Close Nado client
        if self.nado_client:
            import asyncio
            try:
                asyncio.get_event_loop().run_until_complete(self.nado_client.close())
            except:
                pass
        
        self.logger.info("✅ NadoArb shutdown complete")


# Main entry point
if __name__ == '__main__':
    import argparse
    from decimal import Decimal
    
    parser = argparse.ArgumentParser(description='Nado + Lighter Arbitrage Bot')
    parser.add_argument('--ticker', type=str, default='BTC', help='Ticker symbol')
    parser.add_argument('--size', type=str, required=True, help='Order quantity')
    parser.add_argument('--max-position', type=Decimal, default=Decimal('0'), help='Max position')
    parser.add_argument('--long-threshold', type=Decimal, default=Decimal('10'), help='Long threshold')
    parser.add_argument('--short-threshold', type=Decimal, default=Decimal('10'), help='Short threshold')
    parser.add_argument('--robot-id', type=str, default=None, help='Robot ID')
    
    args = parser.parse_args()
    
    # Configure logging
    logging.basicConfig(level=logging.INFO)
    
    # Create and run strategy
    strategy = NadoArb(
        ticker=args.ticker,
        order_quantity=Decimal(args.size),
        max_position=args.max_position,
        long_ex_threshold=args.long_threshold,
        short_ex_threshold=args.short_threshold,
        robot_id=args.robot_id
    )
    
    asyncio.run(strategy.start())

