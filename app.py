import os
import re
import threading
import time
import psycopg
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

FRAPPE_URL = os.getenv("FRAPPE_URL", "").rstrip("/")
FRAPPE_API_KEY = os.getenv("FRAPPE_API_KEY", "")
FRAPPE_API_SECRET = os.getenv("FRAPPE_API_SECRET", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

PRIORIDADES = {
    "BAJA": "BAJA",
    "LOW": "BAJA",
    "MEDIA": "MEDIA",
    "MEDIUM": "MEDIA",
    "ALTA": "ALTA",
    "HIGH": "ALTA",
    "URGENTE": "URGENTE",
    "URGENT": "URGENTE",
}

ESTADOS_FRAPPE = {
    "OPEN": "PENDIENTE",
    "REPLIED": "EN_ATENCION",
    "RESOLVED": "FINALIZADA",
    "CLOSED": "FINALIZADA",
}

ESTADOS_A_FRAPPE = {
    "FINALIZADA": "Closed",
    "EN_ATENCION": "Replied",
    "PENDIENTE": "Open",
}


def normalizar_estado_frappe(estado):
    if not estado or not isinstance(estado, str):
        return "PENDIENTE"
    return ESTADOS_FRAPPE.get(estado.strip().upper(), "PENDIENTE")


def actualizar_estado_frappe(id_pqr, nuevo_estado="Closed"):
    """
    Envía una petición PUT a la API de Frappe/Helpdesk para cambiar el estado del ticket.
    Estados válidos en Helpdesk: 'Open', 'Replied', 'Resolved', 'Closed'
    """
    if not FRAPPE_URL or not FRAPPE_API_KEY or not FRAPPE_API_SECRET:
        raise RuntimeError("Faltan variables de entorno de Frappe.")

    url = f"{FRAPPE_URL}/api/resource/HD%20Ticket/{id_pqr}"
    headers = {
        "Authorization": f"token {FRAPPE_API_KEY}:{FRAPPE_API_SECRET}",
        "Content-Type": "application/json",
    }
    
    response = requests.put(
        url, 
        headers=headers, 
        json={"status": nuevo_estado}, 
        timeout=15
    )
    response.raise_for_status()
    return response.json()


def normalizar_orden(payload):
    id_pqr = str(
        payload.get("name") or payload.get("id_pqr") or ""
    ).strip()
    if not id_pqr:
        raise ValueError("El campo 'id_pqr' es obligatorio.")

    subject_raw = str(payload.get("subject") or "").strip()
    descripcion = str(
        payload.get("descripcion")
        or payload.get("description")
        or "Sin descripción"
    ).strip()

    # 1. Intentar extraer la dirección desde la descripción ("Direccion: CL ...")
    direccion = ""
    match_dir = re.search(r"Direccion:\s*(.*)", descripcion, re.IGNORECASE)
    if match_dir:
        direccion = match_dir.group(1).split("\n")[0].strip()

    # 2. Separar Tipo de Servicio y Dirección desde el asunto (subject) si viene como "Servicio - Dirección"
    tipo_servicio = subject_raw
    if " - " in subject_raw:
        partes = subject_raw.split(" - ", 1)
        tipo_servicio = partes[0].strip()
        # Si no se encontró en la descripción, usar la segunda parte del asunto
        if not direccion:
            direccion = partes[1].strip()

    # Respaldos por si la extracción falla
    if not direccion:
        direccion = str(
            payload.get("direccion") or "No especificada"
        ).strip()

    if not tipo_servicio:
        tipo_servicio = str(
            payload.get("tipo_servicio") or "Atención PQR"
        ).strip()

    # Ajuste de límites según esquema de Neon PostgreSQL
    id_pqr = id_pqr[:30]
    tipo_servicio = tipo_servicio[:100]
    direccion = direccion[:200]

    prioridad_raw = str(
        payload.get("prioridad") or payload.get("priority") or "MEDIA"
    ).strip().upper()
    prioridad = PRIORIDADES.get(prioridad_raw, "MEDIA")

    estado = normalizar_estado_frappe(
        payload.get("estado_frappe") or payload.get("status")
    )

    return {
        "id_pqr": id_pqr,
        "tipo_servicio": tipo_servicio,
        "descripcion": descripcion,
        "direccion": direccion,
        "prioridad": prioridad,
        "estado": estado,
    }


def guardar_ordenes(ordenes):
    if not DATABASE_URL:
        raise RuntimeError("La variable DATABASE_URL no está configurada.")
    if not ordenes:
        return

    with psycopg.connect(DATABASE_URL) as conexion:
        with conexion.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO ordenes_trabajo
                    (id_pqr, tipo_servicio, descripcion, direccion, prioridad, estado)
                VALUES
                    (%(id_pqr)s, %(tipo_servicio)s, %(descripcion)s,
                     %(direccion)s, %(prioridad)s, %(estado)s)
                ON CONFLICT (id_pqr) DO UPDATE SET
                    tipo_servicio = EXCLUDED.tipo_servicio,
                    descripcion = EXCLUDED.descripcion,
                    direccion = EXCLUDED.direccion,
                    prioridad = EXCLUDED.prioridad,
                    estado = EXCLUDED.estado
                """,
                ordenes,
            )


def obtener_pqrs():
    if not FRAPPE_URL or not FRAPPE_API_KEY or not FRAPPE_API_SECRET:
        raise RuntimeError("Faltan variables de entorno de Frappe.")

    r = requests.get(
        f"{FRAPPE_URL}/api/resource/HD%20Ticket",
        headers={"Authorization": f"token {FRAPPE_API_KEY}:{FRAPPE_API_SECRET}"},
        params={
            "fields": '["name","subject","description","status","priority","creation"]',
            "order_by": "creation desc",
            "limit_page_length": 100,
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("data", [])


@app.route("/")
def inicio():
    try:
        return render_template("index.html", pqrs=obtener_pqrs(), error=None)
    except Exception as e:
        return render_template("index.html", pqrs=[], error=str(e))


@app.route("/health")
def health():
    return {"status": "ok"}, 200


@app.route("/api/finalizar-pqr", methods=["POST"])
def finalizar_pqr():
    """
    Endpoint para que la App del Técnico / PWD solicite finalizar una PQR.
    Actualiza simultáneamente Helpdesk (Frappe) a 'Closed' y Neon a 'FINALIZADA'.
    """
    data = request.get_json() or {}
    id_pqr = data.get("id_pqr")
    estado_frappe = data.get("estado_frappe", "Closed")

    if not id_pqr:
        return jsonify({"error": "El campo 'id_pqr' es obligatorio"}), 400

    try:
        # 1. Actualizar estado en Frappe/Helpdesk
        actualizar_estado_frappe(id_pqr, nuevo_estado=estado_frappe)

        # 2. Actualizar estado en la base de datos Neon
        with psycopg.connect(DATABASE_URL) as conexion:
            with conexion.cursor() as cursor:
                cursor.execute(
                    "UPDATE ordenes_trabajo SET estado = 'FINALIZADA' WHERE id_pqr = %s",
                    (id_pqr,)
                )

        return jsonify({
            "status": "ok", 
            "message": f"PQR {id_pqr} finalizada correctamente en Helpdesk y Neon DB."
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Iniciar el sincronizador (poller.py) en un hilo en segundo plano
def _arrancar_poller_background():
    time.sleep(3)  # Espera breve para asegurar carga completa
    try:
        import poller

        poller.main()
    except Exception as e:
        print(f"[ERROR BACKGROUND POLLER] {e}")


threading.Thread(target=_arrancar_poller_background, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
