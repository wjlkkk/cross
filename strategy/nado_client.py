#!/usr/bin/env python3
"""
Nado 交易所 Python 客户端

使用 Nado TypeScript SDK (@nadohq/client)
"""

import asyncio
import json
import subprocess
import sys
import os
from decimal import Decimal
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()


@dataclass
class NadoProduct:
    """Nado 产品信息"""
    product_id: int
    symbol: str
    price_increment: str = "0.1"
    min_size: str = "0.001"


class NadoClient:
    """Nado Python 客户端 (使用 @nadohq/client SDK)"""
    
    SDK_DIR = '/root/nado/node_modules/@nadohq/client'
    NADO_DIR = '/root/nado'
    
    def __init__(self, wallet_address: str = None, private_key: str = None):
        self.wallet_address = wallet_address or os.getenv('NADO_WALLET_ADDRESS')
        self.private_key = private_key or os.getenv('NADO_PRIVATE_KEY')
        self.subaccount_name = os.getenv('NADO_SUBACCOUNT_NAME', 'default')
        
        # 产品缓存
        self.products: Dict[int, NadoProduct] = {}
        self.product_id_map: Dict[str, int] = {}
    
    def _run_node(self, script_content: str, timeout: int = 30) -> Dict:
        """运行 Node.js 脚本"""
        script_path = f'{self.NADO_DIR}/temp_python_script.mjs'
        
        with open(script_path, 'w') as f:
            f.write(script_content)
        
        try:
            # 设置 NODE_PATH 环境变量
            env = os.environ.copy()
            env['NODE_PATH'] = f'{self.NADO_DIR}/node_modules'
            
            result = subprocess.run(
                ['node', 'temp_python_script.mjs'],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.NADO_DIR,
                env=env
            )
            
            os.remove(script_path)
            
            if result.returncode != 0:
                error_info = {
                    'error': result.stderr or 'Unknown error',
                    'stdout': result.stdout,
                    'returncode': result.returncode
                }
                # Try to parse error from stdout if stderr is empty
                if not result.stderr and result.stdout:
                    lines = result.stdout.strip().split('\n')
                    for line in reversed(lines):
                        try:
                            parsed = json.loads(line)
                            if 'error' in parsed or 'success' in parsed:
                                error_info.update(parsed)
                                break
                        except json.JSONDecodeError:
                            continue
                return error_info
            
            # 解析最后一行 JSON
            lines = result.stdout.strip().split('\n')
            last_line = lines[-1]
            try:
                parsed = json.loads(last_line)
                # If the response indicates failure, include it as error
                if parsed.get('success') is False:
                    return {
                        'error': parsed.get('error', 'Unknown error'),
                        'code': parsed.get('code'),
                        'stdout': result.stdout
                    }
                return parsed
            except json.JSONDecodeError:
                # If no valid JSON, check if there's an error message
                if 'error' in result.stdout.lower() or 'Error' in result.stdout:
                    return {'error': result.stdout, 'stdout': result.stdout}
                return {'output': result.stdout}
                
        except subprocess.TimeoutExpired:
            try:
                os.remove(script_path)
            except:
                pass
            return {'error': 'Timeout'}
        except Exception as e:
            return {'error': str(e)}
    
    async def get_products(self) -> Dict:
        """获取所有产品"""
        script = f'''
import {{ createNadoClient }} from './node_modules/@nadohq/client/dist/index.js';
import {{ createPublicClient, createWalletClient, http }} from 'viem';
import {{ privateKeyToAccount }} from 'viem/accounts';
import {{ CHAIN_ENV_TO_CHAIN }} from './node_modules/@nadohq/shared/dist/index.js';

const account = privateKeyToAccount('{self.private_key}');
const chain = CHAIN_ENV_TO_CHAIN['inkMainnet'];

const walletClient = createWalletClient({{
  account,
  chain,
  transport: http('https://rpc-gel.inkonchain.com'),
}});

const publicClient = createPublicClient({{
  chain,
  transport: http('https://rpc-gel.inkonchain.com'),
}});

const client = createNadoClient('inkMainnet', {{
  walletClient,
  publicClient,
}});

const products = await client.context.engineClient.query('symbols', {{}});
console.log(JSON.stringify(products));
'''
        return self._run_node(script)
    
    async def get_order_book(self, product_id: int) -> Dict:
        """获取订单簿"""
        script = f'''
import {{ createNadoClient }} from './node_modules/@nadohq/client/dist/index.js';
import {{ createPublicClient, createWalletClient, http }} from 'viem';
import {{ privateKeyToAccount }} from 'viem/accounts';
import {{ CHAIN_ENV_TO_CHAIN }} from './node_modules/@nadohq/shared/dist/index.js';

const account = privateKeyToAccount('{self.private_key}');
const chain = CHAIN_ENV_TO_CHAIN['inkMainnet'];

const walletClient = createWalletClient({{
  account,
  chain,
  transport: http('https://rpc-gel.inkonchain.com'),
}});

const publicClient = createPublicClient({{
  chain,
  transport: http('https://rpc-gel.inkonchain.com'),
}});

const client = createNadoClient('inkMainnet', {{
  walletClient,
  publicClient,
}});

const orderbook = await client.context.engineClient.query('market_liquidity', {{
  product_id: {product_id},
  depth: 20,
}});
console.log(JSON.stringify(orderbook));
'''
        return self._run_node(script)
    
    async def get_subaccount(self) -> Dict:
        """获取账户信息"""
        script = f'''
import {{ createNadoClient }} from './node_modules/@nadohq/client/dist/index.js';
import {{ createPublicClient, createWalletClient, http }} from 'viem';
import {{ privateKeyToAccount }} from 'viem/accounts';
import {{ CHAIN_ENV_TO_CHAIN, subaccountToHex }} from './node_modules/@nadohq/shared/dist/index.js';

const walletOwner = privateKeyToAccount('{self.private_key}');
const chain = CHAIN_ENV_TO_CHAIN['inkMainnet'];

const walletClient = createWalletClient({{
  account: walletOwner,
  chain,
  transport: http('https://rpc-gel.inkonchain.com'),
}});

const publicClient = createPublicClient({{
  chain,
  transport: http('https://rpc-gel.inkonchain.com'),
}});

const client = createNadoClient('inkMainnet', {{
  walletClient,
  publicClient,
}});

const subaccountHex = subaccountToHex({{
  subaccountOwner: walletOwner.address,
  subaccountName: '{self.subaccount_name}',
}});

const subaccountData = await client.context.engineClient.query('subaccount_info', {{
  subaccount: subaccountHex,
}});
console.log(JSON.stringify(subaccountData));
'''
        return self._run_node(script)
    
    async def get_subaccount_info(self) -> Dict:
        """获取账户信息（get_subaccount 的别名）"""
        return await self.get_subaccount()
    
    async def place_order(self, product_id: int, side: str, 
                         price: Decimal, amount: Decimal,
                         order_type: str = 'default') -> Dict:
        """下单
        
        Args:
            product_id: 产品ID (2 = BTC-PERP)
            side: 'buy' or 'sell'
            price: 价格 (USD format, e.g., 78799.0)
            amount: 数量 (token数量)
            order_type: 'default' | 'ioc' | 'fok' | 'post_only'
        """
        import logging
        logger = logging.getLogger(__name__)
        
        # Log input price for debugging
        logger.info(f"📥 [Place Order Input] price={price}, type={type(price)}, float={float(price)}")
        
        # Convert to int first to ensure we have a clean number
        price_int = int(price)
        
        # Sanity check: BTC price should be 10k-200k USD
        if price_int < 10000 or price_int > 200000:
            logger.error(f"❌ [Price Check] Price seems unreasonable for BTC: {price_int} USD")
            raise ValueError(f"Price {price_int} seems unreasonable for BTC (expected 10k-200k USD)")
        
        # IMPORTANT: Pass USD price as number to SDK (matches user's trade.js)
        # SDK expects number type and handles Wei conversion internally
        amount_float = float(amount)
        
        logger.info(f"💰 [Price Format] USD price: {price_int} (number type)")
        logger.info(f"💰 [Amount Format] Amount: {amount_float} (will convert to Wei string in JS)")
        
        script = f'''
import {{ createNadoClient }} from './node_modules/@nadohq/client/dist/index.js';
import {{ createPublicClient, createWalletClient, http }} from 'viem';
import {{ privateKeyToAccount }} from 'viem/accounts';
import {{ CHAIN_ENV_TO_CHAIN, nowInSeconds, packOrderAppendix }} from './node_modules/@nadohq/shared/dist/index.js';

const account = privateKeyToAccount('{self.private_key}');
const chain = CHAIN_ENV_TO_CHAIN['inkMainnet'];

const walletClient = createWalletClient({{
  account,
  chain,
  transport: http('https://rpc-gel.inkonchain.com'),
}});

const publicClient = createPublicClient({{
  chain,
  transport: http('https://rpc-gel.inkonchain.com'),
}});

const client = createNadoClient('inkMainnet', {{
  walletClient,
  publicClient,
}});

// Amount - 转换为 Wei 字符串（确保是字符串，避免 BigInt 序列化问题）
const amountWei = BigInt(Math.floor({amount_float} * 1e18)).toString();
const amountValue = '{side}' === 'buy' ? amountWei : `-${{amountWei}}`;

// Price - 转换为字符串格式（避免 BigInt 序列化问题）
// SDK 内部会将字符串转换为 Wei，我们直接传递字符串避免序列化问题
const priceUsd = {price_int};  // number 类型
const priceUsdStr = String(priceUsd);  // 明确转换为字符串

// Log for debugging
console.log('Price USD:', priceUsdStr, 'Type:', typeof priceUsdStr);
console.log('Amount Wei:', amountValue, 'Type:', typeof amountValue);

// 确保所有参数都是可序列化的类型（字符串、数字，而不是 BigInt）
// packOrderAppendix 可能返回包含 BigInt 的对象，需要先序列化
let appendix;
try {{
  appendix = packOrderAppendix({{
    orderExecutionType: '{order_type}',
  }});
  // 如果 appendix 包含 BigInt，转换为字符串
  if (typeof appendix === 'bigint') {{
    appendix = appendix.toString();
  }} else if (appendix && typeof appendix === 'object') {{
    // 递归处理对象中的 BigInt
    appendix = JSON.parse(JSON.stringify(appendix, (key, value) => {{
      return typeof value === 'bigint' ? value.toString() : value;
    }}));
  }}
}} catch (e) {{
  console.error('Error packing appendix:', e);
  appendix = packOrderAppendix({{
    orderExecutionType: '{order_type}',
  }});
}}

const orderParams = {{
  subaccountName: '{self.subaccount_name}',
  expiration: nowInSeconds() + 60,
  appendix: appendix,
  price: priceUsdStr,  // 使用字符串格式，避免 BigInt 序列化问题
  amount: amountValue,  // 已经是字符串格式
}};

// Custom JSON serializer to handle BigInt
const bigIntReplacer = (key, value) => {{
  if (typeof value === 'bigint') {{
    return value.toString();
  }}
  return value;
}};

try {{
  const result = await client.market.placeOrder({{
    id: Date.now(),
    productId: {product_id},
    order: orderParams,
  }});
  // Use custom replacer to handle BigInt in result
  console.log(JSON.stringify({{success: true, data: result}}, bigIntReplacer));
}} catch (error) {{
  console.log(JSON.stringify({{success: false, error: error.message, code: error.errorCode}}, bigIntReplacer));
}}
'''
        return self._run_node(script)
    
    async def cancel_order(self, digest: str, product_id: int) -> Dict:
        """取消单个订单
        
        Args:
            digest: 订单 digest (0x...)
            product_id: 产品ID
        
        Returns:
            Dict: 取消结果
        """
        return await self.cancel_orders(digests=[digest], product_ids=[product_id])
    
    async def get_order_info(self, digest: str, product_id: int) -> Optional[Dict]:
        """查询订单状态
        
        Args:
            digest: 订单 digest
            product_id: 产品 ID
        
        Returns:
            dict: 订单信息，包含 status, digest 等字段，如果订单不存在则返回 None
        """
        script = f'''
import {{ createNadoClient }} from './node_modules/@nadohq/client/dist/index.js';
import {{ createPublicClient, createWalletClient, http }} from 'viem';
import {{ privateKeyToAccount }} from 'viem/accounts';
import {{ CHAIN_ENV_TO_CHAIN }} from './node_modules/@nadohq/shared/dist/index.js';
import {{ subaccountToHex }} from './node_modules/@nadohq/shared/dist/index.js';

const account = privateKeyToAccount('{self.private_key}');
const chain = CHAIN_ENV_TO_CHAIN['inkMainnet'];

const walletClient = createWalletClient({{
  account,
  chain,
  transport: http('https://rpc-gel.inkonchain.com'),
}});

const publicClient = createPublicClient({{
  chain,
  transport: http('https://rpc-gel.inkonchain.com'),
}});

const client = createNadoClient('inkMainnet', {{
  walletClient,
  publicClient,
}});

try {{
  const subaccountHex = subaccountToHex({{
    subaccountOwner: account.address,
    subaccountName: '{self.subaccount_name}',
  }});
  
  const senderPadded = account.address.toLowerCase().slice(2).padStart(64, '0');
  
  const orders = await client.context.engineClient.query('subaccount_orders', {{
    subaccount: subaccountHex,
    sender: '0x' + senderPadded,
    product_id: {product_id},
  }});
  
  // Find order by digest
  const order = orders?.orders?.find(o => o.digest === '{digest}');
  
  if (order) {{
    console.log(JSON.stringify({{success: true, data: order}}));
  }} else {{
    console.log(JSON.stringify({{success: false, data: null}}));
  }}
}} catch (error) {{
  console.log(JSON.stringify({{success: false, error: error.message}}));
}}
'''
        result = self._run_node(script)
        
        if isinstance(result, dict) and result.get('success') and result.get('data'):
            return result['data']
        return None
    
    async def cancel_orders(self, digests: List[str], product_ids: List[int]) -> Dict:
        """取消多个订单
        
        Args:
            digests: 订单 digest 列表
            product_ids: 产品ID列表
        
        Returns:
            Dict: 取消结果
        """
        script = f'''
import {{ createNadoClient }} from './node_modules/@nadohq/client/dist/index.js';
import {{ createPublicClient, createWalletClient, http }} from 'viem';
import {{ privateKeyToAccount }} from 'viem/accounts';
import {{ CHAIN_ENV_TO_CHAIN }} from './node_modules/@nadohq/shared/dist/index.js';

const account = privateKeyToAccount('{self.private_key}');
const chain = CHAIN_ENV_TO_CHAIN['inkMainnet'];

const walletClient = createWalletClient({{
  account,
  chain,
  transport: http('https://rpc-gel.inkonchain.com'),
}});

const publicClient = createPublicClient({{
  chain,
  transport: http('https://rpc-gel.inkonchain.com'),
}});

const client = createNadoClient('inkMainnet', {{
  walletClient,
  publicClient,
}});

try {{
  const result = await client.market.cancelOrders({{
    subaccountName: '{self.subaccount_name}',
    productIds: {json.dumps(product_ids)},
    digests: {json.dumps(digests)},
  }});
  console.log(JSON.stringify({{success: true, data: result}}));
}} catch (error) {{
  console.log(JSON.stringify({{success: false, error: error.message}}));
}}
'''
        return self._run_node(script)
    
    async def load_products(self):
        """加载产品缓存"""
        result = await self.get_products()
        
        # SDK 返回格式: {'symbols': {'USDT0': {...}, 'BTC-PERP': {...}}}
        if isinstance(result, dict) and 'symbols' in result:
            for symbol, p in result['symbols'].items():
                if isinstance(p, dict):
                    pid = p.get('product_id')
                    if pid:
                        self.products[pid] = NadoProduct(
                            product_id=pid,
                            symbol=symbol,
                            price_increment=str(p.get('price_increment_x18', '0.1')),
                            min_size=str(p.get('min_size', '0.001')),
                        )
                        self.product_id_map[symbol] = pid
        
        print(f"Loaded {len(self.products)} products")
    
    async def get_product_info(self, symbol: str) -> Optional[Dict]:
        """获取产品信息"""
        if not self.products:
            await self.load_products()
        
        pid = self.product_id_map.get(symbol)
        if pid:
            p = self.products.get(pid)
            if p:
                return {
                    'id': p.product_id,
                    'symbol': p.symbol,
                    'tickSize': p.price_increment,
                    'minSize': p.min_size,
                }
        return None


