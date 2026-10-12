# Связь продуктов: выбранные функции и контракт

Канонический сервер и правила находятся только в [MarkKoro/progressor_core](https://github.com/MarkKoro/progressor_core), main. Боты содержат свои адаптеры и самостоятельные функции; сервер Core не импортируется в боты. Правки публикуются обычным fast-forward в main4 и обновляются существующей командой `update-all` (`updateall`).

## Владение и источники

Core пишет только свою SQLite. Владельцы публикуют private atomic snapshots каждые30с и самостоятельно применяют подтверждённые команды. Telegram id — ключ аккаунта, внутренние id каждого продукта различаются. Отказ чтения не означает удаления. Для личной связи требуются оплаченный/trial/gift доступ источника и получателя, свежесть120с, выбранная связь, согласие Supply. Истечение одного доступа не удаляет историю или остальные подписки.

| Выбор | Реализация |
|---|---|
| П1 | Вес из трёх профилей с происхождением. Явный выбор→команды владельцам с expected value/time, CAS, UUID, TTL1ч и durable receipts. История/рост/цель сохраняются; новая правка не перезаписывается. |
| П2 | Добровольное «На сегодня всё», один вопрос внутри подходящего вечернего ответа после≥8 вечерних дней из14, предварительный итог. Две проигнорированные подсказки→пауза7д. Молчание не подтверждает полноту; add/edit/delete открывает день. Ночная смена не получает обязательный вопрос. |
| П3,К11,К12,К17 | Supply «Найти в еде», все54 кода,8 на страницу, возврат к исходной карточке. NIH/USDA, предпочтения/исключения; обогащённые продукты требуют проверки этикетки. Неизвестное не ноль, ALA не EPA/DHA. |
| П4 | Один opt-in план в неделю, defaultMonday10 Food notification TZ, время можно выбрать. Для чисел≥5 подтверждённых дней и стабильная личная цель; иначе только покрытие. Простые продукты фильтруются по питанию/исключениям; явно сохранённая страна Supply учитывается лишь при разрешённом источнике. |
| П5 | Последние6 собственных блюд, preview порции, token20мин, atomic confirm/source-version/idempotence. Без ИИ/списания; неизвестные нутриенты остаются неизвестными, актуальные человеческие поправки сохраняются. |
| П7,К42 | Свободная сессия общая для фактических записей обеих поверхностей. Явный finish или90мин после последней записи; чтение/блокировка телефона не активность. Не выдумывать duration. Повтор не удваивает занятие; новая запись открывает его. Следующий native reminder продолжается даже после отказа/пропущенной отметки. |
| П16,К39,К73 | Объяснения происхождения/покрытия, общий недельный обзор и статические FAQ. Просмотры не вызывают ИИ. |
| П18 | Две недели одной программы и повторение дней обеих недель→предложение, не автоприменение. Explicit confirm28д, варианты0/100/150ккал; недельный бюджет сохраняется, меняются углеводы, белок/жир прежние. Новая цель/система/день занятий/истёкшие права/отдых→base. Старые значения цели не воскрешают разрешение. |
| П20 | Списки покупок исключены пользователем. |
| П22 | Состав по явно подтверждённой этикетке: code/form/amount/unit, CAS. Сравнивать логические продукты в complex+individual; native split — один продукт. Не складывать разные формы/единицы и не менять дозу/план. Неизвестный состав не доказывает отсутствие пересечений. |
| П36 | Отдых выключает подсказки/обычные напоминания, сохраняет данные и настройки. Оплаченный срок продолжается. Возвращение не догоняет пропущенные напоминания; stale pending меню не объединяется с новым. |
| К9 | Сравнение нагрузки выше/ниже обычной по сопоставимым записям; без ккал на повторение и автоматического изменения цели. |
| К21 | Добровольная привязка конкретного слота Supply к новой Food записи≤90мин перед временем. При отсутствующей/поздней/устаревшей записи остаётся native время. Единый reminder_fires ledger на original slot исключает meal+scheduled дубль. UTC timestamp и entity id, без позиций еды. |
| К25 | При плохом check-in показывать подтверждённые изменения веса/цели/пищевых записей/тренировок/приёмов, только разрешённые свежие источники. Это совпадение во времени, не причина симптомов. |
| К28 | Own timezone preview с часами/местными слотами, отдельный confirm и CAS. История дат не переписывается; Gym historical UTC projection использует owner transition ledger. Food notification TZ не меняет существующую московскую границу журнала. |
| К32,К36,К51 | Opt-in Food review после нового веса Supply; смена Food цели→добровольный переход к собственному плану Gym/Supply. Нет жиросжигателей/автодоз/автозамены силовых. Чтение не completion; поздняя сохранённая цель/занятие/приём — проверяемое действие. |
| К53 | «Связь продуктов» в native меню и общем WebApp: источники, подсказки, уведомления, отдых, частота, питание, weekly/rhythm, trainer permissions. Quiet: одна новая подсказка за7д во всех продуктах, уже прочитанная может оставаться. |
| К71 | Own AI cache12 на человека,512 суммарно,≤128KiB/result, TTL Food6ч/Gym/Supply1ч. Exact input/model/prompt/context+permission revision. Guards/access/quota до cache; hit не списывает повторный Food запрос/Gym quota. Повтор голоса может требовать транскрипции. Purge own cache; не кэшировать некорректный результат, не продлевать TTL на hit. |
| К76 | Исключение только для aggregate nutrition: accepted fresh native trainer role/link, оплаченные Gym coach+Gym/Food pupil, отдельные согласия КБЖУ/top5,7/30/90д. Без отдельных позиций/нутриентов/Supply/анализов/передачи в тренерский ИИ. Deactivate/reaccept не воскрешает согласие; новые данные/permission проверяются при каждом запросе. Native Gym меню подопечного. |
| К79 | После подтверждённой общей покупки один необязательный следующий шаг «Настроить связь продуктов». Pending/cancel не создаёт предложение. RUB website ведёт в исходный бот через pgsettings; Stars использует existing single menu, финансовые сообщения не удаляются. |

## Канонические helpers

`shared/product_links.py`→progressor_links.py; `ai_cache.py`→progressor_ai_cache.py; `day_goal.py`→progressor_day_goal.py; `food_sources.py`→progressor_food_sources.py; `timezone_history.py`→progressor_timezone_history.py. Также прежние bridge/client/bot/ai_guard/native_subscription, Gym workout_metrics и Food nutrition_plan. Копии синхронизируются `deploy/sync_bot_helpers.py`, затем тестируются/публикуются main владельцев. Общий JS navigation/connections обслуживает только Core. Update-core не обновляет копии ботов.

Новые таблицы additive. Чистка собственных Food/Supply данных удаляет их новые cache/intents/confirmed composition. Настройки общей связи принадлежат Core; чистка одного продукта не удаляет чужую историю/оплату. Core tombstones/consent/source freshness исключают недоступные данные из новых ответов.

## Расчёты и доставка

0 AI для view/sync/порций/overview/weekly/rhythm/энергии/atlas/overlap/FAQ. MET остаётся приблизительной моделью при известных duration/weight/возрасте, не «точными калориями по мышце». Незаписанная еда не означает нулевой рацион. Отсутствие рыбы/насыщенных жиров не доказывает клинический дефицит.

Weekly default — продуктовый выбор, не доказанный лучший час для всех. [Apple weekly summary](https://support.apple.com/en-asia/guide/watch/apd29b30023c/watchos), [fresh-start research](https://pubsonline.informs.org/doi/10.1287/mnsc.2014.1901). [NIH B12](https://ods.od.nih.gov/factsheets/VitaminB12-HealthProfessional/), [NIH Omega3](https://ods.od.nih.gov/factsheets/Omega3FattyAcids-HealthProfessional/), [NIH weight-loss supplements](https://ods.od.nih.gov/factsheets/WeightLoss-HealthProfessional/).

Food sender единственный, opt-in/тихие часы/≤1 в местные сутки; weekly≤1/6.5д. Reserve before send, uncertainty не retry. Native current menu renderers и busy flows сохраняются. Supply meal anchors используют прежний sender; отдельного consumer нет. Rest lifecycle rows фильтруются до LIMIT, не блокируют других пользователей.

## Проверка и выпуск

Временные SQLite и dummy providers; функциональные owner/history/CAS/consent/cache/coach/reminder tests. Браузер — реальные локальные HTML/API с synthetic Telegram SDK; не физический Telegram/iPhone и не настоящий платёж. Результаты, archive SHA и выпуск — в docs/WORK_LOG.md и местном ACTION_JOURNAL.md.

Перед switch: свежая bounded consistent backup4DB/code/privateconfig/gate + off-server SHA/restore. Main4 normal FF без reset Food live trackedDB. Existing update-all, isolated runtime/pip checks, drain paid workers, rollback code/runtime preserves subsequent writes and protocol2. Затем9units,4DB integrity,3private exportsfresh,gate2,oneconsumer и postbackup. Не начинать новый финальный финансовый период в тестах.
