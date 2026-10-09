//! The strategies a sweep can run: a [`StrategyRegistry`] of named [`StrategyFactory`]s.
//!
//! No strategy ships with this crate. A strategy crate implements
//! [`Strategy`](crate::strategy::Strategy), writes a factory that builds it from a JSON
//! config, and registers the factory under the name sweep specs use
//! (`strategy = "<name>"`). That name is written into every registry record and hashed
//! into every trial id, so it should never change once trials are recorded under it.
//!
//! ```
//! use serde_json::Value;
//! use tradedesk_backtest::sweep::{BaseConfig, BoxedStrategy, StrategyFactory, StrategyRegistry};
//!
//! struct Mine;
//!
//! impl StrategyFactory for Mine {
//!     fn base_config(&self, base: BaseConfig) -> Result<Value, String> {
//!         match base {
//!             BaseConfig::Default => Ok(serde_json::json!({ "period": 20 })),
//!             BaseConfig::Frozen => Err("no frozen config; use base = \"default\"".into()),
//!         }
//!     }
//!
//!     fn build(&self, config: Value) -> Result<BoxedStrategy, String> {
//!         // Deserialise `config` (refusing unknown fields) and construct the strategy.
//!         Err(format!("not built in this example: {config}"))
//!     }
//! }
//!
//! let mut strategies = StrategyRegistry::new();
//! strategies.register("mine", Mine).unwrap();
//! assert!(strategies.register("mine", Mine).is_err());
//! assert_eq!(strategies.names().collect::<Vec<_>>(), ["mine"]);
//! ```

use std::collections::BTreeMap;

use serde_json::Value;

use super::BaseConfig;
use crate::strategy::Strategy;

/// A built strategy, as a sweep runs it.
pub type BoxedStrategy = Box<dyn Strategy + Send>;

/// Builds one strategy for a sweep. Implemented by the crate that owns the strategy.
pub trait StrategyFactory: Send + Sync {
    /// The config every cell of a sweep starts from, as JSON. A cell sets its parameters
    /// by dotted path in it, and a path that is absent is a spec error, so every field a
    /// sweep may vary must be present.
    ///
    /// # Errors
    /// A message when the strategy has no config for `base`.
    fn base_config(&self, base: BaseConfig) -> Result<Value, String>;

    /// Build the strategy from a resolved config. It should refuse unknown fields, so a
    /// misspelt parameter cannot fall back to a default.
    ///
    /// # Errors
    /// A message when the config does not deserialise or the strategy rejects it; the
    /// sweep records it as that cell's failure.
    fn build(&self, config: Value) -> Result<BoxedStrategy, String>;

    /// The dotted config path the sweep writes each instrument's `raw_scale` (from the
    /// cost config) into, when the strategy's signals depend on the price scale
    /// ([`Strategy::price_scale`]). The value is derived, so a spec that names the path
    /// as a parameter is refused. `None` (the default) when nothing depends on it.
    fn raw_scale_path(&self) -> Option<&str> {
        None
    }
}

/// A name was registered twice.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("strategy {0:?} is already registered")]
pub struct DuplicateStrategy(pub String);

/// The strategies a sweep can run, by the name a spec gives them.
#[derive(Default)]
pub struct StrategyRegistry {
    factories: BTreeMap<String, Box<dyn StrategyFactory>>,
}

impl StrategyRegistry {
    /// An empty registry.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Register `factory` under `name`.
    ///
    /// # Errors
    /// [`DuplicateStrategy`] when `name` is already registered; the registry is unchanged.
    pub fn register(
        &mut self,
        name: impl Into<String>,
        factory: impl StrategyFactory + 'static,
    ) -> Result<&mut Self, DuplicateStrategy> {
        let name = name.into();
        if self.factories.contains_key(&name) {
            return Err(DuplicateStrategy(name));
        }
        self.factories.insert(name, Box::new(factory));
        Ok(self)
    }

    /// The factory registered under `name`.
    #[must_use]
    pub fn get(&self, name: &str) -> Option<&dyn StrategyFactory> {
        self.factories.get(name).map(AsRef::as_ref)
    }

    /// The registered names, in sorted order.
    pub fn names(&self) -> impl Iterator<Item = &str> {
        self.factories.keys().map(String::as_str)
    }

    /// `true` when nothing is registered.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.factories.is_empty()
    }
}

impl std::fmt::Debug for StrategyRegistry {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_set().entries(self.names()).finish()
    }
}
