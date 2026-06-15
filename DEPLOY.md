<!-- Build marker 2026-06-15a — forces a fresh commit so GitHub overwrites the old file and Render redeploys (no functional effect). -->
# Deploying to Render (Starter, $7/mo flat)

This walks you from zero to a live URL. No local tooling required — everything is
done in the browser (GitHub + Render). About 10 minutes.

## What you're deploying

A single web service that serves the page **and** does the conversion, all on
one origin — so there's nothing else to host and no CORS to configure. It's
**stateless**: your ArcGIS credentials are kept in an encrypted, http-only cookie
for the session (never in plain text, never on the server), so restarts and
redeploys don't sign you out.

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

## Step 2 — Create the Render service

1. In Render: **New → Blueprint**, and select your GitHub repo. Render reads
   `render.yaml` and proposes the `ppt2briefing` web service on the **Starter**
   plan. Approve it. There are no secrets to fill in — `APP_SECRET_KEY` is
   generated for you, and `ARCGIS_PORTAL`/`SESSION_HOURS` are preset.
2. Let it build and deploy. The build runs `pip install -r requirements.txt`,
   which installs `arcgis` and all its dependencies. The first build pulls the
   full scientific stack (numpy/pandas/etc.), so give it a few minutes. When it's
   done you'll get a URL like `https://ppt2briefing.onrender.com`.

> Keep `APP_SECRET_KEY` stable. It encrypts the session cookie that holds your
> credentials; if you rotate it, everyone is signed out (which is a fine way to
> force re-login, just know that's the effect).

## Step 3 — Use it

Open your Render URL, sign in with your **ArcGIS username and password**, then
upload a `.pptx`. The console streams progress slide by slide and gives you links
to open the new briefing when it's done.

> Sign-in works for plain ArcGIS accounts only. If your organization uses single
> sign-on (SSO/SAML) or enforces multi-factor authentication, username/password
> can't generate a token — those accounts need OAuth instead.

---

## Costs

- **$7/month**, flat. Bandwidth beyond 100 GB/mo is billed, but this app sends
  very little to users (small page; uploads come *in* for free; briefing images
  go to ArcGIS, not back through Render), so in practice you stay at $7.
- No usage-based surprises: the single Starter instance can't autoscale into a
  larger bill on its own.

## Dependencies: full install (and the lean alternative)

The build uses the full `arcgis` install (`pip install -r requirements.txt`),
which pulls `arcgis` plus all its dependencies, including the scientific stack
(numpy, pandas, etc.). This app doesn't use that stack directly, but installing
it in full is the reliable path: it resolves every dependency `arcgis` needs in
one step.

We originally tried a "minimal" install (`pip install --no-deps arcgis` plus a
hand-picked dependency list) to save memory. In practice it kept surfacing
*undocumented* missing modules one at a time — `truststore`, `cachetools`,
`puremagic`, and others — because Esri's published minimal list is incomplete
for the StoryMaps/Briefings path. The full install avoids that entirely.

**Lean alternative (optional, more upkeep):** if you want the smaller footprint
back, change the build to `pip install --no-deps "arcgis>=2.3,<2.5"` after the
normal install, and add the lightweight deps `arcgis` needs — start with
`truststore cachetools puremagic ujson six requests-toolbelt requests-oauthlib`
and add any further module the app names in its error message. The app reports
missing modules clearly (e.g. "Missing Python dependency 'X'… add 'X' to
requirements.txt"), so you can chase them down — but expect a few redeploys.

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
  Standard and far more headroom.

## Local testing (optional)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export APP_SECRET_KEY=$(python -c 'import secrets;print(secrets.token_hex(32))')
python app.py     # http://localhost:5000
```

Open `http://localhost:5000` and sign in with your ArcGIS username and password.

## Notes / limitations

- **`.pptx` only** — the legacy binary `.ppt` isn't supported; Save As `.pptx`.
- **Layout isn't reproduced** — you get each slide's title, text, notes, and
  images as briefing content blocks in reading order, to arrange in the builder.
  Animations, transitions, video/audio, charts-as-objects, and SmartArt don't map.
- **Version-sensitive lines:** Esri doesn't document the Briefings data model, so
  the slide-building (`build_briefing_stream` / `_add_slide` in `app.py`) adapts
  across `arcgis` versions and is the spot to adjust if a release shifts the API.
- **SSO/SAML/MFA accounts** can't use username/password — they need OAuth. If your
  org enforces those, revert to the OAuth build instead.
