#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

if ! command -v rg >/dev/null 2>&1; then
  echo "ripgrep (rg) is required for this scan." >&2
  exit 1
fi

secret_pattern='xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}'
# 汎用の検査（ホームディレクトリの絶対パス、Slack のメンションとチャンネルIDらしい文字列）。
# 組織固有の語は、リポジトリの外に置いたパターンファイルを PUBLIC_SCAN_PATTERNS で渡して検査する（1行1パターン、# はコメント）
internal_pattern='/Users/[A-Za-z0-9._-]+/|<@[UW][A-Z0-9]{8,}>|\bC[A-Z0-9]{10}\b'
# 架空の値として使ってよい形（UBOT000001・UOWNER0001 のように末尾が0の並び＋数字、例示用のチャンネルID）
allowed_examples='AKIA1234567890ABCDEF|xoxb-test-token|<@U[A-Z]*0+[0-9]>|<@U0BOTEXAMPLE>|C0123456789|C9999999999'

# rg の終了コードは 0=見つかった・1=無し・2以上=実行の失敗。失敗を「無し」と取り違えないよう分けて扱う
scan() {
  local label="$1"; shift
  local out status=0
  out="$(rg -n --hidden --glob '!package-lock.json' --glob '!.git' --glob '!scripts/check-public.sh' "$@" .)" || status=$?
  if [ "$status" -ge 2 ]; then
    echo "rg failed while scanning for ${label} (exit ${status})." >&2
    exit 1
  fi
  out="$(printf '%s\n' "$out" | grep -vE "$allowed_examples" | grep -v '^$' || true)"
  if [ -n "$out" ]; then
    printf '%s\n' "$out"
    echo "${label} found. Replace it with a synthetic value."
    exit 1
  fi
}

scan "Potential credential" "$secret_pattern"
scan "Internal identifier" "$internal_pattern"

if [ -n "${PUBLIC_SCAN_PATTERNS:-}" ]; then
  patterns="$(mktemp)"
  trap 'rm -f "$patterns"' EXIT
  grep -vE '^[[:space:]]*(#|$)' "$PUBLIC_SCAN_PATTERNS" > "$patterns"
  scan "Organization-specific term" -i -f "$patterns"
fi

echo "Public-source scan passed."
