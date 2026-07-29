import os
import re
import json
import time
import hmac
import hashlib
import threading
from collections import deque

import requests
from flask import Flask, request, jsonify
import anthropic

app = Flask(__name__)

# ── Variables de entorno (se configuran en Render) ───────────────────────────
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
APP_SECRET = os.environ.get("APP_SECRET")        # "App Secret" de la app de Meta
MEMORIA_TOKEN = os.environ.get("MEMORIA_TOKEN")  # protege el endpoint /memoria

MODELO = os.environ.get("MODELO", "claude-sonnet-4-6")

# Aviso temprano si falta configuracion, en vez de fallar en silencio despues
_faltantes = [
    nombre
    for nombre, valor in [
        ("VERIFY_TOKEN", VERIFY_TOKEN),
        ("WHATSAPP_TOKEN", WHATSAPP_TOKEN),
        ("PHONE_NUMBER_ID", PHONE_NUMBER_ID),
        ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
        ("APP_SECRET", APP_SECRET),
    ]
    if not valor
]
if _faltantes:
    print("ADVERTENCIA: faltan variables de entorno: " + ", ".join(_faltantes))

# Un solo cliente para todo el proceso, no uno por mensaje
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Memoria de conversaciones ────────────────────────────────────────────────
# Estructura: { "56961909340": {"mensajes": [...], "ultimo": 1234567890.0} }
# Vive en RAM del proceso: se pierde al reiniciar y NO se comparte entre
# workers de gunicorn. Arrancar con un solo worker y varios hilos.
conversaciones = {}
memoria_lock = threading.Lock()

MAX_MENSAJES = 20          # ultimos 20 mensajes (10 idas y vueltas)
EXPIRA_SEGUNDOS = 86400    # 24 horas sin actividad y se borra

# Meta reintenta los webhooks: guardamos los ids ya atendidos para no
# responder dos veces el mismo mensaje ni pagar la llamada dos veces.
ids_procesados = deque(maxlen=500)
ids_lock = threading.Lock()

LIMITE_WHATSAPP = 4096     # tope de caracteres de un mensaje de texto

MENSAJE_DE_ERROR = (
    "Disculpa, tuve un problema tecnico al procesar tu mensaje. "
    "Intenta de nuevo en un momento por favor."
)


def limpiar_conversaciones_viejas():
    """Borra conversaciones sin actividad en las ultimas 24 horas.

    Se llama siempre con memoria_lock tomado.
    """
    ahora = time.time()
    vencidas = [
        numero
        for numero, datos in conversaciones.items()
        if ahora - datos["ultimo"] > EXPIRA_SEGUNDOS
    ]
    for numero in vencidas:
        del conversaciones[numero]
        print("MEMORIA: conversacion expirada para " + str(numero))


def obtener_historial(numero):
    """Devuelve una copia del historial de un numero, o lista vacia si no hay."""
    with memoria_lock:
        limpiar_conversaciones_viejas()
        if numero not in conversaciones:
            return []
        return list(conversaciones[numero]["mensajes"])


def guardar_intercambio(numero, mensaje_usuario, respuesta_aria):
    """Agrega el mensaje del usuario y la respuesta de ARIA al historial."""
    with memoria_lock:
        if numero not in conversaciones:
            conversaciones[numero] = {"mensajes": [], "ultimo": time.time()}

        mensajes = conversaciones[numero]["mensajes"]
        mensajes.append({"role": "user", "content": mensaje_usuario})
        mensajes.append({"role": "assistant", "content": respuesta_aria})

        # Recorta a los ultimos MAX_MENSAJES para no inflar el costo. Como
        # siempre agregamos de a pares, el corte nunca deja el historial
        # empezando por un mensaje del assistant.
        if len(mensajes) > MAX_MENSAJES:
            del mensajes[:-MAX_MENSAJES]

        conversaciones[numero]["ultimo"] = time.time()


def ya_procesado(msg_id):
    """True si este wamid ya fue atendido en un envio anterior de Meta."""
    if not msg_id:
        return False
    with ids_lock:
        if msg_id in ids_procesados:
            return True
        ids_procesados.append(msg_id)
        return False


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

## MEMORIA DE LA CONVERSACION
Recibes el historial completo de tu conversacion con este paciente. Usalo:
no vuelvas a preguntar algo que el paciente ya te respondio, y retoma el hilo
de forma natural. Si ya te dijo su nombre, su sucursal o el motivo de la
consulta, dalo por sabido.

