# Progressor integration v1 — supply

Status: implementation prepared; deployment verification and release commits are recorded in the top-level task INTEGRATION_STATUS.md. This document is the permanent repository contract for future working chats.

## Independent services and source ownership

Gym, Food, Supply keep independent processes, APIs and databases. The new Core uses its own SQLite at /var/lib/progressor/core.sqlite3 and loopback 8040. Core source is an independently runnable package at `integrations/progressor_core/` in [MarkKoro/gym_diary_bot](https://github.com/MarkKoro/gym_diary_bot). Code is colocated for versioned delivery through the existing authorized repository; domain applications do not import Core, share its DB, or depend on its Python environment. It can later move to a fourth repository without changing contracts.

This domain DB: `/srv/supply_bot/data/supply_bot.sqlite3`. Public routes: /supply/; API /supply/api/ -> 8030 with stripped /supply prefix. Identity: users.id -> users.tg_user_id; stack/profile/labs/supplement_day_state.user_id uses the INTERNAL id; API helpers convert Telegram ids before SQL.

Core reads short mode=ro/query_only SQLite snapshots, maps domain users to verified Telegram subjects, normalizes events, and stores an idempotent ledger/projections/checkpoints. Poll interval is 30 seconds, stale cutoff 120 seconds; failed snapshots back off to 300 seconds and retain previous data as stale. Successful entity disappearance creates a tombstone; a failed read never does. Historical bootstrap does not send notifications.

## Event and aggregate contract

Envelope: event_id, event_type, schema_version=1, source, user_id (decimal string), occurred_at (aware UTC), received_at (Core-owned), payload, correlation_id. Payload entity_id and revision are mandatory. Same id/same content is a duplicate; changed content is a conflict. Old revisions cannot resurrect a newer tombstone. Current snapshot revisions live in Core, not in source DBs.

Gym: workout.completed/updated/deleted, profile.updated, bodyweight.updated. Completion comes from day_completion_history; sessions/running/sets enrich the record. Strength duration and energy are unknown when not recorded. Food day matching converts the completion UTC instant to Europe/Moscow, preserving Food's existing calendar-day semantics.

Food: meal.logged/updated/deleted, daily_nutrition.updated, nutrition_target.changed and profile.updated. Existing macros remain owned by meals/goals. Additional observations live in meal_nutrients keyed by meal_id/nutrient_code, with value (nullable), canonical g/mg/ug, source, confidence, reference, meal_version, revision and UTC updated_at. Registry: nutrients.py / Core schemas/nutrients.v1.json. Historical missing micronutrients are not backfilled. Unknown != known zero. AI photo/text/voice-description analysis automatically emits nutrient_schema_version=1 and item nutrients_per_100g with registered codes/canonical units; unknown=null. Typical food composition is an approximate ai_estimate, not a verified laboratory value or food database citation. Python computes consumed quantity and retains observed/total component coverage. Macro-only history remains unknown. Food owns writes and access checks; Core only reads.

Supply: profile.updated, supplement.started/stopped/taken, lab.created/updated; current user_supplements and authoritative supplement_day_state (not the legacy user_intake_log). Internal user ids must join users.id. Deleted/banned users are excluded; lab metadata requires existing consent. Raw lab files, medical notes and billing data are not copied to Core.

Core read API: authenticated `/progressor/api/v1/me/context` and `/progressor/api/v1/me/recommendations/history`; the subject comes from a signed session, never a caller-supplied user id. Context includes activity, Food 7/30/90 day aggregates, targets, per-nutrient units/coverage/quality, Supply summary, source freshness and diagnostic reason codes. Future producers may use `/progressor/api/v1/events` with source-specific private ingest credentials; this is disabled unless credentials are configured. Invalid payloads get a durable hash-only dead letter.

## Authentication and common WebView

Each original bot still opens its original URL. The head script captures initial Telegram launch data before frontend hash routing changes it. Core verifies the launch using that bot's token and issues 15-minute assertions for gym/food/supply/core. A browser keeps these in sessionStorage, never URLs. The fetch bridge attaches only the current audience assertion to known same-origin API routes. Each API verifies locally; native bot HMAC validation is retained. Domain roles, trainer editing and bans are applied after identity verification exactly as before.

Flag: `PROGRESSOR_AUTH_ENABLED=1`; key path: `PROGRESSOR_SIGNING_KEY_FILE=/etc/progressor/signing.key`, root-only and outside Git. Core has `PROGRESSOR_ENABLED_DOMAINS` for incremental rollout; links stay disabled until a target API is ready. Native launches continue when Core is unavailable. Existing foreign-domain assertions work until expiry; a new cross-product session requires Core availability.

## Implemented interactions and deliberate limits

1. Completed Gym workout -> Core -> Food current-day activity card, with source/day basis and missing duration/energy explanation. Food targets are not silently changed.
2. Food history -> Core deterministic averages -> Supply context/recommendation comparing recorded protein with the user's current personal Food target. At least 3 logged completed days within 7; 80% is a display rule against a personal goal, not a medical threshold. Missing days are excluded, never interpreted as no food. Recommendations have rule version, provenance, calculation, coverage, uncertainty and stable history id.
3. Food automatically estimated micronutrients/other nutrients -> Core aggregates by nutrient and recorded day -> shared Supply context. Missing observations remain unknown and unit/form ambiguity is rejected. There are no micronutrient deficiency claims or automatic supplement prescriptions.
4. Supply stack/intake/lab metadata -> authenticated shared context in Gym/Food. Domain data is not mutually overwritten. No extra Telegram consumer or unsolicited notification sender exists; notification routing records webapp_only.

## Tests, diagnostics, migration and recovery

Run Core: `python -m unittest discover -s tests -p 'test_*.py' -v` from the independent package. Test duplicates/conflicts/order/deletion, each unavailable source, timezones, one/all-product membership, historical mapping, 64-bit ids, proof tampering/expiration/audience, cross-user isolation, source read-only guarantees, nullable nutrient coverage and replay/rollback. Food: existing audit suite plus tests/test_progressor_nutrients.py. Browser tests use isolated synthetic launch proofs and API-only staging; production tokens/providers are never used for a test consumer.

Health: each original API retains its existing endpoint. Core /health, /ready, private /metrics; JSON logs include correlation ids and fixed reason/error classes, not launch data, secrets or user payloads. Context reasons explain missing membership/source/history/target and a non-triggered rule.

Rollout: verified backup -> independent Core -> one API/bridge -> verify -> next -> activate navigation after all target bridges -> verify context flows. Food schema addition is additive; old code can still read/write macros. Legacy meal deletion is cleaned by an additive trigger. Portion changes recompute per-item AI estimates in the same transaction; earlier human whole-portion corrections become stale until reconfirmation. On rollback restore code/env/config and restart only the affected process; leave live DB and all later user data intact. Retain the new table for forward recovery.

Baseline checkpoint: /root/progressor-backups/20261007T120054Z-pre-github-sync.tar.gz; offline verified copy exists in task outputs/backups. Before integration rollout create a fresh checkpoint with exact changed-file/unit/proxy rollback. Never expose these archives or credentials in GitHub.

## Extending to a fourth module

Add a versioned source/adapter and owned event schema to Core, with source-specific auth, memberships, read-only or outbox delivery, staleness semantics, provenance and tests. Add one audience/navigation entry after its backend verifier is deployed. No existing domain DB should be altered just to add the fourth product.


## Paid cross-context and AI prompt contract (2026-10-07)

Membership identifies a Telegram account; it does not confer payment access. Core publishes
native paid expiry from each owner (Gym subscriptions join internal user id; Food premium_until
is naive UTC; Supply premium_until_utc). Only active native payment or central bundle expiry
opens cross-context. Native trials/admin privileges remain unchanged inside their product and
do not automatically open paid sources. An unpaid receiving product gets no cross hints.
The 8 possible combinations of native subscriptions are tested; no Supply hints for Gym+Food only.
Unpaid/missing sources are null/absent, not zero intake or an upsell disguised as a recommendation.
Stale sources are excluded from AI; a source outage never converts observed history to zero.

`progressor.cross_context.v1` carries bounded, source-labelled structured inputs, observation dates,
units, coverage and uncertainty. Supply height/weight are self-reported and require existing consent.
Gym recent body weights retain separate provenance; no owner profile is overwritten. Gym AI system
generation/analysis receives extra context beside its own profile. Food photo/text composition is
based on foods/portion only; its goal-survey AI can use paid relevant profile/activity data. Supply
assistant receives observed Food history and Gym activity outside its DB transaction, preserves
native job leases/input-version validation, and never treats approximate incomplete intake as a
clinical deficiency. Labels are appended by backend code only for actually submitted sources.
Food keeps all 54 automatic nutrient observations atomically with the existing meal; unknown=null,
estimated quality=ai_estimate, deterministic portion arithmetic. Historical macro-only records
do not gain invented nutrients. Register new units/codes in the versioned catalog and update tests.

## Shared bundle, durability and platform routes

Core owns bundle_orders, immutable provider requests, receipt/refund ledger and bundle_entitlements.
No domain expiry is overwritten. An additional bundle extends from max(now, previous bundle expiry);
separate subscriptions keep their own dates, and overlapping native/bundle periods do not add together.
Default 30/90/180/365-day plans: RUB 599/1590/2990/5490 and XTR 450/1190/2250/4190; no autorenewal.
Prices are env-configurable; each quote snapshots price/currency/duration/owner/source/terms before
checkout. Telegram bot/WebApp purchases use XTR, per Telegram digital-goods rules. RUB is an
independent browser flow `/progressor/subscribe`: explicit existing-bot login confirmation, receipt
email and YooKassa redirect. Website tokens are separate from mini-app assertions. Never add a
RUB embedded checkout inside Telegram. Existing individual product purchase routes remain independent.

Each owner writes only its own additive progressor_bundle_cache and progressor_payment_outbox.
The existing bot consumer persists successful/refunded Telegram receipts before network calls;
Core reads these outboxes read-only and retries fulfillment. API cache workers use signed loopback
grants every 15 seconds. Idempotent user/revision updates prevent old grants restoring a refund.
Payment precheckout verifies owner/amount/currency/source and reserves one query, but grants nothing.
Only successful_payment or authenticated YooKassa GET grants access. Duplicate charge events cannot
extend twice; a refund received before delayed success remains refunded. Existing native expiry,
credits, trials, AI quotas, roles and bans retain owner enforcement. Bundle Food analyses use the
normal subscription quota and do not debit PAYG; original stored payment preference remains intact.
Native renewal/gift functions must use native expiry, never the effective expiry display helper.

`progressor_client.py`, `progressor_bot.py`, and `progressor_bridge.py` are versioned canonical copies
from Core shared/. Change the canonical implementation and all 3 copies together, comparing hashes.
HMAC private calls bind source/path/body/time; browser assertions cannot authorize them. Public
Nginx denies internal routes and metrics. Env flags PROGRESSOR_AUTH_ENABLED and
PROGRESSOR_CLIENT_ENABLED default 0; signing key lives outside Git. All source databases are
mode=ro/query_only for Core. The Core package is a separate process/venv/DB; domain apps must not
import Core or read another domain DB. A future event producer can replace each snapshot adapter
without altering UI/AI/billing contracts. New modules require registry/event/entitlement mappings,
not a mesh of direct inter-domain reads/writes.

## Operational recovery

Core outage keeps native features and previously granted bundle caches until real expiry. Cross
navigation can use unexpired 15-minute assertions; after expiry it needs Core again or a native
launch from the current product's bot. Do not accept expired assertions indefinitely. New checkout
is rejected when Core/rollout is unavailable; a recorded successful payment is replayed, not charged
again. YooKassa creation retries preserve exact request/idempotence key for at most 23 hours;
later unknown outcomes require support. Provider redirect callbacks never activate access.
Pending orders and verified full-refund amounts reconcile in a separate worker so payment outages
do not stall source synchronization. Partial RUB refunds retain product access until a full refund.

Before any production change verify consistent SQLite + code/config backups by restored copy and
SHA256. Never replace the tracked Food DB during git synchronization. Deploy only reviewed source
paths, restart one affected unit at a time, retain DB/WAL/SHM and user writes. Roll back navigation
and disable Core enabled domains first. Once bundle charges exist, retain payment handlers, cache
workers, effective access helpers and Core ledger/reconciliation; blindly restoring old bots would
strand paid users. Use per-change code rollback with financial compatibility preserved. Closed backups
include env and must never be committed or published. Staging tokens/providers are dummy; zero
additional Telegram consumers and zero real test charges. Record release commits, server changes,
health/results and remaining physical-device test limits in the durable journal.
