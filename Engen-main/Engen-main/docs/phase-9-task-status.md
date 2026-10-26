# Phase 9 task status — Dashboard, API, and user experience

Branch: `feature/phase-7-profile-storage`  
Dashboard: `dashboard/index.html`  
Backend: `dashboard/server.py`

## Current implementation status

- [x] **241.** Inspected the current dashboard before visual changes.
- [x] **242.** Preserved the existing dark theme, branding, wallet management, monitoring controls, and paper-simulation panel.
- [x] **243.** Reused shared CSS variables and consistent panel, form, profile-card, history-card, and button styles.
- [x] **244.** Buy and sell candidate classifications use consistent positive/negative colors and visible classification text. Unknown classifications have a separate warning style.
- [~] **245.** Success, warning, error, buy, and sell styles use readable text labels and strong contrast on the current dark backgrounds. A full assistive-technology/accessibility audit remains.
- [x] **246.** Engine Overview summarizes mode, monitoring state, last successful check, and qualified RPC observations.
- [x] **247.** Read-only monitoring mode is explicit; paper simulation is shown in a separate panel with safety metadata checks. No live mode is implemented.
- [x] **248.** Last successful monitoring check is shown when available.
- [~] **249.** RPC state is inferred from monitor scan outcomes; there is no independent RPC health probe.
- [x] **250.** Added separate destination-profile cards.
- [x] **251.** Each profile shows its destination public address or clearly states that one is not configured.
- [~] **252.** Profiles show stored SOL/token balance snapshots with token mint, amount, wallet, source, observation age, and recorded freshness. A live balance refresh endpoint is not connected.
- [x] **253.** Stored observed balances/transactions are labeled separately from simulated paper-trading values.
- [x] **254.** Source public wallets are listed per profile and can be associated through a validated API.
- [x] **255.** Profile-specific JSON settings can be viewed and updated; invalid JSON and secret-like authorization fields are rejected.
- [x] **256.** Added profile-scoped transaction history filters for classification, status, and token mint.
- [x] **257.** History displays signature, source wallet, token mint, amount/raw amount, decimals, timestamp, status, slot, and classification evidence when stored.
- [x] **258.** Unknown/incomplete classification is not converted into a confirmed buy or sell.
- [~] **259.** Paper-simulation fees and network costs are displayed separately. The profile transaction schema does not yet persist a verified per-transaction fee, so profile-history fees are explicitly marked unavailable.
- [~] **260.** Paper realized P&L is shown separately from unrealized P&L; unrealized P&L stays unavailable until a verified fresh market-price provider is connected. Profile-level market valuation is not implemented.
- [x] **261.** Added loading, empty, stale-observation, API-error, and backend-unavailable messages for the implemented panels.
- [x] **262.** Transaction history is bounded to 25 records per page, with previous/next controls and validated server-side pagination capped at 100 records per request.
- [x] **263.** Profile names, public addresses, JSON settings, filter values, page values, and server-side payloads are validated.
- [x] **264.** Create/update/delete/association and history actions provide success or failure feedback.
- [x] **265.** Removing saved wallets and soft-deleting profiles requires confirmation. Profile history is preserved by soft deletion.
- [~] **266.** Responsive styles support narrow/mobile and wider layouts. Real browser verification across desktop/tablet/mobile is still needed.
- [~] **267.** Keyboard focus styles and high-contrast status colors are present. Full keyboard-only and screen-reader testing remains.
- [x] **268.** Top-level and endpoint-specific response shapes are checked before profile/history values are rendered.
- [~] **269.** Backend-unavailable and missing-database cases have safer handling; a full browser-driven outage test remains.
- [~] **270.** Simulation values are shown only when safety metadata explicitly confirms paper mode, simulated values, no real wallet balances, no real trading, and no broadcast. UI browser automation remains outstanding.

## Files changed

- `dashboard/server.py`: read-only profile report API; profile create/update/settings/source-wallet/soft-delete actions; filtered, profile-scoped, bounded transaction-history API; no fake balance or fee data.
- `dashboard/index.html`: destination-profile cards and forms; source-wallet associations; profile-specific settings; observed balance provenance and freshness; classification/status colors; transaction filters and pagination; safer API response checks and user feedback.
- `engine/engine/engine/tests/test_dashboard_profile_api.py`: tests for stored balance provenance, pagination, filtering, validation, and read-only behavior when the profile database is absent.

## Verification

The GitHub Actions safety/unit workflow succeeded on commit `35b752b5c04f4e89ca051dd5260fefed0ace2308`:
- Python compilation passed.
- **240 unit tests passed.**
- Disposable profile-store backup/restore passed.
- Read-only health check and repository-root entry point passed.

This workflow does not run a real browser. A desktop/tablet/mobile browser pass and automated UI interaction tests have not yet been confirmed. The separate read-only RPC benchmark is a network benchmark, not proof that all dashboard features work end-to-end.

## Remaining limitations

The implemented profile dashboard reads existing profile-store snapshots and transaction records. It does not yet independently refresh destination-wallet balances from RPC, persist a verified fee for each profile transaction, or value real holdings with a verified market-price provider. RPC health remains inferred from monitor scan results rather than a separate probe. These fields are shown as unavailable instead of being invented.

No live trading, transaction signing, or transaction broadcasting was enabled. No changes were merged into `main`; the work remains on the feature branch.
