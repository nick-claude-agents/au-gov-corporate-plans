"""
Agency Analysis Dashboard — local server
========================================

Serves the dashboard (index.html, briefs/) AND generates BD briefs on demand.

The dashboard is a static site, so it cannot run Claude itself. This small
Flask app sits behind it: when someone clicks "Generate BD Brief" for an agency
that doesn't have one yet, the dashboard POSTs to /api/brief and this server
runs the analyser (download plan -> web research -> structured brief), stores it
under briefs/, and returns its URL. Generation is serialised (one at a time),
which also keeps within API rate limits.

Run it:
    python brief_server.py          (then open http://127.0.0.1:8770)
or double-click run_brief_server.bat

Requires: flask (plus the analyser deps). Needs ANTHROPIC_API_KEY in .env.
"""

import json
import os
import threading

from flask import Flask, request, jsonify, send_from_directory, redirect

import analyse_corporate_plan as core
import manage_briefs as mb

core.load_env_file()

app = Flask(__name__, static_folder=str(mb.SCRIPT_DIR), static_url_path="")
_gen_lock = threading.Lock()          # serialise generation (cost + rate limit)
LOG = core.LOG


@app.route("/")
def home():
    return send_from_directory(mb.SCRIPT_DIR, "index.html")


@app.route("/api/brief", methods=["POST"])
def api_brief():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    portfolio = (data.get("portfolio") or "").strip()
    url = (data.get("url") or "").strip()
    if not name or not url:
        return jsonify(status="error", message="name and url are required"), 400

    slug = mb.slugify(name)
    brief_rel = f"briefs/{slug}.html"
    if (mb.BRIEFS_DIR / f"{slug}.html").exists():
        return jsonify(status="exists", url=brief_rel)

    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return jsonify(status="error",
                       message="ANTHROPIC_API_KEY is not set in .env"), 500

    # One generation at a time. A second click on another agency waits here,
    # which is exactly what we want on a low rate-limit tier.
    with _gen_lock:
        if (mb.BRIEFS_DIR / f"{slug}.html").exists():   # built while we waited
            return jsonify(status="exists", url=brief_rel)
        LOG.info("On-demand brief requested: %s", name)
        index = mb.load_index()
        try:
            analysis = mb.generate_one(name, portfolio, url, index)
        except Exception as e:                       # noqa: BLE001
            LOG.error("On-demand generation crashed for %s: %s", name, e)
            return jsonify(status="error", message=str(e)), 500
        if analysis is None:
            return jsonify(status="error",
                           message="Generation failed — see plan_analyser.log"), 502
        mb.save_index(index)

    return jsonify(status="generated", url=brief_rel,
                   confidence=analysis.get("overall_confidence", ""),
                   plan_period=analysis.get("plan_period", ""))


# Has a brief already been generated? (lets the dashboard show the right label)
@app.route("/api/brief-status")
def api_brief_status():
    name = (request.args.get("name") or "").strip()
    slug = mb.slugify(name)
    exists = (mb.BRIEFS_DIR / f"{slug}.html").exists()
    return jsonify(exists=exists, url=f"briefs/{slug}.html" if exists else None)


# Navigation target for the public dashboard's "Generate BD Brief" links.
# The public (HTTPS) page can't fetch this server, but it CAN link to it; this
# page is then same-origin, so it calls /api/brief itself and shows the result.
@app.route("/generate")
def generate_page():
    name = (request.args.get("name") or "").strip()
    portfolio = (request.args.get("portfolio") or "").strip()
    url = (request.args.get("url") or "").strip()
    slug = mb.slugify(name)
    if (mb.BRIEFS_DIR / f"{slug}.html").exists():
        return redirect(f"/briefs/{slug}.html")        # already done — just open it
    payload = json.dumps({"name": name, "portfolio": portfolio, "url": url})
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Generating BD Brief…</title>
<style>
  body {{ font-family: Arial, sans-serif; background:#f0f5f4; color:#222;
         display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
  .box {{ background:#fff; border:1px solid #ddd; border-radius:10px; padding:28px 34px;
         max-width:520px; text-align:center; }}
  h1 {{ color:#0D3D20; font-size:18px; margin:0 0 6px; }}
  .spin {{ width:34px; height:34px; border:4px solid #e3a9b0; border-top-color:#8b0030;
          border-radius:50%; margin:14px auto; animation:r 1s linear infinite; }}
  @keyframes r {{ to {{ transform:rotate(360deg); }} }}
  .muted {{ color:#666; font-size:13px; }}
  .err {{ color:#BF360C; font-size:13px; }}
</style></head>
<body><div class="box">
  <h1>Generating BD Brief</h1>
  <div class="muted">{name}</div>
  <div class="spin" id="spin"></div>
  <p class="muted" id="msg">Reading the corporate plan, searching news / ANAO /
     Parliament, and writing the brief. This can take up to 10 minutes — you can
     leave this tab open and come back.</p>
  <p class="err" id="err" style="display:none"></p>
</div>
<script>
  const body = {payload};
  fetch('/api/brief', {{ method:'POST', headers:{{'Content-Type':'application/json'}},
                        body: JSON.stringify(body) }})
    .then(r => r.json())
    .then(d => {{
      if ((d.status === 'generated' || d.status === 'exists') && d.url) {{
        location.href = '/' + d.url;
      }} else {{
        document.getElementById('spin').style.display='none';
        document.getElementById('msg').style.display='none';
        const e=document.getElementById('err'); e.style.display='block';
        e.textContent = 'Could not generate: ' + (d.message || 'unknown error');
      }}
    }})
    .catch(e => {{
      document.getElementById('spin').style.display='none';
      const er=document.getElementById('err'); er.style.display='block';
      er.textContent = 'Error: ' + e;
    }});
</script>
</body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("BRIEF_SERVER_PORT", "8770"))
    print(f"Agency Analysis Dashboard running at http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, threaded=True)
