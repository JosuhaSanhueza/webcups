#!/bin/bash
# Bloquea las impresiones sin autorizar: la cola que usan los PCs apunta a un destino de retención
# y una cola privada (<cola>_Salida), conectada a la impresora, solo acepta órdenes del servidor.
#
# Instalar:  sudo bash setup_review_queue.sh <cola>
# Deshacer:  sudo bash setup_review_queue.sh <cola> --undo
set -euo pipefail

QUEUE="${1:?Uso: setup_review_queue.sh <cola> [--undo]}"
ACTION="${2:-install}"
OUTPUT="${QUEUE}_Salida"
HOLD_URI="webcups-hold:/"
CUPSD_CONF="${CUPSD_CONF:-/etc/cups/cupsd.conf}"
BACKUP="${CUPSD_CONF}.webcups.bak"
BACKEND_DIR="${BACKEND_DIR:-/usr/lib/cups/backend}"
HERE="$(cd "$(dirname "$0")" && pwd)"

device_of() {
    lpstat -v "$1" 2>/dev/null | sed -E 's/^[^:]*: *//'
}

if [ "$ACTION" = "--undo" ]; then
    DEVICE="$(device_of "$OUTPUT")"
    if [ -n "$DEVICE" ]; then
        lpadmin -p "$QUEUE" -v "$DEVICE" -o job-hold-until-default=no-hold
        lpmove "$OUTPUT" "$QUEUE" 2>/dev/null || true
        lpadmin -x "$OUTPUT"
    fi
    cupsenable "$QUEUE"
    cupsaccept "$QUEUE"
    rm -f "$BACKEND_DIR/webcups-hold"
    [ -f "$BACKUP" ] && cp "$BACKUP" "$CUPSD_CONF" && systemctl restart cups
    echo "DESHECHO: $QUEUE vuelve a imprimir directo."
    exit 0
fi

# Si ya estaba instalado, la impresora real es la de la cola de salida
DEVICE="$(device_of "$QUEUE")"
case "$DEVICE" in
    webcups-hold*) DEVICE="$(device_of "$OUTPUT")" ;;
esac
PPD="${PPD_DIR:-/etc/cups/ppd}/${QUEUE}.ppd"
[ -n "$DEVICE" ] || { echo "ERROR: no encontré el dispositivo de $QUEUE" >&2; exit 1; }
[ -f "$PPD" ] || { echo "ERROR: no existe $PPD" >&2; exit 1; }

install -m 755 -o "$(id -u)" "$HERE/webcups-hold" "$BACKEND_DIR/webcups-hold"

if ! grep -q '^<Policy salida>' "$CUPSD_CONF"; then
    [ -f "$BACKUP" ] || cp "$CUPSD_CONF" "$BACKUP"
    cat >> "$CUPSD_CONF" <<'POLICY'

# Cola de salida de EscolarApp Print: solo el propio servidor puede usarla
<Policy salida>
  JobPrivateAccess default
  JobPrivateValues default
  SubscriptionPrivateAccess default
  SubscriptionPrivateValues default
  <Limit All>
    Order deny,allow
    Deny all
    Allow localhost
  </Limit>
</Policy>
POLICY
    if ! cupsd -t -c "$CUPSD_CONF" >/dev/null 2>&1; then
        cp "$BACKUP" "$CUPSD_CONF"
        echo "ERROR: la configuración no es válida; se restauró la copia." >&2
        exit 1
    fi
    systemctl restart cups
    sleep 2
fi

# Primero la salida, para no perder nunca la referencia a la impresora real
lpadmin -p "$OUTPUT" -E -v "$DEVICE" -P "$PPD" -D "Salida de $QUEUE (solo panel)" \
    -o printer-op-policy=salida -o printer-is-shared=false -o job-hold-until-default=no-hold
cupsenable "$OUTPUT"
cupsaccept "$OUTPUT"

lpadmin -p "$QUEUE" -v "$HOLD_URI" -o job-hold-until-default=indefinite
cupsenable "$QUEUE"
cupsaccept "$QUEUE"

systemctl restart cups
sleep 2
if [ "$(device_of "$QUEUE")" != "$HOLD_URI" ]; then
    echo "ERROR: $QUEUE no quedó apuntando al destino de retención." >&2
    exit 1
fi
lpstat -a "$QUEUE"
lpstat -a "$OUTPUT"
echo "APLICADO: $QUEUE activa con retención y $OUTPUT conectada a $DEVICE."
