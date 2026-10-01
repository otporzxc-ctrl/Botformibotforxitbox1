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

SEMAPHORE = asyncio.Semaphore(1)

# Длина рекламного баннера.
# Если banner.mp4 имеет другую длину — она определяется автоматически.
BANNER_DUR = None

# Качество итогового видео.
# 18 = высокое качество, значительно лучше старого CRF 28.
CRF = "18"

# Исходный FPS сохраняется автоматически.
# Эта переменная используется только для баннера.
BANNER_FPS = 30


def run(cmd, timeout=7200):
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        return (
            result.returncode == 0,
            result.stderr[-3000:]
        )

    except subprocess.TimeoutExpired:
        return False, "FFmpeg превысил время обработки"


def probe(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            path
        ],
        capture_output=True,
        text=True
    )

    try:
        data = json.loads(result.stdout)
    except Exception:
        return None

    video = None
    audio = None

    for stream in data.get("streams", []):

        if stream.get("codec_type") == "video" and video is None:
            video = stream

        elif stream.get("codec_type") == "audio" and audio is None:
            audio = stream

    if not video:
        return None

    duration = float(
        video.get("duration")
        or data.get("format", {}).get("duration")
        or 0
    )

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)

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
        "width": width,
        "height": height,
        "fps": fps,
        "audio": audio is not None,
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name") if audio else None,
    }


def get_banner_duration():
    global BANNER_DUR

    if BANNER_DUR is not None:
        return BANNER_DUR

    data = probe(BANNER)

    if not data:
        raise RuntimeError("Не удалось прочитать banner.mp4")

    BANNER_DUR = data["duration"]

    if BANNER_DUR <= 0:
        raise RuntimeError("У banner.mp4 неправильная длительность")

    return BANNER_DUR


def get_banner_positions(duration, banner_duration):
    """
    Логика строго по твоей схеме.

    Видео <= 60 сек:
        баннер в середине.

    Видео > 60 сек:
        00:20
        01:20
        02:20
        03:20
        ...

    Например 15 минут:
        00:20 ... 14:20
    """

    if duration <= 60:
        position = duration / 2

        # Если баннер физически не помещается после середины,
        # ставим его так, чтобы он полностью закончился.
        if position + banner_duration > duration:
            position = max(0, duration - banner_duration)

        return [round(position, 3)]

    positions = []

    position = 20.0

    while position < duration:

        # Баннер должен полностью проиграться.
        if position + banner_duration > duration:
            break

        positions.append(round(position, 3))

        position += 60.0

    return positions


