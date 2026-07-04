# -*- coding: utf-8 -*-
"""Generate a branded deposit/payment receipt PDF (returns bytes) for a deal.
Pure-Python via fpdf2 (core Helvetica font, no external assets needed)."""
import datetime
from fpdf import FPDF, XPos, YPos

GOLD = (176, 141, 74)
DARK = (32, 33, 36)
GREY = (110, 110, 116)
SOFT = (245, 245, 246)

_MO = ["January","February","March","April","May","June","July",
       "August","September","October","November","December"]

def _money(v):
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "-"

def _date_str(v):
    if not v:
        return ""
    try:
        d = datetime.date.fromisoformat(str(v)[:10])
        return f"{_MO[d.month-1]} {d.day}, {d.year}"
    except Exception:
        return str(v)

def make_receipt(deal, contact, issued=None):
    """Returns (filename, pdf_bytes, mime) ready for mailer.send_email(attachments=[...])."""
    issued = issued or datetime.date.today()
    show_str = _date_str(deal.get("show_date"))
    issued_str = f"{_MO[issued.month-1]} {issued.day}, {issued.year}"
    did = str(deal.get("id") or "")
    receipt_no = f"SMS-{issued:%Y%m%d}" + (f"-{did[:6].upper()}" if did else "")

    pdf = FPDF(format="Letter", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(20, 18, 20)
    pdf.add_page()
    W = pdf.w - 20 - 20  # printable width

    # ---- header ----
    pdf.set_text_color(*DARK); pdf.set_font("Helvetica", "B", 22)
    pdf.cell(0, 10, "THE SIMON SHOW", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10.5); pdf.set_text_color(*GREY)
    pdf.cell(0, 5.5, "Simon Mandal - Celebrity Magician", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 5.5, "732.492.6071   |   Simon@theSimonShow.com   |   www.TheSimonShow.com",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)
    pdf.set_draw_color(*GOLD); pdf.set_line_width(0.8)
    y = pdf.get_y(); pdf.line(20, y, 20 + W, y); pdf.ln(7)

    # ---- title + meta ----
    pdf.set_font("Helvetica", "B", 15); pdf.set_text_color(*DARK)
    pdf.cell(0, 8, "PAYMENT RECEIPT", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10); pdf.set_text_color(*GREY)
    pdf.cell(0, 5, f"Receipt #:  {receipt_no}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 5, f"Date issued:  {issued_str}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    # ---- billed to ----
    pdf.set_font("Helvetica", "B", 9.5); pdf.set_text_color(*GOLD)
    pdf.cell(0, 5, "BILLED TO", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10.5); pdf.set_text_color(*DARK)
    name = contact.get("full_name") or contact.get("first_name") or "Client"
    pdf.cell(0, 5.5, name, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if contact.get("email"):
        pdf.cell(0, 5.5, contact["email"], new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    phone = contact.get("phone_mobile") or contact.get("phone_other")
    if phone:
        pdf.cell(0, 5.5, phone, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(5)

    # ---- line-item table ----
    pdf.set_fill_color(*SOFT); pdf.set_text_color(*DARK); pdf.set_font("Helvetica", "B", 10)
    pdf.cell(W * 0.68, 8.5, "  Description", fill=True)
    pdf.cell(W * 0.32, 8.5, "Amount  ", align="R", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10.5)
    occ = deal.get("occasion") or "event"
    desc = f"  Deposit - {occ}" + (f" on {show_str}" if show_str else "")
    pdf.cell(W * 0.68, 9, desc)
    pdf.cell(W * 0.32, 9, _money(deal.get("deposit_amount")) + "  ", align="R",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_draw_color(*SOFT); pdf.set_line_width(0.3)
    y = pdf.get_y(); pdf.line(20, y, 20 + W, y); pdf.ln(4)

    # ---- summary (right aligned block) ----
    def summ(label, value, bold=False, gold=False):
        pdf.set_font("Helvetica", "B" if bold else "", 10.5)
        pdf.set_text_color(*(GOLD if gold else DARK))
        pdf.cell(W * 0.68, 6.5, "")
        pdf.set_text_color(*GREY); pdf.set_font("Helvetica", "", 10)
        pdf.cell(W * 0.17, 6.5, label, align="R")
        pdf.set_text_color(*(GOLD if gold else DARK)); pdf.set_font("Helvetica", "B" if bold else "", 10.5)
        pdf.cell(W * 0.15, 6.5, _money(value) + "  ", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    summ("Appearance fee", deal.get("amount"))
    summ("Deposit received", deal.get("deposit_amount"), bold=True, gold=True)
    summ("Balance due on arrival", deal.get("balance_amount"), bold=True)
    pdf.ln(8)

    # ---- thank-you note ----
    pdf.set_text_color(*DARK); pdf.set_font("Helvetica", "", 10.5)
    pdf.multi_cell(0, 6,
        "Thank you so much - your deposit is received and your date is locked in. "
        "The remaining balance is due on arrival the day of the event. "
        "If you have any questions at all, just reply to this email or give me a call.")
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 10.5)
    pdf.cell(0, 6, "With gratitude,", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "Simon Mandal", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    data = pdf.output()  # bytes/bytearray in fpdf2
    return (f"Deposit-Receipt-{receipt_no}.pdf", bytes(data), "application/pdf")
