#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Pre-push safety checklist
# Checks everything that causes GitHub Actions or Vercel deployments to break.
#
# Run manually:   bash scripts/pre_push_check.sh
# Auto-runs via:  .githooks/pre-push  (install: git config core.hooksPath .githooks)
# ─────────────────────────────────────────────────────────────────────────────

PASS=0
FAIL=0
WARN=0

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}PASS${RESET}  $1"; PASS=$((PASS + 1)); }
fail() { echo -e "  ${RED}FAIL${RESET}  $1"; FAIL=$((FAIL + 1)); }
warn() { echo -e "  ${YELLOW}WARN${RESET}  $1"; WARN=$((WARN + 1)); }

echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}  Pre-Push Checklist${RESET}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""

# ── 1. Tests ──────────────────────────────────────────────────────────────────
echo -e "${BOLD}[1/7] Tests${RESET}"
if python -m pytest -q --tb=short 2>&1; then
  ok "All tests passed"
else
  fail "Tests failed — fix before pushing"
fi
echo ""

# ── 2. Lint ───────────────────────────────────────────────────────────────────
echo -e "${BOLD}[2/7] Lint${RESET}"
if python -m ruff check . 2>&1; then
  ok "ruff: no errors"
else
  fail "ruff errors found — run: ruff check . --fix"
fi
echo ""

# ── 3. Secrets & .env ─────────────────────────────────────────────────────────
echo -e "${BOLD}[3/7] Secrets & .env${RESET}"

if git ls-files --error-unmatch .env > /dev/null 2>&1; then
  fail ".env IS tracked by git — remove: git rm --cached .env"
else
  ok ".env not tracked by git"
fi

if grep -q "^\.env$" .gitignore 2>/dev/null || grep -q "^\.env " .gitignore 2>/dev/null; then
  ok ".env in .gitignore"
else
  fail ".env not in .gitignore"
fi

# Scan staged diff for real secrets (long values, not placeholder text)
STAGED_DIFF=$(git diff --cached --diff-filter=ACM 2>/dev/null || true)
if echo "$STAGED_DIFF" | grep -E "^\+" | grep -v "^\+\+\+" | \
   grep -iE "(api_key|apikey|secret|password|token|PAT)\s*=\s*['\"]?[A-Za-z0-9_\-]{20,}" | \
   grep -v '=""' | grep -v "your_key" | grep -v "example" | grep -v "#" | grep -q "."; then
  fail "Possible hardcoded secret in staged changes — check before pushing"
  echo "$STAGED_DIFF" | grep -E "^\+" | grep -v "^\+\+\+" | \
    grep -iE "(api_key|apikey|secret|password|token|PAT)\s*=\s*['\"]?[A-Za-z0-9_\-]{20,}" | \
    grep -v '=""' | grep -v "your_key" | grep -v "example" | grep -v "#"
else
  ok "No hardcoded secrets in staged changes"
fi
echo ""

# ── 4. Vercel deployment ──────────────────────────────────────────────────────
echo -e "${BOLD}[4/7] Vercel deployment${RESET}"

