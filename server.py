#!/usr/bin/env python3
"""
Authenova local server (standard library only, no pip installs needed).

Serves the Authenova frontend at http://127.0.0.1:5001/ and provides:

  POST /api/media/verify-image   -> Hive AI-generated & deepfake detection
                                     (same request as test_hive.py, key from .env)
  POST /api/media/verify-video   -> the same Hive detector, run on video frames
                                     (stands in for the InceptionV3 video model,
                                     which lives in the original backend)

  POST /api/claims/investigate   -> Claim Intelligence engine (claim_engine.py):
                                     Groq query planning, Tavily live search,
                                     SBERT ranking, DeBERTa NLI stance

Run:   python server.py      (or double-click run.bat on Windows)
"""

import base64
import json
import mimetypes
import os
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import claim_engine

# Local: 127.0.0.1:5001. On Render (or any host that sets PORT): 0.0.0.0:$PORT
ON_CLOUD = bool(os.environ.get("PORT") or os.environ.get("RENDER"))
HOST = os.environ.get("HOST", "0.0.0.0" if ON_CLOUD else "127.0.0.1")
PORT = int(os.environ.get("PORT", "5001"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
HIVE_URL = "https://api.thehive.ai/api/v3/hive/ai-generated-and-deepfake-content-detection"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

# Explicit types so Windows registry settings can't break CSS/JS loading
FIXED_TYPES = {
    ".html": "text/html", ".css": "text/css", ".js": "application/javascript",
    ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".ico": "image/x-icon",
}

# Same meta classes test_hive.py excludes from generator attribution
EXCLUDED_CLASSES = {
    "ai_generated", "not_ai_generated", "deepfake",
    "not_ai_generated_audio", "ai_generated_audio",
    "inconclusive", "inconclusive_video", "none",
}


# ---------------------------------------------------------------------------
# .env loading (same behaviour as test_hive.py)
# ---------------------------------------------------------------------------
def load_env():
    """Read keys from .env, then fill any missing ones from .env.txt
    (Notepad often saves ".env" as ".env.txt"). BOM-safe."""
    for name in (".env", ".env.txt"):
        path = os.path.join(BASE_DIR, name)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip().lstrip("\ufeff")
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip().strip("'\"")
                if key and val and not os.environ.get(key):
                    os.environ[key] = val


def get_api_key():
    key = os.environ.get("HIVE_API_KEY", "").strip()
    if not key or key == "your_hive_api_key_here":
        return None
    return key


# ---------------------------------------------------------------------------
# Hive call + response shaping (logic mirrors test_hive.py)
# ---------------------------------------------------------------------------
def extract_classes(data):
    if isinstance(data, dict):
        if "classes" in data and isinstance(data["classes"], list):
            return data["classes"]
        for val in data.values():
            found = extract_classes(val)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = extract_classes(item)
            if found:
                return found
    return []


def all_class_lists(data, out=None):
    """Collect every 'classes' list (videos return one per analyzed frame)."""
    if out is None:
        out = []
    if isinstance(data, dict):
        if "classes" in data and isinstance(data["classes"], list):
            out.append(data["classes"])
        else:
            for val in data.values():
                all_class_lists(val, out)
    elif isinstance(data, list):
        for item in data:
            all_class_lists(item, out)
    return out


class UserFacingError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def call_hive(api_key, *, image_bytes=None, image_url=None, timeout=120):
    if image_url:
        input_item = {"media_url": image_url}
    else:
        input_item = {"media_base64": base64.b64encode(image_bytes).decode("utf-8")}

    payload = {"input": [input_item], "processing_mode": "sync_with_fallback"}
    req = urllib.request.Request(
        HIVE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Hive-Dedicated-Detector/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"[hive] HTTP {e.code}: {detail[:500]}", file=sys.stderr)
        if e.code in (401, 403):
            raise UserFacingError("The detection service rejected the Hive key. Check the key in the settings file next to server.py.", 502)
        if e.code == 429:
            raise UserFacingError("The detection service is rate limiting requests. Wait a moment and try again.", 429)
        if e.code == 400:
            raise UserFacingError("This file could not be processed. Try a different file.", 400)
        raise UserFacingError("The detection service returned an error. Please try again.", 502)
    except urllib.error.URLError as e:
        print(f"[hive] connection error: {e.reason}", file=sys.stderr)
        raise UserFacingError("Could not reach the detection service. Check your internet connection.", 502)
    except (TimeoutError, OSError) as e:
        print(f"[hive] error: {e}", file=sys.stderr)
        raise UserFacingError("The detection service took too long to respond. Please try again.", 504)


def shape_result(hive_json):
    classes = extract_classes(hive_json)
    ai_score = not_ai_score = deepfake_score = None
    sources = []
    for item in classes:
        name = str(item.get("class", "")).strip().lower()
        score = item.get("value", item.get("score", 0.0)) or 0.0
        if name == "ai_generated":
            ai_score = score
        elif name == "not_ai_generated":
            not_ai_score = score
        elif name == "deepfake":
            deepfake_score = score
        elif name not in EXCLUDED_CLASSES:
            sources.append((name, score))

    if ai_score is None and not_ai_score is None:
        raise UserFacingError("The detection service returned no result for this image. Try another image.", 502)

    sources.sort(key=lambda x: x[1], reverse=True)
    ai = ai_score or 0.0
    authentic = not_ai_score if not_ai_score is not None else max(0.0, 1.0 - ai)
    deepfake = deepfake_score or 0.0

    # Same threshold as test_hive.py (>= 0.5), also applied to the deepfake score
    is_manipulated = ai >= 0.5 or deepfake >= 0.5
    if ai >= 0.5:
        verdict = "AI-Generated / Manipulated"
    elif deepfake >= 0.5:
        verdict = "Deepfake / Manipulated"
    else:
        verdict = "Authentic Media"

    pct = lambda v: round(float(v) * 100, 2)
    return {
        "verdict": verdict,
        "is_manipulated": is_manipulated,
        "scores": {
            "authentic_percent": pct(authentic),
            "ai_generated_percent": pct(ai),
            "deepfake_percent": pct(deepfake),
        },
        "sources": [{"name": n, "score": pct(s)} for n, s in sources[:3]] if is_manipulated else [],
    }


def shape_video_result(hive_json, filename):
    frames = []
    for classes in all_class_lists(hive_json):
        scores = {str(c.get("class", "")).strip().lower(): (c.get("value", c.get("score", 0.0)) or 0.0) for c in classes}
        if "ai_generated" in scores or "deepfake" in scores:
            frames.append(scores)
    if not frames:
        raise UserFacingError("The detection service returned no result for this video. Try a clearer or longer clip.", 502)

    n = len(frames)
    avg_ai = sum(f.get("ai_generated", 0.0) for f in frames) / n
    avg_df = sum(f.get("deepfake", 0.0) for f in frames) / n
    flagged = sum(1 for f in frames if f.get("ai_generated", 0.0) >= 0.5 or f.get("deepfake", 0.0) >= 0.5)

    # Same 0.5 threshold as the image check, applied to the per-frame averages
    is_manipulated = avg_ai >= 0.5 or avg_df >= 0.5
    if avg_ai >= 0.5:
        verdict = "AI-Generated / Manipulated Video"
    elif avg_df >= 0.5:
        verdict = "Deepfake / Manipulated Video"
    else:
        verdict = "Authentic Video"

    pct = lambda v: round(float(v) * 100, 1)
    frame_word = "frame" if n == 1 else "frames"
    prediction = (f"Flagged in {flagged} of {n} analyzed {frame_word}. "
                  f"Average AI-generated {pct(avg_ai)}%, deepfake {pct(avg_df)}%.")
    return {
        "verdict": verdict,
        "is_manipulated": is_manipulated,
        "filename": filename,
        "prediction": prediction,
        "scores": {
            "ai_generated_percent": pct(avg_ai),
            "deepfake_percent": pct(avg_df),
            "authentic_percent": pct(max(0.0, 1.0 - max(avg_ai, avg_df))),
        },
        "frames_analyzed": n,
        "frames_flagged": flagged,
    }


def read_multipart_parts(ctype, body):
    """Return list of (field_name, bytes, filename) for every part."""
    msg = BytesParser(policy=email_policy).parsebytes(
        b"Content-Type: " + ctype.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    parts = []
    if msg.is_multipart():
        for part in msg.iter_parts():
            parts.append((part.get_param("name", header="content-disposition"),
                          part.get_payload(decode=True) or b"", part.get_filename()))
    return parts


def read_multipart_file(ctype, body):
    msg = BytesParser(policy=email_policy).parsebytes(
        b"Content-Type: " + ctype.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    if msg.is_multipart():
        for part in msg.iter_parts():
            if part.get_param("name", header="content-disposition") == "file":
                return part.get_payload(decode=True), (part.get_filename() or "video")
    return None, None


class ExclusiveServer(ThreadingHTTPServer):
    """Refuse to share the port. On Windows, SO_REUSEADDR lets a second copy
    bind the same port silently, so requests can land on an old server."""
    allow_reuse_address = (os.name != "nt")
    daemon_threads = True

    def server_bind(self):
        import socket
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "Authenova/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---- helpers ----
    def send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        if not os.path.isfile(path):
            self.send_error(404, "Not found")
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = FIXED_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD_BYTES:
            raise UserFacingError("That file is too large. Use a file under 25 MB.", 413)
        return self.rfile.read(length) if length > 0 else b""

    # ---- routes ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.send_file(os.path.join(STATIC_DIR, "index.html"))
        if path.startswith("/static/"):
            rel = os.path.normpath(path[len("/static/"):]).lstrip("\\/")
            full = os.path.abspath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(os.path.abspath(STATIC_DIR) + os.sep):
                return self.send_error(403, "Forbidden")
            return self.send_file(full)
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self.send_error(404, "Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/media/verify-image":
                return self.verify_image()
            if path == "/api/claims/investigate":
                return self.investigate_claim()
            if path == "/api/media/verify-video":
                return self.verify_video()
            self.send_json(404, {"error": "Not found"})
        except UserFacingError as e:
            self.send_json(e.status, {"error": e.message})
        except Exception as e:  # never leak internals to the browser
            print(f"[server] unexpected error: {e!r}", file=sys.stderr)
            self.send_json(500, {"error": "Something went wrong while processing your request."})

    def investigate_claim(self):
        try:
            data = json.loads(self.read_body().decode("utf-8") or "{}")
        except ValueError:
            raise UserFacingError("The request couldn't be read. Please try again.", 400)
        try:
            result = claim_engine.investigate(str(data.get("claim", "")), data.get("max_results", 4))
        except claim_engine.ClaimError as e:
            raise UserFacingError(e.message, e.status)
        self.send_json(200, result)

    def verify_video(self):
        api_key = get_api_key()
        if not api_key:
            raise UserFacingError("Video verification isn't set up yet. Add your Hive key to the settings file next to server.py.", 500)
        ctype = self.headers.get("Content-Type", "")
        body = self.read_body()
        if not ctype.startswith("multipart/form-data"):
            raise UserFacingError("No video was received. Choose a video file and try again.", 400)
        parts = read_multipart_parts(ctype, body)
        video = next(((d, f) for n, d, f in parts if n == "file" and d), (None, None))
        frames = [d for n, d, f in parts if n == "frame" and d][:8]
        filename = video[1] or "video"
        if not video[0] and not frames:
            raise UserFacingError("No video was received. Choose a video file and try again.", 400)

        if frames:
            # Reliable path: check still frames grabbed by the browser with the image detector
            from concurrent.futures import ThreadPoolExecutor
            def one(img):
                try:
                    return call_hive(api_key, image_bytes=img, timeout=90)
                except UserFacingError as e:
                    print(f"[video] frame failed: {e.message}", file=sys.stderr)
                    return None
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = [r for r in pool.map(one, frames) if r]
            if results:
                return self.send_json(200, shape_video_result({"frames": results}, filename))
            if not video[0]:
                raise UserFacingError("The detection service couldn't analyze this video's frames. Please try again.", 502)

        # Fallback: send the whole video
        try:
            hive = call_hive(api_key, image_bytes=video[0], timeout=240)
        except UserFacingError as e:
            if e.status >= 500 or e.status == 400:
                raise UserFacingError("The detection service couldn't process this video file. Try an MP4 clip in a different browser, or a shorter clip.", 502)
            raise
        self.send_json(200, shape_video_result(hive, filename))

    def verify_image(self):
        api_key = get_api_key()
        if not api_key:
            raise UserFacingError("Image verification isn't set up yet. Add your Hive key to the settings file next to server.py.", 500)

        ctype = self.headers.get("Content-Type", "")
        body = self.read_body()

        if ctype.startswith("multipart/form-data"):
            msg = BytesParser(policy=email_policy).parsebytes(
                b"Content-Type: " + ctype.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
            )
            file_bytes = None
            if msg.is_multipart():
                for part in msg.iter_parts():
                    if part.get_param("name", header="content-disposition") == "file":
                        file_bytes = part.get_payload(decode=True)
                        break
            if not file_bytes:
                raise UserFacingError("No image was received. Choose an image file and try again.", 400)
            hive = call_hive(api_key, image_bytes=file_bytes)
        else:
            try:
                data = json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                raise UserFacingError("The request couldn't be read. Please try again.", 400)
            url = str(data.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                raise UserFacingError("Enter a full image URL starting with http:// or https://.", 400)
            hive = call_hive(api_key, image_url=url)

        self.send_json(200, shape_result(hive))


def main():
    load_env()
    if not os.path.isfile(os.path.join(STATIC_DIR, "index.html")):
        print("Error: static/index.html not found next to server.py")
        sys.exit(1)
    try:
        httpd = ExclusiveServer((HOST, PORT), Handler)
    except OSError:
        print(f"Error: port {PORT} is already in use - Authenova is probably already running in another window.")
        print("Close the other Authenova window and run this again.")
        sys.exit(1)

    url = f"http://{HOST}:{PORT}/"
    print("=" * 56)
    print(" AUTHENOVA is running")
    print(f" Open: {url}")
    print(f" Media engine (Hive): {'ready' if get_api_key() else 'missing HIVE_API_KEY in .env'}")
    print(f" Claim engine: {claim_engine.status_line()}")
    print(" Press Ctrl+C to stop")
    print("=" * 56)
    claim_engine.warm_up_async()
    if "--no-browser" not in sys.argv and not ON_CLOUD:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
