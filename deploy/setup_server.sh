#!/usr/bin/env bash
# 식구봇 서버 설치 스크립트 (Ubuntu 24.04 기준, 여러 번 실행해도 안전해요)
# 사용:  sudo bash setup_server.sh
#
# 하는 일: 업데이트 / 스왑 / MariaDB(저메모리 설정) / 봇 전용 계정 / 코드 설치 / 서비스·타이머 등록 / 첫 데이터 채우기
# 비밀 정보는 이 스크립트에 없어요. /etc/dbd-bot.env 파일에 서버에서 만들어지고, 디스코드 토큰은 직접 적어 넣어요.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

APP_DIR=/opt/dbd-bot
APP_USER=dbdbot
REPO=https://github.com/gamppe/dbd-discord-bot.git
ENV_FILE=/etc/dbd-bot.env

log() { echo; echo "=== $* ==="; }

if [ "$(id -u)" -ne 0 ]; then echo "sudo로 실행해 주세요: sudo bash $0"; exit 1; fi

log "시간대와 보안 업데이트"
timedatectl set-timezone Asia/Seoul
apt-get update -y
apt-get -y -o Dpkg::Options::=--force-confold upgrade
apt-get install -y git python3-venv python3-pip mariadb-server unattended-upgrades openssl
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

log "스왑 파일 2GB (메모리가 1GB라서)"
if ! grep -q '^/swapfile' /proc/swaps; then
  [ -f /swapfile ] || { fallocate -l 2G /swapfile; chmod 600 /swapfile; mkswap /swapfile >/dev/null; }
  swapon /swapfile
fi
grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
echo 'vm.swappiness=20' > /etc/sysctl.d/99-dbd.conf
sysctl --system >/dev/null

log "MariaDB (메모리 절약 설정, 이 서버 안에서만 접속)"
cat > /etc/mysql/mariadb.conf.d/99-dbd-lowmem.cnf <<'EOF'
[mysqld]
bind-address = 127.0.0.1
innodb_buffer_pool_size = 64M
performance_schema = OFF
max_connections = 30
key_buffer_size = 8M
tmp_table_size = 16M
max_heap_table_size = 16M
EOF
systemctl enable mariadb >/dev/null 2>&1
systemctl restart mariadb

log "비밀 설정 파일 $ENV_FILE (root만 읽을 수 있어요)"
if [ ! -f "$ENV_FILE" ]; then
  PW=$(openssl rand -hex 24)
  ( umask 077; cat > "$ENV_FILE" <<EOF
# 식구봇 비밀 설정 파일 - 절대 GitHub나 채팅에 올리지 마세요.
# DISCORD_TOKEN 뒤에 봇 토큰을 직접 적어 넣으세요 (따옴표 없이, 공백 없이).
DISCORD_TOKEN=
DB_HOST=127.0.0.1
DB_USER=dbd
DB_NAME=dbd
DB_PASSWORD=$PW
EOF
  )
fi
chown root:root "$ENV_FILE"; chmod 600 "$ENV_FILE"

log "DB와 봇 전용 DB 계정 (root 계정은 쓰지 않아요)"
DBPW=$(grep '^DB_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)
mariadb <<SQL
CREATE DATABASE IF NOT EXISTS dbd CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'dbd'@'localhost' IDENTIFIED BY '$DBPW';
CREATE USER IF NOT EXISTS 'dbd'@'127.0.0.1' IDENTIFIED BY '$DBPW';
ALTER USER 'dbd'@'localhost' IDENTIFIED BY '$DBPW';
ALTER USER 'dbd'@'127.0.0.1' IDENTIFIED BY '$DBPW';
GRANT ALL PRIVILEGES ON dbd.* TO 'dbd'@'localhost';
GRANT ALL PRIVILEGES ON dbd.* TO 'dbd'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

log "봇 전용 리눅스 계정과 코드"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  runuser -u "$APP_USER" -- git -C "$APP_DIR" pull --ff-only
else
  runuser -u "$APP_USER" -- git clone "$REPO" "$APP_DIR"
fi
runuser -u "$APP_USER" -- python3 -m venv "$APP_DIR/venv"
runuser -u "$APP_USER" -- "$APP_DIR/venv/bin/pip" install --no-cache-dir --upgrade pip >/dev/null
runuser -u "$APP_USER" -- "$APP_DIR/venv/bin/pip" install --no-cache-dir -r "$APP_DIR/requirements.txt"

log "서비스와 주간 갱신 타이머 등록"
cat > /etc/systemd/system/dbd-bot.service <<'EOF'
[Unit]
Description=식구봇 (데바데 디스코드 봇)
After=network-online.target mariadb.service
Wants=network-online.target
Requires=mariadb.service

[Service]
Type=simple
User=dbdbot
Group=dbdbot
WorkingDirectory=/opt/dbd-bot
EnvironmentFile=/etc/dbd-bot.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/dbd-bot/venv/bin/python /opt/dbd-bot/bot.py
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/dbd-sync.service <<'EOF'
[Unit]
Description=식구봇 데이터 갱신 (퍽, 아이템, 에드온)
After=network-online.target mariadb.service
Wants=network-online.target
Requires=mariadb.service

[Service]
Type=oneshot
User=dbdbot
Group=dbdbot
WorkingDirectory=/opt/dbd-bot
EnvironmentFile=/etc/dbd-bot.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
ExecStart=/opt/dbd-bot/venv/bin/python /opt/dbd-bot/dbd/sync_perks.py
TimeoutStartSec=900
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
EOF

cat > /etc/systemd/system/dbd-sync.timer <<'EOF'
[Unit]
Description=식구봇 데이터 주간 갱신

[Timer]
OnCalendar=Wed *-*-* 04:00:00
Persistent=true
RandomizedDelaySec=15min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now dbd-sync.timer
# 봇 서비스는 토큰을 넣은 뒤에 켜요 (토큰이 비어 있으면 계속 재시작만 반복해요):  sudo systemctl enable --now dbd-bot

log "첫 데이터 채우기 (1~3분 걸려요)"
systemctl start dbd-sync.service
journalctl -u dbd-sync.service -n 20 --no-pager | tail -n 20

log "끝"
if [ -f /var/run/reboot-required ]; then echo "※ 커널 업데이트가 있어서 재부팅이 필요해요: sudo reboot"; fi
echo "다음: sudo nano $ENV_FILE 에서 DISCORD_TOKEN을 채운 뒤  sudo systemctl enable --now dbd-bot"
