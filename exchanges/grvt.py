# exchanges/grvt.py
"""
GRVT 交易所客户端实现
基于 pysdk 官方 SDK
"""

import os
import asyncio
import time
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple

try:
    from pysdk.grvt_ccxt import GrvtCcxt
    from pysdk.grvt_ccxt_ws import GrvtCcxtWS
    from pysdk.grvt_ccxt_env import GrvtEnv, GrvtWSEndpointType
except ImportError:
    from grvt_pysdk import GrvtCcxt, GrvtCcxtWS, GrvtEnv, GrvtWSEndpointType

from .base import BaseExchangeClient, OrderResult, OrderInfo
from decimal import Decimal, ROUND_HALF_UP


class GrvtClient(BaseExchangeClient):
    """GRVT 交易所客户端"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化 GRVT 客户端
        
        需要的配置项：
        - tick_size: 价格精度
        - contract_id: 合约ID（如 ETH-USD-PERP）
        - quantity: 交易数量
        - direction: 开仓方向（buy/sell）
        - close_order_side: 平仓方向（buy/sell）
        """
        # 先保存 config，基类会设置 self.config
        self._raw_config = config
        
        # 兼容 dict 和 object 类型的 config
        if isinstance(config, dict):
            self._config_dict = config
            self.ticker = config.get('ticker', '')
            self.contract_id = config.get('contract_id', '')
            self.quantity = Decimal(str(config.get('quantity', 0)))
            self.tick_size = Decimal(str(config.get('tick_size', 0.1)))
            self.direction = config.get('direction', 'buy')
            self.close_order_side = config.get('close_order_side', 'sell')
        else:
            # object with attributes
            self._config_dict = {}
            self.ticker = getattr(config, 'ticker', '')
            self.contract_id = getattr(config, 'contract_id', '')
            self.quantity = getattr(config, 'quantity', Decimal(0))
            self.tick_size = getattr(config, 'tick_size', Decimal(0.1))
            self.direction = getattr(config, 'direction', 'buy')
            self.close_order_side = getattr(config, 'close_order_side', 'sell')
        
        # 调用基类初始化（会设置 self.config）
        super().__init__(config)
        
        # Initialize logger
        import logging
        ticker = self.ticker if hasattr(self, 'ticker') else config.get('ticker', 'GRVT') if isinstance(config, dict) else getattr(config, 'ticker', 'GRVT')
        self.logger = logging.getLogger(f"grvt_{ticker.lower()}")
        if not self.logger.handlers:
            # Add console handler if no handlers exist
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)
        
        # GRVT credentials - 从环境变量读取
        self.trading_account_id = os.getenv('GRVT_TRADING_ACCOUNT_ID')
        self.private_key = os.getenv('GRVT_PRIVATE_KEY')
        self.api_key = os.getenv('GRVT_API_KEY')
        self.environment = os.getenv('GRVT_ENVIRONMENT', 'prod')
        
        if not self.trading_account_id or not self.private_key or not self.api_key:
            raise ValueError(
                "GRVT_TRADING_ACCOUNT_ID, GRVT_PRIVATE_KEY, and GRVT_API_KEY must be set "
                "in environment variables"
            )
        
        # Convert environment string to proper enum
        env_map = {
            'prod': GrvtEnv.PROD,
            'testnet': GrvtEnv.TESTNET,
            'staging': GrvtEnv.STAGING,
            'dev': GrvtEnv.DEV
        }
        self.env = env_map.get(self.environment.lower(), GrvtEnv.PROD)
        
        # 初始化 GRVT SDK 客户端
        self._initialize_grvt_clients()
        
        self._order_update_handler = None
        self._ws_client = None
        self._order_update_callback = None
        
        # 市场价格缓存
        self._best_bid = Decimal(0)
        self._best_ask = Decimal(0)
        self._price_update_time = 0
        self._price_max_age = 0.5  # 价格最大有效期（秒）
        self._market_data_subscribed = False
    
    def _initialize_grvt_clients(self) -> None:
        """初始化 GRVT REST 和 WebSocket 客户端"""
        try:
            parameters = {
                'trading_account_id': self.trading_account_id,
                'private_key': self.private_key,
                'api_key': self.api_key
            }
            
            # 初始化 REST 客户端
            self.rest_client = GrvtCcxt(
                env=self.env,
                parameters=parameters
            )
            
        except Exception as e:
            raise ValueError(f"Failed to initialize GRVT client: {e}")
    
    def _validate_config(self) -> None:
        """验证配置（由 __init__ 处理，此处跳过）"""
        pass
    
    def round_to_tick(self, price) -> Decimal:
        """重写基类方法，使用实例属性而非 config"""
        price = Decimal(price)
        tick = self.tick_size
        return price.quantize(tick, rounding=ROUND_HALF_UP)
    
    # ========== 连接管理 ==========
    
    async def connect(self) -> None:
        """建立 WebSocket 连接"""
        try:
            loop = asyncio.get_running_loop()
            
            from pysdk.grvt_ccxt_logging_selector import logger
            
            parameters = {
                'api_key': self.api_key,
                'trading_account_id': self.trading_account_id,
                'api_ws_version': 'v1',
                'private_key': self.private_key
            }
            
            self._ws_client = GrvtCcxtWS(
                env=self.env,
                loop=loop,
                logger=logger,
                parameters=parameters
            )
            
            await self._ws_client.initialize()
            await asyncio.sleep(2)  # 等待连接建立
            
            # 订阅订单更新
            if self._order_update_callback is not None:
                await self._subscribe_to_orders(self._order_update_callback)
            
            # 订阅市场价格
            await self._subscribe_to_market_data()
            
        except Exception as e:
            raise ValueError(f"Error connecting to GRVT WebSocket: {e}")
    
    async def disconnect(self) -> None:
        """断开连接"""
        if self._ws_client:
            await self._ws_client.__aexit__()
    
    def get_exchange_name(self) -> str:
        """返回交易所名称"""
        return "grvt"
    
    # ========== WebSocket 订阅 ==========
    
    def setup_order_update_handler(self, handler) -> None:
        """设置订单更新回调"""
        self._order_update_handler = handler
        
        async def order_update_callback(message: Dict[str, Any]):
            """处理订单更新"""
            try:
                if 'feed' in message:
                    data = message.get('feed', {})
                    leg = data.get('legs', [])[0] if data.get('legs') else None
                    
                    if isinstance(data, dict) and leg:
                        contract_id = leg.get('instrument', '')
                        if contract_id != self.contract_id:
                            return
                        
                        order_state = data.get('state', {})
                        order_id = data.get('order_id', '')
                        status = order_state.get('status', '')
                        side = 'buy' if leg.get('is_buying_asset') else 'sell'
                        size = leg.get('size', '0')
                        price = leg.get('limit_price', '0')
                        filled_size = order_state.get('traded_size', ['0'])[0] if order_state.get('traded_size') else '0'
                        
                        if order_id and status:
                            # 确定订单类型
                            if side == self.close_order_side:
                                order_type = "CLOSE"
                            else:
                                order_type = "OPEN"
                            
                            # 状态映射
                            status_map = {
                                'OPEN': 'OPEN',
                                'FILLED': 'FILLED',
                                'CANCELLED': 'CANCELED',
                                'REJECTED': 'CANCELED'
                            }
                            mapped_status = status_map.get(status, status)
                            
                            # 处理部分成交
                            if status == 'OPEN' and Decimal(filled_size) > 0:
                                mapped_status = "PARTIALLY_FILLED"
                            
                            if mapped_status in ['OPEN', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED']:
                                if self._order_update_handler:
                                    self._order_update_handler({
                                        'order_id': order_id,
                                        'side': side,
                                        'order_type': order_type,
                                        'status': mapped_status,
                                        'size': size,
                                        'price': price,
                                        'contract_id': contract_id,
                                        'filled_size': filled_size
                                    })
                            
            except Exception as e:
                print(f"Error handling order update: {e}")
        
        self._order_update_callback = order_update_callback
        
        if self._ws_client:
            asyncio.create_task(self._subscribe_to_orders(self._order_update_callback))
    
    async def _subscribe_to_orders(self, callback):
        """订阅订单更新"""
        try:
            await self._ws_client.subscribe(
                stream="order",
                callback=callback,
                ws_end_point_type=GrvtWSEndpointType.TRADE_DATA_RPC_FULL,
                params={"instrument": self.contract_id}
            )
            print(f"Successfully subscribed to order updates for {self.contract_id}")
        except Exception as e:
            print(f"Error subscribing to order updates: {e}")
    
    async def _subscribe_to_market_data(self):
        """订阅市场价格"""
        if not self._ws_client or self._market_data_subscribed:
            return
        
        try:
            async def market_data_callback(message: Dict[str, Any]):
                """处理市场价格更新"""
                try:
                    if 'feed' in message:
                        feed = message.get('feed', {})
                        
                        # 处理完整订单簿
                        if 'bids' in feed and 'asks' in feed:
                            bids = feed.get('bids', [])
                            asks = feed.get('asks', [])
                            if bids:
                                self._best_bid = Decimal(bids[0].get('price', 0))
                            if asks:
                                self._best_ask = Decimal(asks[0].get('price', 0))
                            self._price_update_time = time.time()
                        
                        # 处理快照
                        elif 'snapshot' in message:
                            snapshot = message.get('snapshot', {})
                            if 'bids' in snapshot and 'asks' in snapshot:
                                bids = snapshot.get('bids', [])
                                asks = snapshot.get('asks', [])
                                if bids:
                                    self._best_bid = Decimal(bids[0].get('price', 0))
                                if asks:
                                    self._best_ask = Decimal(asks[0].get('price', 0))
                                self._price_update_time = time.time()
                except Exception as e:
                    print(f"Error handling market data: {e}")
            
            # 订阅订单簿数据
            try:
                await self._ws_client.subscribe(
                    stream="book",
                    callback=market_data_callback,
                    ws_end_point_type=GrvtWSEndpointType.MARKET_DATA_RPC_FULL,
                    params={"instrument": self.contract_id}
                )
                self._market_data_subscribed = True
            except Exception as e:
                # 降级到普通 MARKET_DATA
                await self._ws_client.subscribe(
                    stream="book",
                    callback=market_data_callback,
                    ws_end_point_type=GrvtWSEndpointType.MARKET_DATA,
                    params={"instrument": self.contract_id}
                )
                self._market_data_subscribed = True
                
        except Exception as e:
            print(f"Error subscribing to market data: {e}")
    
    # ========== 价格获取 ==========
    
    async def fetch_bbo_prices(self, contract_id: str, force_refresh: bool = False) -> Tuple[Decimal, Decimal]:
        """
        获取最优买卖价
        
        优先使用 WebSocket 实时价格，如果过期则使用 REST API
        """
        current_time = time.time()
        
        # 如果强制刷新或价格过期，使用 REST API
        if force_refresh:
            pass
        elif self._market_data_subscribed and self._best_bid > 0 and self._best_ask > 0:
            price_age = current_time - self._price_update_time if self._price_update_time > 0 else float('inf')
            if price_age <= self._price_max_age:
                return self._best_bid, self._best_ask
        
        # REST API 获取价格
        order_book = self.rest_client.fetch_order_book(contract_id, limit=10)
        
        if not order_book or 'bids' not in order_book or 'asks' not in order_book:
            raise ValueError(f"Unable to get order book: {order_book}")
        
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])
        
        best_bid = Decimal(bids[0]['price']) if bids and len(bids) > 0 else Decimal(0)
        best_ask = Decimal(asks[0]['price']) if asks and len(asks) > 0 else Decimal(0)
        
        # 更新缓存
        if best_bid > 0 and best_ask > 0:
            self._best_bid = best_bid
            self._best_ask = best_ask
            self._price_update_time = current_time
        
        return best_bid, best_ask
    
    async def get_order_price(self, direction: str) -> Decimal:
        """计算订单价格"""
        best_bid, best_ask = await self.fetch_bbo_prices(self.contract_id)
        
        if best_bid <= 0 or best_ask <= 0:
            raise ValueError("Invalid bid/ask prices")
        
        if direction == 'buy':
            return best_ask - self.tick_size
        elif direction == 'sell':
            return best_bid + self.tick_size
        else:
            raise ValueError("Invalid direction")
    
    # ========== 订单操作 ==========
    
    async def place_post_only_order(self, contract_id: str, quantity: Decimal, 
                                     price: Decimal, side: str) -> OrderInfo:
        """下 post-only 限价单（Maker 单）"""
        try:
            order_result = self.rest_client.create_limit_order(
                symbol=contract_id,
                side=side,
                amount=quantity,
                price=price,
                params={
                    'post_only': True,
                    'order_duration_secs': 30 * 86400 - 1,
                }
            )
            
            # Log full response for debugging
            self.logger.debug(f"📋 [GRVT Order Response] Full result: {order_result}")
            
            # Check if response is empty (API error case - SDK returns {} on error)
            if not order_result or order_result == {}:
                # Check if there's error info in SDK logs or response
                # Common errors: Insufficient margin (2080), Invalid order, etc.
                error_msg = "GRVT API returned empty response (likely API error - check logs for details like 'Insufficient margin')"
                self.logger.error(f"❌ [GRVT Order] {error_msg}")
                self.logger.error(f"   This usually means: Insufficient margin, invalid order parameters, or API error")
                raise Exception(error_msg)
            
            # Check for error in response (some error responses may have error field)
            if 'error' in order_result:
                error_msg = order_result.get('error', 'Unknown error')
                error_code = order_result.get('code', 'N/A')
                self.logger.error(f"❌ [GRVT Order] Error from GRVT API: {error_msg} (code: {error_code})")
                raise Exception(f"GRVT API error: {error_msg} (code: {error_code})")
            
            # Check for error codes in response
            if 'code' in order_result and order_result.get('code') != 200:
                error_msg = order_result.get('message', 'Unknown error')
                error_code = order_result.get('code', 'N/A')
                self.logger.error(f"❌ [GRVT Order] Error from GRVT API: {error_msg} (code: {error_code})")
                raise Exception(f"GRVT API error: {error_msg} (code: {error_code})")
        
        except Exception as e:
            # Re-raise with more context
            error_msg = f"Error placing post-only order: {str(e)}"
            self.logger.error(f"❌ [GRVT Order] {error_msg}")
            self.logger.error(f"   Contract: {contract_id}, Side: {side}, Quantity: {quantity}, Price: {price}")
            import traceback
            self.logger.error(f"   Traceback: {traceback.format_exc()}")
            raise Exception(error_msg) from e
        
        # Extract result from response
        # GRVT SDK may return:
        # 1. {'result': {...}} - wrapped format
        # 2. {...} - direct format (order data directly)
        # 3. {} - empty (error case, already handled above)
        if 'result' in order_result:
            result_data = order_result['result']
        elif 'metadata' in order_result and 'state' in order_result:
            # Response is already in the correct format (direct order data)
            result_data = order_result
        else:
            # Try to use order_result directly if it has order-like structure
            result_data = order_result
        
        if not result_data:
            error_msg = "No result data in order response"
            self.logger.error(f"❌ [GRVT Order] {error_msg}")
            raise Exception(error_msg)
        
        # Get metadata and state
        metadata = result_data.get('metadata')
        state = result_data.get('state')
        
        if not metadata:
            error_msg = f"No 'metadata' in result. Result data: {result_data}"
            self.logger.error(f"❌ [GRVT Order] {error_msg}")
            raise Exception(error_msg)
        
        if not state:
            error_msg = f"No 'state' in result. Result data: {result_data}"
            self.logger.error(f"❌ [GRVT Order] {error_msg}")
            raise Exception(error_msg)
        
        client_order_id = metadata.get('client_order_id')
        order_status = state.get('status')
        
        if not client_order_id:
            error_msg = f"No 'client_order_id' in metadata. Metadata: {metadata}"
            self.logger.error(f"❌ [GRVT Order] {error_msg}")
            raise Exception(error_msg)
        
        # 等待订单状态更新
        order_status_start_time = time.time()
        order_info = await self.get_order_info(client_order_id=str(client_order_id))
        
        while order_status in ['PENDING'] and time.time() - order_status_start_time < 10:
            await asyncio.sleep(0.05)
            order_info = await self.get_order_info(client_order_id=str(client_order_id))
            if order_info:
                order_status = order_info.status
        
        return order_info
    
    async def place_market_order(self, contract_id: str, quantity: Decimal, 
                                  side: str) -> OrderInfo:
        """下市价单"""
        client_order_id = int(time.time() * 1000)
        
        order_result = self.rest_client.create_order(
            symbol=contract_id,
            order_type="market",
            side=side,
            amount=quantity,
            params={"client_order_id": client_order_id},
        )
        
        if not order_result:
            raise Exception(f"Error placing market order")
        
        # 等待订单状态更新
        order_status = order_result.get('state', {}).get('status')
        order_status_start_time = time.time()
        order_info = await self.get_order_info(client_order_id=str(client_order_id))
        
        while order_status in ['PENDING'] and time.time() - order_status_start_time < 10:
            await asyncio.sleep(0.05)
            order_info = await self.get_order_info(client_order_id=str(client_order_id))
            if order_info:
                order_status = order_info.status
        
        return order_info
    
    async def place_open_order(self, contract_id: str, quantity: Decimal, 
                               direction: str) -> OrderResult:
        """
        下开仓订单
        
        使用 post-only 限价单，确保是 Maker
        """
        # 获取当前市场价格
        best_bid, best_ask = await self.fetch_bbo_prices(contract_id)
        
        if best_bid <= 0 or best_ask <= 0:
            return OrderResult(success=False, error_message='Invalid bid/ask prices')
        
        # 计算订单价格
        if direction == 'buy':
            order_price = best_ask - self.tick_size
        elif direction == 'sell':
            order_price = best_bid + self.tick_size
        else:
            raise ValueError(f"Invalid direction: {direction}")
        
        # 下单
        try:
            order_info = await self.place_post_only_order(contract_id, quantity, order_price, direction)
        except Exception as e:
            return OrderResult(success=False, error_message=str(e))
        
        return OrderResult(
            success=True,
            order_id=order_info.order_id,
            side=direction,
            size=quantity,
            price=order_price,
            status=order_info.status
        )
    
    async def place_close_order(self, contract_id: str, quantity: Decimal, 
                                price: Decimal, side: str) -> OrderResult:
        """
        下平仓订单
        
        使用 post-only 限价单
        """
        # 获取当前市场价格
        best_bid, best_ask = await self.fetch_bbo_prices(contract_id)
        
        # 调整价格确保是 Maker
        if side == 'sell' and price <= best_bid:
            adjusted_price = best_bid + self.tick_size
        elif side == 'buy' and price >= best_ask:
            adjusted_price = best_ask - self.tick_size
        else:
            adjusted_price = price
        
        adjusted_price = self.round_to_tick(adjusted_price)
        
        try:
            order_info = await self.place_post_only_order(contract_id, quantity, adjusted_price, side)
        except Exception as e:
            return OrderResult(success=False, error_message=str(e))
        
        return OrderResult(
            success=True,
            order_id=order_info.order_id,
            side=side,
            size=quantity,
            price=adjusted_price,
            status=order_info.status
        )
    
    async def cancel_order(self, order_id: str) -> OrderResult:
        """取消订单"""
        try:
            cancel_result = self.rest_client.cancel_order(id=order_id)
            return OrderResult(success=bool(cancel_result))
        except Exception as e:
            return OrderResult(success=False, error_message=str(e))
    
    # ========== 订单查询 ==========
    
    async def get_order_info(self, order_id: str = None, 
                             client_order_id: str = None) -> Optional[OrderInfo]:
        """查询订单详情"""
        try:
            if order_id is not None:
                order_data = self.rest_client.fetch_order(id=order_id)
            elif client_order_id is not None:
                order_data = self.rest_client.fetch_order(params={'client_order_id': client_order_id})
            else:
                raise ValueError("Either order_id or client_order_id must be provided")
            
            if not order_data or 'result' not in order_data:
                return None  # 订单不存在（可能被拒绝/取消）
            
            order = order_data['result']
            legs = order.get('legs', [])
            state = order.get('state', {})
            leg = legs[0] if legs else {}
            
            return OrderInfo(
                order_id=order.get('order_id', ''),
                side=leg.get('is_buying_asset', False) and 'buy' or 'sell',
                size=Decimal(leg.get('size', 0)),
                price=Decimal(leg.get('limit_price', 0)),
                status=state.get('status', ''),
                filled_size=Decimal(state.get('traded_size', ['0'])[0]) 
                           if isinstance(state.get('traded_size'), list) else Decimal(0),
                remaining_size=Decimal(state.get('book_size', ['0'])[0]) 
                              if isinstance(state.get('book_size'), list) else Decimal(0)
            )
        except Exception as e:
            # 订单不存在（404）或查询失败，返回 None
            return None
    
    async def get_active_orders(self, contract_id: str) -> List[OrderInfo]:
        """查询活跃订单"""
        orders = self.rest_client.fetch_open_orders(symbol=contract_id)
        
        if not orders:
            return []
        
        order_list = []
        for order in orders:
            legs = order.get('legs', [])
            if not legs:
                continue
            
            leg = legs[0]
            state = order.get('state', {})
            
            order_list.append(OrderInfo(
                order_id=order.get('order_id', ''),
                side=leg.get('is_buying_asset', False) and 'buy' or 'sell',
                size=Decimal(leg.get('size', 0)),
                price=Decimal(leg.get('limit_price', 0)),
                status=state.get('status', ''),
                filled_size=Decimal(state.get('traded_size', ['0'])[0]) 
                           if isinstance(state.get('traded_size'), list) else Decimal(0),
                remaining_size=Decimal(state.get('book_size', ['0'])[0]) 
                              if isinstance(state.get('book_size'), list) else Decimal(0)
            ))
        
        return order_list
    
    # ========== 持仓查询 ==========
    
    async def get_account_positions(self) -> Decimal:
        """查询账户持仓"""
        positions = self.rest_client.fetch_positions()
        
        for position in positions:
            if position.get('instrument') == self.contract_id:
                # 返回净持仓（正数=多头，负数=空头）
                return Decimal(position.get('size', 0))
        
        return Decimal(0)
    
    # ========== 辅助方法 ==========
    
    async def get_contract_attributes(self) -> Tuple[str, Decimal]:
        """获取合约属性"""
        ticker = self.ticker
        
        markets = self.rest_client.fetch_markets()
        
        for market in markets:
            if (market.get('base') == ticker and
                    market.get('quote') == 'USDT' and
                    market.get('kind') == 'PERPETUAL'):
                
                self.contract_id = market.get('instrument', '')
                self.tick_size = Decimal(market.get('tick_size', 0))
                
                return self.contract_id, self.tick_size
        
        raise ValueError(f"Contract not found for ticker: {ticker}")

