# تحديثات البث المباشر

## الميزات المضافة

- قسم `🎥 بث مباشر` داخل نفس البوت الحالي، بدون توكن جديد.
- بث YouTube عبر `yt-dlp` مع دعم:
  - `YTDLP_COOKIES_PATH`
  - `YTDLP_JS_RUNTIME=deno`
  - `--force-ipv4`
  - User-Agent مناسب.
- بث فيديو محلي:
  - `/stream_start /app/media/video.mp4`
- بث صوت محلي كفيديو:
  - `/stream_audio /app/media/audio.mp3`
  - يستخدم صورة `STREAM_AUDIO_COVER_IMAGE` إن وجدت.
  - وإلا يولد شاشة سوداء 1280x720.
- تسجيل قنوات تليجرام يدويًا لأن Bot API لا يوفر قائمة تلقائية بكل القنوات التي البوت مشرف فيها.
- اختيار قنوات تليجرام أو وجهات RTMP من أزرار Inline قبل بدء البث.
- تشفير مفاتيح RTMP باستخدام `ENCRYPTION_KEY` الحالي.
- إيقاف FFmpeg بأمان: SIGTERM ثم SIGKILL بعد المهلة.

## أوامر البوت

```text
/stream
/stream_start SOURCE
/stream_audio AUDIO_PATH
/stream_status
/stream_stop

/stream_add_channel @CHANNEL_USERNAME
/stream_channels
/stream_set_channel_rtmp @CHANNEL_USERNAME RTMP_URL STREAM_KEY
/stream_remove_channel @CHANNEL_USERNAME

/stream_platform NAME TYPE RTMP_URL STREAM_KEY
/stream_platforms
```

## مثال إضافة قناة تليجرام

```text
/stream_add_channel @M_A_De
/stream_set_channel_rtmp @M_A_De rtmps://dc4-1.rtmp.t.me/s STREAM_KEY
```

ثم:

```text
/stream_start https://youtube.com/watch?v=VIDEO_ID
```

أو:

```text
/stream_audio /app/media/audio.mp3
```

بعدها اختر القناة من الأزرار واضغط `🚀 بدء البث الآن`.

## متغيرات Railway/VPS الجديدة

```env
FFMPEG_PATH=ffmpeg
YTDLP_PATH=yt-dlp
YTDLP_JS_RUNTIME=deno
YTDLP_COOKIES_PATH=
STREAM_AUDIO_COVER_IMAGE=
STREAM_AUDIO_CANVAS_SIZE=1280x720
STREAM_AUDIO_CANVAS_BITRATE=1500k
STREAM_AUDIO_CANVAS_BUFFER_SIZE=3000k
STREAM_VIDEO_BITRATE=4500k
STREAM_AUDIO_BITRATE=160k
STREAM_BUFFER_SIZE=9000k
STREAM_FPS=30
STREAM_GOP=60
STREAM_FALLBACK_PRESET=veryfast
STREAM_GRACEFUL_STOP_SECONDS=8
```

## أوامر النشر على VPS

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y ffmpeg python3 python3-pip curl ca-certificates git
python3 -m pip install -U yt-dlp --break-system-packages
curl -fsSL https://deno.land/install.sh | sh
echo 'export PATH="$HOME/.deno/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

cd /path/to/moataz-repo-agent-pro
python3 -m pip install -r requirements.txt
bash scripts/start.sh
```

## أوامر تحديث Railway/GitHub

```bash
git add .
git commit -m "Add integrated multistream channels and audio/video streaming"
git push
```

ثم في Railway أضف المتغيرات الجديدة وأعد النشر.

## ملاحظة مهمة عن YouTube

إذا ظهر الخطأ `Sign in to confirm you’re not a bot` أو `HTTP 429`، استخدم cookies:

```env
YTDLP_COOKIES_PATH=/app/data/youtube-cookies.txt
YTDLP_JS_RUNTIME=deno
```

يفضل تشغيل هذه الميزة على VPS خاص بدل Railway للبث المستقر، لأن YouTube يقيّد IPs المشتركة كثيرًا.
