#!/usr/bin/env python3
"""
Nado 永续合约交易测试

Nado 交易所只支持永续合约 (PERP) 产品:
- KBTC: Product ID 1 (包装比特币)
- BTC-PERP: Product ID 2 (比特币永续合约)
"""

import asyncio
import sys
import os
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from strategy.nado_client import NadoClient, wei_to_usd, usd_to_wei


async def test_perp_trading():
    """测试永续合约交易"""
    print("=" * 60)
    print("🧪 Nado 永续合约交易测试")
    print("=" * 60)
    
    client = NadoClient()
    
    # 1. 产品查询
    print("\n[1] 获取永续合约产品...")
    products = await client.get_products()
    
    # 筛选永续合约
    perp_products = [p for p in products if 'PERP' in p.get('symbol', '')]
    print(f"   ✅ 找到 {len(perp_products)} 个永续合约产品")
    
    # 显示可用产品
    print("\n   可用永续合约:")
    for p in perp_products[:10]:
        pid = p.get('product_id')
        symbol = p.get('symbol')
        status = p.get('trading_status')
        color = "🟢" if status == "live" else "🟡" if status == "post_only" else "🔴"
        print(f"   {color} ID {pid}: {symbol} ({status})")
    
    # 2. 加载产品缓存
    print("\n[2] 加载产品缓存...")
    await client._load_products()
    print(f"   ✅ 缓存 {len(client.products)} 个产品")
    
    # 3. 获取 KBTC 和 BTC-PERP 信息
    print("\n[3] 获取 BTC 相关产品信息...")
    
    for symbol in ['KBTC', 'BTC-PERP']:
        info = await client.get_product_info(symbol)
        if info:
            print(f"   ✅ {symbol}: ID={info.get('id')}, Tick={info.get('tickSize')}")
        else:
            print(f"   ❌ 未找到 {symbol}")
    
    # 4. 测试订单参数构建 (模拟)
    print("\n[4] 测试订单参数构建...")
    
    # 模拟永续合约订单
    test_cases = [
        {'product': 'KBTC', 'side': 'buy', 'price': Decimal('45000'), 'amount': Decimal('0.001')},
        {'product': 'BTC-PERP', 'side': 'sell', 'price': Decimal('97000'), 'amount': Decimal('0.01')},
    ]
    
    for test in test_cases:
        product = test['product']
        side = test['side']
        price = test['price']
        amount = test['amount']
        
        # 获取产品 ID
        product_info = await client.get_product_info(product)
        if not product_info:
            print(f"   ❌ 未找到产品: {product}")
            continue
        
        product_id = product_info.get('id')
        
        # 构建订单参数
        price_wei = str(int(price * Decimal('1e18')))
        amount_str = str(amount) if side == 'buy' else str(-amount)
        
        print(f"\n   📝 {product} {side.upper()} 订单:")
        print(f"      Product ID: {product_id}")
        print(f"      Price: ${price} ({price_wei} Wei)")
        print(f"      Amount: {amount_str}")
        print(f"      Order Type: post_only")
    
    # 5. 价格转换测试
    print("\n[5] 价格转换测试...")
    
    test_prices = [
        (97000, "BTC-PERP 价格"),
        (45000, "KBTC 价格"),
    ]
    
    for usd_price, desc in test_prices:
        wei_val = usd_to_wei(Decimal(usd_price))
        back_usd = wei_to_usd(wei_val)
        print(f"   ✅ ${usd_price} → {wei_val} Wei → ${back_usd}")
    
    await client.close()
    
    print("\n" + "=" * 60)
    print("✅ 永续合约交易测试完成")
    print("=" * 60)


async def test_order_simulation():
    """模拟订单流程测试"""
    print("\n" + "=" * 60)
    print("📋 订单模拟流程")
    print("=" * 60)
    
    client = NadoClient()
    await client._load_products()
    
    # 使用 BTC-PERP (ID: 2)
    btc_perp_info = await client.get_product_info('BTC-PERP')
    if not btc_perp_info:
        print("❌ 未找到 BTC-PERP 产品")
        await client.close()
        return
    
    product_id = btc_perp_info.get('id')
    tick_size = Decimal(btc_perp_info.get('tickSize', '0.1'))
    
    print(f"\n产品: BTC-PERP (ID: {product_id})")
    print(f"Tick Size: {tick_size}")
    
    # 模拟做多订单
    print("\n[做多 (LONG) 订单模拟]")
    print("-" * 40)
    
    entry_price = Decimal('97000')
    size = Decimal('0.01')
    
    # 计算价格
    buy_price = entry_price - tick_size  # 买入价略低于市价
    buy_price_rounded = (buy_price / tick_size).quantize(Decimal('1')) * tick_size
    
    print(f"📊 当前价格: ${entry_price}")
    print(f"📤 买入价格: ${buy_price_rounded}")
    print(f"📦 仓位大小: {size} BTC")
    print(f"💰 订单价值: ${buy_price_rounded * size}")
    
    # 模拟做空订单
    print("\n[做空 (SHORT) 订单模拟]")
    print("-" * 40)
    
    sell_price = entry_price + tick_size  # 卖出价略高于市价
    sell_price_rounded = (sell_price / tick_size).quantize(Decimal('1')) * tick_size
    
    print(f"📊 当前价格: ${entry_price}")
    print(f"📤 卖出价格: ${sell_price_rounded}")
    print(f"📦 仓位大小: -{size} BTC")
    print(f"💰 订单价值: ${sell_price_rounded * size}")
    
    # 模拟对冲逻辑
    print("\n[对冲逻辑]")
    print("-" * 40)
    print("永续合约对冲:")
    print("1. 在 Nado 开仓 (买入/卖出永续合约)")
    print("2. 在 Lighter 现货对冲")
    print("3. 注意资金费率 (Funding Rate)")
    print("4. 需要维护保证金要求")
    
    await client.close()
    
    print("\n" + "=" * 60)
    print("✅ 订单模拟完成")
    print("=" * 60)


async def main():
    """主函数"""
    print("\n" + "🔷" * 30)
    print("  Nado 永续合约测试套件")
    print("🔷" * 30)
    
    await test_perp_trading()
    await test_order_simulation()
    
    print("\n" + "🔷" * 30)
    print("  ✅ 所有测试完成")
    print("  📝 注意: Nado 只支持永续合约 (PERP)")
    print("  ⚠️ 需要实际 API 文档验证订单 API 端点")
    print("🔷" * 30)


if __name__ == '__main__':
    asyncio.run(main())

