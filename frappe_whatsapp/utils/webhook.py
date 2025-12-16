"""Webhook."""
import frappe
import json
import requests
import time
from werkzeug.wrappers import Response
import frappe.utils

from frappe_whatsapp.utils import get_whatsapp_account
import frappe
from frappe.utils.password import get_decrypted_password
import json
import requests
from healthcare.healthcare.api.patient_portal import (
            get_departments, get_practitioners, get_slots,
            make_appointment, get_fees, get_appointments
        )
from datetime import datetime
from healthcare.healthcare.payment import (send_stk_push , get_access_token)



@frappe.whitelist(allow_guest=True)
def webhook():
	"""Meta webhook."""
	if frappe.request.method == "GET":
		return get()
	return post()


def get():
	"""Get."""
	hub_challenge = frappe.form_dict.get("hub.challenge")
	verify_token = frappe.form_dict.get("hub.verify_token")
	webhook_verify_token = frappe.db.get_value(
		'WhatsApp Account',
		{"webhook_verify_token": verify_token},
		'webhook_verify_token'
	)
	if not webhook_verify_token:
		frappe.throw("No matching WhatsApp account")

	if frappe.form_dict.get("hub.verify_token") != webhook_verify_token:
		frappe.throw("Verify token does not match")

	return Response(hub_challenge, status=200)

