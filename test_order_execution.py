#!/usr/bin/env python3
"""
订单实际发送测试脚本
测试订单是否能成功发送到交易所并检查订单状态
"""
import asyncio
import json
import sys
from decimal import Decimal
from datetime import datetime

# 导入必要的模块
from dotenv import load_dotenv
load_dotenv()

# 颜色日志输出
def log(message, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    colors = {
        "INFO": "\033[94m",      # 蓝色
        "SUCCESS": "\033[92m",   # 绿色
        "WARNING": "\033[93m",   # 黄色
        "ERROR": "\033[91m",     # 红色
        "RESET": "\033[0m"
    }
    color = colors.get(level, colors["INFO"])
    print(f"[{timestamp}] [{level}] {message}{colors['RESET']}")


class OrderTester:
    """订单发送测试器"""

    # ==================== EdgeX 订单测试 ====================
    
    async def test_edgex_order(self) -> bool:
        """测试 EdgeX 订单发送"""
        log("=" * 60)
        log("测试 EdgeX 订单发送")
        log("=" * 60)
        
        try:
            import os
            from edgex_sdk import Authenticator, APIClient
            
            # 加载配置
            private_key = os.getenv("EDGEX_STARK_PRIVATE_KEY")
            account_id = os.getenv("EDGEX_ACCOUNT_ID")
            base_url = os.getenv("EDGEX_BASE_URL", "https://pro.edgex.exchange")
            
            if not private_key or not account_id:
                log("缺少 EDGEX 配置", "ERROR")
                return False
            
            log(f"配置: Account ID = {account_id}")
            
            # 创建客户端
            authenticator = Authenticator(
                account_id=account_id,
                private_key=private_key,
                base_url=base_url
            )
            api_client = APIClient(authenticator=authenticator)
            log("✅ EdgeX 客户端创建成功")
            
            # 获取合约信息
            log("获取合约信息...")
            contract_info = api_client.get_contract_info("BTCUSD")
            if contract_info and contract_info.get('data'):
                contract = contract_info['data'][0]
                contract_id = contract.get('contractId')
                tick_size = Decimal(str(contract.get('tickSize', 0.1)))
                min_size = Decimal(str(contract.get('minOrderSize', 0.001)))
                log(f"✅ 合约: {contract.get('contractName')}")
                log(f"   Contract ID: {contract_id}")
                log(f"   Tick Size: {tick_size}")
                log(f"   Min Size: {min_size}")
            else:
                contract_id = "10000001"  # 默认 BTCUSD
                tick_size = Decimal('0.1')
                min_size = Decimal('0.001')
                log(f"⚠️ 使用默认配置: Contract ID = {contract_id}", "WARNING")
            
            # 获取当前价格
            log("获取当前市场价格...")
            try:
                depth_data = api_client.get_market_depth(contract_id=contract_id, limit=5)
                if depth_data and depth_data.get('data'):
                    bids = depth_data['data'].get('bids', [])
                    asks = depth_data['data'].get('asks', [])
                    if bids:
                        best_bid = Decimal(str(bids[0].get('price', 0)))
                        log(f"   Best Bid: {best_bid}")
                    if asks:
                        best_ask = Decimal(str(asks[0].get('price', 0)))
                        log(f"   Best Ask: {best_ask}")
                else:
                    log("⚠️ 无法获取深度数据", "WARNING")
                    best_bid = Decimal('82000')
                    best_ask = Decimal('82001')
            except Exception as e:
                log(f"获取深度数据失败: {e}", "WARNING")
                best_bid = Decimal('82000')
                best_ask = Decimal('82001')
            
            # 计算订单价格 (POST-ONLY: 买方挂单)
            order_quantity = Decimal('0.001')  # 最小测试数量
            order_price = best_ask - tick_size  # 低于最佳卖价，确保是 maker 订单
            
            client_order_id = str(int(asyncio.get_event_loop().time() * 1000))
            
            log(f"\n准备发送订单:")
            log(f"   方向: BUY")
            log(f"   数量: {order_quantity}")
            log(f"   价格: {order_price}")
            log(f"   Client Order ID: {client_order_id}")
            log(f"   Post-Only: True")
            
            # 发送订单
            log("\n发送订单...")
            try:
                from edgex_sdk.models import OrderSide
                order_result = api_client.create_limit_order(
                    contract_id=contract_id,
                    size=str(order_quantity),
                    price=str(order_price),
                    side=OrderSide.BUY,
                    post_only=True,
                    client_order_id=client_order_id
                )
                
                log(f"API 返回结果: {json.dumps(order_result, indent=2, default=str)}")
                
                if order_result and 'data' in order_result:
                    order_id = order_result['data'].get('orderId')
                    if order_id:
                        log(f"✅ 订单发送成功!", "SUCCESS")
                        log(f"   Order ID: {order_id}")
                        
                        # 检查订单状态
                        log("\n检查订单状态...")
                        await asyncio.sleep(1)
                        
                        order_status = api_client.get_order_by_id(order_id)
                        if order_status and 'data' in order_status:
                            status = order_status['data'].get('status', 'UNKNOWN')
                            log(f"   订单状态: {status}")
                            
                            if status in ['FILLED', 'PARTIAL_FILLED']:
                                log("✅ 订单已成交!", "SUCCESS")
                                return True
                            elif status in ['NEW', 'OPEN', 'PENDING']:
                                log(f"⚠️ 订单未成交 (状态: {status})", "WARNING")
                                log("   尝试取消订单...")
                                
                                # 取消订单
                                try:
                                    cancel_result = api_client.cancel_order(
                                        order_id=order_id,
                                        client_order_id=client_order_id
                                    )
                                    log(f"取消结果: {cancel_result}")
                                    return True
                                except Exception as e:
                                    log(f"取消订单失败: {e}", "WARNING")
                                return True
                            else:
                                log(f"⚠️ 未知订单状态: {status}", "WARNING")
                                return True
                        else:
                            log("无法获取订单状态", "WARNING")
                            return True
                    else:
                        log("❌ 响应中没有 Order ID", "ERROR")
                        return False
                else:
                    log(f"❌ 订单发送失败: {order_result}", "ERROR")
                    return False
                    
            except Exception as e:
                log(f"❌ 发送订单异常: {e}", "ERROR")
                import traceback
                traceback.print_exc()
                return False
                
        except Exception as e:
            log(f"❌ EdgeX 测试异常: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            return False

    # ==================== Lighter 订单测试 ====================
    
    async def test_lighter_order(self) -> bool:
        """测试 Lighter 订单发送"""
        log("=" * 60)
        log("测试 Lighter 订单发送")
        log("=" * 60)
        
        try:
            import os
            from lighter import SignerClient, ApiClient, Configuration, OrderApi
            from solders.keypair import Keypair
            from solders.pubkey import Pubkey
            
            # 加载配置
            private_key = os.getenv("LIGHTER_API_PRIVATE_KEY")
            api_key_index = os.getenv("LIGHTER_API_KEY_INDEX")
            
            if not private_key:
                log("缺少 LIGHTER 配置", "ERROR")
                return False
            
            log(f"配置: API Key Index = {api_key_index or '3'}")
            
            # 初始化 SDK
            config = Configuration.get_default()
            signer_client = SignerClient(
                host=config.host,
                api_key_index=int(api_key_index) if api_key_index else 3,
                api_private_keys=[private_key]
            )
            log("✅ Lighter SignerClient 创建成功")
            
            # 获取账户索引
            kp = Keypair.from_base58_string(private_key)
            pubkey = str(kp.pubkey())
            
            api_client = ApiClient(signer_client)
            accounts = api_client.get_accounts(l1_address=pubkey)
            
            if not accounts or not accounts.data:
                log("❌ 无法获取账户信息", "ERROR")
                return False
            
            account_index = accounts.data[0].account_index
            log(f"账户索引: {account_index}")
            
            # 获取市场信息
            order_books = await OrderApi(api_client).order_books()
            if not order_books or not order_books.order_books:
                log("❌ 无法获取市场信息", "ERROR")
                return False
            
            # 找到 BTC 市场
            btc_market = None
            for market in order_books.order_books:
                if market.symbol == "BTC":
                    btc_market = market
                    break
            
            if not btc_market:
                log("❌ 未找到 BTC 市场", "ERROR")
                return False
            
            market_id = btc_market.market_id
            tick_size = Decimal('0.1')
            log(f"✅ BTC 市场 ID: {market_id}")
            log(f"   Tick Size: {tick_size}")
            log(f"   Min Size: {btc_market.min_base_amount}")
            
            # 获取当前价格
            order_book_details = await OrderApi(api_client).order_book_details(market_id=market_id)
            if order_book_details and order_book_details.order_book_details:
                details = order_book_details.order_book_details[0]
                best_bid = Decimal(str(details.mid_price)) - Decimal('0.5')
                best_ask = Decimal(str(details.mid_price)) + Decimal('0.5')
                log(f"   预估价格: {details.mid_price}")
            else:
                best_ask = Decimal('82000')
                best_bid = Decimal('82000')
            
            # 计算订单价格
            order_quantity = Decimal('0.001')
            order_price = best_ask - tick_size  # POST-ONLY: 低于最佳卖价
            
            client_order_id = int(asyncio.get_event_loop().time() * 1000)
            
            log(f"\n准备发送订单:")
            log(f"   方向: BUY")
            log(f"   数量: {order_quantity}")
            log(f"   价格: {order_price}")
            
            # 发送订单
            log("\n发送订单...")
            
            try:
                # 签名订单
                order_result, error = await OrderApi(api_client).create_order(
                    market_id=market_id,
                    order_type=0,  # LIMIT
                    side=0,  # BUY
                    size=str(order_quantity),
                    price=str(order_price),
                    client_order_id=client_order_id,
                    post_only=True
                )
                
                if error:
                    log(f"❌ 创建订单失败: {error}", "ERROR")
                    return False
                
                if order_result:
                    order_id = order_result.order_id
                    log(f"✅ 订单发送成功!", "SUCCESS")
                    log(f"   Order ID: {order_id}")
                    
                    # 等待并检查订单状态
                    log("\n检查订单状态 (等待 2 秒)...")
                    await asyncio.sleep(2)
                    
                    # 查询订单
                    account_orders, error = await OrderApi(api_client).account_orders(
                        account_index=account_index,
                        market_id=market_id,
                        auth=None  # 使用公开数据
                    )
                    
                    if account_orders and account_orders.orders:
                        for order in account_orders.orders:
                            if order.client_order_id == client_order_id:
                                log(f"   订单状态: {order.status}")
                                if order.status == "FILLED":
                                    log("✅ 订单已成交!", "SUCCESS")
                                elif order.status == "OPEN":
                                    log("⚠️ 订单未成交，尝试取消...")
                                    # 取消订单
                                    await OrderApi(api_client).cancel_order(
                                        order_index=order.order_index,
                                        market_id=market_id
                                    )
                                    log("✅ 取消请求已发送")
                                return True
                    
                    log("   未找到订单或订单已取消")
                    return True
                else:
                    log("❌ 无返回数据", "ERROR")
                    return False
                    
            except Exception as e:
                log(f"❌ 发送订单异常: {e}", "ERROR")
                import traceback
                traceback.print_exc()
                return False
                
        except Exception as e:
            log(f"❌ Lighter 测试异常: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            return False

    # ==================== 主测试函数 ====================
    
    async def run_all_tests(self):
        """运行所有订单测试"""
        print("\n" + "=" * 70)
        print("🚀 订单发送测试开始")
        print("=" * 70 + "\n")
        
        results = {}
        
        # 测试 EdgeX
        print("\n" + "-" * 60)
        input("按回车开始 EdgeX 订单测试 (y/n)? ")
        if input().lower() in ['y', '']:
            results["edgex"] = await self.test_edgex_order()
        else:
            results["edgex"] = None
        
        # 测试 Lighter
        print("\n" + "-" * 60)
        input("按回车开始 Lighter 订单测试 (y/n)? ")
        if input().lower() in ['y', '']:
            results["lighter"] = await self.test_lighter_order()
        else:
            results["lighter"] = None
        
        # 显示结果
        self.print_summary(results)
        
        return results

    def print_summary(self, results):
        """打印测试结果"""
        print("\n" + "=" * 70)
        print("📊 订单测试结果汇总")
        print("=" * 70)
        
        for exchange, result in results.items():
            if result is None:
                status = "⏭️ 跳过"
            elif result:
                status = "✅ 成功"
            else:
                status = "❌ 失败"
            
            print(f"{exchange.upper()}: {status}")
        
        print("=" * 70 + "\n")


async def main():
    """主函数"""
    try:
        tester = OrderTester()
        await tester.run_all_tests()
        return 0
            
    except KeyboardInterrupt:
        print("\n⚠️ 用户中断测试")
        return 130
    except Exception as e:
        log(f"测试异常: {e}", "ERROR")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