# 工具函数
def wei_to_usd(wei_value: int) -> Decimal:
    """Convert Wei (1e18) to USD"""
    return Decimal(str(wei_value)) / Decimal('1e18')


def usd_to_wei(usd_value: Decimal) -> int:
    """Convert USD to Wei (1e18)"""
    return int(usd_value * Decimal('1e18'))


# 测试函数
async def test():
    """测试 Nado 客户端"""
    print("=" * 60)
    print("🧪 Nado Python 客户端测试 (使用 @nadohq/client SDK)")
    print("=" * 60)
    
    client = NadoClient()
    
    # 1. 测试获取产品
    print("\n[1] 获取产品列表...")
    products = await client.get_products()
    
    if isinstance(products, dict) and 'error' in products:
        print(f"   ❌ 错误: {products['error'][:300]}")
        return
    
    print(f"   ✅ 获取到 {len(products)} 个产品")
    
    # 解析产品
    if isinstance(products, list):
        for p in products[:5]:
            if isinstance(p, dict):
                print(f"   - ID: {p.get('product_id')}, Symbol: {p.get('symbol')}")
    
    # 2. 测试获取订单簿
    print("\n[2] 获取 BTC-PERP 订单簿...")
    orderbook = await client.get_order_book(2)
    
    if isinstance(orderbook, dict) and 'error' in orderbook:
        print(f"   ❌ 错误: {orderbook['error'][:300]}")
    elif isinstance(orderbook, dict) and 'bids' in orderbook:
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        if bids and asks:
            best_bid = int(bids[0][0]) / 1e18
            best_ask = int(asks[0][0]) / 1e18
            print(f"   ✅ 买一: ${best_bid:,.2f}")
            print(f"   ✅ 卖一: ${best_ask:,.2f}")
            print(f"   ✅ 价差: ${best_ask - best_bid:,.2f}")
        else:
            print(f"   ⚠️ 订单簿数据为空")
    else:
        print(f"   ⚠️ 响应: {str(orderbook)[:200]}")
    
    # 3. 测试获取账户信息
    print("\n[3] 获取账户信息...")
    account = await client.get_subaccount()
    
    if isinstance(account, dict) and 'error' in account:
        print(f"   ❌ 错误: {account['error'][:300]}")
    elif isinstance(account, dict):
        print(f"   ✅ 账户响应: {str(account)[:200]}...")
    else:
        print(f"   ⚠️ 响应: {str(account)[:200]}")
    
    # 4. 加载产品缓存
    print("\n[4] 加载产品缓存...")
    await client.load_products()
    print(f"   缓存产品: {list(client.product_id_map.keys())[:5]}")
    
    print("\n" + "=" * 60)
    print("✅ 测试完成")
    print("=" * 60)


if __name__ == '__main__':
    asyncio.run(test())
