# Rust paper deployment

The supported runtime is `engen_paper`. Python is not required to build, test, or run it. Old Python files may still be in the repository; they are not the service entry point.

The `rust-paper` GitHub Action runs `cargo test`, builds the release binary, and fails if `scripts/engen-paper.sh` or the systemd unit calls Python.


## Run

```bash
sh scripts/engen-paper.sh
```

Open `http://127.0.0.1:8787/`.

## systemd

```bash
cargo build --release --manifest-path rust/engen_core/Cargo.toml
sudo install -d /opt/engen/data
sudo cp -a . /opt/engen
sudo cp deploy/engen.env.example /opt/engen/engen.env
sudo cp deploy/engen-paper.service /etc/systemd/system/engen-paper.service
sudo systemctl daemon-reload
sudo systemctl enable --now engen-paper
```

Put a reverse proxy in front if the bind is not localhost. Then set `DASHBOARD_TRUST_PROXY=true` and a `DASHBOARD_AUTH_TOKEN` of at least 32 characters. `ENGEN_LIVE_ENABLED=true` is refused.
