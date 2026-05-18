from __future__ import annotations

from typing import Any

import httpx

from app.services.connectors.base import ConnectorError, ConnectorResult, mask_variables


class VercelConnector:
    base = 'https://api.vercel.com'

    def __init__(self, token: str, team_id: str | None = None) -> None:
        self.token = token.strip()
        self.team_id = (team_id or '').strip() or None
        if not self.token:
            raise ConnectorError('Vercel token is required.')

    def _headers(self) -> dict[str, str]:
        return {'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'}

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(extra or {})
        if self.team_id:
            params['teamId'] = self.team_id
        return params

    async def request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.request(method, self.base + path, headers=self._headers(), **kwargs)
        try:
            data = r.json()
        except Exception:
            data = {'raw': r.text}
        if r.status_code >= 400:
            raise ConnectorError(f'Vercel API error {r.status_code}: {data}')
        return data

    async def whoami(self) -> ConnectorResult:
        data = await self.request('GET', '/v2/user')
        return ConnectorResult(True, 'vercel', 'whoami', 'Vercel token works.', data)

    async def projects(self) -> ConnectorResult:
        data = await self.request('GET', '/v9/projects', params=self._params({'limit': 50}))
        return ConnectorResult(True, 'vercel', 'projects', f"Found {len(data.get('projects', []))} Vercel projects.", data)

    async def variables(self, project: str) -> ConnectorResult:
        data = await self.request('GET', f'/v10/projects/{project}/env', params=self._params())
        masked = data.copy()
        if isinstance(masked.get('envs'), list):
            for item in masked['envs']:
                if 'value' in item:
                    item['value'] = '****'
        return ConnectorResult(True, 'vercel', 'variables', 'Vercel variables loaded.', masked)

    async def set_variable(self, project: str, key: str, value: str, target: str = 'production', var_type: str = 'encrypted') -> ConnectorResult:
        body = {'key': key, 'value': value, 'type': var_type, 'target': [target]}
        data = await self.request('POST', f'/v10/projects/{project}/env', params=self._params({'upsert': 'true'}), json=body)
        return ConnectorResult(True, 'vercel', 'set_variable', f'Variable {key} was upserted for {target}.', data)

    async def set_variables(self, project: str, variables: dict[str, str], target: str = 'production', var_type: str = 'encrypted') -> ConnectorResult:
        body = [{'key': k, 'value': v, 'type': var_type, 'target': [target]} for k, v in variables.items()]
        data = await self.request('POST', f'/v10/projects/{project}/env', params=self._params({'upsert': 'true'}), json=body)
        return ConnectorResult(True, 'vercel', 'set_variables', f'Upserted {len(variables)} variables for {target}.', data)

    async def deployments(self, project: str) -> ConnectorResult:
        data = await self.request('GET', '/v6/deployments', params=self._params({'projectId': project, 'limit': 10}))
        return ConnectorResult(True, 'vercel', 'deployments', 'Vercel deployments loaded.', data)
