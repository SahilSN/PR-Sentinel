# TODO

## Must have (MVP)
- [x] Shared contracts (`schemas.py`) + fixtures
- [x] Offline fixture run with fake model
- [x] Claude tool-use loop with limits and stop reasons
- [x] GitHub read/write wrapper, single upserted bot comment
- [x] GitHub Actions workflow
- [x] Re-review: resolved vs. still-open findings across pushes
- [ ] Live check: real Claude call on a fixture (`--fixture tests/fixtures/buggy_pr.json --dry-run` with `ANTHROPIC_API_KEY`)
- [ ] Live check: real PR dry-run and publish against a demo repo
- [ ] Live check: Actions run on open + push in the demo repo

## Nice to have
- [ ] P2: summarize failing check runs
- [ ] P2: CLI command to print review history
- [ ] Dashboard reading `artifacts/run_result.json` / `actions.jsonl`
- [ ] Optional price config for showing an approximate cost

## Needs debugging
- [ ] Not yet verified against the live API: structured output (`output_config.format`) combined with tools and the `fallbacks: "default"` beta. If the API rejects it, set `ANTHROPIC_FALLBACKS=off` first.

## Before submission
- [ ] README filled in
- [ ] Demo video recorded
- [ ] Slides / pitch ready
- [ ] Submission form done
