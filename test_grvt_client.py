#!/usr/bin/env python3
"""
GRVT 交易所客户端测试脚本
测试 GRVT 客户端的各项功能
"""

import os
import sys
import asyncio
import time
from decimal import Decimal
from datetime import datetime

# 设置项目根目录路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# 加载 .env 文件
from dotenv import load_dotenv
dotenv_path = os.path.join(PROJECT_ROOT, '.env')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)
    print(f"✓ 已加载环境变量文件: {dotenv_path}")
else:
    print(f"⚠️  未找到 .env 文件，将使用系统环境变量")

# 导入 GRVT 客户端
from exchanges.grvt import GrvtClient
from exchanges.base import OrderResult, OrderInfo


class GRVTTester:
    """GRVT 客户端测试类"""
    
    def __init__(self):
        self.client = None
        self.test_results = []
        self.contract_id = "BTC-USD-PERP"  # 使用 BTC 合约
        self.test_quantity = Decimal("0.002")  # 0.002 BTC 测试
    
    def log(self, message: str, level: str = "INFO"):
        """日志输出"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] [{level}] {message}")
        self.test_results.append(f"[{level}] {message}")
    
    def log_result(self, test_name: str, success: bool, error: str = None):
        """记录测试结果"""
        status = "✅ PASS" if success else "❌ FAIL"
        self.log(f"{status}: {test_name}", "INFO" if success else "ERROR")
        if error:
            self.log(f"   错误: {error}", "ERROR" if not success else "WARNING")
    
    async def setup(self):
        """初始化测试环境"""
        self.log("=" * 60)
        self.log("开始 GRVT 客户端测试")
        self.log("=" * 60)
        
        # 检查环境变量
        required_vars = ['GRVT_TRADING_ACCOUNT_ID', 'GRVT_PRIVATE_KEY', 'GRVT_API_KEY']
        missing_vars = [var for var in required_vars if not os.getenv(var)]
        
        if missing_vars:
            self.log(f"缺少环境变量: {missing_vars}", "ERROR")
            self.log("请在 .env 文件中设置以下变量:", "ERROR")
            for var in required_vars:
                self.log(f"  {var}=your_value", "ERROR")
            
            # 调试信息：检查 .env 文件
            env_path = os.path.join(PROJECT_ROOT, '.env')
            if os.path.exists(env_path):
                self.log(f".env 文件存在: {env_path}", "INFO")
                with open(env_path, 'r') as f:
                    content = f.read()
                    for var in required_vars:
                        if var in content:
                            self.log(f"  {var} 已配置", "INFO")
                        else:
                            self.log(f"  {var} 未配置", "WARNING")
            else:
                self.log(f".env 文件不存在: {env_path}", "ERROR")
            
            return False
        
        self.log(f"环境变量检查通过", "INFO")
        
        # 打印配置信息（脱敏）
        self.log(f"Trading Account ID: {os.getenv('GRVT_TRADING_ACCOUNT_ID')[:10]}...", "INFO")
        self.log(f"Environment: {os.getenv('GRVT_ENVIRONMENT', 'prod')}", "INFO")
        
        # 创建测试配置（使用对象而非 dict，与本地项目保持一致）
        test_config = type(
            "Config",
            (),
            {
                'ticker': 'BTC',
                'tick_size': Decimal('1'),  # BTC 精度
                'quantity': self.test_quantity,
                'contract_id': self.contract_id,
                'direction': 'buy',
                'close_order_side': 'sell'
            },
        )()
        
        try:
            self.client = GrvtClient(test_config)
            self.log("GRVT 客户端初始化成功", "INFO")
            return True
        except Exception as e:
            self.log(f"GRVT 客户端初始化失败: {e}", "ERROR")
            return False
    
    async def test_1_rest_api_connection(self):
        """测试 REST API 连接"""
        self.log("\n" + "-" * 40)
        self.log("测试 1: REST API 连接")
        self.log("-" * 40)
        
        try:
            # 测试获取市场列表
            markets = self.client.rest_client.fetch_markets()
            self.log(f"获取到 {len(markets)} 个市场", "INFO")
            
            # 检查测试合约是否存在
            contract_found = False
            
            # 先打印所有可用的永续合约
            perp_markets = []
            for market in markets:
                if market.get('kind') == 'PERPETUAL':
                    perp_markets.append(market.get('instrument'))
            
            self.log(f"可用的永续合约数量: {len(perp_markets)}", "INFO")
            if perp_markets:
                self.log(f"前10个永续合约: {perp_markets[:10]}", "INFO")
            
            # 尝试查找指定的合约
            for market in markets:
                if market.get('instrument') == self.contract_id:
                    contract_found = True
                    self.log(f"找到测试合约: {market.get('instrument')}", "INFO")
                    self.log(f"  基础资产: {market.get('base')}", "INFO")
                    self.log(f"  报价资产: {market.get('quote')}", "INFO")
                    self.log(f"  合约类型: {market.get('kind')}", "INFO")
                    self.log(f"  最小数量: {market.get('min_size')}", "INFO")
                    self.log(f"  价格精度: {market.get('tick_size')}", "INFO")
                    break
            
            if not contract_found:
                self.log(f"未找到测试合约 {self.contract_id}", "WARNING")
                # 使用 BTC 开头的第一个合约
                btc_markets = [m for m in perp_markets if 'BTC' in m.upper()]
                if btc_markets:
                    self.contract_id = btc_markets[0]
                    self.log(f"自动切换到 BTC 合约: {self.contract_id}", "WARNING")
                else:
                    # 使用第一个永续合约
                    self.contract_id = perp_markets[0] if perp_markets else perp_markets[0]
                    self.log(f"自动切换到: {self.contract_id}", "WARNING")
            
            self.log_result("REST API 连接测试", True)
            return True
            
        except Exception as e:
            self.log_result("REST API 连接测试", False, str(e))
            return False
    
    async def test_2_fetch_bbo_prices(self):
        """测试获取最优买卖价"""
        self.log("\n" + "-" * 40)
        self.log("测试 2: 获取最优买卖价 (BBO)")
        self.log("-" * 40)
        
        try:
            best_bid, best_ask = await self.client.fetch_bbo_prices(self.contract_id)
            
            if best_bid <= 0 or best_ask <= 0:
                self.log_result("BBO 价格获取", False, "价格无效")
                return False
            
            self.log(f"最佳买入价 (Bid): {best_bid}", "INFO")
            self.log(f"最佳卖出价 (Ask): {best_ask}", "INFO")
            
            spread = best_ask - best_bid
            spread_pct = (spread / best_bid) * 100
            self.log(f"买卖价差: {spread} ({spread_pct:.4f}%)", "INFO")
            
            self.log_result("BBO 价格获取", True)
            return True
            
        except Exception as e:
            self.log_result("BBO 价格获取", False, str(e))
            return False
    
    async def test_3_place_limit_order(self):
        """测试下限价单"""
        self.log("\n" + "-" * 40)
        self.log("测试 3: 下限价单 (Post-Only)")
        self.log("-" * 40)
        
        try:
            # 获取当前价格
            best_bid, best_ask = await self.client.fetch_bbo_prices(self.contract_id)
            
            # 下一个限价单（略低于卖一价）
            order_price = best_bid - self.client.config.tick_size
            order_price = self.client.round_to_tick(order_price)
            
            self.log(f"下单价格: {order_price}", "INFO")
            self.log(f"测试数量: {self.test_quantity}", "INFO")
            self.log(f"下单方向: SELL (做空)", "INFO")
            
            # 下单
            order_info = await self.client.place_post_only_order(
                contract_id=self.contract_id,
                quantity=self.test_quantity,
                price=order_price,
                side='sell'
            )
            
            if not order_info:
                self.log_result("下限价单", False, "订单结果为空")
                return False
            
            self.log(f"订单ID: {order_info.order_id}", "INFO")
            self.log(f"订单状态: {order_info.status}", "INFO")
            self.log(f"订单价格: {order_info.price}", "INFO")
            self.log(f"订单数量: {order_info.size}", "INFO")
            self.log(f"成交数量: {order_info.filled_size}", "INFO")
            
            # 保存订单ID用于后续测试
            self.test_order_id = order_info.order_id
            
            self.log_result("下限价单", True)
            return order_info
            
        except Exception as e:
            self.log_result("下限价单", False, str(e))
            return False
    
    async def test_4_get_order_status(self):
        """测试查询订单状态"""
        self.log("\n" + "-" * 40)
        self.log("测试 4: 查询订单状态")
        self.log("-" * 40)
        
        if not hasattr(self, 'test_order_id') or not self.test_order_id:
            self.log("跳过测试（无订单ID）", "WARNING")
            return True
        
        try:
            order_info = await self.client.get_order_info(order_id=self.test_order_id)
            
            if not order_info:
                self.log_result("查询订单状态", False, "订单不存在")
                return False
            
            self.log(f"订单ID: {order_info.order_id}", "INFO")
            self.log(f"订单状态: {order_info.status}", "INFO")
            self.log(f"成交数量: {order_info.filled_size}", "INFO")
            self.log(f"剩余数量: {order_info.remaining_size}", "INFO")
            
            self.log_result("查询订单状态", True)
            return True
            
        except Exception as e:
            self.log_result("查询订单状态", False, str(e))
            return False
    
    async def test_5_cancel_order(self):
        """测试取消订单"""
        self.log("\n" + "-" * 40)
        self.log("测试 5: 取消订单")
        self.log("-" * 40)
        
        if not hasattr(self, 'test_order_id') or not self.test_order_id:
            self.log("跳过测试（无订单ID）", "WARNING")
            return True
        
        try:
            result = await self.client.cancel_order(self.test_order_id)
            
            self.log(f"取消结果: {'成功' if result.success else '失败'}", "INFO")
            if result.error_message:
                self.log(f"错误信息: {result.error_message}", "WARNING")
            
            self.log_result("取消订单", result.success)
            return result.success
            
        except Exception as e:
            self.log_result("取消订单", False, str(e))
            return False
    
    async def test_6_get_active_orders(self):
        """测试获取活跃订单"""
        self.log("\n" + "-" * 40)
        self.log("测试 6: 获取活跃订单列表")
        self.log("-" * 40)
        
        try:
            orders = await self.client.get_active_orders(self.contract_id)
            
            self.log(f"活跃订单数量: {len(orders)}", "INFO")
            
            for i, order in enumerate(orders):
                self.log(f"  订单 {i+1}: ID={order.order_id[:10]}..., "
                        f"状态={order.status}, "
                        f"方向={order.side}, "
                        f"数量={order.size}", "INFO")
            
            self.log_result("获取活跃订单", True)
            return True
            
        except Exception as e:
            self.log_result("获取活跃订单", False, str(e))
            return False
    
    async def test_7_get_positions(self):
        """测试获取持仓"""
        self.log("\n" + "-" * 40)
        self.log("测试 7: 获取账户持仓")
        self.log("-" * 40)
        
        try:
            # 先等待一下让之前的订单结算
            await asyncio.sleep(1)
            
            position = await self.client.get_account_positions()
            
            # 显示净持仓（正数=多头，负数=空头）
            position_str = f"{position}" if position != 0 else "0 (无持仓)"
            self.log(f"当前合约净持仓: {position_str} {self.contract_id.split('_')[0]}", "INFO")
            
            self.log_result("获取账户持仓", True)
            return True
            
        except Exception as e:
            self.log_result("获取账户持仓", False, str(e))
            return False
    
    async def test_8_place_market_order(self):
        """测试下市价单（小额）"""
        self.log("\n" + "-" * 40)
        self.log("测试 8: 下市价单")
        self.log("-" * 40)
        
        try:
            # 使用 0.002 BTC 测试市价单
            tiny_quantity = Decimal("0.002")
            
            self.log(f"测试数量: {tiny_quantity}", "INFO")
            self.log(f"下单方向: BUY", "INFO")
            
            order_info = await self.client.place_market_order(
                contract_id=self.contract_id,
                quantity=tiny_quantity,
                side='buy'
            )
            
            if not order_info:
                self.log_result("下市价单", False, "订单结果为空")
                return False
            
            self.log(f"订单ID: {order_info.order_id}", "INFO")
            self.log(f"订单状态: {order_info.status}", "INFO")
            self.log(f"成交数量: {order_info.filled_size}", "INFO")
            
            self.test_market_order_id = order_info.order_id
            
            self.log_result("下市价单", True)
            return order_info
            
        except Exception as e:
            self.log_result("下市价单", False, str(e))
            return False
    
    async def test_9_websocket_connection(self):
        """测试 WebSocket 连接"""
        self.log("\n" + "-" * 40)
        self.log("测试 9: WebSocket 连接")
        self.log("-" * 40)
        
        try:
            # 设置订单更新回调
            order_updates = []
            
            def order_handler(update):
                order_updates.append(update)
                self.log(f"收到订单更新: {update.get('status', 'unknown')}", "INFO")
            
            self.client.setup_order_update_handler(order_handler)
            
            # 连接 WebSocket
            await self.client.connect()
            
            self.log("WebSocket 连接成功", "INFO")
            
            # 等待一段时间接收更新
            self.log("等待订单更新 (3秒)...", "INFO")
            await asyncio.sleep(3)
            
            # 断开 WebSocket
            await self.client.disconnect()
            self.log("WebSocket 断开成功", "INFO")
            
            self.log_result("WebSocket 连接", True)
            return True
            
        except Exception as e:
            self.log_result("WebSocket 连接", False, str(e))
            return False
    
    async def run_all_tests(self):
        """运行所有测试"""
        # 初始化
        if not await self.setup():
            self.log("测试初始化失败，退出", "ERROR")
            return False
        
        results = {}
        
        # 运行测试
        results['REST API 连接'] = await self.test_1_rest_api_connection()
        results['BBO 价格获取'] = await self.test_2_fetch_bbo_prices()
        results['下限价单'] = await self.test_3_place_limit_order()
        results['查询订单状态'] = await self.test_4_get_order_status()
        results['取消订单'] = await self.test_5_cancel_order()
        results['获取活跃订单'] = await self.test_6_get_active_orders()
        results['获取账户持仓'] = await self.test_7_get_positions()
        results['下市价单'] = await self.test_8_place_market_order()
        
        # WebSocket 测试放在最后（需要清理之前的订单）
        self.log("\n清理测试订单中...", "INFO")
        if hasattr(self, 'test_order_id') and self.test_order_id:
            try:
                await self.client.cancel_order(self.test_order_id)
            except:
                pass
        if hasattr(self, 'test_market_order_id') and self.test_market_order_id:
            try:
                await self.client.cancel_order(self.test_market_order_id)
            except:
                pass
        
        results['WebSocket 连接'] = await self.test_9_websocket_connection()
        
        # 打印测试总结
        self.log("\n" + "=" * 60)
        self.log("测试总结")
        self.log("=" * 60)
        
        passed = 0
        failed = 0
        
        for test_name, success in results.items():
            status = "✅ 通过" if success else "❌ 失败"
            self.log(f"  {test_name}: {status}", "INFO" if success else "ERROR")
            if success:
                passed += 1
            else:
                failed += 1
        
        self.log(f"\n总计: {passed} 通过, {failed} 失败", "INFO")
        
        # 保存测试结果
        with open("grvt_test_results.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(self.test_results))
        
        self.log(f"\n测试结果已保存到: grvt_test_results.txt", "INFO")
        
        return failed == 0


async def main():
    """主函数"""
    tester = GRVTTester()
    success = await tester.run_all_tests()
    
    if success:
        print("\n" + "=" * 60)
        print("🎉 所有测试通过！GRVT 客户端可以正常使用")
        print("=" * 60)
        sys.exit(0)
    else:
        print("\n" + "=" * 60)
        print("⚠️  部分测试失败，请检查错误信息")
        print("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

