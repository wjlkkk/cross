#!/usr/bin/env python3
"""
仓位平衡监控机器人启动脚本

使用方法：
    # 监控 EdgeX-Lighter 仓位
    python run_position_monitor.py --maker edgex --ticker SOL

    # 监控 GRVT-Lighter 仓位
    python run_position_monitor.py --maker grvt --ticker SOL

    # 启用自动平仓
    python run_position_monitor.py --maker edgex --ticker SOL --auto-close

    # 自定义检查间隔和阈值
    python run_position_monitor.py --maker edgex --ticker SOL --interval 5 --threshold 0.1
"""
import asyncio
import sys
import os
from decimal import Decimal

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from strategy.position_balance_monitor import PositionBalanceMonitor


def print_usage():
    """打印使用说明"""
    print("""
╔══════════════════════════════════════════════════════════════╗
║           Position Balance Monitor - Usage                   ║
╠══════════════════════════════════════════════════════════════╣
║                                                                ║
║  Usage:                                                       ║
║    python run_position_monitor.py [OPTIONS]                   ║
║                                                                ║
║  Options:                                                     ║
║    --maker {edgex,grvt}    Maker交易所 (默认: edgex)           ║
║    --ticker SYMBOL         交易对 (默认: SOL)                  ║
║    --interval SECONDS      检查间隔秒数 (默认: 10)             ║
║    --threshold VALUE       平衡阈值 (默认: 0.05)               ║
║    --auto-close            发现不平衡时自动平仓                ║
║    --webhook URL           警报Webhook URL                     ║
║    --no-csv                不记录CSV文件                       ║
║    --help                  显示此帮助信息                       ║
║                                                                ║
║  环境变量配置:                                                 ║
║    ALERT_WEBHOOK_URL        警报Webhook URL                    ║
║    MONITOR_AUTO_CLOSE       自动平仓开关 (true/false)          ║
║    MONITOR_CHECK_INTERVAL   检查间隔                           ║
║    MONITOR_BALANCE_THRESHOLD 平衡阈值                          ║
║                                                                ║
╚══════════════════════════════════════════════════════════════╝
""")


def parse_args():
    """解析命令行参数"""
    args = {
        'maker': 'edgex',
        'ticker': 'SOL',
        'interval': 10,
        'threshold': Decimal('0.05'),
        'auto_close': False,
        'webhook': None,
        'log_csv': True,
    }

    i = 0
    while i < len(sys.argv):
        arg = sys.argv[i]

        if arg in ['--help', '-h']:
            print_usage()
            sys.exit(0)

        elif arg == '--maker':
            if i + 1 < len(sys.argv):
                args['maker'] = sys.argv[i + 1].lower()
                i += 1

        elif arg == '--ticker':
            if i + 1 < len(sys.argv):
                args['ticker'] = sys.argv[i + 1].upper()
                i += 1

        elif arg == '--interval':
            if i + 1 < len(sys.argv):
                args['interval'] = int(sys.argv[i + 1])
                i += 1

        elif arg == '--threshold':
            if i + 1 < len(sys.argv):
                args['threshold'] = Decimal(sys.argv[i + 1])
                i += 1

        elif arg == '--auto-close':
            args['auto_close'] = True

        elif arg == '--webhook':
            if i + 1 < len(sys.argv):
                args['webhook'] = sys.argv[i + 1]
                i += 1

        elif arg == '--no-csv':
            args['log_csv'] = False

        i += 1

    # 从环境变量读取默认值
    if args['webhook'] is None:
        args['webhook'] = os.getenv('ALERT_WEBHOOK_URL')

    if os.getenv('MONITOR_AUTO_CLOSE', 'false').lower() == 'true':
        args['auto_close'] = True

    if 'MONITOR_CHECK_INTERVAL' in os.environ:
        args['interval'] = int(os.getenv('MONITOR_CHECK_INTERVAL'))

    if 'MONITOR_BALANCE_THRESHOLD' in os.environ:
        args['threshold'] = Decimal(os.getenv('MONITOR_BALANCE_THRESHOLD'))

    if 'MONITOR_MAKER_EXCHANGE' in os.environ:
        args['maker'] = os.getenv('MONITOR_MAKER_EXCHANGE').lower()

    if 'MONITOR_TICKER' in os.environ:
        args['ticker'] = os.getenv('MONITOR_TICKER').upper()

    return args


async def main():
    """主函数"""
    args = parse_args()

    # 验证参数
    if args['maker'] not in ['edgex', 'grvt']:
        print(f"❌ Invalid maker exchange: {args['maker']}")
        print("   Must be 'edgex' or 'grvt'")
        sys.exit(1)

    # 打印配置
    print()
    print("=" * 64)
    print("           Position Balance Monitor")
    print("=" * 64)
    print(f"  Maker Exchange : {args['maker'].upper()}")
    print(f"  Ticker         : {args['ticker']}")
    print(f"  Check Interval : {args['interval']} seconds")
    print(f"  Balance Threshold : {args['threshold']}")
    print(f"  Auto-Close     : {'🟢 ENABLED' if args['auto_close'] else '🔴 DISABLED'}")
    print(f"  Webhook Alerts : {'🟢 ENABLED' if args['webhook'] else '🔴 DISABLED'}")
    print(f"  CSV Logging    : {'🟢 ENABLED' if args['log_csv'] else '🔴 DISABLED'}")
    print("=" * 64)
    print()

    if args['auto_close']:
        print("⚠️  WARNING: Auto-close is ENABLED!")
        print("   The bot will automatically close positions when imbalance is detected.")
        print()

        confirm = input("Continue? (yes/no): ")
        if confirm.lower() != 'yes':
            print("Aborted.")
            sys.exit(0)

    # 创建并运行监控器
    monitor = PositionBalanceMonitor(
        maker_exchange=args['maker'],
        ticker=args['ticker'],
        check_interval=args['interval'],
        balance_threshold=args['threshold'],
        alert_webhook_url=args['webhook'],
        auto_close=args['auto_close'],
        log_to_csv=args['log_csv']
    )

    try:
        await monitor.run()
    except KeyboardInterrupt:
        print("\n\n👋 Monitor stopped by user")


if __name__ == "__main__":
    asyncio.run(main())
