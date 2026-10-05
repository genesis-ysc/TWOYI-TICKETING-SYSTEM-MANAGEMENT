# Twoyi Ticketing System (client / admin logins, local database)

Same app as before — ticket form, filterable report, CSV export — with tickets
stored in a local SQLite file (`tickets.db`, no Google API needed) and now
**two separate account types**:

| Role | Can do |
|---|---|
| **client** | Log in, submit tickets, view **My Tickets** (only what they personally submitted), and **close their own tickets** |
| **admin** | Everything a client can do, plus **All Tickets & Reports**: see every ticket, filter/search, change status, **close or reopen any ticket**, export CSV |

## Default accounts

Created automatically the first time you run the app:

| Username | Password | Role |
|---|---|---|
| `admin` | `admin123` | admin |
| `client` | `client123` | client |

**Change both passwords immediately** — either from the app (top-right →
**Account**, once logged in) or see "Adding real accounts" below.

## Run it

```bash
pip install -r requirements.txt
python app.py
```

Open **http://localhost:5000** — you'll land on the login page.

## What's included

```
twoyi-ticketing-auth/
├── app.py              # Flask routes + login/role decorators
├── db.py                # SQLite: tickets, users, auth
├── requirements.txt
├── templates/
│   ├── base.html          # nav changes based on logged-in role
│   ├── login.html
│   ├── register.html        # email sign-up
│   ├── account.html        # change your own password
│   ├── index.html           # new ticket form (client + admin)
│   ├── my_tickets.html       # "my submissions" view (client + admin)
│   └── tickets.html           # full report (admin only)
└── static/
    └── style.css
```

## Registration with email

Anyone can register at **/register** (linked from the login page) using their
**email address**. The email is their login, and **every ticket they submit is
tied to that email** - **My Tickets** shows only the tickets owned by the
email they're logged in with. Two people can never see each other's tickets,
and admins see everything (with the owner's email in the "Logged by" column).

- Log in with the email (any capitalisation) or, for the built-in `admin` /
  `client` accounts, the username.
- Emails are unique - registering the same address twice is rejected.
- Passwords need at least 6 characters.
- Self-registration always creates a **client** account, never an admin.

### Confirming the email (recommended)

Without email sending, the app can't prove someone really owns the address they
typed, so by default new accounts are active immediately. To require that
people confirm their email, set your SMTP details before starting the app:

```bash
export SMTP_HOST="smtp.gmail.com"        # your mail provider's SMTP server
export SMTP_PORT="587"                    # 587 (STARTTLS) or 465 (SSL)
export SMTP_USER="you@yourcompany.com"
export SMTP_PASSWORD="your-app-password"  # Gmail: use an App Password
export SMTP_FROM="Twoyi Tickets <you@yourcompany.com>"
python app.py
```

(Windows: use `set` instead of `export`.) With `SMTP_HOST` set:

1. A new account is created **unconfirmed** and a confirmation link is emailed.
2. Opening the link (valid 48 hours) activates the account.
3. Logging in before confirming is blocked, with a **Resend confirmation
   email** button on the login page.

Set `FLASK_SECRET_KEY` to a fixed random value too - confirmation links are
signed with it, so links stop working if it changes between restarts.
The confirmation link uses the address the person registered from, so on a real
server register via its real URL, not `localhost`.

### Restrict who can sign up

Optionally require an invite code on the form:

```bash
export REGISTRATION_CODE="TWOYI2026"   # Windows: set REGISTRATION_CODE=TWOYI2026
```

Share the code only with your staff. Leave it unset for open registration.

## Creating admin accounts

Registration never creates admins (otherwise anyone could give themselves
full access). Add admins yourself from the project folder:

```bash
python3 -c "
import db
db.create_user('juan.delacruz', 'a-strong-password', 'admin', 'Juan Dela Cruz')
"
```

`create_user` returns `False` if the username is already taken.

Use `'client'` instead of `'admin'` if you'd rather create a client account
yourself. To create several at once, put one `create_user(...)` call per
person in a `.py` file and run it once.

## Closing tickets

Ticket statuses are **Open -> In Progress -> Closed**.

- **Clients** get a **Close ticket** button on **My Tickets** for their own open
  tickets. They can't close anyone else's, and they can't reopen a ticket once
  it's closed.
- **Admins** get a **Close** button on every open ticket in **All Tickets &
  Reports**, plus the status dropdown, which can also **reopen** a closed
  ticket (set it back to Open or In Progress).
- Closing stamps **Date Finished** and records **who closed it** (shown under
  the status, and in the CSV as "Closed By"). Reopening clears both.
- Tickets that were "Resolved" in an earlier version become "Closed"
  automatically the first time you run this version.

## Email notifications

The app can email people about their tickets. It uses the same SMTP settings
as the registration confirmation email (see above) - once `SMTP_HOST` is set,
notifications are on. Without SMTP settings, nothing is sent and the app works
as before.

| When | Who gets an email |
|---|---|
| A ticket is submitted | The **owner** (confirmation) and the **admins** (new-ticket alert) |
| An admin closes or changes a ticket | The **owner** |
| An owner closes their own ticket | The **admins** |

- **Owners** are emailed at the email address they registered with. The built-in
  `client` account has no email address, so it gets none.
- **Admins** are emailed if their account has a confirmed email address, and at
  any address listed in `ADMIN_NOTIFY_EMAIL` - use that for a shared support
  inbox, e.g. `export ADMIN_NOTIFY_EMAIL="support@yourcompany.com,ops@yourcompany.com"`.
  The default `admin` account has no email; either add an admin account with
  an email (see "Creating admin accounts") or set `ADMIN_NOTIFY_EMAIL`.
- Nobody is emailed about their own action, and nobody gets duplicates.
- Emails are sent in the background, and a mail failure is logged but never
  blocks or breaks a ticket action.
- **Check it works:** log in as admin -> **Account** -> **Send test email**.
- Local test mail servers without TLS (e.g. MailHog on port 1025): also set
  `SMTP_STARTTLS=0`. Leave it unset for real providers.

## How "Created By" works

Every ticket stores who submitted it (their email, or username for the built-in accounts). Clients only ever
see their own tickets on **My Tickets**; admins see everyone's on
**All Tickets & Reports**, with a "Logged by" column, and that column is
included in the CSV export too.

## Security notes for real use

- Sessions are signed with `FLASK_SECRET_KEY` — set this to a long random
  value in production (`export FLASK_SECRET_KEY=...`), don't leave the
  default.
- This runs over plain HTTP by default (`python app.py`). If you put it on a
  shared network or the internet, put it behind HTTPS (a reverse proxy like
  Caddy or nginx, or a host that terminates TLS for you) so passwords and
  session cookies aren't sent in the clear.
- Passwords are hashed with Werkzeug's `generate_password_hash` (salted,
  not reversible) — the database never stores plain text passwords.

## Deploying for the team

SQLite is file-based, so for one shared set of tickets and accounts, run this
app on a single always-on machine or small host (Render, Railway, an internal
VM, etc.) rather than everyone running their own local copy.
