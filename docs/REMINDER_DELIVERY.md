# Native reminder delivery outcomes

Supply owns the existing scheduler, Telegram consumer, reminder menu and SQLite
`reminder_fires` ledger. The key remains the original
`(Telegram user, supplement, local day, local slot)` for both meal anchors and
scheduled reminders. This change creates no Core write, AI request or consumer.

## Outcomes at the send boundary

| State | Meaning and next action |
| --- | --- |
| `sending` | A token and five-minute lease reserve the exact original slot before sending. |
| `delivered` | Telegram returned a message. A later local persistence or menu-cleanup exception cannot authorize another send. This is not an intake confirmation. |
| `uncertain` | A request started but no definitive response arrived, or a sender lease expired after a crash. Keep the outcome durable and do not retry this slot automatically. |
| `pending` | Preparation failed before sending, or Telegram explicitly rejected the send with `RetryAfter`. Retry no earlier than the provider delay and the normal one-minute minimum. |
| `failed` | The actual send returned `Forbidden` or `BadRequest`; this original slot is not retried. |
| `deferred` | No send was attempted because input/navigation was busy. The existing JobQueue carries the original slot references and settles their ledger when it runs. |
| `suppressed` | The owner is unavailable, no eligible positions remain, notifications are disabled/resting, or no deferred job can be scheduled. No intake is recorded. |

The native renderer records the actual `send_message` boundary separately from
its later SQLite work. A `RetryAfter` raised by unrelated preparation or after a
confirmed send is not mistaken for a provider send rejection. Cancellation of an
in-flight request records `uncertain` and still propagates cancellation. Expired
tokens cannot invent a later successful receipt. Historical rows keep their
existing default `delivered`. Additive `delivery_version` distinguishes new
classified pending outcomes from historical ambiguous pending rows; legacy
pending migrates to `uncertain`, without replay. Repeated startup preserves a
new known retry. Additive `first_reserved_at` records the first reservation UTC
epoch, unchanged by later retries; an index supports delivery-state lookups.

An uncertain send may have reached Telegram. Telegram provides no idempotence key
for this operation, so exactly-once delivery cannot be guaranteed. The selected
policy prefers skipping a doubtful reminder to sending a possible duplicate.
It neither marks a supplement taken/skipped nor prevents a different subsequent
slot or tomorrow's ordinary reminders. Optional meal anchoring and scheduled
delivery share the same original slot and cannot independently replay it.

## Existing menu and deferral rules

Use the native one-menu renderer. After an acknowledged new menu, retire the
previous tracked menu even if local state persistence fails. Cleanup still
depends on Telegram accepting deletion/markup removal; no claim is made that a
network outage can remove every visible stale message. Callback IDs retain the
owned supplement/day checks. Financial messages are not reminder menus.

Busy flows remain in the existing JobQueue; this change does not add a durable
job runner. Original references avoid replay when a deferred callback is invoked
twice. Deferred jobs are not replayed after a local-day change, and rest disables
sending. As before, an application restart can lose a busy/snooze JobQueue job;
the next independent scheduled slot still works. There is no catch-up of these
old deferred rows after restart/rest. Explicit snooze keeps its native semantics.
Merging a meal-triggered and an ordinary deferred job preserves both original
slot references and the meal association. The message does not claim that a
busy-delayed reminder arrived earlier than its original time. A referenced slot
already `uncertain` is not added back through the merged list of supplement IDs.
The existing cached `last_pause_started_at` also suppresses a `pending` or
`deferred` reservation made before rest, even if its job never ran during rest
and the user returns on the same day. A missing/invalid cutoff or an older pause
does not suppress a later fresh reservation. A different new slot after return
still runs. This is source-owned logic, with no Core change or cross-DB
transaction. Old code can read/write the additive table and does not claim the
new terminal states; rolling back to old send code cannot retain the improved
classification guarantees for future sends. Keep ledger rows and user writes.

## Verification and rollout

`tests/test_reminder_delivery.py` fault-injects the real scheduler, renderer and
temporary SQLite with a fake Telegram provider: accepted/lost response, reopening
the DB, expired sender lease, explicit rate rejection, permanent rejection,
pre-send failure, post-ack persistence failure, cancellation, next slot,
meal/scheduled deduplication, busy replay, rest, old day and explicit snooze.
These are offline outcomes, not an observed production duplicate rate or a real
Telegram-device test. Run the existing reminder journey/selected/native tests
too. A release still needs the normal bounded backup, source-frozen Linux checks,
drained worker rollout and independent live checks. Keep user data and the ledger
on code rollback; never restore an old domain DB to roll back this change.

## Несколько времён и отдельные приёмы — 2026-10-22

Несколько времён у одной позиции — повторные напоминания об одной дневной
отметке. `taken` или `skipped` закрывает все следующие времена этой позиции
сегодня. Без отметки остаётся следующее независимое время; пропущенное время
не догоняется. Доставка не создаёт отметку приёма.

Для самостоятельных приёмов используется существующее явное «Разделить приём»:
собственная копия, своё время и собственная дневная отметка. Native экран
нескольких времён объясняет это и ведёт в прежний split callback. WebApp
объясняет дневную отметку рядом со временем; единственная прежняя split-кнопка
перенесена сюда, её обработчик/подтверждение/отмена сохранены. Открытие или
закрытие экрана копию не создаёт. Нет нового writer, миграции истории,
назначения/суммирования доз, дополнительного push или зависимости от Core.

Быстрое «Разделить → отменить → разделить» раньше могло столкнуться с UNIQUE
slug в пределах одной секунды: отменённая пользовательская копия сохранялась
в каталоге, а следующий приём получал то же время/порядок. Новая копия получает
UUID-суффикс вместо секундного времени. Исходные/отменённые записи, их ID,
отметки и дозовые снимки сохраняются; схема и обычный split transaction прежние.
Это не дедупликация разных явных приёмов. Native pending split и блокировка
WebApp-кнопки во время запроса остаются прежними.
