# Public market-price retry and timeout behavior

The read-only DEX Screener price fetcher uses bounded retries for transient
transport failures and retryable provider responses.

- Each network attempt uses the configured `timeout` (default: 8 seconds).
- The default maximum is 3 attempts; callers may lower or raise it explicitly.
- HTTP 429 and HTTP 5xx responses, URL/network errors, socket timeouts, and
  other OS-level transport errors are retryable.
- HTTP 4xx responses other than 429 are not retried.
- Backoff is exponential (default: 0.25 seconds) and capped (default: 1 second).
- Invalid JSON, oversized responses, malformed payloads, and unusable price
  data fail closed and return `None`; they are not converted into a price.
- Only successful valid snapshots are cached. Failed attempts do not populate
  the cache.

Example:

```python
price = fetch_market_price(
    mint,
    timeout=5,
    max_attempts=3,
    retry_backoff_seconds=0.25,
    max_backoff_seconds=1.0,
)
```

The timeout applies to each individual network attempt. Therefore the maximum
wall-clock duration is bounded by the configured attempt count, per-attempt
timeout, and capped retry delays. This remains read-only market-data access;
it does not submit, sign, or broadcast transactions.
