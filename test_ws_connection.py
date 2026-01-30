#!/usr/bin/env python3
"""
WebSocket 连接测试脚本
使用与主程序相同的方法测试三个交易所的 WebSocket 连接
"""
import asyncio
import json
import logging
import sys
from decimal import Decimal
from datetime import datetime

# 导入必要的模块
from dotenv import load_dotenv
load_dotenv()

from edgex_sdk import WebSocketManager

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

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


class WebSocketTester:
    """WebSocket 连接测试器"""

    def __init__(self):
        self.results = {
            "edgex": {"connected": False, "message": "", "order_book": False},
            "lighter": {"connected": False, "message": "", "order_book": False},
            "grvt": {"connected": False, "message": "", "order_book": False}
        }

    # ==================== EdgeX WebSocket 测试 ====================
    
    async def test_edgex_ws(self) -> bool:
        """测试 EdgeX WebSocket 连接（使用 SDK）"""
        log("=" * 60)
        log("测试 EdgeX WebSocket 连接 (使用 edgex_sdk)")
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
            log(f"Base URL: {base_url}")
            
            # 创建 EdgeX 客户端 (与主程序相同的方式)
            authenticator = Authenticator(
                account_id=account_id,
                private_key=private_key,
                base_url=base_url
            )
            
            api_client = APIClient(authenticator=authenticator)
            ws_manager = WebSocketManager(api_client)
            log("✅ EdgeX WebSocketManager 创建成功")
            
            # 获取合约信息
            contract_info = api_client.get_contract_info("BTCUSD")
            if contract_info and contract_info.get('data'):
                contract_id = contract_info['data'][0].get('contractId')
                log(f"✅ 合约 ID: {contract_id}")
            else:
                contract_id = "10000001"  # 默认 BTCUSD ID
                log(f"⚠️ 无法获取合约ID，使用默认值: {contract_id}", "WARNING")
            
            # 设置回调函数
            order_book_ready = False
            
            def on_depth_message(message):
                nonlocal order_book_ready
                try:
                    if isinstance(message, str):
                        data = json.loads(message)
                    else:
                        data = message
                    
                    msg_type = data.get("type", "")
                    log(f"📬 [EdgeX WS] 收到消息类型: {msg_type}")
                    
                    if msg_type == "quote-event":
                        content = data.get("content", {})
                        channel = content.get("channel", "")
                        if "depth" in channel:
                            order_book_ready = True
                            self.results["edgex"]["order_book"] = True
                    
                except Exception as e:
                    log(f"处理深度消息失败: {e}", "WARNING")
            
            # 注册深度消息处理
            public_client = ws_manager.get_public_client()
            public_client.on_message("depth", on_depth_message)
            log("✅ 深度消息处理函数已注册")
            
            # 连接 WebSocket (与 websocket_manager.py 中的方式相同)
            try:
                ws_manager.connect_public()
                ws_manager.connect_private()
                log("✅ EdgeX WebSocket 连接已建立")
            except Exception as e:
                error_msg = str(e)
                if "SSL" in error_msg or "EOF" in error_msg:
                    log(f"⚠️ EdgeX WebSocket SSL 错误: {error_msg}", "WARNING")
                    self.results["edgex"]["message"] = f"SSL错误: {error_msg[:60]}"
                else:
                    log(f"⚠️ EdgeX WebSocket 连接失败: {e}", "WARNING")
                    self.results["edgex"]["message"] = error_msg[:60]
                return False
            
            # 订阅深度频道
            try:
                public_client.subscribe(f"depth.{contract_id}.15")
                log(f"✅ 已订阅深度频道: depth.{contract_id}.15")
            except Exception as e:
                log(f"订阅失败: {e}", "WARNING")
            
            # 等待数据
            log("等待订单簿数据...")
            await asyncio.sleep(3)
            
            if order_book_ready:
                self.results["edgex"]["connected"] = True
                self.results["edgex"]["message"] = "订单簿数据接收成功"
                log("✅ EdgeX 订单簿数据接收成功!", "SUCCESS")
            else:
                # 检查连接状态
                self.results["edgex"]["connected"] = True
                self.results["edgex"]["message"] = "WebSocket 已连接，订阅已发送"
                log("⚠️ EdgeX WebSocket 已连接，等待数据中...", "WARNING")
            
            return self.results["edgex"]["connected"]
                
        except Exception as e:
            log(f"❌ EdgeX 测试异常: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            self.results["edgex"]["message"] = str(e)
            return False

    # ==================== Lighter WebSocket 测试 ====================
    
    async def test_lighter_ws(self) -> bool:
        """测试 Lighter WebSocket 连接（使用 websocket_manager.py 中的方法）"""
        log("=" * 60)
        log("测试 Lighter WebSocket 连接 (使用 websocket_manager.py 方法)")
        log("=" * 60)
        
        try:
            import os
            import websockets
            
            # 加载配置
            private_key = os.getenv("LIGHTER_API_PRIVATE_KEY")
            api_key_index = os.getenv("LIGHTER_API_KEY_INDEX")
            
            if not private_key:
                log("缺少 LIGHTER 配置", "ERROR")
                return False
            
            log(f"配置: API Key Index = {api_key_index or '默认(3)'}")
            
            # 初始化 Lighter SDK 客户端
            try:
                from lighter import SignerClient, ApiClient, Configuration
                
                config = Configuration.get_default()
                signer_client = SignerClient(
                    host=config.host,
                    api_key_index=int(api_key_index) if api_key_index else 3,
                    api_private_keys=[private_key]
                )
                log("✅ Lighter SignerClient 创建成功")
                
                # 获取账户索引
                account_index = self._get_lighter_account_index(signer_client)
                if account_index:
                    log(f"账户索引: {account_index}")
                else:
                    account_index = 692775  # 默认值
                    log(f"使用默认账户索引: {account_index}", "WARNING")
                
            except ImportError as e:
                log(f"⚠️ Lighter SDK 未安装: {e}", "WARNING")
                signer_client = None
                account_index = 692775
            except Exception as e:
                log(f"⚠️ SDK 初始化失败: {e}，继续测试", "WARNING")
                signer_client = None
                account_index = 692775
            
            # 使用 websocket_manager.py 中相同的方法
            # URL 带 readonly=true
            url = "wss://mainnet.zklighter.elliot.ai/stream?readonly=true"
            market_id = 1  # BTC 市场
            
            log(f"连接 WebSocket: {url}")
            log(f"Market ID: {market_id}, Account Index: {account_index}")
            
            order_book_ready = False
            
            async with websockets.connect(url) as ws:
                log("✅ Lighter WebSocket 连接成功!", "SUCCESS")
                
                # 订阅订单簿 (与 websocket_manager.py 相同)
                subscribe_msg = {
                    "type": "subscribe",
                    "channel": f"order_book/{market_id}"
                }
                await ws.send(json.dumps(subscribe_msg))
                log(f"📤 订阅请求已发送: {subscribe_msg}")
                
                # 处理消息循环 (与 websocket_manager.py 类似)
                async def message_handler():
                    nonlocal order_book_ready
                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1)
                            data = json.loads(msg)
                            msg_type = data.get("type", "UNKNOWN")
                            
                            log(f"📬 [Lighter WS] 收到消息类型: {msg_type}")
                            
                            if msg_type == "connected":
                                log("✅ Lighter 连接确认")
                                
                            elif msg_type == "subscribed/order_book":
                                order_book = data.get("order_book", {})
                                bids_count = len(order_book.get("bids", []))
                                asks_count = len(order_book.get("asks", []))
                                log(f"✅ 订单簿快照: {bids_count} bids, {asks_count} asks")
                                order_book_ready = True
                                self.results["lighter"]["order_book"] = True
                                
                            elif msg_type == "update/order_book":
                                if not order_book_ready:
                                    continue
                                # 更新订单簿，只在有序列间隙时重新订阅
                                order_book = data.get("order_book", {})
                                if order_book and "offset" in order_book:
                                    order_book_ready = True
                            
                            elif msg_type == "ping":
                                await ws.send(json.dumps({"type": "pong"}))
                                
                        except asyncio.TimeoutError:
                            continue
                        except websockets.exceptions.ConnectionClosed:
                            log("⚠️ Lighter WebSocket 连接关闭", "WARNING")
                            break
                        except Exception as e:
                            log(f"处理消息失败: {e}", "WARNING")
                            break
                
                # 启动消息处理
                handler_task = asyncio.create_task(message_handler())
                
                # 等待数据
                await asyncio.sleep(3)
                
                # 取消任务
                handler_task.cancel()
                try:
                    await handler_task
                except asyncio.CancelledError:
                    pass
            
            self.results["lighter"]["connected"] = True
            if order_book_ready:
                self.results["lighter"]["message"] = "订单簿订阅成功"
            else:
                self.results["lighter"]["message"] = "WebSocket 已连接"
            
            return True
                    
        except Exception as e:
            error_msg = str(e)
            log(f"❌ Lighter WebSocket 测试失败: {e}", "ERROR")
            self.results["lighter"]["message"] = error_msg
            return False

    def _get_lighter_account_index(self, client) -> int:
        """获取 Lighter 账户索引"""
        try:
            from solders.keypair import Keypair
            from lighter import ApiClient
            
            private_key_str = os.getenv("LIGHTER_API_PRIVATE_KEY")
            if not private_key_str:
                return None
            
            # 从私钥推导公钥
            kp = Keypair.from_base58_string(private_key_str)
            pubkey = str(kp.pubkey())
            
            # 调用 API 获取账户
            api_client = ApiClient(client)
            accounts = api_client.get_accounts(l1_address=pubkey)
            
            if accounts and accounts.data:
                return accounts.data[0].account_index
                
        except Exception as e:
            log(f"获取账户索引失败: {e}", "WARNING")
        
        return None

    # ==================== GRVT WebSocket 测试 ====================
    
    async def test_grvt_ws(self) -> bool:
        """测试 GRVT WebSocket 连接"""
        log("=" * 60)
        log("测试 GRVT WebSocket 连接")
        log("=" * 60)
        
        try:
            import os
            import websockets
            
            # 加载配置
            private_key = os.getenv("GRVT_PRIVATE_KEY")
            account_id = os.getenv("GRVT_TRADING_ACCOUNT_ID")
            
            if not private_key or not account_id:
                log("缺少 GRVT 配置", "ERROR")
                return False
            
            log(f"配置: Account ID = {account_id}")
            
            # GRVT WebSocket URL
            ws_url = "wss://api.grvt.io/ws/v2"
            log(f"连接 WebSocket: {ws_url}")
            
            async with websockets.connect(ws_url, timeout=10) as ws:
                log("✅ GRVT WebSocket 连接成功!", "SUCCESS")
                
                # 发送订阅请求
                auth_msg = {
                    "op": "subscribe",
                    "args": ["trade.BTC_USDT_PERP", "orderbook.BTC_USDT_PERP"]
                }
                await ws.send(json.dumps(auth_msg))
                log("📤 订阅请求已发送")
                
                # 等待响应
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(msg)
                    log(f"📬 收到消息: {str(data)[:100]}...", "SUCCESS")
                    
                    if "trade" in str(data) or "orderbook" in str(data) or "subscribed" in str(data):
                        self.results["grvt"]["order_book"] = True
                        self.results["grvt"]["connected"] = True
                        self.results["grvt"]["message"] = "订阅成功"
                        log("✅ GRVT 订阅成功!", "SUCCESS")
                    else:
                        self.results["grvt"]["connected"] = True
                        self.results["grvt"]["message"] = f"收到: {str(data)[:50]}"
                    
                    return True
                    
                except asyncio.TimeoutError:
                    log("⚠️ 等待响应超时", "WARNING")
                    self.results["grvt"]["connected"] = True
                    self.results["grvt"]["message"] = "WebSocket 已连接"
                    return True
                    
        except Exception as e:
            error_msg = str(e)
            log(f"❌ GRVT WebSocket 测试失败: {e}", "ERROR")
            self.results["grvt"]["message"] = error_msg
            return False

    # ==================== 主测试函数 ====================
    
    async def run_all_tests(self):
        """运行所有 WebSocket 测试"""
        print("\n" + "=" * 70)
        print("🚀 WebSocket 连接测试开始 (使用与主程序相同的方法)")
        print("=" * 70 + "\n")
        
        # 测试所有交易所
        await self.test_edgex_ws()
        print()
        await self.test_lighter_ws()
        print()
        await self.test_grvt_ws()
        
        # 显示结果汇总
        self.print_summary()
        
        return self.results

    def print_summary(self):
        """打印测试结果汇总"""
        print("\n" + "=" * 70)
        print("📊 WebSocket 测试结果汇总")
        print("=" * 70)
        
        for exchange, result in self.results.items():
            status = "✅ 成功" if result["connected"] else "❌ 失败"
            order_book = "✅" if result["order_book"] else "⚠️"
            
            print(f"\n{exchange.upper()}:")
            print(f"  连接状态: {status}")
            print(f"  订单簿: {order_book}")
            print(f"  消息: {result['message']}")
        
        # 总体结果
        connected_count = sum(1 for r in self.results.values() if r["connected"])
        print("\n" + "=" * 70)
        print(f"📈 测试完成: {connected_count}/3 个交易所 WebSocket 连接成功")
        print("=" * 70 + "\n")


async def main():
    """主函数"""
    try:
        tester = WebSocketTester()
        results = await tester.run_all_tests()
        
        # 返回退出码
        if all(r["connected"] for r in results.values()):
            print("✅ 所有 WebSocket 测试通过!")
            return 0
        else:
            print("⚠️ 部分 WebSocket 测试失败，请检查日志")
            return 1
            
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
