#!/bin/bash

VENV_DIR="venv"

# Create venv if it doesn't exist
if [ ! -f "$VENV_DIR/bin/python" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# Install dependencies using venv python
"$VENV_DIR/bin/python" -m pip install -r requirements.txt

# Run your server using venv python
"$VENV_DIR/bin/python" -m rewrite.frontend.server