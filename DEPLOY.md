# Deploying to Render (Starter, $7/mo flat)

This walks you from zero to a live URL. No local tooling required — everything is
done in the browser (GitHub + Render + ArcGIS). About 15 minutes.

## What you're deploying

A single web service that serves the page **and** does the conversion, all on
one origin — so there's nothing else to host and no CORS to configure. It's
**stateless**: your ArcGIS sign-in is kept in an encrypted, http-only cookie, not
on the server, so restarts and redeploys never sign you out.

Flat cost: **$7/month** on Render Starter. It does not autoscale into a bigger
bill — under heavy load it gets slower, it doesn't get more expensive. (See
"If you outgrow 512 MB" at the end.)

---

## Step 1 — Put the code on GitHub

Create a new GitHub repo and upload these files (keep the structure):

```
app.py
requirements.txt
render.yaml
Dockerfile            (optional fallback; not used by the default setup)
static/index.html
```

## Step 2 — Register an ArcGIS OAuth application

1. Sign in to ArcGIS Online → **Content → New item → Developer credentials**
   (or **Add item → An application**) and register an **OAuth 2.0** app.
2. Copy the **Client ID** and **Client Secret**. You'll paste them into Render.
3. Leave a redirect URI for now — you'll add the real one in Step 4, once you
   know your Render URL.

## Step 3 — Create the Render service

1. In Render: **New → Blueprint**, and select your GitHub repo. Render reads
   `render.yaml` and proposes the `ppt2briefing` web service on the **Starter**
   plan. Approve it.
2. When prompted, fill the three secret env vars:
   - `ARCGIS_CLIENT_ID` — from Step 2
   - `ARCGIS_CLIENT_SECRET` — from Step 2
   - `ARCGIS_REDIRECT_URI` — leave blank or put a placeholder for now
   (`APP_SECRET_KEY` is generated for you and `ARCGIS_PORTAL` is preset.)
3. Let it build and deploy. The build installs `arcgis` with a **minimal
   dependency set** (no numpy/pandas — see "Minimal install" below), so it's much
   lighter than a default `pip install arcgis`. When it's done you'll get a URL
   like `https://ppt2briefing.onrender.com`. **Treat your first real conversion as
   a validation run** (see "Minimal install").

## Step 4 — Connect the redirect URI

1. Your callback URL is your Render URL + `/oauth/callback`, e.g.
   `https://ppt2briefing.onrender.com/oauth/callback`.
2. Add that **exact** URL to your ArcGIS app's **Redirect URIs**.
3. In Render, set `ARCGIS_REDIRECT_URI` to the same value and save. Render
   redeploys automatically.

## Step 5 — Use it

Open your Render URL, click **Sign in with ArcGIS**, authorize on Esri's page,
then upload a `.pptx`. The console streams progress slide by slide, and gives you
links to open the new briefing when it's done.

---

## Costs

- **$7/month**, flat. Bandwidth beyond 100 GB/mo is billed, but this app sends
  very little to users (small page; uploads come *in* for free; briefing images
  go to ArcGIS, not back through Render), so in practice you stay at $7.
- No usage-based surprises: the single Starter instance can't autoscale into a
  larger bill on its own.

## Minimal install (why the footprint is small, and the one thing to check)

`arcgis` by default drags in a heavy scientific stack (numpy, pandas, scipy,
shapely, pyproj) that this app never uses — it only signs in and creates a
briefing. So the build installs `arcgis` with `--no-deps` plus Esri's documented
*minimum* dependency set (the lightweight `requests-*` helpers). That's the
difference between a multi-hundred-MB environment and a lean one that fits
512 MB comfortably, in both disk and RAM.

The one caveat: Esri documents the minimal set as sufficient for connecting and
**content-management** tasks (which is what creating a briefing is), but the
StoryMaps/Briefings module is higher-level, so there's a small chance it reaches
for a trimmed dependency. If that happens, the conversion console shows a clear
message like:

> Missing Python dependency 'X'. The minimal ArcGIS install doesn't include it.
> Add 'X' to requirements.txt and redeploy.

The fix is exactly that: add the named package to `requirements.txt`, commit,
and Render redeploys. That's why the **first real conversion is your validation
run**. (If you'd rather not iterate, the fallback below switches to the full
install in one line.)

### Fallback: the full install

If you hit missing-dependency messages and don't want to chase them, revert to
the complete dependency tree: in `requirements.txt` replace the minimal block
with a single `arcgis>=2.3,<2.5` line, and in `render.yaml` change the build
command back to just `pip install -r requirements.txt`. It's heavier on the
512 MB instance but needs nothing else.

## The 512 MB caveat (read this)

Render Starter has **512 MB RAM**. The minimal install above keeps the baseline
low, and the app loads `arcgis` only during a conversion and runs one build at a
time — so typical decks should be fine. A very large deck (many high-res images)
could still hit the ceiling. The symptom is the service restarting mid-build, or
"Out of memory" / "Exited with status 137" in Render's **Logs**.

### If you outgrow 512 MB

The code is host-agnostic, so you don't rewrite anything:

- **Easiest:** in Render, bump the service to **Standard** (2 GB RAM, $25/mo).
  One dropdown. (Above your budget, but it's there if you need it.)
- **Cheapest with more power:** run the included `Dockerfile` on a small VPS —
  e.g. a Hetzner CX22 is ~€4/mo with **4 GB RAM**. `docker build` then
  `docker run -p 80:8080 --env-file .env ...` with the same env vars, and put
  Caddy or your host's proxy in front for HTTPS. More hands-on, but cheaper than
  Standard and far more headroom. Update `ARCGIS_REDIRECT_URI` to the new domain.

## Local testing (optional)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install --no-deps "arcgis>=2.3,<2.5"
export ARCGIS_CLIENT_ID=... ARCGIS_CLIENT_SECRET=...
export ARCGIS_REDIRECT_URI=http://localhost:5000/oauth/callback
export APP_SECRET_KEY=$(python -c 'import secrets;print(secrets.token_hex(32))')
python app.py     # http://localhost:5000
```

Add `http://localhost:5000/oauth/callback` to your ArcGIS app's redirect URIs to
test sign-in locally.

## Notes / limitations

- **`.pptx` only** — the legacy binary `.ppt` isn't supported; Save As `.pptx`.
- **Layout isn't reproduced** — you get each slide's title, text, notes, and
  images as briefing content blocks in reading order, to arrange in the builder.
  Animations, transitions, video/audio, charts-as-objects, and SmartArt don't map.
- **Version-sensitive lines:** Esri doesn't document the Briefings data model, so
  the slide-building (`build_briefing_stream` / `_add_slide` in `app.py`) and the
  `GIS(PORTAL, token=...)` call adapt across `arcgis` versions and are the spots
  to adjust if a release shifts those APIs.
- **MFA/SAML accounts** sign in through the same OAuth page — no special handling.
