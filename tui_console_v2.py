#!/usr/bin/env python3
"""
跨交易所套利机器人 TUI 控制台 V2.0
支持多机器人同时运行、Dashboard 状态展示、日志查看
"""

import asyncio
import os
import sys
import json
import subprocess
import signal
import time
import psutil
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.prompt import Prompt, Confirm
from rich.style import Style
from rich import box
from rich.align import Align
from rich.layout import Layout
from rich.live import Live
from rich.console import Group
from rich.columns import Columns

import dotenv


class RobotStatus(Enum):
    """机器人状态"""
    STOPPED = "stopped"
    RUNNING = "running"
    STARTING = "starting"
    ERROR = "error"


@dataclass
class RobotConfig:
    """机器人配置"""
    maker_exchange: str
    taker_exchange: str
    ticker: str
    order_size: str
    max_position: str
    long_threshold: str
    short_threshold: str
    fill_timeout: str = "5"


@dataclass
class RobotInstance:
    """运行中的机器人实例"""
    id: str
    config: RobotConfig
    status: RobotStatus = RobotStatus.STOPPED
    pid: Optional[int] = None
    start_time: Optional[datetime] = None
    last_update: datetime = field(default_factory=datetime.now)
    log_lines: List[str] = field(default_factory=list)
    stats: Dict = field(default_factory=lambda: {
        "trades": 0,
        "pnl": 0.0,
        "last_price": None,
    })


