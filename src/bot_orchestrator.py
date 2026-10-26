from typing import Dict
from rich.console import Console
from rich.table import Table

from src.config import config
from src.market_data import MarketDataFeeder
from src.ai_analyzer import AIAnalyzer
from src.risk_manager import RiskManager
from src.paper_trader import PaperTrader
from src.live_trader import BinanceFuturesTrader
from src.telegram_notifier import TelegramNotifier

console = Console()

class BotOrchestrator:
    def __init__(self):
        self.feeder = MarketDataFeeder()
        self.ai = AIAnalyzer()
        self.risk_manager = RiskManager()
        self.telegram = TelegramNotifier()

        # Choose execution trader
        if config.trading_mode == "paper":
            self.paper_trader = PaperTrader()
            self.live_trader = None
        else:
            self.paper_trader = None
            self.live_trader = BinanceFuturesTrader(is_testnet=(config.trading_mode == "testnet"))

    def get_current_balance(self) -> float:
        if config.trading_mode == "paper":
            return self.paper_trader.get_balance()
        else:
            return self.live_trader.get_balance()

    def get_open_positions_count(self) -> int:
        if config.trading_mode == "paper":
            return self.paper_trader.get_open_positions_count()
        else:
            return len(self.live_trader.get_open_positions())

    def run_cycle(self):
        """
        Executes one full iteration over all configured symbols:
        1. Updates open positions against current market prices.
        2. Scans for new trade setups.
        3. Consults AI and Risk Manager.
        4. Executes orders if criteria are met.
        """
        mode_str = config.trading_mode.upper()
        console.print(f"\n[bold cyan]─── Scan Cycle Started [{mode_str} MODE] ───[/bold cyan]")

        # 1. Fetch current prices for all symbols
        current_prices: Dict[str, float] = {}
        for sym in config.trading_symbols:
            price = self.feeder.fetch_current_price(sym)
            if price > 0:
                current_prices[sym] = price

        # 2. Check and manage active positions
        if config.trading_mode == "paper":
            closed_trades = self.paper_trader.check_and_update_positions(current_prices)
            for trade in closed_trades:
                self.risk_manager.record_closed_trade_pnl(trade["net_pnl"])
                self.telegram.notify_trade_closed(trade, mode=config.trading_mode)
                pnl_color = "green" if trade["net_pnl"] >= 0 else "red"
                console.print(
                    f"[{pnl_color}]🔔 Position Closed: {trade['symbol']} ({trade['side']}) | "
                    f"Reason: {trade['reason']} | Net PnL: ${trade['net_pnl']} USDT ({trade['roi_pct']}%)"
                )

        # 3. Display Portfolio Overview Table
        self._display_portfolio_table(current_prices)

        # 4. Scan symbols for new trade opportunities
        balance = self.get_current_balance()
        open_count = self.get_open_positions_count()

        for symbol in config.trading_symbols:
            # Check if position already open for this symbol
            if config.trading_mode == "paper" and symbol in self.paper_trader.open_positions:
                continue

            console.print(f"\n[bold yellow]🔍 Analyzing {symbol} ({config.timeframe})...[/bold yellow]")
            snapshot = self.feeder.get_market_analysis_context(symbol, timeframe=config.timeframe)
            if not snapshot:
                console.print(f"[red]⚠️ Could not fetch market context for {symbol}. Skipping.[/red]")
                continue

            # Query AI Decision
            signal = self.ai.analyze_market(snapshot)
            console.print(
                f"🧠 [bold]AI Output:[/bold] Action: [bold {self._action_color(signal.action)}]{signal.action}[/] | "
                f"Confidence: {signal.confidence:.0%} | RR: {signal.risk_reward_ratio} | Reason: [italic]{signal.primary_reason}[/italic]"
            )

            if signal.action in ["LONG", "SHORT"]:
                # Evaluate with Risk Guard
                is_approved, reason, sizing = self.risk_manager.evaluate_signal(
                    signal=signal,
                    portfolio_balance=balance,
                    open_positions_count=open_count
                )

                if is_approved:
                    console.print(f"[bold green]✅ RISK APPROVED:[/bold green] Sizing: {sizing['quantity']} contracts (${sizing['position_notional_usdt']} notional, Margin: ${sizing['required_margin_usdt']} USDT)")
                    # Execute
                    if config.trading_mode == "paper":
                        success = self.paper_trader.open_position(symbol, sizing, signal.primary_reason)
                    else:
                        success = self.live_trader.execute_bracket_order(symbol, sizing)

                    if success:
                        self.telegram.notify_trade_opened(symbol, sizing, signal.primary_reason, mode=config.trading_mode)
                        open_count += 1
                        console.print(f"[bold green]🚀 Order Successfully Placed for {symbol} ({signal.action})[/bold green]")
                else:
                    console.print(f"[dim yellow]✋ Trade Blocked by Risk Guard: {reason}[/dim yellow]")

    def _action_color(self, action: str) -> str:
        if action == "LONG":
            return "green"
        elif action == "SHORT":
            return "red"
        return "yellow"

    def _display_portfolio_table(self, current_prices: Dict[str, float]):
        table = Table(title=f"💼 Portfolio Status [{config.trading_mode.upper()} MODE]", header_style="bold magenta")
        table.add_column("Symbol", style="cyan")
        table.add_column("Side", justify="center")
        table.add_column("Entry Price", justify="right")
        table.add_column("Current Price", justify="right")
        table.add_column("Unrealized PnL", justify="right")
        table.add_column("Stop Loss", justify="right")
        table.add_column("Take Profit", justify="right")

        if config.trading_mode == "paper":
            summary = self.paper_trader.get_portfolio_summary(current_prices)
            console.print(
                f"💰 Balance: [bold green]${summary['wallet_balance']} USDT[/] | "
                f"Total Equity: [bold]${summary['total_equity']} USDT[/] | "
                f"Win Rate: [bold cyan]{summary['win_rate_pct']}%[/] ({summary['total_trades']} trades)"
            )

            for pos in summary["active_positions"]:
                pnl_color = "green" if pos["unrealized_pnl"] >= 0 else "red"
                table.add_row(
                    pos["symbol"],
                    f"[{'green' if pos['side'] == 'LONG' else 'red'}]{pos['side']}[/]",
                    f"${pos['entry']:.4f}",
                    f"${pos['current']:.4f}",
                    f"[{pnl_color}]${pos['unrealized_pnl']:.2f} ({pos['roe_pct']}%)[/]",
                    f"${pos['sl']:.4f}",
                    f"${pos['tp']:.4f}"
                )
        else:
            balance = self.live_trader.get_balance()
            console.print(f"💰 Available Binance Futures Balance: [bold green]${balance:.2f} USDT[/]")
            positions = self.live_trader.get_open_positions()
            for pos in positions:
                pnl_color = "green" if pos["unrealized_pnl"] >= 0 else "red"
                curr = current_prices.get(pos["symbol"], 0.0)
                table.add_row(
                    pos["symbol"],
                    f"[{'green' if pos['side'] == 'LONG' else 'red'}]{pos['side']}[/]",
                    f"${pos['entry_price']:.4f}",
                    f"${curr:.4f}",
                    f"[{pnl_color}]${pos['unrealized_pnl']:.2f}[/]",
                    "-",
                    "-"
                )

        if table.row_count > 0:
            console.print(table)
        else:
            console.print("[dim]No active open positions. Watching markets for high-probability setups...[/dim]")
