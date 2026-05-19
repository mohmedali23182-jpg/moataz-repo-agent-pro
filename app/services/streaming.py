from __future__ import annotations

import asyncio
import shlex
import time
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Literal, Any

from app.config import get_settings

StreamCallback = Callable[[str, dict], Awaitable[None] | None]
SourceType = Literal["youtube", "file", "audio_file", "direct"]


@dataclass
class StreamSession:
    stream_id: str
    source: str
    source_type: SourceType
    destinations: list[str]  # Full RTMP URLs (url + key)
    title: str = "Live Stream"
    status: str = "starting"
    process: asyncio.subprocess.Process | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    error: str = ""
    history_id: int | None = None
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

    async def start(self, *, source: str, destinations: list[str], title: str = "Live Stream", source_type_hint: str | None = None) -> StreamSession:
        if self.is_active():
            raise RuntimeError("يوجد بث نشط بالفعل. أوقفه أولًا عبر /stream_stop")
        if not destinations:
            raise ValueError("لم يتم تحديد أي وجهة RTMP.")
        
        source_type = source_type_hint or self._detect_source(source)
        if source_type in ["file", "audio_file"] and not Path(source).exists():
            raise FileNotFoundError(f"الملف غير موجود: {source}")
            
        stream_id = f"stream_{int(time.time())}"
        session = StreamSession(
            stream_id=stream_id, 
            source=source, 
            source_type=source_type, 
            destinations=destinations, 
            title=title
        )
        self.active = session
        # The actual process start is handled in a task to allow for retries/transcoding
        asyncio.create_task(self._run_workflow(session))
        return session

    async def stop(self) -> bool:
        if not self.active:
            return False
        await self.active.stop(timeout=self.settings.stream_graceful_stop_seconds)
        self.active = None
        return True

    def _detect_source(self, source: str) -> SourceType:
        lower = source.lower().strip()
        if any(x in lower for x in ["youtube.com/", "youtu.be/", "music.youtube.com/"]):
            return "youtube"
        if lower.startswith("http://") or lower.startswith("https://"):
            return "direct"
        
        audio_exts = {'.mp3', '.m4a', '.aac', '.wav', '.ogg', '.flac', '.opus'}
        if Path(source).suffix.lower() in audio_exts:
            return "audio_file"
        return "file"

    async def _resolve_youtube(self, source: str) -> str:
        ytdlp = self.settings.ytdlp_path or "yt-dlp"
        cookies = os.getenv("YTDLP_COOKIES_PATH", "")
        js_runtime = os.getenv("YTDLP_JS_RUNTIME", "deno")
        
        cmd = [ytdlp, "-f", "best[protocol^=http][ext=mp4]/best[protocol^=http]/best", "--no-playlist", "-g", "--force-ipv4", "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"]
        if cookies and Path(cookies).exists():
            cmd += ["--cookies", cookies]
        if js_runtime:
            cmd += ["--javascript-filter", js_runtime]
            
        cmd.append(source)
        
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            raise RuntimeError("فشل yt-dlp: " + err.decode(errors="ignore")[-500:])
        
        direct = out.decode(errors="ignore").strip().splitlines()[0] if out else ""
        if not direct.startswith("http"):
            raise RuntimeError("لم يتم العثور على رابط مباشر.")
        return direct

    def _build_ffmpeg_args(self, input_url: str, destinations: list[str], source_type: SourceType, force_reencode: bool = False) -> list[str]:
        ffmpeg = self.settings.ffmpeg_path or "ffmpeg"
        args = [ffmpeg, "-hide_banner", "-loglevel", "warning"]
        
        # Input handling
        if source_type == "audio_file":
            cover = os.getenv("STREAM_AUDIO_COVER_IMAGE")
            if cover and Path(cover).exists():
                args += ["-loop", "1", "-i", cover]
            else:
                args += ["-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30"]
            args += ["-i", input_url, "-shortest"]
        else:
            if not input_url.startswith("http"):
                args += ["-re"]
            else:
                args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_at_eof", "1", "-reconnect_delay_max", "5"]
            args += ["-i", input_url]

        # Output mapping for multiple destinations
        # We use tee pseudo-muxer for multiple RTMP outputs
        tee_outputs = "|".join([f"[f=flv]{d}" for d in destinations])
        
        # Video encoding
        if source_type == "audio_file":
            args += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p"]
        elif force_reencode:
            args += ["-c:v", "libx264", "-preset", self.settings.stream_fallback_preset, "-tune", "zerolatency", "-pix_fmt", "yuv420p"]
        else:
            args += ["-c:v", "copy"]

        # Audio encoding
        args += ["-c:a", "aac", "-b:a", self.settings.stream_audio_bitrate, "-ar", "44100", "-ac", "2"]
        
        # General output settings
        args += [
            "-b:v", self.settings.stream_video_bitrate,
            "-maxrate", self.settings.stream_video_bitrate,
            "-bufsize", self.settings.stream_buffer_size,
            "-r", str(self.settings.stream_fps),
            "-g", str(self.settings.stream_gop),
            "-f", "tee", "-map", "0:v:0", "-map", "1:a:0" if source_type == "audio_file" else "0:a:0?",
            tee_outputs
        ]
        
        return args

    async def _run_workflow(self, session: StreamSession) -> None:
        try:
            input_url = session.source
            if session.source_type == "youtube":
                await session.emit("log", {"message": "⏳ جاري استخراج رابط YouTube..."})
                input_url = await self._resolve_youtube(session.source)
            
            await session.emit("start", {"stream_id": session.stream_id})
            
            # Attempt 1: Fast (copy if possible)
            code = await self._execute_ffmpeg(session, input_url, force_reencode=False)
            
            # Attempt 2: Fallback to re-encode if failed and not stopped by user
            if code != 0 and not session._stop_requested and session.source_type != "audio_file":
                await session.emit("log", {"message": "⚠️ فشل البث السريع، جاري إعادة المحاولة مع إعادة الترميز (Transcoding)..."})
                code = await self._execute_ffmpeg(session, input_url, force_reencode=True)
            
            if session._stop_requested:
                return
                
            if code == 0:
                session.status = "completed"
                await session.emit("end", {"stream_id": session.stream_id, "status": "success"})
            else:
                session.status = "failed"
                session.error = f"FFmpeg exited with code {code}"
                await session.emit("error", {"error": session.error})
                
        except Exception as e:
            session.status = "failed"
            session.error = str(e)
            await session.emit("error", {"error": str(e)})
        finally:
            session.ended_at = time.time()

    async def _execute_ffmpeg(self, session: StreamSession, input_url: str, force_reencode: bool) -> int:
        args = self._build_ffmpeg_args(input_url, session.destinations, session.source_type, force_reencode)
        
        proc = await asyncio.create_subprocess_exec(
            *args, 
            stdout=asyncio.subprocess.PIPE, 
            stderr=asyncio.subprocess.PIPE
        )
        session.process = proc
        session.status = "active"
        await session.emit("active", {"pid": proc.pid})

        # Monitor stderr for logs
        async def log_monitor():
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line: break
                msg = line.decode(errors="ignore").strip()
                if msg: await session.emit("log", {"message": msg})

        monitor_task = asyncio.create_task(log_monitor())
        code = await proc.wait()
        monitor_task.cancel()
        return int(code or 0)


streaming_manager = StreamingManager()
