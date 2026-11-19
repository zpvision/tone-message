"""Local CLI only; the database never stores bearer secrets."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import uuid


class KeyStore:
    def __init__(self, directory):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / 'api.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS api_keys(id TEXT PRIMARY KEY,name TEXT NOT NULL,created TEXT NOT NULL,enabled INTEGER NOT NULL,digest TEXT NOT NULL,revoked TEXT)')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, name):
        if not name.strip() or len(name) > 80:
            raise ValueError('Name must contain 1–80 characters')
        key_id = uuid.uuid4().hex[:16]
        secret = f'fkai_{key_id}_{secrets.token_urlsafe(48)}'
        created = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute('INSERT INTO api_keys(id,name,created,enabled,digest) VALUES(?,?,?,1,?)',
                       (key_id,name,created,hashlib.sha256(secret.encode()).hexdigest()))
        return {'id':key_id,'name':name,'created':created,'api_key':secret}

    def authenticate(self, authorization):
        value = authorization or ''
        scheme, _, secret = value.partition(' ')
        parts = secret.split('_',2)
        key_id = parts[1] if len(parts)==3 and parts[0]=='fkai' else ''
        with self.connect() as db:
            row = db.execute('SELECT id,enabled,digest FROM api_keys WHERE id=?',(key_id,)).fetchone()
        expected = row['digest'] if row else '0'*64
        matches = hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(),expected)
        return row['id'] if scheme.lower()=='bearer' and matches and row and row['enabled'] else None

    def list(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,name,created,enabled,revoked FROM api_keys ORDER BY created')]

    def revoke(self, key_id):
        with self.connect() as db:
            return db.execute('UPDATE api_keys SET enabled=0,revoked=? WHERE id=?',
                              (datetime.now(timezone.utc).isoformat(),key_id)).rowcount

    def enable(self, key_id):
        with self.connect() as db:
            return db.execute('UPDATE api_keys SET enabled=1,revoked=NULL WHERE id=?',(key_id,)).rowcount


if __name__=='__main__':
    from app.config import Settings
    parser=argparse.ArgumentParser(description='Local FinKoper API key administration; create prints the secret ONCE')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('create').add_argument('--name',required=True)
    sub.add_parser('list')
    sub.add_parser('revoke').add_argument('id')
    sub.add_parser('enable').add_argument('id')
    args=parser.parse_args()
    keys=KeyStore(Settings().api_data_dir)
    if args.command=='create':
        result=keys.create(args.name)
    elif args.command=='list':
        result=keys.list()
    elif args.command=='revoke':
        result={'revoked':bool(keys.revoke(args.id))}
    else:
        result={'enabled':bool(keys.enable(args.id))}
    print(json.dumps(result,ensure_ascii=False))
