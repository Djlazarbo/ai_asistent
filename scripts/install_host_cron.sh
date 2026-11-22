#!/usr/bin/env bash
set -euo pipefail
ROOT=/opt/job-hunter
mkdir -p "$ROOT/scripts" /var/lib/job-hunter "$ROOT/backups"
chmod +x "$ROOT/scripts/"*.sh "$ROOT/scripts/"*.py 2>/dev/null || true
# ensure GETMATCH off by default in .env
grep -q '^GETMATCH_ENABLED=' "$ROOT/.env" 2>/dev/null \
  && sed -i 's/^GETMATCH_ENABLED=.*/GETMATCH_ENABLED=0/' "$ROOT/.env" \
  || echo 'GETMATCH_ENABLED=0' >> "$ROOT/.env"

CRON_FILE=/etc/cron.d/job-hunter
cat > "$CRON_FILE" <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
JH_ROOT=/opt/job-hunter

# health every 2 minutes
*/2 * * * * root /usr/bin/python3 $ROOT/scripts/jh_monitor.py >> /var/log/jh_monitor.log 2>&1

# daily backup 02:15 UTC
15 2 * * * root /bin/bash $ROOT/scripts/jh_backup.sh >> /var/log/jh_backup.log 2>&1

# daily digest 06:30 UTC (~09:30 MSK)
30 6 * * * root /usr/bin/python3 $ROOT/scripts/jh_digest.py >> /var/log/jh_digest.log 2>&1
EOF
chmod 644 "$CRON_FILE"
# run once now
/usr/bin/python3 "$ROOT/scripts/jh_monitor.py" || true
/bin/bash "$ROOT/scripts/jh_backup.sh" || true
echo CRON_INSTALLED
cat "$CRON_FILE"
