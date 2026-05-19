from __future__ import annotations

import asyncio
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Literal

from app.config import get_settings

StreamCallback = Callable[[str, dict], Awaitable[None] | None]
SourceType = Literal["youtube", "file", "direct"]


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
        return bool(self.active and self.active.process and self.active.process.returncode is None and self.active.status in {"starting", "active"})

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

    async def start(self, *, source: str, destinations: list[str], title: str = "Live Stream", prefer_copy: bool = True) -> StreamSession:
        if self.is_active():
            raise RuntimeError("يوجد بث نشط بالفعل. أوقفه أولًا عبر /stream_stop")
        if not destinations:
            raise ValueError("لم يتم تحديد أي وجهة RTMP.")
        for url in destinations:
            if not (url.startswith("rtmp://") or url.startswith("rtmps://")):
                raise ValueError(f"وجهة RTMP غير صحيحة: {url}")
        source_type = self._detect_source(source)
        if source_type == "file" and not Path(source).exists():
            raise FileNotFoundError(f"الملف غير موجود: {source}")
        stream_id = f"stream_{int(time.time())}"
        session = StreamSession(stream_id=stream_id, source=source, source_type=source_type, destinations=destinations, title=title)
        self.active = session
        asyncio.create_task(self._run_with_retry(session, prefer_copy=prefer_copy))
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
            return "direct"
        return "file"

    async def _resolve_input(self, source: str, source_type: SourceType) -> str:
        if source_type != "youtube":
            return source
        ytdlp = self.settings.ytdlp_path or "yt-dlp"
        cmd = [ytdlp, "-f", "best[protocol^=http][ext=mp4]/best[protocol^=http]/best", "--no-playlist", "-g", source]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            raise RuntimeError("فشل yt-dlp في استخراج رابط البث: " + err.decode(errors="ignore")[-1000:])
        direct = out.decode(errors="ignore").strip().splitlines()[0] if out else ""
        if not direct.startswith("http"):
            raise RuntimeError("yt-dlp لم يرجع رابط فيديو صالح.")
        return direct

    def _build_ffmpeg_args(self, input_url: str, destinations: list[str], *, force_transcode: bool, prefer_copy: bool) -> list[str]:
        ffmpeg = self.settings.ffmpeg_path or "ffmpeg"
        args = [ffmpeg, "-hide_banner", "-loglevel", "warning"]
        if not input_url.startswith("http"):
            args += ["-re"]
        args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_at_eof", "1", "-reconnect_delay_max", "5", "-i", input_url]
        for dest in destinations:
            args += ["-map", "0:v:0", "-map", "0:a:0?"]
            if prefer_copy and not force_transcode:
                args += ["-c:v", "copy"]
            else:
                args += ["-c:v", "libx264", "-preset", self.settings.stream_fallback_preset, "-tune", "zerolatency", "-b:v", self.settings.stream_video_bitrate, "-maxrate", self.settings.stream_video_bitrate, "-bufsize", self.settings.stream_buffer_size, "-pix_fmt", "yuv420p", "-r", str(self.settings.stream_fps), "-g", str(self.settings.stream_gop)]
            args += ["-c:a", "aac", "-b:a", self.settings.stream_audio_bitrate, "-ar", "44100", "-ac", "2", "-fflags", "+genpts", "-avoid_negative_ts", "make_zero", "-f", "flv", dest]
        return args

    async def _run_with_retry(self, session: StreamSession, *, prefer_copy: bool) -> None:
        try:
            input_url = await self._resolve_input(session.source, session.source_type)
            await session.emit("start", {"stream_id": session.stream_id, "phase": "resolved_input"})
            code = await self._run_ffmpeg(session, input_url, force_transcode=False, prefer_copy=prefer_copy)
            if code != 0 and prefer_copy and not session._stop_requested:
                await session.emit("log", {"stream_id": session.stream_id, "message": "فشل النسخ المباشر، إعادة المحاولة بترميز H.264..."})
                code = await self._run_ffmpeg(session, input_url, force_transcode=True, prefer_copy=prefer_copy)
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

    async def _run_ffmpeg(self, session: StreamSession, input_url: str, *, force_transcode: bool, prefer_copy: bool) -> int:
        args = self._build_ffmpeg_args(input_url, session.destinations, force_transcode=force_transcode, prefer_copy=prefer_copy)
        await session.emit("start", {"stream_id": session.stream_id, "command": " ".join(shlex.quote(x) for x in args[:20]) + " ..."})
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
                    await session.emit("log", {"stream_id": session.stream_id, "message": text[-900:]})

        reader = asyncio.create_task(read_stderr())
        code = await proc.wait()
        try:
            await asyncio.wait_for(reader, timeout=2)
        except asyncio.TimeoutError:
            reader.cancel()
        return int(code or 0)


streaming_manager = StreamingManager()
