#!/bin/zsh
# Set up Kronos for shadow verdicts (trader-broker/evidence.py). Run it yourself:
#   trader-kronos/setup.sh /path/to/python3.11          # any Python 3.10 to 3.12
#   KRONOS_MODEL=mini trader-kronos/setup.sh ...         # the smaller model instead of Kronos-small
# It creates trader-kronos/.venv, installs pinned packages from PyPI, fetches the Kronos code at a
# pinned commit from github.com/shiyu-coder/Kronos into vendor/, and the model files from Hugging Face
# (NeoQuasar) into models/. All three folders are git-ignored. About 400 MB in total.
set -eu
cd "${0:A:h}"
PY="${1:-}"
if [[ -z "$PY" ]]; then
  for c in python3.12 python3.11 python3.10 /usr/local/bin/python3.12 /usr/local/bin/python3.11 /usr/local/bin/python3.10; do
    if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
  done
fi
if [[ -z "$PY" ]]; then
  echo "No Python 3.10+ found. Install one (python.org, macOS installer), then: trader-kronos/setup.sh /path/to/python3.11" >&2
  exit 2
fi
"$PY" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)' || {
  echo "$PY is not Python 3.10 to 3.12 (PyTorch 2.2.2, the last Intel Mac build, needs one of those)" >&2; exit 2; }
"$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
# Intel Macs: PyTorch 2.2.2 is the last build for x86_64 macOS, and it needs numpy < 2
.venv/bin/python -m pip install "torch==2.2.2" "numpy<2" "pandas<2.3" "einops==0.8.0" "huggingface_hub<0.26" \
    "safetensors<0.5" "tqdm"
KRONOS_COMMIT=67b630e67f6a18c9e9be918d9b4337c960db1e9a
if [[ ! -d vendor/Kronos ]]; then
  mkdir -p vendor
  git clone --quiet https://github.com/shiyu-coder/Kronos.git vendor/Kronos
fi
git -C vendor/Kronos checkout --quiet "$KRONOS_COMMIT"
MODEL="${KRONOS_MODEL:-small}"
.venv/bin/python - "$MODEL" <<'PY'
import sys
from huggingface_hub import snapshot_download
model = sys.argv[1]
pairs = {"small": ("NeoQuasar/Kronos-small", "NeoQuasar/Kronos-Tokenizer-base"),
         "mini": ("NeoQuasar/Kronos-mini", "NeoQuasar/Kronos-Tokenizer-2k")}[model]
for repo in pairs:
    snapshot_download(repo_id=repo, local_dir="models/" + repo.split("/")[1])
open("models/MODEL", "w").write(model + "\n")
print("models ready:", ", ".join(pairs))
PY
echo "Checking one forecast on a fixed test series..."
.venv/bin/python kronos_shadow.py --self-test
