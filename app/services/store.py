from __future__ import annotations

import base64
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _parse_repo_light(value: str) -> tuple[str, str]:
    value = (value or '').strip().replace('.git', '')
    if 'github.com/' in value:
        value = value.split('github.com/', 1)[1]
    value = value.strip('/')
    parts = [x for x in value.split('/') if x]
    if len(parts) < 2:
        raise ValueError('صيغة المستودع غير صحيحة. استخدم owner/repo أو رابط GitHub.')
    return parts[0], parts[1]


class Store:
    """SQLite persistence layer with isolated repo sessions per Telegram user."""

    def __init__(self) -> None:
        self.settings = get_settings()
        Path(self.settings.database_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.settings.database_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()
        self.fernet = self._build_fernet()

    def _init(self) -> None:
        self.conn.execute(
            '''CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                github_token_enc TEXT,
                repo TEXT,
                branch TEXT,
                installation_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )'''
        )
        self.conn.execute(
            '''CREATE TABLE IF NOT EXISTS repo_sessions (
                telegram_id INTEGER PRIMARY KEY,
                github_token_id TEXT,
                repo_url TEXT,
                owner TEXT,
                repo TEXT,
                branch TEXT,
                connected_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT DEFAULT CURRENT_TIMESTAMP
            )'''
        )
        self.conn.execute(
            '''CREATE TABLE IF NOT EXISTS repo_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                repo_url TEXT NOT NULL,
                owner TEXT NOT NULL,
                repo TEXT NOT NULL,
                branch TEXT,
                connected_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(telegram_id, owner, repo)
            )'''
        )
        self.conn.commit()

    def _build_fernet(self) -> Fernet:
        key = self.settings.encryption_key.strip()
        key_path = Path(self.settings.database_path).with_suffix('.key')
        if not key:
            if key_path.exists():
                key = key_path.read_text().strip()
            else:
                key = Fernet.generate_key().decode()
                key_path.write_text(key)
        try:
            return Fernet(key.encode())
        except Exception:
            digest = hashlib.sha256(key.encode('utf-8')).digest()
            return Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str | None) -> str:
        if not value:
            return ''
        try:
            return self.fernet.decrypt(value.encode()).decode()
        except InvalidToken:
            return ''

    def upsert_user(self, telegram_id: int, **fields: Any) -> None:
        self.conn.execute('INSERT OR IGNORE INTO users(telegram_id) VALUES(?)', (telegram_id,))
        allowed = {'github_token_enc', 'repo', 'branch', 'installation_id'}
        for key, value in fields.items():
            if key not in allowed:
                continue
            self.conn.execute(
                f'UPDATE users SET {key}=?, updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?',
                (value, telegram_id),
            )
        self.conn.commit()

    def set_token(self, telegram_id: int, token: str) -> None:
        self.upsert_user(telegram_id, github_token_enc=self.encrypt(token))
        if self.get_session(telegram_id):
            self.conn.execute(
                'UPDATE repo_sessions SET github_token_id=?, last_used_at=CURRENT_TIMESTAMP WHERE telegram_id=?',
                (f'user:{telegram_id}', telegram_id),
            )
            self.conn.commit()

    def clear_token(self, telegram_id: int) -> None:
        self.upsert_user(telegram_id, github_token_enc=None)
        if self.get_session(telegram_id):
            self.conn.execute(
                'UPDATE repo_sessions SET github_token_id=?, last_used_at=CURRENT_TIMESTAMP WHERE telegram_id=?',
                ('env:GITHUB_TOKEN', telegram_id),
            )
            self.conn.commit()

    def set_repo(self, telegram_id: int, repo: str, branch: str | None = None) -> None:
        owner, repo_name = _parse_repo_light(repo)
        normalized = f'https://github.com/{owner}/{repo_name}'
        user = self.get_user(telegram_id)
        current_branch = branch or user.get('branch') or self.settings.github_default_branch
        token_id = f'user:{telegram_id}' if user.get('github_token') else 'env:GITHUB_TOKEN'
        self.upsert_user(telegram_id, repo=normalized, branch=current_branch)
        self.conn.execute(
            '''INSERT INTO repo_sessions(telegram_id, github_token_id, repo_url, owner, repo, branch)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   github_token_id=excluded.github_token_id,
                   repo_url=excluded.repo_url,
                   owner=excluded.owner,
                   repo=excluded.repo,
                   branch=excluded.branch,
                   last_used_at=CURRENT_TIMESTAMP''',
            (telegram_id, token_id, normalized, owner, repo_name, current_branch),
        )
        self.conn.execute(
            '''INSERT INTO repo_history(telegram_id, repo_url, owner, repo, branch)
               VALUES(?,?,?,?,?)
               ON CONFLICT(telegram_id, owner, repo) DO UPDATE SET
                   repo_url=excluded.repo_url,
                   branch=excluded.branch,
                   last_used_at=CURRENT_TIMESTAMP''',
            (telegram_id, normalized, owner, repo_name, current_branch),
        )
        self.conn.commit()

    def set_branch(self, telegram_id: int, branch: str) -> None:
        self.upsert_user(telegram_id, branch=branch)
        self.conn.execute(
            'UPDATE repo_sessions SET branch=?, last_used_at=CURRENT_TIMESTAMP WHERE telegram_id=?',
            (branch, telegram_id),
        )
        self.conn.execute(
            'UPDATE repo_history SET branch=?, last_used_at=CURRENT_TIMESTAMP WHERE telegram_id=? AND repo_url=(SELECT repo_url FROM repo_sessions WHERE telegram_id=?)',
            (branch, telegram_id, telegram_id),
        )
        self.conn.commit()

    def get_session(self, telegram_id: int) -> dict[str, Any]:
        row = self.conn.execute('SELECT * FROM repo_sessions WHERE telegram_id=?', (telegram_id,)).fetchone()
        return dict(row) if row else {}

    def get_user(self, telegram_id: int) -> dict[str, Any]:
        self.conn.execute('INSERT OR IGNORE INTO users(telegram_id) VALUES(?)', (telegram_id,))
        self.conn.commit()
        row = self.conn.execute('SELECT * FROM users WHERE telegram_id=?', (telegram_id,)).fetchone()
        data = dict(row) if row else {}
        data['github_token'] = self.decrypt(data.get('github_token_enc')) if data else ''
        session = self.get_session(telegram_id)
        if session:
            data['repo'] = session.get('repo_url') or data.get('repo')
            data['branch'] = session.get('branch') or data.get('branch')
            data['session'] = session
        else:
            data['session'] = {}
        return data

    def disconnect_repo(self, telegram_id: int) -> None:
        self.conn.execute('DELETE FROM repo_sessions WHERE telegram_id=?', (telegram_id,))
        self.upsert_user(telegram_id, repo=None)
        self.conn.commit()

    def disconnect_all(self, telegram_id: int, clear_token: bool = False) -> None:
        self.conn.execute('DELETE FROM repo_sessions WHERE telegram_id=?', (telegram_id,))
        self.conn.execute('DELETE FROM repo_history WHERE telegram_id=?', (telegram_id,))
        fields: dict[str, Any] = {'repo': None, 'branch': self.settings.github_default_branch}
        if clear_token:
            fields['github_token_enc'] = None
        self.upsert_user(telegram_id, **fields)
        self.conn.commit()

    def list_repo_history(self, telegram_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            'SELECT * FROM repo_history WHERE telegram_id=? ORDER BY last_used_at DESC LIMIT 30',
            (telegram_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def connections_status(self, telegram_id: int) -> dict[str, Any]:
        user = self.get_user(telegram_id)
        session = self.get_session(telegram_id)
        history = self.list_repo_history(telegram_id)
        return {
            'telegram_id': telegram_id,
            'has_user_token': bool(user.get('github_token')),
            'has_env_token': bool(self.settings.github_token),
            'active_session': session,
            'known_repositories_count': len(history),
            'known_repositories': history,
        }
