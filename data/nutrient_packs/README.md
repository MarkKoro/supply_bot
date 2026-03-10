# Nutrient Data Packs

Папка `data/nutrient_packs/` хранит пакеты правил нутриентов (JSON), из которых строится runtime registry нутриентного помощника.

Текущий runtime corpus покрывает **34** нутриента.

## Полный микронутриентный корпус (текущий scope)

### Витамины
- vitamin_a
- vitamin_d
- vitamin_e
- vitamin_k
- vitamin_c
- vitamin_b1
- vitamin_b2
- vitamin_b3
- vitamin_b5
- vitamin_b6
- vitamin_b7
- vitamin_b9 / folate
- vitamin_b12

### Витаминоподобные essential
- choline

### Макроминералы / электролиты
- calcium
- magnesium
- phosphorus
- potassium
- sodium
- chloride

### Микроэлементы
- iron
- zinc
- copper
- selenium
- iodine
- manganese
- chromium
- molybdenum
- fluoride

### Extended metadata-only
- boron
- silicon
- vanadium
- nickel

## Формат pack-файла

```json
{
  "pack_id": "core_v1",
  "pack_title_ru": "Базовый deterministic v1",
  "pack_version": "1.0.0",
  "pack_category": "core_v1",
  "rules_version": "nutrient_rules_v2",
  "source_label": "internal_curated",
  "curation_status": "curated",
  "generated_at_utc": "2026-01-01T00:00:00+00:00",
  "notes_ru": "...",
  "allow_override": false,
  "items": [ { "nutrient_code": "vitamin_d", "...": "rule profile" } ]
}
```

## Обязательные поля pack
- `pack_id`
- `pack_title_ru`
- `pack_version`
- `pack_category`
- `rules_version`
- `items`

## Readiness уровни
- `deterministic_v1` — полноценная персональная оценка.
- `limited_interpretation` — ограниченная персональная оценка.
- `metadata_only` — справочный режим.
- `planned` — зарезервировано для будущего расширения.

## Быстрый pipeline
1. Validate:
   - `python seed_import.py --nutrient-packs-mode validate-only`
2. Coverage/QA report:
   - `python seed_import.py --nutrient-packs-mode report-only`
3. Runtime check (быстрая проверка количества и кодов):
   - `python - <<'PY'
from nutrient_registry import get_all_nutrient_rules
rules = get_all_nutrient_rules()
print(len(rules), sorted(rules.keys()))
PY`
4. Sync runtime + DB:
   - `python seed_import.py --nutrient-packs-mode sync-runtime-and-db`

## Как добавить новый micronutrient
1. Добавьте item в релевантный pack.
2. Укажите `readiness_level`, `engine/planner/explanation_mode`, analytes/units, source metadata.
3. Прогоните validate/report.
4. Выполните sync в БД.

## Что не входит в текущий scope
- amino acids
- fatty acids
- botanical extracts
- probiotics
- adaptogens
