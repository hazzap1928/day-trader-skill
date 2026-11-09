"""Where the research data lives: RESEARCH_DATA, or trader/research-data/ (git-ignored, like all of trader/).
The data is fetched by the *_fetch.py scripts and never committed."""
import os

DATA = os.path.join(os.environ.get("RESEARCH_DATA") or
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "trader", "research-data"), "")
os.makedirs(DATA, exist_ok=True)
