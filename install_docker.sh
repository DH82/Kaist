#!/usr/bin/env bash
# Ubuntu에 Docker·Compose v2·NVIDIA Container Toolkit을 설치하고 현재 계정에 docker 권한을 줍니다.
# (sudo 필요, 한 번만. 여러 번 실행해도 안전합니다.)
set -euo pipefail
sudo apt-get update
sudo apt-get install -y python3 zstd curl openssl iproute2 nfs-common ca-certificates gnupg git gh
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  echo "Docker가 이미 설치되어 있습니다: $(docker --version)"
else
  sudo apt-get update
  sudo apt-get install -y docker.io docker-compose-v2 docker-buildx
fi

# GPU 컨테이너(gpu-worker)용 NVIDIA Container Toolkit. GPU가 없는 PC는 건너뜁니다.
if command -v nvidia-smi >/dev/null && ! command -v nvidia-ctk >/dev/null; then
  KEY=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor --yes -o "$KEY"
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed "s#deb https://#deb [signed-by=$KEY] https://#g" \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y nvidia-container-toolkit
fi
if command -v nvidia-ctk >/dev/null; then
  sudo nvidia-ctk runtime configure --runtime=docker
fi
sudo systemctl enable docker
sudo systemctl restart docker

if ! id -nG "$USER" | grep -qw docker; then
  sudo usermod -aG docker "$USER"
  echo "docker 그룹에 추가했습니다. 로그아웃 후 다시 로그인하거나 'newgrp docker'를 실행하세요."
fi
echo "완료. GPU 확인: docker run --rm --gpus all ubuntu:26.04 nvidia-smi"
