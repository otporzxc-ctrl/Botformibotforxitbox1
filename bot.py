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

TOKEN = os.environ["BOT_TOKEN"]
BANNER = "/app/banner.mp4"

ALLOWED_USERS = (
    set(map(int, os.environ["ALLOWED_USERS"].split(",")))
    if os.environ.get("ALLOWED_USERS")
    else set()
)

LOCK = asyncio.Semaphore(1)

CRF = "18"
PRESET = "veryfast"


def run(cmd, timeout=7200):
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return p.returncode == 0, p.stderr[-4000:]
    except subprocess.TimeoutExpired:
        return False, "FFmpeg timeout"


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

    video = next(
        (x for x in data.get("streams", [])
         if x.get("codec_type") == "video"),
        None
    )

    audio = next(
        (x for x in data.get("streams", [])
         if x.get("codec_type") == "audio"),
        None
    )

    if not video:
        return None

    duration = float(
        video.get("duration")
        or data.get("format", {}).get("duration")
        or 0
    )

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


def get_banner_duration():
    info = probe(BANNER)

    if not info:
        raise RuntimeError(
            "Не удалось прочитать /app/banner.mp4"
        )

    return info["duration"]


def get_positions(duration, banner_duration):
    """
    До 60 секунд:
        баннер в середине.

    Больше 60 секунд:
        00:20
        01:20
        02:20
        03:20
        ...

    Баннер всегда должен проиграться полностью.
    """

    if duration <= 60:
        return [round(duration / 2, 3)]

    result = []

    position = 20.0

    while position < duration:

        if position + banner_duration > duration:
            break

        result.append(round(position, 3))

        position += 60.0

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
    """

    command = [
        "ffmpeg",
        "-y",

        "-ss",
        f"{start:.3f}",

        "-i",
        source,

        "-t",
        f"{duration:.3f}",

        "-map",
        "0:v:0",
    ]

    if has_audio:
        command += [
            "-map",
            "0:a:0?"
        ]

    command += [
        "-c:v",
        "libx264",

        "-preset",
        PRESET,

        "-crf",
        CRF,

        "-r",
        f"{fps:.6f}",

        "-pix_fmt",
        "yuv420p",
    ]

    if has_audio:
        command += [
            "-c:a",
            "aac",

            "-b:a",
            "192k",

            "-ar",
            "48000",

            "-ac",
            "2",
        ]
    else:
        command += [
            "-an"
        ]

    command += [
        "-avoid_negative_ts",
        "make_zero",

        output
    ]

    return run(command)


def make_banner_segment(
    output,
    duration,
    width,
    height,
    fps
):
    """
    Создаёт отдельный сегмент баннера.

    Звук banner.mp4 НЕ используется.

    После этого сегмента исходное видео
    продолжается с той же позиции, где
    был вставлен баннер.
    """

    filter_complex = (
        f"[1:v]"
        f"scale={width}:{height}:"
        f"force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:"
        f"(ow-iw)/2:(oh-ih)/2:"
        f"color=black@0,"
        f"chromakey=0x00FF00:0.30:0.05,"
        f"format=yuva420p[ban];"

        f"[0:v][ban]"
        f"overlay=0:0:shortest=1,"
        f"format=yuv420p[v]"
    )

    command = [
        "ffmpeg",
        "-y",

        "-f",
        "lavfi",

        "-i",
        (
            f"color=c=black:"
            f"s={width}x{height}:"
            f"r={fps:.6f}:"
            f"d={duration:.6f}"
        ),

        "-stream_loop",
        "-1",

        "-i",
        BANNER,

        "-filter_complex",
        filter_complex,

        "-map",
        "[v]",

        # ЗВУК БАННЕРА ПОЛНОСТЬЮ ОТКЛЮЧЕН
        "-an",

        "-t",
        f"{duration:.3f}",

        "-c:v",
        "libx264",

        "-preset",
        PRESET,

        "-crf",
        CRF,

        "-r",
        f"{fps:.6f}",

        "-pix_fmt",
        "yuv420p",

        "-avoid_negative_ts",
        "make_zero",

        output
    ]

    return run(command)


def concat_files(files, output):
    list_file = output + ".txt"

    with open(
        list_file,
        "w",
        encoding="utf-8"
    ) as f:

        for path in files:

            path = os.path.abspath(path)
            path = path.replace("\\", "/")
            path = path.replace("'", "'\\''")

            f.write(
                f"file '{path}'\n"
            )

    ok, error = run(
        [
            "ffmpeg",
            "-y",

            "-f",
            "concat",

            "-safe",
            "0",

            "-i",
            list_file,

            "-c",
            "copy",

            "-movflags",
            "+faststart",

            output
        ]
    )

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
        width = source_info["width"]
        height = source_info["height"]
        fps = source_info["fps"]
        has_audio = source_info["audio"]

        banner_duration = banner_info["duration"]

        if duration < 3:
            return False, "Видео слишком короткое"

        if banner_duration <= 0:
            return False, "banner.mp4 пустой"

        positions = get_positions(
            duration,
            banner_duration
        )

        if not positions:
            return False, "Баннер не помещается полностью"

        parts = []

        current = 0.0

        for index, position in enumerate(positions):

            # -----------------------------
            # ИСХОДНОЕ ВИДЕО ДО БАННЕРА
            # -----------------------------

            if position > current + 0.01:

                segment = os.path.join(
                    tmp,
                    f"source_{index}.mp4"
                )

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

            # -----------------------------
            # ПОЛНЫЙ БАННЕР
            # -----------------------------

            banner_segment = os.path.join(
                tmp,
                f"banner_{index}.mp4"
            )

            ok, error = make_banner_segment(
                banner_segment,
                banner_duration,
                width,
                height,
                fps
            )

            if not ok:
                return False, (
                    f"Ошибка баннера "
                    f"{index + 1}: {error}"
                )

            parts.append(banner_segment)

            # ВАЖНО!
            #
            # НЕ position + banner_duration.
            #
            # Исходное видео должно продолжиться
            # с ТОЙ ЖЕ позиции после вставленного
            # баннера.

            current = position

        # -----------------------------
        # ИСХОДНОЕ ВИДЕО ПОСЛЕ БАННЕРА
        # -----------------------------

        if current < duration - 0.01:

            tail = os.path.join(
                tmp,
                "tail.mp4"
            )

            ok, error = make_source_segment(
                source,
                current,
                duration - current,
                tail,
                fps,
                has_audio
            )

            if not ok:
                return False, (
                    f"Ошибка последнего сегмента: "
                    f"{error}"
                )

            parts.append(tail)

        # -----------------------------
        # СКЛЕЙКА
        # -----------------------------

        ok, error = concat_files(
            parts,
            output
        )

        if not ok:
            return False, (
                f"Ошибка финальной склейки: "
                f"{error}"
            )

        if not os.path.exists(output):
            return False, "Итоговый файл не создан"

        if os.path.getsize(output) == 0:
            return False, "Итоговый файл пустой"

        # Проверяем итоговую длительность.
        result_info = probe(output)

        if result_info:

            expected = (
                duration
                + len(positions) * banner_duration
            )

            actual = result_info["duration"]

            tolerance = max(
                1.0,
                len(positions) * 0.20
            )

            if abs(actual - expected) > tolerance:

                return False, (
                    f"Неверная длительность.\n"
                    f"Ожидалось примерно "
                    f"{expected:.2f} сек.\n"
                    f"Получилось "
                    f"{actual:.2f} сек."
                )

        return True, (
            f"Готово. "
            f"Баннеров: {len(positions)}"
        )


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    uid = update.effective_user.id

    if ALLOWED_USERS and uid not in ALLOWED_USERS:

        await update.message.reply_text(
            "⛔ Нет доступа"
        )

        return

    await update.message.reply_text(
        "👋 Скидывай видео.\n\n"
        "До 1 минуты — баннер в середине.\n"
        "Больше минуты — 00:20, 01:20, "
        "02:20, 03:20...\n\n"
        "Баннер проигрывается полностью.\n"
        "Звук баннера отключён.\n"
        "Исходный звук видео сохраняется.\n\n"
        "Лимита 50 MB и 2 минут нет."
    )


async def get_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        f"Твой ID: {update.effective_user.id}"
    )


async def handle_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.message

    uid = update.effective_user.id

    if ALLOWED_USERS and uid not in ALLOWED_USERS:

        await message.reply_text(
            "⛔ Нет доступа"
        )

        return

    media = (
        message.video
        or message.document
    )

    if not media:
        return

    status = await message.reply_text(
        "⏳ Скачиваю оригинал..."
    )

    async with LOCK:

        with tempfile.TemporaryDirectory() as tmp:

            input_file = os.path.join(
                tmp,
                "input.mp4"
            )

            output_file = os.path.join(
                tmp,
                "output.mp4"
            )

            try:

                telegram_file = (
                    await context.bot.get_file(
                        media.file_id
                    )
                )

                await telegram_file.download_to_drive(
                    input_file
                )

            except Exception as error:

                await status.edit_text(
                    f"❌ Ошибка скачивания:\n"
                    f"{error}"
                )

                return

            await status.edit_text(
                "🎬 Обрабатываю..."
            )

            try:

                loop = asyncio.get_running_loop()

                ok, result = (
                    await loop.run_in_executor(
                        None,
                        process_video,
                        input_file,
                        output_file
                    )
                )

            except Exception as error:

                await status.edit_text(
                    f"❌ Ошибка обработки:\n"
                    f"{error}"
                )

                return

            if not ok:

                await status.edit_text(
                    f"❌ {result}"
                )

                return

            await status.edit_text(
                "📤 Отправляю..."
            )

            try:

                with open(
                    output_file,
                    "rb"
                ) as video:

                    await message.reply_video(
                        video=video,
                        caption="✅ Готово!",
                        supports_streaming=True
                    )

                await status.delete()

            except Exception as error:

                await status.edit_text(
                    f"❌ Ошибка отправки:\n"
                    f"{error}"
                )


def main():

    app = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "id",
            get_id
        )
    )

    app.add_handler(
        MessageHandler(
            filters.VIDEO
            | filters.Document.VIDEO,
            handle_video
        )
    )

    print("✅ Bot started")

    app.run_polling()


if __name__ == "__main__":
    main()