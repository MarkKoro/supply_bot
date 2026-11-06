# Instructions for coding agents

This repository participates in the Progressor ecosystem. Before changing auth, database schema, user identity, meal/workout/intake writes, or the WebApp entry point, read [the integration contract](docs/PROGRESSOR_INTEGRATION.md).

- Preserve the independent supply bot/API/database. Core downtime must not block native domain operations. Do not introduce synchronous Core calls into write transactions.
- Identity: users.id -> users.tg_user_id; stack/profile/labs/supplement_day_state.user_id uses the INTERNAL id; API helpers convert Telegram ids before SQL. Keep canonical Telegram ids as positive decimal strings at integration boundaries, bounded below 2^63. Never join domains by a local id, name, username, or matching column name alone.
- The temporary Core adapter is read-only. Changes to source columns/semantics also require reviewing `progressor_core/adapters.py` in MarkKoro/progressor_core and its integration tests. Core must never write this domain DB.
- Keep existing native Telegram proof verification and all domain permissions, bans, premium gates, and trainer delegation. The versioned `progressor_bridge.py` only supplies a verified identity; it does not grant domain capabilities.
- `progressor_bridge.py` is a verbatim copy of Core `shared/domain_bridge.py`. Update the canonical file and all three copies together; test expiration, tampering, audience, user isolation and disabled/native fallback. Signing keys, bot tokens, launch proofs and assertions never belong in Git, URLs, or logs.
- The shared navigation is served by Core at `/progressor/assets/navigation.v1.js`. Preserve its early launch-context capture and the fallback in this frontend. Do not send its header to another origin. Cross-domain navigation is enabled only after each API bridge is ready.
- Events use envelope schema_version=1, stable ids, monotonic entity revisions, UTC instants plus source day/timezone, duplicate/conflict handling and tombstones. Failed source reads are not deletions. Future outbox writes belong in the owner's transaction, not another DB.
- Food owns nutrient observations and personal nutrition targets. Unknown nutrient amounts are null/absent, never zero. Preserve units, source, confidence, portion revision and per-nutrient coverage. AI photo/text analysis provides per-item nutrients_per_100g estimates (schema v1, 54 registered codes); Food computes consumed quantities deterministically. Portion edits recompute AI observations; earlier human corrections become stale. Do not infer clinical deficiency or supplement doses from an incomplete food log.
- Production paths and rollout instructions are in the integration contract. Before changing production: discover current state, create and verify consistent SQLite/code/config backups, stage on separate copies with dummy tokens/providers and zero consumers, record exact rollback, deploy one service at a time, check health/logs/DB and preserve all later user writes.
- Git tracks the Food production SQLite file. Never run broad reset/checkout/clean that replaces a live DB. Never restore a domain DB merely to roll back additive code.
- Run the tests relevant to your change and update the integration document and durable work journal. Do not claim real Telegram/iPhone testing from browser emulation. Never start another consumer with the production bot token.

- Shared payment/cache/AI contract: read the updated integration document before editing premium checks, prompts, payment callbacks or user purge. Never overwrite native expiry with a bundle. After paid bundle orders exist, preserve financial compatibility during rollback. Synchronize canonical shared helper copies and test paid-source/target gating.

- Source export: the native API owns its online backup worker; Core reads only private atomically published DELETE-journal copies. Preserve snapshot freshness rejection and keep export/cache tasks independent. Never widen Core permissions to live database directories.

- Cross-context belongs to the verified personal account. Gym trainer/acting mode keeps Gym AI and must omit trainee Food/Supply data; a native trainer link alone grants no cross-product delegation. Explicit separate pupil nutrition permissions allow only aggregate KBJU/top5 in the native Gym coach menu, never trainee Food/Supply AI context.

- Native guidance/AI controls: read [docs/PROGRESSOR_NATIVE.md](docs/PROGRESSOR_NATIVE.md) before editing hints, completion, duration, nutrient actions, providers or attempt limits. Deterministic views/actions must not call AI. Keep canonical ai_guard/workout_metrics copies synchronized; preserve unknown values, paid/consented/fresh gating and account-scoped dismissal. Never infer clinical deficiency, auto-dose or eat back exercise calories.


