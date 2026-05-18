from __future__ import annotations

from typing import Any

import httpx

from app.services.connectors.base import ConnectorError, ConnectorResult, mask_variables


class RenderConnector:
    base = 'https://api.render.com/v1'

    def __init__(self, token: str) -> None:
        self.token = token.strip()
        if not self.token:
            raise ConnectorError('Render API key is required.')

    def _headers(self) -> dict[str, str]:
        return {'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'}

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.request(method, self.base + path, headers=self._headers(), **kwargs)
        try:
            data = r.json()
        except Exception:
            data = {'raw': r.text}
        if r.status_code >= 400:
            raise ConnectorError(f'Render API error {r.status_code}: {data}')
        return data

    async def whoami(self) -> ConnectorResult:
        data = await self.request('GET', '/owners')
        return ConnectorResult(True, 'render', 'whoami', 'Render token works.', {'owners': data})

    async def projects(self) -> ConnectorResult:
        data = await self.request('GET', '/services?limit=50')
        return ConnectorResult(True, 'render', 'services', 'Render services loaded.', {'services': data})

    async def services(self) -> ConnectorResult:
        return await self.projects()

    async def variables(self, service_id: str) -> ConnectorResult:
        data = await self.request('GET', f'/services/{service_id}/env-vars')
        if isinstance(data, list):
            masked = [{**x, 'value': '****'} if isinstance(x, dict) and 'value' in x else x for x in data]
        else:
            masked = data
        return ConnectorResult(True, 'render', 'variables', 'Render env vars loaded.', {'variables': masked})

    async def set_variable(self, service_id: str, key: str, value: str) -> ConnectorResult:
        # Render supports updating env vars as a list. This method preserves existing keys when the API returns them.
        current = await self.request('GET', f'/services/{service_id}/env-vars')
        items: list[dict[str, str]] = []
        if isinstance(current, list):
            for item in current:
                if isinstance(item, dict):
                    k = item.get('key') or item.get('name')
                    v = item.get('value') or ''
                    if k and k != key:
                        items.append({'key': k, 'value': v})
        items.append({'key': key, 'value': value})
        data = await self.request('PUT', f'/services/{service_id}/env-vars', json=items)
        return ConnectorResult(True, 'render', 'set_variable', f'Render variable {key} upserted.', data if isinstance(data, dict) else {'result': data})

    async def set_variables(self, service_id: str, variables: dict[str, str]) -> ConnectorResult:
        for k, v in variables.items():
            await self.set_variable(service_id, k, v)
        return ConnectorResult(True, 'render', 'set_variables', f'Upserted {len(variables)} variables.', {'variables': mask_variables(variables)})

    async def redeploy_service(self, service_id: str) -> ConnectorResult:
        data = await self.request('POST', f'/services/{service_id}/deploys', json={'clearCache': 'do_not_clear'})
        return ConnectorResult(True, 'render', 'redeploy', 'Render deploy triggered.', data if isinstance(data, dict) else {'result': data})
