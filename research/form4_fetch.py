"""The SEC's free Form 3/4/5 bulk data sets ("Insider Transactions Data Sets"), one zip a quarter, saved under
research-data/form4/. Research only; read-only.

The SEC refuses requests without a contact in the User-Agent (its fair-access rule). The contact is never written
in this file: set it yourself for the one run, e.g.

    SEC_USER_AGENT="research-script/1.0 you@example.com" python3 research/form4_fetch.py

Quarters 2018q4 (for the 30-day look-back at the start of 2019) to the newest the SEC has published.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import time, urllib.request, urllib.error

UA = os.environ.get("SEC_USER_AGENT")
if not UA:
    sys.exit("set SEC_USER_AGENT (a name and a contact email) for this run; see the top of this file")
OUT = DATA + "form4/"
os.makedirs(OUT, exist_ok=True)
BASES = ("https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/",
         "https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/")  # 2026q2 on

got, missing = [], []
for y in range(2018, 2027):
    for q in range(1, 5):
        if (y, q) < (2018, 4):
            continue
        f = "%dq%d_form345.zip" % (y, q)
        if os.path.exists(OUT + f) and os.path.getsize(OUT + f) > 100000:
            got.append(f); continue
        err = None
        for base in BASES:
            try:
                req = urllib.request.Request(base + f, headers={"User-Agent": UA})
                data = urllib.request.urlopen(req, timeout=120).read()
                open(OUT + f, "wb").write(data)
                got.append(f)
                print("%s %.1f MB" % (f, len(data) / 1e6), flush=True)
                err = None
                break
            except urllib.error.HTTPError as e:
                err = e.code
            time.sleep(0.5)
        if err is not None:
            missing.append("%s (%d)" % (f, err))
print("have %d quarters; not published or refused: %s" % (len(got), ", ".join(missing) or "none"))
