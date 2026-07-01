#!/usr/bin/env bash
# Wrapper to activate venv, optionally update repo, and run main.py

cd /home/truevar/Documents/TrueVAR-Core || exit 1

# Activate virtual environment
source .venv/bin/activate

exec pip install -r requirements.txt & python -m core.main
