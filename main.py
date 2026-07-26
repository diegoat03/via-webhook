import os
import re
import json
import requests
from flask import Flask, request, jsonify
import anthropic

app = Flask(__name__)

# ── Variables de entorno (las configuras en Render, no acá) ──────────────────
VERIFY_TOKEN   = os.environ.get("VERIFY_TOKEN", "diegoat0301")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")   # token de Meta
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID") # ID del número de prueba
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# ── System prompt de ARIA ─────────────────────────────────────────────────────
SYSTEM_PROMPT = """
Eres ARIA, el agente Chief of Staff de Clínica Dental Sonrisa Plena. Trabajas
para la Dra. Carolina Reyes, fundadora y dueña de la clínica. Tu trabajo es
filtrar, resolver y coordinar todo lo administrativo que le llega por WhatsApp,
para que ella pueda enfocarse en pacientes y en hacer crecer el negocio.

## CONTEXTO DEL NEGOCIO
- 3 sucursales: Sonrisa Plena Centro, Sonrisa Plena Norte, Sonrisa Plena Sur.
- 14 empleados: dentistas, asistentes y recepción.
- Servicios: limpiezas, ortodoncia, blanqueamiento, endodoncia, urgencias.
- Horario de atención: lunes a sábado, 9:00–19:00.
- Protección de datos: información clínica es confidencial. Nunca la compartas.

## NIVELES DE AUTONOMÍA

Nivel 1 — Resuelves directo:
- Confirmar, reprogramar o cancelar citas (con más de 24h de anticipación).
- Responder preguntas frecuentes: horarios, ubicaciones, servicios, pagos.
- Archivar spam.

Nivel 2 — Mencionas que derivarás internamente (sin ejecutar nada):
- Pacientes sin visita en 6+ meses → seguimiento automático.
- Choques de horario entre sucursales → revisión de agenda.
- Facturas de proveedores → procesamiento interno.

Nivel 3 — Escalas a Carolina (dile al paciente que será contactado pronto):
- Menciones de dolor, sangrado, hinchazón o urgencia médica.
- Quejas o conflictos.
- Solicitudes de descuento o excepciones de precio.
- Temas legales o de seguros.
- Cualquier caso de baja confianza.

## TONO
Con pacientes: cálido, tranquilizador, claro. Muchas personas le tienen
ansiedad al dentista — nunca suenes frío ni robótico.
Con proveedores: directo y profesional.

## NUNCA HAGAS
- Prometer precios fuera de la lista oficial.
- Hacer afirmaciones clínicas o diagnósticos.
- Compartir datos de salud de pacientes.

## IMPORTANTE PARA ESTE ENTORNO
Responde siempre en texto plano, sin markdown, sin asteriscos, sin emojis
excesivos. Sé breve — estás respondiendo por WhatsApp, no escribiendo un email.
No incluyas etiquetas de nivel en tu respuesta final al paciente.
""".strip()


# ── Función: enviar mensaje de WhatsApp ──────────────────────────────────────
def send_whatsapp_message(to: str, body: str) -> dict:
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    response = requests.post(url, headers=headers, json=payload, timeout=10)
    return response.json()


# ── Función: llamar a ARIA vía Claude API ────────────────────────────────────
def ask_aria(user_message: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    text = response.content[0].text
    # Elimina etiquetas [NIVEL X] por si el modelo las incluye
    text = re.sub(r"^\[NIVEL\s*[123]\]\s*\n?", "", text, flags=re.IGNORECASE)
    return text.strip()


# ── Ruta GET: verificación del webhook (Meta la llama una sola vez) ──────────
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook verificado por Meta")
        return challenge, 200

    print("❌ Verificación fallida — token incorrecto")
    return "Forbidden", 403


# ── Ruta POST: recibe mensajes reales de WhatsApp ────────────────────────────
@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.json or {}

    try:
        entry   = data["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]

       if "statuses" in value:
            print(f"📊 Estado de entrega: {value['statuses']}")
            return jsonify({"status": "status ok"}), 200

        if "messages" not in value:
            print(f"ℹ️ Payload sin mensajes: {value}")
            return jsonify({"status": "no message"}), 200
        message     = value["messages"][0]
        from_number = message["from"]

        # Solo procesamos mensajes de texto por ahora
        if message.get("type") != "text":
            send_whatsapp_message(
                from_number,
                "Por ahora solo puedo procesar mensajes de texto. "
                "Escríbeme tu consulta y te respondo enseguida."
            )
            return jsonify({"status": "ok"}), 200

        user_text = message["text"]["body"]
        print(f"📩 Mensaje de {from_number}: {user_text}")

        aria_reply = ask_aria(user_text)
        print(f"🤖 ARIA responde: {aria_reply}")

        resultado = send_whatsapp_message(from_number, aria_reply)
        print(f"📤 Respuesta de Meta al envío: {resultado}")

    except (KeyError, IndexError) as e:
        print(f"⚠️ Error procesando el payload: {e}")

    # Meta exige que respondas 200 siempre, aunque haya un error interno
    return jsonify({"status": "ok"}), 200


# ── Arranque ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
