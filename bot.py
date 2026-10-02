import os
import json
import asyncio
import tempfile
import subprocess

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# ВЕРСИЯ — меняй при каждом обновлении, она видна в /start
# ============================================================

VERSION = "2.3"
VERSION_DATE = "01.10.2026"
VERSION_NOTES = (
    "• баннер идёт со своим звуком, без хромакея\n"
    "• ролик стоит на стоп-кадре, пока играет баннер\n"
    "• после баннера ролик продолжается с того же места\n"
    "• баннеры на 1:20, 2:20, 3:20 и так далее\n"
    "• прозрачность баннера 10%"
)

# ============================================================
# НАСТРОЙКИ
# ============================================================

TOKEN = os.environ["BOT_TOKEN"]
BANNER = "/app/banner.mp4"

ALLOWED_USERS = {
    int(x)
    for x in os.environ.get("ALLOWED_USERS", "").split(",")
    if x.strip()
}

# Прозрачность баннера в процентах (0 = совсем непрозрачный).
# По рекомендации поста — 10. Хочешь меньше/больше — меняй число.
BANNER_TRANSPARENCY = 10

# Для видео длиннее минуты баннеры ставятся на 1:20, 2:20, 3:20 ...
# Хочешь ещё и на 0:20 — поставь FIRST_BANNER_AT = 20.0
FIRST_BANNER_AT = 80.0
BANNER_STEP = 60.0

CLOUD_DOWNLOAD_LIMIT = 20 * 1024 * 1024
CLOUD_UPLOAD_LIMIT = 50 * 1024 * 1024

LOCK = asyncio.Semaphore(1)

CRF = "18"
PRESET = "veryfast"

# libx264 + yuv420p требуют чётные размеры
EVEN = "scale=trunc(iw/2)*2:trunc(ih/2)*2"


# ============================================================
# FFMPEG / FFPROBE
# ============================================================

def run(cmd, timeout=7200):
    if cmd and cmd[0] == "ffmpeg":
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-nostats",
            "-nostdin",
        ] + cmd[1:]

    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return False, "FFmpeg timeout"

    if p.returncode == 0:
        return True, ""

    if p.returncode < 0:
        return False, (
            f"ffmpeg убит системой (сигнал {-p.returncode}) — "
            f"скорее всего не хватило памяти на сервере"
        )

    return False, p.stderr.strip()[-1500:]


def probe(path):
    p = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            path
        ],
        capture_output=True,
        text=True
    )

    try:
        data = json.loads(p.stdout)
    except Exception:
        return None

    streams = data.get("streams", [])

    video = next(
        (x for x in streams if x.get("codec_type") == "video"),
        None
    )
    audio = next(
        (x for x in streams if x.get("codec_type") == "audio"),
        None
    )

    if not video:
        return None

    try:
        duration = float(
            video.get("duration")
            or data.get("format", {}).get("duration")
            or 0
        )
    except (TypeError, ValueError):
        duration = 0

    if duration <= 0:
        return None

    fps_text = (
        video.get("avg_frame_rate")
        or video.get("r_frame_rate")
        or "30/1"
    )

    try:
        a, b = fps_text.split("/")
        fps = float(a) / float(b)
        if fps <= 0 or fps > 240:
            fps = 30.0
    except Exception:
        fps = 30.0

    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": fps,
        "audio": audio is not None,
    }


def image_size(path):
    p = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            path
        ],
        capture_output=True,
        text=True
    )

    try:
        s = json.loads(p.stdout)["streams"][0]
        return int(s["width"]), int(s["height"])
    except Exception:
        return None


def get_positions(duration):
    """
    До 60 секунд включительно:
        один баннер в середине.

    Больше 60 секунд:
        1:20, 2:20, 3:20 ... хоть до часа и дальше
        (на 20-й секунде каждой минуты).

    Если видео чуть длиннее минуты и до 1:20 оно
    не дотягивает — баннер ставится в середину.

    Ролик на время баннера стоит, поэтому баннеру
    не нужно «помещаться» в оставшееся время.
    """
    if duration <= 60:
        return [round(duration / 2, 3)]

    result = []
    position = FIRST_BANNER_AT

    while position < duration - 1.0:
        result.append(round(position, 3))
        position += BANNER_STEP

    if not result:
        result = [round(duration / 2, 3)]

    return result


def make_source_segment(
    source,
    start,
    duration,
    output,
    fps,
    has_audio
):
    """
    Обычный кусок исходного видео.
    Звук всегда есть (если у исходника его нет —
    добавляется тишина), чтобы все куски были
    одинаковыми и склеивались без рассинхрона.
    """
    command = [
        "ffmpeg", "-y",
        "-threads", "4",
        "-ss", f"{start:.3f}",
        "-i", source,
    ]

    if not has_audio:
        command += [
            "-f", "lavfi",
            "-i", f"anullsrc=r=48000:cl=stereo:d={duration + 1:.3f}",
        ]

    command += [
        "-map", "0:v:0",
        "-map", "0:a:0" if has_audio else "1:a:0",
        "-vf", EVEN,
        "-af", f"aresample=48000,apad=whole_dur={duration:.3f}",
        "-t", f"{duration:.3f}",
        "-threads", "4",
        "-c:v", "libx264",
        "-preset", PRESET,
        "-crf", CRF,
        "-r", f"{fps:.6f}",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-avoid_negative_ts", "make_zero",
        output
    ]

    return run(command)


