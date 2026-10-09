//! `tradedesk-backtest`: the backtester's command line ([`tradedesk_backtest::cli`]) with
//! no strategy registered. `report` and `walkforward` work on any trial registry; `sweep`
//! refuses every spec, naming the strategies it knows. A strategy crate builds its own
//! binary on `tradedesk_backtest::cli::main` with its strategies registered.

use std::process::ExitCode;

use tradedesk_backtest::sweep::StrategyRegistry;

fn main() -> ExitCode {
    tradedesk_backtest::cli::main(&StrategyRegistry::new())
}
