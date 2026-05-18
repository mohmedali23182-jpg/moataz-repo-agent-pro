from __future__ import annotations

import base64
import hashlib
import json
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
    """SQLite persistence layer.

    Stores user GitHub tokens, isolated repo sessions, platform connector tokens,
    AI provider tokens and lightweight audit events. Tokens are encrypted locally.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        Path(self.settings.database_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.settings.database_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()
        self.fernet = self._build_fernet()

    def _init(self) -> None:
        # Migrate early ultra builds that used provider/account_label for connector_tokens.
        existing = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='connector_tokens'").fetchone()
        if existing:
            cols = [row[1] for row in self.conn.execute('PRAGMA table_info(connector_tokens)').fetchall()]
            if 'provider' in cols and 'platform' not in cols:
                self.conn.execute('ALTER TABLE connector_tokens RENAME TO connector_tokens_old')
                self.conn.commit()
        self.conn.execute('''CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            github_token_enc TEXT,
            repo TEXT,
            branch TEXT,
            installation_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS repo_sessions (
            telegram_id INTEGER PRIMARY KEY,
            github_token_id TEXT,
            repo_url TEXT,
            owner TEXT,
            repo TEXT,
            branch TEXT,
            connected_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_used_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS repo_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            repo_url TEXT NOT NULL,
            owner TEXT NOT NULL,
            repo TEXT NOT NULL,
            branch TEXT,
            connected_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_used_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(telegram_id, owner, repo)
        )''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS connector_tokens (
            telegram_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            token_enc TEXT NOT NULL,
            meta_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(telegram_id, platform)
        )''')
        old_connector = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='connector_tokens_old'").fetchone()
        if old_connector:
            try:
                rows = self.conn.execute('SELECT telegram_id, provider, token_enc, account_label, created_at, updated_at FROM connector_tokens_old').fetchall()
                for row in rows:
                    self.conn.execute(
                        'INSERT OR IGNORE INTO connector_tokens(telegram_id, platform, token_enc, meta_json, created_at, updated_at) VALUES(?,?,?,?,?,?)',
                        (row['telegram_id'], row['provider'], row['token_enc'], '{}', row['created_at'], row['updated_at']),
                    )
                self.conn.execute('DROP TABLE connector_tokens_old')
                self.conn.commit()
            except Exception:
                pass
        self.conn.execute('''CREATE TABLE IF NOT EXISTS ai_tokens (
            telegram_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            token_enc TEXT NOT NULL,
            base_url TEXT DEFAULT '',
            model TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(telegram_id, provider)
        )''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            action TEXT NOT NULL,
            target TEXT,
            details TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
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

    def audit(self, telegram_id: int | None, action: str, target: str = '', details: str = '') -> None:
        self.conn.execute('INSERT INTO audit_log(telegram_id, action, target, details) VALUES(?,?,?,?)', (telegram_id, action, target, details[:2000]))
        self.conn.commit()

    def upsert_user(self, telegram_id: int, **fields: Any) -> None:
        self.conn.execute('INSERT OR IGNORE INTO users(telegram_id) VALUES(?)', (telegram_id,))
        allowed = {'github_token_enc', 'repo', 'branch', 'installation_id'}
        for key, value in fields.items():
            if key not in allowed:
                continue
            self.conn.execute(f'UPDATE users SET {key}=?, updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?', (value, telegram_id))
        self.conn.commit()

    def set_token(self, telegram_id: int, token: str) -> None:
        self.upsert_user(telegram_id, github_token_enc=self.encrypt(token))
        if self.get_session(telegram_id):
            self.conn.execute('UPDATE repo_sessions SET github_token_id=?, last_used_at=CURRENT_TIMESTAMP WHERE telegram_id=?', (f'user:{telegram_id}', telegram_id))
            self.conn.commit()
        self.audit(telegram_id, 'github_token_set', 'github', '{}')

    def clear_token(self, telegram_id: int) -> None:
        self.upsert_user(telegram_id, github_token_enc=None)
        if self.get_session(telegram_id):
            self.conn.execute('UPDATE repo_sessions SET github_token_id=?, last_used_at=CURRENT_TIMESTAMP WHERE telegram_id=?', ('env:GITHUB_TOKEN', telegram_id))
            self.conn.commit()
        self.audit(telegram_id, 'github_token_cleared', 'github', '{}')

    def set_repo(self, telegram_id: int, repo: str, branch: str | None = None) -> None:
        owner, repo_name = _parse_repo_light(repo)
        normalized = f'https://github.com/{owner}/{repo_name}'
        user = self.get_user(telegram_id)
        current_branch = branch or user.get('branch') or self.settings.github_default_branch
        token_id = f'user:{telegram_id}' if user.get('github_token') else 'env:GITHUB_TOKEN'
        self.upsert_user(telegram_id, repo=normalized, branch=current_branch)
        self.conn.execute('''INSERT INTO repo_sessions(telegram_id, github_token_id, repo_url, owner, repo, branch)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
              github_token_id=excluded.github_token_id, repo_url=excluded.repo_url, owner=excluded.owner,
              repo=excluded.repo, branch=excluded.branch, last_used_at=CURRENT_TIMESTAMP''',
            (telegram_id, token_id, normalized, owner, repo_name, current_branch))
        self.conn.execute('''INSERT INTO repo_history(telegram_id, repo_url, owner, repo, branch)
            VALUES(?,?,?,?,?)
            ON CONFLICT(telegram_id, owner, repo) DO UPDATE SET
              repo_url=excluded.repo_url, branch=excluded.branch, last_used_at=CURRENT_TIMESTAMP''',
            (telegram_id, normalized, owner, repo_name, current_branch))
        self.conn.commit()
        self.audit(telegram_id, 'repo_switch', normalized, '{}')

    def set_branch(self, telegram_id: int, branch: str) -> None:
        self.upsert_user(telegram_id, branch=branch)
        self.conn.execute('UPDATE repo_sessions SET branch=?, last_used_at=CURRENT_TIMESTAMP WHERE telegram_id=?', (branch, telegram_id))
        self.conn.execute('UPDATE repo_history SET branch=?, last_used_at=CURRENT_TIMESTAMP WHERE telegram_id=? AND repo_url=(SELECT repo_url FROM repo_sessions WHERE telegram_id=?)', (branch, telegram_id, telegram_id))
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
        self.audit(telegram_id, 'repo_disconnect', 'current_repo', '{}')

    def disconnect_all(self, telegram_id: int, clear_token: bool = False) -> None:
        self.conn.execute('DELETE FROM repo_sessions WHERE telegram_id=?', (telegram_id,))
        self.conn.execute('DELETE FROM repo_history WHERE telegram_id=?', (telegram_id,))
        fields: dict[str, Any] = {'repo': None, 'branch': self.settings.github_default_branch}
        if clear_token:
            fields['github_token_enc'] = None
        self.upsert_user(telegram_id, **fields)
        self.conn.commit()
        self.audit(telegram_id, 'disconnect_all', 'github', json.dumps({'clear_token': clear_token}))

    def list_repo_history(self, telegram_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute('SELECT * FROM repo_history WHERE telegram_id=? ORDER BY last_used_at DESC LIMIT 30', (telegram_id,)).fetchall()
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
            'platform_connectors': self.list_connectors(telegram_id),
            'ai_providers': self.list_ai_providers(telegram_id),
        }

    def set_connector_token(self, telegram_id: int, platform: str, token: str, meta: dict[str, Any] | None = None) -> None:
        platform = platform.lower().strip()
        self.conn.execute('''INSERT INTO connector_tokens(telegram_id, platform, token_enc, meta_json)
            VALUES(?,?,?,?)
            ON CONFLICT(telegram_id, platform) DO UPDATE SET
              token_enc=excluded.token_enc, meta_json=excluded.meta_json, updated_at=CURRENT_TIMESTAMP''',
            (telegram_id, platform, self.encrypt(token), json.dumps(meta or {}, ensure_ascii=False)))
        self.conn.commit()
        self.audit(telegram_id, 'connector_token_set', platform, '{}')

    def get_connector_token(self, telegram_id: int, platform: str) -> tuple[str, dict[str, Any]]:
        platform = platform.lower().strip()
        row = self.conn.execute('SELECT * FROM connector_tokens WHERE telegram_id=? AND platform=?', (telegram_id, platform)).fetchone()
        if row:
            return self.decrypt(row['token_enc']), json.loads(row['meta_json'] or '{}')
        env_map = {
            'railway': self.settings.railway_api_token or self.settings.railway_token,
            'vercel': self.settings.vercel_token,
            'render': self.settings.render_api_key,
            'netlify': self.settings.netlify_auth_token,
            'fly': self.settings.fly_api_token,
            'replit': self.settings.replit_token,
        }
        return env_map.get(platform, ''), {}

    def list_connectors(self, telegram_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute('SELECT platform, meta_json, created_at, updated_at FROM connector_tokens WHERE telegram_id=? ORDER BY platform', (telegram_id,)).fetchall()
        saved = [dict(r) | {'source': 'user'} for r in rows]
        for p, token in {
            'railway': self.settings.railway_api_token or self.settings.railway_token,
            'vercel': self.settings.vercel_token,
            'render': self.settings.render_api_key,
            'netlify': self.settings.netlify_auth_token,
            'fly': self.settings.fly_api_token,
            'replit': self.settings.replit_token,
        }.items():
            if token and not any(x['platform'] == p for x in saved):
                saved.append({'platform': p, 'source': 'env', 'created_at': '', 'updated_at': ''})
        return saved

    def delete_connector_token(self, telegram_id: int, platform: str) -> None:
        self.conn.execute('DELETE FROM connector_tokens WHERE telegram_id=? AND platform=?', (telegram_id, platform.lower().strip()))
        self.conn.commit()
        self.audit(telegram_id, 'connector_token_deleted', platform, '{}')

    def set_ai_token(self, telegram_id: int, provider: str, token: str, base_url: str = '', model: str = '') -> None:
        provider = provider.lower().strip()
        self.conn.execute('''INSERT INTO ai_tokens(telegram_id, provider, token_enc, base_url, model)
            VALUES(?,?,?,?,?)
            ON CONFLICT(telegram_id, provider) DO UPDATE SET
              token_enc=excluded.token_enc, base_url=excluded.base_url, model=excluded.model, updated_at=CURRENT_TIMESTAMP''',
            (telegram_id, provider, self.encrypt(token), base_url, model))
        self.conn.commit()
        self.audit(telegram_id, 'ai_token_set', provider, '{}')

    def get_ai_token(self, telegram_id: int, provider: str | None = None) -> tuple[str, str, str, str]:
        provider = (provider or self.settings.ai_default_provider).lower().strip()
        row = self.conn.execute('SELECT * FROM ai_tokens WHERE telegram_id=? AND provider=?', (telegram_id, provider)).fetchone()
        if row:
            return provider, self.decrypt(row['token_enc']), row['base_url'] or '', row['model'] or ''
        env_token = {
            'openai': self.settings.openai_api_key or self.settings.ai_api_key,
            'openrouter': self.settings.openrouter_api_key or self.settings.ai_api_key,
            'gemini': self.settings.gemini_api_key or self.settings.ai_api_key,
            'anthropic': self.settings.anthropic_api_key or self.settings.ai_api_key,
            'groq': self.settings.groq_api_key or self.settings.ai_api_key,
            'mistral': self.settings.mistral_api_key or self.settings.ai_api_key,
            'together': self.settings.together_api_key or self.settings.ai_api_key,
            'perplexity': self.settings.perplexity_api_key or self.settings.ai_api_key,
            'deepseek': self.settings.deepseek_api_key or self.settings.ai_api_key,
            'xai': self.settings.xai_api_key or self.settings.ai_api_key,
            'cohere': self.settings.cohere_api_key or self.settings.ai_api_key,
            'huggingface': self.settings.huggingface_api_key or self.settings.ai_api_key,
            'fireworks': self.settings.fireworks_api_key or self.settings.ai_api_key,
            'custom': self.settings.ai_api_key,
        }.get(provider, self.settings.ai_api_key)
        return provider, env_token, self.settings.ai_base_url, self.settings.ai_default_model

    def list_ai_providers(self, telegram_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute('SELECT provider, base_url, model, created_at, updated_at FROM ai_tokens WHERE telegram_id=? ORDER BY provider', (telegram_id,)).fetchall()
        items = [dict(r) | {'source': 'user'} for r in rows]
        for p, token in {
            'openai': self.settings.openai_api_key,
            'openrouter': self.settings.openrouter_api_key,
            'gemini': self.settings.gemini_api_key,
            'anthropic': self.settings.anthropic_api_key,
            'groq': self.settings.groq_api_key,
            'mistral': self.settings.mistral_api_key,
            'together': self.settings.together_api_key,
            'perplexity': self.settings.perplexity_api_key,
            'deepseek': self.settings.deepseek_api_key,
            'xai': self.settings.xai_api_key,
            'cohere': self.settings.cohere_api_key,
            'huggingface': self.settings.huggingface_api_key,
            'fireworks': self.settings.fireworks_api_key,
            self.settings.ai_default_provider: self.settings.ai_api_key,
        }.items():
            if p and token and not any(x['provider'] == p for x in items):
                items.append({'provider': p, 'base_url': self.settings.ai_base_url, 'model': self.settings.ai_default_model, 'source': 'env'})
        return items
