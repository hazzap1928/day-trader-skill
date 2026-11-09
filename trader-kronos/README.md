# Kronos, shadow mode

[Kronos](https://github.com/shiyu-coder/Kronos) is an open-source foundation model for candlestick data
(MIT licence). Here it only **watches**. In evidence mode, `trader-broker/evidence.py` asks it about each
signal the autopilot logs, and the Friday report compares how the signals it agreed with did against the
ones it disagreed with. It never places, blocks or changes a trade.

## Will it run on this Mac?

Yes, as **Kronos-small**, so Kronos-mini isn't needed.

| | This Mac | Kronos-small needs |
|---|---|---|
| Chip | 2017 Intel Core i7-7567U, 2 cores | any CPU |
| Memory | 16 GB | about 0.5 GB |
| Model | | 24.7M parameters, up to 512 bars of context |
| GPU | none (CPU only) | optional |

On this CPU expect roughly tens of seconds per signal for 10 sampled paths. The autopilot gives Kronos at
most 6 minutes a cycle, and anything over that is logged as skipped. If it proves too slow, run
`KRONOS_MODEL=mini trader-kronos/setup.sh` for Kronos-mini (4.1M parameters).

## Setup (you run it)

1. **Install Python 3.10 to 3.12.** The system Python here is 3.9. The python.org macOS installer works.
   Pick 3.12 or lower: PyTorch 2.2.2, the last release built for Intel Macs, does not support 3.13.
2. **Run the setup script:**
   ```bash
   trader-kronos/setup.sh /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11
   ```
   It downloads about 400 MB, all into git-ignored folders:
   - PyTorch 2.2.2 and NumPy, pandas, einops, huggingface_hub and safetensors, from PyPI, into `.venv/`
   - the Kronos code, at a pinned commit, from github.com/shiyu-coder/Kronos, into `vendor/`
   - the `NeoQuasar/Kronos-small` and `NeoQuasar/Kronos-Tokenizer-base` model files, about 115 MB, from
     Hugging Face, into `models/`

   It finishes by running one test forecast.

Without the setup, every signal is logged with Kronos "unavailable", and nothing else changes.

## What it reports per signal

- `p_target` and `p_stop`: the share of the 10 sampled paths that reach the target or the stop first.
- `avg_R` and `avg_R_net`: the average result in R, before and after the GBP account's costs (0.15% FX
  fee plus 0.05% slippage on the buy and on the sell).
- `verdict`: **agree** when `avg_R_net` is above 0, otherwise **disagree**. It is shadow only.

Education and tooling, not financial advice.
