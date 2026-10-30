# End-to-end paper pipeline regression

The isolated E2E regression exercises the offline path across the real monitor
service, monitor bridge, paper-copy order queue, SQLite-backed paper simulator,
and restart recovery.

## Scenario

1. The monitor performs its initial polling scan and receives no candidate.
2. A deterministic WebSocket listener emits a wallet notification.
3. The monitor immediately rescans the wallet through the normal scan callback.
4. The mocked public-RPC observation boundary supplies one confirmed BUY
   candidate signed by the monitored public wallet.
5. The real paper-copy queue validates the observation and creates one paper
   order using the configured fixed virtual USD size.
6. The market-price provider is deliberately unavailable, so the order remains
   queued and no simulated fill is recorded.
7. A fresh simulator instance reopens the same temporary SQLite database and
   verifies that the order and virtual balance survived restart.

## Run locally

From `Engen-main/Engen-main`:

```bash
python -m unittest engine.engine.engine.tests.test_paper_pipeline_e2e -v
```

GitHub Actions runs this isolated test ten times in the paper-simulation
regression workflow.

## Safety and scope

This is an offline E2E regression, not a live-network acceptance test. The
wallet RPC observation and public market-price provider are deterministic test
doubles. The monitor, bridge, queue logic, paper simulator, SQLite persistence,
and restart recovery are exercised. The test does not use wallet secrets,
sign transactions, broadcast transactions, or enable live trading. Market data
is unavailable by design, so the expected outcome is one persisted CREATED
paper order and zero fills.
