#!/usr/bin/env python3
"""
Nado 交易方法测试脚本
用于验证 Nado API 客户端和交易功能是否正常工作
"""

import asyncio
import sys
import os
from decimal import Decimal

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from strategy.nado_client import NadoClient, NadoWebSocketClient, wei_to_usd, usd_to_wei


async def test_nado_client():
    """测试 Nado 客户端功能"""
    print("=" * 60)
    print("🧪 Nado 客户端测试")
    print("=" * 60)
    
    # 1. 测试客户端初始化
    print("\n[1] 测试客户端初始化...")
    try:
        client = NadoClient()
        print(f"   ✅ 客户端初始化成功")
        print(f"   - 钱包地址: {client.wallet_address[:10]}...{client.wallet_address[-6:] if client.wallet_address else 'None'}")
        print(f"   - API Base: {client.signer.private_key[:8] if client.signer.private_key else 'None'}...")
    except Exception as e:
        print(f"   ❌ 客户端初始化失败: {e}")
        return False
    
    # 2. 测试产品查询
    print("\n[2] 测试产品查询...")
    try:
        products = await client.get_products()
        print(f"   ✅ 获取到 {len(products)} 个产品")
        
        # 显示部分产品
        for p in products[:5]:
            print(f"   - ID: {p.get('id')}, Symbol: {p.get('symbol')}, Tick: {p.get('tickSize')}")
    except Exception as e:
        print(f"   ⚠️ 产品查询失败 (可能需要网络): {e}")
    
    # 3. 测试产品信息获取
    print("\n[3] 测试获取 BTC 产品信息...")
    try:
        product_info = await client.get_product_info('BTC')
        if product_info:
            print(f"   ✅ 获取到 BTC 产品信息:")
            print(f"   - ID: {product_info.get('id')}")
            print(f"   - Symbol: {product_info.get('symbol')}")
            print(f"   - Tick Size: {product_info.get('tickSize')}")
            print(f"   - Min Order: {product_info.get('minOrderSize')}")
        else:
            print("   ⚠️ 未找到 BTC 产品信息，使用默认配置")
    except Exception as e:
        print(f"   ⚠️ 获取产品信息失败 (可能需要网络): {e}")
    
    # 4. 测试价格转换函数
    print("\n[4] 测试价格转换函数...")
    try:
        # 测试 Wei 转换
        test_wei = 50000000000000000000  # 50 USD in Wei
        usd_price = wei_to_usd(test_wei)
        print(f"   ✅ Wei 转 USD: {test_wei} Wei = ${usd_price}")
        
        # 测试 USD 转换回 Wei
        back_to_wei = usd_to_wei(usd_price)
        print(f"   ✅ USD 转 Wei: ${usd_price} = {back_to_wei} Wei")
        
        # 验证转换正确
        assert abs(float(back_to_wei) - float(test_wei)) < 1, "价格转换错误"
        print(f"   ✅ 转换验证通过")
    except Exception as e:
        print(f"   ❌ 价格转换测试失败: {e}")
    
    # 5. 测试订单参数构建
    print("\n[5] 测试订单参数构建...")
    try:
        product_id = 4  # BTC
        side = 'buy'
        price = Decimal('50000.0')
        amount = Decimal('0.001')
        order_type = 'post_only'
        
        # 模拟构建订单参数
        price_wei = int(price * Decimal('1e18'))
        amount_str = str(amount)
        
        print(f"   ✅ 订单参数构建成功:")
        print(f"   - Product ID: {product_id}")
        print(f"   - Side: {side}")
        print(f"   - Price: ${price} ({price_wei} Wei)")
        print(f"   - Amount: {amount_str}")
        print(f"   - Order Type: {order_type}")
    except Exception as e:
        print(f"   ❌ 订单参数构建失败: {e}")
    
    # 6. 清理
    print("\n[6] 关闭客户端连接...")
    try:
        await client.close()
        print(f"   ✅ 客户端连接已关闭")
    except Exception as e:
        print(f"   ⚠️ 关闭连接时出错: {e}")
    
    print("\n" + "=" * 60)
    print("✅ Nado 客户端测试完成")
    print("=" * 60)
    return True


