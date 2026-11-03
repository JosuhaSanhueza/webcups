from flask import Flask, render_template, request, redirect, flash, jsonify, Response, send_file, abort, session
from werkzeug.security import check_password_hash
from datetime import timedelta
import subprocess
import os
import time
import traceback
import json
import csv
import io
import gzip
import re
import shutil
import secrets
import sqlite3
import fcntl
import threading
from contextlib import closing

app = Flask(__name__, static_folder="static")


def load_auth_config():
    """Lee la contraseña (hash) y la clave de sesión, creadas con set_password.py."""
    try:
        with open(os.environ.get("WEBCUPS_CONFIG", "/etc/webcups.conf")) as f:
            return json.load(f)
    except (OSError, ValueError):
        print("AVISO: panel sin contraseña configurada; ejecuta set_password.py", flush=True)
        return {}


AUTH = load_auth_config()
# Sin configuración el login queda deshabilitado, nunca abierto
app.secret_key = AUTH.get("secret_key") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12)
)

MAX_LOGIN_FAILS = 5
LOGIN_LOCK_SECONDS = 300

LAST_ACTION = {"type": None, "job": None, "user": None, "time": 0}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IPP_DIR = os.path.join(BASE_DIR, "ipp")
SPOOL_DIR = "/var/spool/cups"
CACHE_DIR = os.environ.get("WEBCUPS_CACHE_DIR", "/tmp/webcups-cache")
CACHE_MAX_AGE = 7 * 86400
DB_PATH = os.environ.get("WEBCUPS_DB", "/var/lib/webcups/webcups.db")
SYNC_SECONDS = 5
IPP_URI = os.environ.get("WEBCUPS_IPP_URI", "ipp://localhost/")
# Cola privada conectada a la impresora: la pública apunta al destino de retención y
# solo Autorizar saca trabajos
OUTPUT_SUFFIX = "_Salida"
HOLD_URI = "webcups-hold:/"
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY,
                user TEXT DEFAULT '',
                account TEXT DEFAULT '',
                printer TEXT DEFAULT '',
                name TEXT DEFAULT '',
                pages INTEGER,
                copies INTEGER DEFAULT 1,
                duplex INTEGER DEFAULT 0,
                sheets INTEGER,
                state TEXT DEFAULT '',
                created_at INTEGER,
                finished_at INTEGER,
                review_on INTEGER DEFAULT 0,
                approved_at INTEGER,
                denied_at INTEGER,
                unauthorized INTEGER DEFAULT 0,
                acknowledged INTEGER DEFAULT 0,
                reprint_of INTEGER
            );
            CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at);
            CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT, at INTEGER);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)


init_db()


def run_ipptool(test_file, **variables):
    """Ejecuta una consulta ipptool contra el CUPS local y devuelve filas (dict) del CSV."""
    defines = [arg for name, value in variables.items() for arg in ("-d", f"{name}={value}")]
    output = subprocess.check_output(
        ["ipptool", "-c", *defines, IPP_URI, os.path.join(IPP_DIR, test_file)],
        timeout=10
    ).decode(errors="ignore")
    return list(csv.DictReader(io.StringIO(output)))


def spool_file(job_id):
    return os.path.join(SPOOL_DIR, f"d{int(job_id):05d}-001")


