#!/usr/bin/env python3
"""EMOJIBENCH: can a model explain a random Wikipedia article to itself using only emojis?

An EXPLAINER instance gets a random Wikipedia article and may only send emojis
(enforced by a filter, not just the prompt). A GUESSER instance of the same model
writes free text and makes guesses. Score = guesser turns until a correct guess.

A model is picked by name, and the name picks the backend:
  opus, sonnet, claude-opus-5-5, ...  headless `claude -p` (your Claude Code subscription), with
                                      --safe-mode so no plugins/hooks/CLAUDE.md leak into the game
  openrouter:<id>                     any model on OpenRouter, e.g. openrouter:deepseek/deepseek-v4-pro
                                      (needs OPENROUTER_API_KEY; `models` lists ids and prices)

Usage:
  ./emojibench.py bench -m openrouter:<id>             # EMOJIBENCH @ $1 on the 20-article set
  ./emojibench.py report                               # leaderboard over runs/
  ./emojibench.py check                                # validate the runs, as CI does for a PR
  ./emojibench.py models qwen                          # search OpenRouter model ids and prices
  ./emojibench.py run -m sonnet                        # normal games only (no ladder)
  ./emojibench.py fetch -n 20 -o articles/set2.json    # freeze a new set of random articles
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import difflib
import hashlib
import json
import math
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EMOJI_TEST = ROOT / "data" / "emoji-test.txt"
SESSIONS_DIR = ROOT / ".sessions"  # cwd for claude subprocesses, keeps their history out of yours
RUNS_DIR = ROOT / "runs"
CLAUDE = shutil.which("claude") or "claude"
# Any OpenAI-compatible endpoint works here, e.g. a local vLLM server; costs then come from usage.cost if it sends one.
OPENROUTER_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
USER_AGENT = "emojibench/0.1"

MAX_TURNS = 30
JUDGE_MODEL = "haiku"
JUDGE_OPENROUTER = "openrouter:anthropic/claude-haiku-5.5"  # the same judge, for machines without Claude Code

# ---------------------------------------------------------------------------
# Emoji filter
# ---------------------------------------------------------------------------

# Anything that renders letters/digits/words is illegal: no spelling allowed.
BANNED_SUBGROUPS = {"alphanum", "keycap"}  # 🅰️ 🆗 ℹ️ Ⓜ️ 🔤 🈁 ... and 1️⃣ #️⃣ 🔟
BANNED_GROUPS = {"Component"}  # bare skin tones / hair pieces
BANNED_EXTRA = {"©", "®", "™", "🔙", "🔚", "🔛", "🔜", "🔝", "🏧", "🚾", "🔞"}

VS16 = "️"


def load_emoji_table():
    """Map FE0F-stripped sequence -> canonical fully-qualified emoji, plus key -> emoji-test group."""
    table, groups = {}, {}
    group = subgroup = None
    for line in EMOJI_TEST.read_text(encoding="utf-8").splitlines():
        if line.startswith("# group:"):
            group = line.split(":", 1)[1].strip()
        elif line.startswith("# subgroup:"):
            subgroup = line.split(":", 1)[1].strip()
        elif line and not line.startswith("#"):
            codes, rest = line.split(";", 1)
            if "fully-qualified" not in rest:
                continue
            if group in BANNED_GROUPS or subgroup in BANNED_SUBGROUPS:
                continue
            emoji = "".join(chr(int(c, 16)) for c in codes.split())
            key = emoji.replace(VS16, "")
            if key in BANNED_EXTRA:
                continue
            table[key] = emoji
            groups[key] = group
    return table, groups


EMOJI, EMOJI_GROUP = load_emoji_table()
MAX_EMOJI_LEN = max(len(k) for k in EMOJI)
SKIN_TONES = {chr(c) for c in range(0x1F3FB, 0x1F400)}


def base_of(key):
    """Emoji key without skin-tone modifiers, so 👍🏽 travels with 👍 on a keyboard."""
    return "".join(ch for ch in key if ch not in SKIN_TONES)


def keyboard(title, fraction, seed):
    """A random subset of base emoji for one article, the same for every model that plays it.
    Returns (allowed keys incl. skin-tone variants, {group: [emoji shown on the keyboard]})."""
    bases = list(dict.fromkeys(base_of(k) for k in EMOJI))
    rng = random.Random(f"{seed}|{fraction}|{title}")
    chosen = set(rng.sample(bases, round(len(bases) * fraction)))
    allowed = {k for k in EMOJI if base_of(k) in chosen}
    shown = {}
    for b in bases:  # emoji-test order, grouped like a phone keyboard
        if b in chosen:
            key = b if b in EMOJI else next(k for k in EMOJI if base_of(k) == b)
            shown.setdefault(EMOJI_GROUP[key], []).append(EMOJI[key])
    return allowed, shown


def filter_emojis(text, allowed=None, rejected=None):
    """Keep only whitelisted emojis (and, with a keyboard, only `allowed` ones) plus whitespace.
    Returns (kept, n_dropped). Off-keyboard emojis are appended to `rejected` if given."""
    s = text.replace(VS16, "")
    out, dropped, i = [], 0, 0
    while i < len(s):
        for n in range(min(MAX_EMOJI_LEN, len(s) - i), 0, -1):
            key = s[i : i + n]
            if key in EMOJI:
                if allowed is None or key in allowed:
                    out.append(EMOJI[key])
                else:
                    dropped += 1
                    if rejected is not None:
                        rejected.append(EMOJI[key])
                i += n
                break
        else:
            ch = s[i]
            if ch == "\n":
                out.append("\n")
            elif ch.isspace():
                out.append(" ")
            else:
                dropped += 1
            i += 1
    kept = re.sub(r" *\n[\s]*", "\n", re.sub(r"[ \t]+", " ", "".join(out))).strip()
    return kept, dropped


# ---------------------------------------------------------------------------
# Wikipedia
# ---------------------------------------------------------------------------


def random_article():
    req = urllib.request.Request(
        "https://en.wikipedia.org/api/rest_v1/page/random/summary",
        headers={"User-Agent": USER_AGENT},
    )
    while True:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        if d.get("type") == "standard":  # skip disambiguation pages
            return {
                "title": d["title"],
                "description": d.get("description", ""),
                "extract": d.get("extract", ""),
                "url": d.get("content_urls", {}).get("desktop", {}).get("page", ""),
            }


# ---------------------------------------------------------------------------
# Claude calls
# ---------------------------------------------------------------------------


class ModelError(RuntimeError):
    """A refusal or an API failure that retrying didn't fix. Ends the game; the transcript is kept."""


ClaudeError = ModelError


