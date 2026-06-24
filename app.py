import os
import sys
import io
import uuid
import json
import time
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import zipfile
import shutil
import queue
from pathlib import Path
from datetime import datetime

from flask import Flask, request, jsonify, Response, send_file, session
from flask_cors import CORS

# ─── Fix Windows console encoding ────────────────────────────────────────────
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif sys.stdout and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ─── Configuration ───────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.resolve()
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

# ─── Cookies Setup ──────────────────────────────────────────────
SECRETS_FILE = Path("/etc/secrets/cookies.txt")
COOKIES_FILE = None
cookies_env = os.environ.get("COOKIES")
cookies_b64 = os.environ.get("COOKIES_B64")
CLEAN_COOKIES_PATH = BASE_DIR / "cookies_clean.txt"


def clean_cookie_file(content: str) -> str:
    """Fix Netscape cookie file where tabs were converted to spaces."""
    lines = content.splitlines()
    result = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            result.append(line)
            continue
        if "\t" in line:
            result.append(line)
        else:
            parts = re.split(r" {2,}", line)
            if len(parts) >= 7:
                result.append("\t".join(parts))
            else:
                result.append(line)
    return "\n".join(result)


def trim_task_logs(task):
    """Keep logs and errors within memory limits."""
    if len(task.get("logs", [])) > 100:
        task["logs"] = task["logs"][-100:]
    if len(task.get("errors", [])) > 10:
        task["errors"] = task["errors"][-10:]


raw_cookies = None

if SECRETS_FILE.exists():
    raw_cookies = SECRETS_FILE.read_text(encoding="utf-8")
    print(f"  [>] Cookies loaded from Render Secret File ({SECRETS_FILE})")
elif cookies_b64:
    import base64
    try:
        raw_cookies = base64.b64decode(cookies_b64).decode("utf-8")
        print(f"  [>] Cookies loaded from COOKIES_B64 env var ({len(raw_cookies)} chars)")
    except Exception as e:
        print(f"  [>] Failed to decode COOKIES_B64: {e}")
elif cookies_env:
    raw_cookies = cookies_env
    print(f"  [>] Cookies loaded from COOKIES env var ({len(raw_cookies)} chars)")
elif (BASE_DIR / "cookies.txt").exists():
    raw_cookies = (BASE_DIR / "cookies.txt").read_text(encoding="utf-8")
    print(f"  [>] Cookies loaded from {BASE_DIR / 'cookies.txt'}")

if raw_cookies:
    cleaned = clean_cookie_file(raw_cookies)
    CLEAN_COOKIES_PATH.write_text(cleaned, encoding="utf-8")
    COOKIES_FILE = CLEAN_COOKIES_PATH
    print(f"  [>] Cookies cleaned and written to {COOKIES_FILE}")
else:
    print("  [>] No cookies file found — YouTube may block server IPs")

COOKIES_ARG = []
if COOKIES_FILE and COOKIES_FILE.exists():
    COOKIES_ARG = ["--cookies", str(COOKIES_FILE)]

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "spotfetch-dev-key-change-in-prod")
CORS(app)

# ─── Download Queue & State ─────────────────────────────────────────────────

download_queue = {}      # task_id -> task_info dict
queue_lock = threading.Lock()
active_downloads = 0
MAX_CONCURRENT = 1       # sequential downloads to avoid rate limiting

# ─── Cleanup old files ──────────────────────────────────────────────

CLEANUP_INTERVAL = 300
FILE_MAX_AGE = 600

