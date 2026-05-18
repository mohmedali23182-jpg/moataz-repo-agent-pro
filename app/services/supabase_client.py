from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


class SupabaseError(RuntimeError):
    pass


class SupabaseClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.key = self.settings.supabase_service_role_key or self.settings.supabase_anon_key

    def enabled(self) -> bool:
        return bool(self.settings.supabase_url and self.key)

    def _headers(self) -> dict[str, str]:
        return {
            'apikey': self.key,
            'Authorization': f'Bearer {self.key}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

    def _guard_table(self, table: str) -> None:
        if self.settings.allowed_tables and table not in self.settings.allowed_tables:
            raise SupabaseError('هذا الجدول غير مسموح. أضفه في SUPABASE_ALLOWED_TABLES.')

    async def select(self, table: str, limit: int = 20, query: str = '*') -> list[dict[str, Any]]:
        self._guard_table(table)
        if not self.enabled():
            raise SupabaseError('Supabase غير مضبوط.')
        url = f"{self.settings.supabase_url.rstrip('/')}/rest/v1/{table}?select={query}&limit={limit}"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=self._headers())
        if r.status_code >= 400:
            raise SupabaseError(r.text[:1000])
        return r.json()

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> Any:
        self._guard_table(table)
        if not self.enabled():
            raise SupabaseError('Supabase غير مضبوط.')
        url = f"{self.settings.supabase_url.rstrip('/')}/rest/v1/{table}"
        headers = self._headers() | {'Prefer': 'return=representation'}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, headers=headers, json=rows)
        if r.status_code >= 400:
            raise SupabaseError(r.text[:1000])
        return r.json()

    async def rpc(self, fn: str, params: dict[str, Any]) -> Any:
        if not self.enabled():
            raise SupabaseError('Supabase غير مضبوط.')
        if not self.settings.supabase_service_role_key:
            raise SupabaseError('RPC يحتاج غالبًا SUPABASE_SERVICE_ROLE_KEY.')
        url = f"{self.settings.supabase_url.rstrip('/')}/rest/v1/rpc/{fn}"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, headers=self._headers(), json=params)
        if r.status_code >= 400:
            raise SupabaseError(r.text[:1000])
        try:
            return r.json()
        except Exception:
            return {'ok': True, 'text': r.text}


class SupabaseSqlClient:
    """Runs SQL on a Supabase Postgres database using DATABASE_URL/DIRECT_URL.

    This is intentionally disabled unless SUPABASE_ALLOW_SQL=true. Use it only with
    databases you own or are explicitly authorized to administer.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.dsn = self.settings.direct_url or self.settings.database_url

    def enabled(self) -> bool:
        return bool(self.settings.supabase_allow_sql and self.dsn)

    async def execute(self, sql: str) -> dict[str, Any]:
        if not self.settings.supabase_allow_sql:
            raise SupabaseError('تنفيذ SQL معطل. فعّل SUPABASE_ALLOW_SQL=true عند الحاجة.')
        if not self.dsn:
            raise SupabaseError('ضع DIRECT_URL أو DATABASE_URL لقاعدة Supabase.')
        if len(sql) > 20000:
            raise SupabaseError('SQL طويل جدًا.')
        lowered = sql.lower()
        blocked = ['drop database', 'drop schema public cascade', 'pg_read_file', 'copy ', 'program '] 
        if any(x in lowered for x in blocked):
            raise SupabaseError('SQL يحتوي أمرًا خطيرًا أو غير مسموح.')
        import asyncpg
        conn = await asyncpg.connect(self.dsn)
        try:
            if lowered.strip().startswith(('select', 'with')):
                rows = await conn.fetch(sql)
                return {'ok': True, 'rows': [dict(r) for r in rows[:200]], 'row_count': len(rows)}
            status = await conn.execute(sql)
            return {'ok': True, 'status': status}
        finally:
            await conn.close()
