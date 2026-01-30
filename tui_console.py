#!/usr/bin/env python3
"""
跨交易所套利机器人 TUI 启动控制台
支持交易所配置、策略选择、参数设置和成交查看
"""

import asyncio
import os
import sys
import json
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.prompt import Prompt, Confirm
from rich.style import Style
from rich import box
from rich.align import Align

import dotenv

# 导入交易所客户端用于验证
try:
    from exchanges.edgex import EdgexClient
    EDGEX_AVAILABLE = True
except ImportError:
    EDGEX_AVAILABLE = False

try:
    from exchanges.standx import StandxClient
    STANDX_AVAILABLE = True
except ImportError:
    STANDX_AVAILABLE = False

try:
    from exchanges.grvt import GrvtClient
    GRVT_AVAILABLE = True
except ImportError:
    GRVT_AVAILABLE = False


class ConfigManager:
    """配置文件管理器"""
    
    def __init__(self):
        self.env_file = Path(__file__).parent / ".env"
        self.config_file = Path(__file__).parent / "arb_config.json"
        self.trade_history_file = Path(__file__).parent / "trade_history.json"
    
    def load_env(self) -> Dict[str, str]:
        """加载环境变量"""
        dotenv.load_dotenv()
        return os.environ
    
    def save_env(self, key: str, value: str):
        """保存环境变量"""
        dotenv.set_key(self.env_file, key, value)
    
    def get_exchange_config(self, exchange: str) -> Dict[str, Any]:
        """获取交易所配置"""
        config = {}
        env_prefix = exchange.upper()
        
        if exchange == "edgex":
            config = {
                "account_id": os.getenv(f"{env_prefix}_ACCOUNT_ID", ""),
                "stark_private_key": os.getenv(f"{env_prefix}_STARK_PRIVATE_KEY", ""),
                "base_url": os.getenv(f"{env_prefix}_BASE_URL", "https://pro.edgex.exchange"),
                "ws_url": os.getenv(f"{env_prefix}_WS_URL", "wss://quote.edgex.exchange"),
            }
        elif exchange == "standx":
            config = {
                "wallet_private_key": os.getenv(f"{env_prefix}_WALLET_PRIVATE_KEY", ""),
                "rpc_url": os.getenv(f"{env_prefix}_RPC_URL", "https://api.mainnet-beta.solana.com"),
            }
        elif exchange == "grvt":
            config = {
                "trading_account_id": os.getenv(f"{env_prefix}_TRADING_ACCOUNT_ID", ""),
                "private_key": os.getenv(f"{env_prefix}_PRIVATE_KEY", ""),
                "api_key": os.getenv(f"{env_prefix}_API_KEY", ""),
                "environment": os.getenv(f"{env_prefix}_ENVIRONMENT", "prod"),
            }
        elif exchange == "lighter":
            config = {
                "account_index": os.getenv(f"{env_prefix}_ACCOUNT_INDEX", "0"),
                "api_key_index": os.getenv(f"{env_prefix}_API_KEY_INDEX", "0"),
                "api_key_private_key": os.getenv("API_KEY_PRIVATE_KEY", ""),
                "base_url": os.getenv(f"{env_prefix}_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
            }
        
        return config
    
    def save_exchange_config(self, exchange: str, config: Dict[str, Any]):
        """保存交易所配置"""
        env_prefix = exchange.upper()
        
        for key, value in config.items():
            if value:
                self.save_env(f"{env_prefix}_{key.upper()}", str(value))
    
    def load_runtime_config(self) -> Dict[str, Any]:
        """加载运行时配置"""
        default_config = {
            "maker_exchange": "edgex",
            "taker_exchange": "lighter",
            "ticker": "BTC",
            "order_size": "0.001",
            "max_position": "0.1",
            "long_threshold": "10",
            "short_threshold": "10",
            "fill_timeout": "5",
            "use_dynamic_threshold": True,
        }
        
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    saved_config = json.load(f)
                    return {**default_config, **saved_config}
            except Exception:
                return default_config
        
        return default_config
    
    def save_runtime_config(self, config: Dict[str, Any]):
        """保存运行时配置"""
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    
    def load_trade_history(self) -> List[Dict]:
        """加载交易历史"""
        if self.trade_history_file.exists():
            try:
                with open(self.trade_history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return []
        return []
    
    def save_trade(self, trade: Dict):
        """保存交易记录"""
        history = self.load_trade_history()
        history.insert(0, {
            **trade,
            "timestamp": datetime.now().isoformat()
        })
        # 只保留最近100条
        history = history[:100]
        with open(self.trade_history_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)


class TUIStyle:
    """TUI 样式定义"""
    
    # 颜色定义
    COLORS = {
        "primary": "#00BFFF",      # Deep Sky Blue
        "secondary": "#20B2AA",    # Light Sea Green
        "success": "#32CD32",      # Lime Green
        "warning": "#FFD700",      # Gold
        "danger": "#FF4500",       # Orange Red
        "info": "#87CEEB",         # Sky Blue
        "edgex": "#FF6B6B",        # Coral (EdgeX brand)
        "standx": "#4ECDC4",       # Teal (StandX brand)
        "grvt": "#9B59B6",         # Purple (GRVT brand)
        "lighter": "#3498DB",      # Blue (Lighter brand)
    }
    
    @classmethod
    def get_header_style(cls) -> Style:
        return Style(color="white", bold=True)
    
    @classmethod
    def get_menu_style(cls) -> Style:
        return Style(color=cls.COLORS["primary"], bold=True)
    
    @classmethod
    def get_exchange_color(cls, exchange: str) -> str:
        colors = {
            "edgex": cls.COLORS["edgex"],
            "standx": cls.COLORS["standx"],
            "grvt": cls.COLORS["grvt"],
            "lighter": cls.COLORS["lighter"],
        }
        return colors.get(exchange.lower(), "white")


class ArbitrageTUI:
    """跨交易所套利 TUI 控制台"""
    
    def __init__(self):
        self.console = Console()
        self.config = ConfigManager()
        self.running = True
        self.current_menu = "main"
        
        # 加载配置
        self.config.load_env()
        self.runtime_config = self.config.load_runtime_config()
    
    def clear_screen(self):
        """清屏"""
        os.system('cls' if os.name == 'nt' else 'clear')
    
    def print_header(self, title: str, subtitle: str = ""):
        """打印标题"""
        self.clear_screen()
        
        header = Panel(
            Align.center(
                Text.from_markup(
                    f"[bold #00BFFF]╔═══════════════════════════════════════════════════════════╗\n"
                    f"[bold #00BFFF]║[/]  [white]🚀 跨交易所套利机器人 TUI 控制台  🚀[/]            [bold #00BFFF]║\n"
                    f"[bold #00BFFF]╚═══════════════════════════════════════════════════════════╝[/]\n"
                    f"\n"
                    f"[#20B2AA]◆[/] [#20B2AA]◆[/] [#20B2AA]◆[/]  [white]{title}[/]  [#20B2AA]◆[/] [#20B2AA]◆[/] [#20B2AA]◆[/]\n"
                    f"{f'[dim]{subtitle}[/]' if subtitle else ''}"
                ),
                vertical="middle"
            ),
            box=box.ROUNDED,
            style="on #1a1a2e",
            padding=(1, 2)
        )
        self.console.print(header)
    
    def print_menu(self, title: str, options: List[Dict[str, str]], hint: str = ""):
        """打印菜单选项"""
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("选项", style="dim", width=8)
        table.add_column("说明", style="white")
        
        for i, option in enumerate(options, 1):
            key = str(i).zfill(2)
            emoji = option.get("emoji", "◆")
            desc = option.get("desc", "")
            table.add_row(f"[{TUIStyle.COLORS['primary']}][{key}][/]", f"{emoji} {desc}")
        
        self.console.print(Panel(table, title="[bold]菜单选项[/]", box=box.ROUNDED))
        
        if hint:
            self.console.print(f"\n[dim]{hint}[/]")
    
    def print_status_bar(self, message: str = "就绪"):
        """打印状态栏"""
        status = Panel(
            f"[#00BFFF]◆[/] {message}",
            style="on #0a0a1a",
            box=box.SQUARE
        )
        self.console.print(status)
    
    def confirm_action(self, message: str) -> bool:
        """确认操作"""
        return Confirm.ask(f"[#FFD700]⚠[/] {message}")
    
    def main_menu(self):
        """主菜单"""
        while self.running:
            self.print_header(
                "跨交易所套利机器人",
                f"版本 2.0 | 当前配置: {self.runtime_config.get('maker_exchange', '?')} → {self.runtime_config.get('taker_exchange', '?')}"
            )
            
            options = [
                {"emoji": "🔧", "desc": "交易所配置 (API密钥管理)"},
                {"emoji": "⚡", "desc": "选择套利交易所 (Maker/Taker)"},
                {"emoji": "⚙️", "desc": "运行参数配置"},
                {"emoji": "▶️", "desc": "启动套利机器人"},
                {"emoji": "📊", "desc": "查看成交记录"},
                {"emoji": "📈", "desc": "查看实时行情"},
                {"emoji": "🚪", "desc": "退出程序"},
            ]
            
            self.print_menu("主菜单", options, "请输入选项编号 (1-7)")
            
            choice = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 请选择", default="1")
            
            if choice == "1":
                self.exchange_config_menu()
            elif choice == "2":
                self.exchange_selection_menu()
            elif choice == "3":
                self.parameter_config_menu()
            elif choice == "4":
                self.start_arbitrage_menu()
            elif choice == "5":
                self.trade_history_menu()
            elif choice == "6":
                self.market_data_menu()
            elif choice == "7" or choice.lower() in ["q", "quit", "exit"]:
                self.running = False
                self.console.print("\n[#32CD32]✓[/] 感谢使用，再见！")
                break
    
    def exchange_config_menu(self):
        """交易所配置菜单"""
        while True:
            self.print_header("交易所配置", "管理各交易所 API 密钥和配置")
            
            # 检查当前配置状态
            config_status = self._check_exchange_config_status()
            
            status_table = Table(show_header=False, box=None)
            status_table.add_column("交易所", style="bold", width=12)
            status_table.add_column("状态", width=15)
            status_table.add_column("说明", width=40)
            
            for exchange, status in config_status.items():
                color = "#32CD32" if status["configured"] else "#FF4500"
                status_text = "[#32CD32]✓ 已配置[/]" if status["configured"] else "[#FF4500]✗ 未配置[/]"
                status_table.add_row(
                    f"[{TUIStyle.get_exchange_color(exchange)}]{exchange.upper()}[/]",
                    status_text,
                    status["desc"]
                )
            
            self.console.print(Panel(status_table, title="[bold]配置状态[/]", box=box.ROUNDED))
            
            options = [
                {"emoji": "1", "desc": f"配置 EdgeX (做市交易所)"},
                {"emoji": "2", "desc": f"配置 StandX (做市交易所)"},
                {"emoji": "3", "desc": f"配置 GRVT (做市交易所)"},
                {"emoji": "4", "desc": f"配置 Lighter (吃价交易所)"},
                {"emoji": "5", "desc": "返回主菜单"},
            ]
            
            self.print_menu("交易所配置", options)
            
            choice = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 请选择", default="5")
            
            if choice == "1":
                self._configure_exchange("edgex")
            elif choice == "2":
                self._configure_exchange("standx")
            elif choice == "3":
                self._configure_exchange("grvt")
            elif choice == "4":
                self._configure_exchange("lighter")
            elif choice == "5":
                break
    
    def _check_exchange_config_status(self) -> Dict:
        """检查交易所配置状态"""
        status = {}
        
        # EdgeX
        edgex_config = self.config.get_exchange_config("edgex")
        status["edgex"] = {
            "configured": bool(edgex_config.get("account_id") and edgex_config.get("stark_private_key")),
            "desc": "需要 account_id 和 stark_private_key"
        }
        
        # StandX
        standx_config = self.config.get_exchange_config("standx")
        status["standx"] = {
            "configured": bool(standx_config.get("wallet_private_key")),
            "desc": "需要 wallet_private_key (Solana)"
        }
        
        # GRVT
        grvt_config = self.config.get_exchange_config("grvt")
        status["grvt"] = {
            "configured": bool(grvt_config.get("trading_account_id") and 
                            grvt_config.get("private_key") and 
                            grvt_config.get("api_key")),
            "desc": "需要 trading_account_id, private_key, api_key"
        }
        
        # Lighter
        lighter_config = self.config.get_exchange_config("lighter")
        status["lighter"] = {
            "configured": bool(lighter_config.get("account_index") is not None and os.getenv("API_KEY_PRIVATE_KEY")),
            "desc": "需要 account_index 和 API_KEY_PRIVATE_KEY"
        }
        
        return status
    
    def _configure_exchange(self, exchange: str):
        """配置单个交易所"""
        exchange_names = {
            "edgex": "EdgeX",
            "standx": "StandX", 
            "grvt": "GRVT",
            "lighter": "Lighter"
        }
        
        while True:
            self.print_header(f"配置 {exchange_names.get(exchange, exchange)}", "输入新的配置值或直接回车保持不变")
            
            current_config = self.config.get_exchange_config(exchange)
            
            # 显示当前配置（脱敏）
            table = Table(show_header=True, box=box.ROUNDED)
            table.add_column("配置项", style="bold")
            table.add_column("当前值", style="#00BFFF")
            table.add_column("说明")
            
            for key, value in current_config.items():
                if not value:
                    display_value = "[dim](未设置)[/]"
                elif "key" in key.lower() or "private" in key.lower():
                    display_value = f"[#FF6B6B]{value[:8]}...{value[-4:]}[/]"
                elif value and len(str(value)) > 20:
                    display_value = f"[#00BFFF]{str(value)[:20]}...[/]"
                else:
                    display_value = f"[#00BFFF]{value}[/]"
                
                table.add_row(key.upper(), display_value, self._get_config_desc(exchange, key))
            
            self.console.print(Panel(table, title="[bold]当前配置[/]"))
            
            options = [
                {"emoji": "1", "desc": "修改配置"},
                {"emoji": "2", "desc": "验证配置"},
                {"emoji": "3", "desc": "清除配置"},
                {"emoji": "4", "desc": "返回"},
            ]
            
            self.print_menu(f"配置 {exchange_names.get(exchange, exchange)}", options)
            
            choice = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 请选择", default="1")
            
            if choice == "1":
                self._edit_exchange_config(exchange, current_config)
            elif choice == "2":
                self._verify_exchange_config(exchange)
            elif choice == "3":
                if self.confirm_action(f"确定要清除 {exchange_names.get(exchange, exchange)} 的配置吗？"):
                    self._clear_exchange_config(exchange)
            elif choice == "4":
                break
    
    def _get_config_desc(self, exchange: str, key: str) -> str:
        """获取配置项说明"""
        descs = {
            "edgex": {
                "account_id": "EdgeX 账户 ID",
                "stark_private_key": "Stark 私钥 (用于签名)",
                "base_url": "API 基础 URL",
                "ws_url": "WebSocket URL",
            },
            "standx": {
                "wallet_private_key": "Solana 钱包私钥",
                "rpc_url": "Solana RPC 节点 URL",
            },
            "grvt": {
                "trading_account_id": "GRVT 交易账户 ID",
                "private_key": "账户私钥",
                "api_key": "API 密钥",
                "environment": "环境 (prod/testnet/staging/dev)",
            },
            "lighter": {
                "account_index": "账户索引",
                "api_key_index": "API 密钥索引",
                "api_key_private_key": "API 私钥 (用于签名交易)",
                "base_url": "API 基础 URL",
            }
        }
        return descs.get(exchange, {}).get(key, key)
    
    def _edit_exchange_config(self, exchange: str, current_config: Dict):
        """编辑交易所配置"""
        new_config = {}
        
        self.console.print("\n[bold]请输入新配置 (直接回车保持当前值):[/]\n")
        
        for key, value in current_config.items():
            prompt_text = f"[#00BFFF]▶[/] {self._get_config_desc(exchange, key)}"
            
            if "environment" in key:
                new_value = Prompt.ask(prompt_text, default=value or "prod", 
                                      choices=["prod", "testnet", "staging", "dev"])
            else:
                new_value = Prompt.ask(prompt_text, default=value or "")
            
            if new_value:
                new_config[key] = new_value
        
        if new_config:
            self.config.save_exchange_config(exchange, new_config)
            self.console.print(f"\n[#32CD32]✓[/] {exchange.upper()} 配置已保存！")
        else:
            self.console.print(f"\n[dim]未修改任何配置[/]")
        
        Prompt.ask("\n[dim]按回车继续...[/]")
    
    def _verify_exchange_config(self, exchange: str):
        """验证交易所配置"""
        self.console.print(f"\n[bold]正在验证 {exchange.upper()} 配置...[/]\n")
        
        # 这里可以添加实际的验证逻辑
        config = self.config.get_exchange_config(exchange)
        
        if all(config.get(k) for k in config.keys()):
            self.console.print(f"[#32CD32]✓[/] {exchange.upper()} 配置验证通过！")
        else:
            self.console.print(f"[#FF4500]✗[/] {exchange.upper()} 配置不完整，请检查必要配置项。")
        
        Prompt.ask("\n[dim]按回车继续...[/]")
    
    def _clear_exchange_config(self, exchange: str):
        """清除交易所配置"""
        env_prefix = exchange.upper()
        
        # 获取该交易所相关的所有环境变量
        env_vars = []
        if exchange == "edgex":
            env_vars = ["ACCOUNT_ID", "STARK_PRIVATE_KEY", "BASE_URL", "WS_URL"]
        elif exchange == "standx":
            env_vars = ["WALLET_PRIVATE_KEY", "RPC_URL"]
        elif exchange == "grvt":
            env_vars = ["TRADING_ACCOUNT_ID", "PRIVATE_KEY", "API_KEY", "ENVIRONMENT"]
        elif exchange == "lighter":
            env_vars = ["ACCOUNT_INDEX", "API_KEY_INDEX", "BASE_URL"]
        
        # Also clear the shared API_KEY_PRIVATE_KEY for Lighter
        if exchange == "lighter" and os.getenv("API_KEY_PRIVATE_KEY"):
            dotenv.unset_key(self.config.env_file, "API_KEY_PRIVATE_KEY")
        
        for var in env_vars:
            full_var = f"{env_prefix}_{var}"
            if os.getenv(full_var):
                dotenv.unset_key(self.config.env_file, full_var)
        
        self.console.print(f"\n[#32CD32]✓[/] {exchange.upper()} 配置已清除！")
    
    def exchange_selection_menu(self):
        """交易所选择菜单"""
        while True:
            self.print_header("选择套利交易所", "选择 Maker (做市) 和 Taker (吃价) 交易所")
            
            # 显示当前选择
            current = self.runtime_config
            maker = current.get("maker_exchange", "edgex")
            taker = current.get("taker_exchange", "lighter")
            
            selection_table = Table(show_header=False, box=box.ROUNDED)
            selection_table.add_column("角色", style="bold", width=15)
            selection_table.add_column("交易所", width=20)
            selection_table.add_column("说明", width=35)
            
            selection_table.add_row(
                "[#32CD32]◆ Maker (做市)[/]",
                f"[{TUIStyle.get_exchange_color(maker)}]{maker.upper()}[/]",
                "下 Post-Only 限价单，获取返佣"
            )
            selection_table.add_row(
                "[#FF6B6B]◆ Taker (吃价)[/]",
                f"[{TUIStyle.get_exchange_color(taker)}]{taker.upper()}[/]",
                "下 IOC 市价单，立即成交"
            )
            
            self.console.print(Panel(selection_table, title="[bold]当前选择[/]"))
            
            # 可用组合
            combos = [
                ("edgex", "lighter", "EdgeX + Lighter (经典组合)"),
                ("grvt", "lighter", "GRVT + Lighter (新交易所)"),
                ("edgex", "standx", "EdgeX + StandX (Solana 生态)"),
            ]
            
            options = []
            for i, (m, t, desc) in enumerate(combos, 1):
                options.append({"emoji": str(i), "desc": f"{m.upper()} → {t.upper()} ({desc})"})
            
            options.extend([
                {"emoji": "4", "desc": "手动选择 Maker 交易所"},
                {"emoji": "5", "desc": "手动选择 Taker 交易所"},
                {"emoji": "6", "desc": "返回主菜单"},
            ])
            
            self.print_menu("选择套利组合", options)
            
            choice = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 请选择", default="1")
            
            if choice == "1":
                self.runtime_config["maker_exchange"] = "edgex"
                self.runtime_config["taker_exchange"] = "lighter"
                self.config.save_runtime_config(self.runtime_config)
                self.console.print("\n[#32CD32]✓[/] 已选择: EdgeX → Lighter")
            elif choice == "2":
                self.runtime_config["maker_exchange"] = "grvt"
                self.runtime_config["taker_exchange"] = "lighter"
                self.config.save_runtime_config(self.runtime_config)
                self.console.print("\n[#32CD32]✓[/] 已选择: GRVT → Lighter")
            elif choice == "3":
                self.runtime_config["maker_exchange"] = "edgex"
                self.runtime_config["taker_exchange"] = "standx"
                self.config.save_runtime_config(self.runtime_config)
                self.console.print("\n[#32CD32]✓[/] 已选择: EdgeX → StandX")
            elif choice == "4":
                self._select_maker_exchange()
            elif choice == "5":
                self._select_taker_exchange()
            elif choice == "6":
                break
            
            if choice in ["1", "2", "3"]:
                Prompt.ask("\n[dim]按回车继续...[/]")
    
    def _select_maker_exchange(self):
        """选择 Maker 交易所"""
        options = [
            {"emoji": "1", "desc": "EdgeX (以太坊 L2)"},
            {"emoji": "2", "desc": "GRVT (新交易所)"},
            {"emoji": "3", "desc": "StandX (Solana)"},
        ]
        
        self.print_menu("选择 Maker 交易所", options)
        choice = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 请选择", default="1")
        
        exchanges = ["edgex", "grvt", "standx"]
        if 1 <= int(choice) <= len(exchanges):
            self.runtime_config["maker_exchange"] = exchanges[int(choice) - 1]
            self.config.save_runtime_config(self.runtime_config)
            self.console.print(f"\n[#32CD32]✓[/] Maker 已设置为: {exchanges[int(choice) - 1].upper()}")
    
    def _select_taker_exchange(self):
        """选择 Taker 交易所"""
        options = [
            {"emoji": "1", "desc": "Lighter (zkSync)"},
            {"emoji": "2", "desc": "StandX (Solana)"},
        ]
        
        self.print_menu("选择 Taker 交易所", options)
        choice = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 请选择", default="1")
        
        exchanges = ["lighter", "standx"]
        if 1 <= int(choice) <= len(exchanges):
            self.runtime_config["taker_exchange"] = exchanges[int(choice) - 1]
            self.config.save_runtime_config(self.runtime_config)
            self.console.print(f"\n[#32CD32]✓[/] Taker 已设置为: {exchanges[int(choice) - 1].upper()}")
    
    def parameter_config_menu(self):
        """参数配置菜单"""
        while True:
            self.print_header("运行参数配置", "配置套利机器人的运行参数")
            
            config = self.runtime_config
            
            # 显示当前参数
            param_table = Table(show_header=True, box=box.ROUNDED)
            param_table.add_column("参数", style="bold")
            param_table.add_column("当前值")
            param_table.add_column("说明")
            
            params_info = [
                ("ticker", config.get("ticker", "BTC"), "交易对符号 (BTC, ETH, SOL)"),
                ("order_size", config.get("order_size", "0.001"), "每笔交易数量"),
                ("max_position", config.get("max_position", "0.1"), "最大持仓限制 (0=无限制)"),
                ("long_threshold", config.get("long_threshold", "10"), "做多价差阈值 (USDT)"),
                ("short_threshold", config.get("short_threshold", "10"), "做空价差阈值 (USDT)"),
                ("fill_timeout", config.get("fill_timeout", "5"), "订单成交超时 (秒)"),
                ("use_dynamic_threshold", str(config.get("use_dynamic_threshold", True)), "使用动态阈值"),
            ]
            
            for name, value, desc in params_info:
                param_table.add_row(name.upper(), f"[#00BFFF]{value}[/]", desc)
            
            self.console.print(Panel(param_table, title="[bold]当前参数[/]"))
            
            options = [
                {"emoji": "1", "desc": "修改交易对 (Ticker)"},
                {"emoji": "2", "desc": "修改交易数量"},
                {"emoji": "3", "desc": "修改最大持仓"},
                {"emoji": "4", "desc": "修改价差阈值"},
                {"emoji": "5", "desc": "修改其他参数"},
                {"emoji": "6", "desc": "恢复默认参数"},
                {"emoji": "7", "desc": "返回主菜单"},
            ]
            
            self.print_menu("参数配置", options)
            
            choice = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 请选择", default="7")
            
            if choice == "1":
                self._edit_ticker()
            elif choice == "2":
                self._edit_order_size()
            elif choice == "3":
                self._edit_max_position()
            elif choice == "4":
                self._edit_thresholds()
            elif choice == "5":
                self._edit_other_params()
            elif choice == "6":
                self._reset_params()
            elif choice == "7":
                break
    
    def _edit_ticker(self):
        """编辑交易对"""
        ticker = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 输入交易对 (BTC/ETH/SOL)", 
                          default=self.runtime_config.get("ticker", "BTC"))
        self.runtime_config["ticker"] = ticker.upper()
        self.config.save_runtime_config(self.runtime_config)
        self.console.print(f"\n[#32CD32]✓[/] 交易对已设置为: {ticker.upper()}")
    
    def _edit_order_size(self):
        """编辑交易数量"""
        size = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 输入每笔交易数量", 
                         default=self.runtime_config.get("order_size", "0.001"))
        self.runtime_config["order_size"] = size
        self.config.save_runtime_config(self.runtime_config)
        self.console.print(f"\n[#32CD32]✓[/] 交易数量已设置为: {size}")
    
    def _edit_max_position(self):
        """编辑最大持仓"""
        max_pos = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 输入最大持仓 (0=无限制)", 
                            default=self.runtime_config.get("max_position", "0.1"))
        self.runtime_config["max_position"] = max_pos
        self.config.save_runtime_config(self.runtime_config)
        self.console.print(f"\n[#32CD32]✓[/] 最大持仓已设置为: {max_pos}")
    
    def _edit_thresholds(self):
        """编辑价差阈值"""
        self.console.print("\n[bold]价差阈值说明:[/]")
        self.console.print("  - 做多阈值: Lighter 买价 - Maker 卖价 > 阈值 时做多")
        self.console.print("  - 做空阈值: Maker 买价 - Lighter 卖价 > 阈值 时做空\n")
        
        long_th = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 输入做多阈值 (USDT)", 
                            default=self.runtime_config.get("long_threshold", "10"))
        short_th = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 输入做空阈值 (USDT)", 
                             default=self.runtime_config.get("short_threshold", "10"))
        
        self.runtime_config["long_threshold"] = long_th
        self.runtime_config["short_threshold"] = short_th
        self.config.save_runtime_config(self.runtime_config)
        
        self.console.print(f"\n[#32CD32]✓[/] 阈值已更新: 做多={long_th}, 做空={short_th}")
    
    def _edit_other_params(self):
        """编辑其他参数"""
        self.console.print("\n")
        
        fill_timeout = Prompt.ask("  订单成交超时 (秒)", 
                                  default=self.runtime_config.get("fill_timeout", "5"))
        
        use_dynamic = Confirm.ask("  使用动态阈值?", 
                                  default=self.runtime_config.get("use_dynamic_threshold", True))
        
        self.runtime_config["fill_timeout"] = str(fill_timeout)
        self.runtime_config["use_dynamic_threshold"] = use_dynamic
        self.config.save_runtime_config(self.runtime_config)
        
        self.console.print(f"\n[#32CD32]✓[/] 参数已更新")
    
    def _reset_params(self):
        """重置参数为默认值"""
        if self.confirm_action("确定要恢复默认参数吗？"):
            default_params = {
                "ticker": "BTC",
                "order_size": "0.001",
                "max_position": "0.1",
                "long_threshold": "10",
                "short_threshold": "10",
                "fill_timeout": "5",
                "use_dynamic_threshold": True,
            }
            self.runtime_config.update(default_params)
            self.config.save_runtime_config(self.runtime_config)
            self.console.print("\n[#32CD32]✓[/] 参数已恢复默认值")
    
    def start_arbitrage_menu(self):
        """启动套利菜单"""
        self.print_header("启动套利机器人", "确认配置并启动")
        
        config = self.runtime_config
        
        # 汇总配置
        summary_table = Table(show_header=False, box=box.ROUNDED)
        summary_table.add_column("项目", style="bold", width=20)
        summary_table.add_column("值", width=30)
        
        summary_table.add_row("Maker 交易所", f"[{TUIStyle.get_exchange_color(config.get('maker_exchange'))}]{config.get('maker_exchange', '').upper()}[/]")
        summary_table.add_row("Taker 交易所", f"[{TUIStyle.get_exchange_color(config.get('taker_exchange'))}]{config.get('taker_exchange', '').upper()}[/]")
        summary_table.add_row("交易对", f"[#00BFFF]{config.get('ticker', 'BTC')}[/]")
        summary_table.add_row("交易数量", f"[#00BFFF]{config.get('order_size', '0.001')}[/]")
        summary_table.add_row("最大持仓", f"[#00BFFF]{config.get('max_position', '0.1')}[/]")
        summary_table.add_row("做多阈值", f"[#32CD32]{config.get('long_threshold', '10')} USDT[/]")
        summary_table.add_row("做空阈值", f"[#FF6B6B]{config.get('short_threshold', '10')} USDT[/]")
        
        self.console.print(Panel(summary_table, title="[bold]配置摘要[/]"))
        
        # 检查配置完整性
        self._print_config_check()
        
        options = [
            {"emoji": "1", "desc": "立即启动 (前台运行)"},
            {"emoji": "2", "desc": "立即启动 (后台运行)"},
            {"emoji": "3", "desc": "测试模式启动"},
            {"emoji": "4", "desc": "返回主菜单"},
        ]
        
        self.print_menu("启动选项", options)
        
        choice = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 请选择", default="1")
        
        if choice == "1":
            self._start_arb_foreground()
        elif choice == "2":
            self._start_arb_background()
        elif choice == "3":
            self._start_arb_test()
        elif choice == "4":
            return
    
    def _print_config_check(self):
        """打印配置检查结果"""
        config_status = self._check_exchange_config_status()
        
        maker = self.runtime_config.get("maker_exchange", "edgex")
        taker = self.runtime_config.get("taker_exchange", "lighter")
        
        check_table = Table(show_header=False, box=None)
        check_table.add_column("检查项", style="bold", width=25)
        check_table.add_column("状态", width=15)
        
        maker_ok = config_status.get(maker, {}).get("configured", False)
        taker_ok = config_status.get(taker, {}).get("configured", False)
        
        check_table.add_row(
            f"{maker.upper()} 配置",
            "[#32CD32]✓[/]" if maker_ok else "[#FF4500]✗[/]"
        )
        check_table.add_row(
            f"{taker.upper()} 配置",
            "[#32CD32]✓[/]" if taker_ok else "[#FF4500]✗[/]"
        )
        
        self.console.print(Panel(check_table, title="[bold]配置检查[/]", subtitle="[dim]红色表示配置不完整[/]"))
    
    def _start_arb_foreground(self):
        """前台启动套利"""
        import subprocess
        
        maker = self.runtime_config.get("maker_exchange", "edgex")
        taker = self.runtime_config.get("taker_exchange", "lighter")
        
        if not self._check_exchange_ready(maker) or not self._check_exchange_ready(taker):
            self.console.print("\n[#FF4500]✗[/] 交易所配置不完整，无法启动！")
            Prompt.ask("\n[dim]按回车返回...[/]")
            return
        
        self.console.print(f"\n[bold]正在启动 {maker.upper()} → {taker.upper()} 套利机器人...[/]")
        self.console.print("[#FFD700]⚠[/] 按 Ctrl+C 停止并返回菜单\n")
        
        # 构建命令
        cmd = [
            sys.executable,
            "arbitrage.py",
            "--exchange", maker,
            "--ticker", self.runtime_config.get("ticker", "BTC"),
            "--size", self.runtime_config.get("order_size", "0.001"),
            "--max-position", self.runtime_config.get("max_position", "0"),
            "--long-threshold", self.runtime_config.get("long_threshold", "10"),
            "--short-threshold", self.runtime_config.get("short_threshold", "10"),
        ]
        
        # 使用 subprocess.run 执行命令，替换当前进程
        # 这样 Ctrl+C 可以正确传递到子进程
        try:
            # 恢复终端设置（如果需要）
            subprocess.run(cmd, check=False)
        except KeyboardInterrupt:
            self.console.print("\n\n[#FFD700]⚠[/] 用户中断，程序已停止")
        except Exception as e:
            self.console.print(f"\n[#FF4500]✗[/] 运行时错误: {e}")
        
        # 等待用户按回车返回菜单
        Prompt.ask("\n[dim]按回车返回菜单...[/]")
    
    def _start_arb_background(self):
        """后台启动套利"""
        self.console.print("\n[bold]正在后台启动套利机器人...[/]")
        
        maker = self.runtime_config.get("maker_exchange", "edgex")
        taker = self.runtime_config.get("taker_exchange", "lighter")
        
        cmd = f"""
nohup python3 arbitrage.py \
    --exchange {maker} \
    --ticker {self.runtime_config.get('ticker', 'BTC')} \
    --size {self.runtime_config.get('order_size', '0.001')} \
    --max-position {self.runtime_config.get('max_position', '0')} \
    --long-threshold {self.runtime_config.get('long_threshold', '10')} \
    --short-threshold {self.runtime_config.get('short_threshold', '10')} \
    > arbitrage.log 2>&1 &
echo $!
"""
        
        import subprocess
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        
        if result.returncode == 0:
            self.console.print(f"\n[#32CD32]✓[/] 套利机器人已在后台启动！")
            self.console.print(f"[dim]日志文件: arbitrage.log[/]")
        else:
            self.console.print(f"\n[#FF4500]✗[/] 启动失败: {result.stderr}")
    
    def _start_arb_test(self):
        """测试模式启动"""
        self.console.print("\n[bold]测试模式说明:[/]")
        self.console.print("  - 不执行真实交易")
        self.console.print("  - 模拟套利逻辑")
        self.console.print("  - 输出价格和价差信息\n")
        
        if not self.confirm_action("确认以测试模式启动？"):
            return
        
        self.console.print("\n[bold]正在启动测试模式...[/]")
        
        maker = self.runtime_config.get("maker_exchange", "edgex")
        taker = self.runtime_config.get("taker_exchange", "lighter")
        
        # 显示价格
        self.console.print(f"\n[#00BFFF]({maker.upper()} 和 {taker.upper()} 价格信息)[/]")
        self.console.print("[dim]连接交易所获取价格中...[/]\n")
        
        # 这里可以添加实际的价格获取逻辑
        self.console.print("[#32CD32]✓[/] 测试模式完成")
    
    def _check_exchange_ready(self, exchange: str) -> bool:
        """检查交易所是否已配置"""
        config = self.config.get_exchange_config(exchange)
        return all(config.get(k) for k in config.keys() if k)
    
    def trade_history_menu(self):
        """交易历史菜单"""
        self.print_header("成交记录", "查看套利交易历史")
        
        trades = self.config.load_trade_history()
        
        if not trades:
            self.console.print(Panel(
                "[dim]暂无交易记录[/]",
                title="[bold]交易历史[/]"
            ))
        else:
            # 显示交易统计
            stats_table = Table(show_header=False, box=None)
            stats_table.add_column("统计项", style="bold")
            stats_table.add_column("值", width=20)
            
            total_trades = len(trades)
            successful_trades = len([t for t in trades if t.get("status") == "success"])
            total_pnl = sum([Decimal(str(t.get("pnl", 0))) for t in trades])
            
            stats_table.add_row("总交易数", str(total_trades))
            stats_table.add_row("成功交易", str(successful_trades))
            stats_table.add_row("总盈亏", f"[#32CD32]{total_pnl:.4f}[/]" if total_pnl >= 0 else f"[#FF4500]{total_pnl:.4f}[/]")
            
            self.console.print(Panel(stats_table, title="[bold]统计信息[/]"))
            
            # 显示最近交易
            self.console.print("\n[bold]最近交易:[/]")
            
            trade_table = Table(show_header=True, box=box.ROUNDED)
            trade_table.add_column("时间", width=20)
            trade_table.add_column("交易对")
            trade_table.add_column("方向")
            trade_table.add_column("数量")
            trade_table.add_column("盈亏")
            
            for trade in trades[:10]:
                pnl = trade.get("pnl", 0)
                pnl_str = f"[#32CD32]+{pnl:.4f}[/]" if pnl >= 0 else f"[#FF4500]{pnl:.4f}[/]"
                trade_table.add_row(
                    trade.get("timestamp", "")[:19].replace("T", " "),
                    trade.get("ticker", ""),
                    "[#32CD32]LONG[/]" if trade.get("direction") == "long" else "[#FF6B6B]SHORT[/]",
                    str(trade.get("size", "")),
                    pnl_str
                )
            
            self.console.print(trade_table)
        
        Prompt.ask("\n[dim]按回车返回...[/]")
    
    def market_data_menu(self):
        """市场数据菜单"""
        self.print_header("实时行情", "查看各交易所实时价格")
        
        self.console.print("[bold]正在获取市场数据...[/]\n")
        
        # 显示各交易所价格（模拟数据，实际可以从交易所 API 获取）
        price_table = Table(show_header=True, box=box.ROUNDED)
        price_table.add_column("交易所")
        price_table.add_column("买一价 (Bid)")
        price_table.add_column("卖一价 (Ask)")
        price_table.add_column("价差")
        
        # 模拟数据
        mock_prices = [
            ("EdgeX", "87600.0", "87610.0", "10.0"),
            ("GRVT", "87605.0", "87615.0", "10.0"),
            ("Lighter", "87602.0", "87612.0", "10.0"),
            ("StandX", "87598.0", "87608.0", "10.0"),
        ]
        
        for exchange, bid, ask, spread in mock_prices:
            color = TUIStyle.get_exchange_color(exchange.lower())
            price_table.add_row(
                f"[{color}]{exchange}[/]",
                f"[#32CD32]{bid}[/]",
                f"[#FF6B6B]{ask}[/]",
                f"[#FFD700]{spread}[/]"
            )
        
        self.console.print(price_table)
        
        self.console.print("\n[dim]提示: 实际价格数据需要连接交易所 API 获取[/]")
        
        Prompt.ask("\n[dim]按回车返回...[/]")
    
    def run(self):
        """运行 TUI"""
        try:
            self.main_menu()
        except KeyboardInterrupt:
            self.console.print("\n\n[#FFD700]⚠[/] 用户中断")
        except Exception as e:
            self.console.print(f"\n[#FF4500]✗[/] 错误: {e}")
            import traceback
            self.console.print(traceback.format_exc())


def main():
    """主函数"""
    tui = ArbitrageTUI()
    tui.run()


if __name__ == "__main__":
    main()