def cleanup_old_files():
    while True:
        time.sleep(CLEANUP_INTERVAL)
        now = time.time()
        try:
            for f in DOWNLOADS_DIR.rglob("*"):
                if f.is_file() and f.name != ".gitkeep":
                    age = now - f.stat().st_mtime
                    if age > FILE_MAX_AGE:
                        f.unlink(missing_ok=True)
            for d in sorted(DOWNLOADS_DIR.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if d.is_dir() and d != DOWNLOADS_DIR:
                    try:
                        d.rmdir()
                    except OSError:
                        pass
        except Exception:
            pass

cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

# ─── Helper: Parse spotdl output ────────────────────────────────────────────

def parse_spotdl_output(line, task_id):
    """Parse a line of spotdl output to extract progress info."""
    with queue_lock:
        task = download_queue.get(task_id)
        if not task:
            return

    # spotdl outputs lines like:
    # "Downloaded "Song Name - Artist": ..."
    # "Processing query: ..."
    # "Found ... results for ..."
    # Progress bar lines with percentages

    line_stripped = line.strip()
    if not line_stripped:
        return

    with queue_lock:
        task["logs"].append(line_stripped)

    # DEBUG: Print to flask console so we can see what spotdl says
    print(f"SPOTDL: {repr(line_stripped)}", flush=True)

    with queue_lock:
        # Detect "Found X songs" for playlists
        found_match = re.search(r"Found (\d+) songs", line_stripped, re.IGNORECASE)
        if found_match:
            task["total_tracks"] = int(found_match.group(1))

        # Detect downloading specific track
        if "Downloading" in line_stripped:
            # Extract track name if possible
            track_match = re.search(r'Downloading\s+"?(.+?)"?\s*$', line_stripped)
            if track_match:
                task["current_track"] = track_match.group(1).strip('"')

        # Detect downloaded/completed track
        if "Downloaded" in line_stripped or "Skipping" in line_stripped:
            task["completed_tracks"] = task.get("completed_tracks", 0) + 1
            total = task.get("total_tracks", 1)
            if total > 0:
                task["progress"] = min(100, int((task["completed_tracks"] / total) * 100))

        # Detect errors
        if "error" in line_stripped.lower() or "Error" in line_stripped:
            task["errors"].append(line_stripped)
            
        # Detect Spotify rate limit
        if "rate/request limit" in line_stripped.lower():
            task["errors"].append("Spotify API Rate Limit: Configure um Client ID/Secret do Spotify nas opções do spotDL, ou tente novamente mais tarde.")
            task["status"] = "error"


def normalize_title(title):
    """Normalize a title for fuzzy comparison."""
    import unicodedata
    t = unicodedata.normalize('NFKD', title.lower())
    t = t.encode('ascii', 'ignore').decode('ascii')
    t = re.sub(r'\(official\s*(video|audio|lyrics?|music\s*video|clip)\)', '', t)
    t = re.sub(r'\(video\s*(oficial|clipe|lyric)\)', '', t)
    t = re.sub(r'\(official\)', '', t)
    t = re.sub(r'\(audio\s*(only)?\)', '', t)
    t = re.sub(r'\(lyrics?\)', '', t)
    t = re.sub(r'\(hq\)|\(hd\)', '', t)
    t = re.sub(r'-?\s*(topic)', '', t)
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def clip_score(title):
    """Lower score = more likely to be plain audio (not a clip)."""
    t = title.lower()
    score = 0
    for term in ['clip', 'clipe', 'video oficial', 'official video',
                 'music video', 'video clip', 'vídeo clipe', 'visualizer',
                 'vevo']:
        if term in t:
            score += 10
    for term in ['audio', 'lyrics', 'lyric', 'topic', '- topic', 'official audio']:
        if term in t:
            score -= 5
    if len(t) > 80:
        score += 3
    return score


def run_download(task_id):
    """Execute yt-dlp download in a background thread with pipeline."""
    global active_downloads
    task = None

    try:
        with queue_lock:
            task = download_queue.get(task_id)
            if not task:
                return
            task["status"] = "downloading"
            task["started_at"] = datetime.now().isoformat()
        url = task["url"]

        import spotify_scraper
        raw_queries = [url]
        folder_name = None
        track_list = []

        if "spotify.com" in url:
            task["logs"].append("Lendo metadata do Spotify sem API...")
            raw_queries, folder_name = spotify_scraper.get_spotify_queries(url)
            if not raw_queries:
                with queue_lock:
                    task["errors"].append("Não foi possível extrair dados desse link do Spotify.")
                    task["status"] = "error"
                return

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        # ─── Folder setup & Resume ────────────────────────────────────────
        all_queries = None
        folder_path = None
        safe_name = None
        if folder_name and task["type"] in ("playlist", "album"):
            safe_name = re.sub(r'[<>:"/\\|?*]', '', folder_name).strip()
            safe_name = re.sub(r'\s+', ' ', safe_name)
            if not safe_name:
                safe_name = task["type"].capitalize()
            folder_path = DOWNLOADS_DIR / safe_name
            folder_path.mkdir(exist_ok=True)
            with queue_lock:
                task["folder_name"] = safe_name

            all_queries = list(raw_queries)

            existing = list(folder_path.glob("*.mp3"))
            if existing:
                remaining = []
                skipped = 0
                existing_norm = [(f.stem, set(normalize_title(f.stem).split())) for f in existing]
                for q in all_queries:
                    q_words = set(normalize_title(q).split())
                    found = False
                    for fname, fwords in existing_norm:
                        if len(q_words & fwords) / max(1, min(len(q_words), len(fwords))) >= 0.6:
                            found = True
                            break
                    if found:
                        skipped += 1
                    else:
                        remaining.append(q)

                total = len(all_queries)
                with queue_lock:
                    task["total_tracks"] = total
                    task["completed_tracks"] = skipped
                    task["progress"] = int((skipped / max(1, total)) * 100)
                if skipped:
                    task["logs"].append(f"{skipped} de {total} já baixadas. Baixando {len(remaining)} restantes...")
                raw_queries = remaining

        # Build track list
        track_list = []
        if all_queries is not None:
            remaining_set = set(raw_queries)
            for q in all_queries:
                status = "pending" if q in remaining_set else "completed"
                track_list.append({"name": str(q), "status": status})
        elif len(raw_queries) == 1:
            track_list.append({"name": str(raw_queries[0]), "status": "pending"})
        if track_list:
            with queue_lock:
                task["tracks"] = track_list

        if not raw_queries:
            with queue_lock:
                task["status"] = "completed"
                task["progress"] = 100
                task["logs"].append("Todas as músicas já foram baixadas!")
            return

        total_tracks = len(raw_queries)
        with queue_lock:
            if task["total_tracks"] == 0:
                task["total_tracks"] = total_tracks

        # ─── Pipeline: Search → Download → ZIP ───────────────────────────

        dl_done = 0
        pipeline_lock = threading.Lock()
        download_slots = threading.Semaphore(1)

        zip_path = None
        if folder_path and safe_name:
            zip_path = DOWNLOADS_DIR / f"{safe_name}.zip"

        def get_output_template(idx):
            base = folder_path if folder_path else DOWNLOADS_DIR
            return str(base / f"{idx}_%(id)s.%(ext)s")

        def find_track_entry(query):
            for i, t in enumerate(track_list):
                if t["name"] == query:
                    return i
            return None

        def process_one(idx, query):
            """Search YouTube and download a single track."""
            nonlocal dl_done

            # ── Step 1: Search YouTube ──
            if "youtube.com/watch" in query or "youtu.be/" in query or "music.youtube.com" in query:
                youtube_url = query
            else:
                search_cmd = [
                    "yt-dlp", "--flat-playlist", "-J", "-i", "--no-warnings",
                    "--extractor-args", "youtube:player_client=android",
                ] + COOKIES_ARG + [f"ytsearch5:{query}"]
                try:
                    proc = subprocess.run(
                        search_cmd, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=30, env=env,
                    )
                    if proc.returncode == 0:
                        data = json.loads(proc.stdout)
                        entries = data.get("entries") or []
                        valid = [e for e in entries if e.get("id") and e.get("title")]
                        valid.sort(key=lambda e: clip_score(e["title"]))
                        youtube_url = f"https://www.youtube.com/watch?v={valid[0]['id']}" if valid else f"ytsearch1:{query}"
                    else:
                        youtube_url = f"ytsearch1:{query}"
                except Exception:
                    youtube_url = f"ytsearch1:{query}"

            t_idx = find_track_entry(query)
            if t_idx is not None:
                track_list[t_idx]["status"] = "found"

            # ── Step 2: Download (respects concurrency limit via semaphore) ──
            with download_slots:
                if t_idx is not None:
                    track_list[t_idx]["status"] = "downloading"
                    with queue_lock:
                        task["current_track"] = track_list[t_idx]["name"]

                output_tmpl = get_output_template(idx)
                cmd = [
                    "yt-dlp",
                    "--remote-components", "ejs:github",
                    "--extractor-args", "youtube:player_client=web",
                    "-f", "ba/best",
                    "--extract-audio",
                    "--audio-format", "mp3",
                    "--audio-quality", "320k",
                    "--no-check-formats",
                    "--socket-timeout", "30",
                    "--retries", "3",
                    "--throttled-rate", "100K",
                    "--concurrent-fragments", "3",
                    "--output", output_tmpl,
                    "--newline",
                    "--ignore-errors",
                    youtube_url,
                ] + COOKIES_ARG

                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                )

                output_queue = queue.Queue()

                def reader_thread(proc, q):
                    try:
                        for line in proc.stdout:
                            q.put(line)
                    finally:
                        q.put(None)

                reader = threading.Thread(target=reader_thread, args=(process, output_queue), daemon=True)
                reader.start()

                download_ok = True
                while True:
                    try:
                        line = output_queue.get(timeout=120)
                    except queue.Empty:
                        process.kill()
                        download_ok = False
                        if t_idx is not None:
                            track_list[t_idx]["status"] = "error"
                        with queue_lock:
                            task["errors"].append(f"yt-dlp travado (sem saída por 120s) em '{query}'")
                        break
                    if line is None:
                        break

                    line_stripped = line.strip()
                    if not line_stripped:
                        continue

                    try:
                        print(f"YT-DLP: {line_stripped}", flush=True)
                    except UnicodeEncodeError:
                        safe = line_stripped.encode("utf-8", errors="replace").decode("utf-8")
                        print(f"YT-DLP: {safe}", flush=True)
                    with queue_lock:
                        task["logs"].append(line_stripped)
                        trim_task_logs(task)

                process.wait()

                if download_ok:
                    base = folder_path if folder_path else DOWNLOADS_DIR
                    candidates = list(base.glob(f"{idx}_*.mp3"))
                    mp3_path = candidates[0] if candidates else None

                    if mp3_path and mp3_path.exists():
                        clean_name = re.sub(r'[<>:"/\\|?*]', '', query).strip()
                        clean_name = re.sub(r'\s+', ' ', clean_name)
                        new_path = mp3_path.parent / f"{clean_name}.mp3"
                        try:
                            mp3_path.rename(new_path)
                            mp3_path = new_path
                        except Exception:
                            pass

                        if t_idx is not None:
                            track_list[t_idx]["status"] = "completed"
                        with pipeline_lock:
                            dl_done += 1

                        if zip_path:
                            with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_DEFLATED) as zf:
                                zf.write(mp3_path, mp3_path.name)
                            try:
                                mp3_path.unlink(missing_ok=True)
                            except Exception:
                                pass
                    else:
                        if t_idx is not None:
                            track_list[t_idx]["status"] = "error"
                        with queue_lock:
                            task["errors"].append(f"Formato indisponível: '{query}'")

        # ── Execute pipeline: all tracks searched in parallel,               ──
        #    downloads limited to 2 concurrent via semaphore                   ──
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(process_one, i, q) for i, q in enumerate(raw_queries)]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass

                with pipeline_lock:
                    current_done = dl_done
                with queue_lock:
                    task["completed_tracks"] = current_done
                    task["progress"] = min(99, int((current_done / max(1, total_tracks)) * 100))
                    task["tracks"] = list(track_list)
                    trim_task_logs(task)

        # ─── Finalização ──────────────────────────────────────────────────
        success_count = sum(1 for t in track_list if t["status"] == "completed")

        with queue_lock:
            task["completed_tracks"] = success_count
            task["progress"] = 100

            if success_count > 0:
                task["logs"].append(f"Download concluído: {success_count} de {total_tracks} músicas.")
                task["status"] = "completed"

                if all_queries is not None and folder_name and safe_name:
                    if zip_path and zip_path.exists():
                        task["zip_url"] = f"/api/downloads/{zip_path.name}"
                else:
                    base = folder_path if folder_path else DOWNLOADS_DIR
                    mp3_files = sorted(base.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
                    if mp3_files:
                        latest = mp3_files[-1]
                        rel = latest.relative_to(DOWNLOADS_DIR)
                        task["file_url"] = f"/api/downloads/{rel}"
            else:
                task["status"] = "error"
                if not task["errors"]:
                    task["errors"].append("Nenhuma música foi baixada com sucesso.")

    except FileNotFoundError:
        with queue_lock:
            task["status"] = "error"
            task["errors"].append("yt-dlp not found. Install it with: pip install yt-dlp")
    except Exception as e:
        with queue_lock:
            task["status"] = "error"
            task["errors"].append(str(e))
        import traceback
        print(f"ERRO: {traceback.format_exc()}", flush=True)
    finally:
        with queue_lock:
            if task:
                task["finished_at"] = datetime.now().isoformat()
            active_downloads -= 1

        process_queue()


def process_queue():
    """Start next queued download if capacity allows."""
    global active_downloads
    with queue_lock:
        if active_downloads >= MAX_CONCURRENT:
            return
        for tid, task in download_queue.items():
            if task["status"] == "queued":
                active_downloads += 1
                break
        else:
            return

    thread = threading.Thread(target=run_download, args=(tid,), daemon=True)
    thread.start()


# ─── API Routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file("static/index.html")


@app.route("/api/session")
def get_session():
    if "user_id" not in session:
        session["user_id"] = str(uuid.uuid4())[:8]
    return jsonify({"user_id": session["user_id"]})


@app.route("/api/download", methods=["POST"])
def start_download():
    """Start a new download task."""
    data = request.get_json()
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "URL is required"}), 400

    # Validate Spotify or YouTube URL
    spotify_pattern = r"https?://open\.spotify\.com/(track|playlist|album|artist)/[a-zA-Z0-9]+"
    youtube_pattern = r"https?://(www\.)?(youtube\.com/watch\?v=|youtu\.be/|music\.youtube\.com/watch\?v=|youtube\.com/playlist\?list=|music\.youtube\.com/playlist\?list=)[a-zA-Z0-9_-]+"
    
    if not (re.match(spotify_pattern, url) or re.match(youtube_pattern, url)):
        return jsonify({"error": "URL inválida. Cole um link do Spotify ou do YouTube (Music)."}), 400

    # Detect type from URL
    url_type = "track"
    if "playlist" in url or "album" in url or "artist" in url:
        url_type = "playlist"

    user_id = session.get("user_id", "")

    task_id = str(uuid.uuid4())[:8]

    with queue_lock:
        download_queue[task_id] = {
            "id": task_id,
            "url": url,
            "type": url_type,
            "user_id": user_id,
            "status": "queued",
            "progress": 0,
            "total_tracks": 1 if url_type == "track" else 0,
            "completed_tracks": 0,
            "current_track": "",
            "folder_name": "",
            "tracks": [],
            "created_at": datetime.now().isoformat(),
            "started_at": None,
            "finished_at": None,
            "errors": [],
            "logs": [],
            "pid": None,
        }

    process_queue()

    return jsonify({"task_id": task_id, "status": "queued"}), 202


