# Adding a model

A results pull request adds one folder under `runs/` and changes nothing else. The README's leaderboard updates itself after the merge, so don't edit it.

## Run the bench

```sh
export OPENROUTER_API_KEY=sk-or-...
./emojibench.py models deepseek          # search model ids and prices
./emojibench.py bench -m openrouter:deepseek/deepseek-v4-pro
./emojibench.py check                    # what CI will run
./emojibench.py report --bench           # the leaderboard with your model in it
```

`bench` climbs the ladder on all 20 articles in `articles/set1.json`, 4 articles at a time (`-j` changes that). It writes a new folder in `runs/` holding every game as JSON plus a readable transcript (`.md`).

- **Keep the defaults:** $1 per article, 5 turns per rung, the 20-article set. `check` fails a run played with other settings.
- **Use the paid model id.** A `:free` variant reports a cost of $0, so its $1 never runs out, and `check` rejects it.
- **Reasoning effort** is the provider's default unless you pass `--effort low|medium|high`. A run with an effort level is a separate leaderboard entry, such as "Gemini 3.1 Pro Preview (high)".
- **The judge** is Claude Haiku 5.5. It runs through Claude Code if you have it installed, otherwise through OpenRouter as `anthropic/claude-haiku-5.5`. Its cost, about a tenth of a percent of a game, counts toward the $1 like everything else.
- **One run per model** (and effort). To improve a run, continue it rather than starting a second one.

## If it stops partway

A run can stop partway if credits run out, you hit a rate limit, or the model refuses. Every finished game is already saved, and a rung that hit an error is saved as `error-NN-rRR.json`. Resume with the same model:

```sh
./emojibench.py bench -m openrouter:<id> --continue runs/<the run's folder>
```

Saved rungs aren't played again. If one rung keeps failing with the same error, such as a safety refusal, open the pull request anyway and say so. The maintainers will decide how to score it.

## What CI checks

`./emojibench.py check` re-derives everything it can from the saved games:

- Every article in the set was played, and its text is unchanged.
- Every message the guesser saw is exactly what the filter makes of the explainer's text, with that rung's banned emoji removed.
- Each rung's banned list is every emoji the earlier rungs used.
- The solved turn matches the judge's verdicts.
- No ladder goes on past a rung that doesn't count.
- Every call has a cost.
- No ladder was left unfinished with money still to spend.
- No model has two runs.

It can't prove the games came from the model they name. Every call's token counts are recorded, along with the provider that served it. A maintainer may replay a few articles to compare.

## Changing the harness

The prompts, the rules, and the filter decide the scores. Every game records a fingerprint of them (`harness` in each game file). After any change to them, new results can't be compared with the old ones, so open an issue first. Code changes go in their own pull request, separate from results.
