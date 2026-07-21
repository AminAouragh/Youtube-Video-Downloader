import os
import shutil
import threading
import uuid
import subprocess

from flask import Flask, render_template, request, jsonify
import yt_dlp

app = Flask(__name__)

OUTPUT_DIR = os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "downloads")
os.makedirs(OUTPUT_DIR, exist_ok=True)

ALL_RESOLUTIONS = [
    ("240p", 240), ("360p", 360), ("480p", 480), ("720p", 720),
    ("1080p", 1080), ("1440p (2K)", 1440), ("2160p (4K)", 2160), ("4320p (8K)", 4320),
]

JOBS = {}


def find_ffmpeg():
    return shutil.which("ffmpeg")


def is_playlist_url(url):
    return "list=" in url or "/playlist" in url


def build_format_string(max_height):
    if max_height is None:
        return "bestvideo+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
    return (
        f"bestvideo[height<={max_height}]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={max_height}]+bestaudio"
        f"/best[height<={max_height}]/best"
    )


def verify_resolution(filepath, ffmpeg_path):
    ffprobe_path = shutil.which("ffprobe")
    if ffprobe_path is None and ffmpeg_path:
        candidate = os.path.join(
            os.path.dirname(ffmpeg_path),
            "ffprobe.exe" if os.name == "nt" else "ffprobe",
        )
        if os.path.exists(candidate):
            ffprobe_path = candidate
    if ffprobe_path is None:
        return None
    try:
        result = subprocess.run(
            [ffprobe_path, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", filepath],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def clean_stale_partial_files(output_dir):
    for root, _, files in os.walk(output_dir):
        for f in files:
            if f.endswith(".part") or f.endswith(".ytdl"):
                try:
                    os.remove(os.path.join(root, f))
                except OSError:
                    pass


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/probe", methods=["POST"])
def probe():
    """Given a URL, return which resolutions actually exist plus playlist info."""
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    playlist = is_playlist_url(url)

    probe_opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            info = entries[0] if entries else info

        title = info.get("title", "Unknown title") if info else "Unknown"
        formats = info.get("formats", []) if info else []
        heights = sorted({f.get("height") for f in formats if f.get("height")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    menu = [(label, h) for label, h in ALL_RESOLUTIONS if h in heights]
    if not menu and heights:
        max_h = max(heights)
        menu = [(label, h) for label, h in ALL_RESOLUTIONS if h <= max_h]
    menu.append(("Best available", None))

    return jsonify({
        "title": title,
        "is_playlist": playlist,
        "resolutions": [{"label": label, "height": h} for label, h in menu],
        "ffmpeg_found": find_ffmpeg() is not None,
    })


def run_download_job(job_id, url, max_height, download_playlist):
    ffmpeg_path = find_ffmpeg()
    job = JOBS[job_id]

    clean_stale_partial_files(OUTPUT_DIR)
    fmt = build_format_string(max_height)
    playlist = is_playlist_url(url)
    noplaylist = not (playlist and download_playlist)

    outtmpl = (
        os.path.join(OUTPUT_DIR, "%(playlist_title)s",
                     "%(playlist_index)02d - %(title)s.%(ext)s")
        if (playlist and not noplaylist)
        else os.path.join(OUTPUT_DIR, "%(title)s.%(ext)s")
    )
    archive_path = os.path.join(OUTPUT_DIR, "download_archive.txt")

    def progress_hook(d):
        if d["status"] == "downloading":
            job["percent"] = d.get("_percent_str", "").strip()
            job["speed"] = d.get("_speed_str", "").strip()
            job["title"] = d.get("info_dict", {}).get(
                "title", job.get("title", ""))
        elif d["status"] == "finished":
            job["percent"] = "100%"
            job["status_text"] = "Merging / post-processing..."

    def postprocessor_hook(d):
        if d["status"] == "finished":
            filepath = d.get("info_dict", {}).get(
                "filepath") or d.get("filepath")
            if filepath and os.path.exists(filepath):
                res = verify_resolution(filepath, ffmpeg_path)
                job["files"].append({
                    "name": os.path.basename(filepath),
                    "resolution": res or "unknown",
                })

    ydl_opts = {
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "noplaylist": noplaylist,
        "ignoreerrors": True,
        "download_archive": archive_path,
        "continuedl": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
    }
    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = ffmpeg_path

    try:
        job["status"] = "running"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        job["status"] = "done"
        job["status_text"] = "Complete"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    max_height = data.get("height")  # None means "best available"
    download_playlist = bool(data.get("download_playlist", False))

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "status": "starting",
        "percent": "0%",
        "speed": "",
        "title": "",
        "status_text": "Starting...",
        "files": [],
        "error": None,
    }

    thread = threading.Thread(
        target=run_download_job,
        args=(job_id, url, max_height, download_playlist),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify(job)


if __name__ == "__main__":
    if find_ffmpeg() is None:
        print("WARNING: ffmpeg not found on PATH. High-res merging will fail.")
    app.run(debug=False, port=5000)
