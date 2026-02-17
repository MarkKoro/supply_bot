import hmac
import hashlib
from urllib.parse import parse_qsl

def verify_init_data(init_data: str, bot_token: str) -> tuple[bool, dict]:
    """
    Проверяет подпись initData (Telegram WebApp).
    Возвращает (ok, parsed_dict).
    """
    if not init_data or not bot_token:
        return False, {}

    data = dict(parse_qsl(init_data, keep_blank_values=True))
    their_hash = data.pop("hash", "")
    if not their_hash:
        return False, data

    # data-check-string: ключи по алфавиту, join "\n" как "k=v"
    pairs = [f"{k}={data[k]}" for k in sorted(data.keys())]
    data_check_string = "\n".join(pairs).encode("utf-8")

    # secret_key = HMAC_SHA256("WebAppData", bot_token)
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    our_hash = hmac.new(secret_key, data_check_string, hashlib.sha256).hexdigest()

    return hmac.compare_digest(our_hash, their_hash), data
