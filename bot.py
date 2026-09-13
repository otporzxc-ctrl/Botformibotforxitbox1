import os
import math
import json
import asyncio
import tempfile
import subprocess
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
BANNER = "/app/banner.mp4"

ALLOWED_USERS = set(map(int, os.environ.get("ALLOWED_USERS", "").split(","))) \
    if os.environ.get("ALLOWED_USERS") else set()

SEMAPHORE = asyncio.Semaphore(1)
BANNER_DURATION = 4.4
MAX_DURATION = 120


def run_cmd(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return r.returncode == 0, r.stderr


def get_info(path):
    r = subprocess.run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", path
    ], capture_output=True, text=True)
    try:
        data = json.loads(r.stdout)
    except:
        return None
    info = {"duration": 0, "width": 1280, "height": 720, "has_audio": False}
    for s in data.get("streams", []):
        if s["codec_type"] == "video":
            info["duration"] = float(s.get("duration") or
                data.get("format", {}).get("duration", 0))
            info["width"] = int(s.get("width", 1280))
            info["height"] = int(s.get("height", 720))
        elif s["codec_type"] == "audio":
            info["has_audio"] = True
    if not info["duration"]:
        info["duration"] = float(data.get("format", {}).get("duration", 0))
    return info if info["duration"] > 0 else None


def get_insert_point(duration):
    if duration <= 60:
        return round(duration / 2, 3)
    return 20.0


def prepare(src, tmp, info):
    """Приводим к 576x1024"""
    out = os.path.join(tmp, "prepared.mp4")
    W, H = info["width"], info["height"]
    is_vertical = H >= W

    if is_vertical:
        vf = "scale=576:1024:force_original_aspect_ratio=increase,crop=576:1024"
        cmd = ["ffmpeg", "-y", "-i", src,
               "-vf", vf, "-r", "30",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
               "-c:a", "aac", "-b:a", "128k",
               "-movflags", "+faststart", out]
    else:
        orig_h = int(576 * H / W)
        orig_y = (1024 - orig_h) // 2
        fc = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale=576:1024:force_original_aspect_ratio=increase,"
            f"crop=576:1024,gblur=sigma=30[blurred];"
            f"[fg]scale=576:{orig_h}[orig];"
            f"[blurred][orig]overlay=x=0:y={orig_y}[outv]"
        )
        cmd = ["ffmpeg", "-y", "-i", src,
               "-filter_complex", fc, "-map", "[outv]", "-map", "0:a?",
               "-r", "30",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
               "-c:a", "aac", "-b:a", "128k",
               "-movflags", "+faststart", out]

    ok, err = run_cmd(cmd)
    return (out, None) if ok else (None, err[-300:])


def cut_segment(src, start, end, out):
    """Вырезаем сегмент"""
    dur = round(end - start, 3)
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start), "-i", src,
        "-t", str(dur),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-r", "30",
        "-c:a", "aac", "-b:a", "128k",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart", out
    ]
    return run_cmd(cmd)


def make_banner_segment(src, freeze_at, out):
    """
    Стоп-кадр на freeze_at + баннер поверх.
    Баннер накладывается с хрома-кеем на замороженный кадр.
    """
    bw = 576
    bh = int(576 / (1350 / 750))  # ~320px
    bx = (576 - bw) // 2
    by = (1024 - bh) // 2

    # Сначала вытащим стоп-кадр как отдельное видео длиной banner_duration
    freeze_vid = out + "_freeze.mp4"
    cmd_freeze = [
        "ffmpeg", "-y",
        "-ss", str(freeze_at),
        "-i", src,
        "-vframes", "1",
        "-vf", f"tpad=stop_mode=clone:stop_duration={BANNER_DURATION}",
        "-r", "30",
        "-t", str(BANNER_DURATION),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-an",
        freeze_vid
    ]
    ok, err = run_cmd(cmd_freeze)
    if not ok:
        return False, f"freeze err: {err[-200:]}"

    # Накладываем баннер поверх стоп-кадра
    cmd_overlay = [
        "ffmpeg", "-y",
        "-i", freeze_vid,
        "-i", BANNER,
        "-filter_complex",
        f"[1:v]scale={bw}:{bh},"
        f"chromakey=color=00FF00:similarity=0.30:blend=0.05[banner];"
        f"[0:v][banner]overlay=x={bx}:y={by}[outv]",
        "-map", "[outv]",
        "-map", "1:a",
        "-t", str(BANNER_DURATION),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-r", "30",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out
    ]
    ok, err = run_cmd(cmd_overlay)
    try:
        os.remove(freeze_vid)
    except:
        pass
    return ok, err[-200:] if not ok else "ok"


