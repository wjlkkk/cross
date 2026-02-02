#!/usr/bin/env python3
"""
仓位平衡检查机器人
持续监控两个交易所的持仓，当发现不平衡时及时发出警报或自动平仓。

支持的功能：
1. 持续监控 EdgeX/Lighter 或 GRVT/Lighter 的持仓
2. 检查净持仓是否平衡
3. 检测裸多头或裸空头（高风险状态）
4. 发现不平衡时发送警报（Webhook/日志）
5. 可选的自动平仓功能
6. 持仓数据记录到CSV

使用方法：
    python -m strategy.position_balance_monitor
"""
import asyncio
import signal
import logging
import os
import sys
import time
import csv
import json
import requests
from decimal import Decimal
from datetime import datetime
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass, asdict
import pytz

from dotenv import load_dotenv

# Load environment variables
load_dotenv()


@dataclass
class PositionSnapshot:
    """持仓快照数据结构"""
    timestamp: float
    datetime_str: str
    maker_exchange: str  # 'edgex' or 'grvt'
    maker_position: Decimal
    lighter_position: Decimal
    net_position: Decimal
    is_naked_long: bool
    is_naked_short: bool
    is_balanced: bool
    balance_threshold: Decimal


class PositionBalanceMonitor:
    """仓位平衡监控机器人"""

    def __init__(self,
                 maker_exchange: str = 'edgex',
                 ticker: str = 'SOL',
                 check_interval: int = 10,
                 balance_threshold: Decimal = Decimal('0.05'),
                 alert_webhook_url: Optional[str] = None,
                 auto_close: bool = False,
                 log_to_csv: bool = True):
        """
        初始化仓位平衡监控机器人

        Args:
            maker_exchange: Maker交易所 ('edgex' or 'grvt')
            ticker: 交易对
            check_interval: 检查间隔（秒）
            balance_threshold: 平衡阈值（净持仓超过此值视为不平衡）
            alert_webhook_url: 警报Webhook URL
            auto_close: 发现不平衡时是否自动平仓
            log_to_csv: 是否记录到CSV
        """
        self.maker_exchange = maker_exchange.lower()
        self.ticker = ticker
        self.check_interval = check_interval
        self.balance_threshold = balance_threshold
        self.alert_webhook_url = alert_webhook_url or os.getenv('ALERT_WEBHOOK_URL')
        self.auto_close = auto_close
        self.log_to_csv = log_to_csv

        self.stop_flag = False
        self._setup_logger()

        # Exchange clients (will be initialized later)
        self.edgex_client = None
        self.grvt_client = None
        self.lighter_client = None

        # Position tracking
        self.last_snapshots: List[PositionSnapshot] = []
        self.alert_cooldown = 300  # 5分钟警报冷却时间
        self.last_alert_time = {}  # 按警报类型记录最后发送时间

        # Statistics
        self.check_count = 0
        self.imbalance_count = 0
        self.naked_position_count = 0
        self.auto_close_count = 0

    def _setup_logger(self):
        """设置日志"""
        os.makedirs("logs", exist_ok=True)
        log_filename = f"logs/position_monitor_{self.maker_exchange}_{self.ticker}.txt"

        self.logger = logging.getLogger(f"position_monitor_{self.maker_exchange}_{self.ticker}")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()

        # File handler
        file_handler = logging.FileHandler(log_filename)
        file_handler.setLevel(logging.INFO)

        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)

        # Formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
        self.logger.propagate = False

    def setup_signal_handlers(self):
        """设置信号处理器"""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """信号处理器"""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.stop_flag = True

    async def initialize_edgex_client(self):
        """初始化 EdgeX 客户端"""
        try:
            from edgex_sdk import Client

            edgex_account_id = os.getenv('EDGEX_ACCOUNT_ID')
            edgex_stark_private_key = os.getenv('EDGEX_STARK_PRIVATE_KEY')
            edgex_base_url = os.getenv('EDGEX_BASE_URL', 'https://pro.edgex.exchange')

            if not edgex_account_id or not edgex_stark_private_key:
                raise ValueError("EDGEX_ACCOUNT_ID and EDGEX_STARK_PRIVATE_KEY must be set")

            self.edgex_client = Client(
                account_id=int(edgex_account_id),
                stark_private_key=edgex_stark_private_key,
                base_url=edgex_base_url
            )

            # Get contract ID
            metadata = await self.edgex_client.get_metadata()
            contract_list = metadata.get('data', {}).get('contractList', [])

            for contract in contract_list:
                if contract.get('contractName') == f'{self.ticker}USD':
                    self.edgex_contract_id = contract['contractId']
                    self.edgex_tick_size = Decimal(contract.get('tickSize', '1'))
                    break
            else:
                raise ValueError(f"Contract {self.ticker}USD not found on EdgeX")

            self.logger.info(f"✅ EdgeX client initialized: contract_id={self.edgex_contract_id}")
            return True

        except Exception as e:
            self.logger.error(f"❌ Failed to initialize EdgeX client: {e}")
            return False

    async def initialize_grvt_client(self):
        """初始化 GRVT 客户端"""
        try:
            from exchanges.grvt import GrvtClient
            from .edgex_arb import Config

            grvt_config = Config({
                'ticker': self.ticker,
                'tick_size': Decimal('1'),
                'quantity': Decimal('1'),
                'contract_id': f"{self.ticker}_USDT_Perp",
                'direction': 'buy',
                'close_order_side': 'sell'
            })

            self.grvt_client = GrvtClient(grvt_config)
            contract_id, tick_size = await self.grvt_client.get_contract_attributes()
            self.grvt_contract_id = contract_id
            self.grvt_tick_size = tick_size

            self.logger.info(f"✅ GRVT client initialized: contract_id={self.grvt_contract_id}")
            return True

        except Exception as e:
            self.logger.error(f"❌ Failed to initialize GRVT client: {e}")
            return False

    def initialize_lighter_client(self):
        """初始化 Lighter 客户端"""
        try:
            from lighter.signer_client import SignerClient

            lighter_base_url = os.getenv('LIGHTER_BASE_URL', 'https://mainnet.zklighter.elliot.ai')
            account_index = int(os.getenv('LIGHTER_ACCOUNT_INDEX', '0'))
            api_key_index = int(os.getenv('LIGHTER_API_KEY_INDEX', '0'))
            api_key_private_key = os.getenv('API_KEY_PRIVATE_KEY')

            if not api_key_private_key:
                raise ValueError("API_KEY_PRIVATE_KEY must be set")

            api_private_keys = {api_key_index: api_key_private_key}

            self.lighter_client = SignerClient(
                url=lighter_base_url,
                account_index=account_index,
                api_private_keys=api_private_keys,
            )

            err = self.lighter_client.check_client()
            if err is not None:
                raise Exception(f"CheckClient error: {err}")

            # Get market info
            response = requests.get(
                f"{lighter_base_url}/api/v1/orderBooks",
                headers={"accept": "application/json"},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            for market in data.get("order_books", []):
                if market["symbol"] == self.ticker:
                    self.lighter_market_id = market["market_id"]
                    self.base_amount_multiplier = 10 ** market["supported_size_decimals"]
                    self.price_multiplier = 10 ** market["supported_price_decimals"]
                    break
            else:
                raise ValueError(f"Market {self.ticker} not found on Lighter")

            self.lighter_account_index = account_index
            self.lighter_base_url = lighter_base_url

            self.logger.info(f"✅ Lighter client initialized: market_id={self.lighter_market_id}")
            return True

        except Exception as e:
            self.logger.error(f"❌ Failed to initialize Lighter client: {e}")
            return False

    async def get_edgex_position(self) -> Decimal:
        """获取 EdgeX 持仓"""
        try:
            positions_data = await self.edgex_client.get_account_positions()
            if not positions_data or 'data' not in positions_data:
                return Decimal('0')

            positions = positions_data.get('data', {}).get('positionList', [])
            for p in positions:
                if isinstance(p, dict) and p.get('contractId') == self.edgex_contract_id:
                    return Decimal(p.get('openSize', 0))
            return Decimal('0')

        except Exception as e:
            self.logger.error(f"❌ Error getting EdgeX position: {e}")
            return Decimal('0')

    async def get_grvt_position(self) -> Decimal:
        """获取 GRVT 持仓"""
        try:
            position = await self.grvt_client.get_account_positions()
            return position
        except Exception as e:
            self.logger.error(f"❌ Error getting GRVT position: {e}")
            return Decimal('0')

    def get_lighter_position(self) -> Decimal:
        """获取 Lighter 持仓"""
        try:
            url = f"{self.lighter_base_url}/api/v1/account"
            headers = {"accept": "application/json"}
            parameters = {"by": "index", "value": self.lighter_account_index}

            response = requests.get(url, headers=headers, params=parameters, timeout=10)
            response.raise_for_status()

            data = response.json()
            if 'accounts' not in data or not data['accounts']:
                return Decimal('0')

            positions = data['accounts'][0].get('positions', [])
            for position in positions:
                if position.get('symbol') == self.ticker:
                    return Decimal(position['position']) * position['sign']
            return Decimal('0')

        except Exception as e:
            self.logger.error(f"❌ Error getting Lighter position: {e}")
            return Decimal('0')

    async def get_positions(self) -> Tuple[Decimal, Decimal]:
        """获取两个交易所的持仓"""
        if self.maker_exchange == 'edgex':
            maker_pos = await self.get_edgex_position()
        else:  # grvt
            maker_pos = await self.get_grvt_position()

        lighter_pos = self.get_lighter_position()
        return maker_pos, lighter_pos

    def check_balance(self, maker_pos: Decimal, lighter_pos: Decimal) -> Dict:
        """
        检查持仓平衡

        Returns:
            dict: {
                'is_balanced': bool,
                'net_position': Decimal,
                'is_naked_long': bool,
                'is_naked_short': bool,
                'alert_type': str or None
            }
        """
        net_position = maker_pos + lighter_pos

        # 检查是否平衡（净持仓接近0）
        is_balanced = abs(net_position) <= self.balance_threshold

        # 检查裸多头（两个都是正数）
        is_naked_long = maker_pos > Decimal('0.01') and lighter_pos > Decimal('0.01')

        # 检查裸空头（两个都是负数）
        is_naked_short = maker_pos < Decimal('-0.01') and lighter_pos < Decimal('-0.01')

        # 确定警报类型
        alert_type = None
        if is_naked_long:
            alert_type = 'NAKED_LONG'
        elif is_naked_short:
            alert_type = 'NAKED_SHORT'
        elif not is_balanced:
            alert_type = 'IMBALANCE'

        return {
            'is_balanced': is_balanced,
            'net_position': net_position,
            'is_naked_long': is_naked_long,
            'is_naked_short': is_naked_short,
            'alert_type': alert_type
        }

    def should_send_alert(self, alert_type: str) -> bool:
        """检查是否应该发送警报（考虑冷却时间）"""
        current_time = time.time()
        last_time = self.last_alert_time.get(alert_type, 0)
        return (current_time - last_time) >= self.alert_cooldown

    async def send_alert(self, snapshot: PositionSnapshot, balance_info: Dict):
        """发送警报"""
        alert_type = balance_info['alert_type']

        if not self.should_send_alert(alert_type):
            self.logger.debug(f"Alert {alert_type} in cooldown, skipping")
            return

        self.last_alert_time[alert_type] = time.time()

        # 构建警报消息
        if alert_type == 'NAKED_LONG':
            emoji = "🚨"
            title = f"🚨 [Naked Long Detected] {self.maker_exchange.upper()}/{self.ticker}"
            description = (
                f"**Naked Long Position Detected!**\n"
                f"{self.maker_exchange.upper()}: {snapshot.maker_position}\n"
                f"Lighter: {snapshot.lighter_position}\n"
                f"Net: {snapshot.net_position}\n"
                f"Both exchanges have LONG positions - HIGH RISK!"
            )
        elif alert_type == 'NAKED_SHORT':
            emoji = "🚨"
            title = f"🚨 [Naked Short Detected] {self.maker_exchange.upper()}/{self.ticker}"
            description = (
                f"**Naked Short Position Detected!**\n"
                f"{self.maker_exchange.upper()}: {snapshot.maker_position}\n"
                f"Lighter: {snapshot.lighter_position}\n"
                f"Net: {snapshot.net_position}\n"
                f"Both exchanges have SHORT positions - HIGH RISK!"
            )
        else:  # IMBALANCE
            emoji = "⚠️"
            title = f"⚠️ [Position Imbalance] {self.maker_exchange.upper()}/{self.ticker}"
            description = (
                f"**Position Imbalance Detected**\n"
                f"{self.maker_exchange.upper()}: {snapshot.maker_position}\n"
                f"Lighter: {snapshot.lighter_position}\n"
                f"Net: {snapshot.net_position} (threshold: {self.balance_threshold})"
            )

        # Log alert
        self.logger.warning(f"{emoji} {title}")
        for line in description.split('\n'):
            self.logger.warning(f"   {line}")

        # Send webhook alert
        if self.alert_webhook_url:
            await self._send_webhook_alert(title, description, alert_type)

        # Auto-close if enabled
        if self.auto_close:
            self.logger.warning(f"🔄 Auto-close enabled, attempting to close positions...")
            await self.auto_close_positions(snapshot.maker_position, snapshot.lighter_position)

    async def _send_webhook_alert(self, title: str, description: str, alert_type: str):
        """发送 Webhook 警报"""
        try:
            # 颜色映射
            colors = {
                'NAKED_LONG': 0xFF0000,  # 红色
                'NAKED_SHORT': 0xFF0000,  # 红色
                'IMBALANCE': 0xFFAA00,    # 橙色
            }

            # Discord webhook format
            if 'discord' in self.alert_webhook_url.lower():
                payload = {
                    "embeds": [{
                        "title": title,
                        "description": description,
                        "color": colors.get(alert_type, 0xFFAA00),
                        "timestamp": datetime.utcnow().isoformat()
                    }]
                }
            # Telegram bot format
            elif 'telegram' in self.alert_webhook_url.lower():
                message = f"{title}\n\n{description}"
                payload = {"text": message}
            # Generic webhook
            else:
                payload = {
                    "title": title,
                    "description": description,
                    "alert_type": alert_type,
                    "timestamp": datetime.utcnow().isoformat(),
                    "maker_exchange": self.maker_exchange,
                    "ticker": self.ticker
                }

            response = requests.post(
                self.alert_webhook_url,
                json=payload,
                timeout=5
            )
            response.raise_for_status()
            self.logger.info(f"✅ Alert sent to webhook: {alert_type}")

        except Exception as e:
            self.logger.error(f"❌ Failed to send webhook alert: {e}")

    async def auto_close_positions(self, maker_pos: Decimal, lighter_pos: Decimal):
        """自动平仓"""
        try:
            self.auto_close_count += 1

            # 关 Maker 仓位
            if abs(maker_pos) > Decimal('0.01'):
                await self._close_maker_position(maker_pos)

            # 关 Lighter 仓位
            if abs(lighter_pos) > Decimal('0.01'):
                await self._close_lighter_position(lighter_pos)

            # 等待订单成交
            await asyncio.sleep(3)

            # 验证持仓
            new_maker_pos, new_lighter_pos = await self.get_positions()
            new_net = new_maker_pos + new_lighter_pos

            if abs(new_net) <= self.balance_threshold:
                self.logger.info(f"✅ Auto-close successful: net={new_net}")
            else:
                self.logger.error(f"❌ Auto-close incomplete: net={new_net}")

        except Exception as e:
            self.logger.error(f"❌ Auto-close failed: {e}")

    async def _close_maker_position(self, position: Decimal):
        """平 Maker 交易所仓位"""
        try:
            if self.maker_exchange == 'edgex':
                await self._close_edgex_position(position)
            else:
                await self._close_grvt_position(position)
        except Exception as e:
            self.logger.error(f"❌ Failed to close {self.maker_exchange} position: {e}")

    async def _close_edgex_position(self, position: Decimal):
        """平 EdgeX 仓位"""
        try:
            from edgex_sdk import OrderSide

            side = OrderSide.SELL if position > 0 else OrderSide.BUY
            quantity = abs(position)

            # 获取当前市场价格
            # 使用对手价确保成交
            self.logger.info(f"🔄 Closing EdgeX position: {side} {quantity}")

            order_result = await self.edgex_client.create_limit_order(
                contract_id=self.edgex_contract_id,
                size=str(quantity),
                price=str(Decimal('0')),  # Will be set by market
                side=side,
                post_only=False
            )

            self.logger.info(f"✅ EdgeX close order submitted: {order_result}")

        except Exception as e:
            self.logger.error(f"❌ EdgeX close failed: {e}")

    async def _close_grvt_position(self, position: Decimal):
        """平 GRVT 仓位"""
        try:
            side = 'sell' if position > 0 else 'buy'
            quantity = abs(position)

            self.logger.info(f"🔄 Closing GRVT position: {side} {quantity}")

            # 使用 GRVT client 的平仓方法
            result = await self.grvt_client.close_position(quantity, side)

            self.logger.info(f"✅ GRVT close order submitted: {result}")

        except Exception as e:
            self.logger.error(f"❌ GRVT close failed: {e}")

    async def _close_lighter_position(self, position: Decimal):
        """平 Lighter 仓位"""
        try:
            is_ask = position > 0  # 多头需要卖出
            quantity = abs(position)

            # 获取当前市场价格
            response = requests.get(
                f"{self.lighter_base_url}/api/v1/orderBook/{self.lighter_market_id}",
                headers={"accept": "application/json"},
                timeout=5
            )

            if response.status_code == 200:
                orderbook = response.json()
                bids = orderbook.get('bids', [])
                asks = orderbook.get('asks', [])

                if bids and asks:
                    best_bid = Decimal(bids[0]['price'])
                    best_ask = Decimal(asks[0]['price'])

                    # 使用 1.5% 滑点确保成交
                    if is_ask:
                        close_price = best_bid * Decimal('0.985')
                    else:
                        close_price = best_ask * Decimal('1.015')
                else:
                    close_price = Decimal('3000')  # Fallback price
            else:
                close_price = Decimal('3000')  # Fallback price

            raw_quantity = int(quantity * self.base_amount_multiplier)
            raw_price = int(close_price * self.price_multiplier)
            client_order_index = int(time.time() * 1000)

            self.logger.info(f"🔄 Closing Lighter position: {'sell' if is_ask else 'buy'} {quantity} @ {close_price}")

            # 使用 sign_create_order + send_tx 方式
            tx_type, tx_info, tx_hash, error = self.lighter_client.sign_create_order(
                market_index=self.lighter_market_id,
                client_order_index=client_order_index,
                base_amount=raw_quantity,
                price=raw_price,
                is_ask=is_ask,
                order_type=self.lighter_client.ORDER_TYPE_LIMIT,
                time_in_force=self.lighter_client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                reduce_only=False,
                trigger_price=0,
                order_expiry=self.lighter_client.DEFAULT_IOC_EXPIRY,
            )

            if error:
                raise Exception(f"Sign error: {error}")

            await self.lighter_client.send_tx(tx_type=tx_type, tx_info=tx_info)

            self.logger.info(f"✅ Lighter close order submitted: tx_hash={tx_hash}")

        except Exception as e:
            self.logger.error(f"❌ Lighter close failed: {e}")

    def log_to_csv_file(self, snapshot: PositionSnapshot):
        """记录到CSV文件"""
        if not self.log_to_csv:
            return

        try:
            os.makedirs("logs", exist_ok=True)
            csv_filename = f"logs/position_monitor_{self.maker_exchange}_{self.ticker}.csv"

            file_exists = os.path.isfile(csv_filename)

            with open(csv_filename, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=[
                    'timestamp', 'datetime', 'maker_exchange', 'maker_position',
                    'lighter_position', 'net_position', 'is_naked_long',
                    'is_naked_short', 'is_balanced', 'balance_threshold'
                ])

                if not file_exists:
                    writer.writeheader()

                writer.writerow({
                    'timestamp': snapshot.timestamp,
                    'datetime': snapshot.datetime_str,
                    'maker_exchange': snapshot.maker_exchange,
                    'maker_position': str(snapshot.maker_position),
                    'lighter_position': str(snapshot.lighter_position),
                    'net_position': str(snapshot.net_position),
                    'is_naked_long': snapshot.is_naked_long,
                    'is_naked_short': snapshot.is_naked_short,
                    'is_balanced': snapshot.is_balanced,
                    'balance_threshold': str(snapshot.balance_threshold)
                })

        except Exception as e:
            self.logger.error(f"❌ Failed to log to CSV: {e}")

    async def monitor_loop(self):
        """主监控循环"""
        self.logger.info("=" * 60)
        self.logger.info(f"🔍 Position Balance Monitor Started")
        self.logger.info(f"   Maker Exchange: {self.maker_exchange.upper()}")
        self.logger.info(f"   Ticker: {self.ticker}")
        self.logger.info(f"   Check Interval: {self.check_interval}s")
        self.logger.info(f"   Balance Threshold: {self.balance_threshold}")
        self.logger.info(f"   Auto-Close: {self.auto_close}")
        self.logger.info(f"   Webhook Alerts: {self.alert_webhook_url is not None}")
        self.logger.info("=" * 60)

        while not self.stop_flag:
            try:
                self.check_count += 1
                current_time = time.time()

                # 获取持仓
                maker_pos, lighter_pos = await self.get_positions()

                # 检查平衡
                balance_info = self.check_balance(maker_pos, lighter_pos)

                # 创建快照
                snapshot = PositionSnapshot(
                    timestamp=current_time,
                    datetime_str=datetime.fromtimestamp(current_time, tz=pytz.UTC).isoformat(),
                    maker_exchange=self.maker_exchange,
                    maker_position=maker_pos,
                    lighter_position=lighter_pos,
                    net_position=balance_info['net_position'],
                    is_naked_long=balance_info['is_naked_long'],
                    is_naked_short=balance_info['is_naked_short'],
                    is_balanced=balance_info['is_balanced'],
                    balance_threshold=self.balance_threshold
                )

                # 保存快照
                self.last_snapshots.append(snapshot)
                if len(self.last_snapshots) > 1000:  # 保留最近1000条
                    self.last_snapshots = self.last_snapshots[-1000:]

                # 记录到CSV
                self.log_to_csv_file(snapshot)

                # 定期日志（每分钟）
                if self.check_count % (60 // self.check_interval) == 0:
                    self.logger.info(
                        f"📊 [{snapshot.datetime_str}] "
                        f"{self.maker_exchange.upper()}={maker_pos}, "
                        f"Lighter={lighter_pos}, "
                        f"Net={balance_info['net_position']} "
                        f"{'✅' if balance_info['is_balanced'] else '⚠️'}"
                    )

                # 处理不平衡
                if not balance_info['is_balanced']:
                    self.imbalance_count += 1

                    if balance_info['is_naked_long'] or balance_info['is_naked_short']:
                        self.naked_position_count += 1

                    # 发送警报
                    await self.send_alert(snapshot, balance_info)

                # 等待下一次检查
                await asyncio.sleep(self.check_interval)

            except Exception as e:
                self.logger.error(f"❌ Error in monitor loop: {e}")
                import traceback
                self.logger.error(traceback.format_exc())
                await asyncio.sleep(self.check_interval)

    async def run(self):
        """运行监控机器人"""
        self.setup_signal_handlers()

        try:
            # 初始化客户端
            if self.maker_exchange == 'edgex':
                if not await self.initialize_edgex_client():
                    return
            elif self.maker_exchange == 'grvt':
                if not await self.initialize_grvt_client():
                    return
            else:
                self.logger.error(f"❌ Unknown maker exchange: {self.maker_exchange}")
                return

            if not self.initialize_lighter_client():
                return

            # 运行监控循环
            await self.monitor_loop()

        except KeyboardInterrupt:
            self.logger.info("Received keyboard interrupt")
        except asyncio.CancelledError:
            self.logger.info("Task cancelled")
        except Exception as e:
            self.logger.error(f"Fatal error: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
        finally:
            await self.cleanup()

    async def cleanup(self):
        """清理资源"""
        self.logger.info("=" * 60)
        self.logger.info("📊 Monitor Statistics:")
        self.logger.info(f"   Total Checks: {self.check_count}")
        self.logger.info(f"   Imbalance Events: {self.imbalance_count}")
        self.logger.info(f"   Naked Position Events: {self.naked_position_count}")
        self.logger.info(f"   Auto-Close Actions: {self.auto_close_count}")
        self.logger.info("=" * 60)

        try:
            if self.edgex_client:
                await self.edgex_client.close()
        except Exception as e:
            self.logger.error(f"Error closing EdgeX client: {e}")

        try:
            if self.grvt_client:
                await self.grvt_client.disconnect()
        except Exception as e:
            self.logger.error(f"Error closing GRVT client: {e}")

        self.logger.info("Monitor shutdown complete")


async def main():
    """主函数"""
    # 从环境变量读取配置
    maker_exchange = os.getenv('MONITOR_MAKER_EXCHANGE', 'edgex')
    ticker = os.getenv('MONITOR_TICKER', 'SOL')
    check_interval = int(os.getenv('MONITOR_CHECK_INTERVAL', '10'))
    balance_threshold = Decimal(os.getenv('MONITOR_BALANCE_THRESHOLD', '0.05'))
    alert_webhook_url = os.getenv('ALERT_WEBHOOK_URL')
    auto_close = os.getenv('MONITOR_AUTO_CLOSE', 'false').lower() == 'true'

    monitor = PositionBalanceMonitor(
        maker_exchange=maker_exchange,
        ticker=ticker,
        check_interval=check_interval,
        balance_threshold=balance_threshold,
        alert_webhook_url=alert_webhook_url,
        auto_close=auto_close,
        log_to_csv=True
    )

    await monitor.run()


if __name__ == "__main__":
    asyncio.run(main())