def post():
	"""Post."""
	data = frappe.local.form_dict
	frappe.get_doc({
		"doctype": "WhatsApp Notification Log",
		"template": "Webhook",
		"meta_data": json.dumps(data)
	}).insert(ignore_permissions=True)

	messages = []
	phone_id = None
	try:
		messages = data["entry"][0]["changes"][0]["value"].get("messages", [])
		phone_id = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("metadata", {}).get("phone_number_id")
	except KeyError:
		messages = data["entry"]["changes"][0]["value"].get("messages", [])
	sender_profile_name = next(
		(
			contact.get("profile", {}).get("name")
			for entry in data.get("entry", [])
			for change in entry.get("changes", [])
			for contact in change.get("value", {}).get("contacts", [])
		),
		None,
	)

	whatsapp_account = get_whatsapp_account(phone_id) if phone_id else None
	if not whatsapp_account:
		return

	if messages:
		for message in messages:
			message_type = message['type']
			is_reply = True if message.get('context') and 'forwarded' not in message.get('context') else False
			reply_to_message_id = message['context']['id'] if is_reply else None
			if message_type == 'text':
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['text']['body'],
					"message_id": message['id'],
					"reply_to_message_id": reply_to_message_id,
					"is_reply": is_reply,
					"content_type":message_type,
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)

				text = message['text']['body']
				wa_number = message['from']

				# Idempotency
				cache = frappe.cache
				if cache.get(IDEMPOTENCY_PREFIX + message['id']):
					continue
					# pass
				cache.set(IDEMPOTENCY_PREFIX + message['id'], "1", TTL)

				# Process message safely
				
				reply = process_message_safe(wa_number, text.lower().strip())
				if reply:
					send_reply(wa_number, reply)

			elif message_type == 'reaction':
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['reaction']['emoji'],
					"reply_to_message_id": message['reaction']['message_id'],
					"message_id": message['id'],
					"content_type": "reaction",
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)
			elif message_type == 'interactive':
				interactive_data = message['interactive']
				interactive_type = interactive_data.get('type')

				# Handle button reply
				if interactive_type == 'button_reply':
					frappe.get_doc({
						"doctype": "WhatsApp Message",
						"type": "Incoming",
						"from": message['from'],
						"message": interactive_data['button_reply']['id'],
						"message_id": message['id'],
						"reply_to_message_id": reply_to_message_id,
						"is_reply": is_reply,
						"content_type": "button",
						"profile_name": sender_profile_name,
						"whatsapp_account": whatsapp_account.name
					}).insert(ignore_permissions=True)
				# Handle list reply
				elif interactive_type == 'list_reply':
					frappe.get_doc({
						"doctype": "WhatsApp Message",
						"type": "Incoming",
						"from": message['from'],
						"message": interactive_data['list_reply']['id'],
						"message_id": message['id'],
						"reply_to_message_id": reply_to_message_id,
						"is_reply": is_reply,
						"content_type": "button",
						"profile_name": sender_profile_name,
						"whatsapp_account": whatsapp_account.name
					}).insert(ignore_permissions=True)
				# Handle WhatsApp Flows (nfm_reply)
				elif interactive_type == 'nfm_reply':
					nfm_reply = interactive_data['nfm_reply']
					response_json_str = nfm_reply.get('response_json', '{}')

					# Parse the response JSON
					try:
						flow_response = json.loads(response_json_str)
					except json.JSONDecodeError:
						flow_response = {}

					# Create a summary message from the flow response
					summary_parts = []
					for key, value in flow_response.items():
						if value:
							summary_parts.append(f"{key}: {value}")
					summary_message = ", ".join(summary_parts) if summary_parts else "Flow completed"

					msg_doc = frappe.get_doc({
						"doctype": "WhatsApp Message",
						"type": "Incoming",
						"from": message['from'],
						"message": summary_message,
						"message_id": message['id'],
						"reply_to_message_id": reply_to_message_id,
						"is_reply": is_reply,
						"content_type": "flow",
						"flow_response": json.dumps(flow_response),
						"profile_name": sender_profile_name,
						"whatsapp_account": whatsapp_account.name
					}).insert(ignore_permissions=True)

					# Publish realtime event for flow response
					frappe.publish_realtime(
						"whatsapp_flow_response",
						{
							"phone": message['from'],
							"message_id": message['id'],
							"flow_response": flow_response,
							"whatsapp_account": whatsapp_account.name
						}
					)
			elif message_type in ["image", "audio", "video", "document"]:
				token = whatsapp_account.get_password("token")
				url = f"{whatsapp_account.url}/{whatsapp_account.version}/"

				media_id = message[message_type]["id"]
				headers = {
					'Authorization': 'Bearer ' + token

				}
				response = requests.get(f'{url}{media_id}/', headers=headers)

				if response.status_code == 200:
					media_data = response.json()
					media_url = media_data.get("url")
					mime_type = media_data.get("mime_type")
					file_extension = mime_type.split('/')[1]

					media_response = requests.get(media_url, headers=headers)
					if media_response.status_code == 200:

						file_data = media_response.content
						file_name = f"{frappe.generate_hash(length=10)}.{file_extension}"

						message_doc = frappe.get_doc({
							"doctype": "WhatsApp Message",
							"type": "Incoming",
							"from": message['from'],
							"message_id": message['id'],
							"reply_to_message_id": reply_to_message_id,
							"is_reply": is_reply,
							"message": message[message_type].get("caption", ""),
							"content_type" : message_type,
							"profile_name":sender_profile_name,
							"whatsapp_account":whatsapp_account.name
						}).insert(ignore_permissions=True)

						file = frappe.get_doc(
							{
								"doctype": "File",
								"file_name": file_name,
								"attached_to_doctype": "WhatsApp Message",
								"attached_to_name": message_doc.name,
								"content": file_data,
								"attached_to_field": "attach"
							}
						).save(ignore_permissions=True)


						message_doc.attach = file.file_url
						message_doc.save()
                                   
							
			elif message_type == "button":
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['button']['text'],
					"message_id": message['id'],
					"reply_to_message_id": reply_to_message_id,
					"is_reply": is_reply,
					"content_type": message_type,
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)
			else:
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message_id": message['id'],
					"message": message[message_type].get(message_type),
					"content_type" : message_type,
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)

	else:
		changes = None
		try:
			changes = data["entry"][0]["changes"][0]
		except KeyError:
			changes = data["entry"]["changes"][0]
		update_status(changes)
	return