def make_banner_segment(
    output,
    source,
    position,
    banner_duration,
    banner_has_audio,
    fps
):
    """
    Сегмент баннера:
      - картинка = стоп-кадр ролика в точке position;
      - поверх него банер целиком, СО СВОИМ ЗВУКОМ;
      - без хромакея.

    После этого сегмента ролик продолжается
    с той же точки position.
    """
    frame = output + ".frame.mkv"

    # Стоп-кадр без потери цвета (без перегонки в RGB).
    ok, error = run([
        "ffmpeg", "-y",
        "-threads", "4",
        "-ss", f"{position:.3f}",
        "-i", source,
        "-map", "0:v:0",
        "-frames:v", "1",
        "-vf", EVEN,
        "-c:v", "ffv1",
        frame
    ])

    if not ok or not os.path.exists(frame):
        return False, f"Не удалось взять стоп-кадр: {error}"

    size = image_size(frame)

    if not size:
        return False, "Не удалось определить размер кадра"

    width, height = size
    d = f"{banner_duration:.6f}"

    command = [
        "ffmpeg", "-y",
        "-i", frame,
        "-i", BANNER,
    ]

    if not banner_has_audio:
        command += [
            "-f", "lavfi",
            "-i", f"anullsrc=r=48000:cl=stereo:d={banner_duration + 1:.3f}",
        ]

    transparency = min(max(BANNER_TRANSPARENCY, 0), 100)
    alpha = ""

    if transparency > 0:
        opacity = 1 - transparency / 100
        alpha = f",format=rgba,colorchannelmixer=aa={opacity:.3f}"

    filter_complex = (
        f"[0:v]tpad=stop_mode=clone:stop_duration={d},"
        f"fps={fps:.6f},setsar=1[base];"
        f"[1:v]scale={width}:{height}:"
        f"force_original_aspect_ratio=decrease,"
        f"setsar=1{alpha}[ban];"
        f"[base][ban]overlay="
        f"(W-w)/2:(H-h)/2:eof_action=pass:format=auto,"
        f"format=yuv420p[v]"
    )

    command += [
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "1:a:0" if banner_has_audio else "2:a:0",
        "-af", f"aresample=48000,apad=whole_dur={d}",
        "-t", d,
        "-threads", "4",
        "-c:v", "libx264",
        "-preset", PRESET,
        "-crf", CRF,
        "-r", f"{fps:.6f}",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-avoid_negative_ts", "make_zero",
        output
    ]

    result = run(command)

    try:
        os.remove(frame)
    except OSError:
        pass

    return result


def concat_files(files, output):
    list_file = output + ".txt"

    with open(list_file, "w", encoding="utf-8") as f:
        for path in files:
            path = os.path.abspath(path)
            path = path.replace("\\", "/")
            path = path.replace("'", "'\\''")
            f.write(f"file '{path}'\n")

    ok, error = run([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_file,
        "-c", "copy",
        "-movflags", "+faststart",
        output
    ])

    try:
        os.remove(list_file)
    except OSError:
        pass

    return ok, error


def process_video(source, output):
    with tempfile.TemporaryDirectory() as tmp:
        source_info = probe(source)
        banner_info = probe(BANNER)

        if not source_info:
            return False, "Не удалось прочитать исходное видео"

        if not banner_info:
            return False, "Не удалось прочитать banner.mp4"

        duration = source_info["duration"]
        fps = source_info["fps"]
        has_audio = source_info["audio"]

        banner_duration = banner_info["duration"]
        banner_has_audio = banner_info["audio"]

        if duration < 3:
            return False, "Видео слишком короткое"

        positions = get_positions(duration)

        parts = []
        current = 0.0

        for index, position in enumerate(positions):

            # ---- ролик до баннера ----
            if position > current + 0.01:
                segment = os.path.join(tmp, f"source_{index}.mp4")

                ok, error = make_source_segment(
                    source,
                    current,
                    position - current,
                    segment,
                    fps,
                    has_audio
                )

                if not ok:
                    return False, (
                        f"Ошибка исходного сегмента "
                        f"{index + 1}: {error}"
                    )

                parts.append(segment)

            # ---- баннер на стоп-кадре, со звуком ----
            banner_segment = os.path.join(tmp, f"banner_{index}.mp4")

            ok, error = make_banner_segment(
                banner_segment,
                source,
                position,
                banner_duration,
                banner_has_audio,
                fps
            )

            if not ok:
                return False, (
                    f"Ошибка баннера {index + 1}: {error}"
                )

            parts.append(banner_segment)

            # Ролик продолжится с той же точки.
            current = position

        # ---- остаток ролика ----
        if current < duration - 0.01:
            tail = os.path.join(tmp, "tail.mp4")

            ok, error = make_source_segment(
                source,
                current,
                duration - current,
                tail,
                fps,
                has_audio
            )

            if not ok:
                return False, f"Ошибка последнего сегмента: {error}"

            parts.append(tail)

        # ---- склейка ----
        ok, error = concat_files(parts, output)

        if not ok:
            return False, f"Ошибка финальной склейки: {error}"

        # Освобождаем место на диске.
        for path in parts:
            try:
                os.remove(path)
            except OSError:
                pass

        if not os.path.exists(output):
            return False, "Итоговый файл не создан"

        if os.path.getsize(output) == 0:
            return False, "Итоговый файл пустой"

        result_info = probe(output)

        if result_info:
            expected = duration + len(positions) * banner_duration
            actual = result_info["duration"]
            tolerance = max(1.5, len(positions) * 0.25)

            if abs(actual - expected) > tolerance:
                return False, (
                    f"Неверная длительность.\n"
                    f"Ожидалось примерно {expected:.2f} сек.\n"
                    f"Получилось {actual:.2f} сек."
                )

        return True, f"Готово. Баннеров: {len(positions)}"