def claude(model, prompt, *, session_id=None, resume=False, system_prompt=None, effort=None):
    """One headless turn. Returns dict(text, usage...). Retries transient failures.
    Token counts are for this turn, but cost_usd is the session's running total (see Chat.send)."""
    cmd = [CLAUDE, "-p", "--safe-mode", "--tools", "", "--strict-mcp-config",
           "--disable-slash-commands", "--model", model, "--output-format", "json"]
    if resume:
        cmd += ["--resume", session_id]
    elif session_id:
        cmd += ["--session-id", session_id]
    else:
        cmd += ["--no-session-persistence"]
    if system_prompt and not resume:
        cmd += ["--system-prompt", system_prompt]
    if effort:
        cmd += ["--effort", effort]

    SESSIONS_DIR.mkdir(exist_ok=True)
    last_err = None
    for attempt in range(4):
        if attempt and session_id and not resume:
            # A failed first turn may have created the session already: start a fresh one.
            session_id = str(uuid.uuid4())
            cmd[cmd.index("--session-id") + 1] = session_id
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                               cwd=SESSIONS_DIR, timeout=900)
            d = json.loads(p.stdout)
            if d.get("is_error"):
                raise ClaudeError(d.get("result") or p.stderr[-500:])
            u = d.get("usage", {})
            return {
                "text": d.get("result", ""),
                "session_id": session_id,
                "model": next(iter(d.get("modelUsage", {})), model),
                "input_tokens": u.get("input_tokens", 0),
                "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
                "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0),
                "thinking_tokens": u.get("output_tokens_details", {}).get("thinking_tokens", 0),
                "cost_usd": d.get("total_cost_usd", 0.0),
                "duration_ms": d.get("duration_ms", 0),
            }
        except (json.JSONDecodeError, subprocess.TimeoutExpired, ClaudeError) as e:
            last_err = e
            time.sleep(10 * (attempt + 1))
    raise ClaudeError(f"claude failed after retries: {last_err}")


_or_models = None


def openrouter_models():
    """OpenRouter's public model list, {id: model}. Cached for the run."""
    global _or_models
    if _or_models is None:
        req = urllib.request.Request(f"{OPENROUTER_URL}/models", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r:
            _or_models = {m["id"]: m for m in json.loads(r.read())["data"]}
    return _or_models


def openrouter_price(model, prompt_tokens, cached, completion_tokens):
    """List price from the model list, for responses that don't report usage.cost."""
    try:
        p = openrouter_models()[model]["pricing"]
    except Exception:
        return 0.0
    f = lambda k, fallback="0": float(p.get(k) or fallback)  # noqa: E731
    return ((prompt_tokens - cached) * f("prompt") + cached * f("input_cache_read", p.get("prompt"))
            + completion_tokens * f("completion"))


def openrouter(model, messages, effort=None):
    """One chat completion through OpenRouter. Same return shape as claude()."""
    body = {"model": model, "messages": messages, "usage": {"include": True}}
    if effort:
        body["reasoning"] = {"effort": {"xhigh": "high", "max": "high"}.get(effort, effort)}
    req = urllib.request.Request(
        f"{OPENROUTER_URL}/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}",
                 "Content-Type": "application/json", "X-Title": "EMOJIBENCH", "User-Agent": USER_AGENT})
    last_err = None
    for attempt in range(4):
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                d = json.loads(r.read())
            if d.get("error"):
                raise ModelError(json.dumps(d["error"], ensure_ascii=False)[:500])
            choice = d["choices"][0]
            if choice.get("finish_reason") == "error":
                raise ModelError(json.dumps(choice, ensure_ascii=False)[:500])
            # Reasoning models sometimes inline their thinking; it isn't part of the message.
            text = re.sub(r"<think>.*?</think>", "", choice["message"].get("content") or "", flags=re.S).strip()
            u = d.get("usage") or {}
            prompt, completion = u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            cost = u.get("cost")
            return {
                "text": text,
                "session_id": None,
                "model": model,
                "served_by": d.get("provider"),
                "input_tokens": prompt - cached,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cached,
                "output_tokens": completion,
                "thinking_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0,
                "cost_usd": cost if cost is not None else openrouter_price(model, prompt, cached, completion),
                "duration_ms": round((time.time() - started) * 1000),
            }
        except urllib.error.HTTPError as e:
            msg = f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}"
            if e.code in (400, 401, 402, 403, 404):  # bad request, key, credits, moderation, unknown model
                raise ModelError(msg) from None
            last_err = msg
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, ModelError) as e:
            last_err = e
        time.sleep(10 * (attempt + 1))
    raise ModelError(f"openrouter failed after retries: {last_err}")


def backend(spec):
    """("openrouter", id) for openrouter:<id> (or:<id> for short), else ("claude", spec)."""
    for prefix in ("openrouter:", "or:"):
        if spec.startswith(prefix):
            return "openrouter", spec[len(prefix):]
    return "claude", spec


def safe_name(spec):
    """A model spec as a directory-name piece: openrouter:deepseek/deepseek-v4-pro → openrouter-deepseek-deepseek-v4-pro."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", spec).strip("-")


def check_model(spec):
    """Fail fast, before any game starts, on a missing key or a misspelled model id."""
    kind, model = backend(spec)
    if kind == "claude":
        if not shutil.which("claude"):
            sys.exit(f"{spec}: Claude models run through Claude Code, and `claude` isn't on PATH. "
                     f"For other models use openrouter:<id>.")
        return
    if not os.environ.get("OPENROUTER_API_KEY"):
        sys.exit(f"{spec}: set OPENROUTER_API_KEY (https://openrouter.ai/keys)")
    try:
        ids = openrouter_models()
    except Exception as e:  # a custom OPENROUTER_BASE_URL may not have a model list
        log(f"couldn't fetch the model list to check {model!r} ({e}); trying it anyway")
        return
    if model not in ids:
        near = difflib.get_close_matches(model, ids, n=5, cutoff=0.5)
        sys.exit(f"{model!r} isn't an OpenRouter model id." + (f" Did you mean: {', '.join(near)}?" if near else "")
                 + " See ./emojibench.py models <search>")


class Chat:
    """One side of a game: a conversation that remembers what was said. Claude Code keeps the history in
    a session; for OpenRouter the harness keeps it and resends it each turn."""

    def __init__(self, spec, system=None, effort=None):
        self.kind, self.model = backend(spec)
        self.system, self.effort = system, effort
        self.session_id = str(uuid.uuid4()) if self.kind == "claude" else None
        self.messages = [{"role": "system", "content": system}] if system else []
        self.started = False
        self.billed = 0.0  # Claude Code's running cost total for the session so far

    def send(self, prompt):
        if self.kind == "claude":
            r = claude(self.model, prompt, session_id=self.session_id, resume=self.started,
                       system_prompt=self.system, effort=self.effort)
            if r["session_id"] != self.session_id:  # a failed first turn was retried in a fresh session
                self.billed = 0.0
            self.session_id = r["session_id"]
            # total_cost_usd covers the whole resumed session, not just this turn: keep the difference.
            r["cost_usd"], self.billed = r["cost_usd"] - self.billed, r["cost_usd"]
        else:
            r = openrouter(self.model, self.messages + [{"role": "user", "content": prompt}], self.effort)
            self.messages += [{"role": "user", "content": prompt}, {"role": "assistant", "content": r["text"]}]
        self.started = True
        return r


def ask(spec, system, prompt):
    """A one-off question with no history (the judge)."""
    kind, model = backend(spec)
    if kind == "claude":
        return claude(model, prompt, system_prompt=system)
    return openrouter(model, [{"role": "system", "content": system}, {"role": "user", "content": prompt}])


# ---------------------------------------------------------------------------
# Game
# ---------------------------------------------------------------------------

EXPLAINER_TEMPLATE = """You are playing EMOJIBENCH, a cooperative communication game.

