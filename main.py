import os
import re
import json
import requests
from flask import Flask, request, jsonify
import anthropic

app = Flask(__name__)

# ── Variables de entorno (se configuran en Render) ───────────────────────────
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "diegoat0301")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# ── System prompt de ARIA ────────────────────────────────────────────────────
SYSTEM_PROMPT = """
Eres ARIA, el agente Chief of Staff de Clinica Dental Sonrisa Plena. Trabajas
para la Dra. Carolina Reyes, fundadora y duena de la clinica. Tu trabajo es
filtrar, resolver y coordinar todo lo administrativo que llega por WhatsApp,
para que ella pueda enfocarse en pacientes y en hacer crecer el negocio.

## CONTEXTO DEL NEGOCIO
- 3 sucursales: Sonrisa Plena Centro, Sonrisa Plena Norte, Sonrisa Plena Sur.
- 14 empleados: dentistas, asistentes y recepcion.
- Servicios: limpiezas, ortodoncia, blanqueamiento, endodoncia, urgencias.
- Horario de atencion: lunes a sabado, 9:00-19:00.
- Proteccion de datos: la informacion clinica es confidencial. Nunca la compartas.

## NIVELES DE AUTONOMIA

Nivel 1 - Resuelves directo:
- Confirmar, reprogramar o cancelar citas (con mas de 24h de anticipacion).
- Responder preguntas frecuentes: horarios, ubicaciones, servicios, pagos.

Nivel 2 - Mencionas que derivaras internamente (sin ejecutar nada):
- Pacientes sin visita en 6+ meses.
- Choques de horario entre sucursales.
- Facturas de proveedores.

Nivel 3 - Escalas a Carolina (dile al paciente que sera contactado pronto):
- Menciones de dolor, sangrado, hinchazon o urgencia medica.
- Quejas o conflictos.
- Solicitudes de descuento o excepciones de precio.
- Temas legales o de seguros.
- Cualquier caso de baja confianza.

## TONO
Con pacientes: calido, tranquilizador, claro. Muchas personas le tienen
ansiedad al dentista, nunca suenes frio ni robotico.
Con proveedores: directo y profesional.

## NUNCA HAGAS
- Prometer precios fuera de la lista oficial.
- Hacer afirmaciones clinicas o diagnosticos.
- Compartir datos de salud de pacientes.

## IMPORTANTE PARA ESTE ENTORNO
Responde siempre en texto plano, sin markdown, sin asteriscos. Se breve,
estas respondiendo por WhatsApp. No incluyas etiquetas de nivel en tu
respuesta final al paciente.
""".strip()


# ── Enviar mensaje de WhatsApp ───────────────────────────────────────────────
def send_whatsapp_message(to, body):
    url = "https://graph.facebook.com/v19.0/" + str(PHONE_NUMBER_ID) + "/messages"
    headers = {
        "Authorization": "Bearer " + str(WHATSAPP_TOKEN),
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


# ── Llamar a ARIA via Claude API ─────────────────────────────────────────────
def ask_aria(user_message):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    text = response.content[0].text
    text = re.sub(r"^\[NIVEL\s*[123]\]\s*\n?", "", text, flags=re.IGNORECASE)
    return text.strip()


# ── Verificacion del webhook (Meta la llama una sola vez) ────────────────────
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("Webhook verificado por Meta")
        return challenge, 200

    print("Verificacion fallida - token incorrecto")
    return "Forbidden", 403


# ── Recibe mensajes reales de WhatsApp ───────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.json or {}

    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        # Estados de entrega (sent, delivered, read, failed)
        if "statuses" in value:
            print("ESTADO DE ENTREGA: " + json.dumps(value["statuses"]))
            return jsonify({"status": "status ok"}), 200

        if "messages" not in value:
            print("PAYLOAD SIN MENSAJES: " + json.dumps(value))
            return jsonify({"status": "no message"}), 200

        message = value["messages"][0]
        from_number = message["from"]

        if message.get("type") != "text":
            send_whatsapp_message(
                from_number,
                "Por ahora solo puedo procesar mensajes de texto. "
                "Escribeme tu consulta y te respondo enseguida."
            )
            return jsonify({"status": "ok"}), 200

        user_text = message["text"]["body"]
        print("MENSAJE DE " + str(from_number) + ": " + str(user_text))

        aria_reply = ask_aria(user_text)
        print("ARIA RESPONDE: " + str(aria_reply))

        resultado = send_whatsapp_message(from_number, aria_reply)
        print("RESPUESTA DE META AL ENVIO: " + json.dumps(resultado))

    except (KeyError, IndexError) as e:
        print("ERROR PROCESANDO EL PAYLOAD: " + str(e))

    # Meta exige que respondas 200 siempre
    return jsonify({"status": "ok"}), 200


# ── Arranque ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
