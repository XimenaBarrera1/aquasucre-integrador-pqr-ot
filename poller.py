import json
import logging
import os
import time
from datetime import datetime

import psycopg
import requests
from dotenv import load_dotenv

from app import (
    DATABASE_URL,
    FRAPPE_API_KEY,
    FRAPPE_API_SECRET,
    FRAPPE_URL,
    guardar_ordenes,
    normalizar_orden,
)

load_dotenv()

INTERVALO_SEGUNDOS = int(os.getenv("FRAPPE_POLL_INTERVAL", "60"))
CAMPO_TIPO_SERVICIO = os.getenv("FRAPPE_OT_SERVICE_FIELD", "subject")
CAMPO_DESCRIPCION = os.getenv("FRAPPE_OT_DESCRIPTION_FIELD", "description")
CAMPO_DIRECCION = os.getenv("FRAPPE_OT_ADDRESS_FIELD", "address")
TAMANO_PAGINA = 100
ESTADO_SYNC = "HD Ticket"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def inicializar_estado():
    with psycopg.connect(DATABASE_URL) as conexion:
        with conexion.cursor() as cursor:
            # Crear índice único para soportar ON CONFLICT(id_pqr)
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ordenes_trabajo_id_pqr_uidx
                    ON ordenes_trabajo (id_pqr)
                """
            )
            # Tabla para rastrear la última fecha de sincronización
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS integrador_sync_state (
                    origen VARCHAR(100) PRIMARY KEY,
                    ultima_modificacion TIMESTAMP NOT NULL
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO integrador_sync_state (origen, ultima_modificacion)
                VALUES (%s, %s)
                ON CONFLICT (origen) DO NOTHING
                """,
                (ESTADO_SYNC, datetime(1970, 1, 1)),
            )


def obtener_ultima_modificacion():
    with psycopg.connect(DATABASE_URL) as conexion:
        with conexion.cursor() as cursor:
            cursor.execute(
                "SELECT ultima_modificacion FROM integrador_sync_state WHERE origen = %s",
                (ESTADO_SYNC,),
            )
            resultado = cursor.fetchone()
    if resultado is None:
        raise RuntimeError("No se encontró el estado de sincronización.")
    return resultado[0]


