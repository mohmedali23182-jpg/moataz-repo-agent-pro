from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt


class GoogleDriveError(RuntimeError):
    pass


@dataclass
class DriveUploadResult:
    id: str
    name: str
    web_view_link: str
    web_content_link: str
    shared_with: str = ''


class GoogleDriveClient:
    """Google Drive REST helper.

    Supports either:
    - OAuth access token with Drive scope.
    - Service account JSON credentials. Share the target folder with the service-account email first.
    """

    def __init__(self, token_or_service_account_json: str, folder_id: str = '') -> None:
        self.raw = token_or_service_account_json.strip()
        self.folder_id = folder_id.strip()
        if not self.raw:
            raise GoogleDriveError('Google Drive token or service-account JSON is required.')
        self._access_token_cache: tuple[str, float] | None = None

    async def access_token(self) -> str:
        if self.raw.startswith('{'):
            if self._access_token_cache and self._access_token_cache[1] > time.time() + 60:
                return self._access_token_cache[0]
            token = await self._service_account_access_token(json.loads(self.raw))
            self._access_token_cache = (token, time.time() + 3500)
            return token
        return self.raw

    async def _service_account_access_token(self, info: dict[str, Any]) -> str:
        now = int(time.time())
        claims = {
            'iss': info['client_email'],
            'scope': 'https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/drive',
            'aud': 'https://oauth2.googleapis.com/token',
            'exp': now + 3600,
            'iat': now,
        }
        assertion = jwt.encode(claims, info['private_key'], algorithm='RS256')
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post('https://oauth2.googleapis.com/token', data={
                'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
                'assertion': assertion,
            })
        data = r.json() if r.headers.get('content-type', '').startswith('application/json') else {'raw': r.text}
        if r.status_code >= 400:
            raise GoogleDriveError(f'Google OAuth error {r.status_code}: {data}')
        return data['access_token']

    async def about(self) -> dict[str, Any]:
        token = await self.access_token()
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(
                'https://www.googleapis.com/drive/v3/about?fields=user,storageQuota',
                headers={'Authorization': f'Bearer {token}'},
            )
        data = r.json() if r.headers.get('content-type', '').startswith('application/json') else {'raw': r.text}
        if r.status_code >= 400:
            raise GoogleDriveError(f'Google Drive error {r.status_code}: {data}')
        return data

    async def upload_file(self, path: Path, folder_id: str = '', share_email: str = '') -> DriveUploadResult:
        token = await self.access_token()
        target_folder = folder_id.strip() or self.folder_id
        metadata: dict[str, Any] = {'name': path.name}
        if target_folder:
            metadata['parents'] = [target_folder]
        async with httpx.AsyncClient(timeout=300) as client:
            # Resumable upload works for small and large files and is reliable on Railway.
            init = await client.post(
                'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,name,webViewLink,webContentLink',
                headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json; charset=UTF-8',
                    'X-Upload-Content-Type': 'application/octet-stream',
                    'X-Upload-Content-Length': str(path.stat().st_size),
                },
                json=metadata,
            )
            if init.status_code >= 400:
                raise GoogleDriveError(f'Google Drive upload init error {init.status_code}: {init.text[:1000]}')
            upload_url = init.headers.get('location')
            if not upload_url:
                raise GoogleDriveError('لم يرجع Google Drive رابط resumable upload.')
            put = await client.put(
                upload_url,
                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/octet-stream'},
                content=path.read_bytes(),
            )
            data = put.json() if put.headers.get('content-type', '').startswith('application/json') else {'raw': put.text}
            if put.status_code >= 400:
                raise GoogleDriveError(f'Google Drive upload error {put.status_code}: {data}')
            if share_email:
                await self.share_file(data['id'], share_email, role='reader')
            return DriveUploadResult(
                id=data.get('id', ''),
                name=data.get('name', path.name),
                web_view_link=data.get('webViewLink', ''),
                web_content_link=data.get('webContentLink', ''),
                shared_with=share_email,
            )

    async def share_file(self, file_id: str, email: str, role: str = 'reader') -> dict[str, Any]:
        token = await self.access_token()
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f'https://www.googleapis.com/drive/v3/files/{file_id}/permissions?sendNotificationEmail=true',
                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                json={'type': 'user', 'role': role, 'emailAddress': email},
            )
        data = r.json() if r.headers.get('content-type', '').startswith('application/json') else {'raw': r.text}
        if r.status_code >= 400:
            raise GoogleDriveError(f'Google Drive share error {r.status_code}: {data}')
        return data
