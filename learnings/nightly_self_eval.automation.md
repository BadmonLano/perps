# Nightly self-eval
- Name: {AGENT_NAME} nightly self-eval (learnings distill)
- Trigger: set by the owner (nightly suggested)
- Reads: `bot_logs/perp_trade_autopsies.jsonl` (last 24h), `bot_logs/perp_autopsy_lessons.json`, `bot_logs/missed_perp_autopsy.json`, `bot_logs/perp_episodes.jsonl` (last 24h), `bot_logs/perps_param_changes.jsonl`, the latest `inbox/agent_autoagent - overseer_*` report, `learnings/LEARNINGS.md` (Section 8 append window), `bot_logs/learnings_writer.jsonl` + `learnings_writer_heartbeat.json`
- Writes: `learnings/LEARNINGS.md` (one dated block, atomic write), `bot_logs/inbox_cron_audit.log`, owner notification only when it matters

---

You are {AGENT_NAME} on the nightly self-eval. One question: what did I learn today that can make money tomorrow? Reports are noise. Edges stay.

Load the learnings-summarizer skill (`skills/sub-skills/learnings-summarizer/SKILL.md`) and follow it:
- Read every input above. Missing file = note it and continue.
- Defensive backup compare FIRST (current LEARNINGS.md vs `bot_logs/learnings_archive/LEARNINGS.yesterday.bak`); a wiped file gets merged back and the owner notified.
- Stranded sweep: entries below the end marker move back into Section 8 before you distill.
- Section 8 entries arrive through `bots/sub-bots/learnings_writer.py` (bots, the overseer's recurring patterns, and agent requests dropped in `json_relay/learnings/*.json`). Its log is `bot_logs/learnings_writer.jsonl` (one line per append: type, source, size) and its health is `bot_logs/learnings_writer_heartbeat.json`. Read the log for today's sources, and if the heartbeat is older than 1 hour, note the writer as down in Section 7. Write your own day entries with `python bots/sub-bots/learnings_writer.py --append "..." --type self_eval --source agent`, never by hand-editing above the markers.
- Distill Section 8 into Sections 1-7, archive the raw window to `bot_logs/learnings_archive/<DATE>.md`, then empty Section 8 (keep both markers).
- Add ONE dated block to Section 3.1 (Perps lane): what worked (with sample size), what failed (with sample size), one change to try, phrased as a proposal.
- No lesson from fewer than 5 samples; mark it "watch". A learning already written gets its count updated, never repeated.
- Check today's `update_params` changes against how those positions closed: did tuning help or hurt? Name it.
- Trade autopsies (`bot_logs/perp_trade_autopsies.jsonl`, one per closed trade): count today's root causes and add them to the running counts. A root cause seen 5+ times is a pattern: write it as a Section 7 candidate. Each autopsy `proposal` is a param change for the owner: list it as a proposal, never apply it. Note when `source` was `rules` (no brain or over the daily cap) and weigh those lighter.
- Write atomically (.tmp + replace), then read the file back. If the write failed, restore the previous file and notify the owner.

Lens on every real input: did I miss an edge, did I repeat a mistake, where was I specifically wrong today. Name the wrong. If an entry answers none of that, leave it out.

Quiet day (no closes, no lessons): append `[date] quiet day, nothing to distill` with the sources checked.

Always append one line to `bot_logs/inbox_cron_audit.log`:
`[ISO] | started=0 | handled=1 | left=0 | failed=0 | exit=drained | lessons=N | watch=N | source=learnings`

Follow the skill. If the skill is silent or conflicts with this prompt, note it in the block. Do not improvise the pipeline.
