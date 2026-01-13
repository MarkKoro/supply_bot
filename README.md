# Supply Bot — skeleton v3 (no WebApp yet)

v3:
- Исправлены вызовы `execute_fetchone/execute_fetchall` (они есть не во всех версиях `aiosqlite`)
- Добавлен error handler, чтобы ошибки не “съедались”
- Чуть аккуратнее сообщения/логика

## Git Bash (Windows)
```bash
cd /d/supply_bot

python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt

cp .env.example .env
notepad.exe .env

python init_db.py
python seed_import.py --supplements data/seed/supplements.jsonl --interactions data/seed/interactions.jsonl
python bot.py
```
