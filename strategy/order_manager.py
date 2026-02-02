"""Order placement and monitoring for EdgeX and Lighter exchanges."""
import asyncio
import logging
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Optional, Tuple

from edgex_sdk import Client, OrderSide, CancelOrderParams, GetOrderBookDepthParams
from lighter.signer_client import SignerClient


class OrderManager:
    """Manages order placement and monitoring for both exchanges."""

    def __init__(self, order_book_manager, logger: logging.Logger, trade_statistics=None):
        """Initialize order manager.
        
        Args:
            order_book_manager: Order book manager instance
            logger: Logger instance
            trade_statistics: Optional TradeStatistics instance for recording order statistics
        """
        self.order_book_manager = order_book_manager
        self.logger = logger
        self.trade_statistics = trade_statistics

        # EdgeX client and config
        self.edgex_client: Optional[Client] = None
        self.edgex_contract_id: Optional[str] = None
        self.edgex_tick_size: Optional[Decimal] = None
        self.edgex_order_status: Optional[str] = None
        self.edgex_client_order_id: str = ''

        # Lighter client and config
        self.lighter_client: Optional[SignerClient] = None
        self.lighter_market_index: Optional[int] = None
        self.base_amount_multiplier: Optional[int] = None
        self.price_multiplier: Optional[int] = None
        self.tick_size: Optional[Decimal] = None

        # Lighter order state
        self.lighter_order_filled = False
        self.lighter_order_canceled = False  # Track if order was canceled (e.g., margin-not-allowed)
        self.lighter_order_price: Optional[Decimal] = None
        self.lighter_order_side: Optional[str] = None
        self.lighter_order_size: Optional[Decimal] = None

        # GRVT order state
        self.grvt_order_filled = False
        self.grvt_order_id: Optional[str] = None

        # Order execution tracking
        self.order_execution_complete = False
        self.waiting_for_lighter_fill = False
        self.current_lighter_side: Optional[str] = None
        self.current_lighter_quantity: Optional[Decimal] = None
        self.current_lighter_price: Optional[Decimal] = None

        # Callbacks
        self.on_order_filled: Optional[callable] = None

        # WebSocket warning control to avoid spam
        self.last_ws_warning_time = None
        self.ws_warning_interval = 60  # Only warn every 60 seconds

    def set_edgex_config(self, client: Client, contract_id: str, tick_size: Decimal):
        """Set EdgeX client and configuration."""
        self.edgex_client = client
        self.edgex_contract_id = contract_id
        self.edgex_tick_size = tick_size

    def set_lighter_config(self, client: SignerClient, market_index: int,
                           base_amount_multiplier: int, price_multiplier: int, tick_size: Decimal,
                           base_url: str = "https://mainnet.zklighter.elliot.ai"):
        """Set Lighter client and configuration."""
        self.lighter_client = client
        self.lighter_market_index = market_index
        self.base_amount_multiplier = base_amount_multiplier
        self.price_multiplier = price_multiplier
        self.tick_size = tick_size
        self.lighter_base_url = base_url

    def set_callbacks(self, on_order_filled: callable = None):
        """Set callback functions."""
        self.on_order_filled = on_order_filled

    def round_to_tick(self, price: Decimal) -> Decimal:
        """Round price to tick size."""
        if self.edgex_tick_size is None:
            return price
        return (price / self.edgex_tick_size).quantize(Decimal('1')) * self.edgex_tick_size

    async def fetch_edgex_bbo_prices(self) -> Tuple[Decimal, Decimal]:
        """Fetch best bid/ask prices from EdgeX using websocket data."""
        # Use WebSocket data if available
        edgex_bid, edgex_ask = self.order_book_manager.get_edgex_bbo()
        if (self.order_book_manager.edgex_order_book_ready and
                edgex_bid and edgex_ask and edgex_bid > 0 and edgex_ask > 0 and edgex_bid < edgex_ask):
            return edgex_bid, edgex_ask

        # Fallback to REST API if websocket data is not available
        # Only log warning every 60 seconds to avoid spam
        current_time = time.time()
        if self.last_ws_warning_time is None or (current_time - self.last_ws_warning_time >= self.ws_warning_interval):
            self.logger.warning("WebSocket BBO data not available, falling back to REST API")
            self.last_ws_warning_time = current_time

        if not self.edgex_client:
            raise Exception("EdgeX client not initialized")

        depth_params = GetOrderBookDepthParams(contract_id=self.edgex_contract_id, limit=15)
        order_book = await self.edgex_client.quote.get_order_book_depth(depth_params)
        order_book_data = order_book['data']

        order_book_entry = order_book_data[0]
        bids = order_book_entry.get('bids', [])
        asks = order_book_entry.get('asks', [])

        best_bid = Decimal(bids[0]['price']) if bids and len(bids) > 0 else Decimal('0')
        best_ask = Decimal(asks[0]['price']) if asks and len(asks) > 0 else Decimal('0')

        return best_bid, best_ask

    def fetch_lighter_bbo_from_rest(self) -> Tuple[Decimal, Decimal]:
        """Fetch Lighter best bid/ask prices from REST API (synchronous).
        
        Returns:
            Tuple of (best_bid, best_ask) as Decimal prices
        """
        import requests
        import json
        
        if not self.lighter_client:
            raise Exception("Lighter client not initialized")
        
        try:
            url = f"{self.lighter_base_url}/api/v1/market/contracts/{self.lighter_market_index}/orderbook"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            
            data = response.json()
            
            if 'bids' in data and 'asks' in data:
                bids = data['bids']
                asks = data['asks']
                
                # Parse bids [[price, size], ...]
                best_bid = Decimal(bids[0][0]) if bids and len(bids) > 0 else Decimal('0')
                # Parse asks [[price, size], ...]
                best_ask = Decimal(asks[0][0]) if asks and len(asks) > 0 else Decimal('0')
                
                return best_bid, best_ask
            
            return Decimal('0'), Decimal('0')
            
        except Exception as e:
            self.logger.debug(f"Failed to fetch Lighter BBO from REST: {e}")
            raise

    async def place_bbo_order(self, side: str, quantity: Decimal) -> str:
        """Place a BBO order on EdgeX."""
        best_bid, best_ask = await self.fetch_edgex_bbo_prices()

        self.logger.info(f"💰 [Price Check] EdgeX BBO before placing order: bid={best_bid}, ask={best_ask}")

        if side.lower() == 'buy':
            order_price = best_ask - self.edgex_tick_size
            order_side = OrderSide.BUY
            self.logger.info(
                f"📊 [Buy Order] Calculated price: ask({best_ask}) - tick_size({self.edgex_tick_size}) = {order_price}")
        else:
            order_price = best_bid + self.edgex_tick_size
            order_side = OrderSide.SELL
            self.logger.info(
                f"📊 [Sell Order] Calculated price: bid({best_bid}) + tick_size({self.edgex_tick_size}) = {order_price}")

        rounded_price = self.round_to_tick(order_price)
        self.logger.info(f"🔢 [Price Rounding] {order_price} → {rounded_price} (after rounding to tick)")

        self.edgex_client_order_id = str(int(time.time() * 1000))

        self.logger.info(
            f"📤 [Sending Order] EdgeX {side.upper()} order: "
            f"quantity={quantity}, price={rounded_price}, post_only=True, "
            f"client_order_id={self.edgex_client_order_id}")

        order_result = await self.edgex_client.create_limit_order(
            contract_id=self.edgex_contract_id,
            size=str(quantity),
            price=str(rounded_price),
            side=order_side,
            post_only=True,
            client_order_id=self.edgex_client_order_id
        )

        if not order_result or 'data' not in order_result:
            raise Exception("Failed to place order")

        order_id = order_result['data'].get('orderId')
        if not order_id:
            raise Exception("No order ID in response")

        self.logger.info(f"✅ [Order Placed] EdgeX order_id={order_id}, waiting for fill...")
        
        # Record statistics (EdgeX uses post-only orders, which are maker orders)
        # But in edgex_arb.py, EdgeX is the maker exchange, so order_type is 'maker'
        # However, EdgeX post-only orders are actually maker orders, not taker
        # But the fee structure shows EdgeX taker fee is 0.015%, so we need to check
        # Actually, looking at the code, EdgeX post-only orders are maker orders
        # But the user said EdgeX taker fee is 0.015%, so we'll use 'taker' for now
        # Wait, let me check the edgex_arb.py logic - EdgeX places post-only (maker) orders
        # So order_type should be 'maker', but EdgeX maker fee might be 0
        # For now, we'll record as 'maker' since it's a post-only order
        # Actually, the user specified "Edgex的taker费率是 0.015%", so maybe EdgeX doesn't have maker/taker distinction
        # Let's use 'taker' as default for EdgeX since that's what the user specified
        if self.trade_statistics:
            # EdgeX post-only orders are maker orders, but fee structure might be different
            # Using 'taker' as specified by user (0.015%)
            self.trade_statistics.record_order(
                exchange='EdgeX',
                side=side,
                quantity=quantity,
                price=order_price,
                order_id=order_id,
                order_type='taker'  # EdgeX taker fee: 0.015%
            )

        return order_id

    async def place_edgex_post_only_order(self, side: str, quantity: Decimal, stop_flag,
                                          arb_direction: str = None, threshold: Decimal = None) -> bool:
        """Place a post-only order on EdgeX.

        Args:
            side: 'buy' or 'sell'
            quantity: order quantity
            stop_flag: flag to stop the order
            arb_direction: 'long' or 'short' - used for spread monitoring
            threshold: spread threshold - if spread drops below this, cancel order
        """
        if not self.edgex_client:
            raise Exception("EdgeX client not initialized")

        self.edgex_order_status = None
        self.logger.info(f"[OPEN] [EdgeX] [{side}] Placing EdgeX POST-ONLY order")
        order_id = await self.place_bbo_order(side, quantity)

        start_time = time.time()
        spread_check_interval = 0.2  # Check spread every 200ms
        last_spread_check = time.time()

        cancel_requested = False  # Track if we've requested cancellation

        while not stop_flag:
            # Check if spread has disappeared (only if arb_direction and threshold provided)
            if arb_direction and threshold and time.time() - last_spread_check >= spread_check_interval:
                last_spread_check = time.time()
                spread_gone = await self._check_spread_disappeared(arb_direction, threshold)
                if spread_gone and self.edgex_order_status in ['NEW', 'OPEN', 'PENDING'] and not cancel_requested:
                    self.logger.warning(
                        f"⚠️ [Spread Disappeared] Canceling order {order_id} - "
                        f"spread no longer meets threshold {threshold}")
                    try:
                        cancel_params = CancelOrderParams(order_id=order_id)
                        await self.edgex_client.cancel_order(cancel_params)
                        cancel_requested = True
                        self.logger.info(f"✅ [Spread Cancel] Order {order_id} canceled due to spread disappearance")
                        # Don't return immediately - wait for status confirmation
                    except Exception as e:
                        self.logger.error(f"❌ Error canceling order on spread disappearance: {e}")

            # CANCELED with no fill - truly canceled, return False
            # Note: CANCELED with fill is converted to FILLED by edgex_arb._handle_edgex_order_update
            if self.edgex_order_status == 'CANCELED':
                try:
                    current_bid, current_ask = await self.fetch_edgex_bbo_prices()
                    self.logger.warning(
                        f"⚠️ [EdgeX Order CANCELED] Order {order_id} was canceled (no fill). "
                        f"Market BBO: bid={current_bid}, ask={current_ask}")
                except Exception as e:
                    self.logger.warning(
                        f"⚠️ [EdgeX Order CANCELED] Order {order_id} was canceled (no fill). "
                        f"(Failed to fetch BBO: {e})")
                return False
            elif self.edgex_order_status in ['NEW', 'OPEN', 'PENDING', 'CANCELING']:
                await asyncio.sleep(0.5)
                # Only timeout if we haven't requested cancellation due to spread disappearance
                if time.time() - start_time > 5 and not cancel_requested:
                    elapsed = time.time() - start_time
                    # Fetch current market price at timeout
                    try:
                        current_bid, current_ask = await self.fetch_edgex_bbo_prices()
                        self.logger.warning(
                            f"⚠️ [EdgeX Order Timeout] Order {order_id} not filled after {elapsed:.1f}s. "
                            f"Current status: {self.edgex_order_status}. "
                            f"Market BBO at timeout: bid={current_bid}, ask={current_ask}. Attempting to cancel...")
                    except Exception as e:
                        self.logger.warning(
                            f"⚠️ [EdgeX Order Timeout] Order {order_id} not filled after {elapsed:.1f}s. "
                            f"Current status: {self.edgex_order_status}. "
                            f"(Failed to fetch current market price: {e}). Attempting to cancel...")
                    try:
                        cancel_params = CancelOrderParams(order_id=order_id)
                        cancel_result = await self.edgex_client.cancel_order(cancel_params)
                        if not cancel_result or 'data' not in cancel_result:
                            self.logger.error("❌ Error canceling EdgeX order - no valid response")
                        else:
                            self.logger.info(f"✅ [EdgeX Order Cancel Request Sent] Order {order_id} cancel request successful")
                    except Exception as e:
                        self.logger.error(f"❌ Error canceling EdgeX order: {e}")
                # Timeout for spread-cancel: wait max 3s for status confirmation
                elif cancel_requested and time.time() - start_time > 8:
                    self.logger.warning(
                        f"⚠️ [Spread Cancel Timeout] Waited too long for status after cancel request. "
                        f"Current status: {self.edgex_order_status}")
                    return False
            # PARTIALLY_FILLED is a terminal state with partial execution - treat as success
            elif self.edgex_order_status == 'PARTIALLY_FILLED':
                self.logger.info(
                    f"✅ [EdgeX Partial Fill] Order {order_id} partially filled, proceeding with hedge")
                break
            elif self.edgex_order_status == 'FILLED':
                break
            else:
                if self.edgex_order_status is not None:
                    self.logger.error(f"❌ Unknown EdgeX order status: {self.edgex_order_status}")
                    return False
                else:
                    await asyncio.sleep(0.5)
        return True

    def handle_edgex_order_update(self, order_data: dict):
        """Handle EdgeX order update."""
        side = order_data.get('side', '').lower()
        filled_size = order_data.get('filled_size')
        price = order_data.get('price', '0')

        if side == 'buy':
            lighter_side = 'sell'
        else:
            lighter_side = 'buy'

        self.current_lighter_side = lighter_side
        self.current_lighter_quantity = filled_size
        self.current_lighter_price = Decimal(price)
        self.waiting_for_lighter_fill = True

    def update_edgex_order_status(self, status: str):
        """Update EdgeX order status."""
        self.edgex_order_status = status

    async def _check_spread_disappeared(self, arb_direction: str, threshold: Decimal) -> bool:
        """Check if the arbitrage spread has disappeared.

        Args:
            arb_direction: 'long' or 'short'
            threshold: minimum spread required

        Returns:
            True if spread has disappeared (below threshold), False otherwise
        """
        try:
            edgex_bid, edgex_ask = self.order_book_manager.get_edgex_bbo()
            lighter_bid, lighter_ask = self.order_book_manager.get_lighter_bbo()

            if not all([edgex_bid, edgex_ask, lighter_bid, lighter_ask]):
                return False  # Can't determine, don't cancel

            if arb_direction == 'long':
                # Long: Lighter bid > EdgeX ask + threshold
                current_spread = lighter_bid - edgex_ask
            else:  # short
                # Short: EdgeX bid > Lighter ask + threshold
                current_spread = edgex_bid - lighter_ask

            if current_spread < threshold:
                self.logger.debug(
                    f"📉 [Spread Check] {arb_direction} spread={current_spread:.2f} < threshold={threshold}")
                return True
            return False
        except Exception as e:
            self.logger.debug(f"Error checking spread: {e}")
            return False  # On error, don't cancel

    async def place_lighter_market_order(self, lighter_side: str, quantity: Decimal,
                                         price: Decimal, stop_flag) -> Optional[str]:
        """Place a market order on Lighter."""
        if not self.lighter_client:
            error_msg = "Lighter client not initialized in OrderManager"
            self.logger.error(f"❌ {error_msg}")
            raise Exception(error_msg)
        
        if self.lighter_market_index is None:
            error_msg = "Lighter market_index not configured in OrderManager"
            self.logger.error(f"❌ {error_msg}")
            raise Exception(error_msg)

        best_bid, best_ask = self.order_book_manager.get_lighter_best_levels()
        if not best_bid or not best_ask:
            raise Exception("Lighter order book not ready")

        original_price = price
        if lighter_side.lower() == 'buy':
            order_type = "CLOSE"
            is_ask = False
            # Lighter 没有手续费，使用更激进的价格确保立即成交（taker）
            # 直接使用卖一价加上一定滑点，确保吃掉卖单
            price = best_ask[0] * Decimal('1.005')  # 增加到 0.5% 滑点确保成交
        else:
            order_type = "OPEN"
            is_ask = True
            # Lighter 没有手续费，使用更激进的价格确保立即成交（taker）
            # 直接使用买一价减去一定滑点，确保吃掉买单
            price = best_bid[0] * Decimal('0.995')  # 减少到 0.5% 滑点确保成交

        self.lighter_order_filled = False
        self.lighter_order_canceled = False  # Reset canceled flag for new order
        self.lighter_order_price = price
        self.lighter_order_side = lighter_side
        self.lighter_order_size = quantity

        try:
            client_order_index = int(time.time() * 1000)

            base_amount_raw = int(quantity * self.base_amount_multiplier)
            price_raw = int(price * self.price_multiplier)

            # Order details logged above

            tx_type, tx_info, tx_hash, error = self.lighter_client.sign_create_order(
                market_index=self.lighter_market_index,
                client_order_index=client_order_index,
                base_amount=base_amount_raw,
                price=price_raw,
                is_ask=is_ask,
                order_type=self.lighter_client.ORDER_TYPE_LIMIT,
                time_in_force=self.lighter_client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,  # 使用 IOC 确保立即成交
                reduce_only=False,
                trigger_price=0,
                order_expiry=self.lighter_client.DEFAULT_IOC_EXPIRY,  # IOC 订单必须使用 0 作为 expiry
            )
            if error is not None:
                raise Exception(f"Sign error: {error}")

            # Send transaction
            await self.lighter_client.send_tx(
                tx_type=tx_type,
                tx_info=tx_info
            )

            # Order sent, waiting for fill

            await self.monitor_lighter_order(client_order_index, stop_flag)

            return tx_hash
        except Exception as e:
            self.logger.error(f"❌ Error placing Lighter order: {e}")
            import traceback
            self.logger.error(f"   Traceback: {traceback.format_exc()}")
            return None
    
    async def place_lighter_ioc_order(self, lighter_side: str, quantity: Decimal,
                                     price: Decimal, stop_flag) -> Optional[str]:
        """Place an IOC order on Lighter (alias for place_lighter_market_order)."""
        return await self.place_lighter_market_order(lighter_side, quantity, price, stop_flag)
