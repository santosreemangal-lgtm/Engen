#!/usr/bin/env bash
# Launch the Rust-native dashboard runtime. This script never invokes Python.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ROOT="$REPO_ROOT/Engen-main/Engen-main"
MANIFEST="$APP_ROOT/rust-engine/Cargo.toml"
RUST_BIN="$APP_ROOT/rust-engine/target/release/engen-engine"

if [[ ! -f "$MANIFEST" ]]; then
  printf 'Rust engine manifest not found: %s\n' "$MANIFEST" >&2
  exit 1
fi

export ENGEN_APP_ROOT="${ENGEN_APP_ROOT:-$APP_ROOT}"

if [[ ! -x "$RUST_BIN" ]]; then
  cargo build --release --manifest-path "$MANIFEST"
fi

if [[ ! -x "$RUST_BIN" ]]; then
  printf 'Rust engine binary was not produced: %s\n' "$RUST_BIN" >&2
  exit 1
fi

exec "$RUST_BIN" serve-dashboard