@app.route("/api/progress/<task_id>")
def stream_progress(task_id):
    """SSE endpoint for real-time progress updates (own user only)."""
    user_id = session.get("user_id", "")
    def generate():
        last_progress = -1
        last_status = ""
        last_log_count = 0
        last_current_track = ""
        last_tracks_len = -1

        while True:
            with queue_lock:
                task = download_queue.get(task_id)

            if not task:
                yield f"data: {json.dumps({'error': 'Task not found'})}\n\n"
                break

            if task.get("user_id") != user_id:
                yield f"data: {json.dumps({'error': 'Unauthorized'})}\n\n"
                break

            current_progress = task["progress"]
            current_status = task["status"]
            current_log_count = len(task["logs"])
            current_track_val = task["current_track"]
            current_tracks = task.get("tracks", [])
            current_tracks_len = len(current_tracks) if current_tracks else 0

            # Send update if something changed
            if (current_progress != last_progress or
                current_status != last_status or
                current_log_count != last_log_count or
                current_track_val != last_current_track or
                current_tracks_len != last_tracks_len):

                event_data = {
                    "id": task["id"],
                    "status": task["status"],
                    "progress": task["progress"],
                    "total_tracks": task["total_tracks"],
                    "completed_tracks": task["completed_tracks"],
                    "current_track": task["current_track"],
                    "folder_name": task.get("folder_name", ""),
                    "type": task["type"],
                    "tracks": current_tracks,
                    "errors": task["errors"][-3:],
                    "logs": task["logs"][-5:],
                    "file_url": task.get("file_url", ""),
                    "zip_url": task.get("zip_url", ""),
                }
                yield f"data: {json.dumps(event_data)}\n\n"

                last_progress = current_progress
                last_status = current_status
                last_log_count = current_log_count
                last_current_track = current_track_val
                last_tracks_len = current_tracks_len

            if current_status in ("completed", "error", "cancelled"):
                break

            time.sleep(0.5)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/queue")
