import os
import shutil
import yt_dlp

ALL_RESOLUTIONS = [
    ("240p", 240),
    ("360p", 360),
    ("480p", 480),
    ("720p", 720),
    ("1080p", 1080),
    ("1440p (2K)", 1440),
    ("2160p (4K)", 2160),
    ("4320p (8K)", 4320),
]


def find_ffmpeg():
    """Return path to ffmpeg if found on PATH, else None."""
    return shutil.which("ffmpeg")


def is_playlist_url(url):
    return "list=" in url or "/playlist" in url


def get_available_heights(url, ydl_opts_base):
    """
    Probe the URL and return the sorted set of video heights actually
    available. For playlists, checks the first video as a representative
    sample (playlists can vary slightly per video, but this avoids having
    to probe every single entry up front).
    """
    probe_opts = dict(ydl_opts_base)
    probe_opts.update({
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "noplaylist": True,
    })

    with yt_dlp.YoutubeDL(probe_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        info = entries[0] if entries else info

    formats = info.get("formats", []) if info else []
    heights = {f.get("height") for f in formats if f.get("height")}
    return sorted(heights)


def build_resolution_menu(available_heights):
    """
    Build the menu list, keeping only resolutions that are <= the max
    available height and reasonably close to real options, plus a
    'best available' catch-all entry.
    """
    if not available_heights:
        menu = list(ALL_RESOLUTIONS)
    else:
        max_h = max(available_heights)
        menu = [(label, h)
                for label, h in ALL_RESOLUTIONS if h in available_heights]
        if not menu:
            menu = [(label, h) for label, h in ALL_RESOLUTIONS if h <= max_h]

    menu.append(("Best available", None))
    return menu


def show_resolution_menu(menu):
    print("\nAvailable resolutions for this video:")
    for i, (label, _) in enumerate(menu, start=1):
        print(f"  {i}. {label}")
    choice = input(f"Enter choice [1-{len(menu)}]: ").strip()
    while not choice.isdigit() or not (1 <= int(choice) <= len(menu)):
        choice = input(f"Invalid choice, try again [1-{len(menu)}]: ").strip()
    return menu[int(choice) - 1]


def build_format_string(max_height):
    if max_height is None:
        return "bestvideo+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
    return (
        f"bestvideo[height<={max_height}]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={max_height}]+bestaudio"
        f"/best[height<={max_height}]/best"
    )


def make_progress_hook(ffmpeg_path):
    def progress_hook(d):
        if d["status"] == "downloading":
            title = d.get("info_dict", {}).get("title", "video")
            pct = d.get("_percent_str", "").strip()
            speed = d.get("_speed_str", "").strip()
            print(f"\r{title[:40]:40} {pct:>7} {speed:>12}",
                  end="", flush=True)
        elif d["status"] == "finished":
            print("\nDone downloading, now merging/post-processing...")
    return progress_hook


def postprocessor_hook(d, ffmpeg_path):
    if d["status"] == "finished":
        filepath = d.get("info_dict", {}).get("filepath") or d.get("filepath")
        if filepath and os.path.exists(filepath):
            verify_resolution(filepath, ffmpeg_path)


def clean_stale_partial_files(output_dir):
    """
    Remove leftover .part / .ytdl fragment files from previous interrupted
    downloads. These can confuse yt-dlp into a bad resume state, or just
    sit there uselessly if the format selection changed between runs.
    """
    removed = []
    for root, _, files in os.walk(output_dir):
        for f in files:
            if f.endswith(".part") or f.endswith(".ytdl"):
                full = os.path.join(root, f)
                try:
                    os.remove(full)
                    removed.append(full)
                except OSError:
                    pass
    if removed:
        print(
            f"Cleaned up {len(removed)} leftover partial file(s) from a previous run.")


def verify_resolution(filepath, ffmpeg_path):
    """Run ffprobe on the finished file and print its actual resolution,
    so there's no guessing whether you really got the quality you picked."""
    ffprobe_path = shutil.which("ffprobe")
    if ffprobe_path is None and ffmpeg_path:
        candidate = os.path.join(os.path.dirname(
            ffmpeg_path), "ffprobe.exe" if os.name == "nt" else "ffprobe")
        if os.path.exists(candidate):
            ffprobe_path = candidate

    if ffprobe_path is None:
        return

    import subprocess
    try:
        result = subprocess.run(
            [ffprobe_path, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", filepath],
            capture_output=True, text=True, timeout=15,
        )
        res = result.stdout.strip()
        if res:
            print(
                f"  -> Actual resolution: {res}  ({os.path.basename(filepath)})")
    except Exception:
        pass


def download(url, max_height, ffmpeg_path, output_dir="downloads"):
    os.makedirs(output_dir, exist_ok=True)
    clean_stale_partial_files(output_dir)

    fmt = build_format_string(max_height)

    playlist = is_playlist_url(url)
    if playlist:
        ask = input(
            "This looks like a playlist URL. Download entire playlist? [y/n]: ").strip().lower()
        noplaylist = ask != "y"
    else:
        noplaylist = True

    outtmpl = (
        os.path.join(output_dir, "%(playlist_title)s",
                     "%(playlist_index)02d - %(title)s.%(ext)s")
        if (playlist and not noplaylist)
        else os.path.join(output_dir, "%(title)s.%(ext)s")
    )

    archive_path = os.path.join(output_dir, "download_archive.txt")

    ydl_opts = {
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "noplaylist": noplaylist,
        "ignoreerrors": True,
        "download_archive": archive_path,
        "continuedl": True,
        "progress_hooks": [make_progress_hook(ffmpeg_path)],
        "postprocessor_hooks": [lambda d: postprocessor_hook(d, ffmpeg_path)],
    }
    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = ffmpeg_path

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


def main():
    print("=== YouTube Downloader (yt-dlp) ===")
    url = input("Enter video or playlist URL: ").strip()

    ffmpeg_path = find_ffmpeg()
    if ffmpeg_path is None:
        print(
            "\nWARNING: ffmpeg was not found on your PATH.\n")
        proceed = input("Continue anyway? [y/n]: ").strip().lower()
        if proceed != "y":
            raise SystemExit("Aborted: install ffmpeg first.")

    print("Checking available resolutions...")
    try:
        heights = get_available_heights(url, {})
    except Exception as e:
        print(
            f"Could not probe formats ({e}), showing full resolution list instead.")
        heights = []

    menu = build_resolution_menu(heights)
    label, max_height = show_resolution_menu(menu)
    print(f"\nSelected: {label}")

    if is_playlist_url(url):
        print(
            "Note: resolutions were checked against the first video in the "
            "playlist. Other videos will use the closest available quality "
            "if this exact resolution isn't offered for them.\n"
        )

    try:
        download(url, max_height, ffmpeg_path)
        print("\nAll done. Files saved in the 'downloads' folder.")
    except Exception as e:
        print(f"\nError: {e}")


if __name__ == "__main__":
    main()
