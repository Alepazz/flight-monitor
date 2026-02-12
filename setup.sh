#!/bin/bash
# Setup Flight Monitor
# Configures the environment and installs the macOS LaunchAgent.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.flightmonitor"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
PYTHON=$(which python3)
VENV_DIR="$SCRIPT_DIR/venv"

echo "================================================"
echo "  Flight Monitor Setup"
echo "================================================"
echo ""

# 1. Config
if [ ! -f "$SCRIPT_DIR/config.json" ]; then
    if [ ! -f "$SCRIPT_DIR/config.example.json" ]; then
        echo "Errore: config.example.json non trovato!"
        exit 1
    fi

    echo "→ config.json non trovato. Creo dalla template..."
    cp "$SCRIPT_DIR/config.example.json" "$SCRIPT_DIR/config.json"

    echo ""
    echo "  Configurazione interattiva (premi Invio per il default):"
    echo ""

    read -rp "  Email mittente (Gmail): " EMAIL_FROM
    if [ -n "$EMAIL_FROM" ]; then
        # Cross-platform sed
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' "s/you@gmail.com/$EMAIL_FROM/g" "$SCRIPT_DIR/config.json"
        else
            sed -i "s/you@gmail.com/$EMAIL_FROM/g" "$SCRIPT_DIR/config.json"
        fi
    fi

    read -rp "  Gmail App Password (vedi README): " APP_PASS
    if [ -n "$APP_PASS" ]; then
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' "s/YOUR_APP_PASSWORD/$APP_PASS/" "$SCRIPT_DIR/config.json"
        else
            sed -i "s/YOUR_APP_PASSWORD/$APP_PASS/" "$SCRIPT_DIR/config.json"
        fi
    fi

    read -rp "  Email CC (opzionale, Invio per saltare): " EMAIL_CC
    if [ -n "$EMAIL_CC" ]; then
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' "s/\"email_cc\": \"\"/\"email_cc\": \"$EMAIL_CC\"/" "$SCRIPT_DIR/config.json"
        else
            sed -i "s/\"email_cc\": \"\"/\"email_cc\": \"$EMAIL_CC\"/" "$SCRIPT_DIR/config.json"
        fi
    fi

    echo ""
    echo "  ✓ config.json creato. Puoi modificare altri parametri manualmente."
    echo ""
else
    echo "  ✓ config.json trovato"
fi

# 2. Virtual environment
echo "→ Creo virtual environment e installo dipendenze..."
$PYTHON -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install -q -r "$SCRIPT_DIR/requirements.txt"
echo "  ✓ Dipendenze installate"

# 3. Test rapido
echo ""
echo "→ Verifico che fast-flights funzioni..."
"$VENV_DIR/bin/python3" -c "
from fast_flights import FlightData, Passengers, get_flights
print('  ✓ fast-flights importato correttamente')
" 2>&1

# 4. LaunchAgent (macOS only)
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo ""
    echo "→ Configuro esecuzione automatica (macOS LaunchAgent)..."

    INTERVAL=$("$VENV_DIR/bin/python3" -c "import json; print(int(json.load(open('$SCRIPT_DIR/config.json'))['check_interval_hours'] * 3600))")

    mkdir -p "$HOME/Library/LaunchAgents"

    cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_DIR}/bin/python3</string>
        <string>${SCRIPT_DIR}/monitor.py</string>
    </array>
    <key>StartInterval</key>
    <integer>${INTERVAL}</integer>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/launchd_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/launchd_stderr.log</string>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
PLIST

    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    launchctl load "$PLIST_PATH"

    echo "  ✓ LaunchAgent installato (check ogni $(($INTERVAL / 3600)) ore)"
else
    echo ""
    echo "  ℹ Non sei su macOS — configura un cron job o systemd timer manualmente:"
    echo "    */720 * * * * $VENV_DIR/bin/python3 $SCRIPT_DIR/monitor.py"
fi

echo ""
echo "================================================"
echo "  Setup completato!"
echo "================================================"
echo ""
echo "  Comandi utili:"
echo "  • Test manuale:    $VENV_DIR/bin/python3 $SCRIPT_DIR/monitor.py"
echo "  • Vedi log:        tail -f $SCRIPT_DIR/monitor.log"
echo "  • Storico prezzi:  cat $SCRIPT_DIR/price_history.jsonl"
echo "  • Offerte trovate: cat $SCRIPT_DIR/deals.txt"
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo "  • Stop monitor:    launchctl unload $PLIST_PATH"
    echo "  • Riavvia:         launchctl unload $PLIST_PATH && launchctl load $PLIST_PATH"
fi
echo ""

# 5. Telegram (opzionale)
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  OPZIONALE: Notifiche Telegram"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Per ricevere notifiche anche su telefono:"
echo "  1. Cerca @BotFather su Telegram → /newbot → copia il token"
echo "  2. Cerca @userinfobot → ottieni il tuo chat_id"
echo "  3. Inserisci token e chat_id in config.json"
echo ""