def build_filter(width, height, banner_width, banner_height):
    """
    Баннер масштабируется относительно исходного видео.

    Он сохраняет пропорции и помещается по центру.
    """

    x = max(0, (width - banner_width) // 2)
    y = max(0, (height - banner_height) // 2)

    return (
        f"[1:v]"
        f"scale={banner_width}:{banner_height}:"
        f"force_original_aspect_ratio=decrease,"
        f"pad={banner_width}:{banner_height}:"
        f"(ow-iw)/2:(oh-ih)/2:color=black@0,"
        f"fps={BANNER_FPS},"
        f"trim=duration={get_banner_duration():.3f},"
        f"setpts=PTS-STARTPTS,"
        f"chromakey=0x00FF00:0.30:0.05[banner];"

        f"[0:v]"
        f"trim=start=0:"
        f"end={get_banner_duration():.3f},"
        f"setpts=PTS-STARTPTS[base];"

        f"[base][banner]"
        f"overlay={x}:{y}:"
        f"shortest=1,"
        f"format=yuv420p[v]"
    )


def make_segment_with_banner(
    source,
    banner,
    start,
    end,
    output,
    width,
    height,
    fps,
    has_audio
):
    """
    Берёт кусок исходного видео и проигрывает banner.mp4
    поверх него.

    ЗВУК BANNER НЕ ИСПОЛЬЗУЕТСЯ.

    Оригинальный звук этого участка сохраняется.
    """

    duration = end - start
    banner_duration = get_banner_duration()

    # Размер баннера примерно 55% ширины исходника.
    banner_width = max(
        320,
        int(width * 0.55)
    )

    banner_height = max(
        180,
        int(height * 0.30)
    )

    x = (width - banner_width) // 2
    y = (height - banner_height) // 2

    filter_complex = (
        f"[1:v]"
        f"scale={banner_width}:{banner_height}:"
        f"force_original_aspect_ratio=decrease,"
        f"pad={banner_width}:{banner_height}:"
        f"(ow-iw)/2:(oh-ih)/2:color=black@0,"
        f"fps={BANNER_FPS},"
        f"trim=duration={banner_duration:.3f},"
        f"setpts=PTS-STARTPTS,"
        f"chromakey=0x00FF00:0.30:0.05[ban];"

        f"[0:v]"
        f"trim=duration={duration:.3f},"
        f"setpts=PTS-STARTPTS[base];"

        f"[base][ban]"
        f"overlay={x}:{y}:shortest=1,"
        f"format=yuv420p[v]"
    )

    cmd = [
        "ffmpeg",
        "-y",

        "-ss",
        f"{start:.3f}",

        "-t",
        f"{duration:.3f}",

        "-i",
        source,

        "-stream_loop",
        "-1",

        "-i",
        banner,

        "-filter_complex",
        filter_complex,

        "-map",
        "[v]",
    ]

    # ВАЖНО:
    # берём аудио ТОЛЬКО от исходного видео.
    # Аудио banner.mp4 вообще не подключается.
    if has_audio:
        cmd += [
            "-map",
            "0:a:0?",
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
        cmd += [
            "-an"
        ]

    cmd += [
        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        CRF,

        "-r",
        f"{fps:.3f}",

        "-pix_fmt",
        "yuv420p",

        "-movflags",
        "+faststart",

        "-avoid_negative_ts",
        "make_zero",

        "-t",
        f"{duration:.3f}",

        output
    ]

    return run(cmd)


def make_normal_segment(
    source,
    start,
    end,
    output,
    fps,
    has_audio
):
    """
    Обычный кусок видео без баннера.

    Качество максимально близкое к исходнику.
    """

    duration = end - start

    cmd = [
        "ffmpeg",
        "-y",

        "-ss",
        f"{start:.3f}",

        "-i",
        source,

        "-t",
        f"{duration:.3f}",

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        CRF,

        "-r",
        f"{fps:.3f}",

        "-pix_fmt",
        "yuv420p",
    ]

    if has_audio:
        cmd += [
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
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
        cmd += [
            "-map",
            "0:v:0",
            "-an"
        ]

    cmd += [
        "-movflags",
        "+faststart",

        "-avoid_negative_ts",
        "make_zero",

        output
    ]

    return run(cmd)


def concat_segments(files, output):
    """
    Финальная склейка.
    """

    list_file = output + ".txt"

    with open(list_file, "w", encoding="utf-8") as f:

        for file in files:

            path = os.path.abspath(file)

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
    """
    Основная обработка.
    """

    with tempfile.TemporaryDirectory() as tmp:

        source_info = probe(source)

        if not source_info:
            return False, "Не удалось прочитать исходное видео"

        duration = source_info["duration"]
        width = source_info["width"]
        height = source_info["height"]
        fps = source_info["fps"]
        has_audio = source_info["audio"]

        if duration < 3:
            return False, "Видео слишком короткое"

        if width <= 0 or height <= 0:
            return False, "Неверное разрешение видео"

        banner_duration = get_banner_duration()

        positions = get_banner_positions(
            duration,
            banner_duration
        )

        if not positions:
            return False, "Для этого видео баннер не помещается"

        parts = []

        current = 0.0

        for index, position in enumerate(positions):

            # Кусок ДО баннера.
            if position > current + 0.01:

                normal = os.path.join(
                    tmp,
                    f"normal_{index}.mp4"
                )

                ok, error = make_normal_segment(
                    source,
                    current,
                    position,
                    normal,
                    fps,
                    has_audio
                )

                if not ok:
                    return False, (
                        f"Ошибка сегмента {index + 1}: "
                        f"{error}"
                    )

                parts.append(normal)

            # Баннер.
            banner_part = os.path.join(
                tmp,
                f"banner_{index}.mp4"
            )

            banner_end = min(
                duration,
                position + banner_duration
            )

            ok, error = make_segment_with_banner(
                source,
                BANNER,
                position,
                banner_end,
                banner_part,
                width,
                height,
                fps,
                has_audio
            )

            if not ok:
                return False, (
                    f"Ошибка баннера {index + 1}: "
                    f"{error}"
                )

            parts.append(banner_part)

            current = banner_end

        # Последний кусок после последнего баннера.
        if current < duration - 0.01:

            tail = os.path.join(
                tmp,
                "tail.mp4"
            )

            ok, error = make_normal_segment(
                source,
                current,
                duration,
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

        if not parts:
            return False, "Не создано ни одного сегмента"

        ok, error = concat_segments(
            parts,
            output
        )

        if not ok:
            return False, (
                "Ошибка финальной склейки: "
                f"{error}"
            )

        if not os.path.exists(output):
            return False, "Итоговый файл не создан"

        if os.path.getsize(output) == 0:
            return False, "Итоговый файл пустой"

        result_info = probe(output)

        if result_info:

            expected = (
                duration +
                len(positions) * banner_duration
            )

            actual = result_info["duration"]

            # Допустимая погрешность из-за кодирования.
            tolerance = max(
                2.0,
                len(positions) * 0.15
            )

            if abs(actual - expected) > tolerance:

                return False, (
                    f"Неверная длительность. "
                    f"Ожидалось примерно {expected:.2f} сек, "
                    f"получилось {actual:.2f} сек."
                )

        return True, (
            f"Готово. "
            f"Баннеров вставлено: {len(positions)}"
        )


async def cmd_start(
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
        "Ограничения 50 MB и 2 минуты убраны."
    )


async def cmd_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        f"Твой ID: `{update.effective_user.id}`",
        parse_mode="Markdown"
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

    async with SEMAPHORE:

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
                    f"❌ Ошибка скачивания:\n{error}"
                )

                return

            await status.edit_text(
                "🎬 Обрабатываю..."
            )

            loop = asyncio.get_running_loop()

            try:

                success, result = (
                    await loop.run_in_executor(
                        None,
                        process_video,
                        input_file,
                        output_file
                    )
                )

            except Exception as error:

                await status.edit_text(
                    f"❌ Ошибка обработки:\n{error}"
                )

                return

            if not success:

                await status.edit_text(
                    f"❌ {result}"
                )

                return

            if (
                not os.path.exists(output_file)
                or os.path.getsize(output_file) == 0
            ):

                await status.edit_text(
                    "❌ Итоговый файл не создан"
                )

                return

            await status.edit_text(
                "📤 Отправляю..."
            )

            try:

                with open(
                    output_file,
                    "rb"
                ) as video_file:

                    await message.reply_video(
                        video=video_file,
                        caption="✅ Готово!",
                        supports_streaming=True
                    )

                await status.delete()

            except Exception as error:

                await status.edit_text(
                    f"❌ Ошибка отправки:\n{error}"
                )


def main():

    application = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            cmd_start
        )
    )

    application.add_handler(
        CommandHandler(
            "id",
            cmd_id
        )
    )

    application.add_handler(
        MessageHandler(
            filters.VIDEO
            | filters.Document.VIDEO,
            handle_video
        )
    )

    print("✅ Bot started")

    application.run_polling()


if __name__ == "__main__":
    main()