Canonical Core source is now [MarkKoro/progressor_core](https://github.com/MarkKoro/progressor_core), published to main. Its production updater is `update-progressor` (`updatecore`). The former Gym integrations/progressor_core package has been removed. Core application code exists only in the separate Core repository; bots contain only their native adapters and owned data/UX logic. Bot-local shared helper copies still require synchronized bot main commits and native tests. Publish bot changes directly to main with a normal fast-forward update, as requested by the owner.

For native subscription examples, both exercise surfaces, profile reconciliation, deterministic nutrition plans and opt-in Food reminders, also read [docs/NATIVE_GROWTH.md](docs/NATIVE_GROWTH.md). Source data stay owner-local; do not add a consumer, diagnose from incomplete logs or retry uncertain Telegram delivery. Canonical nutrition_plan.py is mirrored only to Food and must match.


Native Telegram subscriptions and expiry: read [docs/BOT_NATIVE.md](docs/BOT_NATIVE.md) before payment grants/tariffs, source schedules, notifications or native menus. Canonical native_subscription helper requires synchronized all3 mirrors. After v2 gate/order, forbid Core protocol<2 or client schedule<2 rollback. Always preserve financial/user DB records; use native renderers and defer busy flows.

Supply reminder delivery: read [docs/REMINDER_DELIVERY.md](docs/REMINDER_DELIVERY.md) before changing send retries, leases or deferred jobs. A started request with unknown acceptance and an expired sender lease become durable `uncertain`, never an automatic resend. Only an explicit send rejection is retryable/permanent; an acknowledged message remains delivered if later local persistence fails. Preserve original meal/scheduled slot keys, native one-menu cleanup and later independent slots.

Subscription screen and camera pipeline: read [docs/SUBSCRIPTIONS_AND_FOOD_PHOTOS.md](docs/SUBSCRIPTIONS_AND_FOOD_PHOTOS.md). Keep a single canonical shared UI in Core, equal RUB/Stars choices and existing external Redirect/provider verification. Ordinary Telegram photos must remain the primary one-step input; file upload is optional. Never remove historical human nutrient records when removing editor controls.


## Selected native connections

Read [docs/SELECTED_FEATURES.md](docs/SELECTED_FEATURES.md) before changing shared profile commands, source-owned caches, reminders, day closure, food atlas/overlap, rhythm, timezone or coach permissions. Shared product_links/ai_cache/day_goal/food_sources/timezone_history are canonical in Core and mirrored together. Trainer aggregate nutrition is a narrow explicit consent exception; native trainer role/link alone never grants it or cross-product AI delegation. Keep source ownership, history/CAS/idempotence, no-replay rest, protocol2 and normal FF main4.


## Quality program — 2026-10-16

Read [docs/QUALITY_PROGRAM.md](docs/QUALITY_PROGRAM.md) before storage, access, AI, native UI or deployment changes. Preserve the request-owned context projection, source-owned doses, read-only GET behavior, durable AI attempts and explicit native readiness. Install declared requirements/constraints into a fresh runtime; retain the old runtime for rollback. Match the change to meaningful tests and record exact source/evidence; no extra consumers or real paid tests.

Critical launch mirror: `webapp/assets/progressor-bootstrap.v1.js` must exactly match Core `shared/bootstrap.v1.js`. Keep native own-proof requests independent of Core, preserve original-source identity across SDK deliveries, and prevent cross-navigation when its assertions cannot be transferred. The complete shared UI and backend remain canonical in Core.


## Native ecosystem journeys — 2026-10-17

Read docs/ECOSYSTEM_CONNECTIONS.md before changing daily summaries, source-health explanations, workout/meal completion or preferences-aware hints. Keep own-account/source+target access, freshness, Supply consent and explicit unknowns. New daily reads must stay small/read-only; never replace them with full history/AI generation. Shared progressor_bot.py stays canonical in Core with identical all3 mirrors. Sleep remains a documented future native connector, not a live source or Telegram permission.

## Bounded journey handoffs — 2026-10-19

Read the canonical [journey contract](https://github.com/MarkKoro/progressor_core/blob/main/docs/JOURNEY_HANDOFFS.md) before changing purpose routing, return, change-goal review or Food goal confirmation. Keep the single Core UI, native owned actions, account/proof binding and read-only navigation. A saved workout/intake does not prove plan review; explicit scoped review is separate from seen/dismissal. Food apply must reference its server preview and preserve concurrent owner edits; see Food docs/GOAL_PREVIEWS.md. Use the frozen JQ-v1 in Core docs/JOURNEY_QUALITY_RUBRIC.md for matched tasks, separate concept and observed evidence. Do not replace agreed 90-minute free-workout completion, Moscow Food day, meal-anchor parameters or native financial/consent contracts as a side effect.


## Verified second journey package — 2026-10-21

Keep the canonical owner-policy boundary and synchronized progressor_client mirrors; read Core docs/SUPPLY_OWNER_POLICY.md before changing source metadata, legal eligibility or updater recovery. The persistent owner-consent-v1-ready gate is independent from financial protocol2 and must survive ordinary code rollback. Supply keeps its single native consent predicate; Core reads only private owner snapshots.

Preserve operation-key retries in Gym /set/add_once (no legacy fallback after an uncertain response), explicit saved-time provenance in Food, and Supply uncertain-delivery suppression (an unknown Telegram response must never trigger a blind retry). Keep native functions available during Core outages.


## Native UI opening ownership — 2026-11-06

Keep a queued active-card opening owned by its current native parent and tab.
Parent Close/reopen, tab departure or a canonical Core overlay cancels only
that opening intention. An already-sent owner save and an opened card's read
may complete under their existing ownership; do not abort, replay or claim
server rollback. Native focus synchronization and document key handling yield
while the canonical Core settings dialog is open. Preserve same-origin launch/auth,
consent, native permissions and all payment/provider checks. Core remains the single
shared UI; this change adds no shared helper or mirror. See
docs/PROGRESSOR_INTEGRATION.md for standalone CJS and browser/Python boundaries.
