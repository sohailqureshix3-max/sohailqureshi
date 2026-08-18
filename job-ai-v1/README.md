# Job AI V1 — Find, Score, Pitch

This is Sohail Qureshi's automated job-discovery and outreach-preparation engine.

## What V1 does

- Searches the web for current UAE and Saudi Arabia leadership roles.
- Targets General Manager - Operations, Head of Operations, Fleet/Transport/Mobility leadership and related roles.
- Scores every opportunity against Sohail's real profile using a 100-point weighted model.
- Rejects weak or junior matches.
- Generates a tailored email subject, email pitch and short LinkedIn pitch for each qualifying job.
- Writes the latest results to `output/latest.json` and `output/latest.md`.
- Publishes the results through the GitHub Pages dashboard at `/job-ai-v1/`.
- Runs automatically every day at 04:00 UTC (08:00 UAE), and can also be triggered manually from GitHub Actions.

## One-time activation

1. Create an OpenAI API key in the OpenAI Platform.
2. In this GitHub repository go to **Settings → Secrets and variables → Actions**.
3. Click **New repository secret**.
4. Name it exactly: `OPENAI_API_KEY`
5. Paste the API key as the secret value and save it.
6. Go to **Actions → Job AI V1 → Run workflow** to run the first search immediately.

Never put the API key in `profile.json`, `run.py`, the website, or any public repository file.

## Candidate data

Edit `profile.json` to change target roles, markets, scoring thresholds or candidate facts. The system is deliberately instructed not to invent achievements or numbers.

## Fit scoring

- Operations leadership: 25
- Fleet / transport relevance: 20
- Team scale: 15
- Commercial / finance controls: 15
- Compliance: 10
- Systems / data: 10
- GCC relevance: 5

Default qualifying threshold: **70/100**.

## V1 boundary

V1 finds, scores and prepares pitches. It does **not** send applications or unsolicited messages automatically. Outbound sending belongs in V2 after an approval step is added.
