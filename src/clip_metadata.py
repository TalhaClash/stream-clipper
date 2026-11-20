"""Per-clip title, description and hashtags, built from the clip's OWN
content — its real transcribed speech, the stream it came from, and the
viral peak it was cut at. Nothing is a fixed template: two clips from
the same stream get different hooks, different content tags, and a
different description, because they contain different words."""

import re
from typing import Dict, List, Optional

# Words that mark a quotable, high-energy moment. A phrase containing
# one of these is a hook; a phrase with none of them is not.
ENERGY_WORDS = {
    "OH", "GOD", "WOW", "INSANE", "CRAZY", "NO", "WAY", "HEADSHOT",
    "DESTROYED", "DESTROY", "KILLED", "KILL", "CLUTCH", "UNBELIEVABLE",
    "WAIT", "LOOK", "OMG", "WHAT", "DAMN", "SHIT", "BRO", "LETS", "GO",
    "WIN", "WON", "EPIC", "SAVAGE", "NOOO", "YES", "YESS", "YEAH",
    "AYE", "BRUH", "HOLY", "JESUS", "MORTY", "RICK", "ANIMATION",
    "SNIPE", "SNIPED", "ONE", "SHOT", "COMEBACK", "REVENGE", "GLITCH",
    "FUNNY", "RAGE", "SCREAM", "HYPE", "LETSGO", "BOOM", "DEAD", "DIED",
}
# Words a hook should not start or end with.
FILLERS = {
    "THE", "A", "AN", "AND", "OF", "TO", "IT", "IS", "I", "MY", "IN",
    "ON", "AT", "SO", "BUT", "OR", "IF", "THEN", "THAT", "THIS", "HE",
    "SHE", "WE", "YOU", "ME", "HIM", "HER", "US", "THEM", "WAS",
    "WERE", "BE", "BEEN", "ARE", "AM", "DO", "DID", "HAVE", "HAS",
    "HAD", "NOT", "FOR", "WITH", "FROM", "AS", "BY", "WHEN", "WHERE",
}
# Game-action words that become content hashtags when the clip says them.
CONTENT_WORDS = [
    "headshot", "clutch", "kill", "killed", "win", "won", "victory",
    "destroy", "destroyed", "snipe", "sniped", "epic", "insane", "crazy",
    "funny", "reaction", "comeback", "revenge", "glitch", "rage",
    "scream", "hype", "boom", "dead", "died", "shot", "sniper",
]
# Games recognised in a stream title, checked in this order (longest /
# most specific first — "gta vi" must beat "gta v").
GAMES = [
    ("gta vi", "GTA VI", "GTAVI"),
    ("gta 6", "GTA VI", "GTAVI"),
    ("gta v", "GTA V", "GTAV"),
    ("bgmi", "BGMI", "BGMI"),
    ("pubg", "PUBG", "PUBG"),
    ("fortnite", "Fortnite", "Fortnite"),
    ("valorant", "Valorant", "Valorant"),
    ("minecraft", "Minecraft", "Minecraft"),
    ("free fire", "Free Fire", "FreeFire"),
    ("call of duty", "Call of Duty", "CallOfDuty"),
    ("warzone", "Warzone", "Warzone"),
    ("apex", "Apex Legends", "ApexLegends"),
    ("counter-strike", "CS2", "CS2"),
    ("cs2", "CS2", "CS2"),
    ("fifa", "FIFA", "FIFA"),
    ("eafc", "EA FC", "EAFC"),
    ("roblox", "Roblox", "Roblox"),
    ("rocket league", "Rocket League", "RocketLeague"),
    ("dota", "Dota 2", "Dota2"),
    ("league of legends", "League of Legends", "LeagueOfLegends"),
]
GENERIC_TAGS = ["shorts", "gaming"]


def detect_game(stream_title: str) -> Dict[str, str]:
    """Find the game in the stream title. Returns display name + tag."""
    low = (stream_title or "").lower()
    for needle, display, tag in GAMES:
        if needle in low:
            return {"display": display, "tag": tag}
    return {"display": "", "tag": ""}


def _clean_word(word: str) -> str:
    return re.sub(r"[^A-Z0-9'!]", "", (word or "").upper())


