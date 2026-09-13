import os, math, json, asyncio, tempfile, subprocess
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
BANNER = "/app/banner.mp4"
ALLOWED_USERS = set(map(int, os.environ.get("ALLOWED_USERS","").split(","))) \
    if os.environ.get("ALLOWED_USERS") else set()
SEMAPHORE = asyncio.Semaphore(1)
BANNER_DUR = 4.4
MAX_DUR = 120
FPS = 30


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return r.returncode == 0, r.stderr


def info(path):
    r = subprocess.run([
        "ffprobe","-v","quiet","-print_format","json",
        "-show_streams","-show_format", path
    ], capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
    except:
        return None
    out = {"dur":0,"w":1280,"h":720,"audio":False}
    for s in d.get("streams",[]):
        if s["codec_type"]=="video":
            out["dur"] = float(s.get("duration") or d.get("format",{}).get("duration",0))
            out["w"] = int(s.get("width",1280))
            out["h"] = int(s.get("height",720))
        elif s["codec_type"]=="audio":
            out["audio"] = True
    if not out["dur"]:
        out["dur"] = float(d.get("format",{}).get("duration",0))
    return out if out["dur"]>0 else None


def prepare(src, tmp, inf):
    out = os.path.join(tmp,"prep.mp4")
    W,H = inf["w"],inf["h"]
    if H >= W:
        vf = "scale=576:1024:force_original_aspect_ratio=increase,crop=576:1024"
        cmd = ["ffmpeg","-y","-i",src,"-vf",vf,"-r",str(FPS),
               "-c:v","libx264","-preset","ultrafast","-crf","28",
               "-c:a","aac","-b:a","128k","-movflags","+faststart",out]
    else:
        oh = int(576*H/W)
        oy = (1024-oh)//2
        fc = (f"[0:v]split=2[bg][fg];"
              f"[bg]scale=576:1024:force_original_aspect_ratio=increase,"
              f"crop=576:1024,gblur=sigma=30[bl];"
              f"[fg]scale=576:{oh}[or];"
              f"[bl][or]overlay=0:{oy}[v]")
        cmd = ["ffmpeg","-y","-i",src,"-filter_complex",fc,
               "-map","[v]","-map","0:a?","-r",str(FPS),
               "-c:v","libx264","-preset","ultrafast","-crf","28",
               "-c:a","aac","-b:a","128k","-movflags","+faststart",out]
    ok,err = run(cmd)
    return (out,None) if ok else (None,err[-300:])


def cut(src, start, end, out):
    dur = round(end-start, 3)
    ok,err = run([
        "ffmpeg","-y",
        "-ss",str(start),"-i",src,
        "-t",str(dur),
        "-c:v","libx264","-preset","ultrafast","-crf","28",
        "-r",str(FPS),
        "-c:a","aac","-b:a","128k",
        "-avoid_negative_ts","make_zero",
        "-movflags","+faststart", out
    ])
    return ok, err[-200:]


def make_freeze(src, at, out):
    """
    Вытаскиваем один кадр и зацикливаем его на BANNER_DUR секунд.
    Без звука — звук будет от баннера.
    """
    frames = int(BANNER_DUR * FPS) + 2
    ok,err = run([
        "ffmpeg","-y",
        "-ss",str(at),"-i",src,
        "-vf",f"select='eq(n,0)',loop={frames}:1:0,trim=duration={BANNER_DUR},setpts=PTS-STARTPTS",
        "-r",str(FPS),
        "-an",
        "-c:v","libx264","-preset","ultrafast","-crf","28",
        "-movflags","+faststart", out
    ])
    return ok, err[-200:]


def make_banner_over_freeze(freeze_vid, out):
    """
    Накладываем баннер (с хрома-кеем) поверх замороженного кадра.
    Звук берём от баннера.
    """
    bw = 576
    bh = int(576/(1350/750))
    bx = (576-bw)//2
    by = (1024-bh)//2

    ok,err = run([
        "ffmpeg","-y",
        "-i", freeze_vid,
        "-i", BANNER,
        "-filter_complex",
        (f"[1:v]trim=duration={BANNER_DUR},setpts=PTS-STARTPTS,"
         f"scale={bw}:{bh},"
         f"chromakey=color=00FF00:similarity=0.30:blend=0.05[ban];"
         f"[0:v]trim=duration={BANNER_DUR},setpts=PTS-STARTPTS[base];"
         f"[base][ban]overlay={bx}:{by}[v]"),
        "-map","[v]",
        "-map","1:a",
        "-t",str(BANNER_DUR),
        "-r",str(FPS),
        "-c:v","libx264","-preset","ultrafast","-crf","28",
        "-c:a","aac","-b:a","128k",
        "-movflags","+faststart", out
    ])
    return ok, err[-200:]


def concat(files, out):
    lst = out+"_list.txt"
    with open(lst,"w") as f:
        for p in files:
            f.write(f"file '{p}'\n")
    ok,err = run([
        "ffmpeg","-y",
        "-f","concat","-safe","0","-i",lst,
        "-c:v","libx264","-preset","ultrafast","-crf","28",
        "-r",str(FPS),
        "-c:a","aac","-b:a","128k",
        "-movflags","+faststart", out
    ])
    try: os.remove(lst)
    except: pass
    return ok, err[-200:]


def process(src, dst):
    with tempfile.TemporaryDirectory() as tmp:
        inf = info(src)
        if not inf: return False,"Не удалось прочитать видео"
        dur = inf["dur"]
        if dur < 3: return False,"Видео слишком короткое"
        if dur > MAX_DUR: return False,"Максимум 2 минуты"

        prep, err = prepare(src, tmp, inf)
        if not prep: return False, f"Подготовка: {err}"

        pt = round(dur/2, 3) if dur<=60 else 20.0

        # 1. Сегмент до баннера
        s1 = os.path.join(tmp,"s1.mp4")
        ok,err = cut(prep, 0, pt, s1)
        if not ok: return False,f"Сег1: {err}"

        # 2. Стоп-кадр
        frz = os.path.join(tmp,"freeze.mp4")
        ok,err = make_freeze(prep, pt, frz)
        if not ok: return False,f"Стоп-кадр: {err}"

        # 3. Баннер поверх стоп-кадра
        ban = os.path.join(tmp,"ban.mp4")
        ok,err = make_banner_over_freeze(frz, ban)
        if not ok: return False,f"Баннер: {err}"

        # 4. Сегмент после баннера
        s2 = os.path.join(tmp,"s2.mp4")
        ok,err = cut(prep, pt, dur, s2)
        if not ok: return False,f"Сег2: {err}"

        # 5. Склейка
        ok,err = concat([s1, ban, s2], dst)
        if not ok: return False,f"Склейка: {err}"

        # 6. Проверка
        ri = info(dst)
        if ri:
            exp = round(dur+BANNER_DUR,1)
            act = round(ri["dur"],1)
            if abs(act-exp)>3:
                return False,f"Длина неверная: ожидалось {exp}с, получилось {act}с"

        return True,"ok"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if ALLOWED_USERS and uid not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Нет доступа"); return
    await update.message.reply_text(
        "👋 Скидывай видео!\n\n"
        "📌 Что делаю:\n"
        "• Стоп-кадр в середине\n"
        "• Баннер CSDOG поверх полностью\n"
        "• Видео продолжается с того же места\n\n"
        "📦 Макс: 50MB, 2 минуты"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    await update.message.reply_text(f"Твой ID: `{uid}`", parse_mode="Markdown")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = msg.from_user.id
    if ALLOWED_USERS and uid not in ALLOWED_USERS:
        await msg.reply_text("⛔ Нет доступа"); return

    video = msg.video or msg.document
    if not video: return

    if video.file_size and video.file_size > 50*1024*1024:
        await msg.reply_text("❌ Максимум 50MB"); return

    status = await msg.reply_text("⏳ Скачиваю...")
    async with SEMAPHORE:
        with tempfile.TemporaryDirectory() as tmp:
            inp = os.path.join(tmp,"input.mp4")
            out = os.path.join(tmp,"output.mp4")
            try:
                f = await context.bot.get_file(video.file_id)
                await f.download_to_drive(inp)
            except Exception as e:
                await status.edit_text(f"❌ Скачивание: {e}"); return

            await status.edit_text("🎬 Обрабатываю...")
            loop = asyncio.get_event_loop()
            try:
                ok,err = await loop.run_in_executor(None, process, inp, out)
            except Exception as e:
                await status.edit_text(f"❌ Ошибка: {e}"); return

            if not ok:
                await status.edit_text(f"❌ {err}"); return
            if not os.path.exists(out) or os.path.getsize(out)==0:
                await status.edit_text("❌ Файл не создался"); return

            await status.edit_text("📤 Отправляю...")
            try:
                with open(out,"rb") as f:
                    await msg.reply_video(video=f, caption="✅ Готово!",
                                          supports_streaming=True)
                await status.delete()
            except Exception as e:
                await status.edit_text(f"❌ Отправка: {e}")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.VIDEO|filters.Document.VIDEO, handle_video))
    print("✅ Bot started")
    app.run_polling()

if __name__=="__main__":
    main()