You are the EXPLAINER. Your partner, the GUESSER, is {partner}. You know a secret Wikipedia article, picked with Special:Random, so it is probably obscure. Your goal: get the guesser to name that exact article in as few turns as possible.

Rules:
- Put the message for the guesser inside <msg>...</msg>. Anything outside the tags is your private scratchpad; the guesser never sees it.
- Inside <msg>, a filter deletes everything that is not a standard emoji: letters, digits, punctuation, ASCII art, and emojis that contain letters or numbers (🅰️ 🆗 ℹ️ Ⓜ️ 🆒 1️⃣ #️⃣ 🔟 🔤 ™️ 🔝 etc.). Spaces and newlines survive and can group emojis. Flags are allowed.{kb_rule}
- The guesser writes normal text: questions, hypotheses, guesses. Answer their questions with emojis only (👍 👎 🤷 ↗️ 🔥 etc.).
- When the guesser makes a guess, the harness tells you both whether it was correct.
- The game ends when the guesser names the article, or after {max_turns} turns.

{closing}"""

GUESSER_TEMPLATE = """You are playing EMOJIBENCH, a cooperative communication game.

You are the GUESSER. Your partner, the EXPLAINER, is {partner}. The explainer knows a secret English Wikipedia article, picked with Special:Random, so it is probably obscure (a small village, a minor athlete, an obscure species, a forgotten film, ...). The explainer can only send emojis; a filter enforces it.{kb_rule}

Rules:
- You may write normal text: ask questions (yes/no or open), share hypotheses, say what you've understood so far. Your whole message is shown to the explainer.
- To guess, include a line exactly like:
  GUESS: <article title>
  One guess per turn (only the last GUESS line counts). Aim for the exact Wikipedia article title.
- The harness judges each guess and tells you both whether it was correct.
- You have at most {max_turns} turns. Each of your messages is one turn, so be efficient: guess whenever you have a reasonable candidate.

{closing}"""

DISPLAY_NAMES = {"haiku": "Claude Haiku", "sonnet": "Claude Sonnet", "opus": "Claude Opus", "fable": "Claude Fable"}


def display_name(spec):
    """How a model is named to its partner in cross-play."""
    return DISPLAY_NAMES.get(spec, backend(spec)[1])


# Hard-mode rules: (explainer rule, guesser rule). Normal mode adds nothing, so its prompts are unchanged.
HARD_RULES = {
    None: ("", ""),
    "keyboard": (
        "\n- HARD MODE: for this game you only have a random part of the emoji keyboard. It is "
        "listed with the article. Any emoji not on your keyboard is deleted like text, so the "
        "obvious emoji is often missing and you must improvise. Skin-tone variants of listed "
        "emojis are allowed.",
        " HARD MODE: the explainer only has a random part of the emoji keyboard, so the obvious "
        "emoji is often missing and they have to improvise with substitutes."),
    "ban": (
        "\n- HARD MODE: some emojis are banned for this game. They are listed with the article, and "
        "they are probably the ones you would reach for first. A banned emoji is deleted like text, "
        "so you must improvise. Skin-tone variants of banned emojis are banned too.",
        " HARD MODE: the explainer's go-to emojis for this article are banned, so they have to "
        "improvise with substitutes."),
}


def system_prompts(explainer_model, guesser_model, mode=None):
    """Self-play says "same model as you"; cross-play names the partner honestly."""
    ke, kg = HARD_RULES[mode]
    if explainer_model == guesser_model:
        same = "another instance of the exact same AI model as you"
        return (EXPLAINER_TEMPLATE.format(partner=same, max_turns=MAX_TURNS, kb_rule=ke, closing=(
                    "Your partner thinks exactly like you. For each message, ask: "
                    "what emojis would make ME name this exact article?")),
                GUESSER_TEMPLATE.format(partner=same, max_turns=MAX_TURNS, kb_rule=kg,
                                        closing="Your partner thinks exactly like you."))
    e = display_name(explainer_model)
    g = display_name(guesser_model)
    return (EXPLAINER_TEMPLATE.format(partner=f"a different AI model, {g}", max_turns=MAX_TURNS, kb_rule=ke, closing=(
                f"Your partner is {g}, not you. For each message, ask: "
                f"what emojis would make {g} name this exact article?")),
            GUESSER_TEMPLATE.format(partner=f"a different AI model, {e}", max_turns=MAX_TURNS, kb_rule=kg,
                                    closing=f"Your partner is {e}, a different model from you."))

JUDGE_SYSTEM = """You judge a guessing game. Reply with only YES or NO."""
JUDGE_PROMPT = (
    "Target Wikipedia article: {title}\n"
    "Description: {description}\n"
    "Guess: {guess}\n\n"
    "Does the guess name the same subject as the target article? Spelling slips, missing "
    "diacritics, alternative names, and an omitted trailing disambiguator like \"(film)\" are "
    "fine. A broader category, a related topic, or a different entity is NOT a match; "
    "different numbers, oxidation states, years, or editions make it a different entity.")

# Fingerprint of everything that decides a score: prompts, rules, filter. Games record it, so results
# played under different rules can be told apart (`check` flags them).
HARNESS = hashlib.sha256(json.dumps(
    [EXPLAINER_TEMPLATE, GUESSER_TEMPLATE, sorted((str(k), v) for k, v in HARD_RULES.items()), JUDGE_SYSTEM,
     JUDGE_PROMPT, MAX_TURNS, sorted(BANNED_SUBGROUPS), sorted(BANNED_GROUPS), sorted(BANNED_EXTRA)],
    ensure_ascii=False).encode()).hexdigest()[:12]


def normalize_title(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def judge(article, guess, log_usage):
    if normalize_title(guess) == normalize_title(article["title"]):
        return True
    r = ask(JUDGE_MODEL, JUDGE_SYSTEM, JUDGE_PROMPT.format(title=article["title"],
                                                          description=article["description"], guess=guess))
    log_usage("judge", r)
    return r["text"].strip().upper().startswith("YES")


def extract_msg(text):
    msgs = re.findall(r"<msg>(.*?)</msg>", text, flags=re.S)
    return msgs[-1] if msgs else text


def parse_guess(text):
    guesses = re.findall(r"^\s*\**GUESS\**\s*:\s*(.+?)\s*$", text, flags=re.M | re.I)
    return guesses[-1].strip("*`\"' ") if guesses else None


_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


def play(explainer_model, guesser_model, article, tag, effort=None, kb_fraction=None, kb_seed="v1",
         banned=None, stop_after=None, budget_left=None):
    """One game. kb_fraction: random partial keyboard. banned: set of base emoji keys that are off-limits.
    stop_after: end the game unsolved after this many turns (the models still see the normal rules).
    budget_left: end the game once it has cost more than this; such a rung can no longer count."""
    mode = "keyboard" if kb_fraction else "ban" if banned else None
    explainer_system, guesser_system = system_prompts(explainer_model, guesser_model, mode)
    allowed, kb_text, kb_meta = None, "", None
    if banned:
        allowed = {k for k in EMOJI if base_of(k) not in banned}
        shown = [EMOJI[b] if b in EMOJI else EMOJI[next(k for k in EMOJI if base_of(k) == b)]
                 for b in sorted(banned)]
        kb_text = f"\n\nBanned emojis for this game ({len(shown)}): {' '.join(shown)}"
    if kb_fraction:
        allowed, shown = keyboard(article["title"], kb_fraction, kb_seed)
        n = sum(len(v) for v in shown.values())
        kb_text = (f"\n\nYour keyboard for this game ({n} emojis; skin-tone variants of these also work):\n"
                   + "\n".join(f"{grp}: {' '.join(es)}" for grp, es in shown.items()))
        kb_meta = {"fraction": kb_fraction, "seed": kb_seed, "size": n}
    chats = {"explainer": Chat(explainer_model, explainer_system, effort),
             "guesser": Chat(guesser_model, guesser_system, effort)}
    usage = {"explainer": [], "guesser": [], "judge": []}

    def log_usage(role, r):
        usage[role].append({k: v for k, v in r.items() if k not in ("text", "session_id")})

    def turn(role, prompt):
        r = chats[role].send(prompt)
        log_usage(role, r)
        return r["text"]

    turns = []
    exp_prompt = (
        f"Secret article:\nTitle: {article['title']}\nDescription: {article['description']}\n"
        f"Summary: {article['extract']}{kb_text}\n\nTurn 1/{MAX_TURNS}. Send your first message."
    )
    gue_note = ""
    solved_turn = None
    started = time.time()

    error, out_of_budget = None, False
    stage, t = "explainer", 0
    try:
        for t in range(1, (stop_after or MAX_TURNS) + 1):
            stage = "explainer"
            exp_raw = turn("explainer", exp_prompt)
            rejected = []
            delivered, dropped = filter_emojis(extract_msg(exp_raw), allowed, rejected)
            retried = False
            if not delivered:
                retried = True
                exp_raw += "\n\n[RETRY]\n" + turn("explainer",
                    "[harness] Your message contained no valid emojis after filtering and was NOT delivered. "
                    "Send emojis only, inside <msg>...</msg>.")
                rejected = []
                delivered, dropped = filter_emojis(extract_msg(exp_raw.split("[RETRY]")[-1]), allowed, rejected)

            gue_prompt = (gue_note + f"Turn {t}/{MAX_TURNS}. Explainer: {delivered or '(nothing got through)'}").strip()
            stage = "guesser"
            gue_raw = turn("guesser", gue_prompt)
            guess = parse_guess(gue_raw)
            stage = "judge"
            correct = judge(article, guess, log_usage) if guess else None

            turns.append({"turn": t, "explainer_raw": exp_raw, "delivered": delivered,
                          "dropped_chars": dropped, "off_keyboard": rejected, "retried": retried,
                          "guesser": gue_raw, "guess": guess, "correct": correct})
            mark = "" if guess is None else (" ✅" if correct else " ❌")
            log(f"[{tag} t{t:02d}] {delivered.replace(chr(10), ' ')[:60]}  →  {guess or '…'}{mark}")

            if correct:
                solved_turn = t
                break
            if budget_left is not None and sum(c["cost_usd"] for cs in usage.values() for c in cs) > budget_left:
                out_of_budget = True
                break

            verdict = f"[harness] Guess \"{guess}\" is WRONG.\n" if guess else ""
            gue_note = verdict
            delivery_note = ""
            if dropped:
                delivery_note = f"[harness] Non-emoji content was removed. Your last message was delivered as: {delivered}\n"
                if rejected:
                    why = "Banned" if mode == "ban" else "Not on your keyboard"
                    delivery_note += f"[harness] {why}, removed: {' '.join(dict.fromkeys(rejected))}\n"
            exp_prompt = (f"{delivery_note}Guesser: {gue_raw}\n{verdict}\n"
                          f"Turn {t + 1}/{MAX_TURNS}. Your message?")
    except ModelError as e:
        # Refusals and persistent API failures end the game; keep the partial transcript.
        error = {"turn": t, "role": stage, "message": str(e)}
        log(f"[{tag} t{t:02d}] ERROR in {stage}: {e}")

    totals = {}
    for role, calls in usage.items():
        totals[role] = {k: sum(c[k] for c in calls) for k in
                        ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                         "output_tokens", "thinking_tokens", "cost_usd")}
        totals[role]["calls"] = len(calls)
    return {
        "model": explainer_model,
        "model_id": (usage["explainer"] or [{}])[0].get("model", explainer_model),
        "guesser_model": guesser_model,
        "guesser_model_id": (usage["guesser"] or [{}])[0].get("model", guesser_model),
        "effort": effort,
        "article": article,
        "solved": solved_turn is not None,
        "turns_taken": solved_turn if solved_turn else len(turns),
        "solved_turn": solved_turn,
        "error": error,
        "keyboard": kb_meta,
        "stopped_after": (stop_after if stop_after and solved_turn is None and not error and not out_of_budget
                          else None),
        "out_of_budget": out_of_budget,
        "max_turns": MAX_TURNS,
        "seconds": round(time.time() - started, 1),
        "usage": totals,
        "calls": usage,
        "turns": turns,
        "sessions": {role: c.session_id for role, c in chats.items()},
        "harness": HARNESS,
        "judge": JUDGE_MODEL,
    }


def game_markdown(g):
    a = g["article"]
    if g.get("error"):
        e = g["error"]
        status = f"**error** on turn {e['turn']} ({e['role']}): {e['message']}"
    elif g["solved"]:
        status = f"solved in **{g['solved_turn']}** turns"
    else:
        status = f"**not solved** in {g['max_turns']} turns"
    lines = [f"# {a['title']}", "", f"*{a['description']}*, {a['url']}", "",
             *([f"Ladder round {g['ladder']['round']}, {g['ladder']['n_banned']} emojis banned", ""]
               if g.get("ladder") else []),
             f"Explainer `{g['model_id']}` → guesser `{g.get('guesser_model_id', g['model_id'])}`, {status}", "", f"> {a['extract']}", ""]
    for t in g["turns"]:
        lines += [f"### Turn {t['turn']}", "", f"**Explainer:** {t['delivered'] or '(nothing)'}", ""]
        scratch = re.sub(r"<msg>.*?</msg>", "", t["explainer_raw"], flags=re.S).strip()
        if scratch:
            lines += ["<details><summary>explainer scratchpad</summary>", "", scratch, "", "</details>", ""]
        lines += [f"**Guesser:** {t['guesser']}", ""]
        if t["guess"]:
            lines += [f"→ `{t['guess']}` {'✅' if t['correct'] else '❌'}", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_fetch(args):
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    arts = []
    for i in range(args.n):
        a = random_article()
        arts.append(a)
        log(f"{i + 1:3d}. {a['title']}: {a['description']}")
    out.write_text(json.dumps(arts, indent=2, ensure_ascii=False))
    log(f"wrote {len(arts)} articles to {out}")


def cmd_run(args):
    arts = list(enumerate(json.loads(Path(args.articles).read_text()), 1))
    if args.only:
        keep = {int(x) for x in args.only.split(",")}
        arts = [(i, a) for i, a in arts if i in keep]
    if args.limit:
        arts = arts[: args.limit]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    exp_model = args.explainer or args.model
    gue_model = args.guesser or args.model
    if not (exp_model and gue_model):
        sys.exit("give -m MODEL, or both --explainer and --guesser")
    for m in {exp_model, gue_model, JUDGE_MODEL}:
        check_model(m)
    pair = safe_name(exp_model) if exp_model == gue_model else f"{safe_name(exp_model)}-x-{safe_name(gue_model)}"
    kb = f"-kb{round(args.keyboard * 100)}" if args.keyboard else ""
    run_dir = RUNS_DIR / f"{stamp}-{pair}{kb}{'-' + args.effort if args.effort else ''}"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps(
        {"explainer": exp_model, "guesser": gue_model, "effort": args.effort,
         "keyboard": args.keyboard, "keyboard_seed": args.keyboard_seed, "articles": str(args.articles),
         "max_turns": MAX_TURNS, "judge": JUDGE_MODEL, "harness": HARNESS}, indent=2))
    log(f"run → {run_dir}")

    def one(i, a):
        tag = f"{pair} g{i:02d}"
        g = play(exp_model, gue_model, a, tag, effort=args.effort,
                 kb_fraction=args.keyboard, kb_seed=args.keyboard_seed)
        stem = f"game-{i:02d}"
        (run_dir / f"{stem}.json").write_text(json.dumps(g, indent=2, ensure_ascii=False))
        (run_dir / f"{stem}.md").write_text(game_markdown(g))
        res = (f"ERROR ({g['error']['role']}): {g['error']['message']}" if g.get("error")
               else f"solved in {g['solved_turn']}" if g["solved"] else "NOT solved")
        log(f"[{tag}] {a['title']}: {res}")
        return g

    with cf.ThreadPoolExecutor(args.parallel) as ex:
        list(ex.map(lambda p: one(*p), arts))
    cmd_report(argparse.Namespace(runs=[str(run_dir)], json=False, bench=False, ladder_break=None,
                                  ladder_rounds=None, budget=BUDGET))


def used_bases(g):
    """Base keys of every emoji the explainer got through to the guesser in a game."""
    used = set()
    for t in g["turns"]:
        s, i = t["delivered"].replace(VS16, ""), 0
        while i < len(s):
            for n in range(min(MAX_EMOJI_LEN, len(s) - i), 0, -1):
                if s[i : i + n] in EMOJI:
                    used.add(base_of(s[i : i + n]))
                    i += n
                    break
            else:
                i += 1
    return used


def find_normal_game(model, title, effort=None):
    """An existing normal-mode self-play game, to reuse as ladder round 0."""
    for f in sorted(RUNS_DIR.rglob("game-*.json")):
        g = json.loads(f.read_text())
        if (g["model"] == model and g.get("guesser_model", model) == model and not g.get("keyboard")
                and g.get("effort") == effort and not g.get("error") and g["article"]["title"] == title):
            return g, f
    return None, None


def cmd_ladder(args):
    """Ban-and-replay: after each solve, ban every emoji the explainer used and play the article again."""
    for m in {args.model, JUDGE_MODEL}:
        check_model(m)
    arts = list(enumerate(json.loads(Path(args.articles).read_text()), 1))
    if args.only:
        keep = {int(x) for x in args.only.split(",")}
        arts = [(i, a) for i, a in arts if i in keep]
    if args.cont:
        run_dir = Path(args.cont)
        log(f"ladder (continuing) → {run_dir}")
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = RUNS_DIR / f"{stamp}-{safe_name(args.model)}-ladder"
        run_dir.mkdir(parents=True)
        log(f"ladder → {run_dir}")
    (run_dir / "meta.json").write_text(json.dumps(
        {"model": args.model, "mode": "ladder", "rounds": args.rounds, "break_turns": args.break_turns,
         "budget": args.budget,
         "articles": str(args.articles), "reuse_round0": not args.fresh, "max_turns": MAX_TURNS,
         "judge": JUDGE_MODEL, "harness": HARNESS}, indent=2))

    def survives(g):
        return g["solved"] and g["solved_turn"] <= args.break_turns

    def climb(i, a):
        banned, spent = set(), 0.0
        for r in range(args.rounds + 1):
            if args.budget and spent >= args.budget:
                log(f"[{args.model} a{i:02d}] {a['title']}: budget spent (${spent:.2f})")
                return
            tag = f"{args.model} a{i:02d} r{r}"
            stem = f"ladder-{i:02d}-r{r:02d}"
            done = run_dir / f"{stem}.json"
            if done.exists():  # --continue: replay the saved round instead of playing it again
                g = json.loads(done.read_text())
            else:
                g, src = (find_normal_game(args.model, a["title"], args.effort) if r == 0 and not args.fresh else (None, None))
                if g is None:  # every round, round 0 included, stops at the turn limit: past it nothing counts
                    g = play(args.model, args.model, a, tag, effort=args.effort, banned=set(banned),
                             stop_after=args.break_turns,
                             budget_left=args.budget - spent if args.budget else None)
                if g.get("error"):  # refusals, limits: keep the record aside so --continue retries this rung
                    (run_dir / f"error-{i:02d}-r{r:02d}.json").write_text(json.dumps(g, indent=2, ensure_ascii=False))
                    log(f"[{tag}] {a['title']}: ERROR ({g['error']['message'][:80]}), ladder paused here")
                    return
                g["ladder"] = {"article_no": i, "round": r, "n_banned": len(banned),
                               "break_turns": args.break_turns,
                               "reused_from": str(src.relative_to(ROOT)) if src else None}
                done.write_text(json.dumps(g, indent=2, ensure_ascii=False))
                (run_dir / f"{stem}.md").write_text(game_markdown(g))
                res = ("ERROR" if g.get("error") else f"solved in {g['solved_turn']}" if g["solved"]
                       else f"NOT solved in {args.break_turns}" if g.get("stopped_after") else "NOT solved")
                log(f"[{tag}] {a['title']}: {res} with {len(banned)} banned{' (reused)' if src else ''}")
            spent += game_cost_capped(g, args.break_turns)
            if g.get("out_of_budget"):
                log(f"[{args.model} a{i:02d}] {a['title']}: budget spent mid-game (${spent:.2f})")
                return
            if not survives(g):
                return
            banned |= used_bases(g)

    with cf.ThreadPoolExecutor(args.parallel) as ex:
        list(ex.map(lambda p: climb(*p), arts))
    ladder_report([run_dir], args.break_turns, args.rounds, args.budget or BUDGET)


BUDGET = 1.0  # rungs are counted within $1 of spend per article
ROUNDS = 30  # ban rounds after round 0: a ladder that gets there has climbed as far as the bench goes
ELO_ANCHOR = ("claude-sonnet-5-5", 1000)  # fixed reference so ratings stay put as models are added


def elo_ratings(rungs):
    """Bradley-Terry ratings from per-article head-to-heads. rungs: {(model, article): rungs @ budget}.
    On each article both models played, more rungs wins, equal is a draw; raw rung counts are never
    compared across articles, so article difficulty can't leak into the rating."""
    models = sorted({m for m, _ in rungs})
    arts = {}
    for (m, a), r in rungs.items():
        arts.setdefault(a, {})[m] = r
    record = {m: [0, 0, 0] for m in models}  # wins, draws, losses
    games = []  # (i, j, score of i)
    for res in arts.values():
        ms = sorted(res)
        for x in range(len(ms)):
            for y in range(x + 1, len(ms)):
                a, b = ms[x], ms[y]
                sc = 1.0 if res[a] > res[b] else 0.0 if res[a] < res[b] else 0.5
                games.append((models.index(a), models.index(b), sc))
                record[a][0 if sc == 1 else 1 if sc == 0.5 else 2] += 1
                record[b][0 if sc == 0 else 1 if sc == 0.5 else 2] += 1
    if len(models) < 2:
        return {m: (ELO_ANCHOR[1], record[m]) for m in models}
    # Maximum likelihood with a weak prior (one virtual draw against an average opponent per model),
    # so a perfect record doesn't run off to infinity.
    theta = [0.0] * len(models)
    for _ in range(5000):
        grad = [-t for t in theta]  # prior pulls toward the mean
        for i, j, sc in games:
            p = 1 / (1 + math.exp(theta[j] - theta[i]))
            grad[i] += sc - p
            grad[j] -= sc - p
        theta = [t + 0.05 * g for t, g in zip(theta, grad)]
    anchor_model, anchor_elo = ELO_ANCHOR
    base = theta[models.index(anchor_model)] if anchor_model in models else statistics.mean(theta)
    scale = 400 / math.log(10)
    return {m: (anchor_elo + scale * (theta[k] - base), record[m]) for k, m in enumerate(models)}


def game_cost_capped(g, brk):
    """A game's cost, pro rata to the turn limit (a reused round 0 may have run longer than the ladder allows)."""
    return game_cost(g) * min(1, brk / max(1, len(g["turns"])))


def entry(g):
    """A leaderboard entry: the model id, plus the reasoning effort if one was set."""
    return g["model_id"] + (f" ({g['effort']})" if g.get("effort") else "")


def ladder_data(paths, break_turns=None, max_rounds=None, budget=BUDGET):
    """EMOJIBENCH @ $budget: rungs climbed per article before failing a rung or running out of money,
    turned into Elo by per-article head-to-heads. break_turns / max_rounds re-read a run with stricter
    limits for free (the ladder is identical up to the first rung that breaks under them).
    Returns None if there are no ladder games."""
    rows, caps = {}, {}
    for p in paths:
        for f in sorted(Path(p).rglob("ladder-*.json")):
            if f.parent not in caps:  # the run's round cap; rounds past it (from an older, higher cap) are ignored
                meta = f.parent / "meta.json"
                caps[f.parent] = json.loads(meta.read_text()).get("rounds") if meta.exists() else None
            cap = min(c for c in (caps[f.parent], max_rounds) if c is not None) if (caps[f.parent] or max_rounds) else None
            g = json.loads(f.read_text())
            if cap is not None and g["ladder"]["round"] > cap:
                continue
            g["_path"], g["_cap"] = f, cap
            rows.setdefault((entry(g), g["article"]["title"]), []).append(g)
    if not rows:
        return None

    articles, rungs_at_budget, brk = [], {}, None
    for (m, title), gs in sorted(rows.items()):
        gs.sort(key=lambda g: g["ladder"]["round"])
        run_brk = gs[-1]["ladder"].get("break_turns") or gs[-1]["max_turns"]
        brk = min(break_turns, run_brk) if break_turns else run_brk
        for k, g in enumerate(gs):  # truncate at the first rung that breaks under this limit
            if not (g["solved"] and g["solved_turn"] <= brk):
                gs = gs[: k + 1]
                break
        ok = [g["solved"] and g["solved_turn"] <= brk for g in gs]
        cap = gs[-1]["_cap"]
        capped = ok[-1] and cap is not None and cap >= ROUNDS and gs[-1]["ladder"]["round"] >= cap
        costs = [game_cost_capped(g, brk) for g in gs]
        rungs_budget, spent = 0, 0.0
        for good, c in zip(ok, costs):
            spent += c
            if spent > budget or not good:
                break
            rungs_budget += 1
        rungs_at_budget[(m, title)] = rungs_budget
        articles.append({
            "model": m, "title": title, "rungs": rungs_budget, "rungs_free": sum(ok),
            "still_climbing": ok[-1], "spend": sum(costs), "n_banned": gs[-1]["ladder"]["n_banned"],
            # still climbing when the data ends, with money left: a low round cap or an error cut it short
            "capped": capped,
            "incomplete": ok[-1] and sum(costs) < budget and not capped,
            "run": gs[0]["_path"].parent, "last_rung": gs[-1]["_path"],
            "path": ["⚠" if g.get("error") else "$" if g.get("out_of_budget") else str(g["solved_turn"])
                     if g["solved"] else f">{g['stopped_after']}" if g.get("stopped_after") else "✗" for g in gs],
        })

    elo = elo_ratings(rungs_at_budget)
    board = []
    for m in elo:
        xs = [a for a in articles if a["model"] == m]
        board.append({"model": m, "elo": elo[m][0], "wdl": elo[m][1], "articles": len(xs),
                      "rungs": statistics.mean(a["rungs"] for a in xs),
                      "rungs_free": statistics.mean(a["rungs_free"] for a in xs),
                      "spend": statistics.mean(a["spend"] for a in xs),
                      "incomplete": sum(a["incomplete"] for a in xs)})
    board.sort(key=lambda b: -b["elo"])
    return {"budget": budget, "break_turns": brk, "anchor": ELO_ANCHOR, "board": board, "articles": articles}


def ladder_report(paths, break_turns=None, max_rounds=None, budget=BUDGET):
    d = ladder_data(paths, break_turns, max_rounds, budget)
    if d is None:
        return
    print("\n## EMOJIBENCH\n")
    print(f"| Rank | Model | **EMOJIBENCH** (Elo) | Head-to-heads W–D–L | Rungs @ ${budget:g} | $ per article | Articles |")
    print("|---:|---|---:|---:|---:|---:|---:|")
    for rank, b in enumerate(d["board"], 1):
        w, dr, l = b["wdl"]
        inc = f" ({b['incomplete']} cut short)" if b["incomplete"] else ""
        print(f"| {rank} | {b['model']} | **{b['elo']:.0f}** | {w}–{dr}–{l} | {b['rungs']:.2f} "
              f"| {b['spend']:.2f} | {b['articles']}{inc} |")
    print(f"\nEMOJIBENCH is an Elo rating ({ELO_ANCHOR[0]} = {ELO_ANCHOR[1]}). On every article two models both "
          f"played, the one that climbs more rungs with ${budget:g} to spend wins; equal rungs is a draw. Rung 1 is "
          f"the normal game; each next rung replays the article with every emoji used so far banned. A rung counts "
          f"if it is solved within {d['break_turns']} turns before the money runs out. Costs are API list-price "
          f"equivalents. +200 Elo means winning about 3 head-to-heads in 4. In paths: >N = not solved in N turns, "
          f"$ = stopped mid-game when the money ran out, ⚠ = error.\n")
    print("| Model | Article | Turns per rung | Rungs @ budget | Rungs, no budget | Spend | Emojis banned at the end |")
    print("|---|---|---|---:|---:|---:|---:|")
    for a in d["articles"]:
        print(f"| {a['model']} | {a['title']} | {' → '.join(a['path'])} | {a['rungs']} "
              f"| {a['rungs_free']}{'+' if a['still_climbing'] else ''} | {a['spend']:.2f} | {a['n_banned']} |")


def load_games(paths):
    games = []
    for p in paths:
        for f in sorted(Path(p).rglob("game-*.json")):
            games.append(json.loads(f.read_text()))
    return games


def pair_key(g, ids=None):
    """"opus → sonnet" style key. `ids` maps aliases to model ids, for games that errored
    before a role made a successful call (and so never learned its model id)."""
    ids = ids or {}
    e, gu = g["model_id"], g.get("guesser_model_id", g["model_id"])
    e, gu = ids.get(e, e), ids.get(gu, gu)
    kb = f" [keyboard {round(g['keyboard']['fraction'] * 100)}%]" if g.get("keyboard") else ""
    return (e if e == gu else f"{e} → {gu}") + kb + (f" ({g['effort']})" if g.get("effort") else "")


def game_cost(g):
    return sum(u["cost_usd"] for u in g["usage"].values())


def game_tokens(g, output_only=False):
    keys = ("output_tokens",) if output_only else (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    return sum(u[k] for u in g["usage"].values() for k in keys)


def summarize(all_gs):
    """Scores plus the cost index: $ and tokens per *solved* article, so failing slowly costs you."""
    gs = [g for g in all_gs if not g.get("error")]
    solved = [g["solved_turn"] for g in gs if g["solved"]]
    total_cost = sum(game_cost(g) for g in gs)
    total_tokens = sum(game_tokens(g) for g in gs)
    return {
        "games": len(gs),
        "solved": len(solved),
        "errors": len(all_gs) - len(gs),
        "solve_rate": len(solved) / len(gs) if gs else 0,
        "mean_turns_solved": statistics.mean(solved) if solved else None,
        "median_score": statistics.median([g["solved_turn"] if g["solved"] else g["max_turns"] + 1
                                           for g in gs]) if gs else None,
        "tokens_per_game": total_tokens / len(gs) if gs else 0,
        "output_tokens_per_game": sum(game_tokens(g, True) for g in gs) / len(gs) if gs else 0,
        "cost_per_game": total_cost / len(gs) if gs else 0,
        "cost_per_solve": total_cost / len(solved) if solved else None,
        "tokens_per_solve": total_tokens / len(solved) if solved else None,
    }


def cmd_report(args):
    games = load_games(args.runs or [RUNS_DIR])
    if not games or args.bench:
        ladder_report(args.runs or [RUNS_DIR], args.ladder_break, args.ladder_rounds, args.budget)
        return
    ids = {}
    for g in games:
        for alias, mid in ((g["model"], g["model_id"]),
                           (g.get("guesser_model"), g.get("guesser_model_id"))):
            if alias and mid and alias != mid:
                ids[alias] = mid
    by_model = {}
    for g in games:
        by_model.setdefault(pair_key(g, ids), []).append(g)
    stats = {m: summarize(gs) for m, gs in sorted(by_model.items())}
    ladder_report(args.runs or [RUNS_DIR], args.ladder_break, args.ladder_rounds, args.budget)
    if getattr(args, "json", False):
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return

    print("| Model | Games | Solved | Mean turns (solved) | Median score* | Errors "
          "| Tokens/game | $/game | **$/solve** |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for m, st in stats.items():
        mean_solved = f"{st['mean_turns_solved']:.1f}" if st["solved"] else "n/a"
        per_solve = f"{st['cost_per_solve']:.3f}" if st["solved"] else "∞"
        median = f"{st['median_score']:g}" if st["games"] else "n/a"
        print(f"| {m} | {st['games']} | {st['solved']}/{st['games']} ({100 * st['solve_rate']:.0f}%) "
              f"| {mean_solved} | {median} | {st['errors']} | {st['tokens_per_game']:,.0f} "
              f"| {st['cost_per_game']:.3f} | {per_solve} |")
    print(f"\n*unsolved games count as {games[0]['max_turns'] + 1}; errored games (refusals, API failures) "
          "are not scored. $ = API list-price equivalent (you pay via subscription); "
          "$/solve = total spend / articles solved. ✗ = not solved, ⚠ = error\n")

    print("| Article | " + " | ".join(sorted(by_model)) + " |")
    print("|---|" + "---:|" * len(by_model))
    titles = list(dict.fromkeys(g["article"]["title"] for g in games))
    for t in titles:
        cells = []
        for m in sorted(by_model):
            g = next((g for g in by_model[m] if g["article"]["title"] == t), None)
            cells.append("" if g is None else "⚠" if g.get("error") else
                         str(g["solved_turn"]) if g["solved"] else "✗")
        print(f"| {t} | " + " | ".join(cells) + " |")


def check_run(d, articles):
    """Problems (they make the score wrong) and warnings for one bench run directory."""
    problems, warnings = [], []
    meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
    if meta.get("break_turns") != 5:
        problems.append(f"meta.json: break_turns is {meta.get('break_turns')}; the bench uses 5")
    rows = {}
    for f in sorted(d.glob("ladder-*.json")):
        g = json.loads(f.read_text())
        rows.setdefault(g["article"]["title"], {})[g["ladder"]["round"]] = (f, g)
    models = sorted({entry(g) for rs in rows.values() for _, g in rs.values()})
    if len(models) != 1:
        problems.append(f"a run holds one model's games; found {models or 'none'}")
    if missing := [t for t in articles if t not in rows]:
        problems.append(f"{len(missing)} of {len(articles)} articles not played: {', '.join(missing)}")
    if old := sorted({g.get("harness") for rs in rows.values() for _, g in rs.values()} - {None, HARNESS}):
        warnings.append(f"played with harness {', '.join(old)}, this checkout is {HARNESS}: prompts or rules changed since")

    for title, rs in rows.items():
        if title not in articles:
            problems.append(f"{title}: not in the article set")
            continue
        banned = set()
        for r in range(max(rs) + 1):
            if r not in rs:
                problems.append(f"{title}: round {r} is missing")
                break
            f, g = rs[r]
            if g["article"] != articles[title]:
                problems.append(f"{f.name}: the article text differs from the set's")
            if g.get("guesser_model_id", g["model_id"]) != g["model_id"]:
                problems.append(f"{f.name}: explainer and guesser are different models")
            if g["ladder"]["n_banned"] != len(banned):
                problems.append(f"{f.name}: {g['ladder']['n_banned']} emoji banned, but the earlier rounds used {len(banned)}")
            allowed = {k for k in EMOJI if base_of(k) not in banned} if banned else None
            for t in g["turns"]:
                raw = t["explainer_raw"].split("[RETRY]")[-1] if t["retried"] else t["explainer_raw"]
                if filter_emojis(extract_msg(raw), allowed)[0] != t["delivered"]:
                    problems.append(f"{f.name} turn {t['turn']}: the guesser got something other than the "
                                    f"filter's output for the explainer's message")
            right = [t["turn"] for t in g["turns"] if t["correct"]]
            if right != ([g["solved_turn"]] if g["solved"] else []):
                problems.append(f"{f.name}: solved_turn doesn't match the judge's verdicts")
            for role in ("explainer", "guesser"):
                if g["usage"][role]["calls"] and not g["usage"][role]["cost_usd"] > 0:
                    problems.append(f"{f.name}: the {role} cost $0, so the $1 budget never binds "
                                    f"(play the paid model id, not a :free one)")
            if r < max(rs) and not (g["solved"] and g["solved_turn"] <= 5 and not g.get("out_of_budget")):
                problems.append(f"{f.name}: the ladder went on after a rung that doesn't count")
            banned |= used_bases(g)

    pending = sorted(f.name for f in d.glob("error-*.json") if not (d / f.name.replace("error-", "ladder-")).exists())
    lad = ladder_data([d]) if rows else None
    cut = [a["title"] for a in lad["articles"] if a["incomplete"]] if lad else []
    if pending or cut:
        problems.append(f"unfinished ({len(pending)} rungs stopped by errors, {len(cut)} ladders still climbing with "
                        f"money left): resume with ./emojibench.py bench -m <model> --continue {d}")
    return models, rows, problems, warnings


def cmd_check(args):
    """What CI runs on a results PR. It re-derives what it can from the saved games: the article set, the
    filter and the ban list on every message, the verdicts, the money, completeness. It can't prove a
    model really played them; maintainers can replay a few articles for that."""
    articles = {a["title"]: a for a in json.loads(Path(args.articles).read_text())}
    dirs = [Path(p) for p in args.runs] or sorted({Path(os.path.relpath(f.parent)) for f in RUNS_DIR.rglob("ladder-*.json")})
    failed, seen = 0, {}
    for d in dirs:
        models, rows, problems, warnings = check_run(d, articles)
        for m in models:
            if m in seen:
                problems.append(f"{m} already has a run in {seen[m]}: one run per model (and effort), or the "
                                f"ladders mix. Continue that run instead, or replace it.")
            seen.setdefault(m, d)
        games = sum(len(rs) for rs in rows.values())
        print(f"{'FAIL' if problems else 'ok  '} {d}  {', '.join(models)}: {len(rows)} articles, {games} games")
        for p in problems[:20]:
            print(f"     ✗ {p}")
        if len(problems) > 20:
            print(f"     … and {len(problems) - 20} more")
        for w in warnings:
            print(f"     ! {w}")
        failed += bool(problems)
    if not dirs:
        print("no bench runs found")
    sys.exit(1 if failed or not dirs else 0)


def cmd_models(args):
    words = [w.lower() for w in args.query]
    rows = []
    for mid, m in sorted(openrouter_models().items()):
        if all(w in f"{mid} {m.get('name', '')}".lower() for w in words):
            p = m.get("pricing", {})
            rows.append((mid, float(p.get("prompt") or 0) * 1e6, float(p.get("completion") or 0) * 1e6))
    print("| Model | $ / M input | $ / M output |\n|---|---:|---:|")
    for mid, i, o in rows:
        print(f"| openrouter:{mid} | {i:.2f} | {o:.2f} |")
    log(f"{len(rows)} models. Run one with: ./emojibench.py bench -m openrouter:<id>")


JUDGE_HELP = (f"model that judges guesses (default: Claude Haiku 5.5, as {JUDGE_MODEL} through Claude Code if "
              f"it is installed, else {JUDGE_OPENROUTER})")


def main():
    global JUDGE_MODEL
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(required=True)

    f = sub.add_parser("fetch", help="sample random Wikipedia articles into a fixed set")
    f.add_argument("-n", type=int, default=20)
    f.add_argument("-o", "--out", default="articles/set1.json")
    f.set_defaults(fn=cmd_fetch)

    r = sub.add_parser("run", help="play every article in a set with one model")
    r.add_argument("-m", "--model", help="model for both roles: haiku | sonnet | opus | fable | openrouter:<id>")
    r.add_argument("-e", "--explainer", help="explainer model (overrides -m)")
    r.add_argument("-g", "--guesser", help="guesser model (overrides -m)")
    r.add_argument("-a", "--articles", default="articles/set1.json")
    r.add_argument("--limit", type=int, help="only the first N articles")
    r.add_argument("--only", help="comma-separated article numbers (1-based), e.g. 2,5")
    r.add_argument("-j", "--parallel", type=int, default=4, help="games in parallel")
    r.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    r.add_argument("--keyboard", type=float, metavar="FRACTION",
                   help="hard mode: each article gets a random keyboard with this fraction of emoji, e.g. 0.5")
    r.add_argument("--keyboard-seed", default="v1", help="change to deal different keyboards")
    r.add_argument("--judge", help=JUDGE_HELP)
    r.set_defaults(fn=cmd_run)

    lad = sub.add_parser("bench", aliases=["ladder"],
                         help="EMOJIBENCH: the ban-and-replay ladder on every article, $1 per article")
    lad.add_argument("-m", "--model", required=True, help="haiku | sonnet | opus | fable | openrouter:<id>")
    lad.add_argument("-a", "--articles", default="articles/set1.json")
    lad.add_argument("--only", help="comma-separated article numbers (1-based)")
    lad.add_argument("--rounds", type=int, default=ROUNDS, help=f"max ban rounds after round 0 (default {ROUNDS})")
    lad.add_argument("--budget", type=float, default=BUDGET,
                     help="stop climbing once this many $ (list price) are spent on the article (default 1)")
    lad.add_argument("--fresh", action="store_true", help="play round 0 instead of reusing a normal game")
    lad.add_argument("--break-turns", type=int, default=5,
                     help="the ladder ends when a round takes more turns than this; ban rounds are cut off there")
    lad.add_argument("--continue", dest="cont", metavar="RUN_DIR", help="resume a ladder run directory")
    lad.add_argument("-j", "--parallel", type=int, default=4, help="articles in parallel")
    lad.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    lad.add_argument("--judge", help=JUDGE_HELP)
    lad.set_defaults(fn=cmd_ladder)

    rep = sub.add_parser("report", help="leaderboard over run directories")
    rep.add_argument("runs", nargs="*", help="run dirs (default: all of runs/)")
    rep.add_argument("--json", action="store_true", help="print per-pair stats as JSON")
    rep.add_argument("--bench", action="store_true", help="only the EMOJIBENCH leaderboard, not the normal games")
    rep.add_argument("--ladder-break", type=int, metavar="N",
                     help="re-score ladders with a stricter turn limit (free: no new games)")
    rep.add_argument("--budget", type=float, default=BUDGET, help="EMOJIBENCH budget per article in $ (default 1)")
    rep.add_argument("--ladder-rounds", type=int, metavar="N",
                     help="re-score ladders with a lower round cap (free: no new games)")
    rep.set_defaults(fn=cmd_report)

    mo = sub.add_parser("models", help="search OpenRouter model ids, with prices per million tokens")
    mo.add_argument("query", nargs="*", help="words that must all appear in the id or name")
    mo.set_defaults(fn=cmd_models)

    ch = sub.add_parser("check", help="validate bench runs: the rules, the filter, completeness (CI runs this on PRs)")
    ch.add_argument("runs", nargs="*", help="run dirs (default: every bench run in runs/)")
    ch.add_argument("-a", "--articles", default="articles/set1.json")
    ch.set_defaults(fn=cmd_check)

    t = sub.add_parser("filter", help="debug: run text through the emoji filter")
    t.add_argument("text")
    t.set_defaults(fn=lambda a: print(filter_emojis(a.text)))

    args = ap.parse_args()
    if hasattr(args, "judge"):
        JUDGE_MODEL = args.judge or (JUDGE_MODEL if shutil.which("claude") else JUDGE_OPENROUTER)
    args.fn(args)


if __name__ == "__main__":
    main()
