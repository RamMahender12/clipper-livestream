#!/usr/bin/env bash
# One-time server setup, safe to re-run. Runs ON the server (deploy.sh pipes it
# over SSH). Tested target: Ubuntu 24.04 LTS.
set -euo pipefail

if ! command -v docker >/dev/null 2>&1 || ! sudo docker compose version >/dev/null 2>&1; then
  echo "[setup] installing Docker"
  sudo apt-get update -y
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 rsync
  sudo systemctl enable --now docker
fi
command -v rsync >/dev/null 2>&1 || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y rsync

# A little swap so a long render + transcription can't get the box OOM-killed.
if [ -z "$(sudo swapon --show --noheadings)" ]; then
  echo "[setup] adding 4G swap"
  sudo fallocate -l 4G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

sudo mkdir -p /opt/gta6clipper/data /opt/gta6clipper/secrets
sudo chown -R "$(id -u):$(id -g)" /opt/gta6clipper
chmod 700 /opt/gta6clipper/secrets
echo "[setup] ok: $(sudo docker --version), $(sudo docker compose version --short)"
