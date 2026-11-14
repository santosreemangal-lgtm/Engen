# Engen paper dashboard API

Served by `engen_paper`. Paper only. `real_trading` is false. No signing, no broadcast.

Auth: if `DASHBOARD_AUTH_TOKEN` is set, every `/api/*` call needs `Authorization: Bearer <token>`. Otherwise open. Limit: 120 calls / 60 seconds, then `429`.

| Method | Path | Body / query | Success |
|---|---|---|---|
| GET | `/` | dashboard HTML | 200 HTML |
| GET | `/health` | | 200 `{ok, mode: PAPER_SIMULATION}` |
| GET | `/api/wallets` | | 200 `{wallets: string[]}` |
| POST | `/api/wallets` | `{action: add\|remove, address}` | 200 `{wallets}` |
| GET | `/api/profiles` | | 200 `{profiles}` |
| POST | `/api/profiles` | `action`: `create`, `update`, `settings`, `associate-source-wallet`, `disassociate-source-wallet`, `delete` | create `201 {profile}`, else `200` |
| GET | `/api/profile-monitor` | `?profile_id=` | 200 `{profile_id, monitor:{enabled,wallets}}` |
| POST | `/api/profile-monitor` | `{action: start\|stop, profile_id}` | 200 monitor object. Inactive profile: `409` |
| GET | `/api/paper-simulation` | | 200 paper report (`simulated`, `available_balance`, `database_available`) |
| POST | `/api/paper-simulation` | `set-initial-balance`, `set-profile-initial-balance`, `add-balance`, `initialize-profile-ledger` | 200 `{report}` |
| GET | `/api/profile-transactions` | | 200 `{transactions}` |
| GET | `/api/monitor` | | 200 `{enabled, wallets, real_trading:false}` |
| GET | `/api/rpc-health` | | 200 `{available:false, status:paper_only}` |
| POST | `/api/classify` | `{wallet, token_balance_changes, signature?}` | 200 classification |
| POST | `/api/candidates` | array of monitor candidates | 200 `{queued}` |
| POST | `/api/fill` | `{mint, price_usd, liquidity_usd?}` | 200 `{outcomes}` |

Profile object: `profile_id`, `name`, `status` (`draft` or `active`), `source_wallets`, `destination_public_address`, `settings`.

Missing profile: `404 {"error":"not found: Profile not found."}`. Unknown action: `400`.
