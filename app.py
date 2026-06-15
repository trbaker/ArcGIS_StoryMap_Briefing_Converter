# Build marker 2026-06-15a — forces a fresh commit so GitHub overwrites the old file and Render redeploys (no functional effect).
"""
PPT -> ArcGIS StoryMaps Briefing converter (backend) — stateless build for Render.

Authentication: ArcGIS username + password
-------------------------------------------
The user signs in with their ArcGIS Online username and password. We validate the
credentials by signing in once, then keep them in an ENCRYPTED, http-only cookie
(Fernet, keyed off APP_SECRET_KEY) for the session — never in plain text, never on
the server. Each conversion re-authenticates from those credentials.

NOTE: username/password works only for plain ArcGIS accounts. Accounts that use
SSO/SAML or enforce multi-factor authentication cannot sign in this way (that is
what OAuth is for).

Design notes (unchanged from the OAuth build)
---------------------------------------------
* Stateless: no server-side session/job store. Any instance can serve any
  request; restarts/redeploys don't sign people out.
* Single-request streaming: the conversion runs inside one POST and streams
  newline-delimited JSON (NDJSON) progress to the browser. No background threads.
* Memory-aware: `arcgis` is heavy and Render Starter has 512 MB, so it's imported
  lazily and conversions are serialized with a semaphore.

Environment variables
---------------------
    APP_SECRET_KEY    (required in prod; derives the cookie-encryption key — keep stable)
    ARCGIS_PORTAL     (default https://www.arcgis.com; used as the default portal URL)
    SESSION_HOURS     (optional, default 4; how long a sign-in stays valid)

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

from cryptography.fernet import Fernet, InvalidToken
from flask import (
    Flask,
    request,
    jsonify,
    send_from_directory,
    Response,
    make_response,
    stream_with_context,
)

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ.get("APP_SECRET_KEY") or secrets.token_hex(32)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORTAL_DEFAULT = os.environ.get("ARCGIS_PORTAL", "https://www.arcgis.com").rstrip("/")
SESSION_TTL = float(os.environ.get("SESSION_HOURS", "4")) * 3600

AUTH_COOKIE = "p2b_auth"

# Only one heavy (arcgis) build at a time per instance, to stay within 512 MB.
_build_gate = threading.BoundedSemaphore(1)


# ---------------------------------------------------------------------------
# Encrypted cookie session (stateless) — carries the ArcGIS credentials
# ---------------------------------------------------------------------------

def _fernet():
    """Derive a stable Fernet key from APP_SECRET_KEY."""
    digest = hashlib.sha256(app.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _horizon(bundle):
    return bundle.get("signed_in_at", 0) + SESSION_TTL


def read_auth():
    """Decrypt the auth cookie into a credentials bundle, or return None."""
    raw = request.cookies.get(AUTH_COOKIE)
    if not raw:
        return None
    try:
        bundle = json.loads(_fernet().decrypt(raw.encode()).decode())
    except (InvalidToken, ValueError):
        return None
    if _horizon(bundle) < time.time():
        return None
    return bundle


def write_auth(resp, bundle):
    token = _fernet().encrypt(json.dumps(bundle).encode()).decode()
    max_age = max(int(_horizon(bundle) - time.time()), 60)
    resp.set_cookie(
        AUTH_COOKIE, token,
        max_age=max_age, httponly=True, samesite="Lax",
        secure=request.is_secure,
    )


def clear_auth(resp):
    resp.delete_cookie(AUTH_COOKIE)


# ---------------------------------------------------------------------------
# ArcGIS sign-in (lazy import: arcgis is heavy)
# ---------------------------------------------------------------------------

def login_gis(portal, username, password):
    """
    Authenticate to ArcGIS and return (gis, display_name).
    Raises ModuleNotFoundError if the minimal install is missing a dependency,
    or a generic Exception if the credentials/portal are invalid.
    """
    from arcgis.gis import GIS
    gis = GIS(portal, username, password)
    me = gis.users.me
    display = getattr(me, "fullName", None) or getattr(me, "username", username)
    return gis, display


def get_gis(bundle):
    """Re-authenticate for a conversion from the stored credentials."""
    gis, _ = login_gis(bundle["portal"], bundle["username"], bundle["password"])
    return gis


# ---------------------------------------------------------------------------
# Routes: static page + config probe
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/config")
def config():
    return jsonify(portal=PORTAL_DEFAULT, auth="password")


@app.route("/healthz")
def healthz():
    return "ok", 200


# ---------------------------------------------------------------------------
# Routes: authentication
# ---------------------------------------------------------------------------

@app.route("/api/auth", methods=["POST"])
def auth():
    data = request.get_json(force=True, silent=True) or {}
    portal = (data.get("portal_url") or PORTAL_DEFAULT).strip().rstrip("/")
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify(ok=False, error="Enter both a username and a password."), 400

    try:
        _, display = login_gis(portal, username, password)
    except ModuleNotFoundError as e:
        return jsonify(ok=False, error=f"Missing Python dependency '{e.name}'. The minimal "
                                       f"ArcGIS install doesn't include it. Add '{e.name}' to "
                                       f"requirements.txt and redeploy (see DEPLOY.md)."), 500
    except Exception as e:
        return jsonify(ok=False, error="Could not sign in. Check the portal URL, username, and "
                                       "password. Accounts that use SSO or multi-factor sign-in "
                                       f"can't use username/password. ({type(e).__name__})"), 401
    finally:
        password = None  # drop the plaintext reference promptly after this scope

    bundle = {
        "username": username,
        "password": data.get("password") or "",   # stored encrypted in the cookie below
        "portal": portal,
        "signed_in_at": time.time(),
    }
    resp = make_response(jsonify(ok=True, user=display, username=username,
                                 expires_at=_horizon(bundle)))
    write_auth(resp, bundle)
    return resp


@app.route("/api/session")
def session_status():
    bundle = read_auth()
    if not bundle:
        return jsonify(ok=True, signed_in=False)
    return jsonify(ok=True, signed_in=True, username=bundle["username"],
                   portal=bundle.get("portal", PORTAL_DEFAULT),
                   expires_at=_horizon(bundle), seconds_left=int(_horizon(bundle) - time.time()))


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
            yield _log("Signing in to ArcGIS…")
            gis = get_gis(bundle)
            yield from build_briefing_stream(gis, slides, briefing_title)
        finally:
            _build_gate.release()
    except ModuleNotFoundError as e:
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

    file_bytes = upload.read()
    briefing_title = (request.form.get("title") or "").strip()

    @stream_with_context
    def gen():
        for event in convert_stream(bundle, file_bytes, briefing_title):
            yield json.dumps(event) + "\n"

    resp = Response(gen(), mimetype="application/x-ndjson")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"  # discourage proxy buffering
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), threaded=True)
