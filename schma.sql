CREATE TABLE master_appointments (
  patient_id UUID DEFAULT gen_random_uuid(),
  patient_name TEXT NOT NULL,
  patient_phone TEXT NOT NULL UNIQUE,
  gender TEXT CHECK (gender IN ('Male', 'Female')),
  age INT CHECK (age > 0),
  blood_group TEXT CHECK (blood_group IN (
    'A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-'
  )),
  genotype TEXT CHECK (genotype IN ('AA', 'AS', 'SS', 'AC', 'SC')),
  location TEXT,
  service_type TEXT CHECK (service_type IN (
    'Antenatal Care', 'Postnatal Care', 'Childbirth Delivery',
    'Immunization', 'Vaccination', 'Family Planning', 'General'
  )),
  emergency_contact_name TEXT,
  emergency_contact_phone TEXT,
  preferred_language TEXT DEFAULT 'English',
  appointment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  appointment_datetime TIMESTAMPTZ NOT NULL,
  status TEXT CHECK (status IN (
    'pending', 'confirmed', 'rescheduled', 'transferred', 'unreachable', 'calling', 'human_escalation', 'failed_escalation'
  )) DEFAULT 'pending',
  handled_by_ai BOOLEAN DEFAULT TRUE,
  volunteer_id UUID,
  volunteer_name TEXT,
  volunteer_phone TEXT,
  volunteer_email TEXT,
  volunteer_role TEXT CHECK (volunteer_role IN ('volunteer', 'admin')),
  volunteer_notes TEXT,
  patient_call_attempts INT DEFAULT 0,
  emergency_call_attempts INT DEFAULT 0,
  last_call_timestamp TIMESTAMPTZ,
  last_call_sid TEXT, -- Added this column to track the Twilio Call SID
  human_escalation_attempts INT DEFAULT 0,
  last_escalation_attempt_timestamp TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_appointments_status_datetime ON master_appointments (status, appointment_datetime);
CREATE INDEX idx_appointments_last_call_sid ON master_appointments (last_call_sid); -- Added index for faster lookups

CREATE TABLE volunteers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  full_name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  password TEXT NOT NULL,
  role TEXT CHECK (role IN ('volunteer', 'local', 'state', 'national', 'supa_user')) DEFAULT 'volunteer',
  spoken_languages TEXT[] DEFAULT '{"English"}',
  phone_number TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE UNIQUE INDEX idx_volunteers_email ON volunteers (email);

CREATE TABLE app_settings (
  setting_key TEXT PRIMARY KEY,
  setting_value TEXT NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now()
);

INSERT INTO app_settings (setting_key, setting_value) VALUES
('TWILIO_ACCOUNT_SID', ''),
('TWILIO_AUTH_TOKEN', ''),
('TWilio_PHONE_NUMBER', ''),
('WEBHOOK_URL', ''),
('GEMINI_API_KEY', '');
