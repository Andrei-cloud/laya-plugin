"""Deployed thresholds — the ONE place any rail/threshold is written (DRY;
fleet doctrine: "thresholds must not drift"). Each constant cites its
section of .hermes/plans/2026-09-25-jev-parity-api-spec-v2.md so a change is
always a deliberate, auditable re-read of the contract.

These are Laya's OWN re-implementations of published policy numbers (D1:
native rewrite; the numbers are the spec, not their code).
"""
import re

# ---- routing rails (spec §3 "Routing policy") ------------------------------
MIN_CONFIDENCE = 0.6              # confidence < this == "unsure"
HARD_NEEDS_PROB = 0.6             # P(level >= 2) >= this -> hard tier
SIMPLE_NEEDS_PROB = 0.7           # P(level 0) >= this ...
SIMPLE_NEEDS_CONF = 0.85          # ... AND conf >= this -> simple
HARD_RISK_STAKES_FLOOR = 0.4      # risk words OR stakes > this floor simple->medium
HARD_TIER_STAKES = 0.85           # stakes > this ...
HARD_TIER_P_HARD = 0.35           # ... AND p_hard >= this -> hard
SPECIALTY_MIN_KIND_CONF = 0.5     # specialty only if kind-conf >= this
STICKY_CONTEXT_TOKENS = 32_000    # above this, never downgrade the model

# hard-risk vocabulary (spec §3 _HARD_RISK): words that force caution
HARD_RISK_WORDS = (
    "production", "prod", "delete", "drop table", "truncate", "format",
    "force push", "rm -rf", "sudo", "chmod -R", "chmod 777", "kill -9",
    "terraform destroy", "migration", "release", "deploy", "pay", "wire",
    "invoice", "contract", "credentials", "secret", "customer", "pii",
    "irreversible", "overwrite", "wipe",
)

# route rubric / vocabularies (spec §2 route row)
DIFFICULTY_LEVELS = ["Trivial", "Moderate", "Hard", "Expert"]
KINDS = ["coding", "writing", "research", "general"]

# ---- rerank / context filter (spec §2 rerank row) --------------------------
RERANK_BATCH = 60                 # passages per request (their cap)
RERANK_MAX_TOTAL = 480            # hard cap; overflow -> truncated:true
RERANK_CHARS_PER_PASSAGE = 900
REL_THRESHOLD = 0.5               # rel noul entering context
INJ_THRESHOLD = 0.5               # inj noul (injection probability)

# ---- compaction (spec §2 compact-select row) -------------------------------
COMPACT_BATCH_TURNS = 40
COMPACT_BATCH_CHARS = 40_000
COMPACT_DROP_MIN_CONF = 0.7       # drop needs conf >= this; default summarize
COMPACT_KEEP_LAST = 6             # tail always kept (+ system always keep)

# ---- skill pick (spec §2 pick-skill row + §5 stage-2 eval) -----------------
SKILL_BATCH = 120                 # stage-1 skills per choice question
SKILL_BATCH_CAP = 960             # per request
SHORTLIST_FLOOR = 0.02            # finalists carry prob >= this
STAGE2_MIN = 0.5                  # needs_skill AND per-finalist noul >= this
ACK_GATE_WORDS = 6                # <= this words -> local trivial ack answers free

# ---- next_action / choose (spec §2 choose row + §5 floor eval) -------------
CHOOSE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
CHOOSE_MIN_CANDIDATES = 2
CHOOSE_MAX_CANDIDATES = 32        # must include the fail-safe ids below
CHOOSE_FAILSAFE_IDS = ("reobserve", "abstain")
CHOOSE_FLOOR_DEFAULT = 0.65       # below it the trap slips 1-in-5; above it stands
CHOOSE_FLOOR_CLAMP = (0.60, 0.95)
CHOOSE_GOAL_MAX_CHARS = 2000
CHOOSE_REGIONS_MAX_CHARS = 100
CHOOSE_HISTORY_MAX = 16

# ---- supervise (spec §3 supervise thresholds) -------------------------------
SUPERVISE_DONE = 0.8
SUPERVISE_NEEDS_INPUT = 0.7
SUPERVISE_BLOCKED = 0.7           # strong reading ...
SUPERVISE_BLOCKED_CONF = 0.6      # ... AND conf >= this
SUPERVISE_ALERT_AFTER = 2         # streak before alerting
SUPERVISE_NO_OUTPUT_S = 180       # FACT that overrides the model's disagreement
SUPERVISE_ACTIONS = ["keep_waiting", "answer_question", "nudge",
                     "escalate", "collect"]

# ---- triage / mail (spec §2 triage + mail rows) -----------------------------
URGENCY_LEVELS = ["none", "later", "today", "right-now", "deadline"]  # lowest first
TRIAGE_KINDS = ["customer-problem", "question", "info", "promotion",
                "sales", "noise"]
MAIL_LANES = ["needs_reply", "updates", "promotional", "sales", "spam"]

# ---- privacy before send (spec §4) -----------------------------------------
ASK_CHARS = 2500                  # ask payload cap
ASK_CHARS_HEADROOM = 50
REDACT_PATTERNS = {               # redact before ANY send / log
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "phone": re.compile(r"(?<!\d)(?:\+?\d[\d ()-]{7,18}\d)(?!\d)"),
    "bearer": re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    "keyval": re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\b\s*[:=]\s*\S+"),
    "hex": re.compile(r"\b[0-9a-fA-F]{24,}\b"),
}

# ---- ladder (spec §2 ladder row) -------------------------------------------
LADDER_PROBE_CACHE_S = 900        # probe executables cached this long

# ---- decision log kinds (spec §4; the dual-written log's vocabulary) -------
LOG_KINDS = ("route", "route_effective", "skill", "merged", "skill_unreachable")