def concat_files(files, out):
    """Склеиваем файлы через concat demuxer"""
    lst = out + "_list.txt"
    with open(lst, "w") as f:
        for p in files:
            f.write(f"file '{p}'\n")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", lst,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-r", "30",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out
    ]
    ok, err = run_cmd(cmd)
    try:
        os.remove(lst)
    except:
        pass
    return ok, err[-200:] if not ok else "ok"


def process(src, dst):
    with tempfile.TemporaryDirectory() as tmp:
        info = get_info(src)
        if not info:
            return False, "Не удалось прочитать видео"

        dur = info["duration"]
        if dur < 3:
            return False, "Видео слишком короткое"
        if dur > MAX_DURATION:
            return False, "Максимум 2 минуты"

        # Приводим к 576x1024
        prepared, err = prepare(src, tmp, info)
        if not prepared:
            return False, f"Ошибка подготовки: {err}"

        # Точка вставки
        pt = get_insert_point(dur)

        # Сегмент ДО баннера
        seg1 = os.path.join(tmp, "seg1.mp4")
        ok, err = cut_segment(prepared, 0, pt, seg1)
        if not ok:
            return False, f"Ошибка сег1: {err}"

        # Стоп-кадр + баннер
        ban = os.path.join(tmp, "banner_seg.mp4")
        ok, err = make_banner_segment(prepared, pt, ban)
        if not ok:
            return False, f"Ошибка баннера: {err}"

        # Сегмент ПОСЛЕ баннера
        seg2 = os.path.join(tmp, "seg2.mp4")
        ok, err = cut_segment(prepared, pt, dur, seg2)
        if not ok:
            return False, f"Ошибка сег2: {err}"

        # Склеиваем: seg1 + banner + seg2
        ok, err = concat_files([seg1, ban, seg2], dst)
        if not ok:
            return False, f"Ошибка склейки: {err}"

        # Проверка длительности
        result_info = get_info(dst)
        if result_info:
            expected = round(dur + BANNER_DURATION, 1)
            actual = round(result_info["duration"], 1)
            if abs(actual - expected) > 3:
                return False, f"Ошибка длины: ожидалось {expected}с, получилось {actual}с"

        return True, "ok"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if ALLOWED_USERS and uid not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Нет доступа")
        return
    await update.message.reply_text(
        "👋 Скидывай видео!\n\n"
        "📌 Что делаю:\n"
        "• Стоп-кадр в середине\n"
        "• Баннер CSDOG поверх\n"
        "• Видео продолжается\n\n"
        "📦 Макс: 50MB, 2 минуты"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    await update.message.reply_text(f"Твой ID: `{uid}`", parse_mode="Markdown")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = msg.from_user.id

    if ALLOWED_USERS and uid not in ALLOWED_USERS:
        await msg.reply_text("⛔ Нет доступа")
        return

    video = msg.video or msg.document
    if not video:
        return

    if video.file_size and video.file_size > 50 * 1024 * 1024:
        await msg.reply_text("❌ Максимум 50MB")
        return

    status = await msg.reply_text("⏳ Скачиваю...")

    async with SEMAPHORE:
        with tempfile.TemporaryDirectory() as tmp:
            inp = os.path.join(tmp, "input.mp4")
            out = os.path.join(tmp, "output.mp4")

            try:
                f = await context.bot.get_file(video.file_id)
                await f.download_to_drive(inp)
            except Exception as e:
                await status.edit_text(f"❌ Ошибка скачивания: {e}")
                return

            await status.edit_text("🎬 Обрабатываю...")

            loop = asyncio.get_event_loop()
            try:
                ok, err = await loop.run_in_executor(None, process, inp, out)
            except Exception as e:
                await status.edit_text(f"❌ Ошибка: {e}")
                return

            if not ok:
                await status.edit_text(f"❌ {err}")
                return

            if not os.path.exists(out) or os.path.getsize(out) == 0:
                await status.edit_text("❌ Файл не создался")
                return

            await status.edit_text("📤 Отправляю...")
            try:
                with open(out, "rb") as f:
                    await msg.reply_video(
                        video=f,
                        caption="✅ Готово!",
                        supports_streaming=True
                    )
                await status.delete()
            except Exception as e:
                await status.edit_text(f"❌ Ошибка отправки: {e}")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.Document.VIDEO, handle_video
    ))
    print("✅ Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