class RobotManager:
    """机器人进程管理器"""
    
    ROBOTS_FILE = "running_robots.json"
    
    def __init__(self):
        self.console = Console()
        self.robots: Dict[str, RobotInstance] = {}
        self.load_robots()
    
    def _get_robots_file(self) -> Path:
        return Path(__file__).parent / self.ROBOTS_FILE
    
    def load_robots(self):
        """加载已保存的机器人配置"""
        robots_file = self._get_robots_file()
        if robots_file.exists():
            try:
                with open(robots_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for robot_data in data.get("robots", []):
                        config = RobotConfig(**robot_data["config"])
                        robot = RobotInstance(
                            id=robot_data["id"],
                            config=config,
                            status=RobotStatus(robot_data.get("status", "stopped")),
                        )
                        self.robots[robot.id] = robot
            except Exception as e:
                self.console.print(f"[#FF4500]✗[/] 加载机器人配置失败: {e}")
    
    def save_robots(self):
        """保存机器人配置"""
        robots_file = self._get_robots_file()
        data = {
            "robots": [
                {
                    "id": robot.id,
                    "config": {
                        "maker_exchange": robot.config.maker_exchange,
                        "taker_exchange": robot.config.taker_exchange,
                        "ticker": robot.config.ticker,
                        "order_size": robot.config.order_size,
                        "max_position": robot.config.max_position,
                        "long_threshold": robot.config.long_threshold,
                        "short_threshold": robot.config.short_threshold,
                        "fill_timeout": robot.config.fill_timeout,
                    },
                    "status": robot.status.value,
                }
                for robot in self.robots.values()
            ]
        }
        with open(robots_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    def generate_robot_id(self, maker: str, taker: str, ticker: str) -> str:
        """生成唯一的机器人 ID"""
        return f"{maker[:3]}_{taker[:3]}_{ticker}_{int(time.time())}"
    
    def check_duplicate(self, maker: str, taker: str, ticker: str) -> Optional[str]:
        """检查是否有重复的交易所+币种对配置"""
        for robot_id, robot in self.robots.items():
            if robot.status == RobotStatus.RUNNING:
                # 检查是否同一个交易所对
                same_exchange_pair = (
                    robot.config.maker_exchange == maker and 
                    robot.config.taker_exchange == taker
                )
                # 检查是否同一个币种
                same_ticker = robot.config.ticker == ticker.upper()
                
                if same_exchange_pair and same_ticker:
                    return robot_id
        return None
    
    def start_robot(self, robot: RobotInstance) -> bool:
        """启动机器人进程"""
        try:
            # 检查是否已运行
            if robot.pid and psutil.pid_exists(robot.pid):
                self.console.print(f"[#FFD700]⚠[/] 机器人 {robot.id} 已在运行中 (PID: {robot.pid})")
                return False
            
            # 构建命令
            cmd = [
                sys.executable,
                "arbitrage.py",
                "--exchange", robot.config.maker_exchange,
                "--ticker", robot.config.ticker,
                "--size", robot.config.order_size,
                "--max-position", robot.config.max_position,
                "--long-threshold", robot.config.long_threshold,
                "--short-threshold", robot.config.short_threshold,
            ]
            
            # 启动进程
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True  # 创建新会话，避免信号问题
                )
            except TypeError:
                # 如果 start_new_session 不可用，使用旧方式
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
            
            robot.pid = process.pid
            robot.status = RobotStatus.RUNNING
            robot.start_time = datetime.now()
            robot.last_update = datetime.now()
            
            self.save_robots()
            self.console.print(f"[#32CD32]✓[/] 机器人 {robot.id} 已启动 (PID: {process.pid})")
            return True
            
        except Exception as e:
            self.console.print(f"[#FF4500]✗[/] 启动机器人失败: {e}")
            robot.status = RobotStatus.ERROR
            return False
    
    def stop_robot(self, robot_id: str) -> bool:
        """停止机器人进程"""
        if robot_id not in self.robots:
            return False
        
        robot = self.robots[robot_id]
        
        if robot.pid and psutil.pid_exists(robot.pid):
            try:
                parent = psutil.Process(robot.pid)
                # 先尝试优雅终止
                parent.terminate()
                try:
                    parent.wait(timeout=3)
                except psutil.TimeoutExpired:
                    parent.kill()
                
                robot.status = RobotStatus.STOPPED
                robot.pid = None
                robot.start_time = None
                self.save_robots()
                self.console.print(f"[#32CD32]✓[/] 机器人 {robot_id} 已停止")
                return True
            except Exception as e:
                self.console.print(f"[#FF4500]✗[/] 停止机器人失败: {e}")
                return False
        else:
            robot.status = RobotStatus.STOPPED
            robot.pid = None
            self.save_robots()
            return True
    
    def stop_all_robots(self):
        """停止所有机器人"""
        for robot_id in list(self.robots.keys()):
            self.stop_robot(robot_id)
    
    def get_robot_status(self, robot_id: str) -> Optional[RobotInstance]:
        """获取机器人当前状态"""
        if robot_id not in self.robots:
            return None
        
        robot = self.robots[robot_id]
        
        # 检查进程是否还在运行
        if robot.pid and not psutil.pid_exists(robot.pid):
            robot.status = RobotStatus.STOPPED
            robot.pid = None
            self.save_robots()
        
        return robot
    
    def cleanup_zombie_robots(self):
        """清理已停止的僵尸进程"""
        for robot_id, robot in list(self.robots.items()):
            if robot.status == RobotStatus.RUNNING and robot.pid:
                if not psutil.pid_exists(robot.pid):
                    robot.status = RobotStatus.STOPPED
                    robot.pid = None
        self.save_robots()


class ConfigManager:
    """配置文件管理器"""
    
    def __init__(self):
        self.env_file = Path(__file__).parent / ".env"
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
    
    def check_exchange_configured(self, exchange: str) -> bool:
        """检查交易所是否已配置"""
        config = self.get_exchange_config(exchange)
        
        if exchange == "edgex":
            return bool(config.get("account_id") and config.get("stark_private_key"))
        elif exchange == "standx":
            return bool(config.get("wallet_private_key"))
        elif exchange == "grvt":
            return bool(config.get("trading_account_id") and 
                       config.get("private_key") and 
                       config.get("api_key"))
        elif exchange == "lighter":
            return bool(config.get("account_index") is not None and 
                       os.getenv("API_KEY_PRIVATE_KEY"))
        return False


class TUIStyle:
    """TUI 样式定义"""
    
    COLORS = {
        "primary": "#00BFFF",      # Deep Sky Blue
        "secondary": "#20B2AA",    # Light Sea Green
        "success": "#32CD32",      # Lime Green
        "warning": "#FFD700",      # Gold
        "danger": "#FF4500",       # Orange Red
        "info": "#87CEEB",         # Sky Blue
        "edgex": "#FF6B6B",        # Coral
        "standx": "#4ECDC4",       # Teal
        "grvt": "#9B59B6",         # Purple
        "lighter": "#3498DB",      # Blue
        "running": "#32CD32",
        "stopped": "#666666",
        "error": "#FF4500",
    }
    
    @classmethod
    def get_exchange_color(cls, exchange: str) -> str:
        return cls.COLORS.get(exchange.lower(), "white")
    
    @classmethod
    def get_status_color(cls, status: RobotStatus) -> str:
        colors = {
            RobotStatus.RUNNING: cls.COLORS["running"],
            RobotStatus.STOPPED: cls.COLORS["stopped"],
            RobotStatus.STARTING: cls.COLORS["warning"],
            RobotStatus.ERROR: cls.COLORS["error"],
        }
        return colors.get(status, "white")


class Dashboard:
    """Dashboard 展示"""
    
    def __init__(self, robot_manager: RobotManager):
        self.robot_manager = robot_manager
        self.console = Console()
    
    def render(self) -> Panel:
        """渲染 Dashboard"""
        # 清理僵尸进程
        self.robot_manager.cleanup_zombie_robots()
        
        robots = self.robot_manager.robots
        
        # 标题
        header = Panel(
            Align.center(
                Text.from_markup(
                    f"[bold #00BFFF]╔══════════════════════════════════════════════════════════════════╗\n"
                    f"[bold #00BFFF]║[/]  [white]🚀 跨交易所套利机器人 Dashboard 🚀[/]                  [bold #00BFFF]║\n"
                    f"[bold #00BFFF]╚══════════════════════════════════════════════════════════════════╝[/]\n"
                    f"\n"
                    f"[#20B2AA]◆[/] [#20B2AA]◆[/] [#20B2AA]◆[/]  [white]多机器人管理控制台 V2.0[/]  [#20B2AA]◆[/] [#20B2AA]◆[/] [#20B2AA]◆[/]"
                ),
                vertical="middle"
            ),
            box=box.ROUNDED,
            style="on #1a1a2e",
            padding=(1, 2)
        )
        
        # 机器人列表
        if not robots:
            robot_list = Panel(
                "[dim]暂无运行中的机器人，请添加新机器人[/]",
                title="[bold]机器人列表[/]",
                box=box.ROUNDED
            )
        else:
            robot_table = Table(show_header=True, box=box.ROUNDED)
            robot_table.add_column("ID", width=8, style="dim")
            robot_table.add_column("状态", width=10)
            robot_table.add_column("交易所对", width=20)
            robot_table.add_column("币种", width=10)
            robot_table.add_column("交易量", width=12)
            robot_table.add_column("运行时长", width=12)
            robot_table.add_column("交易数", width=8)
            robot_table.add_column("盈亏", width=10)
            
            for robot_id, robot in robots.items():
                status_color = TUIStyle.get_status_color(robot.status)
                status_icon = "▶" if robot.status == RobotStatus.RUNNING else "■"
                status_text = f"[{status_color}]{status_icon} {'运行中' if robot.status == RobotStatus.RUNNING else '已停止'}[/]"
                
                exchange_pair = f"[{TUIStyle.get_exchange_color(robot.config.maker_exchange)}]{robot.config.maker_exchange}[/] → [{TUIStyle.get_exchange_color(robot.config.taker_exchange)}]{robot.config.taker_exchange}[/]"
                
                # 计算运行时长
                if robot.start_time:
                    duration = datetime.now() - robot.start_time
                    duration_str = str(duration).split('.')[0]
                else:
                    duration_str = "-"
                
                # 交易数和盈亏
                trades = robot.stats.get("trades", 0)
                pnl = robot.stats.get("pnl", 0.0)
                pnl_color = "#32CD32" if pnl >= 0 else "#FF4500"
                pnl_str = f"[{pnl_color}]{'+' if pnl > 0 else ''}{pnl:.4f}[/]" if pnl else "-"
                
                robot_table.add_row(
                    robot_id.split('_')[-1][:8],  # 取时间戳后8位
                    status_text,
                    exchange_pair,
                    f"[#00BFFF]{robot.config.ticker}[/]",
                    f"[#FFD700]{robot.config.order_size}[/]",
                    f"[dim]{duration_str}[/]",
                    str(trades),
                    pnl_str
                )
            
            robot_list = Panel(robot_table, title="[bold]机器人列表[/]")
        
        # 统计信息
        stats_table = Table(show_header=False, box=None)
        stats_table.add_column("项目", style="bold")
        stats_table.add_column("值", width=15)
        
        running_count = sum(1 for r in robots.values() if r.status == RobotStatus.RUNNING)
        total_trades = sum(r.stats.get("trades", 0) for r in robots.values())
        total_pnl = sum(r.stats.get("pnl", 0.0) for r in robots.values())
        
        stats_table.add_row("总机器人数", str(len(robots)))
        stats_table.add_row("运行中", f"[#32CD32]{running_count}[/]")
        stats_table.add_row("已停止", str(len(robots) - running_count))
        stats_table.add_row("总交易数", str(total_trades))
        pnl_color = "#32CD32" if total_pnl >= 0 else "#FF4500"
        stats_table.add_row("总盈亏", f"[{pnl_color}]{'+' if total_pnl > 0 else ''}{total_pnl:.4f}[/]")
        
        stats_panel = Panel(stats_table, title="[bold]统计信息[/]")
        
        # 快捷操作
        quick_actions = Table(show_header=False, box=None)
        quick_actions.add_column("操作", style="dim", width=20)
        quick_actions.add_column("说明", width=40)
        quick_actions.add_row("[#00BFFF]1[/]", "添加新机器人")
        quick_actions.add_row("[#00BFFF]2[/]", "启动/停止机器人")
        quick_actions.add_row("[#00BFFF]3[/]", "查看机器人日志")
        quick_actions.add_row("[#00BFFF]4[/]", "配置交易所")
        quick_actions.add_row("[#00BFFF]5[/]", "停止所有机器人")
        quick_actions.add_row("[#00BFFF]Q[/]", "退出程序")
        
        actions_panel = Panel(quick_actions, title="[bold]快捷操作[/]")
        
        # 组合所有面板
        content = Group(
            header,
            "",
            robot_list,
            "",
            Columns([stats_panel, actions_panel], equal=True, expand=True)
        )
        
        return Panel(
            content,
            box=box.ROUNDED,
            style="on #0a0a1a",
            padding=(1, 2)
        )


class ArbitrageTUI:
    """跨交易所套利 TUI 控制台 V2.0"""
    
    def __init__(self):
        self.console = Console()
        self.config = ConfigManager()
        self.robot_manager = RobotManager()
        self.dashboard = Dashboard(self.robot_manager)
        self.running = True
        
        # 加载配置
        self.config.load_env()
    
    def clear_screen(self):
        """清屏"""
        os.system('cls' if os.name == 'nt' else 'clear')
    
    def print_header(self, title: str):
        """打印标题"""
        header = Panel(
            Align.center(
                Text.from_markup(
                    f"[bold #00BFFF]╔═══════════════════════════════════════════════════════╗\n"
                    f"[bold #00BFFF]║[/]  [white]{title}[/]                      [bold #00BFFF]║\n"
                    f"[bold #00BFFF]╚═══════════════════════════════════════════════════════╝[/]"
                ),
                vertical="middle"
            ),
            box=box.ROUNDED,
            style="on #1a1a2e",
            padding=(1, 2)
        )
        self.console.print(header)
    
    def main_loop(self):
        """主循环"""
        while self.running:
            self.clear_screen()
            
            # 显示 Dashboard
            panel = self.dashboard.render()
            self.console.print(panel)
            
            # 提示输入
            self.console.print("\n[bold][#00BFFF]➜[/][/] 请选择操作", end=" ")
            choice = Prompt.ask("", default="1", console=self.console)
            
            if choice.lower() in ["q", "quit", "exit"]:
                self._quit()
            elif choice == "1":
                self._add_robot()
            elif choice == "2":
                self._manage_robots()
            elif choice == "3":
                self._view_logs()
            elif choice == "4":
                self._configure_exchanges()
            elif choice == "5":
                self._stop_all()
    
    def _add_robot(self):
        """添加新机器人"""
        self.clear_screen()
        self.print_header("添加新机器人")
        
        # 检查交易所配置
        self.console.print("[bold]检查交易所配置...[/]\n")
        
        exchanges = ["edgex", "grvt", "standx"]
        for ex in exchanges:
            configured = self.config.check_exchange_configured(ex)
            color = "#32CD32" if configured else "#FF4500"
            icon = "✓" if configured else "✗"
            self.console.print(f"  [{color}]{icon}[/] {ex.upper()}: {'已配置' if configured else '未配置'}")
        
        self.console.print("")
        
        # 选择交易所对
        self.console.print("[bold]请选择交易所组合:[/]\n")
        
        combos = [
            ("edgex", "lighter", "EdgeX → Lighter (经典)"),
            ("grvt", "lighter", "GRVT → Lighter (新交易所)"),
            ("edgex", "standx", "EdgeX → StandX (Solana)"),
        ]
        
        for i, (m, t, desc) in enumerate(combos, 1):
            self.console.print(f"  [#00BFFF][{i}][/] {m.upper()} → {t.upper()} ({desc})")
        
        self.console.print("")
        choice = Prompt.ask("[bold][#00BFFF]➜[/][/] 请选择", default="1")
        
        if not choice.isdigit() or int(choice) < 1 or int(choice) > len(combos):
            return
        
        maker, taker = combos[int(choice) - 1][:2]
        
        # 检查交易所是否已配置
        if not self.config.check_exchange_configured(maker):
            self.console.print(f"\n[#FF4500]✗[/] {maker.upper()} 交易所未配置，请先配置！")
            Prompt.ask("\n[dim]按回车继续...[/]")
            return
        
        if not self.config.check_exchange_configured(taker):
            self.console.print(f"\n[#FF4500]✗[/] {taker.upper()} 交易所未配置，请先配置！")
            Prompt.ask("\n[dim]按回车继续...[/]")
            return
        
        # 选择币种
        self.console.print("\n")
        ticker = Prompt.ask("[bold][#00BFFF]➜[/][/] 输入交易对 (BTC/ETH/SOL)", default="BTC")
        ticker = ticker.upper()
        
        # 检查重复
        duplicate_id = self.robot_manager.check_duplicate(maker, taker, ticker)
        if duplicate_id:
            self.console.print(f"\n[#FFD700]⚠[/] 检测到重复配置！")
            self.console.print(f"  机器人 ID: {duplicate_id}")
            self.console.print(f"  交易所: {maker} → {taker}")
            self.console.print(f"  币种: {ticker}")
            if not self.confirm_action("确定要启动多个相同配置的机器人吗？"):
                return
        
        # 配置参数
        self.console.print("\n[bold]配置交易参数:[/]\n")
        
        order_size = Prompt.ask("  交易数量", default="0.001")
        max_position = Prompt.ask("  最大持仓 (0=无限制)", default="0.1")
        long_threshold = Prompt.ask("  做多阈值 (USDT)", default="10")
        short_threshold = Prompt.ask("  做空阈值 (USDT)", default="10")
        
        # 创建机器人配置
        config = RobotConfig(
            maker_exchange=maker,
            taker_exchange=taker,
            ticker=ticker,
            order_size=order_size,
            max_position=max_position,
            long_threshold=long_threshold,
            short_threshold=short_threshold,
        )
        
        # 生成机器人 ID
        robot_id = self.robot_manager.generate_robot_id(maker, taker, ticker)
        
        # 创建机器人实例
        robot = RobotInstance(
            id=robot_id,
            config=config,
            status=RobotStatus.STOPPED,
        )
        
        # 保存并启动
        self.robot_manager.robots[robot_id] = robot
        self.robot_manager.save_robots()
        
        self.console.print(f"\n[#32CD32]✓[/] 机器人配置已保存！")
        self.console.print(f"  机器人 ID: {robot_id}")
        self.console.print(f"  交易所: {maker} → {taker}")
        self.console.print(f"  币种: {ticker}")
        
        if self.confirm_action("立即启动机器人？"):
            self.robot_manager.start_robot(robot)
        
        Prompt.ask("\n[dim]按回车返回...[/]")
    
    def _manage_robots(self):
        """管理机器人"""
        self.clear_screen()
        self.print_header("管理机器人")
        
        robots = self.robot_manager.robots
        if not robots:
            self.console.print("[dim]暂无机器人[/]\n")
            Prompt.ask("\n[dim]按回车返回...[/]")
            return
        
        # 显示机器人列表
        self.console.print("[bold]当前机器人:[/]\n")
        
        robot_table = Table(show_header=True, box=box.ROUNDED)
        robot_table.add_column("编号", width=5)
        robot_table.add_column("ID", width=20)
        robot_table.add_column("交易所", width=20)
        robot_table.add_column("币种", width=8)
        robot_table.add_column("状态", width=12)
        robot_table.add_column("PID", width=8)
        
        for i, (robot_id, robot) in enumerate(robots.items(), 1):
            status_color = TUIStyle.get_status_color(robot.status)
            status_icon = "▶" if robot.status == RobotStatus.RUNNING else "■"
            status_text = f"[{status_color}]{status_icon} {'运行中' if robot.status == RobotStatus.RUNNING else '已停止'}[/]"
            
            exchange_pair = f"{robot.config.maker_exchange} → {robot.config.taker_exchange}"
            pid_str = str(robot.pid) if robot.pid else "-"
            
            robot_table.add_row(
                f"[#00BFFF][{i}][/]",
                robot_id,
                exchange_pair,
                robot.config.ticker,
                status_text,
                pid_str
            )
        
        self.console.print(robot_table)
        
        self.console.print("\n[bold]操作说明:[/]")
        self.console.print("  输入编号 + 回车: 启动/停止该机器人")
        self.console.print("  输入 'a' + 回车: 启动所有机器人")
        self.console.print("  输入 's' + 回车: 停止所有机器人")
        self.console.print("  输入 'd' + 回车: 删除机器人")
        self.console.print("  输入 'q' + 回车: 返回")
        
        choice = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 请选择")
        
        if choice.lower() == "q":
            return
        elif choice.lower() == "a":
            for robot in robots.values():
                if robot.status != RobotStatus.RUNNING:
                    self.robot_manager.start_robot(robot)
        elif choice.lower() == "s":
            if self.confirm_action("确定要停止所有机器人吗？"):
                self.robot_manager.stop_all_robots()
        elif choice.lower() == "d":
            self._delete_robot()
        elif choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(robots):
                robot = list(robots.values())[idx - 1]
                if robot.status == RobotStatus.RUNNING:
                    self.robot_manager.stop_robot(robot.id)
                else:
                    self.robot_manager.start_robot(robot)
        
        Prompt.ask("\n[dim]按回车继续...[/]")
    
    def _delete_robot(self):
        """删除机器人"""
        robots = self.robot_manager.robots
        if not robots:
            return
        
        self.console.print("\n[bold]选择要删除的机器人:[/]")
        for i, robot_id in enumerate(robots.keys(), 1):
            self.console.print(f"  [#00BFFF][{i}][/] {robot_id}")
        
        choice = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 请选择")
        
        if choice.isdigit() and 1 <= int(choice) <= len(robots):
            robot_id = list(robots.keys())[int(choice) - 1]
            
            # 如果运行中，先停止
            robot = robots[robot_id]
            if robot.status == RobotStatus.RUNNING:
                self.robot_manager.stop_robot(robot_id)
            
            # 删除
            del self.robot_manager.robots[robot_id]
            self.robot_manager.save_robots()
            self.console.print(f"\n[#32CD32]✓[/] 机器人已删除")
    
    def _view_logs(self):
        """查看机器人日志"""
        self.clear_screen()
        self.print_header("查看实时日志")
        
        robots = self.robot_manager.robots
        if not robots:
            self.console.print("[dim]暂无机器人[/]\n")
            Prompt.ask("\n[dim]按回车返回...[/]")
            return
        
        # 选择机器人
        self.console.print("[bold]选择机器人:[/]\n")
        for i, (robot_id, robot) in enumerate(robots.items(), 1):
            status_color = TUIStyle.get_status_color(robot.status)
            self.console.print(f"  [#00BFFF][{i}][/] {robot_id} [{status_color}]{'运行中' if robot.status == RobotStatus.RUNNING else '已停止'}[/]")
        
        choice = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 请选择 (输入 'q' 返回)")
        
        if choice.lower() == "q":
            return
        
        if not choice.isdigit() or int(choice) < 1 or int(choice) > len(robots):
            return
        
        robot_id = list(robots.keys())[int(choice) - 1]
        robot = robots[robot_id]
        
        self._show_robot_logs(robot)
    
    def _show_robot_logs(self, robot: RobotInstance):
        """显示机器人日志"""
        self.clear_screen()
        
        # 头部信息
        header = Panel(
            f"机器人: {robot.id}\n"
            f"交易所: {robot.config.maker_exchange} → {robot.config.taker_exchange}\n"
            f"币种: {robot.config.ticker} | 数量: {robot.config.order_size}\n"
            f"状态: {'运行中' if robot.status == RobotStatus.RUNNING else '已停止'}",
            title="[bold]机器人信息[/]",
            box=box.ROUNDED
        )
        
        self.console.print(header)
        self.console.print("\n[bold]实时日志 (实时更新, 按 Q 返回):[/]\n")
        
        # 读取日志文件
        log_file = Path(__file__).parent / "logs" / f"{robot.config.maker_exchange}_{robot.config.ticker}_log.txt"
        
        if not log_file.exists():
            self.console.print(f"[dim]日志文件不存在: {log_file}[/]")
            Prompt.ask("\n[dim]按回车返回...[/]")
            return
        
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                # 显示最后 100 行
                lines = f.readlines()[-100:]
                for line in lines:
                    self.console.print(line.rstrip())
        except Exception as e:
            self.console.print(f"[#FF4500]✗[/] 读取日志失败: {e}")
        
        Prompt.ask("\n[dim]按回车返回...[/]")
    
    def _configure_exchanges(self):
        """配置交易所"""
        self.clear_screen()
        self.print_header("配置交易所")
        
        self.console.print("[bold]选择要配置的交易所:[/]\n")
        
        exchanges = [
            ("1", "edgex", "EdgeX"),
            ("2", "grvt", "GRVT"),
            ("3", "standx", "StandX"),
            ("4", "lighter", "Lighter"),
            ("5", "返回", ""),
        ]
        
        for code, ex, name in exchanges:
            if name:
                configured = self.config.check_exchange_configured(ex)
                color = "#32CD32" if configured else "#FF4500"
                icon = "✓" if configured else "✗"
                self.console.print(f"  [#00BFFF][{code}][/] {name} [{color}]{icon}[/]")
        
        choice = Prompt.ask("\n[bold][#00BFFF]➜[/][/] 请选择")
        
        if choice == "5":
            return
        
        exchange_map = {"1": "edgex", "2": "grvt", "3": "standx", "4": "lighter"}
        if choice in exchange_map:
            self._edit_exchange_config(exchange_map[choice])
        
        Prompt.ask("\n[dim]按回车继续...[/]")
    
    def _edit_exchange_config(self, exchange: str):
        """编辑交易所配置"""
        exchange_names = {
            "edgex": "EdgeX",
            "grvt": "GRVT",
            "standx": "StandX",
            "lighter": "Lighter"
        }
        
        current_config = self.config.get_exchange_config(exchange)
        
        self.console.print(f"\n[bold]配置 {exchange_names.get(exchange, exchange)}:[/]\n")
        
        for key, value in current_config.items():
            if not value:
                display = "[dim](未设置)[/]"
            elif "key" in key.lower() or "private" in key.lower():
                display = f"[#FF6B6B]{value[:8]}...{value[-4:]}[/]"
            else:
                display = f"[#00BFFF]{value}[/]"
            
            self.console.print(f"  {key.upper()}: {display}")
        
        self.console.print("\n[bold]输入新配置 (直接回车保持当前值):[/]\n")
        
        new_config = {}
        for key, value in current_config.items():
            prompt = f"[#00BFFF]▶[/] {key.upper()}"
            new_value = Prompt.ask(prompt, default=value or "")
            if new_value:
                new_config[key] = new_value
        
        if new_config:
            self.config.save_exchange_config(exchange, new_config)
            self.console.print(f"\n[#32CD32]✓[/] {exchange.upper()} 配置已保存！")
    
    def _stop_all(self):
        """停止所有机器人"""
        if not self.confirm_action("确定要停止所有机器人吗？"):
            return
        
        self.robot_manager.stop_all_robots()
        self.console.print("\n[#32CD32]✓[/] 所有机器人已停止")
        Prompt.ask("\n[dim]按回车继续...[/]")
    
    def confirm_action(self, message: str) -> bool:
        """确认操作"""
        return Confirm.ask(f"[#FFD700]⚠[/] {message}")
    
    def _quit(self):
        """退出程序"""
        if self.running:
            running_count = sum(1 for r in self.robot_manager.robots.values() 
                              if r.status == RobotStatus.RUNNING)
            if running_count > 0:
                self.console.print(f"\n[#FFD700]⚠[/] 有 {running_count} 个机器人正在运行")
                if not self.confirm_action("确定要退出吗？(机器人会继续在后台运行)"):
                    self.running = True
                    return
            
            self.running = False
            self.console.print("\n[#32CD32]✓[/] 感谢使用，再见！")
    
    def run(self):
        """运行 TUI"""
        try:
            self.main_loop()
        except KeyboardInterrupt:
            self._quit()
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
