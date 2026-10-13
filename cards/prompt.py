#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = ["tyro"]
# ///
"""Render the shell prompts and directory names of the README's terminal as SVGs.

A README can't colour text, so each prompt and each directory name is an image
in max's jovial prompt and ghibli palette, sized to the text of GitHub's
profile page. The session's last prompt is live: with --live it types the
comments in COMMENTS one after another, deleting each before the next.

Examples:

    uv run cards/prompt.py           # prompts into cards/term/, the last one a blinking cursor
    uv run cards/prompt.py --live    # the last prompt types comments
"""

import shutil
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import tyro

HERE = Path(__file__).parent

# The session's commands, top to bottom: (file stem, command, the directory it names).
COMMANDS = [
    ("pinky", "pinky -l max", None),
    *[(f"tree-{d}", f"tree {d}/", f"{d}/") for d in ("research", "apps", "tools", "open-source", "fun")],
    ("card", "uv run cards/card.py", None),
]
# Pixels of space above a prompt that follows plain output; a blank line is too much.
SPACE_ABOVE = {"tree-research": 6}
DIRS = ["research", "apps", "tools", "open-source", "fun", *map(str, range(2021, 2027))]
# What the live prompt types, in a loop.
COMMENTS = [
    "# click a command to run it",
    "# intelligence: grown, not built",
    "# more at mwolf.dev",
]

# GitHub's profile page sets its code blocks in 11.9px type on 1.45 line height.
SIZE, LINE, BASELINE = 11.9, 17.25, 12.7
CHAR = 0.62 * SIZE  # wide enough for any monospace face, so nothing is clipped
FONT = 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace'

# Terminal palette slots of the prompt (nix/home/terminal-colors.nix), with darker
# inks for GitHub's light theme. Text takes GitHub's own code colour, to match the
# rest of the block.
INK = {  # role: (light, dark)
    "normal": ("#8f6235", "#c89a6f"),   # slot 13: brackets, arrow
    "host": ("#3f6f75", "#7ca3a8"),     # slot 6
    "conj": ("#4b6a33", "#6b8e50"),     # slot 2: as, in
    "user": ("#8c5a3c", "#a67c52"),     # slot 9
    "path": ("#86672a", "#d4b77a"),     # slot 3, bold
    "dir": ("#2b7a8c", "#5ba5b7"),      # slot 12, bold, as ls colours directories
    "text": ("#1f2328", "#f0f6fc"),     # GitHub's code text
    "comment": ("#6e6259", "#8b8178"),
    "cursor": ("#8a6a2a", "#b89d65"),
}

# Seconds: per key typed, per key deleted, a comment shown in full, the empty prompt between two.
KEY, DELETE, HOLD, GAP = 0.06, 0.025, 2.6, 0.7


@dataclass
class Args:
    live: bool = False
    """The last prompt types COMMENTS in a loop; viewers who ask for reduced motion see the first one, still."""
    out: Path = HERE / "term"
    """Directory the SVGs are written to; emptied first."""


def style(extra: str = "") -> str:
    def inks(i):
        return " ".join(f".{k} {{ fill: {v[i]} }}" for k, v in INK.items())

    return (f"text {{ font-family: {FONT}; font-size: {SIZE}px; white-space: pre }} .b {{ font-weight: 700 }} "
            f"{inks(0)} @media (prefers-color-scheme: dark) {{ {inks(1)} }}{extra}")


def svg(lines: list[str], rows: int, width_chars: int, extra_style: str = "", space_above: int = 0) -> str:
    w, h = round(width_chars * CHAR + 8), round(rows * LINE) + space_above
    body = "\n".join(lines)
    if space_above:
        body = f'<g transform="translate(0 {space_above})">{body}</g>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f"<style>{style(extra_style)}</style>\n{body}\n</svg>\n")


def spans(parts: list[tuple[str, str]]) -> str:
    return "".join(f'<tspan class="{c}">{escape(t)}</tspan>' for c, t in parts if t)


PS1 = [("normal", "╭─["), ("host", "github"), ("normal", "] "), ("conj", "as "), ("user", "max"),
       ("conj", " in "), ("path b", "~")]
PS2 = ("normal", "╰─➤ ")
BLINK = (" @keyframes blink { 50% { opacity: 0 } } .cur { animation: blink 1.1s step-end infinite }"
         " @media (prefers-reduced-motion: reduce) { * { animation: none !important } }")


def prompt(command: str, directory: str | None, space_above: int = 0) -> str:
    split = len(command) - len(directory) if directory else len(command)
    lines = [f'<text x="0" y="{BASELINE}">{spans(PS1)}</text>',
             f'<text x="0" y="{LINE + BASELINE}">'
             f'{spans([PS2, ("text", command[:split]), ("dir b", command[split:])])}</text>']
    return svg(lines, 2, max(22, 4 + len(command)), space_above=space_above)


def live_prompt(comments: list[str]) -> str:
    """The last prompt: each comment typed, held, deleted, then the next, forever.

    Every character is its own tspan, shrunk to font size 0 while it is not
    typed, so the cursor after them all sits right after the visible text.
    """
    timeline, t = [], GAP
    for comment in comments:
        typed = [t + i * KEY for i in range(len(comment))]
        shown = typed[-1] + KEY + HOLD
        deleted = [shown + (len(comment) - 1 - i) * DELETE for i in range(len(comment))]
        timeline.append((comment, typed, deleted))
        t = shown + len(comment) * DELETE + GAP
    period = t
    rules, chars = [], []
    for k, (comment, typed, deleted) in enumerate(timeline):
        for i, ch in enumerate(comment):
            name = f"c{k}_{i}"
            on, off = 100 * typed[i] / period, 100 * deleted[i] / period
            rules.append(f"@keyframes {name} {{ 0% {{ font-size: 0 }} {on:.3f}% {{ font-size: {SIZE}px }} "
                         f"{off:.3f}% {{ font-size: 0 }} }}")
            # Without motion, the first comment stands typed out and the rest stay hidden.
            still = f"font-size: {SIZE if k == 0 else 0}px;"
            chars.append(f'<tspan class="comment" style="{still} animation: {name} {period:.2f}s step-end infinite">'
                         f"{escape(ch)}</tspan>")
    lines = [f'<text x="0" y="{BASELINE}">{spans(PS1)}</text>',
             f'<text x="0" y="{LINE + BASELINE}">{spans([PS2])}{"".join(chars)}'
             '<tspan class="cursor cur">▋</tspan></text>']
    return svg(lines, 2, 4 + max(map(len, comments)) + 2, " " + " ".join(rules) + BLINK)


def idle_prompt() -> str:
    lines = [f'<text x="0" y="{BASELINE}">{spans(PS1)}</text>',
             f'<text x="0" y="{LINE + BASELINE}">{spans([PS2])}<tspan class="cursor cur">▋</tspan></text>']
    return svg(lines, 2, 24, BLINK)


def main(args: Args):
    shutil.rmtree(args.out, ignore_errors=True)
    args.out.mkdir(parents=True)
    for stem, command, directory in COMMANDS:
        (args.out / f"prompt-{stem}.svg").write_text(prompt(command, directory, SPACE_ABOVE.get(stem, 0)))
    (args.out / "prompt-idle.svg").write_text(live_prompt(COMMENTS) if args.live else idle_prompt())
    for d in DIRS:
        line = f'<text x="0" y="{BASELINE}" class="dir b">{escape(d)}/</text>'
        (args.out / f"dir-{d}.svg").write_text(svg([line], 1, len(d) + 1))
    print(args.out)


if __name__ == "__main__":
    main(tyro.cli(Args, description=__doc__))
