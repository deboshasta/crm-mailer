"""Drive sending_flag_check.main() against a fake DB, with the REAL entry shapes observed in the
live rows on 2026-11-06. No database, no network.
Module under test comes from $SFC_DIR (default: this directory), so a deliberately broken copy can
be run through the same assertions -- the vacuity check, which this file is useless without:
  SFC_DIR=/path/to/broken python test_sending_flag_check.py   -> must exit 1
Needs no database and no network: `db` and `tz` are stubbed."""
import sys, os, types, io, contextlib
sys.path.insert(0, os.environ.get("SFC_DIR", os.path.dirname(os.path.abspath(__file__))))

fake_db = types.ModuleType("db"); fake_tz = types.ModuleType("tz")
fake_tz.today = lambda: "2026-11-06"
INSERTS = []

class Cur:
    def __init__(self, deals): self.deals=deals; self._r=[]
    def execute(self, sql, args=None):
        s=" ".join(sql.split())
        if s.startswith("select d.id"):             self._r=list(self.deals)
        elif "from email_dispatch" in s:            self._r=[(1,)] if args[1]=="confirm_details" else []
        elif "from error_log" in s:                 self._r=[]
        elif s.startswith("insert into error_log"): INSERTS.append(args); self._r=[]
        else: raise AssertionError("unexpected SQL: "+s[:80])
    def fetchall(self): return self._r
    def fetchone(self): return self._r[0] if self._r else None

class Conn:
    def __init__(self, deals): self.deals=deals; self.autocommit=False
    def cursor(self): return Cur(self.deals)
    def close(self): pass

DEALS = [
  # live, delivered (the real Iria shape: claim exists, approval resurrected)
  ("d-iria","Iria Diaz","closed_won",
     {"confirm_details":{"_sending":True,"pending_approval":True,"pending_since":"2026-11-04"},
      "w9_email":{"sent":"2026-11-04"}}),
  # live, never sent (the dangerous case: no claim)
  ("d-stuck","Never Sent Co","closed_won",
     {"review_request":{"_sending":True,"pending_approval":True,"pending_since":"2026-11-05"}}),
  # inert: the 24 real rows - flag stored, approval already cleared
  ("d-amy","Amy Weiss","closed_won",
     {"customization_request":{"_sending":True,"pending_approval":False,"sent":"2026-10-25"}}),
  # inert: pending AND sent together - the case the `sent` guard exists for
  ("d-both","Both Flags Co","closed_won",
     {"rebook_family":{"_sending":True,"pending_approval":True,"sent":"2026-10-27"}}),
  # inert: cancelled
  ("d-cx","Cancelled Co","closed_lost",
     {"closed_lost_after":{"_sending":True,"pending_approval":True,"cancelled":True}}),
  # clean
  ("d-clean","Clean Co","closed_won", {"rebook":{"sent":"2026-10-01"}}),
]

fake_db.connect = lambda: Conn(DEALS)
sys.modules["db"]=fake_db; sys.modules["tz"]=fake_tz
sys.argv=["sending_flag_check.py","--send","--all"]

import sending_flag_check
buf=io.StringIO()
with contextlib.redirect_stdout(buf): sending_flag_check.main()
out=buf.getvalue(); print(out)

ok=True
def chk(cond,label):
    global ok
    print(("  PASS  " if cond else "  FAIL  ")+label); ok = ok and cond

chk("5 total" in out, "sees all five stored flags")
chk("2 hiding a live entry" in out, "exactly the two live ones")
chk("3 inert" in out, "sent / pending+sent / cancelled are all inert")
chk(len(INSERTS)==2, "writes exactly two error_log rows (got %d)"%len(INSERTS))
msgs={a[1]:a[2] for a in INSERTS}
chk("already delivered, approval resurrected" in msgs.get("d-iria",""), "claim found -> resurrected approval")
chk("NOT SENT" in msgs.get("d-stuck",""), "no claim -> NOT SENT")
chk("d-both" not in msgs, "an entry carrying `sent` is never alerted on")
chk("d-cx" not in msgs, "a cancelled entry is never alerted on")
chk(all(a[0]=="sending-flag" for a in INSERTS), "source is sending-flag")
chk("Clean Co" not in out, "a clean deal is never mentioned")
print("\nRESULT:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
