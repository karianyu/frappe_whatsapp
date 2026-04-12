
import time
from werkzeug.wrappers import Response
import frappe.utils

import frappe
import json
import requests
from healthcare.healthcare.api.patient_portal import (
            get_departments, get_practitioners, get_slots,
            make_appointment, get_fees, get_appointments
        )
from datetime import date, datetime

import re



# ──────────────────────────────────────────────────────────────
# NLP Layer — ollama-powered intent & entity extraction
# ──────────────────────────────────────────────────────────────

import re

NLP_SYSTEM_PROMPT = """You are a medical appointment assistant entity extractor.
Given a patient's WhatsApp message, extract structured data as JSON.
Return ONLY valid JSON, no markdown, no explanation.

Fields to extract (use null if not mentioned):
{{
  "intent": "book_appointment" | "view_appointments" | "cancel" | "pay" | "menu" | "confirm" | "other",
  "department_hint": "string or null",
  "doctor_hint": "string or null",
  "date_hint": "YYYY-MM-DD or null",
  "slot_hint": "HH:MM or null",
  "raw_affirmative": true | false
}}

Today's date is {today}. Resolve relative dates (tomorrow, next Monday, etc.) to YYYY-MM-DD."""


def extract_nlp_entities(text: str) -> dict:
    """
    Use Ollama to extract intent and entities from free-form patient text.
    Returns a dict with extracted fields, or an empty dict on failure.
    """
    today = datetime.today().strftime("%Y-%m-%d")
    system = NLP_SYSTEM_PROMPT.format(today=today)
    # system = NLP_SYSTEM_PROMPT.replace("{today}", today)

    try:
        raw = chat_ollama(prompt=text, system=system)
        # Strip any accidental markdown fences
        clean = re.sub(r"```(?:json)?|```", "", raw).strip()
        return json.loads(clean)
    except Exception as e:
        frappe.log_error("NLP entity extraction failed", text, e)
        return {}


def resolve_department(hint: str) -> dict | None:
    """Fuzzy-match a department hint against available departments."""
    if not hint:
        return None
    depts = get_departments()
    hint_lower = hint.lower()
    for dept in depts:
        if hint_lower in dept["department"].lower() or dept["department"].lower() in hint_lower:
            return dept
    return None


def resolve_doctor(hint: str, department: str | None = None) -> dict | None:
    """Fuzzy-match a doctor hint, optionally within a department."""
    if not hint:
        return None
    doctors = get_practitioners(department) if department else get_practitioners()
    hint_lower = hint.lower()
    for doc in doctors:
        if hint_lower in doc["practitioner_name"].lower():
            return doc
    return None


def merge_nlp_into_session(session: dict, entities: dict, wa_number: str) -> tuple[dict, str | None]:
    """
    Merge NLP-extracted entities into the session dict.
    Returns (updated_session, optional_clarification_message).
    
    Only fills fields not already confirmed in the session.
    Returns a clarification string if the LLM resolved something ambiguous.
    """
    notes = []

    intent = entities.get("intent")

    # Map intents to step transitions (only if session is fresh)
    if intent == "book_appointment" and session.get("step", "start") in ("start", None):
        session["step"] = "book_appointment"

    elif intent == "view_appointments" and session.get("step", "start") == "start":
        session["step"] = "viewing_appointments"

    elif intent == "menu":
        session.clear()
        return session, None

    elif intent == "confirm" or entities.get("raw_affirmative"):
        # Let the step router handle "confirm" naturally via the text
        pass

    # Department — only fill if not yet in session
    if not session.get("department") and entities.get("department_hint"):
        dept = resolve_department(entities["department_hint"])
        if dept:
            session["department"] = dept["name"]
            notes.append(f"🏥 Department: _{dept['department']}_")

    # Doctor — only fill if department is resolved and doctor not yet set
    if not session.get("practitioner") and entities.get("doctor_hint"):
        dept_name = session.get("department")
        doc = resolve_doctor(entities["doctor_hint"], dept_name)
        if doc:
            session["practitioner"] = doc["name"]
            session["practitioner_name"] = doc["practitioner_name"]
            notes.append(f"👨‍⚕️ Doctor: _Dr. {doc['practitioner_name']}_")

    # Date — only fill if not yet set
    if not session.get("date") and entities.get("date_hint"):
        raw_date = entities["date_hint"]  # already YYYY-MM-DD from LLM
        try:
            parsed = datetime.strptime(raw_date, "%Y-%m-%d").date()
            if parsed >= date.today():
                # Store in dd-mm-YYYY as the rest of the app expects
                session["date"] = parsed.strftime("%d-%m-%Y")
                notes.append(f"📅 Date: _{parsed.strftime('%d %b %Y')}_")
        except ValueError:
            pass

    # Slot
    if not session.get("slot") and entities.get("slot_hint"):
        session["slot_hint"] = entities["slot_hint"]  # stored as hint; confirmed after slot list shown

    clarification = None
    if notes:
        clarification = "✅ Understood:\n" + "\n".join(notes) + "\n\n"

    return session, clarification