def update_status(data):
	"""Update status hook."""
	if data.get("field") == "message_template_status_update":
		update_template_status(data['value'])

	elif data.get("field") == "messages":
		update_message_status(data['value'])

def update_template_status(data):
	"""Update template status."""
	frappe.db.sql(
		"""UPDATE `tabWhatsApp Templates`
		SET status = %(event)s
		WHERE id = %(message_template_id)s""",
		data
	)

def update_message_status(data):
	"""Update message status."""
	id = data['statuses'][0]['id']
	status = data['statuses'][0]['status']
	conversation = data['statuses'][0].get('conversation', {}).get('id')
	name = frappe.db.get_value("WhatsApp Message", filters={"message_id": id})

	doc = frappe.get_doc("WhatsApp Message", name)
	doc.status = status
	if conversation:
		doc.conversation_id = conversation
	doc.save(ignore_permissions=True)




def get_api_keys():
    return get_decrypted_password(doctype="Whatsapp API", name="Whatsapp API", fieldname ="api_key")





# Redis keys
SESSION_PREFIX = "wa_session:"
IDEMPOTENCY_PREFIX = "wa_msg:"
TTL = 86400  # 24 hours
session = {}

def get_session_key(whatsapp_number):
    return f"wa_session_{whatsapp_number}"

def get_user_session(wa_number):
    key = get_session_key(wa_number)
    return session.get(key, {})

def save_user_session(wa_number, data):
    key = get_session_key(wa_number)
    frappe.cache.set_value(key, data)



