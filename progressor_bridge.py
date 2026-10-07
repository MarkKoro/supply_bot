"""Progressor assertion verifier v1. No Core network calls in domain transactions.

This versioned file is copied verbatim into every domain repository. See AGENTS.md.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from contextvars import ContextVar
from pathlib import Path

_claims = ContextVar('progressor_claims', default=None)


def verify(token, audience, key, now=None):
    if not isinstance(token, str) or len(token) > 4096 or len(key) < 32:
        raise ValueError('invalid_assertion')
    version, body, mac = token.split('.')
    expected = hmac.new(key.encode(), (version + '.' + body).encode(), hashlib.sha256).hexdigest()
    if version != 'v1' or not hmac.compare_digest(expected, mac):
        raise ValueError('invalid_assertion')
    claims = json.loads(base64.b64decode(body + '=' * (-len(body) % 4), altchars=b'-_', validate=True))
    now = int(time.time()) if now is None else now
    if claims.get('iss') != 'progressor-core' or claims.get('typ') != 'progressor.assertion.v1' or claims.get('aud') != audience:
        raise ValueError('invalid_audience')
    if any(type(claims.get(k)) is not int for k in ('iat', 'exp', 'auth_date')):
        raise ValueError('invalid_time')
    if not claims['iat'] - 30 <= now < claims['exp'] or not 0 < claims['exp'] - claims['iat'] <= 900 or claims['exp'] > claims['auth_date'] + 86400:
        raise ValueError('expired_assertion')
    user = claims.get('user')
    if not isinstance(user, dict) or type(user.get('id')) is not int or not 0 < user['id'] < 2**63 or claims.get('sub') != str(user['id']):
        raise ValueError('invalid_subject')
    return claims


def current_claims():
    return _claims.get()


def install_auth_bridge(app, audience):
    from starlette.responses import JSONResponse
    enabled = os.getenv('PROGRESSOR_AUTH_ENABLED', '0').lower() in ('1', 'true', 'yes')
    key = ''
    if enabled:
        try:
            key = Path(os.environ['PROGRESSOR_SIGNING_KEY_FILE']).read_text().strip()
            if len(key) < 32: raise ValueError('invalid_key')
        except (OSError,KeyError,ValueError):
            enabled = False
            logging.getLogger('progressor_bridge').error(json.dumps({'event':'bridge_disabled','reason':'invalid_signing_key','audience':audience}))

    @app.middleware('http')
    async def bridge(request, call_next):
        token = request.headers.get('X-Progressor-Auth')
        identity = None
        if token and enabled:
            try:
                identity = verify(token, audience, key)
            except Exception:
                return JSONResponse({'detail': 'Invalid or expired Progressor session'}, status_code=401)
        marker = _claims.set(identity)
        try:
            return await call_next(request)
        finally:
            _claims.reset(marker)
