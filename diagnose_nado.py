#!/usr/bin/env python3
"""
Nado API 诊断脚本
用于调试和修复 Nado API 集成问题
"""

import asyncio
import sys
import os
import json
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()


async def diagnose_api():
    """诊断 Nado API 端点"""
    import aiohttp
    
    api_base = os.getenv('NADO_API_BASE', 'https://gateway.prod.nado.xyz/v1')
    wallet_address = os.getenv('NADO_WALLET_ADDRESS', '')
    
    print("=" * 60)
    print("🔍 Nado API 诊断")
    print("=" * 60)
    print(f"\nAPI Base: {api_base}")
    print(f"Wallet: {wallet_address[:20]}...")
    
    session = aiohttp.ClientSession()
    
    # 测试各种可能的端点
    endpoints_to_test = [
        # 公开端点
        ('', 'GET', None, None, 'API 根目录'),
        ('symbols', 'GET', None, None, '产品列表'),
        ('symbols?depth=10', 'GET', None, None, '产品列表(带深度)'),
        
        # 可能的订单簿端点
        ('market/liquidity?product_id=4', 'GET', None, None, '订单簿 Liquidity'),
        ('market/depth?product_id=4', 'GET', None, None, '订单簿 Depth'),
        ('orderbook/4', 'GET', None, None, '订单簿 ID 4'),
        ('orderbook?product_id=4', 'GET', None, None, '订单簿 Product ID'),
        ('depth/4', 'GET', None, None, 'Depth ID 4'),
        
        # 可能的账户端点
        ('account', 'GET', None, None, '账户信息'),
        ('subaccount', 'GET', None, None, '子账户'),
        ('subaccount/info', 'GET', None, None, '子账户信息'),
        ('positions', 'GET', None, None, '持仓'),
    ]
    
    print("\n[测试 API 端点]")
    print("-" * 60)
    
    for endpoint, method, params, data, desc in endpoints_to_test:
        url = f"{api_base}/{endpoint}"
        try:
            async with session.get(url, params=params) as response:
                content_type = response.headers.get('Content-Type', 'N/A')
                status = response.status
                
                if status == 200:
                    try:
                        data = await response.json()
                        print(f"✅ [{status}] {desc}")
                        print(f"   端点: {endpoint}")
                        print(f"   Content-Type: {content_type}")
                        
                        # 显示数据结构
                        if isinstance(data, dict):
                            keys = list(data.keys())[:10]
                            print(f"   Keys: {keys}")
                            # 显示第一条数据
                            if 'data' in data:
                                data_keys = list(data['data'].keys())[:10] if isinstance(data['data'], dict) else 'list'
                                print(f"   Data Keys: {data_keys}")
                        elif isinstance(data, list):
                            print(f"   List length: {len(data)}")
                            if data:
                                print(f"   First item keys: {list(data[0].keys())[:10] if isinstance(data[0], dict) else data[0]}")
                        
                    except Exception as e:
                        print(f"✅ [{status}] {desc} (非 JSON 响应)")
                        print(f"   端点: {endpoint}")
                        text = await response.text()
                        print(f"   Response: {text[:200]}...")
                else:
                    print(f"❌ [{status}] {desc}")
                    print(f"   端点: {endpoint}")
                    text = await response.text()[:200]
                    print(f"   Response: {text}")
                    
        except Exception as e:
            print(f"❌ [错误] {desc}: {e}")
            print(f"   端点: {endpoint}")
    
    # 测试带认证的请求
    print("\n\n[测试带认证的请求]")
    print("-" * 60)
    
    # 可能的认证端点
    auth_endpoints = [
        ('account/orders', 'GET', None, None, '账户订单'),
        ('orders', 'GET', None, None, '订单列表'),
        ('orders/open', 'GET', None, None, '挂单列表'),
    ]
    
    for endpoint, method, params, data, desc in auth_endpoints:
        url = f"{api_base}/{endpoint}"
        headers = {'Content-Type': 'application/json'}
        
        try:
            async with session.get(url, params=params, headers=headers) as response:
                status = response.status
                
                if status == 200:
                    print(f"✅ [{status}] {desc}")
                    print(f"   端点: {endpoint}")
                elif status == 401 or status == 403:
                    print(f"🔐 [{status}] {desc} - 需要认证")
                    print(f"   端点: {endpoint}")
                else:
                    print(f"❌ [{status}] {desc}")
                    print(f"   端点: {endpoint}")
                    
        except Exception as e:
            print(f"❌ [错误] {desc}: {e}")
    
    await session.close()
    
    print("\n" + "=" * 60)
    print("✅ 诊断完成")
    print("=" * 60)


async def test_products_structure():
    """测试产品数据结构"""
    import aiohttp
    
    api_base = os.getenv('NADO_API_BASE', 'https://gateway.prod.nado.xyz/v1')
    
    print("\n" + "=" * 60)
    print("📊 产品数据结构分析")
    print("=" * 60)
    
    session = aiohttp.ClientSession()
    
    try:
        url = f"{api_base}/symbols"
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                print(f"\n✅ 获取到产品数据")
                print(f"数据类型: {type(data)}")
                
                if isinstance(data, list):
                    print(f"产品数量: {len(data)}")
                    print("\n产品数据结构:")
                    for i, product in enumerate(data[:3]):
                        print(f"\n产品 {i+1}:")
                        if isinstance(product, dict):
                            for key, value in product.items():
                                print(f"  {key}: {value} (type: {type(value).__name__})")
                        else:
                            print(f"  {product}")
                elif isinstance(data, dict):
                    print(f"字典 Keys: {list(data.keys())}")
                    if 'data' in data:
                        print(f"data 类型: {type(data['data'])}")
                        if isinstance(data['data'], list):
                            print(f"data 长度: {len(data['data'])}")
                            if data['data']:
                                print(f"第一条数据 keys: {list(data['data'][0].keys())}")
            
    except Exception as e:
        print(f"❌ 获取产品数据失败: {e}")
    
    await session.close()


async def main():
    """主函数"""
    print("\n" + "🔷" * 30)
    print("  Nado API 诊断工具")
    print("🔷" * 30)
    
    await diagnose_api()
    await test_products_structure()
    
    print("\n" + "🔷" * 30)
    print("  诊断完成，请根据输出修复 API 端点")
    print("🔷" * 30)


if __name__ == '__main__':
    asyncio.run(main())

