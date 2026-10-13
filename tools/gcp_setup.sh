#!/usr/bin/env bash
# Інфраструктура GCP для режиму B однією командою (ідемпотентно, можна запускати повторно).
#
#   tools/gcp_setup.sh --project my-project --bucket my-audio-bucket [--region us-central1]
#                      [--sa-name audio-ai-qa] [--runs-retention-days 30] [--key-out ./sa-key.json] [--dry-run]
#
# Створює:
#   1. API: Vertex AI, Cloud Storage, IAM.
#   2. Бакет: uniform access, заборона публічного доступу, lifecycle (ai-qa-runs/ видаляється
#      через N днів, незавершені завантаження через 7 днів).
#   3. Сервісний акаунт для n8n з мінімальними правами: Storage Object Admin лише на цей бакет,
#      Vertex AI User на проєкт.
#   4. Права сервісного агента Vertex AI на бакет (batch-задача читає аудіо й пише результати).
#   5. Ключ (лише з --key-out): n8n потребує JSON ключа для креду Google Service Account.
#      Файл створюється з правами 600; після вставки в n8n його треба видалити.
set -euo pipefail

PROJECT="" BUCKET="" REGION="us-central1" SA_NAME="audio-ai-qa" RETENTION=30 KEY_OUT="" DRY_RUN=0

die() { echo "ERROR: $*" >&2; exit 1; }
usage() { sed -n '2,16p' "$0"; exit "${1:-0}"; }
run() {  # друкує команду; виконує, якщо не --dry-run
  printf '+ %q' "$1"; shift; printf ' %q' "$@"; echo
  [[ $DRY_RUN -eq 1 ]] || "$@"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="${2:-}"; shift 2 ;;
    --bucket) BUCKET="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    --sa-name) SA_NAME="${2:-}"; shift 2 ;;
    --runs-retention-days) RETENTION="${2:-}"; shift 2 ;;
    --key-out) KEY_OUT="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
    *) die "невідомий параметр: $1" ;;
  esac
done

[[ -n "$PROJECT" && -n "$BUCKET" ]] || usage 1
[[ "$PROJECT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "некоректний project id: $PROJECT"
[[ "$BUCKET" =~ ^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$ && "$BUCKET" != *goog* ]] || die "некоректне ім'я бакета: $BUCKET"
[[ "$REGION" =~ ^[a-z]+-[a-z]+[0-9]$ ]] || die "некоректний регіон: $REGION"
[[ "$SA_NAME" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "некоректне ім'я сервісного акаунта: $SA_NAME"
[[ "$RETENTION" =~ ^[0-9]{1,4}$ && "$RETENTION" -ge 1 ]] || die "runs-retention-days має бути числом >= 1"
command -v gcloud >/dev/null || die "gcloud не встановлено"

SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
G=(gcloud --project "$PROJECT" --quiet)

echo "== 1. API"
run "${G[@]}" services enable aiplatform.googleapis.com storage.googleapis.com iam.googleapis.com

echo "== 2. Бакет gs://$BUCKET"
if gcloud storage buckets describe "gs://$BUCKET" --project "$PROJECT" >/dev/null 2>&1; then
  echo "   вже існує"
else
  run "${G[@]}" storage buckets create "gs://$BUCKET" --location "$REGION" \
      --uniform-bucket-level-access --public-access-prevention
fi
lifecycle=$(mktemp)
trap 'rm -f "$lifecycle"' EXIT
cat > "$lifecycle" <<JSON
{"rule": [
  {"action": {"type": "Delete"}, "condition": {"age": $RETENTION, "matchesPrefix": ["ai-qa-runs/"]}},
  {"action": {"type": "AbortIncompleteMultipartUpload"}, "condition": {"age": 7}}
]}
JSON
run "${G[@]}" storage buckets update "gs://$BUCKET" --lifecycle-file "$lifecycle"

echo "== 3. Сервісний акаунт $SA_EMAIL"
if gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT" >/dev/null 2>&1; then
  echo "   вже існує"
else
  run "${G[@]}" iam service-accounts create "$SA_NAME" --display-name "Audio AI-QA (n8n)"
fi
run "${G[@]}" storage buckets add-iam-policy-binding "gs://$BUCKET" \
    --member "serviceAccount:$SA_EMAIL" --role roles/storage.objectAdmin
run "${G[@]}" projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:$SA_EMAIL" --role roles/aiplatform.user --condition None

echo "== 4. Сервісний агент Vertex AI"
run "${G[@]}" beta services identity create --service aiplatform.googleapis.com
if [[ $DRY_RUN -eq 1 ]]; then
  AGENT="service-<PROJECT_NUMBER>@gcp-sa-aiplatform.iam.gserviceaccount.com"
else
  NUMBER=$(gcloud projects describe "$PROJECT" --format 'value(projectNumber)')
  AGENT="service-${NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com"
fi
run "${G[@]}" storage buckets add-iam-policy-binding "gs://$BUCKET" \
    --member "serviceAccount:$AGENT" --role roles/storage.objectAdmin

echo "== 5. Ключ"
if [[ -n "$KEY_OUT" ]]; then
  [[ ! -e "$KEY_OUT" ]] || die "файл $KEY_OUT вже існує, не перезаписую"
  ( umask 077; run "${G[@]}" iam service-accounts keys create "$KEY_OUT" --iam-account "$SA_EMAIL" )
  echo "   Вставте вміст у кред n8n «Google Service Account» (scope https://www.googleapis.com/auth/cloud-platform),"
  echo "   потім видаліть файл: rm $KEY_OUT"
else
  echo "   пропущено (додайте --key-out, щоб створити ключ для n8n)"
fi

echo
echo "Готово. Налаштування режиму B: gcp_project=$PROJECT, gcp_location=$REGION, bucket=$BUCKET"
echo "Видалити все після проєкту: gcloud storage rm -r gs://$BUCKET && gcloud iam service-accounts delete $SA_EMAIL"
