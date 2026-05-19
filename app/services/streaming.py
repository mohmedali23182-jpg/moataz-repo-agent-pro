from __future__ import annotations

import asyncio
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Literal

from app.config import get_settings

StreamCallback = Callable[[str, dict], Awaitable[None] | None]
SourceType = Literal["youtube", "file", "direct", "audio"]

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".opus"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts", ".flv"}


def _is_audio_file(value: str) -> bool:
    return Path(value.split("?", 1)[0]).suffix.lower() in AUDIO_EXTENSIONS


def _is_video_file(value: str) -> bool:
    return Path(value.split("?", 1)[0]).suffix.lower() in VIDEO_EXTENSIONS


def _mask_sensitive(text: str) -> str:
    # Keep ffmpeg logs safe. RTMP keys often appear at the end of output URLs.
    parts = text.split()
    safe: list[str] = []
    for part in parts:
        if part.startswith(("rtmp://", "rtmps://")):
            safe.append(part.rsplit("/", 1)[0] + "/********")
        else:
            safe.append(part)
    return " ".join(safe)


@dataclass
class StreamSession:
    stream_id: str
    source: str
    source_type: SourceType
    destinations: list[str]
    title: str = "Live Stream"
    status: str = "starting"
    process: asyncio.subprocess.Process | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    error: str = ""
    _callbacks: list[StreamCallback] = field(default_factory=list)
    _stop_requested: bool = False

    def on(self, callback: StreamCallback) -> None:
        self._callbacks.append(callback)

    async def emit(self, event: str, payload: dict) -> None:
        for cb in list(self._callbacks):
            try:
                result = cb(event, payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

    async def stop(self, timeout: int = 8) -> None:
        self._stop_requested = True
        self.status = "stopping"
        if not self.process or self.process.returncode is not None:
            self.status = "completed"
            self.ended_at = time.time()
            await self.emit("end", {"stream_id": self.stream_id, "reason": "already_stopped"})
            return
        try:
            self.process.terminate()
            await asyncio.wait_for(self.process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
        finally:
            self.ended_at = time.time()
            self.status = "completed"
            await self.emit("end", {"stream_id": self.stream_id, "reason": "user_stop"})


class StreamingManager:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.active: StreamSession | None = None

    def is_active(self) -> bool:
        return bool(
            self.active
            and self.active.process
            and self.active.process.returncode is None
            and self.active.status in {"starting", "active", "stopping"}
        )

    def status(self) -> dict:
        if not self.active:
            return {"active": False, "status": "offline"}
        s = self.active
        return {
            "active": self.is_active(),
            "stream_id": s.stream_id,
            "title": s.title,
            "source": s.source,
            "source_type": s.source_type,
            "destinations_count": len(s.destinations),
            "status": s.status,
            "pid": s.process.pid if s.process else None,
            "started_at": s.started_at,
            "ended_at": s.ended_at,
            "error": s.error,
        }

    async def start(
        self,
        *,
        source: str,
        destinations: list[str],
        title: str = "Live Stream",
        prefer_copy: bool = True,
        audio_cover_image: str | None = None,
    ) -> StreamSession:
        if self.is_active():
            raise RuntimeError("يوجد بث نشط بالفعل. أوقفه أولًا عبر /stream_stop")
        if not destinations:
            raise ValueError("لم يتم تحديد أي وجهة RTMP.")
        for url in destinations:
            if not (url.startswith("rtmp://") or url.startswith("rtmps://")):
                raise ValueError(f"وجهة RTMP غير صحيحة: {url}")

        source_type = self._detect_source(source)
        if source_type in {"file", "audio"} and not Path(source).exists():
            raise FileNotFoundError(f"الملف غير موجود: {source}")

        cover = audio_cover_image or self.settings.stream_audio_cover_image or ""
        if cover and not Path(cover).exists():
            cover = ""

        stream_id = f"stream_{int(time.time())}"
        session = StreamSession(
            stream_id=stream_id,
            source=source,
            source_type=source_type,
            destinations=destinations,
            title=title,
        )
        self.active = session
        asyncio.create_task(self._run_with_retry(session, prefer_copy=prefer_copy, audio_cover_image=cover or None))
        return session

    async def stop(self) -> bool:
        if not self.active:
            return False
        await self.active.stop(timeout=self.settings.stream_graceful_stop_seconds)
        self.active = None
        return True

    def _detect_source(self, source: str) -> SourceType:
        lower = source.lower().strip()
        if "youtube.com/" in lower or "youtu.be/" in lower or "music.youtube.com/" in lower:
            return "youtube"
        if lower.startswith("http://") or lower.startswith("https://"):
            if _is_audio_file(lower):
                return "audio"
            return "direct"
        if _is_audio_file(lower):
            return "audio"
        return "file"

    async def _resolve_input(self, source: str, source_type: SourceType) -> str:
        if source_type != "youtube":
            return source

        ytdlp = self.settings.ytdlp_path or "yt-dlp"
        cmd = [
            ytdlp,
            "--no-playlist",
            "--force-ipv4",
            "--user-agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "--add-header",
            "Accept-Language:en-US,en;q=0.9",
            "--js-runtimes",
            self.settings.ytdlp_js_runtime or "deno",
            "-f",
            "best[protocol^=http][ext=mp4]/best[protocol^=http]/best",
            "-g",
            source,
        ]
        if self.settings.ytdlp_cookies_path:
            cmd[1:1] = ["--cookies", self.settings.ytdlp_cookies_path]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=90)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("انتهت مهلة yt-dlp أثناء استخراج رابط يوتيوب.")

        if proc.returncode != 0:
            stderr = err.decode(errors="ignore")[-1800:]
            raise RuntimeError("فشل yt-dlp في استخراج رابط البث:\n" + stderr)

        direct = out.decode(errors="ignore").strip().splitlines()[0] if out else ""
        if not direct.startswith("http"):
            raise RuntimeError("yt-dlp لم يرجع رابط فيديو صالح.")
        return direct

    def _common_video_output_args(self, *, force_transcode: bool, prefer_copy: bool) -> list[str]:
        if prefer_copy and not force_transcode:
            video = ["-c:v", "copy"]
        else:
            video = [
                "-c:v",
                "libx264",
                "-preset",
                self.settings.stream_fallback_preset,
                "-tune",
                "zerolatency",
                "-b:v",
                self.settings.stream_video_bitrate,
                "-maxrate",
                self.settings.stream_video_bitrate,
                "-bufsize",
                self.settings.stream_buffer_size,
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(self.settings.stream_fps),
                "-g",
                str(self.settings.stream_gop),
            ]
        return video + [
            "-c:a",
            "aac",
            "-b:a",
            self.settings.stream_audio_bitrate,
            "-ar",
            "44100",
            "-ac",
            "2",
            "-fflags",
            "+genpts",
            "-avoid_negative_ts",
            "make_zero",
            "-f",
            "flv",
        ]

    def _build_audio_ffmpeg_args(self, audio_path: str, destinations: list[str], *, cover_image_path: str | None = None) -> list[str]:
        ffmpeg = self.settings.ffmpeg_path or "ffmpeg"
        args = [ffmpeg, "-hide_banner", "-loglevel", "warning"]
        if cover_image_path:
            args += ["-re", "-loop", "1", "-i", cover_image_path, "-stream_loop", "-1", "-i", audio_path, "-map", "0:v:0", "-map", "1:a:0"]
        else:
            args += [
                "-re",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={self.settings.stream_audio_canvas_size}:r={self.settings.stream_fps}",
                "-stream_loop",
                "-1",
                "-i",
                audio_path,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
            ]

        base_output = [
            "-c:v",
            "libx264",
            "-preset",
            self.settings.stream_fallback_preset,
            "-tune",
            "zerolatency",
            "-b:v",
            self.settings.stream_audio_canvas_bitrate,
            "-maxrate",
            self.settings.stream_audio_canvas_bitrate,
            "-bufsize",
            self.settings.stream_audio_canvas_buffer_size,
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(self.settings.stream_fps),
            "-g",
            str(self.settings.stream_gop),
            "-c:a",
            "aac",
            "-b:a",
            self.settings.stream_audio_bitrate,
            "-ar",
            "44100",
            "-ac",
            "2",
            "-shortest",
            "-f",
            "flv",
        ]
        for dest in destinations:
            args += base_output + [dest]
        return args

    def _build_video_ffmpeg_args(self, input_url: str, destinations: list[str], *, force_transcode: bool, prefer_copy: bool) -> list[str]:
        ffmpeg = self.settings.ffmpeg_path or "ffmpeg"
        args = [ffmpeg, "-hide_banner", "-loglevel", "warning"]
        if not input_url.startswith("http"):
            args += ["-re"]
        args += [
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_at_eof",
            "1",
            "-reconnect_delay_max",
            "5",
            "-i",
            input_url,
        ]
        for dest in destinations:
            args += ["-map", "0:v:0", "-map", "0:a:0?"]
            args += self._common_video_output_args(force_transcode=force_transcode, prefer_copy=prefer_copy)
            args += [dest]
        return args

    async def _run_with_retry(self, session: StreamSession, *, prefer_copy: bool, audio_cover_image: str | None) -> None:
        try:
            input_url = await self._resolve_input(session.source, session.source_type)
            await session.emit("start", {"stream_id": session.stream_id, "phase": "resolved_input", "source_type": session.source_type})

            if session.source_type == "audio":
                args = self._build_audio_ffmpeg_args(input_url, session.destinations, cover_image_path=audio_cover_image)
                code = await self._run_ffmpeg_args(session, args)
            else:
                code = await self._run_ffmpeg_args(
                    session,
                    self._build_video_ffmpeg_args(input_url, session.destinations, force_transcode=False, prefer_copy=prefer_copy),
                )
                if code != 0 and prefer_copy and not session._stop_requested:
                    await session.emit("log", {"stream_id": session.stream_id, "message": "فشل النسخ المباشر، إعادة المحاولة بترميز H.264..."})
                    code = await self._run_ffmpeg_args(
                        session,
                        self._build_video_ffmpeg_args(input_url, session.destinations, force_transcode=True, prefer_copy=prefer_copy),
                    )

            if session._stop_requested:
                session.status = "completed"
                return
            if code == 0:
                session.status = "completed"
                await session.emit("end", {"stream_id": session.stream_id, "code": code})
            else:
                session.status = "failed"
                session.error = f"FFmpeg exited with code {code}"
                await session.emit("error", {"stream_id": session.stream_id, "error": session.error})
        except Exception as exc:
            session.status = "failed"
            session.error = str(exc)
            session.ended_at = time.time()
            await session.emit("error", {"stream_id": session.stream_id, "error": str(exc)})
        finally:
            session.ended_at = time.time()
            if self.active and self.active.stream_id == session.stream_id and session.status in {"completed", "failed"}:
                self.active = None

    async def _run_ffmpeg_args(self, session: StreamSession, args: list[str]) -> int:
        await session.emit("start", {"stream_id": session.stream_id, "command": _mask_sensitive(" ".join(shlex.quote(x) for x in args))[:1400]})
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        session.process = proc
        session.status = "active"
        await session.emit("active", {"stream_id": session.stream_id, "pid": proc.pid})

        async def read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="ignore").strip()
                if text:
                    await session.emit("log", {"stream_id": session.stream_id, "message": _mask_sensitive(text)[-900:]})

        reader = asyncio.create_task(read_stderr())
        code = await proc.wait()
        try:
            await asyncio.wait_for(reader, timeout=2)
        except asyncio.TimeoutError:
            reader.cancel()
        return int(code or 0)


streaming_manager = StreamingManager()
