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

ALLOWED_USERS = set(
    map(int, os.environ.get("ALLOWED_USERS", "").split(","))
) if os.environ.get("ALLOWED_USERS") else set()

BANNER_DUR = 4.4
FPS = 30


def run(cmd, timeout=3600):
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return r.returncode == 0, r.stderr


def get_info(path):
    r = subprocess.run(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            path,
        ],
        capture_output=True,
        text=True,
    )

    try:
        data = json.loads(r.stdout)
    except Exception:
        return None

    duration = float(data.get("format", {}).get("duration", 0) or 0)
    width = 1280
    height = 720
    audio = False

    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            width = int(stream.get("width", 1280))
            height = int(stream.get("height", 720))

            if not duration:
                duration = float(stream.get("duration", 0) or 0)

        elif stream.get("codec_type") == "audio":
            audio = True

    if duration <= 0:
        return None

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "audio": audio,
    }


def prepare_video(src, tmp):
    """
    Приводим видео к 576x1024.

    Вертикальное:
        заполняет весь экран.

    Горизонтальное:
        размытый фон + исходное видео по центру.
    """

    out = os.path.join(tmp, "prepared.mp4")

    info = get_info(src)

    if not info:
        return None, "Не удалось прочитать видео"

    w = info["width"]
    h = info["height"]

    if h >= w:

        vf = (
            "scale=576:1024:"
            "force_original_aspect_ratio=increase,"
            "crop=576:1024"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", src,
            "-vf", vf,
            "-r", str(FPS),
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            out,
        ]

    else:

        foreground_h = int(576 * h / w)
        foreground_h = max(1, foreground_h)

        y = max(0, (1024 - foreground_h) // 2)

        filter_complex = (
            "[0:v]split=2[bg][fg];"
            "[bg]"
            "scale=576:1024:"
            "force_original_aspect_ratio=increase,"
            "crop=576:1024,"
            "gblur=sigma=30[blur];"
            f"[fg]scale=576:{foreground_h}[main];"
            f"[blur][main]overlay=0:{y}[v]"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", src,
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-map", "0:a?",
            "-r", str(FPS),
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            out,
        ]

    ok, err = run(cmd)

    if not ok:
        return None, err[-1000:]

    return out, None


def cut_video(src, start, end, out):
    duration = max(0.001, end - start)

    ok, err = run(
        [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", src,
            "-t", f"{duration:.3f}",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-r", str(FPS),
            "-c:a", "aac",
            "-b:a", "128k",
            "-avoid_negative_ts", "make_zero",
            out,
        ]
    )

    return ok, err[-1000:]


def make_freeze_frame(src, position, out):
    """
    Берём кадр непосредственно в момент появления баннера.
    """

    frames = int(BANNER_DUR * FPS) + 2

    ok, err = run(
        [
            "ffmpeg", "-y",
            "-ss", f"{position:.3f}",
            "-i", src,
            "-vf",
            (
                "select='eq(n,0)',"
                f"loop={frames}:1:0,"
                f"trim=duration={BANNER_DUR},"
                "setpts=PTS-STARTPTS"
            ),
            "-r", str(FPS),
            "-an",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            out,
        ]
    )

    return ok, err[-1000:]


def make_banner(freeze, out):
    """
    Накладываем banner.mp4 на стоп-кадр.
    """

    banner_width = 576
    banner_height = 320

    x = 0
    y = (1024 - banner_height) // 2

    filter_complex = (
        "[1:v]"
        f"scale={banner_width}:{banner_height},"
        "trim=duration=4.4,"
        "setpts=PTS-STARTPTS,"
        "chromakey=0x00FF00:0.30:0.05[ban];"
        "[0:v]"
        "trim=duration=4.4,"
        "setpts=PTS-STARTPTS[base];"
        f"[base][ban]overlay={x}:{y}[v]"
    )

    ok, err = run(
        [
            "ffmpeg", "-y",
            "-i", freeze,
            "-i", BANNER,
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-map", "1:a?",
            "-t", str(BANNER_DUR),
            "-r", str(FPS),
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-c:a", "aac",
            "-b:a", "128k",
            out,
        ]
    )

    return ok, err[-1000:]


def concat_videos(files, output):
    list_file = output + ".txt"

    with open(list_file, "w", encoding="utf-8") as f:
        for file in files:
            path = os.path.abspath(file).replace("\\", "/")
            path = path.replace("'", "'\\''")
            f.write(f"file '{path}'\n")

    ok, err = run(
        [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-r", str(FPS),
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            output,
        ]
    )

    try:
        os.remove(list_file)
    except OSError:
        pass

    return ok, err[-1000:]


def banner_positions(duration):
    """
    ВАЖНО:
    
    <= 60 секунд:
        один баннер по центру.

    > 60 секунд:
        00:20
        01:20
        02:20
        03:20
        ...

    То есть 15 минут:
        00:20 ... 14:20
    """

    if duration <= 60:
        return [round(duration / 2, 3)]

    positions = []

    position = 20.0

    while position + BANNER_DUR < duration:
        positions.append(round(position, 3))
        position += 60.0

    return positions


def process_video(source, output):
    with tempfile.TemporaryDirectory() as tmp:

        info = get_info(source)

        if not info:
            return False, "Не удалось прочитать видео"

        duration = info["duration"]

        if duration < 3:
            return False, "Видео слишком короткое"

        prepared, error = prepare_video(source, tmp)

        if not prepared:
            return False, f"Подготовка видео: {error}"

        positions = banner_positions(duration)

        if not positions:
            return False, "Не удалось определить позиции баннеров"

        parts = []
        current = 0.0

        for i, position in enumerate(positions):

            # Видео до баннера.
            if position > current:

                before = os.path.join(
                    tmp,
                    f"before_{i}.mp4"
                )

                ok, error = cut_video(
                    prepared,
                    current,
                    position,
                    before
                )

                if not ok:
                    return False, f"Сегмент {i}: {error}"

                parts.append(before)

            # Стоп-кадр.
            freeze = os.path.join(
                tmp,
                f"freeze_{i}.mp4"
            )

            ok, error = make_freeze_frame(
                prepared,
                position,
                freeze
            )

            if not ok:
                return False, f"Стоп-кадр {i}: {error}"

            # Баннер.
            banner = os.path.join(
                tmp,
                f"banner_{i}.mp4"
            )

            ok, error = make_banner(
                freeze,
                banner
            )

            if not ok:
                return False, f"Баннер {i}: {error}"

            parts.append(banner)

            # Продолжаем исходное видео после стоп-кадра.
            current = position + BANNER_DUR

        # Оставшийся конец видео.
        if current < duration:

            tail = os.path.join(
                tmp,
                "tail.mp4"
            )

            ok, error = cut_video(
                prepared,
                current,
                duration,
                tail
            )

            if not ok:
                return False, f"Конец видео: {error}"

            parts.append(tail)

        # Финальная сборка.
        ok, error = concat_videos(
            parts,
            output
        )

        if not ok:
            return False, f"Склейка: {error}"

        if not os.path.exists(output):
            return False, "Итоговый файл не создан"

        if os.path.getsize(output) == 0:
            return False, "Итоговый файл пустой"

        return True, f"Готово. Баннеров: {len(positions)}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Нет доступа")
        return

    await update.message.reply_text(
        "👋 Отправляй видео.\n\n"
        "Обработка:\n"
        "• 576×1024\n"
        "• стоп-кадр\n"
        "• CSDOG banner.mp4\n"
        "• продолжение видео после баннера\n\n"
        "Для видео > 1 минуты:\n"
        "00:20 → 01:20 → 02:20 → 03:20 → ...\n\n"
        "Ограничения по длительности и размеру "
        "в коде бота отсутствуют."
    )


