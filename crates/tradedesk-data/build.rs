//! Compile-time `TRADEDESK_CODE_REVISION` injection.
//!
//! Mitigates code revision tampering / repudiation: the bar cache's fingerprint sidecar and
//! every backtest trial record carry `CODE_REVISION`; populating it at compile time means a
//! deployed binary cannot lie about which source revision built it. The `dirty-<sha>` suffix
//! on uncommitted builds prevents a locally-modified tree from masquerading as a clean
//! release.
//!
//! Uses only `std::process::Command` (no tokio, no async). If `git` is not available on the
//! build host, falls back to "unknown" so offline / source-tarball builds still compile.
//!
//! ## Lint exemption
//!
//! `println!` is the cargo build-script PROTOCOL — `println!("cargo:rustc-env=...")` and
//! `println!("cargo:rerun-if-changed=...")` are how a `build.rs` communicates with Cargo.
//! There is no alternative API; the workspace `clippy.toml` bans
//! `println!` globally to enforce the stdout/stderr discipline, but build scripts run
//! in a separate compilation context and `println!` here is correct, not pollution. The
//! crate-level `#![allow(clippy::disallowed_macros)]` makes this exemption explicit and
//! audited: any future contributor cannot quietly add `println!` to a NON-build-script
//! source file without `cargo clippy -D warnings` rejecting the change.

#![allow(clippy::disallowed_macros)]

use std::process::Command;

fn main() {
    // Resolve the current HEAD commit SHA. `map_or_else` (rather than `.map().unwrap_or_else()`)
    // satisfies `clippy::map_unwrap_or` — single combinator, no allocation when git is missing.
    // The fallback case ("unknown") handles git absence AND non-git source trees (e.g., a
    // published crate tarball).
    let sha = Command::new("git")
        .args(["rev-parse", "HEAD"])
        .output()
        .ok()
        .and_then(|o| {
            if o.status.success() {
                String::from_utf8(o.stdout).ok()
            } else {
                None
            }
        })
        .map_or_else(|| "unknown".to_string(), |s| s.trim().to_string());

    // `git diff --quiet HEAD` exits non-zero when the worktree differs from HEAD —
    // catching BOTH staged-but-uncommitted changes AND unstaged worktree changes.
    // The bare `git diff --quiet` (no revision argument) compares worktree-vs-INDEX
    // only, which silently ignores `git add`ed-but-not-yet-committed changes — a
    // T-01-04 (code revision tampering) hole: a developer could `git add` a patch,
    // build a binary that records the previous clean HEAD SHA without the `dirty-`
    // prefix, then `git checkout` the file to restore the clean tree. The explicit
    // `HEAD` argument closes that gap.
    //
    // We treat "git not available" as not-dirty (we already fell back to "unknown" above).
    let dirty = Command::new("git")
        .args(["diff", "--quiet", "HEAD"])
        .status()
        .map(|s| !s.success())
        .unwrap_or(false);

    let rev = if dirty { format!("dirty-{sha}") } else { sha };

    // Emit the env var that `env!("TRADEDESK_CODE_REVISION")` reads inside lib.rs. The cargo
    // build-script protocol uses stdout — this is the legitimate exception covered by the
    // crate-level `#![allow(clippy::disallowed_macros)]` above.
    println!("cargo:rustc-env=TRADEDESK_CODE_REVISION={rev}");
    // Re-run only when HEAD moves (avoids invalidating the build cache on every cargo invocation).
    println!("cargo:rerun-if-changed=.git/HEAD");
}
