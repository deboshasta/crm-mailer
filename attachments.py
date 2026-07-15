# -*- coding: utf-8 -*-
"""Resolve per-template email attachments: static files in assets/ plus the
dynamically generated deposit receipt. Returns a list of
(filename, data_bytes, mime) tuples ready for mailer.send_email(attachments=...)."""
import os
import receipt

ASSETS = os.path.join(os.path.dirname(__file__), "assets")

def _static(diskname, sendname, mime="application/pdf"):
    with open(os.path.join(ASSETS, diskname), "rb") as f:
        return (sendname, f.read(), mime)

def attachments_for(key, deal, contact):
    """Attachments a given template needs when sent. Empty list for plain emails."""
    if key == "deposit_receipt":
        try:
            paid_in_full = (float(deal.get("deposit_amount") or 0) > 0 and
                            float(deal.get("deposit_amount") or 0) == float(deal.get("amount") or 0))
        except Exception:
            paid_in_full = False
        return [receipt.make_receipt(deal, contact, paid_in_full=paid_in_full)]
    if key == "w9_email":
        return [_static("w9.pdf", "W9 - Simon Show Productions LLC.pdf")]
    if key == "magic_castle":
        return [_static("magic-castle-invite.pdf", "Magic Castle Invitation.pdf")]
    return []
