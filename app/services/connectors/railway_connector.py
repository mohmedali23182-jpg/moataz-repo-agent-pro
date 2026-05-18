from __future__ import annotations

from typing import Any

import httpx

from app.services.connectors.base import ConnectorError, ConnectorResult, mask_variables


class RailwayConnector:
    """Official Railway Public API connector using GraphQL.

    Works with account/workspace tokens through Authorization: Bearer.
    Project tokens may need the Project-Access-Token header; pass token_kind='project'.
    """

    endpoint = 'https://backboard.railway.app/graphql/v2'

    def __init__(self, token: str, token_kind: str = 'account') -> None:
        self.token = token.strip()
        self.token_kind = token_kind
        if not self.token:
            raise ConnectorError('Railway token is required.')

    def _headers(self) -> dict[str, str]:
        if self.token_kind == 'project':
            return {'Project-Access-Token': self.token, 'Content-Type': 'application/json'}
        return {'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'}

    async def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(self.endpoint, headers=self._headers(), json={'query': query, 'variables': variables or {}})
        try:
            data = r.json()
        except Exception:
            data = {'raw': r.text}
        if r.status_code >= 400 or data.get('errors'):
            raise ConnectorError(f'Railway API error {r.status_code}: {data}')
        return data.get('data') or {}

    async def whoami(self) -> ConnectorResult:
        query = 'query { me { id name email } }'
        data = await self.graphql(query)
        return ConnectorResult(True, 'railway', 'whoami', 'Railway token works.', data)

    async def projects(self) -> ConnectorResult:
        query = '''
        query Projects { projects(first: 30) { edges { node { id name createdAt updatedAt } } } }
        '''
        data = await self.graphql(query)
        edges = (((data.get('projects') or {}).get('edges')) or [])
        projects = [edge.get('node') for edge in edges if edge.get('node')]
        return ConnectorResult(True, 'railway', 'projects', f'Found {len(projects)} Railway projects.', {'projects': projects})

    async def project(self, project_id: str) -> ConnectorResult:
        query = '''
        query Project($id: String!) {
          project(id: $id) {
            id name
            environments { edges { node { id name } } }
            services { edges { node { id name } } }
          }
        }
        '''
        data = await self.graphql(query, {'id': project_id})
        return ConnectorResult(True, 'railway', 'project', 'Railway project loaded.', data)

    async def variables(self, project_id: str, environment_id: str, service_id: str | None = None, unrendered: bool = True) -> ConnectorResult:
        field = 'variables' if unrendered else 'variables'
        query = '''
        query Vars($projectId: String!, $environmentId: String!, $serviceId: String) {
          variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId)
        }
        '''
        data = await self.graphql(query, {'projectId': project_id, 'environmentId': environment_id, 'serviceId': service_id or None})
        values = data.get(field) or data.get('variables') or {}
        return ConnectorResult(True, 'railway', 'variables', f'Found {len(values)} variables.', {'variables': mask_variables(values)})

    async def set_variable(self, project_id: str, environment_id: str, name: str, value: str, service_id: str | None = None, skip_deploys: bool = False) -> ConnectorResult:
        mutation = '''
        mutation VariableUpsert($input: VariableUpsertInput!) { variableUpsert(input: $input) }
        '''
        payload: dict[str, Any] = {
            'projectId': project_id,
            'environmentId': environment_id,
            'name': name,
            'value': value,
            'skipDeploys': skip_deploys,
        }
        if service_id:
            payload['serviceId'] = service_id
        data = await self.graphql(mutation, {'input': payload})
        return ConnectorResult(True, 'railway', 'set_variable', f'Variable {name} was upserted.', data)

    async def set_variables(self, project_id: str, environment_id: str, variables: dict[str, str], service_id: str | None = None, replace: bool = False, skip_deploys: bool = False) -> ConnectorResult:
        mutation = '''
        mutation VariableCollectionUpsert($input: VariableCollectionUpsertInput!) { variableCollectionUpsert(input: $input) }
        '''
        payload: dict[str, Any] = {
            'projectId': project_id,
            'environmentId': environment_id,
            'variables': variables,
            'replace': replace,
            'skipDeploys': skip_deploys,
        }
        if service_id:
            payload['serviceId'] = service_id
        data = await self.graphql(mutation, {'input': payload})
        return ConnectorResult(True, 'railway', 'set_variables', f'Upserted {len(variables)} variables.', data)

    async def delete_variable(self, project_id: str, environment_id: str, name: str, service_id: str | None = None) -> ConnectorResult:
        mutation = '''
        mutation VariableDelete($input: VariableDeleteInput!) { variableDelete(input: $input) }
        '''
        payload: dict[str, Any] = {'projectId': project_id, 'environmentId': environment_id, 'name': name}
        if service_id:
            payload['serviceId'] = service_id
        data = await self.graphql(mutation, {'input': payload})
        return ConnectorResult(True, 'railway', 'delete_variable', f'Deleted variable {name}.', data)

    async def redeploy_service(self, service_id: str, environment_id: str) -> ConnectorResult:
        # Kept intentionally narrow: schema names may differ between Railway API revisions.
        mutation = '''
        mutation ServiceInstanceRedeploy($serviceId: String!, $environmentId: String!) {
          serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
        }
        '''
        data = await self.graphql(mutation, {'serviceId': service_id, 'environmentId': environment_id})
        return ConnectorResult(True, 'railway', 'redeploy', 'Redeploy requested.', data)
