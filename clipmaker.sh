#!/bin/bash
# Navigate to the folder where this script is located
cd "$(dirname "$0")"

# Activate the virtual environment
source venv/bin/activate

# Launch the app
streamlit run app_streamlit.py
