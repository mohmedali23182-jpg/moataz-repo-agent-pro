from __future__ import annotations

import re
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, unquote

import httpx

from app.config import get_settings


class DownloadError(RuntimeError):
    pass


@dataclass
class DownloadResult:
    ok: bool
    url: str
    path: Path | None
    filename: str
    size_bytes: int
    content_type: str
    source_type: str
    message: str


APK_EXTENSIONS = {'.apk', '.apkm', '.xapk', '.apks', '.aab'}
DIRECT_EXTENSIONS = APK_EXTENSIONS | {
    '.zip', '.rar', '.7z', '.tar', '.gz', '.tgz', '.pdf', '.txt', '.json', '.csv',
    '.png', '.jpg', '.jpeg', '.webp', '.mp4', '.mp3', '.wav', '.doc', '.docx',
}


def classify_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()
    if 'play.google.com' in host:
        return 'google_play_listing'
    if any(path.endswith(ext) for ext in APK_EXTENSIONS):
        return 'android_package_direct'
    if any(path.endswith(ext) for ext in DIRECT_EXTENSIONS):
        return 'direct_file'
    return 'web_page_or_unknown'


def filename_from_url_or_headers(url: str, headers: httpx.Headers, fallback: str = 'download.bin') -> str:
    cd = headers.get('content-disposition', '')
    # RFC 6266-ish, tolerant enough for common servers.
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I) or re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        return Path(unquote(m.group(1))).name
    name = Path(unquote(urlparse(url).path)).name
    if name:
        return name
    ctype = headers.get('content-type', '').split(';', 1)[0].strip()
    ext = mimetypes.guess_extension(ctype) or ''
    return fallback if fallback.endswith(ext) else fallback + ext


async def download_direct_url(url: str, dest_dir: Path, preferred_name: str = '', allow_html: bool = False) -> DownloadResult:
    settings = get_settings()
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        raise DownloadError('الرابط يجب أن يبدأ بـ http أو https.')

    source_type = classify_url(url)
    if source_type == 'google_play_listing':
        raise DownloadError(
            'هذا رابط صفحة Google Play وليس رابط APK مباشر. Google Play لا يوفر API رسميًا لتنزيل APK للمستخدمين من رابط المتجر. '
            'أرسل رابط ملف مباشر تملك حق تحميله مثل .apk/.xapk/.apks أو GitHub Release asset أو استخدم ملفًا من جهازك.'
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    limit = settings.max_upload_mb * 1024 * 1024
    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        async with client.stream('GET', url, headers={'User-Agent': 'MoatazRepoAgent/1.0'}) as r:
            if r.status_code >= 400:
                raise DownloadError(f'فشل التحميل من الرابط. HTTP {r.status_code}')
            ctype = r.headers.get('content-type', '').lower()
            if 'text/html' in ctype and not allow_html:
                raise DownloadError('الرابط يبدو صفحة HTML وليس ملفًا مباشرًا. أرسل رابط تحميل مباشر للملف.')
            filename = preferred_name.strip() or filename_from_url_or_headers(str(r.url), r.headers)
            filename = re.sub(r'[^\w.\-\u0600-\u06FF ]+', '_', filename).strip() or 'download.bin'
            path = dest_dir / filename
            total = 0
            with path.open('wb') as f:
                async for chunk in r.aiter_bytes():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > limit:
                        try:
                            path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        raise DownloadError(f'الملف أكبر من MAX_UPLOAD_MB={settings.max_upload_mb}.')
                    f.write(chunk)
    return DownloadResult(True, url, path, path.name, total, ctype, source_type, 'تم التحميل بنجاح')


def android_package_report(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in APK_EXTENSIONS:
        return 'الملف ليس حزمة Android مباشرة حسب الامتداد.'
    size = path.stat().st_size if path.exists() else 0
    return (
        '📱 <b>تقرير حزمة Android</b>\n'
        f'النوع: <code>{suffix}</code>\n'
        f'الحجم: <b>{size / 1024 / 1024:.2f} MB</b>\n'
        'ملاحظة: ملفات APK/XAPK/APKS قد تكون موجهة لمعمارية أو إصدار Android محدد حسب المصدر. '
        'إذا أردت نسخًا للهواتف الحديثة والقديمة، أرسل روابط مباشرة متعددة أو مصدرًا يوفّر variants بشكل قانوني.'
    )
