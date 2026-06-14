"""
PPT -> ArcGIS StoryMaps Briefing converter (backend) — stateless build for Render.

Design goals for this deployment
--------------------------------
* Stateless: no server-side session store and no job store. The OAuth token
  bundle lives in an ENCRYPTED, httpOnly cookie, so any instance can serve any
  request and restarts/redeploys never log people out. (Render Starter is a
  single instance, but this also means a viral spike can add instances or the
  box can restart without breaking sessions.)
* Single-request streaming: the conversion runs inside one POST and streams
  newline-delimited JSON (NDJSON) progress back to the browser. No background
  threads (which don't survive serverless/PaaS request lifecycles) and no
  separate "job" endpoint to look up across instances.
* Memory-aware: `arcgis` is heavy and Render Starter has 512 MB. We import it
  lazily (only during a conversion) and serialize conversions with a semaphore
  so two heavy builds never run at once in the same instance.

Authentication: OAuth 2.0 authorization code flow. No passwords are collected.

Environment variables
---------------------
    ARCGIS_CLIENT_ID       (required)
    ARCGIS_CLIENT_SECRET   (required)
    ARCGIS_REDIRECT_URI    (required in prod, e.g. https://<your-app>.onrender.com/oauth/callback)
    ARCGIS_PORTAL          (default https://www.arcgis.com)
    APP_SECRET_KEY         (required in prod; signs the state cookie AND derives
                            the encryption key for the auth cookie — keep it stable)

Why a Python backend at all: Esri publishes no REST API / data model for
StoryMaps Briefings; the only supported way to build one programmatically is the
ArcGIS API for Python (arcgis.apps.storymap.Briefing), which runs server-side.
"""

import io
import os
import json
import time
import base64
import hashlib
import secrets
import tempfile
import threading
import traceback
import urllib.parse

import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import (
    Flask,
    request,
    jsonify,
    redirect,
    session as flask_session,
    send_from_directory,
    Response,
    make_response,
    stream_with_context,
)

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ.get("APP_SECRET_KEY") or secrets.token_hex(32)

# ---------------------------------------------------------------------------
# OAuth configuration
# ---------------------------------------------------------------------------