def get_job_pdf(job_id):
    """Devuelve la ruta a un PDF del documento (descomprime gzip, quita cabeceras PJL
    y convierte PostScript si hace falta), o None si el formato no se puede previsualizar."""
    src = spool_file(job_id)
    if not os.path.exists(src):
        return None

    base = os.path.join(CACHE_DIR, f"{job_id}-{int(os.path.getmtime(src))}")
    cached = base + ".pdf"
    if os.path.exists(cached):
        return cached

    with open(src, "rb") as f:
        head = f.read(4096)

    # Documento comprimido por el cliente IPP (compression=gzip)
    if head.startswith(b"\x1f\x8b"):
        raw = base + ".raw"
        if not os.path.exists(raw):
            with gzip.open(src, "rb") as fin, open(raw + ".tmp", "wb") as fout:
                shutil.copyfileobj(fin, fout)
            os.replace(raw + ".tmp", raw)
        src = raw
        with open(src, "rb") as f:
            head = f.read(4096)

    tmp = cached + ".tmp"

    if head.startswith(b"%PDF"):
        if src.startswith(SPOOL_DIR):
            return src
        os.replace(src, cached)
        return cached

    # PDF precedido por una cabecera PJL (ej. "\x1b%-12345X@PJL ...")
    pdf_start = head.find(b"%PDF")
    if pdf_start > 0:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            fin.seek(pdf_start)
            shutil.copyfileobj(fin, fout)
        os.replace(tmp, cached)
        return cached

    if head.startswith(b"%!") or b"%!PS" in head:
        subprocess.run(
            ["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
             f"-sOutputFile={tmp}", src],
            capture_output=True, timeout=60
        )
        if os.path.exists(tmp):
            os.replace(tmp, cached)
        return cached if os.path.exists(cached) else None

    # Formatos crudos (PCL, ESC/P, raster...) no se pueden previsualizar.
    # Se registra una sola vez por trabajo para poder diagnosticar el formato.
    marker = base + ".unknown"
    if not os.path.exists(marker):
        open(marker, "w").close()
        print(f"Trabajo {job_id}: formato sin vista previa, primeros bytes: {head[:48]!r}", flush=True)
    return None


def get_page_count(job_id):
    """Número de páginas del documento (con caché en disco), o None si no se puede leer."""
    src = spool_file(job_id)
    if not os.path.exists(src):
        return None

    cache = os.path.join(CACHE_DIR, f"{job_id}-{int(os.path.getmtime(src))}.pages")
    if os.path.exists(cache):
        with open(cache) as f:
            value = f.read().strip()
        if value.isdigit():
            return int(value)

    pages = None
    try:
        pdf = get_job_pdf(job_id)
        if pdf:
            info = subprocess.check_output(["pdfinfo", pdf], timeout=10).decode(errors="ignore")
            match = re.search(r"^Pages:\s+(\d+)", info, re.MULTILINE)
            if match:
                pages = int(match.group(1))
    except Exception:
        print(f"ERROR get_page_count {job_id}:")
        print(traceback.format_exc())

    # Solo se guarda en caché un resultado válido, para reintentar si el formato cambia
    if pages:
        with open(cache, "w") as f:
            f.write(str(pages))
    return pages


def get_printers():
    """Colas de CUPS: {nombre: {hold, device}}."""
    return {
        row["printer-name"]: {
            "hold": row.get("job-hold-until-default") == "indefinite",
            "device": row.get("device-uri", "")
        }
        for row in run_ipptool("get-printers.test")
    }


def public_name(queue):
    return queue[:-len(OUTPUT_SUFFIX)] if queue.endswith(OUTPUT_SUFFIX) else queue


def get_review_printers():
    """Diccionario {cola pública: True/False} indicando si retiene los trabajos para revisión."""
    try:
        return {name: info["hold"] for name, info in get_printers().items() if not name.endswith(OUTPUT_SUFFIX)}
    except Exception:
        print("ERROR get_review_printers:")
        print(traceback.format_exc())
        return {}


def split_user(raw):
    """Separa 'EQUIPO\\\\cuenta' (Windows) en equipo y cuenta."""
    parts = [part for part in re.split(r"\\+", raw or "") if part]
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


