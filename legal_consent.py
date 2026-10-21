"""Supply-owned current-document decision, reused by native reads and export."""
import hashlib
import json
import uuid
from datetime import datetime, timezone

PREDICATE_VERSION=1
REASONS=('current','not_accepted','offer_changed','privacy_changed','both_changed')


def policy(offer_version,privacy_version):
    values=(offer_version,privacy_version)
    if any(not isinstance(v,str) or not v or len(v)>128 for v in values):
        raise ValueError('invalid_owner_consent_policy')
    return {'predicate_version':PREDICATE_VERSION,'offer_version':offer_version,'privacy_version':privacy_version}


def decision(row,current_policy):
    """No I/O, defaults, Core imports or implicit sharing grants."""
    if row is None:return {'current':False,'reason':'not_accepted'}
    try:accepted=int(row['consent_given'] or 0)==1
    except (TypeError,ValueError):accepted=False
    if not accepted:return {'current':False,'reason':'not_accepted'}
    offer=str(row['consent_offer_version'] or '')==current_policy['offer_version']
    privacy=str(row['consent_privacy_version'] or '')==current_policy['privacy_version']
    reason='current' if offer and privacy else 'both_changed' if not offer and not privacy else 'offer_changed' if not offer else 'privacy_changed'
    return {'current':offer and privacy,'reason':reason}


def metadata_writer(offer_version,privacy_version):
    """Capture one owner policy; enrich only the already-backed-up private copy."""
    current_policy=policy(offer_version,privacy_version)
    fingerprint=hashlib.sha256(json.dumps(current_policy,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    def write(copy):
        copy.executescript('''DROP TABLE IF EXISTS progressor_owner_policy;
            DROP TABLE IF EXISTS progressor_owner_consent;
            CREATE TABLE progressor_owner_policy(schema_version INTEGER NOT NULL,source TEXT NOT NULL,
                generation TEXT NOT NULL,generated_at_utc TEXT NOT NULL,policy_fingerprint TEXT NOT NULL,
                required_offer_version TEXT NOT NULL,required_privacy_version TEXT NOT NULL);
            CREATE TABLE progressor_owner_consent(native_user_id INTEGER PRIMARY KEY,user_subject TEXT NOT NULL,
                legal_current INTEGER NOT NULL CHECK(legal_current IN (0,1)),reason TEXT NOT NULL,
                policy_fingerprint TEXT NOT NULL);''')
        copy.execute('INSERT INTO progressor_owner_policy VALUES(1,?,?,?,?,?,?)',('supply',uuid.uuid4().hex,
            datetime.now(timezone.utc).isoformat(),fingerprint,current_policy['offer_version'],current_policy['privacy_version']))
        copy.row_factory=__import__('sqlite3').Row
        for row in copy.execute('SELECT id,tg_user_id,consent_given,consent_offer_version,consent_privacy_version FROM users'):
            uid=str(row['tg_user_id'])
            if not uid.isascii() or not uid.isdecimal() or uid.startswith('0') or not 0<int(uid)<2**63:
                raise ValueError('invalid_owner_subject')
            value=decision(row,current_policy)
            copy.execute('INSERT INTO progressor_owner_consent VALUES(?,?,?,?,?)',
                (row['id'],uid,int(value['current']),value['reason'],fingerprint))
        copy.commit()
    return write