PORTAL = os.environ.get("ARCGIS_PORTAL", "https://www.arcgis.com").rstrip("/")
CLIENT_ID = os.environ.get("ARCGIS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("ARCGIS_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("ARCGIS_REDIRECT_URI", "http://localhost:5000/oauth/callback")

AUTHORIZE_URL = f"{PORTAL}/sharing/rest/oauth2/authorize"
TOKEN_URL = f"{PORTAL}/sharing/rest/oauth2/token"

# Requested refresh-token lifetime (minutes). Passing this also makes ArcGIS
# issue a refresh token. 20160 = 14 days.
REFRESH_EXPIRATION_MINUTES = 20160

AUTH_COOKIE = "p2b_auth"

# Only one heavy (arcgis) build at a time per instance, to stay within 512 MB.
_build_gate = threading.BoundedSemaphore(1)


def is_configured():
    return bool(CLIENT_ID and CLIENT_SECRET)


# ---------------------------------------------------------------------------
# Encrypted cookie session (stateless)
# ---------------------------------------------------------------------------

def _fernet():
    """Derive a stable Fernet key from APP_SECRET_KEY."""
    digest = hashlib.sha256(app.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def read_auth():
    """Decrypt the auth cookie into a bundle dict, or return None."""
    raw = request.cookies.get(AUTH_COOKIE)
    if not raw:
        return None
    try:
        bundle = json.loads(_fernet().decrypt(raw.encode()).decode())
    except (InvalidToken, ValueError):
        return None
    horizon = bundle.get("refresh_expires_at") or bundle.get("access_expires_at", 0)
    if horizon and horizon < time.time():
        return None
    return bundle


def write_auth(resp, bundle):
    token = _fernet().encrypt(json.dumps(bundle).encode()).decode()
    horizon = bundle.get("refresh_expires_at") or bundle.get("access_expires_at", 0)
    max_age = max(int(horizon - time.time()), 60) if horizon else 3600
    resp.set_cookie(
        AUTH_COOKIE, token,
        max_age=max_age, httponly=True, samesite="Lax",
        secure=request.is_secure,
    )


def clear_auth(resp):
    resp.delete_cookie(AUTH_COOKIE)


# ---------------------------------------------------------------------------
# Token exchange / refresh
# ---------------------------------------------------------------------------

def _exchange_code_for_token(code):
    return requests.post(TOKEN_URL, data={
        "f": "json",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
        "code": code,
    }, timeout=30).json()


def _bundle_from_token_response(data):
    now = time.time()
    refresh_exp = (now + int(data["refresh_token_expires_in"])) \
        if data.get("refresh_token_expires_in") else 0.0
    return {
        "username": data.get("username", "ArcGIS user"),
        "access_token": data["access_token"],
        "access_expires_at": now + int(data.get("expires_in", 1800)),
        "refresh_token": data.get("refresh_token", ""),
        "refresh_expires_at": refresh_exp,
    }


def ensure_fresh(bundle):
    """
    If the access token is expired/near expiry, refresh it. Returns
    (bundle, changed). Raises RuntimeError if a refresh is needed but impossible.
    """
    if bundle["access_expires_at"] - 60 > time.time():
        return bundle, False
    if not bundle.get("refresh_token") or bundle.get("refresh_expires_at", 0) < time.time():
        raise RuntimeError("SESSION_EXPIRED")
    data = requests.post(TOKEN_URL, data={
        "f": "json",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": bundle["refresh_token"],
    }, timeout=30).json()
    if "access_token" not in data:
        raise RuntimeError("SESSION_EXPIRED")
    now = time.time()
    bundle = dict(bundle)
    bundle["access_token"] = data["access_token"]
    bundle["access_expires_at"] = now + int(data.get("expires_in", 1800))
    if data.get("refresh_token"):
        bundle["refresh_token"] = data["refresh_token"]
        bundle["refresh_expires_at"] = now + int(data.get("refresh_token_expires_in", 0))
    return bundle, True


def get_gis(bundle):
    """Build an arcgis GIS from the OAuth access token (lazy import: heavy)."""
    from arcgis.gis import GIS
    # `token=` is the version-sensitive line; adjust here if a future arcgis
    # build changes how an existing OAuth token is accepted.
    return GIS(PORTAL, token=bundle["access_token"])


# ---------------------------------------------------------------------------
# Routes: static page + config probe
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/config")
def config():
    return jsonify(configured=is_configured(), portal=PORTAL, redirect_uri=REDIRECT_URI)


@app.route("/healthz")
def healthz():
    return "ok", 200


# ---------------------------------------------------------------------------
# Routes: OAuth
# ---------------------------------------------------------------------------

@app.route("/api/login")
def login():
    if not is_configured():
        return jsonify(ok=False, error="OAuth isn't configured. Set ARCGIS_CLIENT_ID "
                                       "and ARCGIS_CLIENT_SECRET (see DEPLOY.md)."), 500
    state = secrets.token_urlsafe(24)
    flask_session["oauth_state"] = state
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "expiration": REFRESH_EXPIRATION_MINUTES,
    }
    return redirect(AUTHORIZE_URL + "?" + urllib.parse.urlencode(params))


@app.route("/oauth/callback")
def oauth_callback():
    err = request.args.get("error")
    if err:
        return redirect("/?auth_error=" + urllib.parse.quote(
            request.args.get("error_description", err)))

    state = request.args.get("state")
    if not state or state != flask_session.pop("oauth_state", None):
        return redirect("/?auth_error=" + urllib.parse.quote(
            "Sign-in could not be verified (state mismatch). Please try again."))

    code = request.args.get("code")
    if not code:
        return redirect("/?auth_error=" + urllib.parse.quote("No authorization code returned."))

    data = _exchange_code_for_token(code)
    if "access_token" not in data:
        if isinstance(data.get("error"), dict):
            msg = data["error"].get("message", "Token exchange failed.")
        else:
            msg = data.get("error_description") or "Token exchange failed."
        return redirect("/?auth_error=" + urllib.parse.quote(str(msg)))

    resp = make_response(redirect("/"))
    write_auth(resp, _bundle_from_token_response(data))
    return resp


@app.route("/api/session")
def session_status():
    bundle = read_auth()
    if not bundle:
        return jsonify(ok=True, signed_in=False)
    horizon = bundle.get("refresh_expires_at") or bundle["access_expires_at"]
    return jsonify(ok=True, signed_in=True, username=bundle["username"],
                   portal=PORTAL, expires_at=horizon, seconds_left=int(horizon - time.time()))


@app.route("/api/logout", methods=["POST"])
def logout():
    resp = make_response(jsonify(ok=True))
    clear_auth(resp)
    return resp


# ---------------------------------------------------------------------------
# PPTX parsing
# ---------------------------------------------------------------------------

def parse_pptx(file_bytes, workdir):
    """Extract per-slide title, body text, speaker notes, and images."""
    from pptx import Presentation

    prs = Presentation(io.BytesIO(file_bytes))
    slides = []

    for s_idx, slide in enumerate(prs.slides):
        title = ""
        body = []
        images = []

        try:
            if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
                title = slide.shapes.title.text_frame.text.strip()
        except Exception:
            pass

        for shape in slide.shapes:
            if shape.has_text_frame:
                if slide.shapes.title is not None and shape == slide.shapes.title:
                    continue
                for para in shape.text_frame.paragraphs:
                    txt = "".join(run.text for run in para.runs).strip()
                    if not txt and para.text:
                        txt = para.text.strip()
                    if txt:
                        body.append(txt)

            if shape.shape_type == 13 or getattr(shape, "image", None) is not None:
                try:
                    image = shape.image
                    ext = (image.ext or "png").lower()
                    fpath = os.path.join(workdir, f"slide{s_idx + 1}_{secrets.token_hex(4)}.{ext}")
                    with open(fpath, "wb") as fh:
                        fh.write(image.blob)
                    images.append({"path": fpath, "ext": ext})
                except Exception:
                    continue

        notes = ""
        try:
            if slide.has_notes_slide:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
        except Exception:
            pass

        if not title and body:
            title = body[0][:120]

        slides.append({
            "index": s_idx,
            "title": title or f"Slide {s_idx + 1}",
            "body": body,
            "notes": notes,
            "images": images,
        })

    return slides


# ---------------------------------------------------------------------------
# Briefing construction (version-sensitive: the data model is undocumented)
# ---------------------------------------------------------------------------

def _resolve_storymap_imports():
    from arcgis.apps.storymap import Briefing
    from arcgis.apps.storymap import story_content as sc
    return {
        "Briefing": Briefing,
        "Text": getattr(sc, "Text"),
        "Image": getattr(sc, "Image"),
        "TextStyles": getattr(sc, "TextStyles", None),
        "BriefingSlide": getattr(sc, "BriefingSlide", None),
        "SlideLayout": getattr(sc, "SlideLayout", None),
    }


def _add_slide(briefing, api):
    BriefingSlide = api["BriefingSlide"]
    SlideLayout = api["SlideLayout"]
    layout = None
    if SlideLayout is not None:
        for name in ("SINGLE", "FULL", "TITLE_ONLY", "FULLSCREEN"):
            if hasattr(SlideLayout, name):
                layout = getattr(SlideLayout, name)
                break
    if hasattr(briefing, "add_slide"):
        try:
            return briefing.add_slide(slide_layout=layout) if layout is not None else briefing.add_slide()
        except TypeError:
            try:
                return briefing.add_slide(layout) if layout is not None else briefing.add_slide()
            except Exception:
                pass
    if BriefingSlide is not None and hasattr(briefing, "add"):
        slide = BriefingSlide(slide_layout=layout) if layout is not None else BriefingSlide()
        briefing.add(slide)
        return slide
    raise RuntimeError("No recognized slide-creation method on Briefing for this arcgis version.")


def _slide_add_content(slide, content):
    for attr in ("add_content", "add"):
        if hasattr(slide, attr):
            getattr(slide, attr)(content)
            return
    blocks = getattr(slide, "blocks", None)
    if blocks:
        for block in blocks:
            if hasattr(block, "add_content"):
                block.add_content(content)
                return
    raise RuntimeError("Could not add content to this slide object.")


def _log(message, level="info"):
    return {"type": "log", "level": level, "message": message}


def build_briefing_stream(gis, slides, briefing_title):
    """Generator: yields progress events while building the briefing."""
    api = _resolve_storymap_imports()
    Briefing, Text, Image = api["Briefing"], api["Text"], api["Image"]
    TextStyles = api["TextStyles"]

    yield _log("Creating a new briefing draft in your ArcGIS Online content…")
    briefing = Briefing(gis=gis)

    cover_title = briefing_title or (slides[0]["title"] if slides else "Imported Briefing")
    cover_sub = slides[0]["body"][0][:200] if (slides and slides[0]["body"]) else ""
    try:
        if hasattr(briefing, "cover"):
            briefing.cover(title=cover_title, summary=cover_sub)
            yield _log(f"Set cover slide: “{cover_title}”.")
    except Exception as e:
        yield _log(f"Could not set cover; continuing. ({type(e).__name__})", "warn")

    for ps in slides:
        yield _log(f"Building slide {ps['index'] + 1}: “{ps['title']}”")
        slide = _add_slide(briefing, api)

        try:
            if TextStyles is not None and hasattr(TextStyles, "HEADING"):
                _slide_add_content(slide, Text(text=ps["title"], style=TextStyles.HEADING))
            else:
                _slide_add_content(slide, Text(text=ps["title"]))
        except Exception as e:
            yield _log(f"  • title text skipped ({type(e).__name__})", "warn")

        for para in ps["body"]:
            try:
                _slide_add_content(slide, Text(text=para))
            except Exception:
                continue

        for img in ps["images"]:
            try:
                _slide_add_content(slide, Image(img["path"]))
                yield _log(f"  • uploaded image {os.path.basename(img['path'])}")
            except Exception as e:
                yield _log(f"  • image upload skipped ({type(e).__name__})", "warn")

        if ps["notes"]:
            try:
                _slide_add_content(slide, Text(text=f"Notes: {ps['notes']}"))
            except Exception:
                pass

    yield _log("Saving the briefing…")
    item = None
    try:
        item = briefing.save(title=cover_title, publish=False)
    except TypeError:
        item = briefing.save()
    except Exception as e:
        yield _log(f"save() raised {type(e).__name__}; the draft may still exist.", "warn")

    item_id = getattr(item, "id", None) if item else None
    portal = gis.url.rstrip("/")
    yield {
        "type": "end",
        "status": "done",
        "result": {
            "item_id": item_id,
            "title": cover_title,
            "slide_count": len(slides),
            "item_url": f"{portal}/home/item.html?id={item_id}" if item_id else None,
            "edit_url": f"https://storymaps.arcgis.com/briefings/{item_id}/edit" if item_id else None,
        },
    }


def convert_stream(bundle, file_bytes, briefing_title):
    """Top-level generator for one conversion. Yields progress + final event."""
    workdir = tempfile.mkdtemp(prefix="p2b_")
    try:
        yield _log("Reading the PowerPoint file…")
        slides = parse_pptx(file_bytes, workdir)
        if not slides:
            yield {"type": "end", "status": "error", "error": "No slides were found in the file."}
            return
        yield _log(f"Found {len(slides)} slide(s).")

        # Serialize heavy arcgis builds to protect 512 MB instances.
        if not _build_gate.acquire(timeout=0.5):
            yield _log("Another conversion is in progress; waiting for it to finish…", "warn")
            _build_gate.acquire()
        try:
            gis = get_gis(bundle)
            yield from build_briefing_stream(gis, slides, briefing_title)
        finally:
            _build_gate.release()
    except ModuleNotFoundError as e:
        # Minimal arcgis install: a needed dependency was trimmed. Name it so the
        # fix is obvious — add it to requirements.txt and redeploy.
        traceback.print_exc()
        yield {"type": "end", "status": "error",
               "error": f"Missing Python dependency '{e.name}'. The minimal ArcGIS install "
                        f"doesn't include it. Add '{e.name}' to requirements.txt and redeploy "
                        f"(see DEPLOY.md → 'Minimal install')."}
    except Exception as e:
        traceback.print_exc()
        yield {"type": "end", "status": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            for f in os.listdir(workdir):
                os.remove(os.path.join(workdir, f))
            os.rmdir(workdir)
        except Exception:
            pass


@app.route("/api/convert", methods=["POST"])
def convert():
    bundle = read_auth()
    if not bundle:
        return jsonify(ok=False, error="Your session has expired. Please sign in again."), 401

    if "file" not in request.files:
        return jsonify(ok=False, error="No file was uploaded."), 400
    upload = request.files["file"]
    if not (upload.filename or "").lower().endswith(".pptx"):
        return jsonify(ok=False, error="Please upload a .pptx file. The older binary .ppt "
                                       "format isn't supported — Save As .pptx first."), 400

    # Read the upload now (before streaming) and refresh the token if needed so
    # we can set the updated auth cookie on the streaming response's headers.
    file_bytes = upload.read()
    briefing_title = (request.form.get("title") or "").strip()
    try:
        bundle, _ = ensure_fresh(bundle)
    except RuntimeError:
        return jsonify(ok=False, error="Your sign-in has expired. Please sign in again."), 401

    @stream_with_context
    def gen():
        for event in convert_stream(bundle, file_bytes, briefing_title):
            yield json.dumps(event) + "\n"

    resp = Response(gen(), mimetype="application/x-ndjson")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"  # discourage proxy buffering
    write_auth(resp, bundle)                   # persist any refreshed token
    return resp


if __name__ == "__main__":
    if not is_configured():
        print("\n  WARNING: ARCGIS_CLIENT_ID / ARCGIS_CLIENT_SECRET not set — sign-in disabled.\n")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), threaded=True)
