from flask import Flask, render_template, request, redirect, flash, jsonify, Response, send_file, abort
import subprocess
import os
import time
import traceback
import json
import csv
import io
import re
import shutil

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "orion_secure_key")

LAST_ACTION = {"type": None, "job": None, "user": None, "time": 0}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IPP_DIR = os.path.join(BASE_DIR, "ipp")
SPOOL_DIR = "/var/spool/cups"
CACHE_DIR = os.environ.get("WEBCUPS_CACHE_DIR", "/tmp/webcups-cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def run_ipptool(test_file):
    """Ejecuta una consulta ipptool contra el CUPS local y devuelve filas (dict) del CSV."""
    output = subprocess.check_output(
        ["ipptool", "-c", "ipp://localhost/", os.path.join(IPP_DIR, test_file)],
        timeout=5
    ).decode(errors="ignore")
    return list(csv.DictReader(io.StringIO(output)))


def spool_file(job_id):
    return os.path.join(SPOOL_DIR, f"d{int(job_id):05d}-001")


def get_job_pdf(job_id):
    """Devuelve la ruta a un PDF del documento (convierte PostScript si hace falta), o None."""
    src = spool_file(job_id)
    if not os.path.exists(src):
        return None

    with open(src, "rb") as f:
        head = f.read(1024)

    if head.startswith(b"%PDF"):
        return src

    if head.startswith(b"%!") or b"%!PS" in head:
        cached = os.path.join(CACHE_DIR, f"{job_id}-{int(os.path.getmtime(src))}.pdf")
        if not os.path.exists(cached):
            tmp = cached + ".tmp"
            subprocess.run(
                ["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                 f"-sOutputFile={tmp}", src],
                capture_output=True, timeout=60
            )
            if os.path.exists(tmp):
                os.replace(tmp, cached)
        return cached if os.path.exists(cached) else None

    # Formatos crudos (PCL, ESC/P, raster...) no se pueden previsualizar
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
        return int(value) if value.isdigit() else None

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

    with open(cache, "w") as f:
        f.write(str(pages or ""))
    return pages


def get_review_printers():
    """Diccionario {impresora: True/False} indicando si retiene los trabajos para revisión."""
    try:
        return {
            row["printer-name"]: row.get("job-hold-until-default") == "indefinite"
            for row in run_ipptool("get-printers.test")
        }
    except Exception:
        print("ERROR get_review_printers:")
        print(traceback.format_exc())
        return {}


def get_active_jobs():
    try:
        jobs = []
        for row in run_ipptool("get-jobs.test"):
            job_id = row.get("job-id", "")
            if not job_id.isdigit():
                continue

            copies = int(row["copies"]) if row.get("copies", "").isdigit() else 1
            duplex = row.get("sides", "").startswith("two-sided")
            pages = get_page_count(job_id)
            sheets = None
            if pages:
                sheets = ((pages + 1) // 2 if duplex else pages) * copies

            jobs.append({
                "id": job_id,
                "user": row.get("job-originating-user-name", "").split("\\")[0],
                "printer": row.get("job-printer-uri", "").rsplit("/", 1)[-1],
                "name": row.get("job-name", ""),
                "held": row.get("job-state") == "pending-held",
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


def get_history():
    jobs = []
    try:
        # Nota: este archivo puede requerir permisos de lectura del grupo lp
        if os.path.exists("/var/log/cups/page_log"):
            with open("/var/log/cups/page_log") as f:
                lines = f.readlines()
        else:
            lines = []

        for line in lines[-70:]:
            parts = line.split()

            if len(parts) < 5:
                continue

            try:
                printer = parts[0].replace('"', '')
                user = parts[1].split("\\")[0]
                job_id = parts[2]
                date = parts[3] + " " + parts[4]

                jobs.append({
                    "id": job_id,
                    "user": user,
                    "printer": printer,
                    "date": date
                })
            except Exception:
                continue

    except Exception:
        print("ERROR get_history:")
        print(traceback.format_exc())

    return list(reversed(jobs))


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


@app.route("/")
def index():
    try:
        active = get_active_jobs()
        history = get_history()
        status_text, status_class = get_status(active)
        review = get_review_printers()

        return render_template(
            "index.html",
            active=active,
            history=history,
            status_text=status_text,
            status_class=status_class,
            review_mode=bool(review) and all(review.values())
        )
    except Exception:
        print("ERROR index:")
        print(traceback.format_exc())
        return "Error interno", 500


@app.route("/api/status")
def api_status():
    try:
        active = get_active_jobs()
        history = get_history()
        status_text, status_class = get_status(active)
        review = get_review_printers()

        return jsonify({
            "active": active,
            "history": history,
            "status_text": status_text,
            "status_class": status_class,
            "review_mode": bool(review) and all(review.values())
        })
    except Exception:
        print("ERROR api_status:")
        print(traceback.format_exc())
        return jsonify({"error": "Error interno del servidor"}), 500


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
            job_file = f"/var/spool/cups/d{int(job_id):05d}-001"

            if os.path.exists(job_file):
                # Habilitar la cola en la impresora antes de enviar
                subprocess.run(["cupsenable", printer], timeout=3)
                subprocess.run(["cupsaccept", printer], timeout=3)
                
                # Reimprimir el archivo spool con lp de forma inmediata
                result = subprocess.run(
                    ["lp", "-d", printer, "-H", "immediate", job_file],
                    capture_output=True,
                    text=True,
                    timeout=5
                )

                if result.returncode == 0:
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


@app.route("/approve", methods=["POST"])
def approve():
    global LAST_ACTION

    job_id = request.form.get("job_id")
    if not (job_id and job_id.isdigit()):
        return ajax_or_redirect(False, "ID de trabajo no válido.")

    try:
        result = subprocess.run(["lp", "-i", job_id, "-H", "resume"], capture_output=True, text=True, timeout=5)
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

    for printer in get_review_printers():
        result = subprocess.run(
            ["lpadmin", "-p", printer, "-o", f"job-hold-until-default={value}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            errors.append(f"{printer}: {result.stderr.strip()}")

    if errors:
        return ajax_or_redirect(False, "Error al cambiar el modo revisión: " + "; ".join(errors))
    return ajax_or_redirect(True, "Modo revisión " + ("activado." if enabled else "desactivado."))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
