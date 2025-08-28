import os
import logging
import json
from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse, Connect
from flask_sockets import Sockets
from supabase import create_client, Client as SupabaseClient
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv
from Agent_Kusmus import VoiceAgent
from geventwebsocket.exceptions import WebSocketError

load_dotenv()

# --- Google Cloud Credentials ---
GOOGLE_CREDENTIALS_FILE = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_FILE")
if not GOOGLE_CREDENTIALS_FILE or not os.path.exists(GOOGLE_CREDENTIALS_FILE):
    logging.error("GOOGLE_APPLICATION_CREDENTIALS_FILE not set or file not found.")
    exit()
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_CREDENTIALS_FILE

# --- App Initialization ---
app = Flask(__name__)
sockets = Sockets(app)

# --- Environment Variables & Clients ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# === Twilio Voice and WebSocket Routes ===

# ADDED FOR DEBUGGING
@app.route("/test", methods=['POST'])
def test():
    """A minimal endpoint to test TwiML response."""
    logging.info("✅ /test endpoint was hit successfully!")
    response = VoiceResponse()
    response.say("Hello from the test endpoint. Your server is working.")
    response.hangup()
    return str(response), 200, {'Content-Type': 'text/xml'}


@app.route("/voice", methods=['POST'])
def voice():
    """
    This endpoint responds with TwiML to connect the call to our WebSocket stream.
    This is the modern, recommended approach for bidirectional audio.
    """
    appointment_id = request.args.get('appointment_id')
    if not appointment_id:
        logging.error("'/voice' endpoint called without appointment_id.")
        return ('', 400)

    response = VoiceResponse()
    connect = Connect()
    # The websocket_url now includes the appointment_id to pass context
    websocket_url = f"wss://{request.host}/stream?appointment_id={appointment_id}"
    connect.stream(url=websocket_url)
    response.append(connect)
    # This pause gives the WebSocket time to connect before the call might otherwise end.
    response.pause(length=1)
    logging.info(f"Generated <Connect><Stream> TwiML for WebSocket: {websocket_url}")
    return str(response), 200, {'Content-Type': 'text/xml'}

@sockets.route("/stream")
def stream(ws):
    """
    Handles the bidirectional WebSocket audio stream from Twilio.
    """
    call_sid = None
    stream_sid = None
    appointment_id = request.args.get('appointment_id')

    try:
        if not appointment_id:
            logging.error("WebSocket stream opened without appointment_id.")
            ws.close()
            return

        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        
        # The first message from Twilio is a 'connected' event.
        message = ws.receive()
        if message:
            data = json.loads(message)
            if data.get('event') == 'connected':
                call_sid = data['streamSid'].split('_')[0] # Extract CallSid from StreamSid
                stream_sid = data['streamSid']
                logging.info(f"WebSocket stream connected. CallSid: {call_sid}, StreamSid: {stream_sid}, Appointment ID: {appointment_id}")

                voice_agent = VoiceAgent(supabase, twilio_client, GEMINI_API_KEY, call_sid, stream_sid, appointment_id)
                voice_agent.handle_call(ws)
            else:
                logging.warning(f"WebSocket first message was not a 'connected' event: {data.get('event')}")
        else:
            logging.warning("WebSocket received no initial message.")

    except Exception as e:
        logging.error(f"WebSocket Error: {e}", exc_info=True)
    finally:
        if not ws.closed:
            ws.close()

@app.route("/status", methods=['POST'])
def status():
    """Handles call status callbacks to update the database."""
    call_status = request.values.get('CallStatus')
    call_sid = request.values.get('CallSid')
    if call_status in ['no-answer', 'failed', 'busy']:
        supabase.table('master_appointments').update({'status': 'unreachable'}).eq('last_call_sid', call_sid).execute()
    return ('', 204)


if __name__ == "__main__":
    from gevent import pywsgi
    from geventwebsocket.handler import WebSocketHandler
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 5000))
    logging.info(f"Server listening on http://{host}:{port}")
    server = pywsgi.WSGIServer((host, port), app, handler_class=WebSocketHandler)
    server.serve_forever()