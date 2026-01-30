#!/usr/bin/env python3
"""
Lighter 交易所功能测试脚本
测试 REST API 和 WebSocket 连接、数据获取、订单操作
"""

import asyncio
import os
import sys
import json
import time
from decimal import Decimal
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


class LighterTester:
    """Lighter 交易所测试类"""
    
    def __init__(self):
        self.base_url = os.getenv('LIGHTER_BASE_URL', 'https://mainnet.zklighter.elliot.ai')
        self.account_index = int(os.getenv('LIGHTER_ACCOUNT_INDEX', '0'))
        self.api_key_index = int(os.getenv('LIGHTER_API_KEY_INDEX', '0'))
        self.api_key_private_key = os.getenv('API_KEY_PRIVATE_KEY')
        
        self.client = None
        self.api_client = None
        self.order_api = None
        self.test_results = []
        self.passed_count = 0
        self.failed_count = 0
        
        print(f"Configuration:")
        print(f"  Base URL: {self.base_url}")
        print(f"  Account Index: {self.account_index}")
        print(f"  API Key Index: {self.api_key_index}")
        print(f"  Private Key: {'*' * 20}...{self.api_key_private_key[-4:] if self.api_key_private_key else 'NOT SET'}")
    
    def log(self, message: str, level: str = "INFO"):
        """Log message with timestamp"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        prefix = {
            "INFO": "[#00BFFF]INFO[/]",
            "WARNING": "[#FFD700]WARNING[/]",
            "ERROR": "[#FF4500]ERROR[/]",
            "SUCCESS": "[#32CD32]SUCCESS[/]"
        }.get(level, "[INFO]")
        
        formatted = f"[{timestamp}] {prefix} {message}"
        print(formatted)
        self.test_results.append(f"[{level}] {message}")
        
        if level == "SUCCESS":
            self.passed_count += 1
        elif level == "ERROR":
            self.failed_count += 1
    
    async def setup(self):
        """Setup Lighter client and API"""
        if not self.api_key_private_key:
            self.log("API_KEY_PRIVATE_KEY not set in environment", "ERROR")
            return False
        
        try:
            from lighter.signer_client import SignerClient
            from lighter import OrderApi, ApiClient, Configuration
            
            # Create api_private_keys dictionary
            api_private_keys = {self.api_key_index: self.api_key_private_key}
            
            # Initialize SignerClient
            self.log("Initializing SignerClient...")
            self.client = SignerClient(
                url=self.base_url,
                account_index=self.account_index,
                api_private_keys=api_private_keys
            )
            
            # Check client
            err = self.client.check_client()
            if err is not None:
                self.log(f"CheckClient error: {err}", "ERROR")
                return False
            
            self.log("SignerClient initialized successfully", "SUCCESS")
            
            # Create API client
            self.log("Creating ApiClient...")
            self.api_client = ApiClient(configuration=Configuration(host=self.base_url))
            self.order_api = OrderApi(self.api_client)
            self.log("ApiClient created successfully", "SUCCESS")
            
            return True
            
        except ImportError as e:
            self.log(f"Failed to import Lighter SDK: {e}", "ERROR")
            return False
        except Exception as e:
            self.log(f"Setup failed: {e}", "ERROR")
            import traceback
            self.log(traceback.format_exc(), "ERROR")
            return False
    
    async def test_get_order_books(self):
        """Test getting order books (market list)"""
        self.log("\n" + "=" * 50)
        self.log("Test: Get Order Books")
        self.log("=" * 50)
        
        try:
            self.log("Fetching order books from Lighter...")
            order_books = await self.order_api.order_books()
            
            if not order_books:
                self.log("No order books returned", "ERROR")
                return False
            
            self.log(f"Found {len(order_books.order_books)} markets", "SUCCESS")
            
            # Print first 10 markets
            self.log("\nFirst 10 markets:")
            for i, market in enumerate(order_books.order_books[:10]):
                self.log(f"  {i+1}. {market.symbol} (ID: {market.market_id})")
            
            # Check if BTC exists
            btc_found = False
            for market in order_books.order_books:
                if market.symbol == "BTC":
                    btc_found = True
                    self.log(f"\nBTC Market Details:")
                    self.log(f"  Market ID: {market.market_id}")
                    self.log(f"  Market Type: {market.market_type}")
                    self.log(f"  Status: {market.status}")
                    self.log(f"  Supported Size Decimals: {market.supported_size_decimals}")
                    self.log(f"  Supported Price Decimals: {market.supported_price_decimals}")
                    self.log(f"  Min Base Amount: {market.min_base_amount}")
                    self.log(f"  Taker Fee: {market.taker_fee}")
                    self.log(f"  Maker Fee: {market.maker_fee}")
                    break
            
            if not btc_found:
                self.log("BTC market not found!", "WARNING")
            
            return True
            
        except Exception as e:
            self.log(f"Failed to get order books: {e}", "ERROR")
            import traceback
            self.log(traceback.format_exc(), "ERROR")
            return False
    
    async def test_get_account_info(self):
        """Test getting account information"""
        self.log("\n" + "=" * 50)
        self.log("Test: Get Account Info")
        self.log("=" * 50)
        
        try:
            from lighter import AccountApi
            
            self.log("Fetching account information...")
            account_api = AccountApi(self.api_client)
            account_data = await account_api.account(
                by="index", 
                value=str(self.account_index)
            )
            
            if not account_data or not account_data.accounts:
                self.log("No account data returned", "ERROR")
                return False
            
            account = account_data.accounts[0]
            self.log(f"Account found!", "SUCCESS")
            self.log(f"  Address: {account.address}")
            
            # Show positions
            if account.positions:
                self.log(f"  Positions ({len(account.positions)}):")
                for pos in account.positions:
                    if abs(float(pos.position)) > 0.0001:
                        self.log(f"    - Market ID {pos.market_id}: {pos.position}")
            else:
                self.log(f"  Positions: None")
            
            return True
            
        except Exception as e:
            self.log(f"Failed to get account info: {e}", "ERROR")
            import traceback
            self.log(traceback.format_exc(), "ERROR")
            return False
    
    async def test_get_positions(self):
        """Test getting positions"""
        self.log("\n" + "=" * 50)
        self.log("Test: Get Positions")
        self.log("=" * 50)
        
        try:
            self.log("Fetching positions...")
            
            # Use account API to get positions
            from lighter import AccountApi
            account_api = AccountApi(self.api_client)
            account_data = await account_api.account(
                by="index",
                value=str(self.account_index)
            )
            
            if account_data and account_data.accounts:
                account = account_data.accounts[0]
                if account.positions:
                    self.log(f"Found {len(account.positions)} positions", "SUCCESS")
                    for pos in account.positions:
                        self.log(f"  Market {pos.market_id}: {pos.position} @ {pos.avg_price}")
                else:
                    self.log("No positions", "SUCCESS")
            else:
                self.log("Could not get positions", "ERROR")
            
            return True
            
        except Exception as e:
            self.log(f"Failed to get positions: {e}", "ERROR")
            return False
    
    async def test_auth_token(self):
        """Test authentication token generation"""
        self.log("\n" + "=" * 50)
        self.log("Test: Generate Auth Token")
        self.log("=" * 50)
        
        try:
            self.log("Generating auth token...")
            auth_token, error = self.client.create_auth_token_with_expiry()
            
            if error:
                self.log(f"Failed to generate auth token: {error}", "ERROR")
                return False
            
            self.log(f"Auth token generated: {auth_token[:50]}...", "SUCCESS")
            return True
            
        except Exception as e:
            self.log(f"Failed to generate auth token: {e}", "ERROR")
            return False
    
    async def test_active_orders(self):
        """Test getting active orders"""
        self.log("\n" + "=" * 50)
        self.log("Test: Get Active Orders")
        self.log("=" * 50)
        
        try:
            self.log("Fetching active orders...")
            
            # First get markets to find a valid market_id
            order_books = await self.order_api.order_books()
            if not order_books or not order_books.order_books:
                self.log("No markets found", "ERROR")
                return False
            
            # Get first market ID
            test_market_id = order_books.order_books[0].market_id
            
            # Get auth token
            auth_token, error = self.client.create_auth_token_with_expiry()
            if error:
                self.log(f"Failed to get auth token: {error}", "ERROR")
                return False
            
            # Get active orders
            orders_response = await self.order_api.account_active_orders(
                account_index=self.account_index,
                market_id=test_market_id,
                auth=auth_token
            )
            
            if orders_response and orders_response.orders:
                self.log(f"Found {len(orders_response.orders)} active orders", "SUCCESS")
                for order in orders_response.orders[:5]:
                    side = "SELL" if order.is_ask else "BUY"
                    self.log(f"  {side} {order.size} @ {order.price}")
            else:
                self.log("No active orders", "SUCCESS")
            
            return True
            
        except Exception as e:
            self.log(f"Failed to get active orders: {e}", "ERROR")
            return False
    
    async def test_ws_connection(self):
        """Test WebSocket connection using Lighter SDK with proper origin"""
        self.log("\n" + "=" * 50)
        self.log("Test: WebSocket Connection (with readonly=true)")
        self.log("=" * 50)
        
        try:
            import websockets
            import json
            
            # Use readonly=true parameter for restricted regions
            ws_url = "wss://mainnet.zklighter.elliot.ai/stream?readonly=true"
            
            self.log(f"Connecting to {ws_url}...")
            
            async with websockets.connect(ws_url, open_timeout=5) as ws:
                self.log("WebSocket connected!", "SUCCESS")
                
                # Wait for connected message
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(msg)
                    self.log(f"Received: {data.get('type', 'unknown')}", "SUCCESS")
                    
                    # Try subscribing to order book
                    subscribe_msg = {
                        "type": "subscribe",
                        "channel": "order_book/1"
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    self.log("Subscription sent", "SUCCESS")
                    
                    # Wait for order book data
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5)
                        data = json.loads(msg)
                        self.log(f"Received order book: {data.get('type', 'unknown')}", "SUCCESS")
                    except asyncio.TimeoutError:
                        self.log("No order book response within 5 seconds", "WARNING")
                    
                    return True
                    
                except asyncio.TimeoutError:
                    self.log("No message within 5 seconds", "WARNING")
                    return True
                
        except Exception as e:
            self.log(f"WebSocket test failed: {e}", "ERROR")
            import traceback
            self.log(f"Traceback: {traceback.format_exc()}", "DEBUG")
            return False
    
    async def run_all_tests(self):
        """Run all tests"""
        print("\n" + "=" * 60)
        print("Lighter Exchange Functionality Tests")
        print("=" * 60)
        
        if not await self.setup():
            self.log("\nSetup failed, exiting", "ERROR")
            return
        
        tests = [
            self.test_get_order_books,
            self.test_get_account_info,
            self.test_get_positions,
            self.test_auth_token,
            self.test_active_orders,
            self.test_ws_connection,
        ]
        
        for test in tests:
            try:
                await test()
            except Exception as e:
                self.log(f"Test failed with exception: {e}", "ERROR")
        
        # Summary
        print("\n" + "=" * 60)
        print("Test Summary")
        print("=" * 60)
        print(f"Total Tests: {len(tests)}")
        print(f"Passed: {self.passed_count}")
        print(f"Failed: {self.failed_count}")
        print("=" * 60)
        
        if self.failed_count == 0:
            print("\n🎉 All tests passed!")
        else:
            print(f"\n⚠️ {self.failed_count} test(s) failed")
        
        # Save results to file
        with open("lighter_test_results.txt", "w", encoding="utf-8") as f:
            f.write("Lighter Test Results\n")
            f.write("=" * 60 + "\n")
            for result in self.test_results:
                f.write(result + "\n")
        
        print(f"\nResults saved to: lighter_test_results.txt")


async def main():
    """Main entry point"""
    tester = LighterTester()
    await tester.run_all_tests()


if __name__ == "__main__":
    asyncio.run(main())