if [ -f vercel.json ]; then
  if python -c "import json, sys; json.load(open('vercel.json'))" 2>/dev/null; then
    ok "vercel.json is valid JSON"
  else
    fail "vercel.json is invalid JSON"
  fi

  if grep -q '"runtime"' vercel.json 2>/dev/null; then
    fail "vercel.json has 'runtime' key — causes 'must have valid version' error. Remove it."
  else
    ok "vercel.json has no invalid 'runtime' key"
  fi

  MAX_DUR=$(python -c "
import json, sys
try:
    d = json.load(open('vercel.json'))
    vals = [v.get('maxDuration', 0) for v in d.get('functions', {}).values()]
    print(max(vals) if vals else 0)
except:
    print(0)
" 2>/dev/null)
  if [ "${MAX_DUR:-0}" -gt 10 ] 2>/dev/null; then
    warn "vercel.json maxDuration=${MAX_DUR}s exceeds Hobby plan limit (10s) — needs Pro plan"
  else
    ok "vercel.json maxDuration within Hobby limit (<=10s)"
  fi

  FUNC_COUNT=$(python -c "
import json
d = json.load(open('vercel.json'))
funcs = d.get('functions', {})
empty = [k for k,v in funcs.items() if not v]
print(len(empty))
" 2>/dev/null || echo "0")
  if [ "${FUNC_COUNT:-0}" -gt 0 ] 2>/dev/null; then
    fail "vercel.json has empty function object {} — must have at least one property"
  else
    ok "vercel.json function objects are non-empty"
  fi
else
  fail "vercel.json missing"
fi

if [ -f .python-version ]; then
  PY_VER=$(cat .python-version | tr -d '[:space:]')
  ok ".python-version exists (Python $PY_VER)"
else
  fail ".python-version missing — Vercel will use wrong Python version"
fi

if [ -f pyproject.toml ]; then
  if grep -q '^\[build-system\]' pyproject.toml 2>/dev/null; then
    fail "pyproject.toml has [build-system] — Vercel runs 'uv sync' and fails. Remove [build-system] and [project] sections."
  else
    ok "pyproject.toml has no [build-system] — will not conflict with Vercel"
  fi
fi

if [ -f api/index.py ]; then
  if grep -q 'handler\s*=' api/index.py 2>/dev/null; then
    ok "api/index.py has handler variable (Mangum entry point)"
  else
    fail "api/index.py missing 'handler = Mangum(app)'"
  fi
else
  fail "api/index.py missing — Vercel entry point not found"
fi

if [ -f requirements.txt ]; then
  for pkg in fastapi mangum psycopg2-binary sqlmodel jinja2; do
    if grep -qi "^${pkg}" requirements.txt 2>/dev/null; then
      ok "requirements.txt contains $pkg"
    else
      fail "requirements.txt missing '$pkg'"
    fi
  done
  if grep -q 'uvicorn\[standard\]' requirements.txt 2>/dev/null; then
    warn "uvicorn[standard] in requirements.txt pulls uvloop (C extension) — may fail on Vercel. Use plain uvicorn."
  else
    ok "requirements.txt uses plain uvicorn (no C extensions)"
  fi
else
  fail "requirements.txt missing"
fi

if grep -rn 'Jinja2Templates(directory="app/templates")' app/routes/ > /dev/null 2>&1; then
  fail "Relative Jinja2Templates path in app/routes/ — use Path(__file__).parent.parent / 'templates'"
  grep -rn 'Jinja2Templates(directory="app/templates")' app/routes/
else
  ok "All Jinja2Templates use absolute paths"
fi
echo ""

# ── 5. GitHub Actions ─────────────────────────────────────────────────────────
echo -e "${BOLD}[5/7] GitHub Actions${RESET}"

if [ -f .github/workflows/bulk_process.yml ]; then
  ok ".github/workflows/bulk_process.yml present"
  if grep -q 'secrets.DATABASE_URL' .github/workflows/bulk_process.yml 2>/dev/null; then
    ok "Workflow references secrets.DATABASE_URL"
  else
    warn "Workflow missing secrets.DATABASE_URL — bulk jobs will fail if no DB configured"
  fi
  if grep -q 'secrets.BOUNCIFY_API_KEY' .github/workflows/bulk_process.yml 2>/dev/null; then
    ok "Workflow references secrets.BOUNCIFY_API_KEY"
  else
    warn "Workflow missing secrets.BOUNCIFY_API_KEY"
  fi
else
  warn ".github/workflows/bulk_process.yml missing — GitHub Actions bulk processing disabled"
fi

if [ -f scripts/process_job.py ]; then
  ok "scripts/process_job.py present"
else
  fail "scripts/process_job.py missing — bulk_process.yml workflow will fail"
fi
echo ""

# ── 6. Debug debris ───────────────────────────────────────────────────────────
echo -e "${BOLD}[6/7] Debug debris${RESET}"

PRINTS=$(grep -rn "^\s*print(" app/ --include="*.py" 2>/dev/null | grep -v "# noqa" | wc -l | tr -d ' ')
if [ "${PRINTS:-0}" -gt 0 ]; then
  warn "${PRINTS} print() statement(s) in app/ — remove debug output before pushing"
  grep -rn "^\s*print(" app/ --include="*.py" | grep -v "# noqa"
else
  ok "No stray print() statements in app/"
fi

TODOS=$(grep -rn "TODO\|FIXME" app/ --include="*.py" 2>/dev/null | wc -l | tr -d ' ')
if [ "${TODOS:-0}" -gt 0 ]; then
  warn "${TODOS} TODO/FIXME marker(s) in app/ — review before pushing"
else
  ok "No TODO/FIXME markers in app/"
fi
echo ""

# ── 7. Critical files ─────────────────────────────────────────────────────────
echo -e "${BOLD}[7/7] Critical files${RESET}"
MISSING=0
for f in \
  app/main.py app/config.py app/db.py app/models.py app/schemas.py \
  app/routes/api_single.py app/routes/api_bulk.py app/routes/api_stats.py \
  app/routes/ui.py app/routes/health.py app/workers/bulk_worker.py \
  app/templates/base.html api/index.py vercel.json requirements.txt \
  .python-version .gitignore; do
  if [ ! -f "$f" ]; then
    fail "Missing critical file: $f"
    MISSING=$((MISSING + 1))
  fi
done
if [ "$MISSING" -eq 0 ]; then
  ok "All critical files present"
fi
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
TOTAL=$((PASS + FAIL + WARN))
echo -e "  ${GREEN}${PASS} passed${RESET}  |  ${RED}${FAIL} failed${RESET}  |  ${YELLOW}${WARN} warnings${RESET}  (${TOTAL} checks)"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""

if [ "$FAIL" -gt 0 ]; then
  echo -e "${RED}${BOLD}  ✘ Push blocked — fix ${FAIL} failing check(s) above.${RESET}"
  echo ""
  exit 1
elif [ "$WARN" -gt 0 ]; then
  echo -e "${YELLOW}${BOLD}  ⚠ ${WARN} warning(s) — review above, push if intentional.${RESET}"
  echo ""
  exit 0
else
  echo -e "${GREEN}${BOLD}  ✔ All checks passed — safe to push.${RESET}"
  echo ""
  exit 0
fi