def guardar_ultima_modificacion(fecha):
    with psycopg.connect(DATABASE_URL) as conexion:
        with conexion.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integrador_sync_state
                SET ultima_modificacion = GREATEST(ultima_modificacion, %s)
                WHERE origen = %s
                """,
                (fecha, ESTADO_SYNC),
            )


def consultar_pagina(fecha_desde, inicio):
    campos = [
        "name",
        CAMPO_TIPO_SERVICIO,
        CAMPO_DESCRIPCION,
        CAMPO_DIRECCION,
        "priority",
        "status",
        "modified",
    ]
    respuesta = requests.get(
        f"{FRAPPE_URL}/api/resource/HD%20Ticket",
        headers={
            "Authorization": f"token {FRAPPE_API_KEY}:{FRAPPE_API_SECRET}",
        },
        params={
            "fields": json.dumps(list(dict.fromkeys(campos))),
            "filters": json.dumps([["modified", ">", fecha_desde.isoformat(sep=" ")]]),
            "order_by": "modified asc",
            "limit_page_length": TAMANO_PAGINA,
            "limit_start": inicio,
        },
        timeout=30,
    )
    respuesta.raise_for_status()
    data = respuesta.json().get("data")
    if not isinstance(data, list):
        raise RuntimeError("Frappe respondió con un formato de lista inesperado.")
    return data


def actualizar_estado_en_frappe(id_pqr, nuevo_estado="Closed"):
    """Envía una petición PUT a la API de Frappe/Helpdesk para cambiar el estado del ticket."""
    url = f"{FRAPPE_URL}/api/resource/HD%20Ticket/{id_pqr}"
    headers = {
        "Authorization": f"token {FRAPPE_API_KEY}:{FRAPPE_API_SECRET}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.put(
            url, headers=headers, json={"status": nuevo_estado}, timeout=15
        )
        if r.status_code == 200:
            logger.info("PQR %s actualizada exitosamente a '%s' en Helpdesk.", id_pqr, nuevo_estado)
        else:
            logger.warning("No se pudo actualizar PQR %s en Helpdesk: %s", id_pqr, r.text)
    except Exception as e:
        logger.error("Error al conectar con Helpdesk para actualizar PQR %s: %s", id_pqr, e)


def sincronizar_finalizadas_hacia_frappe():
    """Busca PQRs finalizadas en Neon DB que sigan abiertas en Helpdesk y las cierra."""
    with psycopg.connect(DATABASE_URL) as conexion:
        with conexion.cursor() as cursor:
            cursor.execute("SELECT id_pqr FROM ordenes_trabajo WHERE estado = 'FINALIZADA'")
            finalizadas_db = {row[0] for row in cursor.fetchall()}

    if not finalizadas_db:
        return

    # Consultar tickets en Frappe que están en Open o Replied
    respuesta = requests.get(
        f"{FRAPPE_URL}/api/resource/HD%20Ticket",
        headers={
            "Authorization": f"token {FRAPPE_API_KEY}:{FRAPPE_API_SECRET}",
        },
        params={
            "fields": json.dumps(["name", "status"]),
            "filters": json.dumps([["status", "in", ["Open", "Replied"]]]),
            "limit_page_length": TAMANO_PAGINA,
        },
        timeout=20,
    )
    respuesta.raise_for_status()
    tickets_abiertos = respuesta.json().get("data", [])

    for ticket in tickets_abiertos:
        id_pqr = ticket.get("name")
        if id_pqr in finalizadas_db:
            logger.info(
                "PQR %s está FINALIZADA en Neon pero '%s' en Helpdesk. Sincronizando a 'Closed'...",
                id_pqr,
                ticket.get("status"),
            )
            actualizar_estado_en_frappe(id_pqr, "Closed")


def sincronizar_cambios():
    ultima_modificacion = obtener_ultima_modificacion()
    inicio = 0
    procesadas = 0
    nueva_ultima_modificacion = None

    while True:
        tickets = consultar_pagina(ultima_modificacion, inicio)
        if not tickets:
            break

        ordenes = []
        for ticket in tickets:
            datos_orden = {
                "name": ticket.get("name"),
                "tipo_servicio": ticket.get(CAMPO_TIPO_SERVICIO),
                "descripcion": ticket.get(CAMPO_DESCRIPCION),
                "direccion": ticket.get(CAMPO_DIRECCION),
                "prioridad": ticket.get("priority", "MEDIA"),
                "estado_frappe": ticket.get("status"),
            }
            ordenes.append(normalizar_orden(datos_orden))

            modificada = ticket.get("modified")
            if not isinstance(modificada, str):
                raise RuntimeError(
                    f"El ticket {ticket.get('name', '(sin nombre)')} no tiene fecha modified."
                )
            if nueva_ultima_modificacion is None or modificada > nueva_ultima_modificacion:
                nueva_ultima_modificacion = modificada

        guardar_ordenes(ordenes)
        procesadas += len(tickets)
        inicio += len(tickets)

        if len(tickets) < TAMANO_PAGINA:
            break

    if nueva_ultima_modificacion is not None:
        guardar_ultima_modificacion(
            datetime.fromisoformat(nueva_ultima_modificacion)
        )

    return procesadas


def main():
    if not FRAPPE_URL or not FRAPPE_API_KEY or not FRAPPE_API_SECRET:
        raise RuntimeError(
            "Configura FRAPPE_URL, FRAPPE_API_KEY y FRAPPE_API_SECRET en Render."
        )
    if not DATABASE_URL:
        raise RuntimeError("Configura DATABASE_URL en Render.")
    if INTERVALO_SEGUNDOS < 5:
        raise ValueError("FRAPPE_POLL_INTERVAL debe ser de al menos 5 segundos.")

    inicializar_estado()
    logger.info(
        "Sincronizador iniciado. Revisará Frappe cada %s segundos.",
        INTERVALO_SEGUNDOS,
    )

    while True:
        try:
            # 1. Traer novedades desde Frappe -> Neon
            cantidad = sincronizar_cambios()
            if cantidad:
                logger.info("Sincronizados %s tickets de Frappe a Neon PostgreSQL.", cantidad)

            # 2. Enviar PQRs finalizadas desde Neon -> Frappe
            sincronizar_finalizadas_hacia_frappe()

        except (psycopg.Error, requests.RequestException, ValueError, RuntimeError):
            logger.exception("Falló la sincronización; se volverá a intentar en el siguiente ciclo.")
        time.sleep(INTERVALO_SEGUNDOS)


if __name__ == "__main__":
    main()