def compute_sheets(pages, copies, duplex):
    if not pages:
        return None
    return ((pages + 1) // 2 if duplex else pages) * copies


def get_active_jobs():
    try:
        jobs = []
        printers = get_printers()
        for row in run_ipptool("get-jobs.test", which="not-completed", limit=500):
            job_id = row.get("job-id", "")
            if not job_id.isdigit():
                continue

            copies = int(row["copies"]) if row.get("copies", "").isdigit() else 1
            duplex = row.get("sides", "").startswith("two-sided")
            pages = get_page_count(job_id)
            sheets = compute_sheets(pages, copies, duplex)
            user, account = split_user(row.get("job-originating-user-name"))
            queue = row.get("job-printer-uri", "").rsplit("/", 1)[-1]
            state = row.get("job-state")
            guarded = queue + OUTPUT_SUFFIX in printers

            jobs.append({
                "id": job_id,
                "user": user,
                "account": account,
                "printer": public_name(queue),
                "name": row.get("job-name", ""),
                "held": state == "pending-held" or (state == "pending" and guarded),
                "copies": copies,
                "duplex": duplex,
                "pages": pages,
                "sheets": sheets,
                "preview": get_job_pdf(job_id) is not None
            })
        return jobs
    except Exception:
        print("ERROR get_active_jobs (ipptool), usando lpstat:")
        print(traceback.format_exc())
        return get_active_jobs_lpstat()


def get_active_jobs_lpstat():
    jobs = []
    try:
        output = subprocess.check_output(
            ["lpstat", "-o"],
            timeout=3
        ).decode(errors="ignore")

        for line in output.splitlines():
            parts = line.split()

            if len(parts) < 2:
                continue

            try:
                printer_job = parts[0]
                user = parts[1].split("\\")[0]

                if "-" not in printer_job:
                    continue

                printer, job_id = printer_job.rsplit("-", 1)

                jobs.append({
                    "id": job_id,
                    "user": user,
                    "printer": printer
                })
            except Exception:
                continue

    except Exception:
        print("ERROR get_active_jobs:")
        print(traceback.format_exc())

    return jobs


def record_job(conn, row, review, baseline, guarded=()):
    """Inserta o actualiza un trabajo de CUPS y marca los impresos sin autorización."""
    job_id = int(row["job-id"])
    state = row.get("job-state", "")
    copies = int(row["copies"]) if row.get("copies", "").isdigit() else 1
    duplex = row.get("sides", "").startswith("two-sided")
    user, account = split_user(row.get("job-originating-user-name"))
    created = int(row["time-at-creation"]) if row.get("time-at-creation", "").isdigit() else None
    finished = row.get("time-at-completed", "")
    finished = int(finished) if finished.isdigit() and int(finished) > 0 else None

    known = conn.execute("SELECT pages, created_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    pages = known["pages"] if known and known["pages"] else get_page_count(job_id)

    if known is None:
        conn.execute("INSERT INTO jobs (id, review_on) VALUES (?, ?)", (job_id, int(review and not baseline)))

    conn.execute("""
        UPDATE jobs SET
            user = COALESCE(NULLIF(?, ''), user),
            account = COALESCE(NULLIF(?, ''), account),
            printer = COALESCE(NULLIF(?, ''), printer),
            name = COALESCE(NULLIF(?, ''), name),
            pages = ?, copies = ?, duplex = ?, sheets = ?, state = ?,
            created_at = COALESCE(?, created_at),
            finished_at = COALESCE(?, finished_at)
        WHERE id = ?
    """, (user, account, public_name(row.get("job-printer-uri", "").rsplit("/", 1)[-1]), row.get("job-name", ""),
          pages, copies, int(duplex), compute_sheets(pages, copies, duplex), state, created, finished, job_id))

    # En una cola con destino de retención, "processing" no imprime: CUPS lo volverá a retener
    if state == "processing" and row.get("job-printer-uri", "").rsplit("/", 1)[-1] in guarded:
        return

    # Con el modo revisión activo, nada debería imprimirse sin pasar por Autorizar
    conn.execute("""
        UPDATE jobs SET unauthorized = 1
        WHERE id = ? AND review_on = 1 AND approved_at IS NULL AND denied_at IS NULL
          AND state IN ('processing', 'processing-stopped', 'completed', 'aborted')
    """, (job_id,))


def sync_jobs():
    """Copia a la base de datos el estado de los trabajos de CUPS."""
    with closing(db()) as conn:
        first_run = conn.execute("SELECT 1 FROM meta WHERE key = 'baseline_at'").fetchone() is None

    printers = get_printers()
    review = {name: info["hold"] for name, info in printers.items() if not name.endswith(OUTPUT_SUFFIX)}
    review_on = bool(review) and all(review.values())
    guarded = {name for name, info in printers.items() if info["device"] == HOLD_URI}
    rows = run_ipptool("get-jobs.test", which="not-completed", limit=500)
    rows += run_ipptool("get-jobs.test", which="completed", limit=500 if first_run else 50)

    with closing(db()) as conn, conn:
        for row in rows:
            if row.get("job-id", "").isdigit():
                record_job(conn, row, review_on, first_run, guarded)
        if first_run:
            conn.execute("INSERT INTO meta (key, value) VALUES ('baseline_at', ?)", (str(int(time.time())),))


def purge_cache():
    limit = time.time() - CACHE_MAX_AGE
    for entry in os.scandir(CACHE_DIR):
        if entry.is_file() and entry.stat().st_mtime < limit:
            os.remove(entry.path)


def collector_loop():
    """Sincroniza trabajos en segundo plano. El candado deja activo a un solo worker."""
    with open(DB_PATH + ".lock", "w") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(SYNC_SECONDS * 3)

        last_purge = 0
        while True:
            try:
                sync_jobs()
                if time.time() - last_purge > 3600:
                    purge_cache()
                    last_purge = time.time()
            except Exception:
                print("ERROR collector:", flush=True)
                print(traceback.format_exc(), flush=True)
            time.sleep(SYNC_SECONDS)


def start_collector():
    threading.Thread(target=collector_loop, daemon=True).start()


def record_decision(job_id, **fields):
    """Registra una decisión del panel aunque el recolector aún no haya visto el trabajo."""
    with closing(db()) as conn, conn:
        conn.execute("INSERT OR IGNORE INTO jobs (id, created_at) VALUES (?, ?)", (int(job_id), int(time.time())))
        for field, value in fields.items():
            conn.execute(f"UPDATE jobs SET {field} = ? WHERE id = ?", (value, int(job_id)))


def job_status(job):
    if job["denied_at"]:
        return "denied"
    if job["state"] == "completed":
        return "printed"
    if job["state"] in ("canceled", "aborted"):
        return "canceled" if job["state"] == "canceled" else "error"
    return "active"


def period_start(days):
    today = time.localtime()
    midnight = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
    return int(midnight) - (days - 1) * 86400


def get_report(days):
    since = period_start(days)
    with closing(db()) as conn:
        totals = conn.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN state = 'completed' AND denied_at IS NULL THEN sheets END), 0) AS sheets,
                COUNT(CASE WHEN state = 'completed' AND denied_at IS NULL THEN 1 END) AS printed,
                COUNT(CASE WHEN state = 'completed' AND denied_at IS NULL AND sheets IS NULL THEN 1 END) AS unknown,
                COUNT(denied_at) AS denied,
                COALESCE(SUM(CASE WHEN denied_at IS NOT NULL THEN sheets END), 0) AS saved
            FROM jobs WHERE created_at >= ?
        """, (since,)).fetchone()

        by_day = {row["day"]: row["sheets"] for row in conn.execute("""
            SELECT date(created_at, 'unixepoch', 'localtime') AS day, COALESCE(SUM(sheets), 0) AS sheets
            FROM jobs WHERE created_at >= ? AND state = 'completed' AND denied_at IS NULL
            GROUP BY day
        """, (since,))}

        users = conn.execute("""
            SELECT user,
                COUNT(CASE WHEN state = 'completed' AND denied_at IS NULL THEN 1 END) AS printed,
                COALESCE(SUM(CASE WHEN state = 'completed' AND denied_at IS NULL THEN sheets END), 0) AS sheets,
                COUNT(denied_at) AS denied,
                MAX(created_at) AS last_at
            FROM jobs WHERE created_at >= ? AND user != ''
            GROUP BY user ORDER BY sheets DESC, printed DESC, user
        """, (since,)).fetchall()

    days_list = [time.strftime("%Y-%m-%d", time.localtime(since + i * 86400 + 7200)) for i in range(days)]
    return {
        "days": days,
        "totals": dict(totals),
        "by_day": [{"day": day, "sheets": by_day.get(day, 0)} for day in days_list],
        "users": [dict(row) for row in users]
    }


def get_history(query="", status="", days=30, limit=200):
    conditions = ["created_at >= ?"]
    params = [period_start(days)]

    if query:
        conditions.append("(user LIKE ? OR account LIKE ? OR name LIKE ? OR CAST(id AS TEXT) = ?)")
        params += [f"%{query}%", f"%{query}%", f"%{query}%", query]

    filters = {
        "printed": "state = 'completed' AND denied_at IS NULL",
        "denied": "denied_at IS NOT NULL",
        "canceled": "state = 'canceled' AND denied_at IS NULL",
        "error": "state = 'aborted'",
        "unauthorized": "unauthorized = 1"
    }
    if status in filters:
        conditions.append(filters[status])
    else:
        conditions.append("state IN ('completed', 'canceled', 'aborted')")

    sql = f"SELECT * FROM jobs WHERE {' AND '.join(conditions)} ORDER BY id DESC LIMIT ?"
    with closing(db()) as conn:
        rows = conn.execute(sql, params + [limit]).fetchall()

    jobs = []
    for row in rows:
        job = dict(row)
        job["status"] = job_status(row)
        job["reprintable"] = os.path.exists(spool_file(row["id"]))
        jobs.append(job)
    return jobs


def get_alert_count():
    with closing(db()) as conn:
        return conn.execute("SELECT COUNT(*) FROM jobs WHERE unauthorized = 1 AND acknowledged = 0").fetchone()[0]


def get_status(active_jobs):
    global LAST_ACTION

    if time.time() - LAST_ACTION["time"] < 15:
        if LAST_ACTION["type"] == "reprint":
            return (f"Reimprimiendo ({LAST_ACTION['job']} - {LAST_ACTION['user']})", "warn")
        if LAST_ACTION["type"] == "print":
            return (f"Imprimiendo ({LAST_ACTION['job']} - {LAST_ACTION['user']})", "warn")

    printing = [j for j in active_jobs if not j.get("held")]
    if printing:
        job = printing[0]
        return (f"Imprimiendo ({job['id']} - {job['user']})", "warn")

    if active_jobs:
        return (f"{len(active_jobs)} esperando autorización", "warn")

    return ("Esperando documentos", "ok")


@app.before_request
def require_login():
    if request.endpoint in ("login", "static") or session.get("auth"):
        return None
    if request.method == "GET" and request.endpoint == "index":
        return redirect("/login")
    return jsonify({"success": False, "error": "auth", "message": "Sesión no iniciada."}), 401


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("auth"):
        return redirect("/")

    password_hash = AUTH.get("password_hash")
    if not password_hash:
        return render_template("login.html", error="El panel aún no tiene contraseña configurada.", disabled=True), 503

    if request.method == "GET":
        return render_template("login.html", error=None, disabled=False)

    ip = request.remote_addr
    now = int(time.time())

    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM login_attempts WHERE at < ?", (now - LOGIN_LOCK_SECONDS,))
        fails = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE ip = ?", (ip,)).fetchone()[0]

    if fails >= MAX_LOGIN_FAILS:
        return render_template("login.html", error="Demasiados intentos. Espera unos minutos.", disabled=False), 429

    if check_password_hash(password_hash, request.form.get("password", "")):
        with closing(db()) as conn, conn:
            conn.execute("DELETE FROM login_attempts WHERE ip = ?", (ip,))
        session.clear()
        session.permanent = True
        session["auth"] = True
        return redirect("/")

    with closing(db()) as conn, conn:
        conn.execute("INSERT INTO login_attempts (ip, at) VALUES (?, ?)", (ip, now))
    time.sleep(1)
    return render_template("login.html", error="Contraseña incorrecta.", disabled=False), 401


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    try:
        active = get_active_jobs()
        status_text, status_class = get_status(active)
        review = get_review_printers()

        return jsonify({
            "active": active,
            "status_text": status_text,
            "status_class": status_class,
            "review_mode": bool(review) and all(review.values()),
            "alerts": get_alert_count()
        })
    except Exception:
        print("ERROR api_status:")
        print(traceback.format_exc())
        return jsonify({"error": "Error interno del servidor"}), 500


def days_param(default=30):
    value = request.args.get("days", "")
    return min(int(value), 365) if value.isdigit() and int(value) > 0 else default


@app.route("/api/history")
def api_history():
    try:
        return jsonify({"jobs": get_history(
            query=request.args.get("q", "").strip(),
            status=request.args.get("status", ""),
            days=days_param()
        )})
    except Exception:
        print("ERROR api_history:")
        print(traceback.format_exc())
        return jsonify({"error": "Error interno del servidor"}), 500


@app.route("/api/report")
def api_report():
    try:
        return jsonify(get_report(days_param(14)))
    except Exception:
        print("ERROR api_report:")
        print(traceback.format_exc())
        return jsonify({"error": "Error interno del servidor"}), 500


@app.route("/alerts/ack", methods=["POST"])
def alerts_ack():
    with closing(db()) as conn, conn:
        conn.execute("UPDATE jobs SET acknowledged = 1 WHERE unauthorized = 1")
    return ajax_or_redirect(True, "Avisos marcados como revisados.")


@app.route("/cancel", methods=["POST"])
def cancel():
    try:
        job_id = request.form.get("job_id")
        success = False
        message = ""

        if job_id and job_id.isdigit():
            # Ejecutamos cancel y capturamos códigos de error
            result = subprocess.run(["cancel", job_id], capture_output=True, text=True, timeout=3)
            
            if result.returncode == 0:
                message = f"Se canceló el trabajo {job_id} correctamente."
                success = True
            else:
                message = f"No se pudo cancelar el trabajo {job_id}. Detalle: {result.stderr.strip()}"
        else:
            message = "ID de trabajo no válido."

    except Exception as e:
        print("ERROR cancel:")
        print(traceback.format_exc())
        message = f"Error en el servidor al intentar cancelar: {str(e)}"

    # Verificar si es una petición AJAX (fetch)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": success, "message": message})
        
    flash(message)
    return redirect("/")


@app.route("/reprint", methods=["POST"])
def reprint():
    global LAST_ACTION

    try:
        job_id = request.form.get("job_id")
        printer = request.form.get("printer")
        user = request.form.get("user")
        success = False
        message = ""

        if job_id and job_id.isdigit() and printer:
            job_file = spool_file(job_id)

            if os.path.exists(job_file):
                # Una reimpresión ya está autorizada: va directo a la cola de salida si existe,
                # nunca se reanuda la cola pública pausada
                target = printer + OUTPUT_SUFFIX if printer + OUTPUT_SUFFIX in get_printers() else printer
                subprocess.run(["cupsenable", target], timeout=3)
                subprocess.run(["cupsaccept", target], timeout=3)

                # Reimprimir el archivo spool con lp de forma inmediata
                result = subprocess.run(
                    ["lp", "-d", target, "-H", "immediate", job_file],
                    capture_output=True,
                    text=True,
                    timeout=5
                )

                if result.returncode == 0:
                    new_job = re.search(r"-(\d+)", result.stdout)
                    if new_job:
                        record_decision(new_job.group(1), approved_at=int(time.time()),
                                        unauthorized=0, reprint_of=int(job_id))
                    LAST_ACTION = {
                        "type": "reprint",
                        "job": job_id,
                        "user": user,
                        "time": time.time()
                    }
                    message = f"El trabajo {job_id} se envió a reimprimir con éxito."
                    success = True
                else:
                    message = f"Error al reimprimir con CUPS: {result.stderr.strip()}"
            else:
                message = f"El archivo de spool no se encuentra en la ruta: {job_file}. Asegúrate de tener permisos de lectura."
        else:
            message = "Parámetros de reimpresión insuficientes o inválidos."

    except Exception as e:
        print("ERROR reprint:")
        print(traceback.format_exc())
        message = f"Error en el servidor al intentar reimprimir: {str(e)}"

    # Verificar si es una petición AJAX (fetch)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": success, "message": message})

    flash(message)
    return redirect("/")


def ajax_or_redirect(success, message):
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": success, "message": message})
    flash(message)
    return redirect("/")


@app.route("/preview/<job_id>.png")
def preview(job_id):
    """Miniatura PNG de la primera página del trabajo."""
    if not job_id.isdigit():
        abort(404)

    try:
        pdf = get_job_pdf(job_id)
        if not pdf:
            abort(404)

        page = request.args.get("page", "1")
        page = int(page) if page.isdigit() and int(page) > 0 else 1
        size = 1200 if request.args.get("large") else 360

        src = spool_file(job_id)
        out_base = os.path.join(CACHE_DIR, f"{job_id}-{int(os.path.getmtime(src))}-p{page}-{size}")
        out_png = out_base + ".png"

        if not os.path.exists(out_png):
            subprocess.run(
                ["pdftoppm", "-png", "-singlefile", "-f", str(page), "-l", str(page),
                 "-scale-to", str(size), pdf, out_base],
                capture_output=True, timeout=30
            )
        if not os.path.exists(out_png):
            abort(404)

        return send_file(out_png, mimetype="image/png", max_age=300)
    except Exception as e:
        if hasattr(e, "code"):
            raise
        print("ERROR preview:")
        print(traceback.format_exc())
        abort(500)


def release_job(job_id):
    """Pasa un trabajo a la cola de salida y lo libera. Si falla, sigue sin imprimirse."""
    queue = next((row.get("job-printer-uri", "").rsplit("/", 1)[-1]
                  for row in run_ipptool("get-jobs.test", which="not-completed", limit=500)
                  if row.get("job-id") == job_id), "")
    output = queue + OUTPUT_SUFFIX

    if output in get_printers():
        # Mover antes de liberar: liberado en la cola pública caería en el destino de retención
        moved = subprocess.run(["lpmove", job_id, output], capture_output=True, text=True, timeout=5)
        if moved.returncode != 0:
            return moved
        # Un trabajo enviado con no-hold no está retenido y resume falla: ya va a imprimirse
        subprocess.run(["lp", "-i", job_id, "-H", "resume"], capture_output=True, text=True, timeout=5)
        return moved

    return subprocess.run(["lp", "-i", job_id, "-H", "resume"], capture_output=True, text=True, timeout=5)


@app.route("/approve", methods=["POST"])
def approve():
    global LAST_ACTION

    job_id = request.form.get("job_id")
    if not (job_id and job_id.isdigit()):
        return ajax_or_redirect(False, "ID de trabajo no válido.")

    try:
        # Se registra antes de liberar para que el recolector no lo marque como no autorizado
        record_decision(job_id, approved_at=int(time.time()), unauthorized=0)
        result = release_job(job_id)
        if result.returncode != 0:
            record_decision(job_id, approved_at=None)
        if result.returncode == 0:
            LAST_ACTION = {"type": "print", "job": job_id, "user": request.form.get("user"), "time": time.time()}
            return ajax_or_redirect(True, f"Trabajo {job_id} autorizado.")
        return ajax_or_redirect(False, f"No se pudo autorizar el trabajo {job_id}: {result.stderr.strip()}")
    except Exception as e:
        print("ERROR approve:")
        print(traceback.format_exc())
        return ajax_or_redirect(False, f"Error en el servidor al autorizar: {str(e)}")


@app.route("/deny", methods=["POST"])
def deny():
    job_id = request.form.get("job_id")
    if not (job_id and job_id.isdigit()):
        return ajax_or_redirect(False, "ID de trabajo no válido.")

    try:
        result = subprocess.run(["cancel", job_id], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            record_decision(job_id, denied_at=int(time.time()))
            return ajax_or_redirect(True, f"Trabajo {job_id} denegado.")
        return ajax_or_redirect(False, f"No se pudo denegar el trabajo {job_id}: {result.stderr.strip()}")
    except Exception as e:
        print("ERROR deny:")
        print(traceback.format_exc())
        return ajax_or_redirect(False, f"Error en el servidor al denegar: {str(e)}")


@app.route("/review-mode", methods=["POST"])
def review_mode():
    """Activa/desactiva la retención de trabajos (requieren autorización) en todas las impresoras."""
    enabled = request.form.get("enabled") == "1"
    value = "indefinite" if enabled else "no-hold"
    errors = []
    printers = get_printers()

    for printer in printers:
        if printer.endswith(OUTPUT_SUFFIX):
            continue

        command = ["lpadmin", "-p", printer, "-o", f"job-hold-until-default={value}"]
        output = printers.get(printer + OUTPUT_SUFFIX)
        if output:
            command += ["-v", HOLD_URI if enabled else output["device"]]

        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            errors.append(f"{printer}: {result.stderr.strip()}")

    if errors:
        return ajax_or_redirect(False, "Error al cambiar el modo revisión: " + "; ".join(errors))
    return ajax_or_redirect(True, "Modo revisión " + ("activado." if enabled else "desactivado."))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