async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        f"Твой Telegram ID: {update.effective_user.id}"
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = update.message
    user_id = update.effective_user.id

    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        await message.reply_text("⛔ Нет доступа")
        return

    media = message.video or message.document

    if not media:
        return

    status = await message.reply_text(
        "⏳ Скачиваю видео..."
    )

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

            telegram_file = await context.bot.get_file(
                media.file_id
            )

            await telegram_file.download_to_drive(
                input_file
            )

        except Exception as e:

            await status.edit_text(
                f"❌ Ошибка скачивания:\n{e}"
            )

            return

        await status.edit_text(
            "🎬 Обрабатываю видео..."
        )

        loop = asyncio.get_running_loop()

        try:

            success, result = await loop.run_in_executor(
                None,
                process_video,
                input_file,
                output_file
            )

        except Exception as e:

            await status.edit_text(
                f"❌ Ошибка FFmpeg:\n{e}"
            )

            return

        if not success:

            await status.edit_text(
                f"❌ {result}"
            )

            return

        await status.edit_text(
            f"📤 Отправляю...\n{result}"
        )

        try:

            with open(output_file, "rb") as video:

                await message.reply_video(
                    video=video,
                    caption="✅ Готово!",
                    supports_streaming=True,
                )

            await status.delete()

        except Exception as e:

            await status.edit_text(
                f"❌ Ошибка отправки:\n{e}"
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
            filters.VIDEO | filters.Document.VIDEO,
            handle_video
        )
    )

    print("✅ Bot started")

    app.run_polling()


if __name__ == "__main__":
    main()