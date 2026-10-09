//! Shared reader for the Python-generated golden CSVs under `tests/fixtures/`.
//!
//! The format is deliberately plain: `#` comment lines (provenance), one header row,
//! then comma-separated rows with no quoting. Floats were written with Python's
//! `repr`, which round-trips exactly through `str::parse::<f64>`. An empty cell is
//! Python's `None`.
#![allow(dead_code)]

use std::collections::HashMap;
use std::path::PathBuf;

use chrono::{DateTime, Utc};

/// A parsed fixture: column names and rows of raw cells.
pub struct Fixture {
    pub name: String,
    columns: HashMap<String, usize>,
    pub rows: Vec<Vec<String>>,
}

impl Fixture {
    /// Read `tests/fixtures/<relative>`.
    pub fn read(relative: &str) -> Self {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures")
            .join(relative);
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("reading {}: {e}", path.display()));
        let mut lines = text
            .lines()
            .filter(|l| !l.starts_with('#') && !l.is_empty());
        let header = lines.next().expect("fixture has a header row");
        let columns = header
            .split(',')
            .enumerate()
            .map(|(i, c)| (c.to_owned(), i))
            .collect::<HashMap<_, _>>();
        let rows = lines
            .map(|l| l.split(',').map(str::to_owned).collect::<Vec<_>>())
            .collect::<Vec<_>>();
        for (i, row) in rows.iter().enumerate() {
            assert_eq!(row.len(), columns.len(), "{relative}: row {i} width");
        }
        Self {
            name: relative.to_owned(),
            columns,
            rows,
        }
    }

    fn index(&self, column: &str) -> usize {
        *self
            .columns
            .get(column)
            .unwrap_or_else(|| panic!("{}: no column {column}", self.name))
    }

    /// The raw cell.
    pub fn cell<'a>(&self, row: &'a [String], column: &str) -> &'a str {
        &row[self.index(column)]
    }

    /// A float cell; `None` when empty.
    pub fn opt_f64(&self, row: &[String], column: &str) -> Option<f64> {
        let cell = self.cell(row, column);
        (!cell.is_empty()).then(|| {
            cell.parse::<f64>()
                .unwrap_or_else(|e| panic!("{}: {column} = {cell:?}: {e}", self.name))
        })
    }

    /// A required float cell.
    pub fn f64(&self, row: &[String], column: &str) -> f64 {
        self.opt_f64(row, column)
            .unwrap_or_else(|| panic!("{}: {column} is empty", self.name))
    }

    /// An RFC 3339 timestamp cell.
    pub fn ts(&self, row: &[String], column: &str) -> DateTime<Utc> {
        let cell = self.cell(row, column);
        DateTime::parse_from_rfc3339(cell)
            .unwrap_or_else(|e| panic!("{}: {column} = {cell:?}: {e}", self.name))
            .with_timezone(&Utc)
    }
}

/// `|actual - expected| <= tol * max(1, |expected|)`.
pub fn close_enough(actual: f64, expected: f64, tol: f64) -> bool {
    (actual - expected).abs() <= tol * expected.abs().max(1.0)
}

/// Relative tolerance every golden comparison uses.
pub const TOLERANCE: f64 = 1e-9;
