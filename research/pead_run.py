import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import sys
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "three_strategies.py")).read()
head, main = src.split('if __name__ == "__main__":')
exec(head)
def member(s, d):                      # S&P members only, from the day they joined: no hindsight names
    a = added.get(s)
    return bool(a) and s not in BENCH and a[:10] <= d
n_rows = sum(len(v or []) for v in E.values())
print("earnings rows %d over %d days" % (n_rows, len(E)))
sys.argv = ["x", "pead"]
exec("if True:" + main)
