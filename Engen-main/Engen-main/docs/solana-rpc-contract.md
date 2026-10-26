# Solana RPC and Transaction Data Contract

This release remains read-only and paper-trading-only. It does not sign or submit
transactions.

## RPC contract

- `engine/solana_public.py` is the shared entry point for `getHealth`,
  `getSlot`, `getLatestBlockhash`, and `getTransaction`.
- `engine/wallet_monitor.py` uses the same validated JSON-RPC envelope helper
  for `getSignaturesForAddress`; it preserves signature-list slot,
  confirmation status, provider error, and block-time observations.
- Every response must be a JSON-RPC 2.0 object with a matching request ID and
  exactly one usable result or a well-formed provider error object.
- Transport/network failures, HTTP failures, malformed responses, and provider
  JSON-RPC errors are distinct from one another. Public error messages do not
  include endpoint URLs or credentials.
- Retries are bounded to at most five retries after the initial attempt.
  HTTP 429 and 5xx responses may be retried with exponential backoff; nonretryable
  HTTP failures stop immediately. Network failures also stop after the cap.
- `getTransaction` uses `encoding=json`, `commitment=confirmed`, and
  `maxSupportedTransactionVersion=0` deliberately. A future transaction
  version unsupported by this client must be reported as a provider error rather
  than silently interpreted.
- Transaction signatures must be base58 strings decoding to exactly 64 bytes
  before a `getTransaction` request is sent.

## Transaction interpretation contract

- The RPC signature remains the stable transaction identifier.
- Slot and signature-list confirmation metadata are retained separately from
  transaction metadata; an observation from `getSignaturesForAddress` is not
  itself proof of a successful swap.
- A buy/sell candidate requires a successful transaction, confirmed or finalized
  signature observation, known monitored-wallet signer information, and
  interpretable wallet-owned balance deltas. Insufficient confirmation produces
  `UNKNOWN` and leaves the signature retryable.
- Transaction fee (lamports) is recorded independently from balance deltas; it
  is not itself a trading loss.
- Token deltas are keyed by account index, mint, and owner, allowing multiple
  token accounts for one mint. Unknown owners are not attributed to the watched
  wallet. Native SOL and wrapped SOL/SPL quote-token observations are not mixed
  when classifying.
- Transaction summaries preserve available program IDs and instruction context.
  Program instructions and balance movements are evidence, not proof by
  themselves that a swap occurred.
- Missing transaction data returns an explicit unavailable result. Missing or
  malformed metadata is marked incomplete and must not be converted into a
  fabricated success, balance delta, or trade record.

## Regression coverage

The offline test suite covers successful, failed, not-yet-available, incomplete,
and malformed transaction responses; malformed signatures; JSON-RPC envelope
validation; provider/network error handling; bounded retries; transaction
version request parameters; wallet-owner filtering; and insufficient-confirmation
gating. Tests use fixtures/mocks and do not require a wallet secret or submit a
transaction.

## Checklist mapping (091–120)

- 091–093: shared client in `solana_public.py`; wallet signature requests use
  the shared envelope helper.
- 094, 100, 116, 118–120: response and transaction structure validation,
  explicit incomplete/unavailable states, and negative regression fixtures.
- 095–099: finite timeout supplied by callers, typed transport/HTTP/provider
  failures, bounded exponential backoff, request pacing, and retry cap.
- 101–103: unavailable transactions remain retryable; signatures are validated
  before detail requests; transaction version 0 is requested explicitly.
- 104–106: failed/success status, confirmation observations, slot, and signature
  identity are retained without conflating observations with proof.
- 107–112: fee remains separate; native SOL and SPL token deltas preserve
  account index, mint, owner, loaded addresses, and multiple-account activity.
- 113–115: multiple instructions/mints are retained in normalized balance
  changes and program/instruction context.
- 117: insufficient confirmation is `UNKNOWN`, not a buy/sell candidate.
