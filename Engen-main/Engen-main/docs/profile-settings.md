# Destination profile settings (Task 044)

Status: specification only. Destination profiles and profile-specific settings are not yet implemented by the current application.

## Design requirements

Every destination profile must have a stable, opaque `profile_id` and its own settings record. A setting belonging to one profile must never be applied to another profile. Profiles start disabled. Creating or editing a profile must not start monitoring, create a wallet connection, sign a transaction, or submit a transaction.

## Proposed version-1 defaults

| Setting | Default | Rule |
| --- | --- | --- |
| `enabled` | `false` | Explicit user action is required before any monitoring associated with the profile starts. |
| `mode` | `paper` | Only read-only observation and paper simulation are in scope; this is not a live-trading switch. |
| `source_wallet_ids` | empty list | No source wallets are attached until explicitly selected and validated. |
| `destination_public_address` | `null` | A public address may be saved separately; no private key or recovery phrase is stored in a profile. |
| `quote_mint` | `null` | Must be explicitly selected and validated before profile-specific paper simulation can run. |
| `paper_starting_balance` | decimal string `"100"` | Virtual units only, consistent with the current paper runner's default. The quote mint must be known before this value has a currency/token meaning. |
| `scan_interval_seconds` | `30` | Seconds; must be at least the monitor service's current minimum of 30 seconds. |
| `signature_limit_per_scan` | `10` | Must be an integer in the supported range 1–1000. |

These defaults are proposed contract values for future implementation, not a claim that a profile system currently exists. A profile with missing or invalid required settings stays disabled and reports a validation error instead of guessing a value.

## Validation and isolation

- Validate `profile_id` and all source-wallet references before saving.
- Validate public Solana addresses independently from any future authorization mechanism.
- Store decimal amounts as strings or exact decimal values, not binary floating-point values.
- Validate the quote mint against the supported mint allow-list used by the paper runner.
- Updating one profile must not mutate another profile's settings, balances, or history.
- Keep application-wide configuration (RPC endpoint, request timeout, log level, and global emergency-stop state) outside this per-profile record.
- Do not add a live-trading mode or transaction-signing material to this profile settings schema.
- Until persistent profiles and their tests are implemented, the existing single-wallet CLI and dashboard remain the active behavior.

## Acceptance evidence for Task 044

This document defines the profile-specific setting names, defaults, validation expectations, and safe disabled-by-default behavior. Implementation and isolation tests are separate tasks and remain incomplete.
