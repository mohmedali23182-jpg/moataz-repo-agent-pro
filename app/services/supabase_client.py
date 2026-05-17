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

    async def select(self, table: str, limit: int = 20, query: str = '*') -> list[dict[str, Any]]:
        if table not in self.settings.allowed_tables:
            raise SupabaseError('هذا الجدول غير مسموح. أضفه في SUPABASE_ALLOWED_TABLES.')
        if not self.enabled():
            raise SupabaseError('Supabase غير مضبوط.')
        headers = {'apikey': self.key, 'Authorization': f'Bearer {self.key}', 'Accept': 'application/json'}
        url = f"{self.settings.supabase_url.rstrip('/')}/rest/v1/{table}?select={query}&limit={limit}"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=headers)
        if r.status_code >= 400:
            raise SupabaseError(r.text[:1000])
        return r.json()
