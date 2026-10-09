//! The venue cost model (M1-R2) and overnight financing (M1-R3).
//!
//! - [`config`]: the checked-in `config/costs.toml` and its loader: instrument units and
//!   per-venue, per-instrument spread, slippage, commission and financing.
//! - [`fill`]: fill simulation on a joined bar: configured side ± half the venue spread
//!   ± slippage, then commission; a divergent bar is an error.
//! - [`financing`]: the venue rollover calendar and the per-rollover charge.

pub mod config;
pub mod fill;
pub mod financing;
