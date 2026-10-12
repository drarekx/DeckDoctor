#!/usr/bin/env bash
# Instala DeckDoctor para el usuario actual: acceso directo en el menú (y en el Escritorio con --desktop).
# No toca el sistema (SteamOS tiene la raíz de solo lectura): todo queda en tu carpeta personal.
#   ./install.sh              instalar o actualizar el acceso directo
#   ./install.sh --desktop    además, copia del acceso directo en ~/Desktop
#   ./install.sh --uninstall  quitar los accesos directos y, si estaba activada, la API (no borra esta carpeta)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ENTRY="$APPS/deckdoctor.desktop"
DESKTOP="$HOME/Desktop/deckdoctor.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$ENTRY" "$DESKTOP"
    UNIT="$HOME/.config/systemd/user/deckdoctor-api.service"
    if [[ -f "$UNIT" ]]; then
        systemctl --user disable --now deckdoctor-api.service 2>/dev/null || true
        rm -f "$UNIT"
        systemctl --user daemon-reload 2>/dev/null || true
        echo "API para dashboards desactivada y quitada."
    fi
    echo "Accesos directos quitados. Puedes borrar $DIR y ~/.config/deckdoctor si ya no los quieres."
    exit 0
fi

# Requisitos: Python 3 con Tkinter (vienen con SteamOS). pkexec hace falta para los arreglos que piden contraseña.
if ! python3 -c "import tkinter" 2>/dev/null; then
    echo "Error: hace falta Python 3 con Tkinter." >&2
    exit 1
fi
command -v pkexec >/dev/null || echo "Aviso: no está pkexec; los arreglos que necesitan permisos de administrador fallarán."
[[ "$DIR" == "$HOME/Apps/DeckDoctor" ]] || echo "Nota: el botón «Abrir DeckSpace» busca DeckSpace en ~/Apps/DeckSpace."
command -v xdg-open >/dev/null || echo "Aviso: no está xdg-open; «Abrir carpeta» no funcionará."

chmod +x "$DIR/deckdoctor.py"
mkdir -p "$APPS"
cat > "$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Name=DeckDoctor
Comment=Chequeo de salud de la Deck con arreglos seguros
Exec=/usr/bin/env python3 "$DIR/deckdoctor.py"
Icon=utilities-system-monitor
Terminal=false
Categories=Utility;System;
EOF
chmod +x "$ENTRY"
echo "Acceso directo creado: $ENTRY"

if [[ "${1:-}" == "--desktop" ]]; then
    mkdir -p "$HOME/Desktop"
    cp "$ENTRY" "$DESKTOP"
    chmod +x "$DESKTOP"
    echo "Copia en el Escritorio: $DESKTOP"
fi
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
echo "Listo. Abre DeckDoctor desde el menú de aplicaciones (Utilidades o Sistema)."
