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
        self.conn.execute('''CREATE TABLE IF NOT EXISTS agent_tasks (
            task_id TEXT PRIMARY KEY,
            telegram_id INTEGER NOT NULL,
            repo_full TEXT,
            branch TEXT,
            status TEXT NOT NULL,
            objective TEXT,
            plan_json TEXT DEFAULT '{}',
            result_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS agent_task_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            repo_full TEXT,
            step TEXT,
            message TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS repo_memory (
            telegram_id INTEGER NOT NULL,
            repo_full TEXT NOT NULL,
            branch TEXT NOT NULL,
            path TEXT NOT NULL,
            language TEXT DEFAULT '',
            summary TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(telegram_id, repo_full, branch, path)
        )''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS last_errors (
            telegram_id INTEGER NOT NULL,
            repo_full TEXT NOT NULL,
            branch TEXT,
            source TEXT,
            error TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(telegram_id, repo_full)
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
            'gdrive': self.settings.google_drive_token,
            'google_drive': self.settings.google_drive_token,
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
            'gdrive': self.settings.google_drive_token,
            'google_drive': self.settings.google_drive_token,
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
        env_token_map = {
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
            'lovable': self.settings.ai_api_key,
            'cursor': self.settings.ai_api_key,
            'spiko': self.settings.ai_api_key,
        }
        return provider, env_token_map.get(provider, self.settings.ai_api_key), self.settings.ai_base_url, self.settings.ai_default_model

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

    # -------------------------
    # Agentic task + memory helpers
    # -------------------------
    def create_task(self, task_id: str, telegram_id: int, repo_full: str, branch: str, status: str, objective: str, plan: dict[str, Any]) -> None:
        self.conn.execute('''INSERT OR REPLACE INTO agent_tasks(task_id, telegram_id, repo_full, branch, status, objective, plan_json, updated_at)
            VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)''', (task_id, telegram_id, repo_full, branch, status, objective, json.dumps(plan, ensure_ascii=False)))
        self.conn.commit()
        self.audit(telegram_id, 'task_create', task_id, json.dumps({'repo': repo_full, 'status': status}, ensure_ascii=False))

    def update_task(self, task_id: str, status: str | None = None, result: dict[str, Any] | None = None) -> None:
        if status is not None:
            self.conn.execute('UPDATE agent_tasks SET status=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?', (status, task_id))
        if result is not None:
            self.conn.execute('UPDATE agent_tasks SET result_json=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?', (json.dumps(result, ensure_ascii=False), task_id))
        self.conn.commit()

    def get_task(self, task_id: str) -> dict[str, Any]:
        row = self.conn.execute('SELECT * FROM agent_tasks WHERE task_id=?', (task_id,)).fetchone()
        if not row:
            return {}
        data = dict(row)
        for key in ('plan_json', 'result_json'):
            try:
                data[key] = json.loads(data.get(key) or '{}')
            except Exception:
                data[key] = {}
        return data

    def list_tasks(self, telegram_id: int, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute('SELECT * FROM agent_tasks WHERE telegram_id=? ORDER BY updated_at DESC LIMIT ?', (telegram_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def append_task_log(self, telegram_id: int, repo_full: str, step: str, message: str) -> None:
        self.conn.execute('INSERT INTO agent_task_logs(telegram_id, repo_full, step, message) VALUES(?,?,?,?)', (telegram_id, repo_full, step, message[:3500]))
        self.conn.commit()

    def task_logs(self, telegram_id: int, repo_full: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        if repo_full:
            rows = self.conn.execute('SELECT * FROM agent_task_logs WHERE telegram_id=? AND repo_full=? ORDER BY id DESC LIMIT ?', (telegram_id, repo_full, limit)).fetchall()
        else:
            rows = self.conn.execute('SELECT * FROM agent_task_logs WHERE telegram_id=? ORDER BY id DESC LIMIT ?', (telegram_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def upsert_memory(self, telegram_id: int, repo_full: str, branch: str, path: str, summary: str, language: str = '') -> None:
        self.conn.execute('''INSERT INTO repo_memory(telegram_id, repo_full, branch, path, language, summary)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(telegram_id, repo_full, branch, path) DO UPDATE SET
              language=excluded.language, summary=excluded.summary, updated_at=CURRENT_TIMESTAMP''',
            (telegram_id, repo_full, branch, path, language, summary))
        self.conn.commit()

    def memory_status(self, telegram_id: int, repo_full: str | None = None) -> dict[str, Any]:
        if repo_full:
            rows = self.conn.execute('SELECT language, COUNT(*) AS c FROM repo_memory WHERE telegram_id=? AND repo_full=? GROUP BY language', (telegram_id, repo_full)).fetchall()
            total = self.conn.execute('SELECT COUNT(*) AS c FROM repo_memory WHERE telegram_id=? AND repo_full=?', (telegram_id, repo_full)).fetchone()['c']
        else:
            rows = self.conn.execute('SELECT language, COUNT(*) AS c FROM repo_memory WHERE telegram_id=? GROUP BY language', (telegram_id,)).fetchall()
            total = self.conn.execute('SELECT COUNT(*) AS c FROM repo_memory WHERE telegram_id=?', (telegram_id,)).fetchone()['c']
        return {'telegram_id': telegram_id, 'repo_full': repo_full or '', 'total_chunks': total, 'languages': {r['language'] or 'Text': r['c'] for r in rows}}

    def forget_memory(self, telegram_id: int, repo_full: str | None = None) -> int:
        if repo_full:
            cur = self.conn.execute('DELETE FROM repo_memory WHERE telegram_id=? AND repo_full=?', (telegram_id, repo_full))
        else:
            cur = self.conn.execute('DELETE FROM repo_memory WHERE telegram_id=?', (telegram_id,))
        self.conn.commit()
        return cur.rowcount or 0

    def set_last_error(self, telegram_id: int, repo_full: str, branch: str, source: str, error: str) -> None:
        self.conn.execute('''INSERT INTO last_errors(telegram_id, repo_full, branch, source, error) VALUES(?,?,?,?,?)
            ON CONFLICT(telegram_id, repo_full) DO UPDATE SET
              branch=excluded.branch, source=excluded.source, error=excluded.error, updated_at=CURRENT_TIMESTAMP''',
            (telegram_id, repo_full, branch, source, error[:5000]))
        self.conn.commit()

    def get_last_error(self, telegram_id: int, repo_full: str) -> dict[str, Any]:
        row = self.conn.execute('SELECT * FROM last_errors WHERE telegram_id=? AND repo_full=?', (telegram_id, repo_full)).fetchone()
        return dict(row) if row else {}

    # -------------------------
    # Multistreaming helpers
    # -------------------------
    def ensure_streaming_tables(self) -> None:
        self.conn.execute('''CREATE TABLE IF NOT EXISTS stream_platforms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            platform_type TEXT NOT NULL,
            rtmp_url TEXT NOT NULL,
            stream_key_enc TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(telegram_id, name)
        )''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS stream_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            source_type TEXT NOT NULL,
            status TEXT NOT NULL,
            destinations_json TEXT DEFAULT '[]',
            pid INTEGER,
            started_at TEXT DEFAULT CURRENT_TIMESTAMP,
            ended_at TEXT,
            error TEXT DEFAULT ''
        )''')
        self.conn.execute('CREATE INDEX IF NOT EXISTS idx_stream_platforms_user_enabled ON stream_platforms(telegram_id, enabled)')
        self.conn.execute('CREATE INDEX IF NOT EXISTS idx_stream_history_user_status ON stream_history(telegram_id, status)')
        self.conn.commit()

    def add_stream_platform(self, telegram_id: int, name: str, platform_type: str, rtmp_url: str, stream_key: str, enabled: bool = True) -> None:
        self.ensure_streaming_tables()
        self.conn.execute('''INSERT INTO stream_platforms(telegram_id, name, platform_type, rtmp_url, stream_key_enc, enabled)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(telegram_id, name) DO UPDATE SET
              platform_type=excluded.platform_type, rtmp_url=excluded.rtmp_url,
              stream_key_enc=excluded.stream_key_enc, enabled=excluded.enabled, updated_at=CURRENT_TIMESTAMP''',
            (telegram_id, name.strip(), platform_type.upper().strip(), rtmp_url.strip().rstrip('/'), self.encrypt(stream_key.strip()), 1 if enabled else 0))
        self.conn.commit()
        self.audit(telegram_id, 'stream_platform_upsert', name, platform_type)

    def list_stream_platforms(self, telegram_id: int, enabled_only: bool = False, reveal_keys: bool = False) -> list[dict[str, Any]]:
        self.ensure_streaming_tables()
        if enabled_only:
            rows = self.conn.execute('SELECT * FROM stream_platforms WHERE telegram_id=? AND enabled=1 ORDER BY id DESC', (telegram_id,)).fetchall()
        else:
            rows = self.conn.execute('SELECT * FROM stream_platforms WHERE telegram_id=? ORDER BY id DESC', (telegram_id,)).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            key = self.decrypt(d.get('stream_key_enc')) if reveal_keys else ''
            d['stream_key'] = key
            d.pop('stream_key_enc', None)
            items.append(d)
        return items

    def get_stream_platforms_by_ids(self, telegram_id: int, ids: list[int]) -> list[dict[str, Any]]:
        self.ensure_streaming_tables()
        if not ids:
            return []
        placeholders = ','.join('?' for _ in ids)
        rows = self.conn.execute(f'SELECT * FROM stream_platforms WHERE telegram_id=? AND enabled=1 AND id IN ({placeholders})', (telegram_id, *ids)).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d['stream_key'] = self.decrypt(d.get('stream_key_enc'))
            d.pop('stream_key_enc', None)
            items.append(d)
        return items

    def set_stream_platform_enabled(self, telegram_id: int, platform_id: int, enabled: bool) -> None:
        self.ensure_streaming_tables()
        self.conn.execute('UPDATE stream_platforms SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE telegram_id=? AND id=?', (1 if enabled else 0, telegram_id, platform_id))
        self.conn.commit()

    def delete_stream_platform(self, telegram_id: int, platform_id: int) -> None:
        self.ensure_streaming_tables()
        self.conn.execute('DELETE FROM stream_platforms WHERE telegram_id=? AND id=?', (telegram_id, platform_id))
        self.conn.commit()

    def create_stream_history(self, telegram_id: int, title: str, source: str, source_type: str, status: str, destinations: list[dict[str, Any]], pid: int | None = None) -> int:
        self.ensure_streaming_tables()
        cur = self.conn.execute('''INSERT INTO stream_history(telegram_id, title, source, source_type, status, destinations_json, pid)
            VALUES(?,?,?,?,?,?,?)''', (telegram_id, title, source, source_type, status, json.dumps(destinations, ensure_ascii=False), pid))
        self.conn.commit()
        return int(cur.lastrowid)

    def update_stream_history(self, stream_id: int, status: str | None = None, pid: int | None = None, error: str = '', ended: bool = False) -> None:
        self.ensure_streaming_tables()
        if status is not None:
            self.conn.execute('UPDATE stream_history SET status=? WHERE id=?', (status, stream_id))
        if pid is not None:
            self.conn.execute('UPDATE stream_history SET pid=? WHERE id=?', (pid, stream_id))
        if error:
            self.conn.execute('UPDATE stream_history SET error=? WHERE id=?', (error[:2000], stream_id))
        if ended:
            self.conn.execute('UPDATE stream_history SET ended_at=CURRENT_TIMESTAMP WHERE id=?', (stream_id,))
        self.conn.commit()

    def latest_stream_history(self, telegram_id: int, limit: int = 5) -> list[dict[str, Any]]:
        self.ensure_streaming_tables()
        rows = self.conn.execute('SELECT * FROM stream_history WHERE telegram_id=? ORDER BY id DESC LIMIT ?', (telegram_id, limit)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d['destinations'] = json.loads(d.get('destinations_json') or '[]')
            except Exception:
                d['destinations'] = []
            result.append(d)
        return result