def process_message_safe(wa_number: str, text: str):
    cache = frappe.cache
    session_key = SESSION_PREFIX + wa_number
    session = cache.get(session_key)
    if session:
        try:
            session = json.loads(session)
        except:
            session = {}
    else:
        session = {}
    

    # Reset
    if text in ["menu", "hi", "hello", "start"]:
        session = {}
        cache.set(session_key, json.dumps(session), TTL)
        return welcome_message()

    step = session.get("step", "start")
    

    # ——————— Step: Start ———————
    if session.get("step", "start") == "start":
        if text == "1":
            session["step"] = "book_appointment"
            cache.set(session_key, json.dumps(session), TTL)
        if text == "2":
            session["step"] = "viewing_appointments"
            cache.set(session_key, json.dumps(session), TTL)
        if text == "3":
            session["step"] = "new_patient"
            cache.set(session_key, json.dumps(session), TTL)

    # Existing appointments
    if session.get("step", "start") == "viewing_appointments":
        text = text.strip().upper()
        appointments = handle_view_appointments_flow(wa_number)
        if not appointments:
            return "📭 No appointments yet.\n📲 Reply *MENU* to go back."
        session_appointments = {
            "appointments": [
                {
                    "idx": i + 1,
                    "ref": a.name,
                    "time": a.appointment_time,
                    "doctor": a.practitioner_name or "Dr. Unknown",
                    "dept": a.department or "",
                    "status": a.status,
                    "date": a.appointment_date,
                    "paid": a.paid_amount > 0,
                    "invoiced": a.invoiced
                }
                for i, a in enumerate(appointments)
            ]
            }
        lines = []
        for apt in session_appointments["appointments"]:
            status_icon = "Paid" if apt["invoiced"] else "Pending"
            lines.append(
                f"{apt['idx']}. {apt['date']} • {apt['time']}\n"
                f"   Dr. {apt['doctor']} • {apt['dept']}\n"
                f"   Status: {status_icon} • Ref: {apt['ref']}"
            )

        session["step"] = "appointment_details"
        cache.set(session_key, json.dumps(session), TTL)
        return (
            "📅 *Your Upcoming Appointments*\n\n"
            + "\n\n".join(lines) +
            "\n\n✉️ Reply with:\n"
            "• 🔢 Number → View details & options\n"
            "• 📲 MENU → Main menu"
            )

    if session.get("step", "start") == "appointment_details":
        appointments = handle_view_appointments_flow(wa_number)
        session_appointments = {
            "appointments": [
                {
                    "idx": i + 1,
                    "ref": a.name,
                    "time": a.appointment_time,
                    "doctor": a.practitioner_name or "Dr. Unknown",
                    "dept": a.department or "",
                    "status": a.status,
                    "date": a.appointment_date,
                    "paid": a.paid_amount > 0,
                    "invoiced": a.invoiced
                }
                for i, a in enumerate(appointments)
            ]
            }
        if text.isdigit():
            idx = int(text)
            appts = session_appointments.get("appointments", [])
            apt = next((a for a in appts if a["idx"] == idx), None)
            if apt:
                return appointment_details(apt, wa_number)
            return "Invalid number."
        
    if session.get("step", "start") == "appt_detail" and text.lower() == "pay":
        try:
            patient = frappe.get_doc("Patient",session.get("patient") or find_patient_by_mobile(wa_number))
            if not patient:
                return "Patient not found. Please register first."
            
            # Send Payment Request
            call_back = frappe.utils.get_url()+f"/app/patient-appointment/{session['current_apt_ref']}"
            new_transact = frappe.new_doc("Transact Tracker")
            new_transact.appointment = session["current_apt_ref"]
            new_transact.save(ignore_permissions=True)
            # number,id,amount, email, first_name, token,callback
            session_amount = frappe.db.get_value("Patient Appointment",session["current_apt_ref"],"paid_amount")  if frappe.db.get_value("PesaPal","PesaPal","live") else 1
            push = send_stk_push(wa_number,session_key,session_amount,patient.email,patient.first_name or patient.last_name, json.loads(get_access_token().text)["token"],call_back)
            response_text = json.loads(push.text)
            if response_text["error"]["code"] and response_text["error"]["message"]:
                return "💳 Payment request failed. ⏳ Please reply with `Pay` again in 5 minutes to complete transaction.🙏 Thank you!"

            save_to_transact(push.text, new_transact.name)
            session["step"] = "start"
            cache.set(session_key, json.dumps(session), TTL)  # reset

            return "💳 Payment request initiated. ⏳ We will notify you once complete. 🙏 Thank you!"
        except Exception as e:
            return f"Booking failed: {str(e)}"
 

    

    # ——————— New Patient Registration Flow ———————
    if session.get("step", "start") == "new_patient":
        if find_patient_by_mobile(wa_number):
            return "❌📞 User with this phone number already exists!!"
        session["step"] = "awaiting_first_name"
        cache.set(session_key, json.dumps(session), TTL)
        return "Great! Reply with your First Name (e.g. John)"
    
    if session.get("step", "start") == "awaiting_first_name":
        session["first_name"] = text.title()
        session["step"] = "awaiting_second_name"
        cache.set(session_key, json.dumps(session), TTL)
        return "Nice! Reply with your Last Name (e.g. Doe)"
    
    if session.get("step", "start") == "awaiting_second_name":
        session["second_name"] = text.title()
        session["step"] = "awaiting_email"
        cache.set(session_key, json.dumps(session), TTL)
        return "Great! Reply with your email (e.g. janedoe@example.com) - w" \
        "e will use this email to send you documents related toyour journey with us."
    
    if session.get("step", "start") == "awaiting_email":
        if "@" not in text:
            return "Reply with a valid email address"
        session["email"] = text
        session["step"] = "awaiting_age"
        cache.set(session_key, json.dumps(session), TTL)
        return "Great! Now reply with your date of birth (e.g. dd-mm-yy)"

    if session.get("step", "start") == "awaiting_age":
        if not check_if_is_date(text):
            return "That can't be a date. Try again. dd-mm-yy. e.g 15-12-2025"
        session["dob"] = text
        session["step"] = "awaiting_id"
        cache.set(session_key, json.dumps(session), TTL)
        return "Reply with your national ID number"
    
    if session.get("step", "start") == "awaiting_id":
        try:
            if (len(text) > 7) and (len(text) < 9):
                session["national_id"] = int(text)
            else:
                return "Invalid ID number. Reply with a valid ID Number"
        except Exception as e:
            return "Invalid ID number. Reply with a valid ID Number"
        session["step"] = "awaiting_gender"
        cache.set(session_key, json.dumps(session), TTL)
        return "⚧️ Gender?\nReply: MALE / FEMALE / OTHER"

    if session.get("step", "start") == "awaiting_gender":
        gender = text.upper()
        if gender not in ["MALE", "FEMALE", "OTHER"]:
            return "Please reply with MALE, FEMALE or OTHER"
        session["gender"] = gender
        session["step"] = "book_appointment"
        try:
            from datetime import date
            dates = session["dob"].split("-")
            dob = date(int(dates[-1]), int(dates[1]),int(dates[0]))
            try:
                patient = frappe.get_doc({
                    "doctype": "Patient",
                    "first_name": session["first_name"].strip(),
                    "last_name": session["second_name"].strip(),
                    "mobile": wa_number,
                    "email": session["email"],
                    "sex": session["gender"],
                    "dob": dob,
                    "uid": session["national_id"],
                    "status": "Active"
                }).insert(ignore_permissions=True)
            except Exception as e:
                 frappe.log_error(session.get("step", "start"),e)
                 return "Could not complete registration. Try again later"

            frappe.db.commit()
            session["patient"] = patient.name
            session["patient_name"] = patient.first_name
            cache.set(session_key, json.dumps(session), TTL)
            return f"""
            ✅ *Patient Registered Successfully!*

            🆔 Patient ID: {patient.name}
            👤 Patient Name: {patient.first_name}

            📅 Purpose to book an appointment with us soon. To book,  
            {department_list()}
            """

        except Exception as e:
            return f"Registration failed. Try again later.\nError: {str(e)}"


    
    # ——————— Appointment Booking Flow ———————
    if session.get("step", "start") == "book_appointment":
        if text == "1":
            session["step"] = "select_department"
            cache.set(session_key, json.dumps(session), TTL)
            return department_list()

    if session.get("step", "start") == "select_department":
        depts = get_departments()
        try:
            idx = int(text) - 1
            dept = depts[idx]
            session["department"] = dept["name"]
            return doctor_list(dept["name"])
        except Exception as e:
            frappe.log_error(session.get("step", "start"),e)
            return f"Invalid selection. Reply with number only."

    if session.get("step", "start") == "select_doctor":
        doctors = get_practitioners(session["department"])
        try:
            idx = int(text) - 1
            doc = doctors[idx]
            session["practitioner"] = doc["name"]
            session["practitioner_name"] = doc["practitioner_name"]
            session["step"] = "select_date"
            cache.set(session_key, json.dumps(session), TTL)
            return "Choose date:\n1. Reply with the date in the formart dd-mm-yy\n e.g 15-12-2025"
        except:
            return "Invalid doctor. Try again."

    if session.get("step", "start") == "select_date":
        # if is date
        if not check_if_is_date(text):
            return "That can't be a date, can it?.\nReply in the formart\ndd-mm-yy  e.g 15-12-2025"
        
        if text.split("-")[-1] not in ["2025","2026"]:
            return "Select a date with one (1) year of today"


        try:
            session["date"] = text
            session["step"] = "select_slot"
            slots = get_slots(session["practitioner"], session["date"])
            if not slots:
                return "No slots available on this date. Reply MENU to restart Or Choose a different day"
            slot_text = "Available slots:\n" + "\n".join([f"{i+1}. {s}" for i, s in enumerate(slots)])
            cache.set(session_key, json.dumps(session), TTL)
            return slot_text + "\n\nReply with number"
        except:
            return "❌ Invalid date."

    if session.get("step", "start") == "select_slot":
        slots = get_slots(session["practitioner"], session["date"])
        try:
            idx = int(text) - 1
            session["slot"] = slots[idx]
            session["step"] = "confirm"

            fees = get_fees(session["practitioner"], session["date"])
            session["fees"] = fees["details"].get("practitioner_charge", 1) if frappe.db.get_value("PesaPal", "PesaPal","live") else 1
            charge = session["fees"]
            cache.set(session_key, json.dumps(session), TTL)
            return f"""
                📋 **Appointment Summary**

                👨‍⚕️ Doctor: Dr. {session['practitioner_name']}
                📅 Date: {session['date']}
                ⏰ Time: {session['slot']}
                💰 Fee: KSH {charge}

                📝 Reply *CONFIRM* to book and make payment.
                """
        except Exception as e:
            return "❌ Invalid slot."

    if session.get("step", "start") == "confirm" and text.lower() == "confirm":
        try:
            patient = session.get("patient") or find_patient_by_mobile(wa_number)
            if not patient:
                return "Patient not found. Please register first."
            
            doc = make_appointment(
                practitioner=session["practitioner"],
                patient=patient,
                date=session["date"],
                slot=session["slot"]
            )
            frappe.db.commit()
            session["step"] = "pay"
            session["ref"] = doc.name
            cache.set(session_key, json.dumps(session), TTL)  # reset

            return f"""
            🎉 *Appointment Scheduled Successfully!*

            🧾 Ref: {doc.name}
            👨‍⚕️ Dr. {session['practitioner_name']}
            📅 Date: {session['date']}
            ⏰ Time: {session['slot']}

            🙏 Thank you!
            📲 Reply *MENU* anytime for the main menu. To confirm your slot, reply *PAY*
                """
        except Exception as e:
            return f"Booking failed: {str(e)}"
    
    if session.get("step", "start") == "pay" and text.lower() == "pay":
        try:
            patient = frappe.get_doc("Patient",session.get("patient") or find_patient_by_mobile(wa_number))
            if not patient:
                return "Patient not found. Please register first."
            
            # Send Payment Request
            call_back = frappe.utils.get_url()+f"/app/patient-appointment/{session['ref']}"
            new_transact = frappe.new_doc("Transact Tracker")
            new_transact.appointment = session["ref"]
            new_transact.save(ignore_permissions=True)
            # number,id,amount, email, first_name, token,callback    
            push = send_stk_push(wa_number,session_key,session["fees"],patient.email,patient.first_name or patient.last_name, json.loads(get_access_token().text)["token"],call_back)
            response_text = json.loads(push.text)
            if response_text["error"]["code"] and response_text["error"]["message"]:
                return "💳 Payment request failed. ⏳ Please reply with `Pay` again in 5 minutes to complete transaction.🙏 Thank you!"

            save_to_transact(push.text, new_transact.name)
            session["step"] = "start"
            cache.set(session_key, json.dumps(session), TTL)  # reset

            return "💳 Payment request initiated. ⏳ We will notify you once complete. 🙏 Thank you!"
        except Exception as e:
            return f"Booking failed: {str(e)}"

    return "Invalid response - Reply `MENU` to start new Session."

