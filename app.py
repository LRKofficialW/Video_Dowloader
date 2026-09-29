from __future__ import annotations

import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_from_directory

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "downloads"
OUTPUT.mkdir(exist_ok=True)
app = Flask(__name__)
jobs: dict[str, dict] = {}
lock = threading.Lock()


def set_job(job_id: str, **values):
    with lock:
        jobs[job_id].update(values)


def download_worker(job_id: str, url: str, mode: str, quality: str, fmt: str):
    try:
        from yt_dlp import YoutubeDL

        def progress(data):
            if data.get("status") == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                pct = int(data.get("downloaded_bytes", 0) * 100 / total) if total else 0
                set_job(job_id, progress=min(pct, 99), message="Скачивание…")
            elif data.get("status") == "finished":
                set_job(job_id, progress=100, message="Обработка файла…")

        height = {"best": "bestvideo*+bestaudio/best", "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]", "720": "bestvideo[height<=720]+bestaudio/best[height<=720]", "480": "bestvideo[height<=480]+bestaudio/best[height<=480]", "360": "bestvideo[height<=360]+bestaudio/best[height<=360]"}.get(quality, "bestvideo*+bestaudio/best")
        opts = {"outtmpl": str(OUTPUT / f"{job_id}_%(title).100s.%(ext)s"), "noplaylist": True, "progress_hooks": [progress], "restrictfilenames": True, "windowsfilenames": True}
        if mode == "audio":
            opts.update(format="bestaudio/best", postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": fmt, "preferredquality": "192"}])
        else:
            opts["format"] = height
            opts["merge_output_format"] = fmt
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
        if mode == "audio":
            filename = str(Path(filename).with_suffix(f".{fmt}"))
        elif fmt == "mp4":
            # yt-dlp's merge output format can change the extension from prepare_filename.
            candidate = str(Path(filename).with_suffix(".mp4"))
            if Path(candidate).exists():
                filename = candidate
        if not Path(filename).exists():
            candidates = list(OUTPUT.glob(f"{job_id}_*"))
            if not candidates:
                raise RuntimeError("Файл не найден после скачивания")
            filename = str(max(candidates, key=lambda p: p.stat().st_mtime))
        set_job(job_id, status="done", progress=100, message="Готово", filename=Path(filename).name, title=info.get("title", "Видео"))
    except Exception as exc:
        set_job(job_id, status="error", message=str(exc)[:500])


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/download")
def start_download():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return jsonify(error="Введите корректную ссылку, начинающуюся с http:// или https://"), 400
    mode = data.get("mode", "video")
    quality = data.get("quality", "best")
    fmt = data.get("format", "mp4")
    if mode not in {"video", "audio"} or fmt not in ({"mp4", "mkv", "webm"} if mode == "video" else {"mp3", "m4a", "wav", "opus"}):
        return jsonify(error="Неподдерживаемый тип или формат"), 400
    job_id = uuid.uuid4().hex
    with lock:
        jobs[job_id] = {"status": "running", "progress": 0, "message": "Подготовка…"}
    threading.Thread(target=download_worker, args=(job_id, url, mode, quality, fmt), daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/api/status/<job_id>")
def job_status(job_id):
    with lock:
        job = jobs.get(job_id)
    return (jsonify(job), 200) if job else (jsonify(error="Задача не найдена"), 404)


@app.get("/download/<path:filename>")
def download_file(filename):
    return send_from_directory(OUTPUT, filename, as_attachment=True)


def edit_worker(job_id: str, source: str, start: float, end: float | None, speed: float, mute: bool):
    try:
        import subprocess
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("Для редактирования установите FFmpeg и добавьте его в PATH.")
        src = OUTPUT / source
        if not src.is_file() or src.parent.resolve() != OUTPUT.resolve():
            raise RuntimeError("Исходный файл не найден")
        out = OUTPUT / f"edited_{job_id}_{src.stem}.mp4"
        cmd = [ffmpeg, "-y", "-ss", str(start), "-i", str(src)]
        if end is not None:
            cmd += ["-t", str(end - start)]
        vf = f"setpts=PTS/{speed}"
        cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]
        if mute:
            cmd += ["-an"]
        else:
            cmd += ["-af", f"atempo={speed}", "-c:a", "aac"]
        cmd += ["-movflags", "+faststart", str(out)]
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if run.returncode:
            raise RuntimeError((run.stderr or "Ошибка FFmpeg")[-800:])
        set_job(job_id, status="done", progress=100, message="Готово", filename=out.name)
    except Exception as exc:
        set_job(job_id, status="error", message=str(exc)[:800])


@app.post("/api/edit")
def edit_video():
    data = request.get_json(silent=True) or {}
    source = Path(str(data.get("filename", ""))).name
    try:
        start = max(0.0, float(data.get("start", 0)))
        end_val = data.get("end")
        end = float(end_val) if end_val not in (None, "") else None
        speed = float(data.get("speed", 1))
    except (TypeError, ValueError):
        return jsonify(error="Проверьте значения времени и скорости"), 400
    if end is not None and end <= start:
        return jsonify(error="Время окончания должно быть больше времени начала"), 400
    if not 0.25 <= speed <= 4:
        return jsonify(error="Скорость должна быть от 0,25× до 4×"), 400
    if not source or not (OUTPUT / source).is_file():
        return jsonify(error="Сначала скачайте видео"), 400
    job_id = uuid.uuid4().hex
    with lock:
        jobs[job_id] = {"status": "running", "progress": 0, "message": "Редактирование…"}
    threading.Thread(target=edit_worker, args=(job_id, source, start, end, speed, bool(data.get("mute"))), daemon=True).start()
    return jsonify(job_id=job_id)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
