#!/usr/bin/env bash
# Заливка датасету з Google Drive у бакет GCS для режиму B (rclone, без файлу ключа на диску).
#
#   tools/ingest_drive_to_gcs.sh --drive-path "Datasets/Bahasa" --bucket my-audio-bucket --prefix Bahasa [--transfers 16] [--dry-run]
#
# Вимоги:
#   - rclone з remote для Drive (за замовчуванням "gdrive", змінити: RCLONE_DRIVE_REMOTE=...).
#   - Доступ до GCS через Application Default Credentials: `gcloud auth application-default login`
#     або обліковий запис ВМ. Ключ сервісного акаунта у файлі не потрібен.
# Що робить: копіює dataset.csv і audio/ (лише аудіо й CSV), потім звіряє хеші та кількість
# файлів і пише маніфест у ./ingest_<prefix>_<час>.txt. Повторний запуск докопіює лише нове.
set -euo pipefail

DRIVE_REMOTE="${RCLONE_DRIVE_REMOTE:-gdrive}"
DRIVE_PATH="" BUCKET="" PREFIX="" TRANSFERS=16 DRY_RUN=0

die() { echo "ERROR: $*" >&2; exit 1; }
usage() { sed -n '2,13p' "$0"; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --drive-path) DRIVE_PATH="${2:-}"; shift 2 ;;
    --bucket) BUCKET="${2:-}"; shift 2 ;;
    --prefix) PREFIX="${2:-}"; shift 2 ;;
    --transfers) TRANSFERS="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
    *) die "невідомий параметр: $1" ;;
  esac
done

# Перевірка вхідних даних: жодних метасимволів shell, правила імен бакетів GCS
[[ -n "$DRIVE_PATH" && -n "$BUCKET" && -n "$PREFIX" ]] || usage 1
[[ "$BUCKET" =~ ^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$ && "$BUCKET" != *goog* ]] || die "некоректне ім'я бакета: $BUCKET"
[[ "$PREFIX" =~ ^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ ]] || die "prefix: лише A-Z a-z 0-9 . _ - і /"
[[ "$DRIVE_PATH" != *$'\n'* && "$DRIVE_PATH" != *'..'* ]] || die "некоректний drive-path"
[[ "$TRANSFERS" =~ ^[0-9]{1,3}$ ]] || die "transfers має бути числом"
command -v rclone >/dev/null || die "rclone не встановлено"
rclone listremotes | grep -qx "${DRIVE_REMOTE}:" || die "немає rclone remote '${DRIVE_REMOTE}:' (rclone config)"

SRC="${DRIVE_REMOTE}:${DRIVE_PATH}"
DST=":gcs,env_auth=true,bucket_policy_only=true:${BUCKET}/${PREFIX}"
FILTERS=(--filter "+ /dataset.csv" --filter "+ /audio/**.{wav,WAV,mp3,flac,ogg,m4a}" --filter "- **")
COMMON=(--transfers "$TRANSFERS" --checkers 32 --fast-list --retries 5 --low-level-retries 10 --stats 30s --stats-one-line)

echo "Джерело:   $SRC"
echo "Призначення: gs://${BUCKET}/${PREFIX}/"
[[ $DRY_RUN -eq 1 ]] && COMMON+=(--dry-run) && echo "(сухий прогін, нічого не копіюється)"

src_count=$(rclone lsf -R --files-only "${FILTERS[@]}" "$SRC" | wc -l)
[[ "$src_count" -gt 0 ]] || die "у $SRC не знайдено dataset.csv і аудіо в audio/"
echo "Файлів у джерелі: $src_count"

rclone copy "$SRC" "$DST" "${FILTERS[@]}" "${COMMON[@]}"
[[ $DRY_RUN -eq 1 ]] && exit 0

# Звірка: Drive і GCS обидва зберігають MD5, тож порівнюємо хеші, а не лише розміри
echo "Звірка хешів..."
if ! rclone check "$SRC" "$DST" "${FILTERS[@]}" --one-way --fast-list --checkers 32; then
  die "файли не збіглися: запустіть скрипт ще раз (докопіює відсутні) і повторіть перевірку"
fi

manifest="ingest_${PREFIX//\//_}_$(date +%Y%m%d-%H%M%S).txt"
rclone lsf -R --files-only --format "psh" --hash MD5 "$DST" > "$manifest"
dst_count=$(wc -l < "$manifest")
echo "Готово: $dst_count файлів у gs://${BUCKET}/${PREFIX}/, маніфест: $manifest"
echo "Для режиму B: bucket=${BUCKET}, csv_object=${PREFIX}/dataset.csv, audio_prefix=${PREFIX}/audio/"
