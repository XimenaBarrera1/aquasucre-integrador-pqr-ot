import os
import threading
import time
import requests
import psycopg
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify, request

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


def normalizar_estado_frappe(estado):
    if not estado or not isinstance(estado, str):
        return "PENDIENTE"
    return ESTADOS_FRAPPE.get(estado.strip().upper(), "PENDIENTE")


def normalizar_orden(payload):
    id_pqr = str(payload.get("name") or "").strip()
    if not id_pqr:
        raise ValueError("El campo 'id_pqr' es obligatorio.")

    tipo_servicio = str(payload.get("tipo_servicio") or "Atención PQR").strip()
    descripcion = str(payload.get("descripcion") or "Sin descripción").strip()
    direccion = str(payload.get("direccion") or "No especificada").strip()

    # Ajuste de límites según esquema de Neon PostgreSQL
    id_pqr = id_pqr[:30]
    tipo_servicio = tipo_servicio[:100]
    direccion = direccion[:200]

    prioridad_raw = str(payload.get("prioridad", "MEDIA")).strip().upper()
    prioridad = PRIORIDADES.get(prioridad_raw, "MEDIA")

    estado = normalizar_estado_frappe(payload.get("estado_frappe"))

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
            "fields": '["name","subject","status","priority","creation"]',
            "order_by": "creation desc",
            "limit_page_length": 100
        },
        timeout=20
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
