import hmac
import hashlib
import re
import time
from urllib.parse import parse_qsl

def verify_init_data(init_data: str, bot_token: str, *, max_age_seconds: int = 86400) -> tuple[bool, dict]:
    """
    Проверяет подпись initData (Telegram WebApp).
    Возвращает (ok, parsed_dict).
    """
    if not init_data or not bot_token or len(init_data) > 16384:
        return False, {}

    try:
        fields = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True, max_num_fields=64)
    except ValueError:
        return False, {}
    data = dict(fields)
    if len(data) != len(fields):
        return False, {}
    their_hash = data.pop("hash", "")
    if not re.fullmatch(r"[0-9a-f]{64}", their_hash):
        return False, {}

    # data-check-string: ключи по алфавиту, join "\n" как "k=v"
    pairs = [f"{k}={data[k]}" for k in sorted(data.keys())]
    data_check_string = "\n".join(pairs).encode("utf-8")

    # secret_key = HMAC_SHA256("WebAppData", bot_token)
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    our_hash = hmac.new(secret_key, data_check_string, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(our_hash, their_hash):
        return False, {}
    auth_date = data.get("auth_date", "")
    if not re.fullmatch(r"[0-9]{1,12}", auth_date):
        return False, {}
    age = time.time() - int(auth_date)
    if max_age_seconds <= 0 or age < -60 or age > max_age_seconds:
        return False, {}
    return True, data
