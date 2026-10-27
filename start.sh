#!/usr/bin/env bash
# Khởi chạy stack truyenAI — NHANH.
#   Mặc định: dùng image đã dựng sẵn (KHÔNG rebuild) + GPU + up -d.
#   Vì image backend nặng (torch/CUDA ~4.8GB) nên chỉ rebuild khi cần.
#   KHÔNG reset data (volume PG/Redis giữ nguyên), KHÔNG tạo lại settings.
#
# Cách dùng:
#   ./start.sh                    # nhanh: dùng image hiện có, chờ service ready
#   ./start.sh --build            # rebuild image (khi sửa code/deps) rồi up — chậm
#   ./start.sh --cpu              # chạy CPU (bỏ file GPU override)
#   ./start.sh --force-recreate   # ép tạo lại container (áp dụng .env/cấu hình mới)
#   ./start.sh --down             # chỉ tắt stack, không xóa dữ liệu
#   ./start.sh --help
#
# Chạy nhanh nhưng nhớ: nếu mới sửa backend/frontend mà chưa --build thì sẽ
# chạy image cũ. Sau khi đổi code/deps, chạy `./start.sh --build` một lần.

set -euo pipefail
cd "$(dirname "$0")"

MODE="gpu"            # gpu|cpu
REBUILD=false         # mặc định dùng image hiện có (nhanh)
FORCE=""

usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

for arg in "$@"; do
  case "$arg" in
    --gpu) MODE="gpu" ;;
    --cpu) MODE="cpu" ;;
    --rebuild|--build) REBUILD=true ;;
    --force-recreate) FORCE="--force-recreate" ;;
    --down) exec docker compose down ;;
    --help|-h) usage ;;
    *) echo "Tham số không hợp lệ: $arg" >&2; usage ;;
  esac
done

# Settings: không ghi đè .env đã có; nếu chưa từng có thì tạo từ mẫu.
if [[ ! -f .env ]]; then
  echo "⚠ Không thấy .env — tạo từ .env.example (chưa có key thật)."
  cp .env.example .env
fi

COMPOSE_FILE="docker-compose.yml"
if [[ "$MODE" == "gpu" ]]; then
  COMPOSE_FILE="docker-compose.yml -f docker-compose.gpu.yml"
fi

BUILD_ARGS=()
if $REBUILD; then
  echo "→ Rebuild image (mode: $MODE) — có thể mất vài phút."
  BUILD_ARGS+=("--build")
else
  echo "→ Dùng image hiện có (no-build). Thêm '--build' nếu vừa đổi code/deps."
fi

echo "→ docker compose -f $COMPOSE_FILE up -d ${BUILD_ARGS[*]:-} $FORCE"
# shellcheck disable=SC2086
docker compose -f $COMPOSE_FILE up -d ${BUILD_ARGS[@]:-} $FORCE

# ---------- chờ service thật sự sẵn sàng (giống RLVD start.sh) ----------
wait_url() {
  local url="$1" name="$2" timeout="${3:-90}" code
  local elapsed=0
  echo -n "→ Chờ $name ... "
  while (( elapsed < timeout )); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo 000)
    if [[ "$code" != "000" ]]; then
      echo "OK (HTTP $code)"
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  echo "⚠ $name chưa lên sau ${timeout}s (xem ./start.sh down rồi docker compose logs -f)."
  return 1
}

wait_url "http://localhost:18080/api/health" "API" 90
wait_url "http://localhost:13000" "Dashboard web" 90 || true

_BUILD_LABEL="no-build"; $REBUILD && _BUILD_LABEL="rebuild"

echo
echo "✓ Đã chạy (mode: $MODE, $_BUILD_LABEL)."
echo "  Dashboard: http://localhost:13000"
echo "  API/OpenAPI: http://localhost:18080/docs"
echo "  Health: http://localhost:18080/api/health"
echo "  Dữ liệu (PG/Redis) GIỮ NGUYÊN. Settings (.env) không đổi."
echo "  xem log: docker compose logs -f"
exit 0