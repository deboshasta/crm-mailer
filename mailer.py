# -*- coding: utf-8 -*-
"""Provider-agnostic mailer for the CRM sequencer.
Reads From / Reply-To / safe-mode from settings, and transport (SMTP host/user/pass)
from private.config. Safe-mode routes every send to settings.safe_mode_recipient (you),
so NO client is ever emailed until mail_safe_mode is turned off.
"""
import ssl, smtplib, os
from email.message import EmailMessage
from db import connect

# Signature card image: embedded INLINE (Content-ID) so email clients render it without the
# recipient clicking "display images" (which they must do for remote-URL images).
_CARD_URL = "https://www.thesimonshow.com/wp/wp-content/uploads/2024/02/business-card-front-and-back.jpg"
_CARD_LOCAL = os.path.join(os.path.dirname(__file__), "assets", "signature-card.jpg")

def load_config():
    c = connect(); cur = c.cursor()
    cur.execute("select mail_safe_mode, safe_mode_recipient, send_from_email, send_from_name from settings where id=1")
    s = cur.fetchone()
    cur.execute("select key, value from private.config where key like 'smtp\\_%'")
    sm = {k: v for k, v in cur.fetchall()}
    c.close()
    return {
        "safe_mode": bool(s[0]), "safe_recipient": str(s[1]),
        "from_email": str(s[2]), "from_name": s[3],
        "host": sm.get("smtp_host", "smtp.zoho.com"),
        "port": int(sm.get("smtp_port", "465")),
        "user": sm.get("smtp_user"), "password": sm.get("smtp_password"),
    }

def send_email(to_email, subject, html_body, reply_to=None, attachments=None):
    """attachments: optional list of (filename, data_bytes, mime_type),
    e.g. ("Deposit-Receipt.pdf", b"...", "application/pdf")."""
    cfg = load_config()
    if not cfg["user"] or not cfg["password"]:
        raise RuntimeError("SMTP not configured yet (private.config smtp_user / smtp_password).")
    # SAFE MODE: redirect the real send to you, tag the intended recipient in the subject
    real_to = cfg["safe_recipient"] if cfg["safe_mode"] else to_email
    subj = f"[SAFE -> {to_email}] {subject}" if cfg["safe_mode"] else subject

    msg = EmailMessage()
    msg["From"] = f"{cfg['from_name']} <{cfg['from_email']}>"
    msg["To"] = real_to
    msg["Reply-To"] = reply_to or cfg["from_email"]
    msg["Subject"] = subj
    # embed the signature card image inline (Content-ID) -> no "display images" prompt
    inline = []
    if _CARD_URL in html_body and os.path.exists(_CARD_LOCAL):
        html_body = html_body.replace(_CARD_URL, "cid:sigcard")
        with open(_CARD_LOCAL, "rb") as f:
            inline.append(("<sigcard>", f.read()))
    msg.set_content("This message is best viewed in an HTML-capable email client.")
    msg.add_alternative(html_body, subtype="html")
    if inline:
        html_part = msg.get_payload()[1]   # 0 = text/plain, 1 = text/html
        for (cid, data) in inline:
            html_part.add_related(data, maintype="image", subtype="jpeg", cid=cid)
    # file attachments promote the message to multipart/mixed automatically
    for (fn, data, mime) in (attachments or []):
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=fn)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx) as smtp:
        smtp.login(cfg["user"], cfg["password"])
        smtp.send_message(msg)
    return {"routed_to": real_to, "safe_mode": cfg["safe_mode"],
            "attachments": [a[0] for a in (attachments or [])]}

if __name__ == "__main__":
    # smoke test: send a sample to yourself (safe-mode routes to you anyway)
    r = send_email("client@example.com", "CRM mailer test",
                   "<p>Hi Simon - this is a test send from the new CRM mailer.</p>"
                   "<p>If you got this, Zoho SMTP is wired and safe-mode is working.</p>")
    print("sent:", r)
