# Owner consent export

Supply owns the current-document predicate in `legal_consent.py`.
`db.has_required_consent` and the private snapshot metadata writer reuse the
same decision with the current `OFFER_VERSION` and `PRIVACY_VERSION` captured
at the start of each export cycle. Do not copy those policy constants into Core.

Only Supply's existing webapp lifecycle passes the metadata factory to the
canonical Progressor publisher. It enriches the already-created temporary online
backup, not the live native database, and publishes both metadata and domain
facts as one checked atomic SQLite copy. Core receives only a closed, private
DELETE-journal snapshot with exact native-id/Telegram-subject binding.

Core's canonical contract and staged readiness/recovery requirements are in
`progressor_core/docs/SUPPLY_OWNER_POLICY.md` in the Progressor Core repository.
The ready gate must be backed up alongside the financial gate before activation.
No current document decision creates a source-sharing preference, subscription
or trainer permission. Native Supply functions retain their own access checks.

`tests/test_owner_consent_export.py` exercises the real native/export decision,
concurrent live changes after backup, stable policy vs fresh generation, captured
settings, failure and deadline. Real Telegram, AI and payment requests are not
needed. Shared `progressor_client.py` changes belong in Core's canonical shared
file and must be mirrored byte-for-byte to all three bots.
