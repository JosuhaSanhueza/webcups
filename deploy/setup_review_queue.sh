#!/bin/bash
# Bloquea las impresiones sin autorizar: la cola que usan los PCs queda pausada y una cola
# privada (<cola>_Salida), conectada a la misma impresora, solo acepta órdenes del propio servidor.
#
# Instalar:  sudo bash setup_review_queue.sh <cola>
# Deshacer:  sudo bash setup_review_queue.sh <cola> --undo
set -euo pipefail

QUEUE="${1:?Uso: setup_review_queue.sh <cola> [--undo]}"
ACTION="${2:-install}"
OUTPUT="${QUEUE}_Salida"
CUPSD_CONF="${CUPSD_CONF:-/etc/cups/cupsd.conf}"
BACKUP="${CUPSD_CONF}.webcups.bak"

if [ "$ACTION" = "--undo" ]; then
    lpmove "$OUTPUT" "$QUEUE" 2>/dev/null || true
    lpadmin -x "$OUTPUT" 2>/dev/null || true
    cupsenable "$QUEUE"
    cupsaccept "$QUEUE"
    [ -f "$BACKUP" ] && cp "$BACKUP" "$CUPSD_CONF" && systemctl restart cups
    echo "DESHECHO: $QUEUE vuelve a imprimir directo; revisa el Modo revisión en el panel."
    exit 0
fi

DEVICE="$(lpstat -v "$QUEUE" | sed -E 's/^[^:]*: *//')"
PPD="${PPD_DIR:-/etc/cups/ppd}/${QUEUE}.ppd"
[ -n "$DEVICE" ] || { echo "ERROR: no encontré la cola $QUEUE" >&2; exit 1; }
[ -f "$PPD" ] || { echo "ERROR: no existe $PPD" >&2; exit 1; }

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

lpadmin -p "$OUTPUT" -E -v "$DEVICE" -P "$PPD" -D "Salida de $QUEUE (solo panel)" \
    -o printer-op-policy=salida -o printer-is-shared=false -o job-hold-until-default=no-hold
cupsenable "$OUTPUT"
cupsaccept "$OUTPUT"

lpadmin -p "$QUEUE" -o job-hold-until-default=indefinite
cupsdisable -r "Esperando autorización" "$QUEUE"
cupsaccept "$QUEUE"

# El estado debe sobrevivir a un reinicio de CUPS, que además ocurre con cada rotación de logs
systemctl restart cups
sleep 2
if ! lpstat -p "$QUEUE" | grep -qiE 'disabled|deshabilitada'; then
    echo "ERROR: $QUEUE no quedó pausada." >&2
    exit 1
fi
lpstat -a "$QUEUE" "$OUTPUT"
echo "APLICADO: $QUEUE pausada y $OUTPUT lista."