def welcome_message():
    # get company name
    company_name = frappe.get_value("Whatsapp API","Whatsapp API","company_name")
    return f"""👋 Hi! Welcome to {company_name}

        Please select an option:
        1️⃣ Book Appointment
        2️⃣ View My Appointments
        3️⃣ New Patient 

        Reply with 1, 2 or 3"""

def send_reply(number,text,type="text"):
    # response = requests.post('https://api.flaresend.com/send-message',
    #     headers={'Authorization': f'Bearer {get_api_keys()}'},
    #     json={'recipients': [number], 'text': text, "type": type})
    
    new_whatsapp_message = frappe.new_doc("WhatsApp Message")
    new_whatsapp_message.label = "EMT BOT"
    new_whatsapp_message.to = number
    new_whatsapp_message.message = text
    new_whatsapp_message.save(ignore_permissions= True)
    return new_whatsapp_message.name


def department_list():
    depts = get_departments()[:10]
    lines = [f"{i+1}. {d.get('department', 'Unknown')}" for i, d in enumerate(depts)]
    return "Choose Department:\n\n" + "\n".join(lines)

def doctor_list(dept):
    docs = get_practitioners(dept)
    if not docs:
        return "No doctors available in this department. Please select different department."
    lines = [f"{i+1}. Dr. {d['practitioner_name']}" for i, d in enumerate(docs)]
    session["step"] = "select_doctor"
    frappe.cache.set(session_key, json.dumps(session), TTL)
    return "Select Doctor:\n\n" + "\n".join(lines)

