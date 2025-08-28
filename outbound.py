import os
import logging
import re
from datetime import datetime, timedelta, timezone
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioRestException
from supabase import create_client, Client as SupabaseClient
from dotenv import load_dotenv
import asyncio

load_dotenv()

# --- Environment Variables ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

# --- Constants ---
MAX_PATIENT_CALL_ATTEMPTS = 300
MAX_EMERGENCY_CALL_ATTEMPTS = 300
RETRY_DELAY_MINUTES = 0.1

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def format_phone_number(phone_number):
    if not phone_number:
        return None
    digits_only = re.sub(r'\D', '', phone_number)
    if not digits_only.startswith('234'):
        digits_only = '234' + digits_only.lstrip('0')
    return f"+{digits_only}"

def get_volunteer_for_escalation(supabase: SupabaseClient, language: str):
    try:
        response = supabase.table('volunteers').select('phone_number').ilike('spoken_languages', f'%{language}%').limit(1).execute()
        if response.data:
            return response.data[0].get('phone_number')
    except Exception as e:
        logging.error(f"Error fetching volunteer for language {language}: {e}")
    return None

def make_and_track_outbound_call(supabase: SupabaseClient, twilio_client: TwilioClient, to_phone: str, appointment_id: str, update_data: dict):
    formatted_phone = format_phone_number(to_phone)
    if not formatted_phone:
        logging.warning(f"Invalid phone number: {to_phone}. Skipping call for appointment {appointment_id}.")
        return

    try:
        # Pass the appointment_id to the TwiML app via a URL parameter
        twiML_url = f"{WEBHOOK_URL}/voice?appointment_id={appointment_id}"
        
        call = twilio_client.calls.create(
            to=formatted_phone,
            from_=TWILIO_PHONE_NUMBER,
            url=twiML_url,
            status_callback=f"{WEBHOOK_URL}/status",
            status_callback_event=['completed'],
            status_callback_method='POST'
        )
        logging.info(f"Call initiated to {formatted_phone} for appointment {appointment_id}. SID: {call.sid}")
        
        # Track the Call SID in the database
        update_data['last_call_sid'] = call.sid
        supabase.table('master_appointments').update(update_data).eq('appointment_id', appointment_id).execute()

    except TwilioRestException as e:
        logging.error(f"Twilio API Error for appointment {appointment_id}: {e}")
        supabase.table('master_appointments').update({'status': 'unreachable'}).eq('appointment_id', appointment_id).execute()
    except Exception as e:
        logging.error(f"Unexpected error creating call for appointment {appointment_id}: {e}")

def process_appointments():
    if not all([SUPABASE_URL, SUPABASE_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, WEBHOOK_URL]):
        logging.error("Missing required environment variables.")
        return

    supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    try:
        now_utc = datetime.now(timezone.utc)
        twenty_four_hours_from_now = now_utc + timedelta(hours=24)
        
        # Fetch appointments directly from Supabase
        response = supabase.table('master_appointments').select('*').in_('status', ['pending', 'unreachable']).gte('appointment_datetime', now_utc.isoformat()).lt('appointment_datetime', twenty_four_hours_from_now.isoformat()).execute()
        
        if not response.data:
            logging.info("No pending or unreachable appointments to process.")
            return

        for appt in response.data:
            appt_id = appt['appointment_id']
            last_call_timestamp_str = appt['last_call_timestamp']
            
            if last_call_timestamp_str:
                last_call_time = datetime.fromisoformat(last_call_timestamp_str.replace('Z', '+00:00'))
                if appt.get('patient_call_attempts', 0) == 2 and (now_utc - last_call_time) < timedelta(minutes=RETRY_DELAY_MINUTES):
                    logging.info(f"Appointment {appt_id} is waiting for the 3-minute retry delay. Skipping.")
                    continue
            
            now_iso = now_utc.isoformat()
            patient_attempts = appt.get('patient_call_attempts', 0)
            emergency_attempts = appt.get('emergency_call_attempts', 0)

            if patient_attempts < MAX_PATIENT_CALL_ATTEMPTS:
                logging.info(f"Attempting call {patient_attempts + 1} to patient for appointment {appt_id}.")
                update_payload = {
                    'status': 'calling',
                    'patient_call_attempts': patient_attempts + 1,
                    'last_call_timestamp': now_iso
                }
                make_and_track_outbound_call(supabase, twilio_client, appt['patient_phone'], appt_id, update_payload)
            
            elif emergency_attempts < MAX_EMERGENCY_CALL_ATTEMPTS:
                logging.info(f"Attempting call {emergency_attempts + 1} to emergency contact for {appt_id}.")
                update_payload = {
                    'status': 'calling',
                    'emergency_call_attempts': emergency_attempts + 1,
                    'last_call_timestamp': now_iso
                }
                make_and_track_outbound_call(supabase, twilio_client, appt['emergency_contact_phone'], appt_id, update_payload)
            
            else:
                logging.warning(f"All call attempts failed for appointment {appt_id}. Escalating.")
                volunteer_phone = get_volunteer_for_escalation(supabase, appt['preferred_language'])
                if volunteer_phone:
                    update_payload = { 'status': 'human_escalation', 'last_escalation_attempt_timestamp': now_iso }
                    make_and_track_outbound_call(supabase, twilio_client, volunteer_phone, appt_id, update_payload)
                else:
                    logging.error(f"No volunteer found for language {appt['preferred_language']}. Marking as failed escalation.")
                    supabase.table('master_appointments').update({'status': 'failed_escalation'}).eq('appointment_id', appt_id).execute()

    except Exception as e:
        logging.error(f"An error occurred in the main processing loop: {e}", exc_info=True)

if __name__ == "__main__":
    process_appointments()