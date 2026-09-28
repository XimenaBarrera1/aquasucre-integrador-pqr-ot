import os
import requests
from flask import Flask, render_template

app = Flask(__name__)

FRAPPE_URL = os.getenv("FRAPPE_URL", "").rstrip("/")
FRAPPE_API_KEY = os.getenv("FRAPPE_API_KEY", "")
FRAPPE_API_SECRET = os.getenv("FRAPPE_API_SECRET", "")

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
