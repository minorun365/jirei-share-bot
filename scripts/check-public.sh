#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

secret_pattern='xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}'
internal_pattern='github\.kddi\.com|kddi-agdc\.com|kddi-agile\.slack\.com|/Users/(mi-onda|minorun365)|105778051969|628643639414|715841358122|C0BEE9US2PL|NVX7R9YI3W|JURZJZTWXY|melco|auエネルギー|アイシン|Buffmee|エナリス|みのるん|アサヒ|Crosslink|NAiS|花之内|横瀬|トヨタ|SimSta|ビジネスイノベーション|グループイノベーション'

if rg -n --hidden --glob '!package-lock.json' --glob '!scripts/check-public.sh' "$secret_pattern" . \
  | rg -v 'AKIA1234567890ABCDEF|xoxb-test-token'; then
  echo "Potential credential found. Replace it with a synthetic value."
  exit 1
fi

if rg -n -i --hidden --glob '!package-lock.json' --glob '!scripts/check-public.sh' "$internal_pattern" .; then
  echo "Internal identifier or production example found."
  exit 1
fi

echo "Public-source scan passed."
