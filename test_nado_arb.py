#!/usr/bin/env python3
"""
Nado 套利策略测试脚本
测试 nado_arb.py 的各个功能
"""

import asyncio
import sys
import os
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from strategy.nado_arb import NadoArb


async def test_strategy_initialization():
    """测试策略初始化"""
    print("=" * 60)
    print("🧪 测试 NadoArb 策略初始化")
    print("=" * 60)
    
    try:
        # 创建策略实例
        strategy = NadoArb(
            ticker='BTC',
            order_quantity=Decimal('0.001'),
            fill_timeout=5,
            max_position=Decimal('0.01'),
            long_ex_threshold=Decimal('10'),
            short_ex_threshold=Decimal('10'),
            robot_id='test_nado_arb'
        )
        
        print(f"\n✅ 策略初始化成功!")
        print(f"   Ticker: {strategy.ticker}")
        print(f"   Order Quantity: {strategy.order_quantity}")
        print(f"   Max Position: {strategy.max_position}")
        print(f"   Robot ID: {strategy.robot_id}")
        print(f"   Logger: {strategy.logger.name}")
        
        return strategy
        
    except Exception as e:
        print(f"\n❌ 策略初始化失败: {e}")
        import traceback
        traceback.print_exc()
        return None


async def test_client_operations(strategy):
    """测试客户端操作"""
    print("\n" + "=" * 60)
    print("🧪 测试 Nado 客户端操作")
    print("=" * 60)
    
    try:
        # 初始化客户端
        await strategy.initialize()
        print(f"\n✅ Nado 客户端初始化成功!")
        print(f"   Product ID: {strategy.nado_product_id}")
        print(f"   Tick Size: {strategy.nado_tick_size}")
        
        # 测试产品信息
        if strategy.nado_client:
            print(f"\n   Nado 客户端已配置")
        
        return True
        
    except Exception as e:
        print(f"\n❌ 客户端操作失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_market_data(strategy):
    """测试市场数据获取"""
    print("\n" + "=" * 60)
    print("🧪 测试市场数据获取")
    print("=" * 60)
    
    try:
        # 获取 Nado BBO
        nado_bid, nado_ask = await strategy._get_nado_bbo()
        print(f"\n[1] Nado BBO:")
        if nado_bid and nado_ask:
            print(f"   ✅ Bid: ${nado_bid:,.2f}")
            print(f"   ✅ Ask: ${nado_ask:,.2f}")
            print(f"   ✅ Spread: ${nado_ask - nado_bid:,.2f}")
        else:
            print(f"   ⚠️ 无法获取 Nado BBO 数据")
        
        # 获取 Lighter BBO
        lighter_bid, lighter_ask = strategy.order_book_manager.get_lighter_bbo()
        print(f"\n[2] Lighter BBO:")
        if lighter_bid and lighter_ask:
            print(f"   ✅ Bid: ${lighter_bid:,.2f}")
            print(f"   ✅ Ask: ${lighter_ask:,.2f}")
        else:
            print(f"   ⚠️ Lighter 订单簿未就绪 (需要启动 Lighter 连接)")
        
        # 计算套利价差
        if nado_bid and nado_ask and lighter_bid and lighter_ask:
            long_spread = lighter_bid - nado_ask
            short_spread = nado_bid - lighter_ask
            
            print(f"\n[3] 套利机会分析:")
            print(f"   Long Spread (Lighter Bid - Nado Ask): ${long_spread:,.2f}")
            print(f"   Short Spread (Nado Bid - Lighter Ask): ${short_spread:,.2f}")
            print(f"   Long Threshold: {strategy.long_ex_threshold}")
            print(f"   Short Threshold: {strategy.short_ex_threshold}")
            
            if long_spread > strategy.long_ex_threshold:
                print(f"   🟢 做多机会: Spread > Threshold")
            else:
                print(f"   🔴 做多机会不足")
                
            if short_spread > strategy.short_ex_threshold:
                print(f"   🟢 做空机会: Spread > Threshold")
            else:
                print(f"   🔴 做空机会不足")
        
        return True
        
    except Exception as e:
        print(f"\n❌ 市场数据获取失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_position_operations(strategy):
    """测试持仓操作"""
    print("\n" + "=" * 60)
    print("🧪 测试持仓操作")
    print("=" * 60)
    
    try:
        # 检查当前持仓
        nado_pos = strategy.position_tracker.get_current_nado_position()
        lighter_pos = strategy.position_tracker.get_current_lighter_position()
        
        print(f"\n[1] 当前持仓:")
        print(f"   Nado: {nado_pos}")
        print(f"   Lighter: {lighter_pos}")
        
        # 测试位置同步
        print(f"\n[2] 位置同步:")
        if strategy.position_tracker:
            print(f"   ✅ PositionTracker 已初始化")
        else:
            print(f"   ⚠️ PositionTracker 未初始化")
        
        return True
        
    except Exception as e:
        print(f"\n❌ 持仓操作失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_orderbook_manager(strategy):
    """测试订单簿管理器"""
    print("\n" + "=" * 60)
    print("🧪 测试订单簿管理器")
    print("=" * 60)
    
    try:
        # 检查 Nado 订单簿状态
        nado_ready = strategy.order_book_manager.nado_order_book_ready
        print(f"\n[1] Nado 订单簿状态:")
        print(f"   Ready: {nado_ready}")
        print(f"   Best Bid: {strategy.order_book_manager.nado_best_bid}")
        print(f"   Best Ask: {strategy.order_book_manager.nado_best_ask}")
        
        # 检查 Lighter 订单簿状态
        lighter_ready = strategy.order_book_manager.lighter_order_book_ready
        print(f"\n[2] Lighter 订单簿状态:")
        print(f"   Ready: {lighter_ready}")
        if strategy.order_book_manager.lighter_best_bid:
            print(f"   Best Bid: ${strategy.order_book_manager.lighter_best_bid:,.2f}")
        if strategy.order_book_manager.lighter_best_ask:
            print(f"   Best Ask: ${strategy.order_book_manager.lighter_best_ask:,.2f}")
        
        return True
        
    except Exception as e:
        print(f"\n❌ 订单簿管理器测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_shutdown(strategy):
    """测试关闭功能"""
    print("\n" + "=" * 60)
    print("🧪 测试关闭功能")
    print("=" * 60)
    
    try:
        strategy.shutdown()
        print(f"\n✅ 关闭成功")
        return True
        
    except Exception as e:
        print(f"\n❌ 关闭失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """主测试函数"""
    print("\n" + "🔷" * 30)
    print("  Nado 套利策略测试套件")
    print("🔷" * 30)
    
    # 1. 测试策略初始化
    print("\n[步骤 1/6] 测试策略初始化...")
    strategy = await test_strategy_initialization()
    if not strategy:
        print("\n❌ 测试终止：策略初始化失败")
        return 1
    
    # 2. 测试客户端操作
    print("\n[步骤 2/6] 测试客户端操作...")
    client_ok = await test_client_operations(strategy)
    if not client_ok:
        print("\n⚠️ 警告：客户端操作失败，继续测试...")
    
    # 3. 测试市场数据
    print("\n[步骤 3/6] 测试市场数据...")
    market_ok = await test_market_data(strategy)
    if not market_ok:
        print("\n⚠️ 警告：市场数据获取失败，继续测试...")
    
    # 4. 测试持仓操作
    print("\n[步骤 4/6] 测试持仓操作...")
    position_ok = await test_position_operations(strategy)
    if not position_ok:
        print("\n⚠️ 警告：持仓操作失败，继续测试...")
    
    # 5. 测试订单簿管理器
    print("\n[步骤 5/6] 测试订单簿管理器...")
    orderbook_ok = await test_orderbook_manager(strategy)
    if not orderbook_ok:
        print("\n⚠️ 警告：订单簿管理器测试失败，继续测试...")
    
    # 6. 测试关闭功能
    print("\n[步骤 6/6] 测试关闭功能...")
    shutdown_ok = await test_shutdown(strategy)
    
    # 总结
    print("\n" + "=" * 60)
    print("📊 测试结果总结")
    print("=" * 60)
    
    tests = [
        ("策略初始化", strategy is not None),
        ("客户端操作", client_ok),
        ("市场数据", market_ok),
        ("持仓操作", position_ok),
        ("订单簿管理器", orderbook_ok),
        ("关闭功能", shutdown_ok),
    ]
    
    passed = 0
    for name, result in tests:
        status = "✅ 通过" if result else "❌ 失败"
        print(f"   {status} - {name}")
        if result:
            passed += 1
    
    print(f"\n总计: {passed}/{len(tests)} 项测试通过")
    
    if passed == len(tests):
        print("\n🎉 所有测试通过!")
    else:
        print(f"\n⚠️ {len(tests) - passed} 项测试失败")
    
    print("🔷" * 30)
    
    return 0 if passed == len(tests) else 1


if __name__ == '__main__':
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

