# Read-only WebSocket wallet monitoring

## Architecture

The monitor uses two complementary paths:

1. **WebSocket notifications**: one Solana `logsSubscribe` subscription per monitored public wallet, using the `confirmed` commitment.
2. **Polling fallback**: the existing RPC scanner still runs on its configured interval (15 seconds by default), even while WebSocket is connected.

A WebSocket notification is only a wake-up signal. The monitor does not treat notification logs as a trade signal. It runs the existing read-only RPC scan, which continues to verify transaction/signature data and feed the existing paper-only pipeline. No signing, transaction submission, wallet secret, or live-trading behavior is introduced.

## Reconnect and failure behavior

- The WebSocket client reconnects after transport errors, server closure, malformed messages, or subscription rejection.
- Reconnect delay uses bounded exponential backoff: it starts at 1 second and doubles up to 30 seconds.
- Connection and receive operations use finite timeouts.
- A missing WebSocket package or unavailable WebSocket endpoint does not stop monitoring; periodic polling remains enabled.
- Stopping monitoring signals both the polling worker and WebSocket workers. Changing the monitored wallet set closes the old subscriptions before opening the new set.

## Duplicate protection

- Each WebSocket listener keeps a bounded least-recently-used set of the last 2,048 valid transaction signatures.
- Repeated notifications for a signature are ignored by the listener.
- WebSocket bursts are coalesced into a set of pending wallet addresses. A single monitor worker performs scans, preventing overlapping WebSocket-triggered and scheduled polling scans.
- The existing persistent signature store remains the durable duplicate guard across polling cycles and process restarts. The WebSocket cache is an in-memory fast-path only.

## Configuration

Set `ENGEN_WS_URL` to a provider's WebSocket endpoint when using a custom Solana RPC provider. If unset, the monitor uses Solana's public mainnet WebSocket endpoint:

```text
wss://api.mainnet-beta.solana.com
```

Install the transport dependency with the project's normal dependency installation:

```bash
python -m pip install -r requirements.txt
```

To disable WebSocket notifications while retaining polling, construct `MonitorService` with `websocket_endpoint=""`. The service status reports `websocket_enabled`, `polling_fallback_enabled`, each wallet's WebSocket state, reconnect count, last notification time, last signature, and whether its most recent scan came from WebSocket or polling.

## Offline regression tests

Run the WebSocket client and monitor integration tests from `Engen-main/Engen-main`:

```bash
python -m unittest \
  engine.engine.engine.tests.test_solana_websocket \
  engine.engine.engine.tests.test_websocket_monitor_integration \
  engine.engine.engine.tests.test_monitor_service -v
```

These tests use fake WebSocket connections and local callbacks. They do not contact Solana, submit transactions, or require a funded wallet. A separate live-provider smoke test is still needed to verify a particular provider's WebSocket availability and rate limits.
