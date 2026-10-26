# Phase 7 task checklist — profiles, storage, portfolio data

Branch: `feature/phase-7-profile-storage`  
Implementation module: `engine/profile_store.py`  
Tests: `engine/engine/engine/tests/test_profile_store.py`

- [x] **181.** Selected SQLite (Python standard library) and documented the storage decision.
- [x] **182.** Database initializes automatically; schema version is tracked and newer schemas are rejected.
- [x] **183.** Defined version-1 schemas for profiles, profile settings, source-wallet associations, transactions, profile links, and balance snapshots.
- [x] **184.** Added validation for profile names/status, public addresses, JSON settings/evidence, timestamps, raw integer amounts, transaction labels/status, and token decimals.
- [x] **185.** Added initial schema-version initialization. A forward migration is not needed yet because this is the first schema version; future schema changes must add explicit migrations.
- [x] **186.** Destination profiles receive stable UUIDs.
- [x] **187.** Each profile has its own settings and source-wallet associations.
- [x] **188.** Multiple source wallets can be associated with a profile.
- [x] **189.** Source wallets may be shared across profiles; associations are stored separately.
- [x] **190.** Source-to-profile links are checked before transactions or balance snapshots are linked.
- [x] **191.** Settings are stored in a separate row per profile.
- [x] **192.** Tests verify changing one profile's settings does not alter another profile.
- [x] **193.** Destination public address is a separate field; settings reject secret-material field names, including nested fields.
- [x] **194.** Profiles track creation, update, status, and soft-deletion timestamps.
- [x] **195.** Transactions use stable signatures as persistent identifiers.
- [x] **196.** The transaction signature is a primary key; repeated submissions do not create duplicate rows.
- [x] **197.** Raw integer amounts are retained as strings with token decimals.
- [x] **198.** Observation timestamps are timezone-aware ISO-8601 values.
- [x] **199.** Transaction status, classification, and evidence JSON are persisted.
- [x] **200.** Balance snapshots store the observation timestamp.
- [x] **201.** Balance snapshots store source/provenance and optional freshness; aggregation can exclude observations older than a caller-specified age.
- [x] **202.** Historical snapshots remain stored; the `is_current` flag distinguishes current observations.
- [x] **203.** Portfolio holdings aggregate current observations using exact decimal arithmetic and retain raw totals, decimals, provenance, and observation range.
- [x] **204.** Zero-balance observations are retained instead of being silently deleted.
- [~] **205.** Missing token metadata is represented as `null` and does not break the storage API; dashboard display integration remains to be implemented.
- [x] **206.** Profile metadata can be updated; deletion is a soft delete/archive.
- [x] **207.** Profile edits/deletion do not delete transaction or balance history.
- [x] **208.** Added SQLite's online backup API wrapper and automated backup/restore coverage.
- [x] **209.** Tests cover reopening the database and duplicate transaction submissions.
- [~] **210.** Automated tests cover profile isolation, integrity constraints, persistence, and backup/restore. The latest commit's CI run must pass before calling this fully verified; production deployment recovery still needs an operational restore exercise.

## Verification

The safety/unit workflow passed on earlier Phase 7 commits, including compilation, unit tests, the read-only health check, and the repository-root entry point. The newest changes add recursive settings validation and its regression test; verify the workflow for the latest branch head before treating the whole phase as validated.

No changes have been merged into `main`. This module only stores read-only/paper-mode observations and does not sign or submit transactions.
