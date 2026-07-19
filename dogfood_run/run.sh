#!/usr/bin/env bash
# Dogfooding driver for Friday V3 (2026-07-14).
# Runs the full command sequence, capturing per-step timing, exit code, stdout.
set -u
cd "/home/lakshay/Projects/Friday V3"
export LOGDIR="dogfood_run"
mkdir -p "$LOGDIR"
RUN_ID=$(date +%Y%m%d_%H%M%S)
SUMMARY="$LOGDIR/summary_$RUN_ID.tsv"

# step <tag> <command...>
step() {
  local tag="$1"; shift
  local start end dur
  start=$(date +%s.%N)
  local out="$LOGDIR/${tag}.out"
  local err="$LOGDIR/${tag}.err"
  "$@" >"$out" 2>"$err"
  local code=$?
  end=$(date +%s.%N)
  dur=$(awk "BEGIN{printf \"%.2f\", $end-$start}")
  printf "%s\t%s\t%s\t%s\n" "$tag" "$code" "$dur" "$*" >>"$SUMMARY"
  printf "[%s] code=%s dur=%ss  %s\n" "$tag" "$code" "$dur" "$*"
}

# 1. Reset DB
step "00_reset" rm -f ~/.friday/friday.db

# 2. ingest
step "01_ingest" friday ingest ~/Projects

# 3. observe
step "02_observe" friday observe

# 4-5. context
step "03_context_show" friday context
step "04_context_build" friday context build
step "05_context_show2" friday context

# 6. sessions
step "06_sessions" friday sessions
# 7. timeline
step "07_timeline" friday timeline

# 8-13. knowledge
step "08_knowledge_show" friday knowledge
step "09_knowledge_build" friday knowledge build
step "10_knowledge_show2" friday knowledge
step "11_knowledge_verify" friday knowledge verify
# NOTE: knowledge IDs are timestamp-based (e.g. 2026-07-14T..:project_identity:Aether),
# NOT sequential 1..5. The original `explain 1..5` commands fail (exit 2, "not found").
step "12_kx_BAD1" friday knowledge explain 1
step "13_kx_BAD2" friday knowledge explain 2
step "14_kx_BAD3" friday knowledge explain 3
step "15_kx_BAD4" friday knowledge explain 4
step "16_kx_BAD5" friday knowledge explain 5
# Corrected explains using the REAL first 5 IDs.
KID1='2026-07-14T13:45:34.455036+00:00:project_identity:Aether'
KID2='2026-07-14T13:45:34.455078+00:00:project_architecture:Aether'
KID3='2026-07-14T13:45:34.455181+00:00:project_stack:Aether'
KID4='2026-07-14T13:45:34.455189+00:00:project_identity:Friday'
KID5='2026-07-14T13:45:34.455198+00:00:project_architecture:Friday'
step "12b_kx1" friday knowledge explain "$KID1"
step "13b_kx2" friday knowledge explain "$KID2"
step "14b_kx3" friday knowledge explain "$KID3"
step "15b_kx4" friday knowledge explain "$KID4"
step "16b_kx5" friday knowledge explain "$KID5"

# 17-? ask batches
ask_step() {
  local n="$1"; shift
  # safe tag from question
  local tag
  tag=$(printf "ask_%02d" "$n")
  step "$tag" friday ask "$*"
}

ask_step 01 "What engineering knowledge have you accumulated?"
ask_step 02 "What stable engineering knowledge do you have?"
ask_step 03 "What have you learned about my engineering?"
ask_step 04 "What do you know about my projects now?"
ask_step 05 "What long-term engineering trends have you observed?"
ask_step 06 "What recurring engineering habits have you learned?"
ask_step 07 "Which technologies am I consistently investing in?"
ask_step 08 "How has my engineering direction evolved?"
ask_step 09 "What project relationships have become stronger?"
ask_step 10 "Which knowledge is weakly supported?"
ask_step 11 "What am I working on?"
ask_step 12 "What have I been working on?"
ask_step 13 "What have I been building?"
ask_step 14 "What do you know about what I'm building?"
ask_step 15 "What engineering knowledge do you have?"
ask_step 16 "How has my engineering changed?"
ask_step 17 "How have my interests evolved?"
ask_step 18 "Which technologies are becoming more important?"
ask_step 19 "Which technologies are becoming less important?"
ask_step 20 "What trends are strengthening?"
ask_step 21 "What trends are fading?"
ask_step 22 "What has remained stable?"
ask_step 23 "Which projects reinforce each other?"
ask_step 24 "Which projects depend on each other?"
ask_step 25 "Which projects influence each other?"
ask_step 26 "Which project has become infrastructure?"
ask_step 27 "Which projects are converging?"
ask_step 28 "Which projects are diverging?"
ask_step 29 "What engineering habits have you learned?"
ask_step 30 "What engineering patterns repeat?"
ask_step 31 "What do I consistently do?"
ask_step 32 "What workflow keeps repeating?"
ask_step 33 "What bottlenecks have become recurring?"
ask_step 34 "What engineering strengths keep appearing?"
ask_step 35 "What engineering belief have I abandoned?"
ask_step 36 "What mistake do I keep making?"
ask_step 37 "What am I avoiding?"
ask_step 38 "What surprised you?"
ask_step 39 "What changed my mind?"
ask_step 40 "What did I learn this month?"
ask_step 41 "What engineering philosophy do I follow?"
ask_step 42 "Who am I becoming as an engineer?"

# chat (interactive) — feed via stdin
CHAT_IN="$LOGDIR/chat_in.txt"
cat > "$CHAT_IN" <<'EOF'
What engineering knowledge do you have about me?
How confident are you?
Why?
Which evidence supports that?
What knowledge is newest?
What knowledge is oldest?
What changed recently?
Explain further.
EOF
step "43_chat" bash -c "friday chat < '$CHAT_IN'"

ask_step 44 "Explain Friday"
ask_step 45 "Explain Friday V3"
ask_step 46 "Compare Friday and Friday V3"
ask_step 47 "Which project should I continue?"
ask_step 48 "What should I work on today?"
ask_step 49 "Where is my engineering effort going?"
ask_step 50 "What kind of engineer do I seem to be?"
ask_step 51 "Tell me something I haven't noticed."
ask_step 52 "Which project should become a platform?"
ask_step 53 "Which projects should eventually merge?"

echo
echo "=== SLOWEST STEPS (dur >= 5s) ==="
awk -F'\t' 'NR>0 && $3+0>=5 {print $3"\t"$1"\t"$4}' "$SUMMARY" | sort -rn | head -30
echo
echo "=== ANY NONZERO EXIT CODES ==="
awk -F'\t' '$2!=0 {print $2"\t"$1"\t"$4}' "$SUMMARY"
echo
echo "SUMMARY TSV: $SUMMARY"
