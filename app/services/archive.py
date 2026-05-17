from __future__ import annotations

import os
import shutil
import tarfile
import zipfile
from pathlib import Path

import py7zr
import rarfile

from app.config import get_settings


class ArchiveError(RuntimeError):
    pass


def _safe_join(base: Path, name: str) -> Path:
    dest = (base / name).resolve()
    if not str(dest).startswith(str(base.resolve())):
        raise ArchiveError(f'مسار خطير داخل الأرشيف: {name}')
    return dest


def _check_limits(root: Path) -> list[Path]:
    s = get_settings()
    files = [p for p in root.rglob('*') if p.is_file()]
    if len(files) > s.max_extracted_files:
        raise ArchiveError(f'عدد الملفات أكبر من الحد: {len(files)} > {s.max_extracted_files}')
    total = sum(p.stat().st_size for p in files)
    if total > s.max_extracted_mb * 1024 * 1024:
        raise ArchiveError(f'حجم الملفات بعد الفك أكبر من الحد: {total} bytes')
    return files


def extract_archive(src: Path, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    suffix = src.name.lower()
    try:
        if suffix.endswith('.zip'):
            with zipfile.ZipFile(src) as z:
                for member in z.infolist():
                    _safe_join(dest, member.filename)
                z.extractall(dest)
        elif suffix.endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz')):
            with tarfile.open(src) as t:
                for member in t.getmembers():
                    _safe_join(dest, member.name)
                t.extractall(dest, filter='data')
        elif suffix.endswith('.7z'):
            with py7zr.SevenZipFile(src, mode='r') as z:
                for name in z.getnames():
                    _safe_join(dest, name)
                z.extractall(dest)
        elif suffix.endswith('.rar'):
            with rarfile.RarFile(src) as rf:
                for name in rf.namelist():
                    _safe_join(dest, name)
                rf.extractall(dest)
        else:
            raise ArchiveError('نوع الأرشيف غير مدعوم. استخدم zip/rar/7z/tar.')
    except Exception as e:
        raise ArchiveError(str(e)) from e
    return _check_limits(dest)
