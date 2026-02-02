"""Trade statistics module for recording order statistics."""
import csv
import json
import os
import logging
from decimal import Decimal
from datetime import datetime
import pytz
from typing import Optional


class TradeStatistics:
    """Trade statistics manager for recording order statistics by exchange."""
    
    def __init__(self, robot_id: str, ticker: str, logger: logging.Logger):
        """Initialize trade statistics manager.
        
        Args:
            robot_id: Robot identifier
            ticker: Trading pair symbol (e.g., 'BTC')
            logger: Logger instance
        """
        self.robot_id = robot_id
        self.ticker = ticker
        self.logger = logger
        
        # Create logs directory
        os.makedirs("logs/statistics", exist_ok=True)
        
        # Separate ledger files for each exchange
        self.nado_ledger_file = f"logs/statistics/{robot_id}_nado_ledger.csv"
        self.lighter_ledger_file = f"logs/statistics/{robot_id}_lighter_ledger.csv"
        self.grvt_ledger_file = f"logs/statistics/{robot_id}_grvt_ledger.csv"
        self.edgex_ledger_file = f"logs/statistics/{robot_id}_edgex_ledger.csv"
        
        # Exchange fee rates (as Decimal percentages)
        # Nado taker: 0.008%
        # Lighter maker: 0%
        # GRVT taker: -0.001% (negative = rebate)
        # EdgeX taker: 0.015%
        self.fee_rates = {
            'nado': {'taker': Decimal('0.00008'), 'maker': Decimal('0')},
            'lighter': {'taker': Decimal('0'), 'maker': Decimal('0')},
            'grvt': {'taker': Decimal('-0.00001'), 'maker': Decimal('0')},  # Negative = rebate
            'edgex': {'taker': Decimal('0.00015'), 'maker': Decimal('0')}
        }
        
        # Initialize CSV files
        self._initialize_ledger_file(self.nado_ledger_file, "Nado")
        self._initialize_ledger_file(self.lighter_ledger_file, "Lighter")
        self._initialize_ledger_file(self.grvt_ledger_file, "GRVT")
        self._initialize_ledger_file(self.edgex_ledger_file, "EdgeX")
    
    def _initialize_ledger_file(self, filename: str, exchange: str):
        """Initialize ledger CSV file with headers if it doesn't exist.
        
        Args:
            filename: CSV file path
            exchange: Exchange name
        """
        file_exists = os.path.exists(filename)
        
        if not file_exists:
            try:
                with open(filename, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        'timestamp',
                        'datetime',
                        'exchange',
                        'ticker',
                        'side',
                        'quantity',
                        'price',
                        'value',
                        'order_type',
                        'fee_rate',
                        'fee_amount',
                        'order_id'
                    ])
            except Exception as e:
                self.logger.error(f"Failed to initialize {exchange} ledger file: {e}")
    
    def record_order(self, exchange: str, side: str, quantity: Decimal, 
                     price: Decimal, order_id: Optional[str] = None,
                     order_type: str = 'taker'):
        """Record an order to the statistics ledger.
        
        Args:
            exchange: Exchange name ('Nado', 'Lighter', 'GRVT', or 'EdgeX')
            side: Order side ('buy' or 'sell')
            quantity: Order quantity
            price: Order price
            order_id: Optional order ID
            order_type: Order type ('taker' or 'maker'), default 'taker'
        """
        try:
            # Calculate value (quantity * price)
            value = quantity * price
            
            # Get fee rate based on exchange and order type
            exchange_lower = exchange.lower()
            order_type_lower = order_type.lower()
            
            if exchange_lower in self.fee_rates:
                fee_rate = self.fee_rates[exchange_lower].get(order_type_lower, Decimal('0'))
            else:
                fee_rate = Decimal('0')
                self.logger.warning(f"Unknown exchange: {exchange}, using 0% fee rate")
            
            # Calculate fee amount (value * fee_rate)
            # Note: Negative fee_rate means rebate (e.g., GRVT)
            fee_amount = value * fee_rate
            
            # Get timestamp
            beijing_tz = pytz.timezone('Asia/Shanghai')
            now = datetime.now(beijing_tz)
            timestamp = now.timestamp()
            datetime_str = now.strftime('%Y-%m-%d %H:%M:%S')
            
            # Select ledger file based on exchange
            if exchange_lower == 'nado':
                ledger_file = self.nado_ledger_file
            elif exchange_lower == 'lighter':
                ledger_file = self.lighter_ledger_file
            elif exchange_lower == 'grvt':
                ledger_file = self.grvt_ledger_file
            elif exchange_lower == 'edgex':
                ledger_file = self.edgex_ledger_file
            else:
                self.logger.warning(f"Unknown exchange: {exchange}, using Nado ledger")
                ledger_file = self.nado_ledger_file
            
            # Write to CSV
            with open(ledger_file, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    timestamp,
                    datetime_str,
                    exchange,
                    self.ticker,
                    side.upper(),
                    str(quantity),
                    str(price),
                    str(value),
                    order_type.upper(),
                    str(fee_rate),
                    str(fee_amount),
                    order_id or ''
                ])
            
        except Exception as e:
            self.logger.error(f"Failed to record order statistics: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
    
    def get_statistics(self, exchange: Optional[str] = None) -> dict:
        """Get statistics summary.
        
        Args:
            exchange: Optional exchange name to filter by ('Nado', 'Lighter', 'GRVT', or 'EdgeX')
        
        Returns:
            dict: Statistics summary with:
                - total_orders: 总交易次数
                - total_quantity: 总交易量
                - total_value: 总交易价值
                - total_fees: 总手续费（磨损）
                - buy_count: 买入次数
                - sell_count: 卖出次数
        """
        stats = {
            'nado': {
                'total_orders': 0,           # 总交易次数
                'total_quantity': Decimal('0'),  # 总交易量
                'total_value': Decimal('0'),     # 总交易价值
                'total_fees': Decimal('0'),      # 总手续费（磨损）
                'buy_count': 0,
                'sell_count': 0
            },
            'lighter': {
                'total_orders': 0,
                'total_quantity': Decimal('0'),
                'total_value': Decimal('0'),
                'total_fees': Decimal('0'),
                'buy_count': 0,
                'sell_count': 0
            },
            'grvt': {
                'total_orders': 0,
                'total_quantity': Decimal('0'),
                'total_value': Decimal('0'),
                'total_fees': Decimal('0'),
                'buy_count': 0,
                'sell_count': 0
            },
            'edgex': {
                'total_orders': 0,
                'total_quantity': Decimal('0'),
                'total_value': Decimal('0'),
                'total_fees': Decimal('0'),
                'buy_count': 0,
                'sell_count': 0
            }
        }
        
        try:
            # Read Nado ledger
            if os.path.exists(self.nado_ledger_file):
                with open(self.nado_ledger_file, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if row.get('exchange', '').lower() == 'nado':
                            stats['nado']['total_orders'] += 1
                            stats['nado']['total_quantity'] += Decimal(row.get('quantity', '0'))
                            stats['nado']['total_value'] += Decimal(row.get('value', '0'))
                            stats['nado']['total_fees'] += Decimal(row.get('fee_amount', '0'))
                            if row.get('side', '').upper() == 'BUY':
                                stats['nado']['buy_count'] += 1
                            elif row.get('side', '').upper() == 'SELL':
                                stats['nado']['sell_count'] += 1
            
            # Read Lighter ledger
            if os.path.exists(self.lighter_ledger_file):
                with open(self.lighter_ledger_file, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if row.get('exchange', '').lower() == 'lighter':
                            stats['lighter']['total_orders'] += 1
                            stats['lighter']['total_quantity'] += Decimal(row.get('quantity', '0'))
                            stats['lighter']['total_value'] += Decimal(row.get('value', '0'))
                            stats['lighter']['total_fees'] += Decimal(row.get('fee_amount', '0'))
                            if row.get('side', '').upper() == 'BUY':
                                stats['lighter']['buy_count'] += 1
                            elif row.get('side', '').upper() == 'SELL':
                                stats['lighter']['sell_count'] += 1
            
            # Read GRVT ledger
            if os.path.exists(self.grvt_ledger_file):
                with open(self.grvt_ledger_file, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if row.get('exchange', '').lower() == 'grvt':
                            stats['grvt']['total_orders'] += 1
                            stats['grvt']['total_quantity'] += Decimal(row.get('quantity', '0'))
                            stats['grvt']['total_value'] += Decimal(row.get('value', '0'))
                            stats['grvt']['total_fees'] += Decimal(row.get('fee_amount', '0'))
                            if row.get('side', '').upper() == 'BUY':
                                stats['grvt']['buy_count'] += 1
                            elif row.get('side', '').upper() == 'SELL':
                                stats['grvt']['sell_count'] += 1
            
            # Read EdgeX ledger
            if os.path.exists(self.edgex_ledger_file):
                with open(self.edgex_ledger_file, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if row.get('exchange', '').lower() == 'edgex':
                            stats['edgex']['total_orders'] += 1
                            stats['edgex']['total_quantity'] += Decimal(row.get('quantity', '0'))
                            stats['edgex']['total_value'] += Decimal(row.get('value', '0'))
                            stats['edgex']['total_fees'] += Decimal(row.get('fee_amount', '0'))
                            if row.get('side', '').upper() == 'BUY':
                                stats['edgex']['buy_count'] += 1
                            elif row.get('side', '').upper() == 'SELL':
                                stats['edgex']['sell_count'] += 1
            
            # Convert Decimal to float for JSON serialization
            for exchange_name in stats:
                stats[exchange_name]['total_quantity'] = float(stats[exchange_name]['total_quantity'])
                stats[exchange_name]['total_value'] = float(stats[exchange_name]['total_value'])
                stats[exchange_name]['total_fees'] = float(stats[exchange_name]['total_fees'])
            
            # Return filtered stats if exchange specified
            if exchange:
                return stats.get(exchange.lower(), {})
            
            return stats
            
        except Exception as e:
            self.logger.error(f"Failed to get statistics: {e}")
            return stats
