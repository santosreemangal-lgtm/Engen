# Rust paper deployment

The supported entry point is the `engen_paper` binary. It does not sign or broadcast.

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