def get_queue():
    """Get all tasks for the current user."""
    user_id = session.get("user_id", "")
    with queue_lock:
        tasks = []
        for task in download_queue.values():
            if task.get("user_id") != user_id:
                continue
            tasks.append({
                "id": task["id"],
                "url": task["url"],
                "type": task["type"],
                "status": task["status"],
                "progress": task["progress"],
                "total_tracks": task["total_tracks"],
                "completed_tracks": task["completed_tracks"],
                "current_track": task["current_track"],
                "folder_name": task.get("folder_name", ""),
                "tracks": task.get("tracks", []),
                "created_at": task["created_at"],
                "errors": task["errors"][-3:],
                "file_url": task.get("file_url", ""),
                "zip_url": task.get("zip_url", ""),
            })
    return jsonify(tasks)


@app.route("/api/queue/<task_id>", methods=["DELETE"])
def cancel_download(task_id):
    """Cancel a download task (own user only)."""
    user_id = session.get("user_id", "")
    with queue_lock:
        task = download_queue.get(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404
        if task.get("user_id") != user_id:
            return jsonify({"error": "Unauthorized"}), 403

        if task["status"] == "downloading" and task.get("pid"):
            try:
                import signal
                os.kill(task["pid"], signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass

        task["status"] = "cancelled"

    return jsonify({"status": "cancelled"})


@app.route("/api/downloads/<path:filename>")
def download_file(filename):
    """Serve a downloaded file, then delete it from the server."""
    safe_path = DOWNLOADS_DIR / filename
    if not safe_path.exists() or not safe_path.is_file():
        return jsonify({"error": "File not found"}), 404

    response = send_file(safe_path, as_attachment=True, download_name=safe_path.name)

    # Delete the file after serving it to the user
    try:
        safe_path.unlink(missing_ok=True)
        parent = safe_path.parent
        if parent != DOWNLOADS_DIR:
            try:
                parent.rmdir()
            except OSError:
                pass
        # If it's a ZIP, also remove the original playlist folder
        if safe_path.suffix == ".zip":
            folder_name = safe_path.stem
            folder_path = DOWNLOADS_DIR / folder_name
            if folder_path.exists():
                shutil.rmtree(folder_path, ignore_errors=True)
    except Exception:
        pass

    return response


@app.route("/health")
def health():
    return {"status": "ok"}


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  [*] SpotFetch is running!")
    print(f"  [>] Downloads folder: {DOWNLOADS_DIR}")
    print(f"  [>] Open http://localhost:{port} in your browser\n")
    app.run(host="0.0.0.0", port=port, threaded=True)