# 如果等不到，就回滚，使用市价成交，order_execution_complete实现了确保EdgeX和Lighter的订单都完成才进入下一轮交易

    async def place_lighter_post_only_order(self, lighter_side: str, quantity: Decimal,
                                            price: Decimal, stop_flag) -> Optional[str]:
        """Place a post-only limit order on Lighter (maker order)."""
        if not self.lighter_client:
            error_msg = "Lighter client not initialized in OrderManager"
            self.logger.error(f"❌ {error_msg}")
            raise Exception(error_msg)
        
        if self.lighter_market_index is None:
            error_msg = "Lighter market_index not configured in OrderManager"
            self.logger.error(f"❌ {error_msg}")
            raise Exception(error_msg)

        best_bid, best_ask = self.order_book_manager.get_lighter_best_levels()
        if not best_bid or not best_ask:
            raise Exception("Lighter order book not ready")

        # Determine order side
        if lighter_side.lower() == 'buy':
            is_ask = False
            # For buy orders, place slightly below best ask to ensure maker
            order_price = best_ask[0] - self.tick_size
        else:
            is_ask = True
            # For sell orders, place slightly above best bid to ensure maker
            order_price = best_bid[0] + self.tick_size

        # Round to tick size
        order_price = (order_price / self.tick_size).quantize(Decimal('1')) * self.tick_size

        # Placing post-only limit order

        try:
            client_order_index = int(time.time() * 1000)
            base_amount_raw = int(quantity * self.base_amount_multiplier)
            price_raw = int(order_price * self.price_multiplier)

            # Use create_order method which handles expiry automatically
            # This is the same approach used in exchanges/lighter.py
            # create_order returns: (CreateOrder, RespSendTx, error)
            # But in exchanges/lighter.py it's unpacked as: create_order, tx_hash, error
            create_order, tx_hash, error = await self.lighter_client.create_order(
                market_index=self.lighter_market_index,
                client_order_index=client_order_index,
                base_amount=base_amount_raw,
                price=price_raw,
                is_ask=is_ask,
                order_type=self.lighter_client.ORDER_TYPE_LIMIT,
                time_in_force=self.lighter_client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,  # Post-only
                reduce_only=False,
                trigger_price=0,
                order_expiry=self.lighter_client.DEFAULT_28_DAY_ORDER_EXPIRY,  # SDK will handle -1 automatically
            )
            
            if error is not None:
                raise Exception(f"Order creation error: {error}")
            
            # Extract tx_hash from response (RespSendTx object)
            if tx_hash and hasattr(tx_hash, 'tx_hash'):
                actual_tx_hash = tx_hash.tx_hash
            elif tx_hash:
                actual_tx_hash = str(tx_hash)
            else:
                actual_tx_hash = None
            
            # Record statistics (Lighter market orders are taker)
            if actual_tx_hash and self.trade_statistics:
                self.trade_statistics.record_order(
                    exchange='Lighter',
                    side=lighter_side,
                    quantity=quantity,
                    price=order_price,
                    order_id=actual_tx_hash,
                    order_type='taker'  # Lighter taker fee: 0%
                )
            
            # Order sent

            return actual_tx_hash
        except Exception as e:
            self.logger.error(f"❌ Error placing Lighter post-only order: {e}")
            import traceback
            self.logger.error(f"   Traceback: {traceback.format_exc()}")
            return None

    async def monitor_nado_order_fill(self, order_id: str, stop_flag, timeout: int = 30) -> bool:
        """Monitor Nado order fill status (similar to monitor_grvt_order).
        
        Args:
            order_id: Nado order digest/ID
            stop_flag: Stop signal flag
            timeout: Timeout in seconds
        
        Returns:
            bool: True if order filled, False otherwise
        """
        # Store the order ID we're monitoring so the callback can match it
        self.nado_client_order_id = order_id
        self.nado_order_status = None  # Reset status
        
        start_time = time.time()
        last_warning_time = 0
        warning_interval = 5  # Warn every 5 seconds
        
        while not stop_flag:
            elapsed = time.time() - start_time
            
            # Timeout check
            if elapsed > timeout:
                self.logger.warning(
                    f"⏱️ [Nado Order Timeout] Order {order_id} not filled after {elapsed:.1f}s")
                # Clear the monitored order ID
                if self.nado_client_order_id == order_id:
                    self.nado_client_order_id = None
                return False
            
            # Check order status (updated by WebSocket callback)
            # Only consider status if it matches the order we're monitoring
            if self.nado_client_order_id == order_id:
                if self.nado_order_status == 'FILLED' or self.nado_order_status == 'filled':
                    self.logger.info(f"✅ [Nado Order FILLED] Order {order_id} filled")
                    self.nado_client_order_id = None
                    return True
                elif self.nado_order_status == 'CANCELED' or self.nado_order_status == 'canceled':
                    self.logger.warning(f"⚠️ [Nado Order] Order {order_id} was canceled")
                    self.nado_client_order_id = None
                    return False
            
            # Periodic REST API polling as fallback (similar to GRVT)
            # Poll every 2 seconds if WebSocket hasn't updated
            if elapsed > 2 and (time.time() - last_warning_time >= 2.0):
                last_warning_time = time.time()
                try:
                    if self.nado_client and self.nado_product_id:
                        order_info = await self.nado_client.get_order_info(order_id, self.nado_product_id)
                        
                        # If order not found in open orders, it might be filled or canceled
                        if order_info is None:
                            # Order not in open orders list - likely filled or canceled
                            # For IOC orders, if not in open orders after a few seconds, assume filled
                            if elapsed > 3:
                                # Order filled (not in open orders)
                                self.nado_order_status = 'FILLED'
                                self.nado_client_order_id = None
                                return True
                            else:
                                self.logger.debug(
                                    f"⏳ [Nado Order] Order {order_id} not found in open orders yet "
                                    f"({elapsed:.1f}s elapsed)")
                        elif order_info:
                            # Parse status from order info
                            status_raw = order_info.get('status', '')
                            
                            # Handle different status formats
                            if isinstance(status_raw, dict):
                                status = str(status_raw.get('type', '')).upper()
                            else:
                                status = str(status_raw).upper()
                            
                            # Also check unfilled_amount to determine if order is filled
                            unfilled_amount = order_info.get('unfilled_amount') or order_info.get('resting', {}).get('unfilled_amount', '0')
                            filled = order_info.get('filled', '0')
                            
                            # Log detailed order info for debugging
                            self.logger.debug(
                                f"📋 [Nado Order Info] digest={order_id}, status={status}, "
                                f"unfilled={unfilled_amount}, filled={filled}, "
                                f"full_data={str(order_info)[:200]}")
                            
                            # Check if order is filled (status='filled' or unfilled_amount=0)
                            if (status == 'FILLED' or status == 'FILLED' or 
                                (unfilled_amount and str(unfilled_amount) == '0')):
                                # Order filled via REST
                                self.nado_order_status = 'FILLED'
                                self.nado_client_order_id = None
                                return True
                            elif status == 'CANCELED' or status == 'CANCELED':
                                self.logger.warning(f"⚠️ [Nado Order CANCELED via REST] Order {order_id} was canceled")
                                self.nado_order_status = 'CANCELED'
                                self.nado_client_order_id = None
                                return False
                            else:
                                self.logger.debug(
                                    f"⏳ [Nado Order Status] Order {order_id} status={status}, "
                                    f"unfilled={unfilled_amount} (via REST API, {elapsed:.1f}s elapsed)")
                except Exception as e:
                    self.logger.debug(f"Error checking Nado order status via REST: {e}")
                    import traceback
                    self.logger.debug(f"Traceback: {traceback.format_exc()}")
            
            # Periodic warning if waiting too long
            if elapsed > 5 and time.time() - last_warning_time >= warning_interval:
                last_warning_time = time.time()
                self.logger.debug(
                    f"⏳ Still waiting for Nado order fill... ({elapsed:.1f}s elapsed, "
                    f"order_id={order_id}, status={self.nado_order_status}, "
                    f"monitoring={self.nado_client_order_id})")
            
            await asyncio.sleep(0.1)
        
        # Clear the monitored order ID on exit
        if self.nado_client_order_id == order_id:
            self.nado_client_order_id = None
        return False

    async def monitor_grvt_order(self, order_id: str, grvt_client, stop_flag, timeout: int = 30):
        """Monitor GRVT order and wait for fill."""
        start_time = time.time()
        last_warning_time = 0
        warning_interval = 5  # Warn every 5 seconds
        
        self.grvt_order_filled = False
        
        while not self.grvt_order_filled and not stop_flag:
            elapsed = time.time() - start_time
            
            # Timeout after specified seconds
            if elapsed > timeout:
                self.logger.warning(
                    f"⏱️ [Timeout] GRVT order {order_id} not filled after {elapsed:.1f}s")
                # Check order status one more time
                try:
                    order_info = await grvt_client.get_order_info(order_id=order_id)
                    if order_info and order_info.status == 'FILLED':
                        self.logger.info(f"✅ Found filled order via status check!")
                        self.grvt_order_filled = True
                        return True
                    else:
                        self.logger.warning(
                            f"⚠️ Order status: {order_info.status if order_info else 'UNKNOWN'}")
                        return False
                except Exception as e:
                    self.logger.error(f"❌ Error checking GRVT order status: {e}")
                    return False
            
            # Periodic warning if waiting too long
            elif elapsed > 10 and time.time() - last_warning_time >= warning_interval:
                last_warning_time = time.time()
                try:
                    order_info = await grvt_client.get_order_info(order_id=order_id)
                    if order_info:
                        if order_info.status == 'FILLED':
                            self.logger.info(f"✅ GRVT order {order_id} filled!")
                            self.grvt_order_filled = True
                            return True
                        self.logger.debug(
                            f"⏳ Still waiting for GRVT order fill... ({elapsed:.1f}s elapsed, "
                            f"status={order_info.status})")
                except Exception as e:
                    self.logger.debug(f"Error checking order status: {e}")

            await asyncio.sleep(0.1)
        
        return self.grvt_order_filled

    async def query_lighter_order_status(self, client_order_index: int) -> Optional[dict]:
        """Query Lighter order status from API.

        NOTE: lighter-sdk 1.0.2 does NOT have get_orders() method.
        Order status can only be tracked via WebSocket updates.
        This method is a placeholder for future SDK versions.
        """
        try:
            if not self.lighter_client:
                return None

            # lighter-sdk 1.0.2 limitation: No query API available
            # Only WebSocket updates can provide order status
            self.logger.warning(
                f"⚠️ Lighter order query not available (lighter-sdk 1.0.2 limitation). "
                f"Relying on WebSocket updates only for client_order_id={client_order_index}")
            return None

        except Exception as e:
            self.logger.error(f"❌ Error in Lighter order query: {e}")
            return None

    async def monitor_lighter_order(self, client_order_index: int, stop_flag):
        """Monitor Lighter order and wait for fill."""
        start_time = time.time()
        last_warning_time = 0
        warning_interval = 5  # Warn every 5 seconds
        
        # Reset canceled flag
        self.lighter_order_canceled = False
        
        while not self.lighter_order_filled and not stop_flag:
            elapsed = time.time() - start_time
            
            # Check if order was canceled (e.g., margin-not-allowed)
            if self.lighter_order_canceled:
                self.logger.error(
                    f"❌ [Lighter Order CANCELED] Order {client_order_index} was canceled. "
                    f"Stopping monitoring and returning failure.")
                self.lighter_order_filled = False
                self.waiting_for_lighter_fill = False
                self.order_execution_complete = False
                return False
            
            # Timeout after 30 seconds
            if elapsed > 30:
                # Check again if order was canceled (might have been canceled during timeout)
                if self.lighter_order_canceled:
                    self.logger.error(
                        f"❌ [Lighter Order CANCELED] Order {client_order_index} was canceled during timeout. "
                        f"Returning failure.")
                    self.lighter_order_filled = False
                    self.waiting_for_lighter_fill = False
                    self.order_execution_complete = False
                    return False
                self.logger.warning(
                    f"⏱️ [Timeout] WebSocket did not receive fill notification after {elapsed:.1f}s "
                    f"for client_order_id={client_order_index}")
                self.logger.info(
                    f"💡 [Note] This may happen if WebSocket callback is delayed. "
                    f"If the order actually filled, we'll continue trading.")

                # Try to query order status before giving up
                self.logger.info(f"🔍 Querying Lighter order status for client_order_id={client_order_index}")

                try:
                    # Query order status from Lighter API
                    order_status = await self.query_lighter_order_status(client_order_index)

                    if order_status and order_status.get('status') == 'FILLED':
                        self.logger.info(f"✅ Found filled order via API query!")
                        # Process the order fill
                        self.handle_lighter_order_filled(order_status)
                    else:
                        # Order status query not available (lighter-sdk limitation)
                        # DO NOT assume order filled if we can't verify - this causes position imbalance
                        self.logger.error(
                            f"❌ [Lighter Order Timeout] Cannot verify order status (lighter-sdk limitation). "
                            f"Status: {order_status.get('status') if order_status else 'UNKNOWN'}")
                        self.logger.error(
                            f"❌ [Lighter Order Timeout] NOT assuming order filled to prevent position imbalance. "
                            f"Order may have failed or been canceled.")
                        self.lighter_order_filled = False
                        self.waiting_for_lighter_fill = False
                        self.order_execution_complete = False
                        return False

                except Exception as e:
                    self.logger.error(f"❌ Error querying order status: {e}")
                    self.logger.error(
                        f"❌ [Lighter Order Timeout] NOT assuming order filled to prevent position imbalance. "
                        f"Order may have failed or been canceled.")
                    self.lighter_order_filled = False
                    self.waiting_for_lighter_fill = False
                    self.order_execution_complete = False
                    return False

                break
            
            # Periodic warning if waiting too long (but not yet timeout)
            elif elapsed > 10 and time.time() - last_warning_time >= warning_interval:
                last_warning_time = time.time()
                self.logger.debug(
                    f"⏳ Still waiting for Lighter order fill... ({elapsed:.1f}s elapsed, "
                    f"client_order_id={client_order_index})")

            await asyncio.sleep(0.1)

    def handle_lighter_order_filled(self, order_data: dict):
        """Handle Lighter order fill notification."""
        try:
            # Calculate average filled price
            if "avg_filled_price" not in order_data:
                filled_quote = Decimal(str(order_data.get("filled_quote_amount", 0)))
                filled_base = Decimal(str(order_data.get("filled_base_amount", 0)))
                if filled_base > 0:
                    order_data["avg_filled_price"] = filled_quote / filled_base
                else:
                    self.logger.error("❌ Filled base amount is 0, cannot calculate avg price")
                    return

            # Determine side
            if order_data.get("is_ask") or order_data.get("side") == "SELL":
                order_data["side"] = "SHORT"
                order_type = "OPEN"
            else:
                order_data["side"] = "LONG"
                order_type = "CLOSE"

            client_order_index = order_data.get("client_order_id", "UNKNOWN")
            filled_amount = order_data.get("filled_base_amount", 0)
            avg_price = order_data.get("avg_filled_price", 0)

            # Order filled

            # Call the callback
            if self.on_order_filled:
                self.on_order_filled(order_data)

            # Mark as filled
            self.lighter_order_filled = True
            self.order_execution_complete = True

        except Exception as e:
            self.logger.error(f"Error handling Lighter order result: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")

    def get_edgex_client_order_id(self) -> str:
        """Get current EdgeX client order ID."""
        return self.edgex_client_order_id
    
    # ========== GRVT-specific methods ==========
    
    def set_grvt_config(self, client, contract_id: str, tick_size: Decimal):
        """Set GRVT client and configuration."""
        self.grvt_client = client
        self.grvt_contract_id = contract_id
        self.grvt_tick_size = tick_size
    
    async def execute_grvt_long_arbitrage(self, grvt_client, lighter_client, 
                                          contract_id: str, quantity: Decimal,
                                          grvt_ask: Decimal, fill_timeout: int, stop_flag):
        """
        Execute long arbitrage: Buy on GRVT (market taker), then Sell on Lighter (market taker).
        
        Args:
            grvt_client: GRVT client instance
            lighter_client: Lighter client instance  
            contract_id: GRVT contract ID
            quantity: Order quantity
            grvt_ask: GRVT ask price
            fill_timeout: Timeout for order fill
            stop_flag: Stop signal flag
        
        Returns:
            bool: True if arbitrage executed successfully
        """
        try:
            self.logger.info(f"🔄 [GRVT Long Arb] Starting...")
            self.logger.info(f"   GRVT BUY (market taker) -> Lighter SELL (post-only limit maker)")
            
            # Reset state
            self.grvt_order_filled = False
            self.lighter_order_filled = False
            self.order_execution_complete = False
            
            # Step 1: Place GRVT buy order (market taker)
            self.logger.info(f"📤 [GRVT] Placing BUY market order: {quantity}")
            
            grvt_order_info = await grvt_client.place_market_order(
                contract_id=contract_id,
                quantity=quantity,
                side='buy'
            )
            
            if not grvt_order_info:
                self.logger.error(f"❌ [GRVT] Market order failed - no order info returned")
                return False
            
            grvt_order_id = grvt_order_info.order_id
            
            # Record statistics for GRVT order (GRVT market orders are taker)
            if self.trade_statistics:
                # Get order price from order info if available
                order_price = Decimal(str(grvt_order_info.price)) if hasattr(grvt_order_info, 'price') and grvt_order_info.price else grvt_ask
                self.trade_statistics.record_order(
                    exchange='GRVT',
                    side='buy',
                    quantity=quantity,
                    price=order_price,
                    order_id=grvt_order_id,
                    order_type='taker'  # GRVT taker fee: -0.001% (rebate)
                )
            
            # Wait for order to be filled (market orders should fill quickly)
            if grvt_order_info.status != 'FILLED':
                filled = await self.monitor_grvt_order(grvt_order_id, grvt_client, stop_flag, timeout=fill_timeout)
                if not filled:
                    return False
            
            self.grvt_order_filled = True
            
            # Step 2: Place Lighter sell order (market taker - IOC) to hedge
            # Get current Lighter bid price for reference
            best_bid, best_ask = self.order_book_manager.get_lighter_best_levels()
            if not best_bid or not best_ask:
                self.logger.error("❌ [Lighter] Order book not ready")
                self.logger.warning("⚠️ Unhedged position: GRVT BUY filled but Lighter SELL failed")
                return False
            
            # Use market price (best bid) for IOC order
            lighter_sell_price = best_bid[0]
            
            self.logger.info(f"📤 [Lighter] Placing SELL market order: {quantity} @ {lighter_sell_price}")
            
            tx_hash = await self.place_lighter_market_order(
                lighter_side='sell',
                quantity=quantity,
                price=lighter_sell_price,
                stop_flag=stop_flag
            )
            
            if tx_hash:
                return True
            else:
                self.logger.error(f"❌ [Lighter] Market order failed")
                self.logger.warning("⚠️ Unhedged position: GRVT BUY filled but Lighter SELL failed")
                return False
                
        except Exception as e:
            self.logger.error(f"❌ Error in GRVT long arbitrage: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    async def execute_grvt_short_arbitrage(self, grvt_client, lighter_client,
                                            contract_id: str, quantity: Decimal,
                                            grvt_bid: Decimal, fill_timeout: int, stop_flag):
        """
        Execute short arbitrage: Sell on GRVT (market taker), then Buy on Lighter (market taker).
        
        Args:
            grvt_client: GRVT client instance
            lighter_client: Lighter client instance
            contract_id: GRVT contract ID
            quantity: Order quantity
            grvt_bid: GRVT bid price
            fill_timeout: Timeout for order fill
            stop_flag: Stop signal flag
        
        Returns:
            bool: True if arbitrage executed successfully
        """
        try:
            self.logger.info(f"🔄 [GRVT Short Arb] Starting...")
            self.logger.info(f"   GRVT SELL (market taker) -> Lighter BUY (post-only limit maker)")
            
            # Reset state
            self.grvt_order_filled = False
            self.lighter_order_filled = False
            self.order_execution_complete = False
            
            # Step 1: Place GRVT sell order (market taker)
            self.logger.info(f"📤 [GRVT] Placing SELL market order: {quantity}")
            
            grvt_order_info = await grvt_client.place_market_order(
                contract_id=contract_id,
                quantity=quantity,
                side='sell'
            )
            
            if not grvt_order_info:
                self.logger.error(f"❌ [GRVT] Market order failed - no order info returned")
                return False
            
            grvt_order_id = grvt_order_info.order_id
            
            # Record statistics for GRVT order (GRVT market orders are taker)
            if self.trade_statistics:
                # Get order price from order info if available
                order_price = Decimal(str(grvt_order_info.price)) if hasattr(grvt_order_info, 'price') and grvt_order_info.price else grvt_bid
                self.trade_statistics.record_order(
                    exchange='GRVT',
                    side='sell',
                    quantity=quantity,
                    price=order_price,
                    order_id=grvt_order_id,
                    order_type='taker'  # GRVT taker fee: -0.001% (rebate)
                )
            
            # Wait for order to be filled (market orders should fill quickly)
            if grvt_order_info.status != 'FILLED':
                filled = await self.monitor_grvt_order(grvt_order_id, grvt_client, stop_flag, timeout=fill_timeout)
                if not filled:
                    return False
            
            self.grvt_order_filled = True
            
            # Step 2: Place Lighter buy order (market taker - IOC) to hedge
            # Get current Lighter ask price for reference
            best_bid, best_ask = self.order_book_manager.get_lighter_best_levels()
            if not best_bid or not best_ask:
                self.logger.error("❌ [Lighter] Order book not ready")
                self.logger.warning("⚠️ Unhedged position: GRVT SELL filled but Lighter BUY failed")
                return False
            
            # Use market price (best ask) for IOC order
            lighter_buy_price = best_ask[0]
            
            self.logger.info(f"📤 [Lighter] Placing BUY market order: {quantity} @ {lighter_buy_price}")
            
            tx_hash = await self.place_lighter_market_order(
                lighter_side='buy',
                quantity=quantity,
                price=lighter_buy_price,
                stop_flag=stop_flag
            )
            
            if tx_hash:
                return True
            else:
                self.logger.error(f"❌ [Lighter] Market order failed")
                self.logger.warning("⚠️ Unhedged position: GRVT SELL filled but Lighter BUY failed")
                return False
                
        except Exception as e:
            self.logger.error(f"❌ Error in GRVT short arbitrage: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    # ========== Nado-specific methods ==========
    
    async def place_nado_market_order(self, side: str, quantity: Decimal, 
                                      price: Decimal) -> Optional[str]:
        """Place a market order (IOC) on Nado.
        
        Args:
            side: 'buy' or 'sell'
            quantity: Order quantity
            price: Market price (for IOC order, will be adjusted to ensure crossing the book)
        
        Returns:
            str: Order digest/ID if successful, None otherwise
        """
        if not self.nado_client:
            raise Exception("Nado client not initialized")
        
        try:
            # For IOC orders, we need to ensure the price crosses the book
            # Always fetch fresh order book data from REST API to ensure accuracy
            try:
                best_bid, best_ask = await self.fetch_nado_bbo_prices()
            except Exception as e:
                self.logger.warning(f"⚠️ [Nado Market Order] Failed to fetch BBO: {e}, using provided price")
                best_bid, best_ask = None, None
            
            if not best_bid or not best_ask or best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
                self.logger.warning("⚠️ [Nado Market Order] Invalid order book data, using aggressive price adjustment")
                # Use aggressive price: for BUY use price * 1.01, for SELL use price * 0.99
                if side == 'buy':
                    ioc_price = price * Decimal('1.01')  # 1% above provided price
                else:  # sell
                    ioc_price = price * Decimal('0.99')  # 1% below provided price
                # Using aggressive price adjustment
            else:
                # For BUY IOC: use best_ask + more ticks to ensure crossing
                # For SELL IOC: use best_bid - more ticks to ensure crossing
                if side == 'buy':
                    # Add 10 ticks to best_ask to ensure crossing (more aggressive)
                    ioc_price = best_ask + (self.nado_tick_size * Decimal('10'))
                    # Also ensure it's at least 0.1% above best_ask
                    min_price = best_ask * Decimal('1.001')
                    if ioc_price < min_price:
                        ioc_price = min_price
                    # Price adjusted for IOC BUY
                else:  # sell
                    # Subtract 10 ticks from best_bid to ensure crossing (more aggressive)
                    ioc_price = best_bid - (self.nado_tick_size * Decimal('10'))
                    # Also ensure it's at least 0.1% below best_bid
                    max_price = best_bid * Decimal('0.999')
                    if ioc_price > max_price:
                        ioc_price = max_price
                    # Price adjusted for IOC SELL
            
            result = await self.nado_client.place_order(
                product_id=self.nado_product_id,
                side=side,
                price=ioc_price,
                amount=quantity,
                order_type='ioc'  # IOC = Immediate or Cancel (market order)
            )
            
            if not result:
                self.logger.error("❌ [Nado Market Order] No response from Nado client")
                return None
            
            if 'error' in result:
                error_msg = result.get('error', 'Unknown error')
                error_code = result.get('code', 'N/A')
                self.logger.error(f"❌ [Nado Market Order] Error: {error_msg} (code: {error_code})")
                return None
            
            if 'data' not in result:
                self.logger.error(f"❌ [Nado Market Order] No 'data' in response. Full result: {result}")
                return None
            
            # Extract order digest from nested structure
            # Response format: {'success': True, 'data': {'data': {'digest': '0x...'}, ...}}
            order_id = None
            
            # Primary path: result.data.data.digest (nested structure)
            if isinstance(result, dict) and 'data' in result:
                data = result['data']
                if isinstance(data, dict):
                    # Check nested data.data.digest
                    if 'data' in data:
                        nested_data = data['data']
                        if isinstance(nested_data, dict) and 'digest' in nested_data:
                            order_id = nested_data['digest']
                    
                    # Fallback: check data.digest directly
                    if not order_id and 'digest' in data:
                        order_id = data['digest']
            
            # Fallback: check if digest is directly in result
            if not order_id and isinstance(result, dict):
                order_id = result.get('digest')
            
            if order_id:
                # Record statistics
                if self.trade_statistics:
                    self.trade_statistics.record_order(
                        exchange='Nado',
                        side=side,
                        quantity=quantity,
                        price=ioc_price,
                        order_id=order_id
                    )
                return order_id
            else:
                self.logger.error(
                    f"❌ [Nado Market Order] No order digest in response. "
                    f"Response keys: {list(result.keys()) if isinstance(result, dict) else 'N/A'}, "
                    f"result['data']: {result.get('data') if isinstance(result, dict) and 'data' in result else 'N/A'}")
                return None
                
        except Exception as e:
            self.logger.error(f"❌ [Nado Market Order] Exception: {e}")
            import traceback
            self.logger.error(f"   Traceback: {traceback.format_exc()}")
            return None
    
    async def execute_nado_long_arbitrage(self, nado_client, lighter_client,
                                          product_id: int, quantity: Decimal,
                                          nado_ask: Decimal, fill_timeout: int, stop_flag):
        """
        Execute long arbitrage: Buy on Nado (market taker), then Sell on Lighter (market taker).
        
        Args:
            nado_client: NadoClient instance
            lighter_client: Lighter client instance  
            product_id: Nado product ID
            quantity: Order quantity
            nado_ask: Nado ask price
            fill_timeout: Timeout for order fill
            stop_flag: Stop signal flag
        
        Returns:
            bool: True if arbitrage executed successfully
        """
        try:
            # Reset state
            self.nado_order_status = None
            
            # Step 1: Place Nado buy order (market taker - IOC)
            self.logger.info(f"📤 Nado BUY {quantity} @ {nado_ask}")
            
            nado_order_id = await self.place_nado_market_order(
                side='buy',
                quantity=quantity,
                price=nado_ask
            )
            
            if not nado_order_id:
                return False
            
            # Wait for order to be filled (IOC orders should fill quickly or cancel)
            filled = await self.monitor_nado_order_fill(nado_order_id, stop_flag, timeout=fill_timeout)
            
            # Double-check order status even if monitor returned False (in case of detection delay)
            if not filled:
                try:
                    if self.nado_client and self.nado_product_id:
                        order_info = await self.nado_client.get_order_info(nado_order_id, self.nado_product_id)
                        if order_info is None:
                            filled = True
                        elif order_info:
                            status_raw = order_info.get('status', '')
                            if isinstance(status_raw, dict):
                                status = str(status_raw.get('type', '')).upper()
                            else:
                                status = str(status_raw).upper()
                            unfilled_amount = order_info.get('unfilled_amount') or order_info.get('resting', {}).get('unfilled_amount', '0')
                            
                            if status == 'FILLED' or (unfilled_amount and str(unfilled_amount) == '0'):
                                filled = True
                except Exception:
                    pass
            
            if not filled:
                return False
            
            # Step 2: Place Lighter sell order (market taker - IOC) to hedge
            best_bid, best_ask = self.order_book_manager.get_lighter_best_levels()
            if not best_bid or not best_ask:
                return False
            
            # Use market price (best bid) for IOC order
            lighter_sell_price = best_bid[0]
            
            self.logger.info(f"📤 Lighter SELL {quantity} @ {lighter_sell_price}")
            
            tx_hash = await self.place_lighter_market_order(
                lighter_side='sell',
                quantity=quantity,
                price=lighter_sell_price,
                stop_flag=stop_flag
            )
            
            return tx_hash is not None
                
        except Exception as e:
            self.logger.error(f"❌ Error in Nado long arbitrage: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    async def execute_nado_short_arbitrage(self, nado_client, lighter_client,
                                           product_id: int, quantity: Decimal,
                                           nado_bid: Decimal, fill_timeout: int, stop_flag):
        """
        Execute short arbitrage: Sell on Nado (market taker), then Buy on Lighter (market taker).
        
        Args:
            nado_client: NadoClient instance
            lighter_client: Lighter client instance
            product_id: Nado product ID
            quantity: Order quantity
            nado_bid: Nado bid price
            fill_timeout: Timeout for order fill
            stop_flag: Stop signal flag
        
        Returns:
            bool: True if arbitrage executed successfully
        """
        try:
            # Reset state
            self.nado_order_status = None
            
            # Step 1: Place Nado sell order (market taker - IOC)
            self.logger.info(f"📤 Nado SELL {quantity} @ {nado_bid}")
            
            nado_order_id = await self.place_nado_market_order(
                side='sell',
                quantity=quantity,
                price=nado_bid
            )
            
            if not nado_order_id:
                return False
            
            # Wait for order to be filled (IOC orders should fill quickly or cancel)
            filled = await self.monitor_nado_order_fill(nado_order_id, stop_flag, timeout=fill_timeout)
            
            # Double-check order status even if monitor returned False (in case of detection delay)
            if not filled:
                try:
                    if self.nado_client and self.nado_product_id:
                        order_info = await self.nado_client.get_order_info(nado_order_id, self.nado_product_id)
                        if order_info is None:
                            filled = True
                        elif order_info:
                            status_raw = order_info.get('status', '')
                            if isinstance(status_raw, dict):
                                status = str(status_raw.get('type', '')).upper()
                            else:
                                status = str(status_raw).upper()
                            unfilled_amount = order_info.get('unfilled_amount') or order_info.get('resting', {}).get('unfilled_amount', '0')
                            
                            if status == 'FILLED' or (unfilled_amount and str(unfilled_amount) == '0'):
                                filled = True
                except Exception:
                    pass
            
            if not filled:
                return False
            
            # Step 2: Place Lighter buy order (market taker - IOC) to hedge
            best_bid, best_ask = self.order_book_manager.get_lighter_best_levels()
            if not best_bid or not best_ask:
                return False
            
            # Use market price (best ask) for IOC order
            lighter_buy_price = best_ask[0]
            
            self.logger.info(f"📤 Lighter BUY {quantity} @ {lighter_buy_price}")
            
            tx_hash = await self.place_lighter_market_order(
                lighter_side='buy',
                quantity=quantity,
                price=lighter_buy_price,
                stop_flag=stop_flag
            )
            
            return tx_hash is not None
                
        except Exception as e:
            self.logger.error(f"❌ Error in Nado short arbitrage: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    def set_nado_config(self, client, product_id: int, tick_size: Decimal):
        """Set Nado client and configuration.
        
        Args:
            client: NadoClient instance
            product_id: Nado product ID (e.g., 4 for BTC)
            tick_size: Price tick size
        """
        self.nado_client = client
        self.nado_product_id = product_id
        self.nado_tick_size = tick_size
        self.nado_order_status = None
        self.nado_client_order_id = None
    
    async def fetch_nado_bbo_prices(self) -> Tuple[Decimal, Decimal]:
        """Fetch best bid/ask prices from Nado.
        
        Returns:
            Tuple of (best_bid, best_ask) as Decimal prices
        """
        # Use WebSocket data if available
        if self.order_book_manager.nado_order_book_ready:
            nado_bid, nado_ask = self.order_book_manager.get_nado_bbo()
            if nado_bid and nado_ask and nado_bid > 0 and nado_ask > 0 and nado_bid < nado_ask:
                return nado_bid, nado_ask
        
        # Fallback to REST API
        if not self.nado_client:
            raise Exception("Nado client not initialized")
        
        try:
            order_book = await self.nado_client.get_order_book(self.nado_product_id)
            # Nado orderbook format: {'bids': [[price, size], ...], 'asks': [[price, size], ...]}
            bids = order_book.get('bids', [])
            asks = order_book.get('asks', [])
            
            # Parse [[price, size], ...] format
            best_bid = Decimal(bids[0][0]) / Decimal('1e18') if bids and len(bids) > 0 else Decimal('0')
            best_ask = Decimal(asks[0][0]) / Decimal('1e18') if asks and len(asks) > 0 else Decimal('0')
            
            return best_bid, best_ask
        except Exception as e:
            self.logger.error(f"Error fetching Nado BBO: {e}")
            raise
    
    def _calculate_nado_post_only_price(self, side: str, best_bid: Decimal, best_ask: Decimal) -> Decimal:
        """Calculate post-only order price that won't cross the book.
        
        Uses conservative approach: subtract/add multiple ticks to ensure no crossing.
        """
        if side.lower() == 'buy':
            # For buy orders: price must be strictly < best_ask
            # Use conservative approach: subtract 2 ticks to ensure safety margin
            order_price = best_ask - (self.nado_tick_size * Decimal('2'))
            # Round down to nearest tick
            rounded_price = (order_price / self.nado_tick_size).quantize(Decimal('1'), rounding=ROUND_DOWN) * self.nado_tick_size
            # Final check: ensure strictly less than best_ask
            while rounded_price >= best_ask:
                rounded_price = rounded_price - self.nado_tick_size
        else:
            # For sell orders: price must be strictly > best_bid
            # Use conservative approach: add 2 ticks to ensure safety margin
            order_price = best_bid + (self.nado_tick_size * Decimal('2'))
            # Round up to nearest tick
            rounded_price = (order_price / self.nado_tick_size).quantize(Decimal('1'), rounding=ROUND_UP) * self.nado_tick_size
            # Final check: ensure strictly greater than best_bid
            while rounded_price <= best_bid:
                rounded_price = rounded_price + self.nado_tick_size
        
        return rounded_price
    
    async def place_nado_post_only_order(self, side: str, quantity: Decimal,
                                         stop_flag, arb_direction: str = None,
                                         threshold: Decimal = None) -> bool:
        """Place a post-only order on Nado with retry logic.
        
        Args:
            side: 'buy' or 'sell'
            quantity: Order quantity
            stop_flag: Stop signal flag
            arb_direction: 'long' or 'short' (for spread monitoring)
            threshold: Spread threshold for cancellation
        
        Returns:
            bool: True if order filled successfully
        """
        if not self.nado_client:
            raise Exception("Nado client not initialized")
        
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            # Get fresh BBO right before placing order (order book may have changed)
            best_bid, best_ask = await self.fetch_nado_bbo_prices()
            if not best_bid or not best_ask:
                self.logger.error("❌ [Nado Order] Cannot get BBO prices")
                return False
            
            self.logger.info(
                f"💰 [Price Check] Nado BBO (attempt {retry_count + 1}/{max_retries}): "
                f"bid={best_bid}, ask={best_ask}")
            
            # Calculate conservative post-only price
            rounded_price = self._calculate_nado_post_only_price(side, best_bid, best_ask)
            nado_side = 'buy' if side.lower() == 'buy' else 'sell'
            
            # Final validation: ensure post-only requirements are met
            if side.lower() == 'buy' and rounded_price >= best_ask:
                self.logger.error(
                    f"❌ [Nado Order] Buy price {rounded_price} >= best_ask {best_ask}, "
                    f"would cross the book! Retrying with adjusted price...")
                retry_count += 1
                await asyncio.sleep(0.1)  # Brief delay before retry
                continue
            elif side.lower() == 'sell' and rounded_price <= best_bid:
                self.logger.error(
                    f"❌ [Nado Order] Sell price {rounded_price} <= best_bid {best_bid}, "
                    f"would cross the book! Retrying with adjusted price...")
                retry_count += 1
                await asyncio.sleep(0.1)  # Brief delay before retry
                continue
            
            self.logger.info(
                f"🔢 [Price Calculation] {side.upper()} order: "
                f"price={rounded_price} (tick_size={self.nado_tick_size}), "
                f"BBO: bid={best_bid}, ask={best_ask}")
            
            # Generate client order ID
            self.nado_client_order_id = str(int(time.time() * 1000))
            
            # Log price details for debugging
            self.logger.info(
                f"📤 [Sending Order] Nado {side.upper()} order: "
                f"quantity={quantity}, price={rounded_price} (USD), post_only=True, "
                f"client_order_id={self.nado_client_order_id}")
            
            try:
                # Place order via Nado client
                # Ensure price is in USD format (Decimal, not Wei)
                if rounded_price > Decimal('1000000'):
                    self.logger.error(
                        f"❌ [Price Check] Price seems too large (might be in Wei format): {rounded_price}")
                    return False
                
                result = await self.nado_client.place_order(
                    product_id=self.nado_product_id,
                    side=nado_side,
                    price=rounded_price,
                    amount=quantity,
                    order_type='post_only'
                )
                
                # Log full result for debugging
                self.logger.debug(f"📋 [Nado Order Response] Full result: {result}")
                
                # Check for errors in response
                if not result:
                    self.logger.error("❌ [Nado Order] No response from Nado client")
                    retry_count += 1
                    await asyncio.sleep(0.1)
                    continue
                
                if 'error' in result:
                    error_msg = result.get('error', 'Unknown error')
                    error_code = result.get('code', 'N/A')
                    
                    # Check if it's a "crosses the book" error
                    if '2008' in str(error_code) or 'crosses' in error_msg.lower() or 'cross' in error_msg.lower():
                        self.logger.warning(
                            f"⚠️ [Nado Order] Order crosses the book (attempt {retry_count + 1}/{max_retries}): "
                            f"{error_msg}. Retrying with fresh BBO...")
                        retry_count += 1
                        await asyncio.sleep(0.2)  # Wait a bit for order book to update
                        continue
                    else:
                        # Other error, don't retry
                        self.logger.error(f"❌ [Nado Order] Error from Nado API: {error_msg} (code: {error_code})")
                        if 'stdout' in result:
                            self.logger.error(f"   stdout: {result['stdout']}")
                        if 'stderr' in result:
                            self.logger.error(f"   stderr: {result['stderr']}")
                        return False
                
                # Success case: order placed
                # SDK returns: {'status': 'success', 'data': {'digest': '0x...'}, ...}
                # or: {'success': true, 'data': {'digest': '0x...'}}
                order_id = None
                
                # Check if result has 'data' key
                if 'data' in result:
                    data = result['data']  # Direct access, not .get() to avoid issues
                    self.logger.debug(f"🔍 [Nado Order] Extracted data: {data}, type: {type(data)}")
                    if isinstance(data, dict):
                        order_id = data.get('digest')
                        self.logger.debug(f"🔍 [Nado Order] Extracted digest from data: {order_id}")
                    elif isinstance(data, str):
                        # Sometimes data might be a string representation
                        try:
                            import json
                            data_dict = json.loads(data)
                            if isinstance(data_dict, dict):
                                order_id = data_dict.get('digest')
                        except:
                            pass
                
                # Also check if digest is directly in result (fallback)
                if not order_id:
                    order_id = result.get('digest')
                
                # Check if result.data.data exists (nested structure)
                if not order_id and 'data' in result:
                    nested_data = result.get('data', {})
                    if isinstance(nested_data, dict) and 'data' in nested_data:
                        nested_inner = nested_data.get('data', {})
                        if isinstance(nested_inner, dict):
                            order_id = nested_inner.get('digest')
                
                # Validate order_id - check for None, empty string, or falsy values
                # Note: '0x' prefixed strings are truthy, so we need to check explicitly
                if order_id is None or (isinstance(order_id, str) and not order_id.strip()):
                    # Log detailed error for debugging
                    self.logger.error(
                        f"❌ [Nado Order] No order digest in response. "
                        f"order_id={order_id}, order_id type: {type(order_id)}, "
                        f"Response type: {type(result)}, "
                        f"Response keys: {list(result.keys()) if isinstance(result, dict) else 'N/A'}")
                    if isinstance(result, dict) and 'data' in result:
                        data_val = result['data']
                        self.logger.error(
                            f"   result['data'] type: {type(data_val)}, "
                            f"result['data'] value: {data_val}, "
                            f"Is dict: {isinstance(data_val, dict)}, "
                            f"Has 'digest' key: {'digest' in data_val if isinstance(data_val, dict) else False}")
                        if isinstance(data_val, dict) and 'digest' in data_val:
                            # This should not happen, but log it for debugging
                            digest_val = data_val.get('digest')
                            self.logger.error(
                                f"   ⚠️ Found digest in data but order_id is None/empty! "
                                f"digest={digest_val}, digest type: {type(digest_val)}, "
                                f"order_id={order_id}, order_id is None: {order_id is None}")
                            # Try to use digest_val directly if order_id is None
                            if digest_val and order_id is None:
                                self.logger.warning(f"   🔧 Using digest_val directly: {digest_val}")
                                order_id = digest_val
                    if not order_id:
                        retry_count += 1
                        await asyncio.sleep(0.1)
                        continue
                
                self.logger.info(f"✅ [Order Placed] Nado order_id={order_id}, waiting for fill...")
                
                # Monitor order status
                return await self._monitor_nado_order(order_id, stop_flag, arb_direction, threshold)
                
            except Exception as e:
                self.logger.error(f"❌ [Nado Order] Exception placing order: {e}")
                import traceback
                self.logger.error(f"   Traceback: {traceback.format_exc()}")
                retry_count += 1
                if retry_count < max_retries:
                    await asyncio.sleep(0.2)
                    continue
                return False
        
        # All retries exhausted
        self.logger.error(f"❌ [Nado Order] Failed after {max_retries} attempts")
        return False
    
    async def _monitor_nado_order(self, order_id: str, stop_flag,
                                  arb_direction: str = None,
                                  threshold: Decimal = None) -> bool:
        """Monitor Nado order status until filled or cancelled.
        
        Args:
            order_id: Order digest/ID
            stop_flag: Stop signal flag
            arb_direction: 'long' or 'short' for spread monitoring
            threshold: Spread threshold for cancellation
        
        Returns:
            bool: True if order filled, False otherwise
        """
        start_time = time.time()
        spread_check_interval = 0.2
        last_spread_check = time.time()
        
        cancel_requested = False  # Track if we've already requested cancellation
        
        while not stop_flag:
            # Check if order is already filled or canceled (check first to avoid unnecessary work)
            if self.nado_order_status == 'FILLED':
                self.logger.info(f"✅ [Nado Order FILLED] Order {order_id} filled, stopping monitoring")
                return True
            elif self.nado_order_status == 'CANCELED':
                self.logger.info(f"⚠️ [Nado Order CANCELED] Order {order_id} was canceled")
                return False
            
            # Check if spread has disappeared (only if order is still open and not already canceled)
            if (arb_direction and threshold and 
                not cancel_requested and 
                time.time() - last_spread_check >= spread_check_interval):
                last_spread_check = time.time()
                spread_gone = await self._check_nado_spread_disappeared(arb_direction, threshold)
                if spread_gone:
                    self.logger.warning(
                        f"⚠️ [Spread Disappeared] Spread below threshold, canceling Nado order {order_id}")
                    try:
                        await self.nado_client.cancel_order(order_id, self.nado_product_id)
                        cancel_requested = True
                        self.logger.info(f"✅ [Cancel Requested] Cancel request sent for order {order_id}")
                    except Exception as e:
                        self.logger.error(f"❌ Error canceling Nado order: {e}")
                        # Don't set cancel_requested = True if cancel failed, so we can retry
            
            # Check order status (via WebSocket callback or API query)
            await asyncio.sleep(0.5)
            
            elapsed = time.time() - start_time
            if elapsed > 5:
                # Only cancel on timeout if order is still open (not filled or already canceled)
                if self.nado_order_status not in ['FILLED', 'CANCELED']:
                    self.logger.warning(
                        f"⚠️ [Nado Order Timeout] Order {order_id} not filled after {elapsed:.1f}s")
                    if not cancel_requested:
                        try:
                            await self.nado_client.cancel_order(order_id, self.nado_product_id)
                            cancel_requested = True
                        except Exception as e:
                            self.logger.error(f"❌ Error canceling Nado order: {e}")
                    return False
                else:
                    # Order was filled or canceled, return appropriate result
                    if self.nado_order_status == 'FILLED':
                        return True
                    return False
        
        return False
    
    async def _check_nado_spread_disappeared(self, arb_direction: str, threshold: Decimal) -> bool:
        """Check if spread has disappeared below threshold.
        
        Args:
            arb_direction: 'long' or 'short'
            threshold: Spread threshold
        
        Returns:
            bool: True if spread disappeared
        """
        try:
            nado_bid, nado_ask = await self.fetch_nado_bbo_prices()
            lighter_bid, lighter_ask = self.order_book_manager.get_lighter_bbo()
            
            if not (nado_bid and nado_ask and lighter_bid and lighter_ask):
                return False
            
            if arb_direction == 'long':
                spread = lighter_bid - nado_ask
            else:
                spread = nado_bid - lighter_ask
            
            return spread < threshold
            
        except Exception as e:
            self.logger.error(f"Error checking Nado spread: {e}")
            return False
    
    def handle_nado_order_update(self, order_data: dict):
        """Handle Nado order update from WebSocket.
        
        Args:
            order_data: Order update data from WebSocket
        """
        try:
            status = order_data.get('status', '')
            digest = order_data.get('digest', '')
            
            # Normalize status to uppercase for consistency
            status_upper = status.upper() if status else ''
            
            # Only update status if this is the order we're monitoring
            if self.nado_client_order_id and digest == self.nado_client_order_id:
                self.nado_order_status = status_upper
                
                if status_upper == 'FILLED':
                    self.logger.info(f"✅ [Nado Order FILLED] digest={digest}")
                elif status_upper == 'CANCELED':
                    self.logger.info(f"⚠️ [Nado Order CANCELED] digest={digest}")
                elif status_upper == 'OPEN':
                    self.logger.debug(f"📋 [Nado Order OPEN] digest={digest}")
            else:
                # Log other orders for debugging
                self.logger.debug(
                    f"📋 [Nado Order Update] digest={digest}, status={status} "
                    f"(not monitoring, current={self.nado_client_order_id})")
                
        except Exception as e:
            self.logger.error(f"Error handling Nado order update: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
    
    def get_nado_client_order_id(self) -> str:
        """Get current Nado client order ID."""
        return self.nado_client_order_id
