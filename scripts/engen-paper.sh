#!/bin/sh
# Build and run the paper-only Rust dashboard. Does not sign or broadcast.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$ROOT/rust/engen_core"
cargo build --release
exec ./target/release/engen_paper