def extract_hook(words: List[Dict]) -> str:
    """Best quotable phrase from the clip's real speech (3-6 words).

    Scores sliding windows by high-energy words and exclamations, and
    never returns a phrase that starts/ends with a filler word. Returns
    "" when there is nothing quotable.
    """
    clean = [_clean_word(w.get("word", "")) for w in (words or [])]
    raw = [(w.get("word", "") or "") for w in (words or [])]
    starts = [float(w.get("start", 0) or 0) for w in (words or [])]
    ends = [float(w.get("end", 0) or 0) for w in (words or [])]
    # Keep only words that survived cleaning, with their timing aligned.
    kept = [(c, r, s, e) for c, r, s, e in zip(clean, raw, starts, ends) if c]
    if len(kept) < 3:
        return ""

    best, best_score = "", -1
    # Longest windows first: on an exact tie, a complete thought beats a
    # fragment ("YEAH WHAT A LOVELY ENVIRONMENT." over "YEAH WHAT A LOVELY").
    for size in (6, 5, 4, 3):
        for i in range(0, len(kept) - size + 1):
            window = kept[i:i + size]
            phrase = [c for c, _r, _s, _e in window]
            if phrase[0] in FILLERS or phrase[-1] in FILLERS:
                continue
            # A hook is one continuous utterance — never span a long
            # pause (two separate sentences stitched together).
            gap_ok = True
            for j in range(1, size):
                if window[j][2] - window[j - 1][3] > 1.5:
                    gap_ok = False
                    break
            if not gap_ok:
                continue
            score = 0
            counts: Dict[str, int] = {}
            for w in phrase:
                base = w.rstrip("!")
                counts[base] = counts.get(base, 0) + 1
                if base in ENERGY_WORDS:
                    score += 4
                if w.endswith("!"):
                    score += 2
            # A phrase that just repeats itself ("OH GOD OH GOD! OH") is
            # not a hook — penalise every repeated word.
            for _w, c in counts.items():
                if c > 1:
                    score -= 6 * (c - 1)
            # Punchy length wins; long rambling phrases lose.
            score += 1 if size in (4, 5) else 0
            score -= 1 if size == 6 else 0
            # A phrase ending on a full stop is a complete sentence.
            if window[-1][1].strip().endswith("."):
                score += 3
            if score > best_score:
                best_score = score
                best = " ".join(phrase)
    if best_score <= 0:
        return ""
    return best


def _title_case(phrase: str) -> str:
    """'OH MY GOD, HEADSHOT!' -> 'Oh My God, Headshot!'"""
    out = []
    for w in phrase.split(" "):
        if w in ("I", "I'M", "I'VE", "I'LL", "I'D"):
            out.append(w if w == "I" else w.capitalize())
        else:
            out.append(w.capitalize())
    return " ".join(out)


def content_tags(words: List[Dict], limit: int = 3) -> List[str]:
    """Game-action words the clip actually says, as hashtags."""
    seen, tags = set(), []
    for w in (words or []):
        low = (w.get("word", "") or "").lower().strip(".,!?'\"")
        if low in CONTENT_WORDS and low not in seen:
            seen.add(low)
            tags.append(low)
            if len(tags) >= limit:
                break
    return tags


def _fallback_hook(stream_title: str, z_score: float, index: int) -> str:
    """No speech to quote: vary by how loud the peak was and by clip."""
    if z_score >= 3:
        openers = ["Insane peak moment", "Unbelievable peak moment",
                   "The loudest moment"]
    elif z_score >= 2:
        openers = ["Wild peak moment", "Huge peak moment",
                   "The crowd went wild"]
    else:
        openers = ["Peak moment", "Best moment", "Top moment"]
    return openers[index % len(openers)]


def generate_clip_metadata(
    words: List[Dict],
    stream_title: str,
    channel: str,
    moment: Dict,
    index: int,
    used_titles: Optional[set] = None,
) -> Dict[str, object]:
    """Build a unique title, description and tag list for one clip."""
    game = detect_game(stream_title or "")
    game_display = game["display"]
    game_tag = game["tag"]
    z_score = float((moment or {}).get("z_score", 0) or 0)
    streamer = (channel or "Creator").strip()

    hook = extract_hook(words)
    if not hook:
        hook = _fallback_hook(stream_title or "", z_score, index)
    hook_title = _title_case(hook)

    # Title: the hook first, then the game/stream context. Unique within
    # the job — a colliding hook falls back to the next-best phrase by
    # appending the peak number.
    context = f" | {game_display}" if game_display else f" | {streamer}"
    title = f'"{hook_title}"{context}'
    if len(title) > 95:
        title = f'"{hook_title}"'
    if used_titles is not None:
        base_title = title
        n = 2
        while title in used_titles:
            title = f"{base_title} (Part {n})"
            n += 1
        used_titles.add(title)

    # Content hashtags: game + words the clip says + the streamer.
    tags: List[str] = []
    if game_tag:
        tags.append(f"#{game_tag}")
    for t in content_tags(words):
        tags.append(f"#{t}")
    chan_tag = "#" + re.sub(r"[^A-Za-z0-9_]", "", streamer.replace(" ", ""))
    if chan_tag != "#" and chan_tag not in tags:
        tags.append(chan_tag)
    for t in GENERIC_TAGS:
        tags.append(f"#{t}")

    # Description: the real quote, what it is, and where it came from.
    # No brand tagline — the clip's own words are the description.
    where = f"{streamer} in {game_display}" if game_display else streamer
    desc_lines = [
        f'"{hook_title}" — {where}.',
        "",
        "Peak moment from the live stream"
        + (f" — {stream_title.strip()}" if (stream_title or "").strip() else "")
        + ".",
        "",
        " ".join(tags),
    ]
    description = "\n".join(desc_lines)

    return {"title": title, "description": description, "tags": tags}
