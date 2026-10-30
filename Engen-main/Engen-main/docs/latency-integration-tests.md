# Separate latency integration tests

The latency integration suite is intentionally separate from unit tests:

```bash
python -m unittest engine.engine.engine.tests.test_latency_integration -v
```

It exercises the real dashboard HTTP handler and the monitor bridge together.
The test substitutes deterministic wallet-monitor and paper-order collaborators,
records scan-stage timings, then reads those measurements through
`GET /api/latency`.

The test binds only to an ephemeral localhost port. It uses no live Solana RPC,
wallet credentials, transaction signing, or transaction broadcasting. Paper
queueing and execution collaborators are mocked so the test verifies
instrumentation/API integration without creating or submitting orders.

GitHub Actions runs this integration test independently ten times.
