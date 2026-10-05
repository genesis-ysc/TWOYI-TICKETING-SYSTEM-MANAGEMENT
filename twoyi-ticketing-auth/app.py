"""
Twoyi Ticketing System (local database edition, with client/admin logins)
---------------------------------------------------------------------------
Stores tickets in a local SQLite file (tickets.db) — no Google account,
API, or credentials required.

Two roles:
  - client: logs tickets, sees only the tickets they personally submitted.
  - admin:  sees every ticket, can filter/search, change status, export CSV.

Default accounts (created automatically on first run — CHANGE THESE):
  admin  / admin123   (role: admin)
  client / client123  (role: client)

Run:
    pip install -r requirements.txt
    python app.py

Then open http://localhost:5000
"""

import csv
import io
import os
import re
import smtplib
import threading
from email.message import EmailMessage
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, flash, Response, session
)

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

import db as sheets

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "twoyi-dev-key-change-me")

APP_TITLE = "Twoyi Ticketing System"


@app.context_processor
def inject_globals():
    return {
        "app_title": APP_TITLE,
        "smtp_on": smtp_configured(),
        "current_user": {
            "username": session.get("username"),
            "display_name": session.get("display_name"),
            "role": session.get("role"),
            "email": session.get("email"),
        } if session.get("username") else None,
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            flash("That page is for admin accounts only.", "error")
            return redirect(url_for("new_ticket_form"))
        return view(*args, **kwargs)
    return wrapped


def _start_session(user):
    session["username"] = user["username"]
    session["display_name"] = user["display_name"]
    session["role"] = user["role"]
    session["email"] = user.get("email", "")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("username"):
            return redirect(url_for("new_ticket_form"))
        return render_template("login.html")

    identifier = request.form.get("login", "").strip()
    password = request.form.get("password", "")
    user = sheets.verify_login(identifier, password)

    if not user:
        flash("Incorrect email/username or password.", "error")
        return render_template("login.html", login=identifier)

    if not user["verified"]:
        flash("Please confirm your email address first. Check your inbox for the link.", "error")
        return render_template("login.html", login=identifier, unverified_email=user["email"])

    _start_session(user)
    next_url = request.args.get("next") or url_for("new_ticket_form")
    if not next_url.startswith("/"):
        next_url = url_for("new_ticket_form")
    return redirect(next_url)


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Optional: set REGISTRATION_CODE in the environment to require an invite
# code on the sign-up form (recommended if the app is reachable by others).
REGISTRATION_CODE = os.environ.get("REGISTRATION_CODE", "")

_serializer = URLSafeTimedSerializer(app.secret_key)
VERIFY_MAX_AGE = 60 * 60 * 48  # confirmation links last 48 hours


# ---- Email sending (optional SMTP) -----------------------------------------
# If SMTP_HOST is not set, confirmation is skipped and new accounts are active
# immediately. Set the SMTP_* variables (see README) to require that people
# confirm they own the email address before they can log in.

def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST"))


def send_email(to_addr: str, subject: str, body: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", user or "no-reply@localhost")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        server = smtplib.SMTP(host, port, timeout=15)
        # STARTTLS is on by default; set SMTP_STARTTLS=0 only for a local
        # test mail server (e.g. MailHog on port 1025) that has no TLS.
        if os.environ.get("SMTP_STARTTLS", "1") != "0":
            server.starttls()
    try:
        if user:
            server.login(user, password)
        server.send_message(msg)
    finally:
        server.quit()


def send_verification_email(email: str, display_name: str) -> None:
    token = _serializer.dumps(email.lower(), salt="verify-email")
    link = url_for("verify_email", token=token, _external=True)
    send_email(
        email,
        f"Confirm your email - {APP_TITLE}",
        f"Hi {display_name},\n\n"
        f"Confirm your email address to activate your {APP_TITLE} account:\n\n"
        f"{link}\n\n"
        "This link expires in 48 hours. If you didn't sign up, ignore this email.\n",
    )


# ---- Ticket notifications --------------------------------------------------
# Sent only when SMTP is configured. Extra admin recipients (e.g. a shared
# inbox) can be listed in ADMIN_NOTIFY_EMAIL, comma-separated. Admin accounts
# that have a confirmed email address are notified automatically.

EMAIL_ASYNC = True  # send in a background thread so pages don't wait on SMTP


def admin_recipients() -> list[str]:
    extra = [e.strip().lower() for e in os.environ.get("ADMIN_NOTIFY_EMAIL", "").split(",") if e.strip()]
    return sorted(set(sheets.get_admin_emails()) | set(extra))


def owner_email(ticket: dict) -> str:
    user = sheets.get_user_by_username(ticket.get("Created By", "")) if ticket.get("Created By") else None
    return user["email"] if user and user["verified"] else ""


def _deliver(recipients, subject, body):
    for to in recipients:
        try:
            send_email(to, subject, body)
        except Exception:
            app.logger.exception("Could not send notification to %s", to)


def notify(recipients, subject, body) -> None:
    """Email every address in recipients (deduplicated). No-op without SMTP."""
    recipients = sorted({r.lower() for r in recipients if r})
    if not recipients or not smtp_configured():
        return
    if EMAIL_ASYNC:
        threading.Thread(target=_deliver, args=(recipients, subject, body), daemon=True).start()
    else:
        _deliver(recipients, subject, body)


def _ticket_details(t: dict) -> str:
    lines = [
        f"Ticket:   {t['Ticket Number']}",
        f"Branch:   {t['Branch']}",
        f"Priority: {t['Priority']}",
        f"Status:   {t['Status']}",
        f"Created:  {t['Date Created']}",
    ]
    if t.get("Date Finished"):
        lines.append(f"Closed:   {t['Date Finished']}")
    lines += ["", "Concern / Request:", t["Concern/Request"]]
    return "\n".join(lines)


def notify_ticket_created(ticket: dict) -> None:
    mine = url_for("my_tickets", _external=True)
    owner = owner_email(ticket)
    notify([owner],
           f"[{APP_TITLE}] We received ticket {ticket['Ticket Number']}",
           f"Your ticket has been logged.\n\n{_ticket_details(ticket)}\n\n"
           f"Track it here: {mine}\n")
    report = url_for("tickets_report", _external=True)
    notify([a for a in admin_recipients() if a != owner],
           f"[{APP_TITLE}] New {ticket['Priority']} ticket {ticket['Ticket Number']} - {ticket['Branch']}",
           f"A new ticket was submitted by {ticket['Created By'] or 'unknown'}.\n\n"
           f"{_ticket_details(ticket)}\n\nOpen the report: {report}\n")


def notify_status_change(ticket: dict, actor_username: str, actor_is_admin: bool) -> None:
    """Tell the ticket owner when an admin changes it, or the admins when the owner closes it."""
    actor_email = (sheets.get_user_by_username(actor_username) or {}).get("email", "")
    if actor_is_admin:
        owner = owner_email(ticket)
        if owner and owner != actor_email:
            verb = "closed" if ticket["Status"] == "Closed" else f"updated to {ticket['Status']}"
            notify([owner],
                   f"[{APP_TITLE}] Ticket {ticket['Ticket Number']} was {verb}",
                   f"An administrator {verb} your ticket.\n\n{_ticket_details(ticket)}\n\n"
                   f"View your tickets: {url_for('my_tickets', _external=True)}\n")
    else:
        report = url_for("tickets_report", _external=True)
        notify([a for a in admin_recipients() if a != actor_email],
               f"[{APP_TITLE}] Ticket {ticket['Ticket Number']} was closed by its owner",
               f"{ticket['Created By']} closed their ticket.\n\n{_ticket_details(ticket)}\n\n"
               f"Open the report: {report}\n")


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("username"):
        return redirect(url_for("new_ticket_form"))

    ctx = {"code_required": bool(REGISTRATION_CODE)}

    if request.method == "GET":
        return render_template("register.html", **ctx)

    display_name = request.form.get("display_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")
    code = request.form.get("registration_code", "").strip()

    errors = []
    if REGISTRATION_CODE and code != REGISTRATION_CODE:
        errors.append("Invalid registration code.")
    if not display_name:
        errors.append("Please enter your full name.")
    if not EMAIL_RE.match(email) or len(email) > 120:
        errors.append("Please enter a valid email address.")
    elif sheets.get_user_by_login(email):
        errors.append("An account with that email already exists. Try logging in.")
    if len(password) < 6:
        errors.append("Password must be at least 6 characters.")
    elif password != confirm:
        errors.append("Password and confirmation don't match.")

    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("register.html", display_name=display_name, email=email, **ctx)

    need_verify = smtp_configured()

    # The email address is the account's username, so tickets are owned by
    # (and only visible to) the owner of that email. Self-registration always
    # creates a client account, never an admin.
    if not sheets.create_user(email, password, "client", display_name,
                              email=email, verified=not need_verify):
        flash("An account with that email already exists. Try logging in.", "error")
        return render_template("register.html", display_name=display_name, email=email, **ctx)

    if need_verify:
        try:
            send_verification_email(email, display_name)
            flash(f"Almost done! We sent a confirmation link to {email}. "
                  "Open it to activate your account, then log in.", "success")
        except Exception:
            app.logger.exception("Could not send verification email")
            flash("Your account was created but we couldn't send the confirmation "
                  "email. Use 'Resend confirmation email' on the login page to retry.", "error")
        return redirect(url_for("login"))

    _start_session(sheets.get_user_by_email(email))
    flash(f"Welcome, {display_name}! Your account has been created.", "success")
    return redirect(url_for("new_ticket_form"))


@app.route("/verify/<token>")
def verify_email(token):
    try:
        email = _serializer.loads(token, salt="verify-email", max_age=VERIFY_MAX_AGE)
    except SignatureExpired:
        flash("That confirmation link has expired. Request a new one below.", "error")
        return redirect(url_for("login"))
    except BadSignature:
        flash("That confirmation link isn't valid.", "error")
        return redirect(url_for("login"))

    if sheets.mark_verified(email):
        flash("Email confirmed! You can now log in.", "success")
    else:
        flash("We couldn't find that account.", "error")
    return redirect(url_for("login"))


@app.route("/resend-verification", methods=["POST"])
def resend_verification():
    email = request.form.get("email", "").strip().lower()
    user = sheets.get_user_by_email(email) if email else None
    if user and not user["verified"] and smtp_configured():
        try:
            send_verification_email(user["email"], user["display_name"])
        except Exception:
            app.logger.exception("Could not resend verification email")
    # Same message either way so this can't be used to probe which emails exist.
    flash("If that email has an unconfirmed account, a new confirmation link is on its way.", "success")
    return redirect(url_for("login"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# New ticket (client + admin)
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
@login_required
def new_ticket_form():
    return render_template(
        "index.html",
        branches=sheets.BRANCHES,
        priorities=sheets.PRIORITIES,
    )


@app.route("/tickets/new", methods=["POST"])
@login_required
def create_ticket():
    branch = request.form.get("branch", "").strip()
    concern = request.form.get("concern", "").strip()
    priority = request.form.get("priority", "").strip()

    errors = []
    if branch not in sheets.BRANCHES:
        errors.append("Please choose a valid branch.")
    if not concern:
        errors.append("Please describe the concern/request.")
    if priority not in sheets.PRIORITIES:
        errors.append("Please choose a priority.")

    if errors:
        for e in errors:
            flash(e, "error")
        return render_template(
            "index.html",
            branches=sheets.BRANCHES,
            priorities=sheets.PRIORITIES,
            form=request.form,
        )

    ticket = sheets.add_ticket(branch, concern, priority, created_by=session["username"])
    notify_ticket_created(ticket)
    flash(f"Ticket {ticket['Ticket Number']} logged for {branch}.", "success")
    return redirect(url_for("new_ticket_form"))


# ---------------------------------------------------------------------------
# My Tickets (client + admin — whatever they personally submitted)
# ---------------------------------------------------------------------------

@app.route("/my-tickets", methods=["GET"])
@login_required
def my_tickets():
    tickets = sheets.get_tickets_by_creator(session["username"])
    return render_template("my_tickets.html", tickets=tickets)


# ---------------------------------------------------------------------------
# Report / list view (admin only)
# ---------------------------------------------------------------------------

def _apply_filters(tickets, args):
    branch = args.get("branch", "")
    priority = args.get("priority", "")
    status = args.get("status", "")
    search = args.get("q", "").strip().lower()
    date_from = args.get("from", "")
    date_to = args.get("to", "")

    def keep(t):
        if branch and t.get("Branch") != branch:
            return False
        if priority and t.get("Priority") != priority:
            return False
        if status and t.get("Status") != status:
            return False
        if search and search not in str(t.get("Concern/Request", "")).lower() \
                and search not in str(t.get("Ticket Number", "")).lower():
            return False
        created = str(t.get("Date Created", ""))[:10]
        if date_from and created and created < date_from:
            return False
        if date_to and created and created > date_to:
            return False
        return True

    return [t for t in tickets if keep(t)]


@app.route("/tickets", methods=["GET"])
@admin_required
def tickets_report():
    all_tickets = sheets.get_all_tickets()
    filtered = _apply_filters(all_tickets, request.args)

    summary = {
        "total": len(filtered),
        "open": sum(1 for t in filtered if t.get("Status") == "Open"),
        "in_progress": sum(1 for t in filtered if t.get("Status") == "In Progress"),
        "closed": sum(1 for t in filtered if t.get("Status") == "Closed"),
        "high_priority_open": sum(
            1 for t in filtered
            if t.get("Priority") == "High" and t.get("Status") != "Closed"
        ),
    }

    return render_template(
        "tickets.html",
        tickets=filtered,
        branches=sheets.BRANCHES,
        priorities=sheets.PRIORITIES,
        statuses=sheets.STATUSES,
        args=request.args,
        summary=summary,
    )


def _safe_next(default_endpoint: str) -> str:
    nxt = request.form.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return url_for(default_endpoint)


@app.route("/tickets/<int:ticket_id>/status", methods=["POST"])
@admin_required
def update_status(ticket_id):
    new_status = request.form.get("status", "")
    ticket = sheets.get_ticket_by_id(ticket_id)
    if new_status not in sheets.STATUSES:
        flash("Invalid status.", "error")
    elif not ticket:
        flash(f"Ticket #{ticket_id} not found.", "error")
    elif ticket["Status"] == new_status:
        pass  # nothing changed
    else:
        sheets.mark_ticket_status(ticket_id, new_status, changed_by=session["username"])
        notify_status_change(sheets.get_ticket_by_id(ticket_id), session["username"], True)
        flash(f"Ticket {ticket['Ticket Number']} marked as {new_status}.", "success")
    return redirect(_safe_next("tickets_report"))


@app.route("/tickets/<int:ticket_id>/close", methods=["POST"])
@login_required
def close_ticket(ticket_id):
    """Admins can close any ticket; clients can close only their own."""
    is_admin = session.get("role") == "admin"
    ticket = sheets.get_ticket_by_id(ticket_id)
    default = "tickets_report" if is_admin else "my_tickets"

    if not ticket:
        flash(f"Ticket #{ticket_id} not found.", "error")
    elif not is_admin and ticket["Created By"] != session["username"]:
        flash("You can only close tickets that you submitted.", "error")
    elif ticket["Status"] == "Closed":
        flash(f"Ticket {ticket['Ticket Number']} is already closed.", "error")
    else:
        sheets.mark_ticket_status(ticket_id, "Closed", changed_by=session["username"])
        notify_status_change(sheets.get_ticket_by_id(ticket_id), session["username"], is_admin)
        flash(f"Ticket {ticket['Ticket Number']} closed.", "success")
    return redirect(_safe_next(default))


@app.route("/tickets/export.csv", methods=["GET"])
@admin_required
def export_csv():
    all_tickets = sheets.get_all_tickets()
    filtered = _apply_filters(all_tickets, request.args)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=sheets.HEADERS)
    writer.writeheader()
    for t in filtered:
        writer.writerow({h: t.get(h, "") for h in sheets.HEADERS})

    filename = f"twoyi-tickets-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# Account (change own password)
# ---------------------------------------------------------------------------

@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "GET":
        return render_template("account.html")

    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    user = sheets.verify_login(session["username"], current)
    if not user:
        flash("Current password is incorrect.", "error")
    elif len(new) < 6:
        flash("New password must be at least 6 characters.", "error")
    elif new != confirm:
        flash("New password and confirmation don't match.", "error")
    else:
        sheets.change_password(session["username"], new)
        flash("Password updated.", "success")
        return redirect(url_for("new_ticket_form"))

    return render_template("account.html")


@app.route("/admin/test-email", methods=["POST"])
@admin_required
def test_email():
    """Lets an admin check that the SMTP settings actually deliver mail."""
    if not smtp_configured():
        flash("Email isn't set up yet - set SMTP_HOST (see README) and restart the app.", "error")
        return redirect(url_for("account"))
    to = request.form.get("to", "").strip() or session.get("email", "")
    if not EMAIL_RE.match(to):
        flash("Enter a valid email address to send the test to.", "error")
        return redirect(url_for("account"))
    try:
        send_email(to, f"[{APP_TITLE}] Test email",
                   "Email sending is working. Ticket notifications will be delivered to this address.")
        flash(f"Test email sent to {to}.", "success")
    except Exception as exc:
        app.logger.exception("Test email failed")
        flash(f"Couldn't send the test email: {exc}", "error")
    return redirect(url_for("account"))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