# ============================================================
# ХЕНДЛЕРЫ
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    uid = update.effective_user.id

    if ALLOWED_USERS and uid not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Нет доступа")
        return

    await update.message.reply_text(
        "👋 Скидывай видео.\n\n"
        "До 1 минуты — баннер в середине.\n"
        "Дольше минуты — на 1:20, 2:20, 3:20 и так далее.\n\n"
        "Баннер идёт полностью и со своим звуком, "
        "пока ролик стоит на стоп-кадре. "
        "Потом ролик продолжается с того же места.\n\n"
        f"🔖 Версия: v{VERSION} от {VERSION_DATE}\n"
        f"{VERSION_NOTES}"
    )


async def get_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        f"Твой ID: {update.effective_user.id}"
    )


async def set_status(status, text):
    """Меняет текст статуса; игнорирует «Message is not modified»."""
    try:
        await status.edit_text(text)
    except Exception:
        pass


async def handle_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.message
    uid = update.effective_user.id

    if ALLOWED_USERS and uid not in ALLOWED_USERS:
        await message.reply_text("⛔ Нет доступа")
        return

    media = message.video or message.document

    if not media:
        return

    size = getattr(media, "file_size", None) or 0

    if size > CLOUD_DOWNLOAD_LIMIT:
        await message.reply_text(
            "❌ Файл больше 20 МБ — Telegram не отдаёт "
            "ботам файлы крупнее."
        )
        return

    status = await message.reply_text(
        "⏳ В очереди..." if LOCK.locked()
        else "⏳ Скачиваю оригинал..."
    )

    async with LOCK:
        with tempfile.TemporaryDirectory() as tmp:
            input_file = os.path.join(tmp, "input.mp4")
            output_file = os.path.join(tmp, "output.mp4")

            try:
                await set_status(status, "⏳ Скачиваю оригинал...")

                telegram_file = await context.bot.get_file(
                    media.file_id
                )

                await telegram_file.download_to_drive(input_file)

            except Exception as error:
                await set_status(status, 
                    f"❌ Ошибка скачивания:\n{error}"
                )
                return

            try:
                await set_status(status, "🎬 Обрабатываю...")

                loop = asyncio.get_running_loop()

                ok, result = await loop.run_in_executor(
                    None,
                    process_video,
                    input_file,
                    output_file
                )

            except Exception as error:
                await set_status(status, 
                    f"❌ Ошибка обработки:\n{error}"
                )
                return

            if not ok:
                await set_status(status, f"❌ {result}")
                return

            if os.path.getsize(output_file) > CLOUD_UPLOAD_LIMIT:
                await set_status(status, 
                    "❌ Готовое видео получилось больше 50 МБ — "
                    "Telegram не даёт ботам отправлять файлы крупнее."
                )
                return

            await set_status(status, "📤 Отправляю...")

            info = probe(output_file) or {}

            try:
                with open(output_file, "rb") as video:
                    await message.reply_video(
                        video=video,
                        caption=f"✅ Готово! (v{VERSION})",
                        supports_streaming=True,
                        duration=int(info.get("duration") or 0) or None,
                        width=info.get("width") or None,
                        height=info.get("height") or None,
                        read_timeout=600,
                        write_timeout=600,
                        connect_timeout=60,
                        pool_timeout=60,
                    )

                await status.delete()

            except Exception as error:
                await set_status(status, 
                    f"❌ Ошибка отправки:\n{error}"
                )


def main():
    print(f"🔖 MusorDrop bot v{VERSION} ({VERSION_DATE})", flush=True)

    app = (
        Application
        .builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler(["start", "version"], start))
    app.add_handler(CommandHandler("id", get_id))
    app.add_handler(
        MessageHandler(
            filters.VIDEO | filters.Document.VIDEO,
            handle_video
        )
    )

    print("✅ Bot started", flush=True)

    app.run_polling()


if __name__ == "__main__":
    main()
