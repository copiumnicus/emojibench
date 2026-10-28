#!/usr/bin/env python3
"""Build site/index.html and the generated parts of README.md from runs/. Every number and quote
comes from the game files; quotes are checked against the transcripts so neither can drift from
the data. README.md keeps its hand-written text; only the blocks between
<!-- generated:NAME --> and <!-- /generated:NAME --> are rewritten."""

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import emojibench as eb  # noqa: E402

NAMES = {"claude-fable-5-1": "Fable 5.1", "claude-opus-5-5": "Opus 5.5",
         "claude-sonnet-5-5": "Sonnet 5.5", "claude-haiku-5-5": "Haiku 5.5"}
ALIASES = {"fable": "claude-fable-5-1", "opus": "claude-opus-5-5",
           "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
SELFPLAY = ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
MATRIX = ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5"]

# Turn-1 solves to play as puzzles: (article, a phrase from the guesser's decoding).
PUZZLES = [
    ("Mike Pieciak", '🎤🥧 reads as "Mic" + "Pie", so Mike Pieciak'),
    ("Richmond Hill South", '💰🌙 = "Rich" + "moon" = Richmond, ⛰️ = Hill, ⬇️ = South?'),
    ("Jovan Hadži-Vasiljević", 'The 🚽 = "john" = Jovan'),
    ("Sage Derby", 'Herb + wizard → "sage." Horse race + top hat → "Derby."'),
    ("Accord Pond", 'I read 🤝🚗🇯🇵 as "Accord" (Honda Accord)'),
    ("Dorothy Cottrell", "The Dorothy clue (Wizard of Oz)"),
    ("Iron(III) sulfate", "the formula line gives 2 iron to 3 sulfate → Fe₂(SO₄)₃"),
    ("Baggio–Yoshinari syndrome", "an Italian footballer name (Baggio) plus a Japanese name (Yoshinari)"),
    ("Franco Cozzo", "plus trilingual TV ads"),
    ("Mad River Mountain", "Skiing + USA + 🌰 (buckeye → Ohio?) + mad + river + mountain"),
]


def display_name(entry):
    """Claude models by their short name; OpenRouter models by OpenRouter's name, minus the lab prefix.
    A leaderboard entry may end in an effort level, e.g. "google/gemini-3.1-pro-preview (high)"."""
    mid, _, effort = entry.partition(" (")
    if mid in NAMES:
        name = NAMES[mid]
    else:
        try:
            name = eb.openrouter_models()[mid]["name"].split(": ", 1)[-1]
        except Exception:
            name = mid
    return f"{name} ({effort}" if effort else name


def load():
    games = []
    for f in sorted((ROOT / "runs").rglob("game-*.json")):
        g = json.loads(f.read_text())
        if g.get("keyboard"):  # hard-mode games aren't on the page yet
            continue
        g["e"] = ALIASES.get(g["model_id"], g["model_id"])
        gid = g.get("guesser_model_id", g["model_id"])
        g["g"] = ALIASES.get(gid, gid)
        games.append(g)
    return games


def find(games, e, g, title):
    hits = [x for x in games if x["e"] == e and x["g"] == g and x["article"]["title"] == title]
    assert len(hits) == 1, (e, g, title, len(hits))
    return hits[0]


def excerpt(text, phrase):
    assert phrase in text, f"quote not in transcript: {phrase!r}"
    # Extend the phrase to the end of its sentence/line, so quotes stay whole.
    start = text.index(phrase)
    end = start + len(phrase)
    while end < len(text) and text[end] not in "\n":
        end += 1
    return text[start:end].strip()


def segments(raw):
    """Split a raw explainer message into kept emoji and deleted text, for the filter story."""
    s, out, i = raw.replace(eb.VS16, ""), [], 0
    while i < len(s):
        for n in range(min(eb.MAX_EMOJI_LEN, len(s) - i), 0, -1):
            if s[i:i + n] in eb.EMOJI:
                out.append([eb.EMOJI[s[i:i + n]], 1])
                i += n
                break
        else:
            if out and out[-1][1] == 0:
                out[-1][0] += s[i]
            else:
                out.append([s[i], 0])
            i += 1
    return [[t, k] for t, k in out if t.strip() or k]


def rel(p):
    return Path(p).resolve().relative_to(ROOT).as_posix()


def money(x):
    return "never" if x is None else f"${x:.2f}"


def collect():
    games = load()
    stats = {}
    for e in {x["e"] for x in games}:
        for g in {x["g"] for x in games}:
            gs = [x for x in games if x["e"] == e and x["g"] == g]
            if gs:
                stats[f"{e}|{g}"] = {**eb.summarize(gs), "e": e, "g": g}

    articles = json.loads((ROOT / "articles" / "set1.json").read_text())

    puzzles = []
    for title, phrase in PUZZLES:
        cands = [x for x in games if x["article"]["title"] == title and x["solved_turn"] == 1
                 and phrase in x["turns"][0]["guesser"]]
        assert cands, f"no turn-1 solve quoting {phrase!r}"
        x = cands[0]
        puzzles.append({"emoji": x["turns"][0]["delivered"], "title": title,
                        "description": x["article"]["description"], "url": x["article"]["url"],
                        "e": x["e"], "g": x["g"], "quote": excerpt(x["turns"][0]["guesser"], phrase)})

    # Story: Sonnet counts treasurers with ⏩⏩
    s = find(games, "claude-sonnet-5-5", "claude-sonnet-5-5", "Mike Pieciak")
    chain = {"turns": [{"n": t["turn"], "emoji": t["delivered"], "guess": t["guess"], "ok": t["correct"]}
                       for t in s["turns"][4:7]],
             "quote": excerpt(s["turns"][6]["guesser"], "⏩⏩ means two treasurers later")}

    # Story: the filter eats Sonnet's attempt to spell
    s = find(games, "claude-sonnet-5-5", "claude-sonnet-5-5", "Proceratium silaceum")
    t8 = s["turns"][7]
    cheat = {"segments": segments(eb.extract_msg(t8["explainer_raw"])), "delivered": t8["delivered"],
             "guess": t8["guess"], "turn": t8["turn"]}

    # Story: Haiku, one letter off
    s = find(games, "claude-haiku-5-5", "claude-haiku-5-5", "Franco Cozzo")
    o = find(games, "claude-opus-5-5", "claude-opus-5-5", "Franco Cozzo")
    close = {"guesses": [t["guess"] for t in s["turns"] if t["guess"]],
             "haiku_first": s["turns"][0]["delivered"], "haiku_last": s["turns"][-1]["delivered"],
             "opus": o["turns"][0]["delivered"]}

    # Story: thirty turns of skulls
    s = find(games, "claude-haiku-5-5", "claude-haiku-5-5", "Grande Seca")
    skulls = {"last": s["turns"][-1]["delivered"], "guesses": [t["guess"] for t in s["turns"] if t["guess"]][-6:],
              "count": sum(t["delivered"].count("💀") for t in s["turns"])}

    # Story: the safety classifier
    s = find(games, "claude-opus-5-5", "claude-fable-5-1", "Baggio–Yoshinari syndrome")
    assert s["error"], "expected the safeguard error"
    msg = s["error"]["message"]
    safety = {"message": msg[msg.index("API Error: ") + len("API Error: "):].split(" (https")[0]}

    # EMOJIBENCH @ $1: the ladder leaderboard, straight from the same scoring as `report`
    lad = eb.ladder_data([ROOT / "runs"])
    grid, runs, per_game = {}, {}, {}
    for a in lad["articles"]:
        if a["incomplete"]:
            end = "cut"
        elif a["capped"] and a["rungs"] == a["rungs_free"]:
            end = "cap"  # climbed every rung the bench has
        elif a["rungs"] < a["rungs_free"] or a["still_climbing"] or a["path"][-1] == "$":
            end = "money"  # it could still climb, or was stopped mid-game; the $1 ran out
        else:
            end = "fail"
        grid.setdefault(a["title"], {})[a["model"]] = {
            "rungs": a["rungs"], "end": end, "last": rel(a["last_rung"].with_suffix(".md"))}
        runs[a["model"]] = rel(a["run"])
        n, c = per_game.get(a["model"], (0, 0.0))
        per_game[a["model"]] = (n + len(a["path"]), c + a["spend"])

    def ladder_game(model, title, rnd):
        for f in (ROOT / "runs").rglob("ladder-*.json"):
            g = json.loads(f.read_text())
            if g["model_id"] == model and g["article"]["title"] == title and g["ladder"]["round"] == rnd:
                return g
        raise AssertionError((model, title, rnd))

    lusophone = ladder_game("claude-opus-5-5", "Grande Seca", 7)
    assert "🇦🇴" in lusophone["turns"][1]["delivered"] and lusophone["solved_turn"] == 2
    board = [{**b, "wdl": list(b["wdl"]), "run": runs[b["model"]]} for b in lad["board"]]
    names = {m: display_name(m) for m in {*NAMES, *(b["model"] for b in board)}}
    ladder = {
        "budget": lad["budget"], "break_turns": lad["break_turns"], "anchor": lad["anchor"][1],
        "board": board,
        "grid": grid,
        "lusophone": {"banned": lusophone["ladder"]["n_banned"], "rung": lusophone["ladder"]["round"] + 1,
                      "first": lusophone["turns"][0]["delivered"], "first_guess": lusophone["turns"][0]["guess"],
                      "second": lusophone["turns"][1]["delivered"]},
    }

    data = {
        "names": names,
        "selfplay": SELFPLAY, "matrix": MATRIX, "stats": stats,
        "articles": [{"title": a["title"], "description": a["description"], "url": a["url"]} for a in articles],
        "games": [{"e": x["e"], "g": x["g"], "title": x["article"]["title"], "turns": x["solved_turn"],
                   "error": bool(x.get("error")), "cost": round(eb.game_cost(x), 4)} for x in games],
        "puzzles": puzzles,
        "stories": {"chain": chain, "cheat": cheat, "close": close, "skulls": skulls, "safety": safety},
        "max_turns": eb.MAX_TURNS,
        "ladder": ladder,
    }
    data["notes"] = notes(data, {m: c / n for m, (n, c) in per_game.items()})
    return data


def notes(data, cost_per_game):
    """The sentences that interpret the numbers, written once for both the page and the README.
    Each claim is only made when the data supports it, so new models can't make them false."""
    name = data["names"].get
    L, st = data["ladder"], lambda e, g: data["stats"][f"{e}|{g}"]
    titles = [a["title"] for a in data["articles"] if a["title"] in L["grid"]]
    a, b = (x["model"] for x in L["board"][:2])
    count = {m: {end: sum(1 for t in titles if L["grid"][t].get(m, {}).get("end") == end)
                 for end in ("fail", "money", "cut")} for m in (a, b)}
    fails = lambda m: {0: "none", 1: "1 article"}.get(count[m]["fail"], f"{count[m]['fail']} articles")  # noqa: E731
    ladder = f"Out of {len(titles)} articles, {name(a)} failed a rung on {fails(a)} and {name(b)} on {fails(b)}."
    if all(count[m]["fail"] + count[m]["money"] == len(titles) for m in (a, b)):
        ladder += " Every other climb of theirs ended because the dollar ran out."
        if count[a]["fail"] >= count[b]["fail"] and cost_per_game[a] < cost_per_game[b]:
            ladder += (f" What separates them is price: {name(a)} spends less per game, so the same $1 buys it "
                       f"more rungs.")

    self_ = sorted((m for m in data["selfplay"] if st(m, m)["cost_per_solve"] is not None),
                   key=lambda m: st(m, m)["cost_per_solve"])
    h, o, f = (st(m, m) for m in ("claude-haiku-5-5", "claude-opus-5-5", "claude-fable-5-1"))
    cost = (f"Among models playing themselves, {name(self_[0])} gives the cheapest answers. "
            f"Haiku solved only {h['solved']} of {h['games']} and burned {round(h['tokens_per_game'] / 1000)}k tokens "
            f"per game against Opus's {round(o['tokens_per_game'] / 1000)}k, but most of those are cached reads at a "
            f"cent per million, so its answers still cost {money(h['cost_per_solve'])}. Fable takes the fewest turns "
            f"and costs the most per call: {f['cost_per_solve'] / o['cost_per_solve']:.1f}× Opus per answer.")

    t = lambda e, g: st(e, g)["mean_turns_solved"]  # noqa: E731
    oo, ss = t("claude-opus-5-5", "claude-opus-5-5"), t("claude-sonnet-5-5", "claude-sonnet-5-5")
    os_, so = t("claude-opus-5-5", "claude-sonnet-5-5"), t("claude-sonnet-5-5", "claude-opus-5-5")
    pairs = (f"Both roles matter about equally. Two Sonnets need {ss:.1f} turns. Swap in Opus as either the "
             f"explainer ({os_:.1f}) or the guesser ({so:.1f}) and the turns roughly halve. Make it Opus on both "
             f"sides and they halve again, to {oo:.1f}: the two effects multiply. Fable and Opus together are "
             f"already at the one-or-two-turn floor, so their cells can't separate.")
    return {"ladder": ladder, "cost": cost, "pairs": pairs}


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------


def md(s):
    """Plain text, safe inside markdown and table cells."""
    return re.sub(r"([\\`*_\[\]<>|~])", r"\\\1", str(s)).replace("\n", "<br>")


def url(u):
    """Parentheses in a link target end the link early in some renderers."""
    return u.replace("(", "%28").replace(")", "%29")


def table(head, rows, align):
    out = ["| " + " | ".join(head) + " |", "|" + "|".join({"l": "---", "r": "---:", "c": ":---:"}[a] for a in align) + "|"]
    return "\n".join(out + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def readme_blocks(data):
    name = lambda m: data["names"].get(m, m)  # noqa: E731
    pair = lambda e, g: name(e) if e == g else f"{name(e)} → {name(g)}"  # noqa: E731
    st = lambda e, g: data["stats"].get(f"{e}|{g}")  # noqa: E731
    L, S = data["ladder"], data["stories"]
    blocks = {}

    blocks["meta"] = (f"{len(data['articles'])} random articles, {len(L['board'])} models so far, "
                      f"{len(data['games'])} normal games plus a ladder per model and article. "
                      f"Every game is in [`runs/`](runs).")

    items = []
    for p in data["puzzles"]:
        who = name(p["e"]) + (" explaining to itself" if p["e"] == p["g"] else f" explaining to {name(p['g'])}")
        items.append(f"<details><summary>{md(p['emoji'])}</summary>\n\n"
                     f"**[{md(p['title'])}]({url(p['url'])})**<br>*{md(p['description'])}*\n\n{who}, solved in 1 turn:\n\n"
                     f"> “{md(p['quote'])}”\n\n</details>")
    blocks["puzzles"] = "\n".join(items)

    blocks["board"] = table(
        ["", "Model", "EMOJIBENCH", "Won–drawn–lost", "Rungs per article"],
        [[k, f"[{md(name(b['model']))}]({b['run']})", f"**{b['elo']:.0f}**", "–".join(map(str, b["wdl"])),
          f"{b['rungs']:.2f}"] for k, b in enumerate(L["board"], 1)],
        "rlrrr")

    models = [b["model"] for b in L["board"]]
    ends = {"money": "", "fail": " ✗", "cut": " ⚠", "cap": " 🏁"}
    rows = []
    for a in data["articles"]:
        cells = L["grid"].get(a["title"], {})
        rows.append([md(a["title"])] + [f"[{c['rungs']}]({c['last']}){ends[c['end']]}" if (c := cells.get(m)) else "–"
                                        for m in models])
    lu = L["lusophone"]
    blocks["ladders"] = "\n\n".join([
        table(["Article"] + [md(name(m)) for m in models], rows, "l" + "r" * len(models)),
        f"Each number is the rungs climbed within $1 and links to the transcript of the last rung played. "
        f"✗ means the next rung took more than {L['break_turns']} turns; no mark means the $1 ran out mid-climb."
        + (f" 🏁 means it climbed all {eb.ROUNDS + 1} rungs." if any(c["end"] == "cap" for r in L["grid"].values()
                                                                   for c in r.values()) else ""),
        data["notes"]["ladder"],
        f"#### No Brazilian flag\n\nOpus, rung {lu['rung']} of Grande Seca, a drought in Brazil. {lu['banned']} emoji "
        f"were banned by then, 🇧🇷 among them. The first message was read as *{md(lu['first_guess'])}*. The second "
        f"named Brazil through the other Portuguese-speaking countries:\n\n> {md(lu['second'])}\n\n"
        f"Angola, Mozambique, Cape Verde, Guinea-Bissau, East Timor, and a mammoth for the biggest one. "
        f"The guesser got Brazil, then the drought, then the title.",
    ])

    blocks["normal"] = table(
        ["Model", "Solved", "Mean turns when solved", "Cost per solved article"],
        [[name(m), f"{s['solved']}/{s['games']}", f"{s['mean_turns_solved']:.1f}", money(s["cost_per_solve"])]
         for m in data["selfplay"] if (s := st(m, m))],
        "lrrr")

    by_cost = sorted((s for s in data["stats"].values() if s["cost_per_solve"] is not None),
                     key=lambda s: s["cost_per_solve"])
    blocks["cost"] = table(
        ["Explainer → guesser", "Cost per solved article", "Mean turns", "Solved"],
        [[pair(s["e"], s["g"]), money(s["cost_per_solve"]), f"{s['mean_turns_solved']:.1f}", f"{s['solved']}/{s['games']}"]
         for s in by_cost],
        "lrrr") + "\n\n" + data["notes"]["cost"]

    M = data["matrix"]
    blocks["matrix"] = table(
        ["Explainer ↓ guesser →"] + [name(g) for g in M],
        [[f"**{name(e)}**"] + [f"{s['mean_turns_solved']:.1f} ({s['solved']}/{s['games']})" if (s := st(e, g)) else "not run"
                               for g in M] for e in M],
        "l" + "r" * len(M)) + "\n\n" + data["notes"]["pairs"]

    def turn(t):
        guess = f" → {md(t['guess'])} {'✅' if t['ok'] else '❌'}" if t["guess"] else ""
        return f"- turn {t['n']}: {md(t['emoji'])}{guess}"

    cheat = " ".join(md(t) if keep else f"~~{md(t.strip())}~~" for t, keep in S["cheat"]["segments"])
    blocks["stories"] = "\n\n".join([
        "### Two treasurers later\n\nSonnet had narrowed it to a Vermont state treasurer, then guessed the wrong one "
        "twice. The explainer started counting forward with ⏩.\n\n" + "\n".join(turn(t) for t in S["chain"]["turns"])
        + f"\n\nGuesser: “{md(S['chain']['quote'])}”",
        f"### The filter at work\n\nOn turn {S['cheat']['turn']} of the ant game, Sonnet's explainer tried to write "
        f"words between its emoji. This is what it sent, with the deleted text struck through:\n\n> {cheat}\n\n"
        f"The guesser only saw {md(S['cheat']['delivered'])} and still answered "
        f"[{md(S['cheat']['guess'])}](https://en.wikipedia.org/wiki/Proceratium_silaceum). Silaceum is Latin for "
        f"ochre-yellow.",
        "### One letter off\n\nFranco Cozzo is a Melbourne furniture seller famous for shouting TV ads in English, "
        f"Greek and Italian. Opus and Fable both got him in one turn. Opus sent:\n\n> {md(S['close']['opus'])}\n\n"
        f"Haiku spent 30 turns and guessed: {', '.join(md(g) for g in S['close']['guesses'])}. Its explainer never "
        f"sent a sofa. Its last message was {md(S['close']['haiku_last'])}.",
        f"### {S['skulls']['count']} skulls\n\nGrande Seca was a drought in Brazil in 1877 and 1878 that killed "
        "hundreds of thousands of people. Haiku's explainer kept sending a version of the same message for 30 "
        f"turns:\n\n> {md(S['skulls']['last'])}\n\nThe guesser circled the right region of Brazil and never said "
        f"“drought”. Its last guesses: {', '.join(md(g) for g in S['skulls']['guesses'])}.",
        "### Flagged\n\nIn one Opus → Fable game the article was a tick-borne disease. A safety classifier stopped "
        f"the guesser on its first turn:\n\n```\n{S['safety']['message']}\n```\n\nThe game is recorded as an error "
        "and left out of the scores. Every other model got this article in one or two turns.",
    ])

    cols = data["selfplay"]
    rows = []
    for a in data["articles"]:
        cells = []
        for m in cols:
            g = next((x for x in data["games"] if x["e"] == m and x["g"] == m and x["title"] == a["title"]), None)
            cells.append("–" if g is None else "error" if g["error"] else g["turns"] or "✗")
        rows.append([f"[{md(a['title'])}]({url(a['url'])})<br><sub>{md(a['description'])}</sub>"] + cells)
    blocks["articles"] = table(["Article"] + [name(m).split(" ")[0] for m in cols], rows, "l" + "r" * len(cols))
    return blocks


def write_readme(data):
    path = ROOT / "README.md"
    text = path.read_text()
    for key, body in readme_blocks(data).items():
        start, end = f"<!-- generated:{key} -->", f"<!-- /generated:{key} -->"
        assert start in text and end in text, f"README.md is missing the {key} block markers"
        i, j = text.index(start) + len(start), text.index(end)
        text = text[:i] + "\n" + body + "\n" + text[j:]
    path.write_text(text)


def main():
    data = collect()
    html = (HERE / "template.html").read_text()
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (HERE / "index.html").write_text(html.replace("/*DATA*/null", blob))
    write_readme(data)
    print(f"wrote {rel(HERE / 'index.html')} and README.md: {len(data['games'])} games, "
          f"{len(data['ladder']['board'])} models on the leaderboard")


if __name__ == "__main__":
    main()
