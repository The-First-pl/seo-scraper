from __future__ import annotations

import os
import re
import threading
import uuid

from flask import (
    Flask, render_template, redirect, url_for,
    session, request, jsonify, send_file,
)
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from werkzeug.middleware.proxy_fix import ProxyFix

from audit_runner import run_audit
from gsc_client import list_properties
from blog_planner import generate_blog_plan, export_excel, make_filename
from company_audit_runner import run_company_audit
from company_excel_writer import make_company_filename

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-this-in-production")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # trust Railway proxy headers

# Allow HTTP only in local dev (Railway is always HTTPS)
if os.environ.get("FLASK_ENV") == "development":
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

# Railway / production: set these env vars
# Local dev: set GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + APP_URL=http://localhost:5000
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
APP_URL              = os.environ.get("APP_URL", "http://localhost:5000").rstrip("/")
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")

# In-memory task store  {task_id: {...}}
# NOTE: single-worker gunicorn only — use Redis for multi-worker production
_tasks: dict[str, dict] = {}

# ── OAuth helpers ─────────────────────────────────────────────────────────────

def _get_flow() -> Flow:
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        config = {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [f"{APP_URL}/auth/callback"],
            }
        }
        flow = Flow.from_client_config(config, scopes=SCOPES)
    else:
        # Fallback: credentials.json must be "web" type
        # (The "installed" type from Downloads won't work for web OAuth)
        flow = Flow.from_client_secrets_file("credentials.json", scopes=SCOPES)

    flow.redirect_uri = f"{APP_URL}/auth/callback"
    return flow


def _creds_to_dict(creds: Credentials) -> dict:
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else [],
    }

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", logged_in="credentials" in session)


@app.route("/auth/google")
def auth_google():
    shop_url = request.args.get("shop_url", "").strip()
    if shop_url:
        session["shop_url"] = shop_url

    flow = _get_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="select_account consent",
    )
    session["oauth_state"] = state
    # Persist PKCE code_verifier — Flow generates it during authorization_url()
    # and needs the same value in fetch_token(); a new Flow instance won't have it.
    session["oauth_code_verifier"] = getattr(flow, "code_verifier", None)
    return redirect(auth_url)


@app.route("/auth/callback")
def auth_callback():
    flow = _get_flow()
    # Restore PKCE code_verifier so fetch_token can send it to Google's token endpoint.
    # Without this, a freshly created Flow has no verifier and Google returns invalid_grant.
    if session.get("oauth_code_verifier"):
        flow.code_verifier = session.pop("oauth_code_verifier")

    # Ensure HTTPS in the authorization_response on Railway
    callback_url = request.url.replace("http://", "https://") \
        if APP_URL.startswith("https://") else request.url

    try:
        flow.fetch_token(authorization_response=callback_url)
    except Exception as e:
        return render_template("error.html", message=f"Błąd autoryzacji Google: {e}"), 400

    session["credentials"] = _creds_to_dict(flow.credentials)
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    if "credentials" not in session:
        return redirect(url_for("index"))

    try:
        properties = list_properties(session["credentials"])
    except Exception as e:
        properties = []
        session["gsc_error"] = str(e)

    shop_url   = session.get("shop_url", "")
    gsc_error  = session.pop("gsc_error", None)
    api_key_set = bool(ANTHROPIC_API_KEY)

    return render_template(
        "dashboard.html",
        properties=properties,
        shop_url=shop_url,
        gsc_error=gsc_error,
        api_key_set=api_key_set,
    )


@app.route("/generate", methods=["POST"])
def generate():
    if "credentials" not in session:
        return redirect(url_for("index"))

    shop_url = request.form.get("shop_url", "").strip()
    site_url = request.form.get("site_url", "").strip()
    api_key  = request.form.get("api_key", "").strip() or ANTHROPIC_API_KEY
    mode     = request.form.get("mode", "ecommerce")

    if not shop_url:
        session["gsc_error"] = "Podaj URL strony."
        return redirect(url_for("dashboard"))

    if not api_key:
        session["gsc_error"] = "Podaj klucz Anthropic API."
        return redirect(url_for("dashboard"))

    # For e-commerce mode, GSC property is required
    if mode == "ecommerce" and not site_url:
        session["gsc_error"] = "Wybierz property GSC."
        return redirect(url_for("dashboard"))

    task_id    = uuid.uuid4().hex[:8]
    creds_data = dict(session["credentials"])

    _tasks[task_id] = {
        "type":        mode,
        "status":      "running",
        "progress":    0,
        "message":     "Startowanie…",
        "log":         [],
        "result_path": None,
        "error":       None,
        "shop_url":    shop_url,
    }

    def _progress(pct: int, msg: str):
        _tasks[task_id]["progress"] = pct
        _tasks[task_id]["message"]  = msg
        _tasks[task_id]["log"].append(msg)

    if mode == "company":
        def _worker():
            try:
                path = run_company_audit(
                    shop_url, site_url, creds_data, api_key, _progress
                )
                _tasks[task_id]["status"]      = "done"
                _tasks[task_id]["result_path"] = path
            except Exception as e:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["error"]  = str(e)
                _tasks[task_id]["log"].append(f"BŁĄD: {e}")
    else:
        def _worker():
            try:
                path = run_audit(shop_url, site_url, creds_data, api_key, _progress)
                _tasks[task_id]["status"]      = "done"
                _tasks[task_id]["result_path"] = path
            except Exception as e:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["error"]  = str(e)
                _tasks[task_id]["log"].append(f"BŁĄD: {e}")

    threading.Thread(target=_worker, daemon=True).start()
    return redirect(url_for("status_page", task_id=task_id))


