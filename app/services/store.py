from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class Store:
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
        self.conn.commit()

    def _build_fernet(self) -> Fernet:
        key = self.settings.encryption_key.strip()
        if not key:
            key_path = Path(self.settings.database_path).with_suffix('.key')
            if key_path.exists():
                key = key_path.read_text().strip()
            else:
                key = Fernet.generate_key().decode()
                key_path.write_text(key)
        return Fernet(key.encode() if isinstance(key, str) else key)

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
        for key, value in fields.items():
            self.conn.execute(f'UPDATE users SET {key}=?, updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?', (value, telegram_id))
        self.conn.commit()

    def set_token(self, telegram_id: int, token: str) -> None:
        self.upsert_user(telegram_id, github_token_enc=self.encrypt(token))

    def set_repo(self, telegram_id: int, repo: str) -> None:
        self.upsert_user(telegram_id, repo=repo)

    def set_branch(self, telegram_id: int, branch: str) -> None:
        self.upsert_user(telegram_id, branch=branch)

    def get_user(self, telegram_id: int) -> dict[str, Any]:
        self.conn.execute('INSERT OR IGNORE INTO users(telegram_id) VALUES(?)', (telegram_id,))
        self.conn.commit()
        row = self.conn.execute('SELECT * FROM users WHERE telegram_id=?', (telegram_id,)).fetchone()
        data = dict(row) if row else {}
        data['github_token'] = self.decrypt(data.get('github_token_enc')) if data else ''
        return data

    def clear_token(self, telegram_id: int) -> None:
        self.upsert_user(telegram_id, github_token_enc=None)
