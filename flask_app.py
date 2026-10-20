import hmac
import logging
import os
import re
from typing import Any

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

TICKET_COUNT = 150
TICKETS_PER_PAGE = 25
SUPABASE_TIMEOUT = 15
TELEGRAM_TIMEOUT = 10


class SupabaseError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
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
    occupied = occupied_tickets()
    page = offset // TICKETS_PER_PAGE + 1
    page_count = (TICKET_COUNT + TICKETS_PER_PAGE - 1) // TICKETS_PER_PAGE
    send_message(
        chat_id,
        f"Choose an available ticket (page {page} of {page_count}). "
        "Tickets marked “taken” are already pending or confirmed.",
        reply_markup=ticket_keyboard(offset, occupied),
    )


def reply_to_callback(callback_id: str, text: str) -> None:
    telegram_request("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def handle_start(chat_id: int) -> None:
    send_message(
        chat_id,
        "Welcome to Fedhaan Lottery Ticket. There are 150 tickets. "
        "Use /book to choose a ticket. Your booking stays PENDING until an admin approves it. "
        "No payment is collected by this bot.",
    )


def handle_callback(callback: dict[str, Any]) -> None:
    callback_id = callback.get("id", "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    user = callback.get("from") or {}
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

        if data.startswith("ticket:"):
            ticket_number = int(data.split(":", 1)[1])
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
                f"Booking ticket #{ticket_number}\n"
                "Reply directly to this message with your full name and phone number, "
                "separated by a | character.\n"
                "Example: Ada Example | +251911234567",
                reply_markup={"force_reply": True, "selective": True},
            )
            return
        reply_to_callback(callback_id, "That button has expired. Use /book to start again.")
    except (SupabaseError, TelegramError, RuntimeError, ValueError):
        app.logger.exception("Could not process a Telegram button action.")
        if callback_id:
            try:
                reply_to_callback(callback_id, "Sorry, something went wrong. Please try again.")
            except TelegramError:
                app.logger.error("Could not send a callback error response.")
        if chat_id is not None:
            try:
                send_message(chat_id, "Sorry, something went wrong. Please try again.")
            except TelegramError:
                app.logger.error("Could not send a chat error response.")


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
    if "|" not in reply_text:
        send_message(chat_id, "Please reply in this format: Full Name | Phone Number")
        return True

    full_name, phone = (clean_field(part) for part in reply_text.split("|", 1))
    phone_digits = re.sub(r"\D", "", phone)
    if (
        len(full_name) < 3
        or len(full_name) > 100
        or len(full_name.split()) < 2
        or not re.fullmatch(r"[+()\d.\-\s]{7,25}", phone)
        or not 7 <= len(phone_digits) <= 15
    ):
        send_message(
            chat_id,
            "Please enter a full name (at least two words) and a valid phone number, "
            "separated by |. Example: Ada Example | +251911234567",
        )
        return True

    try:
        response = supabase_request(
            "POST",
            "bookings",
            json_body={
                "ticket_number": ticket_number,
                "full_name": full_name,
                "phone": phone,
                "status": "PENDING",
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
        f"Your booking is PENDING admin approval.\n"
        f"Booking ID: {booking['id']}\n"
        f"Ticket: #{ticket_number}",
    )
    return True


def require_admin(user_id: int, chat_id: int) -> bool:
    if user_id not in admin_ids():
        send_message(chat_id, "This command is only available to an admin.")
        return False
    return True


def handle_admin_command(text: str, user_id: int, chat_id: int) -> bool:
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
    if command not in {"/pending", "/confirm", "/reject", "/stats"}:
        return False
    if not require_admin(user_id, chat_id):
        return True

    if command == "/pending":
        response = supabase_request(
            "GET",
            "bookings",
            params={
                "select": "id,ticket_number,full_name,phone,created_at",
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
            phone = clean_field(str(booking["phone"]))
            lines.append(
                f"ID {booking['id']} | Ticket #{booking['ticket_number']} | {name} | {phone}"
            )
        if len(pending) == 100:
            lines.append("Showing the oldest 100 bookings; run /pending again after processing some.")
        for start in range(0, len(lines), 25):
            send_message(chat_id, "\n".join(lines[start : start + 25]))
        return True

    if command in {"/confirm", "/reject"}:
        parts = text.split()
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
            send_message(
                chat_id,
                f"Booking {changed[0]['id']} is now {new_status}.",
            )
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
    if chat_id is None or user_id is None or not text:
        return

    try:
        if handle_admin_command(text, user_id, chat_id):
            return
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            handle_start(chat_id)
        elif command in {"/book", "/tickets"}:
            if chat.get("type") != "private":
                send_message(chat_id, "Please open a private chat with the bot to book a ticket.")
            else:
                show_tickets(chat_id)
        elif chat.get("type") == "private":
            if not handle_booking_reply(message, chat_id):
                send_message(chat_id, "Use /book to choose a ticket, or /help for instructions.")
    except (SupabaseError, TelegramError, RuntimeError):
        app.logger.exception("Could not process a Telegram message.")
        try:
            send_message(chat_id, "Sorry, something went wrong. Please try again shortly.")
        except TelegramError:
            app.logger.error("Could not send a chat error response.")


@app.get("/")
def health_check():
    return jsonify({"status": "ok", "service": "Fedhaan Lottery Ticket"})


@app.post("/webhook")
def telegram_webhook():
    expected_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected_secret:
        app.logger.error("TELEGRAM_WEBHOOK_SECRET is not configured.")
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
