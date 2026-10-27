# LEARNINGS.md  -  {AGENT_NAME}'s Distilled Wisdom

**This is the baseline. Your agent loads this every session.**

How the loop works:
1. Sections 1-7 are the **distilled core**  -  re-summarized nightly by the self-eval run, never grow out of control
2. Section 8 is the **append window**  -  the agent, bots, and helpers dump new entries here during the day
3. The nightly self-eval reads Section 8, distills it into Sections 1-7, archives raw to `learnings_archive/<DATE>.md`, writes the file back atomically.
4. Raw appends are never lost: they live in the dated archives.

**Append rules (CRITICAL):**
- ALWAYS APPEND to Section 8 ("Today's Append Window"). NEVER overwrite Sections 1-7.
- NEVER write the file with a `write` tool  -  only `append`. A wipe here costs everything.
- Format new entries: `### [TIMESTAMP] | [TYPE] | [SOURCE]` followed by a 1-3 line body.
- Types: `trade_lesson | pattern_finding | bot_finding | rule_update | autopsy | overseer_followup | sub_agent_insight | error_pattern | alignment | self_eval`

Last full re-distillation: never

---

## 1. IDENTITY & OPERATING CONTEXT

_Who the agent is, who it works for ({USER}), which lanes it runs, the wallet/accounts it acts through, and the end goal. Stable; only changes when a lane, wallet, or brain changes._

---

## 2. CRITICAL RULES (NEVER VIOLATE)

_Hard rules only {USER} adds or retires (capital floors, size limits, never-do lines). The nightly run never edits this section; it proposes candidates in Section 7. Keep it substantial (the run's checks expect real mass here, 25+ bullets once mature)._

---

## 3. TRADING LESSONS BY LANE

_Distilled lessons per lane. One master bullet per lesson family; add cross-lane notes instead of duplicating._

### 3.1 PERPS LANE

_Entry/exit lessons from SOL, BTC and ETH perps: signals, leverage, update_params tuning, closes and passes._

### 3.2 LANE 2

_Rename or remove to match your lanes._

### 3.3 LANE 3

_Rename or remove to match your lanes._

---

## 4. PATTERN LIBRARY (rolling status)

_Named setups with recognition criteria, sample size, and win rate. Reclassify with old POV / new POV / date / why, never a silent overwrite._

---

## 5. BOT/TOOLING LESSONS

_What the bots and tools actually do vs what they were believed to do; doctrine changes when bot versions move._

---

## 6. SUB-AGENT INSIGHTS

_Wisdom from helper agents, kept even after they are retired. Stable section._

---

## 7. OPEN AUDIT ITEMS (rolling  -  review weekly)

_P1/P2/P3 open items, contradictions tagged [CONTESTED], doctrine drift flags, and `P2 - SECTION 2 CANDIDATE` items the agent promotes (and logs) next run. Resolved items roll off via a "Recently Resolved" bullet._

---

## 8. TODAY'S APPEND WINDOW

Anyone who appends: bots via a marker-aware writer, the agent itself, nightly review follow-ups.
Format:
```
### [ISO_TIMESTAMP] | [TYPE] | [SOURCE]
- lesson body
```
Types: trade_lesson | pattern_finding | bot_finding | rule_update | autopsy | overseer_followup | sub_agent_insight | error_pattern | alignment | self_eval
Sources: agent | perps_bot | overseer | owner | ...

---

<!-- APPEND BELOW THIS LINE - DO NOT EDIT ANYTHING ABOVE -->
<!-- end of append window -->
