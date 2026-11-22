"""Open-market insider purchases from the SEC's Form 3/4/5 bulk data (research/form4_fetch.py). Research only.

Kept (one row per filing):
  - Form 4 only (amendments 4/A left out so nothing counts twice)
  - one reporting owner on the filing (joint filings are almost always funds or family groups)
  - the owner is a director or an officer; a 10% owner who is not an officer is left out (large outside
    funds, often with a seat on the board), and so is any owner whose name looks like a company or a fund
    (LLC, LP, fund, capital, partners, holdings, management, ...)
  - transactions with code P (open-market or private purchase), acquired, shares and price above zero.
    Sales, option exercises (M), grants (A), gifts and every other code are ignored.
Written to research-data/insider_buys.json: [{acc, filed, traded, cik, sym, owner, rel, value, shares}].

    python3 research/insider_extract.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import csv, datetime, glob, io, json, re, zipfile

csv.field_size_limit(10 ** 9)
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
ENTITY = re.compile(r"\b(LLC|L\.?L\.?C|LP|L\.?P|LTD|INC|CORP|CORPORATION|FUND|FUNDS|CAPITAL|PARTNERS|PARTNERSHIP|"
                    r"HOLDINGS?|MANAGEMENT|ADVISORS?|ADVISERS?|INVESTMENTS?|GROUP|TRUST CO|N\.?V|S\.?A|PLC|AG|GMBH|"
                    r"ASSOCIATES|VENTURES|EQUITY|OPPORTUNIT(Y|IES)|MASTER)\b", re.I)


def date(s):
    try:
        d, m, y = s.strip().split("-")
        return datetime.date(int(y), MON[m.upper()], int(d)).isoformat()
    except Exception:
        return None


def rows(z, name):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"), delimiter="\t",
                                  quoting=csv.QUOTE_NONE)


def num(x):
    try:
        return float(x)
    except Exception:
        return None


out, counts = [], {"form4": 0, "joint": 0, "not_insider": 0, "fund_like": 0, "with_P": 0}
for path in sorted(glob.glob(DATA + "form4/*_form345.zip")):
    z = zipfile.ZipFile(path)
    sub = {}
    for r in rows(z, "SUBMISSION.tsv"):
        if r["DOCUMENT_TYPE"].strip() == "4":
            sub[r["ACCESSION_NUMBER"]] = (date(r["FILING_DATE"]), r["ISSUERCIK"].strip().lstrip("0"),
                                          (r["ISSUERTRADINGSYMBOL"] or "").strip().upper())
    counts["form4"] += len(sub)
    owners = {}
    for r in rows(z, "REPORTINGOWNER.tsv"):
        if r["ACCESSION_NUMBER"] in sub:
            owners.setdefault(r["ACCESSION_NUMBER"], []).append(r)
    buys = {}
    for r in rows(z, "NONDERIV_TRANS.tsv"):
        a = r["ACCESSION_NUMBER"]
        if a not in sub or r["TRANS_CODE"].strip() != "P" or r["TRANS_ACQUIRED_DISP_CD"].strip() != "A":
            continue
        sh, p = num(r["TRANS_SHARES"]), num(r["TRANS_PRICEPERSHARE"])
        if not sh or not p or sh <= 0 or p <= 0:
            continue
        b = buys.setdefault(a, {"value": 0.0, "shares": 0.0, "traded": []})
        b["value"] += sh * p; b["shares"] += sh
        t = date(r["TRANS_DATE"])
        if t:
            b["traded"].append(t)
    for a, b in buys.items():
        counts["with_P"] += 1
        ow = owners.get(a, [])
        if len(ow) != 1:
            counts["joint"] += 1; continue
        o = ow[0]
        rel = set(x.strip() for x in o["RPTOWNER_RELATIONSHIP"].split(","))
        if not rel & {"Director", "Officer"} or ("TenPercentOwner" in rel and "Officer" not in rel):
            counts["not_insider"] += 1; continue
        if ENTITY.search(o["RPTOWNERNAME"] or ""):
            counts["fund_like"] += 1; continue
        filed, cik, sym = sub[a]
        if not filed or not b["traded"]:
            continue
        out.append({"acc": a, "filed": filed, "traded": min(b["traded"]), "cik": cik, "sym": sym,
                    "owner": o["RPTOWNERCIK"].strip().lstrip("0"), "rel": ",".join(sorted(rel)),
                    "title": (o["RPTOWNER_TITLE"] or "").strip()[:60],
                    "value": round(b["value"], 2), "shares": b["shares"]})
    print(os.path.basename(path), len(out), flush=True)

out.sort(key=lambda x: (x["filed"], x["acc"]))
json.dump(out, open(DATA + "insider_buys.json", "w"))
print("Form 4 filings %(form4)d; with a purchase %(with_P)d; joint filings left out %(joint)d; "
      "not a director/officer (or a 10%% owner only) %(not_insider)d; fund-like names %(fund_like)d" % counts)
print("kept %d purchase filings" % len(out))
