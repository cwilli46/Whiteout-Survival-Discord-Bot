name: discord_daily_batch

on:
  workflow_dispatch:
  schedule:
    - cron: "20 9 * * *"   # daily 09:20 UTC

concurrency:
  group: discord-daily
  cancel-in-progress: false

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 25

    env:
      PYTHONWARNINGS: ignore
      PIP_DISABLE_PIP_VERSION_CHECK: "1"
      # Debug/test tuning (feel free to change or remove later)
      DEBUG: "0"
      SAVE_CAPTCHAS: "0"
      REDEEM_PACING_SECONDS: "1.8"
      REDEEM_MAX_ATTEMPTS: "3"
      CAPTCHA_MAX_REFRESH: "4"
      MAX_FIDS: "0"      # 0 = no cap; set small numbers while testing
      MAX_CODES: "0"

      # Required secrets
      DISCORD_BOT_TOKEN: ${{ secrets.DISCORD_BOT_TOKEN }}
      DISCORD_CODES_CHANNEL_ID: ${{ secrets.DISCORD_CODES_CHANNEL_ID }}
      DISCORD_IDS_CHANNEL_ID: ${{ secrets.DISCORD_IDS_CHANNEL_ID }}
      DISCORD_STATE_CHANNEL_ID: ${{ secrets.DISCORD_STATE_CHANNEL_ID }}
      WOS_SECRET: ${{ secrets.WOS_SECRET }}

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: "pip"

      - name: Install system deps (tesseract + fonts)
        run: |
          sudo apt-get update
          sudo apt-get install -y tesseract-ocr libtesseract-dev fonts-dejavu-core

      - name: Install Python deps
        run: |
          python -m pip install --upgrade pip
          pip install requests pillow pytesseract easyocr playwright

      - name: Install Playwright Chromium
        run: |
          python -m playwright install chromium --with-deps

      - name: Preflight env echo (redacted)
        run: |
          echo "Codes channel set:  $([ -n "$DISCORD_CODES_CHANNEL_ID" ] && echo true || echo false)"
          echo "IDs channel set:    $([ -n "$DISCORD_IDS_CHANNEL_ID" ] && echo true || echo false)"
          echo "State channel set:  $([ -n "$DISCORD_STATE_CHANNEL_ID" ] && echo true || echo false)"
          echo "WOS secret set:     $([ -n "$WOS_SECRET" ] && echo true || echo false)"

      - name: Run daily job
        run: |
          set -e
          test -f scripts/daily_discord_batch.py
          echo "Found script: scripts/daily_discord_batch.py"
          python scripts/daily_discord_batch.py