## IMPORTANTE PARA ESTE ENTORNO
Responde siempre en texto plano, sin markdown, sin asteriscos. Se breve,
estas respondiendo por WhatsApp. No incluyas etiquetas de nivel en tu
respuesta final al paciente.
""".strip()


# ── Enviar mensaje de WhatsApp ───────────────────────────────────────────────
def send_whatsapp_message(to, body):
    body = (body or "").strip()[:LIMITE_WHATSAPP]
    if not body:
        print("ENVIO OMITIDO: cuerpo vacio")
        return {}

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

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
    except requests.RequestException as e:
        print("ERROR DE RED AL ENVIAR: " + repr(e))
        return {}

    if response.status_code >= 400:
        print("ERROR DE META " + str(response.status_code) + ": " + response.text[:500])
        return {}

    try:
        return response.json()
    except ValueError:
        print("RESPUESTA NO JSON DE META: " + response.text[:500])
        return {}


# ── Llamar a ARIA via Claude API, con historial ──────────────────────────────
def ask_aria(user_message, historial):
    # El historial previo mas el mensaje nuevo
    mensajes = list(historial)
    mensajes.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model=MODELO,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        # Sonnet 4.6 usa effort "high" por defecto. Para WhatsApp priorizamos
        # latencia y costo, que es justo para lo que sirve "low".
        output_config={"effort": "low"},
        messages=mensajes,
    )

    # content es una lista de bloques y el primero no siempre es de texto
    texto = next((b.text for b in response.content if b.type == "text"), "")
    texto = re.sub(r"^\[NIVEL\s*[123]\]\s*\n?", "", texto, flags=re.IGNORECASE)
    return texto.strip()


# ── Validacion de la firma de Meta ───────────────────────────────────────────
def firma_valida(req):
    """Verifica X-Hub-Signature-256 para que solo Meta pueda escribirnos."""
    if not APP_SECRET:
        # Sin APP_SECRET no se puede validar. Se deja pasar para no dejar el
        # bot caido, pero configuralo: sin el, cualquiera puede enviar
        # mensajes falsos. Para exigirlo, cambia este return a False.
        print("ADVERTENCIA: APP_SECRET sin configurar, firma NO validada")
        return True

    recibida = req.headers.get("X-Hub-Signature-256", "")
    esperada = "sha256=" + hmac.new(
        APP_SECRET.encode("utf-8"), req.get_data(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(recibida, esperada)


# ── Verificacion del webhook (Meta la llama una sola vez) ────────────────────
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and VERIFY_TOKEN and token == VERIFY_TOKEN:
        print("Webhook verificado por Meta")
        return challenge or "", 200

    print("Verificacion fallida - token incorrecto")
    return "Forbidden", 403


# ── Procesamiento del mensaje (corre en un hilo aparte) ──────────────────────
def procesar_mensaje(message):
    """Todo el trabajo lento: Claude + envio. Nada puede escapar de aqui."""
    try:
        from_number = message["from"]

        if message.get("type") != "text":
            send_whatsapp_message(
                from_number,
                "Por ahora solo puedo procesar mensajes de texto. "
                "Escribeme tu consulta y te respondo enseguida.",
            )
            return

        user_text = message["text"]["body"]
        print("MENSAJE DE " + str(from_number) + ": " + str(user_text))

        historial = obtener_historial(from_number)
        print("MEMORIA: " + str(len(historial)) + " mensajes previos con este numero")

        try:
            aria_reply = ask_aria(user_text, historial)
        except Exception as e:
            print("ERROR LLAMANDO A CLAUDE: " + repr(e))
            send_whatsapp_message(from_number, MENSAJE_DE_ERROR)
            return

        if not aria_reply:
            print("ARIA DEVOLVIO TEXTO VACIO")
            send_whatsapp_message(from_number, MENSAJE_DE_ERROR)
            return

        print("ARIA RESPONDE: " + str(aria_reply))

        guardar_intercambio(from_number, user_text, aria_reply)

        resultado = send_whatsapp_message(from_number, aria_reply)
        print("RESPUESTA DE META AL ENVIO: " + json.dumps(resultado))

    except Exception as e:
        print("ERROR PROCESANDO EL MENSAJE: " + repr(e))


# ── Recibe mensajes reales de WhatsApp ───────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def receive_message():
    if not firma_valida(request):
        print("FIRMA INVALIDA - peticion rechazada")
        return "Forbidden", 403

    data = request.json or {}

    try:
        value = data["entry"][0]["changes"][0]["value"]

        # Estados de entrega (sent, delivered, read, failed)
        if "statuses" in value:
            print("ESTADO DE ENTREGA: " + json.dumps(value["statuses"]))
            return jsonify({"status": "status ok"}), 200

        if "messages" not in value:
            print("PAYLOAD SIN MENSAJES: " + json.dumps(value))
            return jsonify({"status": "no message"}), 200

        # Un payload puede traer mas de un mensaje
        for message in value["messages"]:
            msg_id = message.get("id")
            if ya_procesado(msg_id):
                print("DUPLICADO IGNORADO: " + str(msg_id))
                continue
            # En hilo aparte: Claude tarda mas de lo que Meta espera el 200,
            # y si Meta no recibe el 200 a tiempo reenvia el mismo mensaje.
            threading.Thread(
                target=procesar_mensaje, args=(message,), daemon=True
            ).start()

    except (KeyError, IndexError) as e:
        print("ERROR PROCESANDO EL PAYLOAD: " + str(e))

    # Meta exige que respondas 200 siempre
    return jsonify({"status": "ok"}), 200


# ── Endpoint opcional para revisar la memoria (util para debug) ──────────────
@app.route("/memoria", methods=["GET"])
def ver_memoria():
    # Expone numeros de pacientes: nunca dejarlo abierto
    if not MEMORIA_TOKEN or request.args.get("token") != MEMORIA_TOKEN:
        return "Forbidden", 403

    with memoria_lock:
        resumen = {
            numero: {
                "cantidad_mensajes": len(datos["mensajes"]),
                "ultimo_contacto": datos["ultimo"],
            }
            for numero, datos in conversaciones.items()
        }
    return jsonify(resumen), 200


# ── Healthcheck ──────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "modelo": MODELO}), 200


# ── Arranque ─────────────────────────────────────────────────────────────────
# En Render usa gunicorn, no esto:
#   gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT main:app
# Un solo worker porque la memoria vive en RAM del proceso.
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
