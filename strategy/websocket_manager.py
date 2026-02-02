"""WebSocket management for EdgeX and Lighter exchanges."""
import asyncio
import json
import logging
import os
import time
import traceback
import websockets
from decimal import Decimal
from typing import Callable, Optional

from edgex_sdk import WebSocketManager


class WebSocketManagerWrapper:
    """Manages WebSocket connections for both exchanges."""

    def __init__(self, order_book_manager, logger: logging.Logger):
        """Initialize WebSocket manager."""
        self.order_book_manager = order_book_manager
        self.logger = logger
        self.stop_flag = False

        # EdgeX WebSocket
        self.edgex_ws_manager: Optional[WebSocketManager] = None
        self.edgex_contract_id: Optional[str] = None

        # Lighter WebSocket
        self.lighter_ws_task: Optional[asyncio.Task] = None
        self.lighter_client = None
        self.lighter_market_index: Optional[int] = None
        self.account_index: Optional[int] = None

        # Nado WebSocket
        self.nado_ws_task: Optional[asyncio.Task] = None
        self.nado_product_id: Optional[int] = None
        self.nado_wallet_address: Optional[str] = None

        # Callbacks
        self.on_lighter_order_filled: Optional[Callable] = None
        self.on_lighter_order_canceled: Optional[Callable] = None  # Callback for canceled orders
        self.on_edgex_order_update: Optional[Callable] = None

    def set_edgex_ws_manager(self, ws_manager: WebSocketManager, contract_id: str):
        """Set EdgeX WebSocket manager and contract ID."""
        self.edgex_ws_manager = ws_manager
        self.edgex_contract_id = contract_id

    def set_lighter_config(self, client, market_index: int, account_index: int):
        """Set Lighter client and configuration."""
        self.lighter_client = client
        self.lighter_market_index = market_index
        self.account_index = account_index

    def set_callbacks(self, on_lighter_order_filled: Callable = None,
                      on_lighter_order_canceled: Callable = None,
                      on_edgex_order_update: Callable = None):
        """Set callback functions for order updates."""
        self.on_lighter_order_filled = on_lighter_order_filled
        self.on_lighter_order_canceled = on_lighter_order_canceled
        self.on_edgex_order_update = on_edgex_order_update

    # EdgeX WebSocket methods
    def handle_edgex_order_book_update(self, message):
        """Handle EdgeX order book updates from WebSocket."""
        try:
            if isinstance(message, str):
                message = json.loads(message)

            self.logger.debug(f"Received EdgeX depth message: {message}")

            # Check if this is a quote-event message with depth data
            if message.get("type") == "quote-event":
                content = message.get("content", {})
                channel = message.get("channel", "")

                if channel.startswith("depth."):
                    data = content.get('data', [])
                    if data and len(data) > 0:
                        order_book_data = data[0]
                        depth_type = order_book_data.get('depthType', '')

                        # Handle SNAPSHOT (full data) or CHANGED (incremental updates)
                        if depth_type in ['SNAPSHOT', 'CHANGED']:
                            bids = order_book_data.get('bids', [])
                            asks = order_book_data.get('asks', [])
                            self.order_book_manager.update_edgex_order_book(bids, asks)

        except Exception as e:
            self.logger.error(f"Error handling EdgeX order book update: {e}")
            self.logger.error(f"Message content: {message}")

    async def setup_edgex_websocket(self):
        """Setup EdgeX websocket for order updates and order book data."""
        if not self.edgex_ws_manager:
            raise Exception("EdgeX WebSocket manager not initialized")

        def order_update_handler(message):
            """Handle order updates from EdgeX WebSocket."""
            if isinstance(message, str):
                message = json.loads(message)

            content = message.get("content", {})
            event = content.get("event", "")
            try:
                if event == "ORDER_UPDATE":
                    data = content.get('data', {})
                    orders = data.get('order', [])

                    if orders and len(orders) > 0:
                        # EdgeX returns TWO filled events for the same order; skip the second one
                        # The second one has collateral data, so we filter it out
                        order_status = orders[0].get('status', '')
                        if order_status == "FILLED" and len(data.get('collateral', [])):
                            return  # Skip duplicate FILLED event

                        for order in orders:
                            if order.get('contractId') != self.edgex_contract_id:
                                continue

                            if self.on_edgex_order_update:
                                self.on_edgex_order_update(order)

            except Exception as e:
                self.logger.error(f"Error handling EdgeX order update: {e}")

        try:
            # Setup order update handler
            private_client = self.edgex_ws_manager.get_private_client()
            private_client.on_message("trade-event", order_update_handler)
            self.logger.info("✅ EdgeX WebSocket order update handler set up")

            # Connect to EdgeX WebSocket
            self.edgex_ws_manager.connect_public()
            self.edgex_ws_manager.connect_private()
            # self.logger.info("✅ EdgeX WebSocket connection established")

            # Setup public client for market data
            public_client = self.edgex_ws_manager.get_public_client()

            # Register handler for depth messages
            public_client.on_message("depth", self.handle_edgex_order_book_update)
            # self.logger.info("✅ EdgeX WebSocket depth handler registered")

            # Subscribe to depth channel after connection is established
            public_client.subscribe(f"depth.{self.edgex_contract_id}.15")
            self.logger.info(f"✅ Subscribed to depth channel: depth.{self.edgex_contract_id}.15")

        except Exception as e:
            self.logger.error(f"Could not setup EdgeX WebSocket handlers: {e}")

    # Lighter WebSocket methods
    async def request_fresh_snapshot(self, ws):
        """Request fresh order book snapshot."""
        await ws.send(json.dumps({
            "type": "subscribe",
            "channel": f"order_book/{self.lighter_market_index}"
        }))

    async def handle_lighter_ws(self):
        """Handle Lighter WebSocket connection and messages."""
        # Use readonly=true parameter for restricted regions
        url = "wss://mainnet.zklighter.elliot.ai/stream?readonly=true"
        cleanup_counter = 0
        
        while not self.stop_flag:
            timeout_count = 0
            try:
                # Reset order book state before connecting
                await self.order_book_manager.reset_lighter_order_book()

                # Connecting to Lighter WebSocket

                async with websockets.connect(url) as ws:
                    # Subscribe to order book updates
                    subscribe_msg = {
                        "type": "subscribe",
                        "channel": f"order_book/{self.lighter_market_index}"
                    }
                    # Subscribing to order book
                    await ws.send(json.dumps(subscribe_msg))

                    # Subscribe to account orders updates
                    account_orders_channel = f"account_orders/{self.lighter_market_index}/{self.account_index}"

                    # Track subscription state to prevent duplicate subscriptions
                    is_subscribed = False

                    # Function to subscribe to account orders with auth token
                    async def subscribe_account_orders():
                        nonlocal is_subscribed

                        # Skip if already subscribed
                        if is_subscribed:
                            self.logger.debug("⏭️ Already subscribed, skipping duplicate subscription")
                            return True

                        try:
                            # Use default expiry time (SDK handles it automatically)
                            # Creating auth token
                            auth_token, err = self.lighter_client.create_auth_token_with_expiry()
                            if err is not None:
                                self.logger.warning(f"⚠️ Failed to create auth token: {err}")
                                return False
                            else:
                                auth_message = {
                                    "type": "subscribe",
                                    "channel": account_orders_channel,
                                    "auth": auth_token
                                }
                                # Auth token created
                                await ws.send(json.dumps(auth_message))
                                is_subscribed = True  # Mark as subscribed
                                return True
                        except Exception as e:
                            self.logger.warning(f"⚠️ Error creating auth token: {e}")
                            return False

                    # Initial subscription (one-time, remains active until WebSocket disconnects)
                    await subscribe_account_orders()

                    while not self.stop_flag:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1)

                            try:
                                data = json.loads(msg)
                                # Log all message types for debugging (except frequent order_book updates)
                                msg_type = data.get("type", "UNKNOWN")
                                # If message type is UNKNOWN, log the full message to debug
                                if msg_type == "UNKNOWN":
                                    self.logger.warning(f"⚠️ [Lighter WS] UNKNOWN message content: {data}")
                            except json.JSONDecodeError as e:
                                self.logger.warning(f"⚠️ JSON parsing error: {e}")
                                continue

                            timeout_count = 0

                            async with self.order_book_manager.lighter_order_book_lock:
                                if data.get("type") == "subscribed/order_book":
                                    # Initial snapshot
                                    self.order_book_manager.lighter_order_book["bids"].clear()
                                    self.order_book_manager.lighter_order_book["asks"].clear()

                                    order_book = data.get("order_book", {})
                                    if order_book and "offset" in order_book:
                                        self.order_book_manager.lighter_order_book_offset = order_book["offset"]
                                        # Order book offset initialized

                                    bids = order_book.get("bids", [])
                                    asks = order_book.get("asks", [])

                                    self.order_book_manager.update_lighter_order_book("bids", bids)
                                    self.order_book_manager.update_lighter_order_book("asks", asks)
                                    self.order_book_manager.lighter_snapshot_loaded = True
                                    self.order_book_manager.lighter_order_book_ready = True
                                    self.order_book_manager.update_lighter_bbo()

                                    # Order book snapshot loaded

                                elif data.get("type") == "subscribed/account_orders":
                                    # Account orders subscription confirmed
                                    channel = data.get("channel", "UNKNOWN")
                                    # Account orders subscription confirmed

                                elif (data.get("type") == "update/order_book" and
                                      self.order_book_manager.lighter_snapshot_loaded):
                                    order_book = data.get("order_book", {})
                                    if not order_book or "offset" not in order_book:
                                        self.logger.warning("⚠️ Order book update missing offset, skipping")
                                        continue

                                    new_offset = order_book["offset"]

                                    if not self.order_book_manager.validate_order_book_offset(new_offset):
                                        self.order_book_manager.lighter_order_book_sequence_gap = True
                                        break

                                    self.order_book_manager.update_lighter_order_book(
                                        "bids", order_book.get("bids", []))
                                    self.order_book_manager.update_lighter_order_book(
                                        "asks", order_book.get("asks", []))

                                    if not self.order_book_manager.validate_order_book_integrity():
                                        self.logger.warning(
                                            "🔄 Order book integrity check failed, requesting fresh snapshot...")
                                        break

                                    self.order_book_manager.update_lighter_bbo()

                                elif data.get("type") == "ping":
                                    await ws.send(json.dumps({"type": "pong"}))

                                elif data.get("type") == "update/account_orders":
                                    orders = data.get("orders", {}).get(str(self.lighter_market_index), [])
                                    for order in orders:
                                        order_status = order.get("status")
                                        client_order_id = order.get("client_order_id", "UNKNOWN")
                                        filled_base_amount = Decimal(str(order.get("filled_base_amount", 0)))
                                        
                                        # Handle filled orders (status == "filled" OR filled_base_amount > 0)
                                        if (order_status == "filled" or filled_base_amount > 0) and self.on_lighter_order_filled:
                                            # Only process if there's actual fill
                                            if filled_base_amount > 0:
                                                self.on_lighter_order_filled(order)
                                            else:
                                                self.logger.debug(
                                                    f"📋 [Lighter Order] Status is 'filled' but filled_base_amount is 0, "
                                                    f"skipping callback")
                                        elif order_status and order_status != "filled":
                                            # Log non-filled orders for debugging, but don't warn for 'open' status
                                            if order_status == "open":
                                                self.logger.debug(
                                                    f"📋 [Lighter Order] Order {client_order_id} is open, "
                                                    f"filled={filled_base_amount}, remaining={order.get('remaining_base_amount', 'N/A')}")
                                            elif "canceled" in order_status.lower():
                                                # Handle canceled orders (e.g., canceled-margin-not-allowed)
                                                self.logger.error(
                                                    f"❌ [Lighter Order CANCELED] Order {client_order_id} was canceled "
                                                    f"with status '{order_status}'. Order data: {order}")
                                                if self.on_lighter_order_canceled:
                                                    self.on_lighter_order_canceled(order)
                                            else:
                                                self.logger.warning(
                                                    f"⚠️ [Lighter Order] Received order with status '{order_status}' "
                                                    f"(not 'filled'), order data: {order}")

                                elif (data.get("type") == "update/order_book" and
                                      not self.order_book_manager.lighter_snapshot_loaded):
                                    continue

                            cleanup_counter += 1
                            if cleanup_counter >= 1000:
                                cleanup_counter = 0

                            if self.order_book_manager.lighter_order_book_sequence_gap:
                                try:
                                    await self.request_fresh_snapshot(ws)
                                    self.order_book_manager.lighter_order_book_sequence_gap = False
                                except Exception as e:
                                    self.logger.error(f"⚠️ Failed to request fresh snapshot: {e}")
                                    break

                        except asyncio.TimeoutError:
                            timeout_count += 1
                            if timeout_count % 3 == 0:
                                self.logger.warning(
                                    f"⏰ No message from Lighter websocket for {timeout_count} seconds")
                            continue
                        except websockets.exceptions.ConnectionClosed as e:
                            self.logger.warning(f"⚠️ Lighter websocket connection closed: {e}")
                            break
                        except websockets.exceptions.WebSocketException as e:
                            self.logger.warning(f"⚠️ Lighter websocket error: {e}")
                            break
                        except Exception as e:
                            self.logger.error(f"⚠️ Error in Lighter websocket: {e}")
                            self.logger.error(f"⚠️ Full traceback: {traceback.format_exc()}")
                            break

            except Exception as e:
                import traceback
                error_details = str(e)
                status_code = getattr(e, 'status_code', None)
                
                # Check if this is HTTP 400 - likely server-side restriction
                if status_code == 400 or '400' in error_details:
                    self.logger.warning(f"⚠️ Lighter WebSocket 连接被服务器拒绝 (HTTP 400): {error_details}")
                    self.logger.warning("⚠️ 将使用 REST API 轮询获取数据")
                else:
                    # Try to extract more info from the exception
                    if hasattr(e, 'response'):
                        try:
                            error_details += f" | Response: {e.response.text[:200]}"
                        except:
                            pass
                    
                    self.logger.warning(f"⚠️ Lighter websocket connection failed (HTTP {status_code}): {error_details}")
                
                # Log full traceback for debugging
                self.logger.debug(f"Connection error traceback: {traceback.format_exc()}")
                
                # Reduce retry frequency to avoid spamming
                await asyncio.sleep(10)

    def start_lighter_websocket(self):
        """Start Lighter WebSocket task."""
        if self.lighter_ws_task is None or self.lighter_ws_task.done():
            self.lighter_ws_task = asyncio.create_task(self.handle_lighter_ws())
            # Lighter WebSocket task started

    def shutdown(self):
        """Shutdown WebSocket connections."""
        # Close EdgeX WebSocket connections
        if self.edgex_ws_manager:
            try:
                self.edgex_ws_manager.disconnect_all()
                self.logger.info("🔌 EdgeX WebSocket connections disconnected")
            except Exception as e:
                self.logger.error(f"Error disconnecting EdgeX WebSocket: {e}")

        # Cancel Lighter WebSocket task
        if self.lighter_ws_task and not self.lighter_ws_task.done():
            try:
                self.lighter_ws_task.cancel()
                self.logger.info("🔌 Lighter WebSocket task cancelled")
            except Exception as e:
                self.logger.error(f"Error cancelling Lighter WebSocket task: {e}")

        # Cancel Nado WebSocket task
        if self.nado_ws_task and not self.nado_ws_task.done():
            try:
                self.nado_ws_task.cancel()
                self.logger.info("🔌 Nado WebSocket task cancelled")
            except Exception as e:
                self.logger.error(f"Error cancelling Nado WebSocket task: {e}")

        self.stop_flag = True
    
    # ========== Nado WebSocket methods ==========
    
    def set_nado_config(self, product_id: int, wallet_address: str = None):
        """Set Nado WebSocket configuration.
        
        Args:
            product_id: Nado product ID (e.g., 4 for BTC)
            wallet_address: Wallet address for order update subscriptions
        """
        self.nado_product_id = product_id
        self.nado_wallet_address = wallet_address or os.getenv('NADO_WALLET_ADDRESS')
        # Nado WebSocket config set
    
    def start_nado_websocket(self):
        """Start Nado WebSocket task."""
        if self.nado_ws_task is None or self.nado_ws_task.done():
            self.nado_ws_task = asyncio.create_task(self.handle_nado_ws())
            # Nado WebSocket task started
    
    async def handle_nado_ws(self):
        """Handle Nado WebSocket messages.
        
        Handles:
        - best_bid_offer: Best bid/ask prices
        - book_depth: Order book updates
        - order_update: Order status updates
        - fill: Fill notifications
        """
        from strategy.nado_client import NadoWebSocketClient
        
        reconnect_interval = 5
        max_reconnect_attempts = 10
        reconnect_count = 0
        
        while not self.stop_flag and reconnect_count < max_reconnect_attempts:
            try:
                # Connecting to Nado WebSocket
                
                ws_client = NadoWebSocketClient(
                    product_id=self.nado_product_id,
                    wallet_address=self.nado_wallet_address
                )
                
                # Set callbacks
                ws_client.set_callbacks(
                    on_order_book_update=self._handle_nado_order_book_update,
                    on_best_bid_offer=self._handle_nado_best_bid_offer,
                    on_order_update=self._handle_nado_order_update,
                    on_fill=self._handle_nado_fill
                )
                
                connected = await ws_client.connect()
                if not connected:
                    reconnect_count += 1
                    self.logger.warning(
                        f"⚠️ Failed to connect to Nado WebSocket (attempt {reconnect_count}/{max_reconnect_attempts})")
                    await asyncio.sleep(reconnect_interval)
                    continue
                
                self.logger.info("✅ Connected to Nado WebSocket")
                reconnect_count = 0  # Reset on successful connection
                
                # Handle messages
                await ws_client.handle_messages()
                
            except asyncio.CancelledError:
                self.logger.info("Nado WebSocket task cancelled")
                break
            except websockets.exceptions.ConnectionClosed as e:
                reconnect_count += 1
                self.logger.warning(
                    f"⚠️ Nado WebSocket connection closed (attempt {reconnect_count}/{max_reconnect_attempts}): {e}")
                await asyncio.sleep(reconnect_interval)
            except Exception as e:
                reconnect_count += 1
                self.logger.error(f"⚠️ Nado WebSocket error (attempt {reconnect_count}/{max_reconnect_attempts}): {e}")
                self.logger.error(f"Traceback: {traceback.format_exc()}")
                await asyncio.sleep(reconnect_interval)
            finally:
                try:
                    await ws_client.close()
                except:
                    pass
        
        if reconnect_count >= max_reconnect_attempts:
            self.logger.error("❌ Nado WebSocket max reconnection attempts reached")
    
    def _handle_nado_order_book_update(self, bids: list, asks: list):
        """Handle Nado order book update.
        
        Args:
            bids: List of (price, size) tuples (price in Wei format)
            asks: List of (price, size) tuples (price in Wei format)
        """
        try:
            # Convert prices from Wei to USD (divide by 1e18)
            formatted_bids = [[str(Decimal(price) / Decimal('1e18')), str(size)] for price, size in bids]
            formatted_asks = [[str(Decimal(price) / Decimal('1e18')), str(size)] for price, size in asks]
            
            self.order_book_manager.update_nado_order_book("bids", formatted_bids)
            self.order_book_manager.update_nado_order_book("asks", formatted_asks)
            self.order_book_manager.update_nado_bbo()
            
            self.logger.debug(
                f"📊 [Nado Order Book] Updated: {len(bids)} bids, {len(asks)} asks")
            
        except Exception as e:
            self.logger.error(f"Error handling Nado order book update: {e}")
    
    def _handle_nado_best_bid_offer(self, bid_price: Decimal, ask_price: Decimal):
        """Handle Nado best bid/offer update.
        
        Args:
            bid_price: Best bid price (Wei format)
            ask_price: Best ask price (Wei format)
        """
        try:
            # Convert from Wei to USD (divide by 1e18)
            bid_usd = bid_price / Decimal('1e18')
            ask_usd = ask_price / Decimal('1e18')
            
            self.order_book_manager.nado_best_bid = bid_usd
            self.order_book_manager.nado_best_ask = ask_usd
            self.order_book_manager.nado_order_book_ready = True
            
            self.logger.debug(
                f"💰 [Nado BBO] bid={bid_usd}, ask={ask_usd}")
            
        except Exception as e:
            self.logger.error(f"Error handling Nado BBO update: {e}")
    
    def _handle_nado_order_update(self, order_data: dict):
        """Handle Nado order update.
        
        Args:
            order_data: Order update data from WebSocket
        """
        try:
            order_status = order_data.get('status', 'UNKNOWN')
            digest = order_data.get('digest', 'UNKNOWN')
            
            self.logger.info(
                f"📋 [Nado Order Update] digest={digest}, status={order_status}")
            
            # Notify order manager
            if hasattr(self, 'order_manager'):
                self.order_manager.handle_nado_order_update(order_data)
            
            # Handle fill
            if order_status == 'filled' and self.on_lighter_order_filled:
                # Reuse Lighter order filled callback
                self.on_lighter_order_filled(order_data)
            
        except Exception as e:
            self.logger.error(f"Error handling Nado order update: {e}")
            self.logger.error(f"Order data: {order_data}")
    
    def _handle_nado_fill(self, fill_data: dict):
        """Handle Nado fill notification.
        
        Args:
            fill_data: Fill data from WebSocket
        """
        try:
            self.logger.info(f"✅ [Nado Fill] {fill_data}")
            
            # Notify order manager
            if hasattr(self, 'order_manager'):
                self.order_manager.handle_nado_order_update(fill_data)
            
        except Exception as e:
            self.logger.error(f"Error handling Nado fill: {e}")

