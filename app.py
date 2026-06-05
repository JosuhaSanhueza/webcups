from flask import Flask, render_template, request, redirect, flash, jsonify, Response
import subprocess
import os
import time
import traceback
import json

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "orion_secure_key")

LAST_ACTION = {"type": None, "job": None, "user": None, "time": 0}


def get_active_jobs():
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

    if active_jobs:
        job = active_jobs[0]
        return (f"Imprimiendo ({job['id']} - {job['user']})", "warn")

    return ("Esperando documentos", "ok")


@app.route("/")
def index():
    try:
        active = get_active_jobs()
        history = get_history()
        status_text, status_class = get_status(active)

        return render_template(
            "index.html",
            active=active,
            history=history,
            status_text=status_text,
            status_class=status_class
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

        return jsonify({
            "active": active,
            "history": history,
            "status_text": status_text,
            "status_class": status_class
        })
    except Exception:
        print("ERROR api_status:")
        print(traceback.format_exc())
        return jsonify({"error": "Error interno del servidor"}), 500


@app.route("/api/events")
def api_events():
    def event_stream():
        last_data = None
        while True:
            try:
                active = get_active_jobs()
                history = get_history()
                status_text, status_class = get_status(active)

                current_data = {
                    "active": active,
                    "history": history,
                    "status_text": status_text,
                    "status_class": status_class
                }

                if current_data != last_data:
                    last_data = current_data
                    yield f"data: {json.dumps(current_data)}\n\n"
            except Exception as e:
                print("SSE Error:", e)
            time.sleep(2)

    return Response(event_stream(), mimetype="text/event-stream")


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
