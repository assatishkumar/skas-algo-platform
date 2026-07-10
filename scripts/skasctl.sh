#!/usr/bin/env bash
#
# skasctl.sh — thin control wrapper for the skas-algo systemd service on a Linux VPS.
# (macOS uses launchd via scripts/start.sh|stop.sh|status.sh — this is the Linux analogue.)
#
#   ./scripts/skasctl.sh restart
#   ./scripts/skasctl.sh logs
#
# Tip: symlink it onto your PATH so you can call it from anywhere as `skas`:
#   sudo ln -sf "$PWD/scripts/skasctl.sh" /usr/local/bin/skas   # then: skas restart
set -euo pipefail
SVC=skas-algo
cmd="${1:-status}"
case "$cmd" in
  start|stop|restart|enable|disable) exec sudo systemctl "$cmd" "$SVC" ;;
  status)  exec systemctl status "$SVC" --no-pager ;;
  logs)    exec journalctl -u "$SVC" -f ;;            # follow (Ctrl-C to exit)
  tail)    exec journalctl -u "$SVC" -n "${2:-100}" --no-pager ;;  # last N lines (default 100)
  health)  exec curl -fsS http://127.0.0.1:8080/api/v1/health ;;
  *)
    echo "usage: $(basename "$0") {start|stop|restart|enable|disable|status|logs|tail [N]|health}" >&2
    exit 2 ;;
esac