# ──────────────────────────────────────────────────────────────
# Auto-advance: skip steps whose data is already in session
# ──────────────────────────────────────────────────────────────

def auto_advance_booking(session: dict, cache, session_key: str) -> str | None:
    """
    If the session already has enough data for the current step, advance it
    and return the next prompt (or None if nothing to skip).
    Called after NLP merge so partially-filled sessions jump ahead.
    """
    step = session.get("step", "start")

    # Already have department → skip department selection
    if step == "book_appointment" and session.get("department"):
        if session.get("practitioner"):
            # Have both dept + doctor → jump to date
            session["step"] = "select_date"
            cache.set(session_key, json.dumps(session), TTL)
            if session.get("date"):
                # Have date too → jump to slot selection
                return _prompt_slots(session, cache, session_key)
            return (
                f"👨‍⚕️ Dr. *{session['practitioner_name']}* selected.\n\n"
                f"📅 What date? Reply in `dd-mm-YYYY` format, e.g. `{get_today_ddmmyy()}`"
            )
        session["step"] = "select_doctor"
        cache.set(session_key, json.dumps(session), TTL)
        return doctor_list(session["department"])

    if step == "select_doctor" and session.get("practitioner"):
        session["step"] = "select_date"
        cache.set(session_key, json.dumps(session), TTL)
        if session.get("date"):
            return _prompt_slots(session, cache, session_key)
        return (
            f"👨‍⚕️ Dr. *{session['practitioner_name']}* selected.\n\n"
            f"📅 Reply with date in `dd-mm-YYYY`, e.g. `{get_today_ddmmyy()}`"
        )

    if step == "select_date" and session.get("date"):
        return _prompt_slots(session, cache, session_key)

    return None  # nothing to skip


def _prompt_slots(session: dict, cache, session_key: str) -> str:
    """Fetch and display available slots, auto-selecting if a hint matches."""
    slots = get_slots(session["practitioner"], session["date"])
    if not slots:
        session.pop("date", None)
        cache.set(session_key, json.dumps(session), TTL)
        return (
            f"😔 No slots on *{session['date']}*.\n"
            f"Try a different date (dd-mm-YYYY):"
        )

    session["step"] = "select_slot"
    cache.set(session_key, json.dumps(session), TTL)

    # If LLM extracted a time hint, try to auto-match
    hint = session.pop("slot_hint", None)
    if hint:
        for i, s in enumerate(slots):
            if s.startswith(hint[:5]):  # HH:MM prefix match
                session["slot"] = s
                session["step"] = "confirm"
                fees = get_fees(session["practitioner"], session["date"])
                charge = fees["details"].get("practitioner_charge", 1) if frappe.db.get_value("PesaPal", "PesaPal", "live") else 1
                session["fees"] = charge
                cache.set(session_key, json.dumps(session), TTL)
                return (
                    f"📋 *Appointment Summary*\n\n"
                    f"👨‍⚕️ Doctor: Dr. {session['practitioner_name']}\n"
                    f"📅 Date: {session['date']}\n"
                    f"⏰ Time: {s}\n"
                    f"💰 Fee: KSH {charge}\n\n"
                    f"📝 Reply *CONFIRM* to book."
                )

    slot_text = "\n".join([f"{i+1}. {s}" for i, s in enumerate(slots)])
    return f"🕐 Available slots for *{session['date']}*:\n\n{slot_text}\n\nReply with a number."


def chat_ollama(
    prompt: str,
    model: str = frappe.db.get_value("Healthcare Settings","Healthcare Settings","model"),
    system: str | None = None,
    host: str = f'http://{frappe.db.get_value("Healthcare Settings","Healthcare Settings","ai_server_endpoint")}:11434'
) -> str:
	"""
	Send a prompt to a local Ollama server and return the response text.
	"""

	url = f"{host}/api/chat"

	messages = []
	if system:
		messages.append({"role": "system", "content": system})

	messages.append({"role": "user", "content": prompt})

	payload = {
		"model": model,
		"messages": messages,
		"stream": False
	}
	if model:
		response = requests.post(url, json=payload, timeout=300)
		response.raise_for_status()

		data = response.json()
		return data["message"]["content"]