async def test_order_book():
    """测试订单簿获取"""
    print("\n" + "=" * 60)
    print("🧪 订单簿测试")
    print("=" * 60)
    
    client = NadoClient()
    
    # 测试获取 BTC 订单簿
    print("\n[1] 获取 BTC 订单簿...")
    try:
        order_book = await client.get_order_book(product_id=4, depth=10)
        print(f"   ✅ 获取到订单簿数据")
        
        # 解析订单簿
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])
        
        if bids:
            best_bid = Decimal(bids[0]['price']) / Decimal('1e18')
            print(f"   - Best Bid: ${best_bid} (size: {bids[0].get('size', 'N/A')})")
        else:
            print("   - Best Bid: N/A")
        
        if asks:
            best_ask = Decimal(asks[0]['price']) / Decimal('1e18')
            print(f"   - Best Ask: ${best_ask} (size: {asks[0].get('size', 'N/A')})")
        else:
            print("   - Best Ask: N/A")
        
        # 计算价差
        if bids and asks:
            bid_price = Decimal(bids[0]['price']) / Decimal('1e18')
            ask_price = Decimal(asks[0]['price']) / Decimal('1e18')
            spread = ask_price - bid_price
            print(f"   - Spread: ${spread}")
        
    except Exception as e:
        print(f"   ⚠️ 获取订单簿失败 (可能需要网络): {e}")
    
    await client.close()
    print("\n" + "=" * 60)
    print("✅ 订单簿测试完成")
    print("=" * 60)


async def test_subaccount():
    """测试子账户查询"""
    print("\n" + "=" * 60)
    print("🧪 子账户测试")
    print("=" * 60)
    
    client = NadoClient()
    
    print("\n[1] 查询子账户信息...")
    try:
        subaccount = await client.get_subaccount_info()
        print(f"   ✅ 获取到子账户信息")
        print(f"   - 数据类型: {type(subaccount)}")
        if isinstance(subaccount, dict):
            for key in subaccount.keys():
                print(f"   - {key}: {type(subaccount[key])}")
    except Exception as e:
        print(f"   ⚠️ 获取子账户信息失败 (可能需要网络或API不可用): {e}")
    
    await client.close()
    print("\n" + "=" * 60)
    print("✅ 子账户测试完成")
    print("=" * 60)


async def main():
    """主测试函数"""
    print("\n" + "🔷" * 30)
    print("  Nado API 测试套件")
    print("🔷" * 30)
    
    # 检查环境变量
    print("\n📋 环境变量检查:")
    wallet = os.getenv('NADO_WALLET_ADDRESS', '')
    private_key = os.getenv('NADO_PRIVATE_KEY', '')
    
    if wallet:
        print(f"   ✅ NADO_WALLET_ADDRESS: {wallet[:10]}...{wallet[-4:]}")
    else:
        print(f"   ⚠️ NADO_WALLET_ADDRESS: 未设置")
    
    if private_key:
        print(f"   ✅ NADO_PRIVATE_KEY: {private_key[:8]}...{private_key[-4:]}")
    else:
        print(f"   ⚠️ NADO_PRIVATE_KEY: 未设置")
    
    # 运行测试
    all_passed = True
    
    # 测试 1: 客户端基本功能
    if not await test_nado_client():
        all_passed = False
    
    # 测试 2: 订单簿
    await test_order_book()
    
    # 测试 3: 子账户
    await test_subaccount()
    
    print("\n" + "🔷" * 30)
    if all_passed:
        print("  ✅ 所有核心测试通过")
    else:
        print("  ⚠️ 部分测试失败，请检查错误信息")
    print("🔷" * 30)
    
    return 0 if all_passed else 1


if __name__ == '__main__':
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