@app.route("/status/<task_id>")
def status_page(task_id: str):
    task = _tasks.get(task_id)
    if not task:
        return redirect(url_for("index"))
    return render_template("status.html", task_id=task_id, task=task)


@app.route("/api/status/<task_id>")
def api_status(task_id: str):
    task = _tasks.get(task_id)
    if not task:
        return jsonify({"error": "not found"}), 404
    # Don't send result_path to client
    return jsonify({k: v for k, v in task.items() if k != "result_path"})


@app.route("/download/<task_id>")
def download(task_id: str):
    task = _tasks.get(task_id)
    if not task or task["status"] != "done" or not task.get("result_path"):
        return "Plik nie jest gotowy.", 404

    shop_url = task.get("shop_url", "")
    if task.get("type") == "company":
        filename = make_company_filename(shop_url)
    else:
        domain   = shop_url.replace("https://", "").replace("http://", "").split("/")[0]
        filename = f"seo_audit_{domain}.xlsx"

    return send_file(
        task["result_path"],
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/blog-planner")
def blog_planner():
    api_key_set = bool(ANTHROPIC_API_KEY)
    return render_template("blog_planner.html", api_key_set=api_key_set)


@app.route("/blog-planner/generate", methods=["POST"])
def blog_planner_generate():
    frazy   = request.form.get("frazy", "").strip()
    jezyk   = request.form.get("jezyk", "Polski").strip()
    api_key = request.form.get("api_key", "").strip() or ANTHROPIC_API_KEY

    if not frazy:
        return jsonify({"error": "Pole 'Frazy kategorii' jest wymagane."}), 400
    if not api_key:
        return jsonify({"error": "Podaj klucz Anthropic API."}), 400

    task_id = uuid.uuid4().hex[:8]
    _tasks[task_id] = {
        "type":        "blog",
        "status":      "running",
        "progress":    0,
        "message":     "Startowanie…",
        "log":         [],
        "result_path": None,
        "error":       None,
        "frazy":       frazy,
    }

    def _progress(pct: int, msg: str):
        _tasks[task_id]["progress"] = pct
        _tasks[task_id]["message"]  = msg
        _tasks[task_id]["log"].append(msg)

    def _worker():
        try:
            import anthropic as _anthropic

            n_phrases = len([f for f in frazy.split(",") if f.strip()])
            _progress(10, f"Generuję plan dla {n_phrases} fraz kategorii przez Claude API…")

            client = _anthropic.Anthropic(api_key=api_key)
            plan   = generate_blog_plan(client, frazy, jezyk)

            cats        = plan.get("categories", [])
            n_pillars   = sum(len(c.get("clusters", [])) for c in cats)
            n_supporting = sum(
                len(cl.get("supporting", []))
                for c in cats for cl in c.get("clusters", [])
            )
            _progress(85, f"Plan gotowy — {len(cats)} kategorii, {n_pillars} pytań pillar, {n_supporting} fraz supporting. Generuję Excel…")

            path = export_excel(plan, frazy)
            _tasks[task_id]["status"]      = "done"
            _tasks[task_id]["result_path"] = path
            _progress(100, f"Gotowe! {len(cats)} kategorii · {n_pillars} pillarów · {n_supporting} fraz supporting.")
        except Exception as e:
            _tasks[task_id]["status"] = "error"
            _tasks[task_id]["error"]  = str(e)
            _tasks[task_id]["log"].append(f"BŁĄD: {e}")

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/blog-planner/download/<task_id>")
def blog_planner_download(task_id: str):
    task = _tasks.get(task_id)
    if not task or task.get("type") != "blog" or task["status"] != "done":
        return "Plik nie jest gotowy.", 404

    filename = make_filename(task.get("frazy", "blog"))

    return send_file(
        task["result_path"],
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_ENV") == "development")
