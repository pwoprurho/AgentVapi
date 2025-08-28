import os
import json
import base64
import threading
import queue
import logging
import re
import wave
from twilio.rest import Client as TwilioClient
from supabase import create_client, Client as SupabaseClient
from geventwebsocket.exceptions import WebSocketError
from google.cloud import speech
from google.cloud import texttospeech
import google.generativeai as genai

# Set up logging for the agent
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class VoiceAgent:
    def __init__(self, supabase: SupabaseClient, twilio_client: TwilioClient, gemini_key: str, call_sid: str, stream_sid: str, appointment_id: str):
        self.supabase = supabase
        self.twilio_client = twilio_client
        self.gemini_key = gemini_key
        genai.configure(api_key=self.gemini_key)
        
        # Initialize Google Cloud clients
        self.stt_client = speech.SpeechClient()
        self.tts_client = texttospeech.TextToSpeechClient()
        
        self.call_sid = call_sid
        self.stream_sid = stream_sid
        self.appointment_id = appointment_id
        self.patient_info = None
        self.patient_preferred_language = "English"

        self.audio_queue = queue.Queue()
        self.tts_audio_queue = queue.Queue()
        self.conversation_history = []
        
        self.language_map = {
            'English': {'stt_code': 'en-US', 'tts_code': 'en-US', 'tts_voice_name': 'en-US-Wavenet-F'},
            'Hausa':   {'stt_code': 'ha-NG', 'tts_code': 'ha-NG', 'tts_voice_name': 'ha-NG-Wavenet-A'},
            'Igbo':    {'stt_code': 'ig-NG', 'tts_code': 'ig-NG', 'tts_voice_name': 'ig-NG-Wavenet-A'},
            'Yoruba':  {'stt_code': 'yo-NG', 'tts_code': 'yo-NG', 'tts_voice_name': 'yo-NG-Wavenet-A'},
            'Pidgin':  {'stt_code': 'en-NG', 'tts_code': 'en-NG', 'tts_voice_name': 'en-NG-Wavenet-B'}
        }
        
        self.inbound_audio_file = f"inbound_{self.call_sid}.wav"
        self.outbound_audio_file = f"outbound_{self.call_sid}.wav"
        self.inbound_writer = None
        self.outbound_writer = None

    def handle_call(self, ws):
        try:
            self._setup_audio_writers()
            self._get_patient_info_from_db()

            transcription_thread = threading.Thread(target=self._start_transcription_stream, args=(ws,))
            transcription_thread.start()

            tts_thread = threading.Thread(target=self._send_tts_audio, args=(ws,))
            tts_thread.start()

            self._send_initial_greeting()

            while not ws.closed:
                message = ws.receive()
                if message is None:
                    continue
                
                data = json.loads(message)
                if data['event'] == "media":
                    audio_payload = data['media']['payload']
                    if audio_payload:
                        audio_chunk = base64.b64decode(audio_payload)
                        self.audio_queue.put(audio_chunk)
                        if self.inbound_writer:
                           self.inbound_writer.writeframes(audio_chunk)
                elif data['event'] == "stop":
                    logging.info(f"Media stream stopped for call_sid: {self.call_sid}")
                    break
        except Exception as e:
            logging.error(f"An error occurred during call handling: {e}")
        finally:
            self.audio_queue.put(None)
            self.tts_audio_queue.put(None)
            self._close_audio_writers()
            logging.info(f"WebSocket connection closed for call_sid {self.call_sid}.")

    def _get_patient_info_from_db(self):
        try:
            response = self.supabase.from_('master_appointments').select("*").eq('appointment_id', self.appointment_id).execute()
            if response.data and len(response.data) > 0:
                self.patient_info = response.data[0]
                self.patient_preferred_language = self.patient_info.get('preferred_language', 'English')
                logging.info(f"Fetched patient info for {self.patient_info.get('patient_name')} in {self.patient_preferred_language}")
            else:
                logging.warning(f"No patient info found for appointment ID: {self.appointment_id}")
        except Exception as e:
            logging.error(f"Failed to fetch patient data from Supabase: {e}")
    
    def _synthesize_text_to_speech(self, text):
        logging.info(f"Synthesizing text: '{text}' in {self.patient_preferred_language}")
        try:
            lang_settings = self.language_map.get(self.patient_preferred_language, self.language_map['English'])
            
            synthesis_input = texttospeech.SynthesisInput(text=text)

            voice = texttospeech.VoiceSelectionParams(
                language_code=lang_settings['tts_code'],
                name=lang_settings['tts_voice_name']
            )

            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MULAW,
                sample_rate_hertz=8000
            )

            # ADDED: Log the exact request details before the API call
            logging.info(f"Requesting TTS with voice: {lang_settings['tts_voice_name']} and language: {lang_settings['tts_code']}")

            response = self.tts_client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config
            )
            
            chunk_size = 2048
            for i in range(0, len(response.audio_content), chunk_size):
                yield response.audio_content[i:i + chunk_size]

        except Exception as e:
            # UPDATED: Added exc_info=True for a full error traceback
            logging.error(f"Error in Google Cloud TTS synthesis: {e}", exc_info=True)
            return

    def _send_initial_greeting(self):
        if self.patient_info:
            patient_name = self.patient_info.get('patient_name', 'there')
            greeting_text = f"Hello {patient_name}, this is an automated call from Safemama Pikin Initiative to confirm your upcoming appointment. Is this a good time to talk?"
        else:
            greeting_text = "Hello, this is an automated call from Safemama Pikin Initiative. Is this a good time to talk?"
        
        for audio_chunk in self._synthesize_text_to_speech(greeting_text):
            self.tts_audio_queue.put(audio_chunk)

    def _start_transcription_stream(self, ws):
        logging.info("Transcription stream started.")
        language_code = self.language_map.get(self.patient_preferred_language, {}).get('stt_code', 'en-US')
        
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.MULAW,
            sample_rate_hertz=8000,
            language_code=language_code
        )
        streaming_config = speech.StreamingRecognitionConfig(config=config, interim_results=False)

        def request_generator():
            while True:
                chunk = self.audio_queue.get()
                if chunk is None:
                    break
                yield speech.StreamingRecognizeRequest(audio_content=chunk)

        try:
            responses = self.stt_client.streaming_recognize(streaming_config, request_generator())
            for response in responses:
                if response.results and response.results[0].alternatives:
                    transcript = response.results[0].alternatives[0].transcript
                    logging.info(f"Transcription: {transcript}")
                    gemini_response = self._get_gemini_response(transcript)
                    if gemini_response:
                        for audio_chunk in self._synthesize_text_to_speech(gemini_response):
                            self.tts_audio_queue.put(audio_chunk)
        except Exception as e:
            logging.error(f"Transcription stream error: {e}")

    def _get_gemini_response(self, user_text):
        self.conversation_history.append({"role": "user", "parts": [{"text": user_text}]})

        appointment_datetime = self.patient_info.get('appointment_datetime', 'your upcoming appointment')
        patient_name = self.patient_info.get('patient_name', 'patient')

        prompt = f"""
        You are a friendly, empathetic, and efficient AI assistant for Safemama Pikin Initiative.
        Your primary goal is to confirm a patient's upcoming appointment or help them reschedule if they cannot make it.
        Keep your spoken responses short, clear, and to the point.

        PATIENT AND APPOINTMENT CONTEXT:
        - Patient Name: {patient_name}
        - Appointment Details: {appointment_datetime}
        - Preferred Language: {self.patient_preferred_language}

        AVAILABLE ACTIONS:
        1.  Confirm Appointment: If the user indicates they can make it (e.g., "yes", "I will be there", "confirmed").
        2.  Reschedule Appointment: If the user cannot make it or asks to reschedule (e.g., "no", "I can't make it", "can we change the time?").
        3.  Clarify: If the user's response is unclear, ask for clarification.

        YOUR TASK:
        Analyze the user's last message and the conversation history.
        Return a JSON object with two keys:
        - "intent": a string that must be one of "confirm", "reschedule", or "clarify".
        - "response_text": a string containing the exact words you will say to the patient.

        CONVERSATION HISTORY:
        {self.conversation_history}

        User's last message: "{user_text}"

        Your JSON response:
        """

        try:
            model = genai.GenerativeModel('gemini-pro')
            response = model.generate_content(prompt)
            
            clean_response = re.search(r'```json\n(.*)\n```', response.text, re.DOTALL)
            if clean_response:
                json_text = clean_response.group(1)
            else:
                json_text = response.text

            decision = json.loads(json_text)
            intent = decision.get("intent")
            response_text = decision.get("response_text")

            if intent == 'confirm':
                self._update_appointment_status('confirmed')
                logging.info(f"Gemini intent: CONFIRM for appointment {self.appointment_id}")
            elif intent == 'reschedule':
                self._update_appointment_status('rescheduled')
                logging.info(f"Gemini intent: RESCHEDULE for appointment {self.appointment_id}")
            else:
                logging.info(f"Gemini intent: CLARIFY for appointment {self.appointment_id}")

            self.conversation_history.append({"role": "model", "parts": [{"text": response_text}]})
            return response_text

        except (json.JSONDecodeError, AttributeError) as e:
            logging.error(f"Error parsing Gemini JSON response: {e}. Raw response: {response.text}")
            return "I'm sorry, I'm having a little trouble at the moment. Someone from our team will call you back shortly."
        except Exception as e:
            logging.error(f"Error getting Gemini response: {e}")
            return "I'm sorry, I'm having some trouble at the moment. Please call us back at your convenience."

    def _update_appointment_status(self, new_status):
        try:
            self.supabase.from_('master_appointments').update({'status': new_status}).eq('appointment_id', self.appointment_id).execute()
            logging.info(f"Updated appointment {self.appointment_id} to status '{new_status}'.")
        except Exception as e:
            logging.error(f"Failed to update appointment status: {e}")

    def _send_tts_audio(self, ws):
        logging.info("TTS audio stream started.")
        while True:
            audio_data = self.tts_audio_queue.get()
            if audio_data is None:
                break
            
            payload = base64.b64encode(audio_data).decode('utf-8')
            message = {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": payload}
            }
            try:
                ws.send(json.dumps(message))
                if self.outbound_writer:
                    self.outbound_writer.writeframes(audio_data)
            except WebSocketError as e:
                logging.error(f"WebSocket send error: {e}")
                break
                
        logging.info("TTS audio stream ended.")

    def _setup_audio_writers(self):
        try:
            audio_dir = "audio_logs"
            if not os.path.exists(audio_dir):
                os.makedirs(audio_dir)
            
            inbound_path = os.path.join(audio_dir, self.inbound_audio_file)
            outbound_path = os.path.join(audio_dir, self.outbound_audio_file)
            
            self.inbound_writer = wave.open(inbound_path, 'wb')
            self.inbound_writer.setnchannels(1)
            self.inbound_writer.setsampwidth(1)
            self.inbound_writer.setframerate(8000)

            self.outbound_writer = wave.open(outbound_path, 'wb')
            self.outbound_writer.setnchannels(1)
            self.outbound_writer.setsampwidth(1)
            self.outbound_writer.setframerate(8000)
            
            logging.info(f"Audio writers set up for inbound: {inbound_path} and outbound: {outbound_path}")
            
        except Exception as e:
            logging.error(f"Failed to set up audio writers: {e}")

    def _close_audio_writers(self):
        if self.inbound_writer:
            self.inbound_writer.close()
        if self.outbound_writer:
            self.outbound_writer.close()