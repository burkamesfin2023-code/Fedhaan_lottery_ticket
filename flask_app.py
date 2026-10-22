import hmac
import logging
import os
import re
from typing import Any

import requests
from flask import Flask, jsonify, request

app = Flask(name)
logging.basicConfig(level=logging.INFO)

TICKET_COUNT = 150
TICKETS_PER_PAGE = 25
SUPABASE_TIMEOUT = 15
TELEGRAM_TIMEOUT = 10


class SupabaseError(Exception):
    def init(self, message: str, status_code: int | None = None):
        super().init(message)
        self.status_code = status_code


class TelegramError(Exception):
    pass


def required_setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"The {name} environment variable is not set.")
    return value


def admin_ids() -> set[int]:
    raw_ids = required_setting("ADMIN_IDS")
    try:
        return {int(value.strip()) for value in raw_ids.split(",") if value.strip()}
    except ValueError as exc:
        raise RuntimeError("ADMIN_IDS must contain comma-separated Telegram user IDs.") from exc


def supabase_request(
    method: str,
    table: str,
    *,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    prefer: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> requests.Response:
    base_url = required_setting("SUPABASE_URL").rstrip("/")
    service_key = required_setting("SUPABASE_SERVICE_ROLE_KEY")
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    if extra_headers:
        headers.update(extra_headers)

    try:
        response = requests.request(
            method,
            f"{base_url}/rest/v1/{table}",
            headers=headers,
            params=params,
            json=json_body,
            timeout=SUPABASE_TIMEOUT,
        )
    except requests.RequestException:
        raise SupabaseError("Could not connect to Supabase.") from None

    if not response.ok:
        raise SupabaseError("Supabase returned an error.", response.status_code)
    return response


def telegram_request(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = required_setting("TELEGRAM_BOT_TOKEN")
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
            timeout=TELEGRAM_TIMEOUT,
        )
    except requests.RequestException:
        raise TelegramError("Could not connect to Telegram.") from None

    try:
        result = response.json()
    except ValueError as exc:
        raise TelegramError("Telegram returned an unreadable response.") from exc
    if not response.ok or not result.get("ok"):
        raise TelegramError("Telegram rejected the request.")
    return result


def send_message(
    chat_id: int,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    telegram_request("sendMessage", payload)


def send_photo(
    chat_id: int,
    photo: str,
    caption: str,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "photo": photo, "caption": caption}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    telegram_request("sendPhoto", payload)


def occupied_tickets() -> set[int]:
    response = supabase_request(
        "GET",
        "bookings",
        params={
            "select": "ticket_number",
            "status": "in.(PENDING,CONFIRMED)",
        },
    )
    return {int(row["ticket_number"]) for row in response.json()}


def check_and_reset_round(chat_id: int) -> bool:"""If all tickets are booked, reset/archive them to start a new round."""
    occupied = occupied_tickets()
    if len(occupied) >= TICKET_COUNT:
        # Reset round by deleting or marking old bookings as ARCHIVED/COMPLETED
        # Here we delete/update them so tickets become free again
        supabase_request(
            "DELETE",
            "bookings",
            params={"status": "in.(PENDING,CONFIRMED)"},
        )
        send_message(
            chat_id,
            "🎉 Tikkeetiwwan hundinuu guutamaniiru! Marsaa haaraan (New Round) eegalamee jira. Ammas /book fayyadamuun filachuu dandeessu!"
        )
        return True
    return False


def ticket_keyboard(offset: int, occupied: set[int]) -> dict[str, Any]:
    end = min(offset + TICKETS_PER_PAGE, TICKET_COUNT)
    buttons = []
    for number in range(offset + 1, end + 1):
        label = f"{number} (taken)" if number in occupied else str(number)
        buttons.append(
            {
                "text": label,
                "callback_data": f"ticket:{number}" if number not in occupied else "taken",
            }
        )
    rows = [buttons[index : index + 5] for index in range(0, len(buttons), 5)]
    navigation = []
    if offset > 0:
        navigation.append(
            {"text": "Previous", "callback_data": f"page:{max(0, offset - TICKETS_PER_PAGE)}"}
        )
    if end < TICKET_COUNT:
        navigation.append(
            {"text": "Next", "callback_data": f"page:{end}"}
        )
    if navigation:
        rows.append(navigation)
    return {"inline_keyboard": rows}


def show_tickets(chat_id: int, offset: int = 0) -> None:
    if check_and_reset_round(chat_id):
        offset = 0  # reset to first page on new round

    occupied = occupied_tickets()
    page = offset // TICKETS_PER_PAGE + 1
    page_count = (TICKET_COUNT + TICKETS_PER_PAGE - 1) // TICKETS_PER_PAGE
    
    # List free vs taken summary
    free_count = TICKET_COUNT - len(occupied)
    
    send_message(
        chat_id,
        "BAGA NAGAAN DHUFTAN!!\n\n"
        f"📊 Haala Tikkeetii:\n"
        f"- Kan qabaman (Booked/Taken): {len(occupied)}\n"
        f"- Kan hafan (Free available): {free_count}\n\n"
        f"lakkoofsa barbaaddan filadhaa (page {page} of {page_count}).\n"
        "Tickets marked “taken” are already pending or confirmed.",
        reply_markup=ticket_keyboard(offset, occupied),
    )


def reply_to_callback(callback_id: str, text: str) -> None:
    telegram_request("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def handle_start(chat_id: int) -> None:
    send_message(
        chat_id,
        "BAGA NAGAAN DHUFTAN!!\n\n"
        "Use /book to choose a ticket. \n"
        "kaffaltii qaamaan raawwachuuf 0935230062-irratti bilbilaa",
    )


def handle_callback(callback: dict[str, Any]) -> None:
    callback_id = callback.get("id", "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    user = callback.get("from") or {}
    user_id = user.get("id")
    data = str(callback.get("data", ""))

    if chat.get("type") != "private":
        reply_to_callback(callback_id, "Please book in a private chat with the bot.")
        return

    try:
        if data.startswith("page:"):
            offset = int(data.split(":", 1)[1])
            if offset < 0 or offset >= TICKET_COUNT or offset % TICKETS_PER_PAGE:
                reply_to_callback(callback_id, "That ticket page is not valid.")
                return
            reply_to_callback(callback_id, "Loading tickets…")
            show_tickets(chat_id, offset)
            return

        if data == "taken":
            reply_to_callback(callback_id, "That ticket is already taken.")
            return

        if data.startswith("ticket:"):ticket_number = int(data.split(":", 1)[1])
            if ticket_number < 1 or ticket_number > TICKET_COUNT:
                reply_to_callback(callback_id, "That ticket number is not valid.")
                return
            if ticket_number in occupied_tickets():
                reply_to_callback(callback_id, "That ticket was just taken. Please choose another.")
                show_tickets(chat_id)
                return
            reply_to_callback(callback_id, "Ticket selected.")
            
            send_message(
                chat_id,
                f"Booking ticket #{ticket_number}\n\n"
                "maqaa ykn lakk-bilbilaa keessan barreessaa\n\n"
                "Daataa Kaffaltii:\n"
                "- CBE Account: 1000123456789\n"
                "- Telebirr: 0911223344\n\n"
                "dursa kaffaltii keessan xumuraa\n\n"
                "kaffaltii qaamaan raawwachuuf 0935230062-irratti bilbilaa\n\n"
                "Reply directly to this message with your full name or phone number.",
                reply_markup={"force_reply": True, "selective": True},
            )
            return

        if data.startswith("approve:"):
            booking_id = data.split(":", 1)[1]
            if user_id not in admin_ids():
                reply_to_callback(callback_id, "Only admins can approve.")
                return
            
            response = supabase_request(
                "PATCH",
                "bookings",
                params={"id": f"eq.{booking_id}", "status": "eq.PENDING"},
                json_body={"status": "CONFIRMED"},
                prefer="return=representation",
            )
            changed = response.json()
            if not changed:
                reply_to_callback(callback_id, "Booking not found or already processed.")
                return

            booking = changed[0]
            customer_chat_id = booking.get("telegram_chat_id")
            
            if customer_chat_id:
                send_message(
                    customer_chat_id,
                    "CARRAA GAARII\nmilkaa'inaan citeera"
                )
            
            reply_to_callback(callback_id, "Approved successfully!")
            telegram_request("editMessageCaption", {
                "chat_id": chat_id,
                "message_id": message.get("message_id"),
                "caption": message.get("caption", "") + "\n\n✅ CONFIRMED",
                "reply_markup": {"inline_keyboard": []}
            })
            return

        reply_to_callback(callback_id, "That button has expired. Use /book to start again.")
    except (SupabaseError, TelegramError, RuntimeError, ValueError):
        app.logger.exception("Could not process a Telegram button action.")


def clean_field(value: str) -> str:
    return re.sub(r"[\r\n\t]+", " ", value).strip()


def handle_booking_reply(message: dict[str, Any], chat_id: int) -> bool:
    replied_message = message.get("reply_to_message") or {}
    prompt = replied_message.get("text", "")
    match = re.fullmatch(r"Booking ticket #(\d+)", prompt.splitlines()[0] if prompt else "")
    if not match or not (replied_message.get("from") or {}).get("is_bot"):
        return False

    ticket_number = int(match.group(1))
    reply_text = str(message.get("text", ""))
    
    if not reply_text:
        send_message(chat_id, "maqaa ykn lakk-bilbilaa keessan barreessaa")
        return True

    full_name = clean_field(reply_text)
    if len(full_name) < 2:
        send_message(chat_id, "maqaa ykn lakk-bilbilaa sirrii barreessaa.")
        return True

    try:
        response = supabase_request(
            "POST",
            "bookings",
            json_body={
                "ticket_number": ticket_number,
                "full_name": full_name,"phone": full_name,
                "status": "PENDING",
                "telegram_chat_id": chat_id,
            },
            prefer="return=representation",
        )
    except SupabaseError as exc:
        if exc.status_code == 409:
            send_message(
                chat_id,
                f"Sorry, ticket #{ticket_number} was just booked by someone else. Use /book to choose another.",
            )
            return True
        raise

    booking = response.json()[0]
    send_message(
        chat_id,
        "Ragaa kaffaltii keessan erguun mirkaneessaa!!!\n\n"
        f"Booking ID: {booking['id']} | Ticket: #{ticket_number}\n"
        "Maaloo suuraa ragaa kaffaltii keessan (screenshot) asitti ergaa.",
    )
    return True


def handle_receipt_photo(message: dict[str, Any], chat_id: int) -> bool:
    photos = message.get("photo")
    if not photos:
        return False

    response = supabase_request(
        "GET",
        "bookings",
        params={
            "select": "id,ticket_number,full_name",
            "telegram_chat_id": f"eq.{chat_id}",
            "status": "eq.PENDING",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    pending_list = response.json()
    if not pending_list:
        send_message(chat_id, "Use /book to select a ticket first before sending a receipt.")
        return True

    booking = pending_list[0]
    photo_file_id = photos[-1].get("file_id")
    user = message.get("from") or {}
    username = user.get("username", user.get("first_name", "Unknown"))

    caption = (
        f"📥 Ragaa Kaffaltii Haaraa (Receipt):\n"
        f"Booking ID: {booking['id']}\n"
        f"Ticket: #{booking['ticket_number']}\n"
        f"Maqaa/Lakk: {booking['full_name']}\n"
        f"User: @{username} (ID: {chat_id})"
    )

    keyboard = {
        "inline_keyboard": [
            [{"text": "Mirkaneessi (Approve)", "callback_data": f"approve:{booking['id']}"}]
        ]
    }

    for admin_id in admin_ids():
        try:
            send_photo(admin_id, photo_file_id, caption, reply_markup=keyboard)
        except TelegramError:
            app.logger.error(f"Could not send receipt to admin {admin_id}")

    send_message(
        chat_id,
        "Ragaa kaffaltii keessan fudhannee jirru; Admin-ni hanga mirkaneessutti obsaan eegaa."
    )
    return True


def require_admin(user_id: int, chat_id: int) -> bool:
    if user_id not in admin_ids():
        send_message(chat_id, "This command is only available to an admin.")
        return False
    return True


def handle_admin_command(text: str, user_id: int, chat_id: int) -> bool:
    parts = text.split()
    command = parts[0].split("@", 1)[0].lower()
    
    if command not in {"/pending", "/confirm", "/reject", "/stats", "/cashbook"}:
        return False
    if not require_admin(user_id, chat_id):
        return True

    if command == "/pending":
        response = supabase_request(
            "GET",
            "bookings",
            params={
                "select": "id,ticket_number,full_name,created_at",
                "status": "eq.PENDING",
                "order": "created_at.asc",
                "limit": "100",
            },
        )
        pending = response.json()
        if not pending:
            send_message(chat_id, "There are no pending bookings.")
            return True
        lines = ["Pending bookings:"]
        for booking in pending:
            name = clean_field(str(booking["full_name"]))
            lines.append(
                f"ID {booking['id']} | Ticket #{booking['ticket_number']} | {name}"
            )
        for start in range(0, len(lines), 25):
            send_message(chat_id, "\n".join(lines[start : start + 25]))
        return True

    if command == "/cashbook":if len(parts) < 3:
            send_message(chat_id, "Usage: /cashbook TICKET_NUMBER NAME_OR_PHONE")
            return True
        ticket_number = parts[1]
        cash_user_info = " ".join(parts[2:])
        
        try:
            response = supabase_request(
                "POST",
                "bookings",
                json_body={
                    "ticket_number": int(ticket_number),
                    "full_name": cash_user_info,
                    "phone": cash_user_info,
                    "status": "CONFIRMED",
                },
                prefer="return=representation",
            )
            send_message(chat_id, f"Successfully booked ticket #{ticket_number} for {cash_user_info} as CONFIRMED (Cash Payment).")
        except SupabaseError as exc:
            if exc.status_code == 409:
                send_message(chat_id, f"Ticket #{ticket_number} is already taken!")
            else:
                send_message(chat_id, "Error booking ticket via cash.")
        return True

    if command in {"/confirm", "/reject"}:
        if len(parts) != 2 or not parts[1].isdigit():
            send_message(chat_id, f"Usage: {command} BOOKING_ID")
            return True
        new_status = "CONFIRMED" if command == "/confirm" else "REJECTED"
        response = supabase_request(
            "PATCH",
            "bookings",
            params={"id": f"eq.{parts[1]}", "status": "eq.PENDING"},
            json_body={"status": new_status},
            prefer="return=representation",
        )
        changed = response.json()
        if not changed:
            send_message(chat_id, "Booking not found, or it is no longer PENDING.")
        else:
            booking = changed[0]
            if new_status == "CONFIRMED" and booking.get("telegram_chat_id"):
                send_message(booking["telegram_chat_id"], "CARRAA GAARII\nmilkaa'inaan citeera")
            send_message(chat_id, f"Booking {booking['id']} is now {new_status}.")
        return True

    if command == "/stats":
        counts = {}
        for status in ("PENDING", "CONFIRMED", "REJECTED"):
            response = supabase_request(
                "GET",
                "bookings",
                params={"select": "id", "status": f"eq.{status}"},
                prefer="count=exact",
                extra_headers={"Range": "0-0"},
            )
            content_range = response.headers.get("Content-Range", "")
            total = content_range.rsplit("/", 1)[-1]
            if not total.isdigit():
                raise SupabaseError("Supabase did not return a booking count.")
            counts[status] = int(total)
        available = TICKET_COUNT - counts["PENDING"] - counts["CONFIRMED"]
        send_message(
            chat_id,
            "Lottery statistics:\n"
            f"Pending: {counts['PENDING']}\n"
            f"Confirmed: {counts['CONFIRMED']}\n"
            f"Rejected: {counts['REJECTED']}\n"
            f"Available tickets: {available}",
        )
        return True

    return False


def handle_message(message: dict[str, Any]) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    user_id = (message.get("from") or {}).get("id")
    text = str(message.get("text", "")).strip()

    if chat_id is None or user_id is None:
        return

    try:
        if text and handle_admin_command(text, user_id, chat_id):
            return

        if message.get("photo") and chat.get("type") == "private":
            handle_receipt_photo(message, chat_id)
            return

        if not text:
            return

        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            handle_start(chat_id)
        elif command in {"/book", "/tickets"}:if chat.get("type") != "private":
                send_message(chat_id, "Please open a private chat with the bot to book a ticket.")
            else:
                show_tickets(chat_id)
        elif chat.get("type") == "private":
            if not handle_booking_reply(message, chat_id):
                send_message(chat_id, "Use /book to choose a ticket, or /help for instructions.")
    except (SupabaseError, TelegramError, RuntimeError):
        app.logger.exception("Could not process a Telegram message.")


@app.get("/")
def health_check():
    return jsonify({"status": "ok", "service": "Fedhaan Lottery Ticket"})


@app.post("/telegram")
def telegram_webhook():
    expected_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected_secret:
        return jsonify({"error": "Webhook is not configured."}), 503
    if not hmac.compare_digest(received_secret, expected_secret):
        return jsonify({"error": "Unauthorized."}), 401

    update = request.get_json(silent=True)
    if not isinstance(update, dict):
        return jsonify({"error": "Invalid Telegram update."}), 400

    try:
        if isinstance(update.get("callback_query"), dict):
            handle_callback(update["callback_query"])
        elif isinstance(update.get("message"), dict):
            handle_message(update["message"])
    except (SupabaseError, TelegramError, RuntimeError, ValueError):
        app.logger.exception("Could not process a Telegram update.")
        return jsonify({"error": "Update processing failed."}), 500

    return jsonify({"ok": True})



    app.run(host="0.0.0.0", port=5000)