def view_appointments(wa_number):
    patient = find_patient_by_mobile(wa_number)
    if not patient:
        return "No patient record found. Reply 4 to register."
    appts = get_appointments() or []
    if not appts:
        return "You have no upcoming appointments."
    lines = [f"• {a['appointment_date']} {a.get('appointment_time','')} - Dr. {a.get('practitioner_name','')}" for a in appts[:5]]
    return "Your Upcoming Appointments:\n\n" + "\n".join(lines)

def find_patient_by_mobile(mobile):
    patient = frappe.db.get_value("Patient", {"mobile": f"+{mobile}",}, ["name", "patient_name"], as_dict=1)
    return patient["name"] if patient else None


def save_to_transact(resp,name):
	try:
		transact = frappe.get_doc("Transact Tracker", name)
		transact.response = resp
		transact.order_id = json.loads(resp)["order_tracking_id"]
		transact.save(ignore_permissions=True)
	except Exception as e:
		frappe.log_error("TRANSACT TRACKER",(f"Error adding to transaction - {e}"))




def handle_view_appointments_flow(wa_number: str,):
    patient = find_patient_by_mobile(wa_number)
    if not patient:
        return "You are not registered yet.\nReply 4 to register."

    # Fetch appointments (upcoming first)
    appointments = frappe.db.get_all(
        "Patient Appointment",
        filters={
            "patient": patient,
            "status": ["not in", ["Cancelled"]],
        },
        fields=["name", "practitioner_name", "department", "appointment_date", 
                "appointment_time", "status", "paid_amount", "billing_item","invoiced"],
        order_by="appointment_date desc, appointment_time desc",
        limit=10
    )

    if not appointments:
        return "You have no upcoming appointments.\n\nReply 1 to book one!"
    return appointments



def appointment_details(apt: dict, wa_number: str):
    payment_status = "Paid" if apt["invoiced"] else "Pending Payment"
    
    msg = (
        "📋 *Appointment Details*\n\n"
        f"🆔 Reference: *{apt['ref']}*\n"
        f"📅 Date: {apt['date']}\n"
        f"⏰ Time: {apt['time']}\n"
        f"👨‍⚕️ Doctor: Dr. {apt['doctor']}\n"
        f"🏥 Department: {apt['dept']}\n"
        f"💳 Status: {payment_status}\n\n"
        "⚙️ Options:\n"
        "• 📲 MENU → Main menu\n"
        f"{'•  Reply with *Pay* to finish payment ..' if payment_status == 'Pending Payment' else ''}"
    )

    # Update session to this appointment
    session = {"step": "appt_detail" if payment_status == 'Pending Payment' else 'start', "current_apt_ref": apt["ref"]}
    frappe.cache.set(f"{SESSION_PREFIX}{wa_number}", json.dumps(session), TTL)
    
    return msg


def check_if_is_date(text):
    sections = text.split("-")
    if len(sections) != 3:
        return False
    for sect in sections:
        try:
            as_int = int(sect)
        except:
            return False
    return True


