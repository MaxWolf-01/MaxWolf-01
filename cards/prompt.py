#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = ["tyro"]
# ///
"""Render the shell prompts and directory names of the README's terminal as SVGs.

A README can't colour text, so each prompt and each directory name is an image
in max's jovial prompt and ghibli palette, sized to the text of GitHub's
profile page. With --animate, the prompts appear one after another and type
their commands out, as if the session were running.

Examples:

    uv run cards/prompt.py               # still prompts into cards/term/
    uv run cards/prompt.py --animate     # prompts that type themselves out
"""

import shutil
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import tyro

HERE = Path(__file__).parent

# The session, top to bottom: (file stem, command, the directory it names, output lines).
SESSION = [
    ("pwd", "pwd", None, ["/home/max"]),
    *[(f"tree-{d}", f"tree {d}/", f"{d}/", []) for d in ("research", "apps", "tools", "open-source", "fun")],
    ("card", "uv run cards/card.py", None, []),
]
DIRS = ["research", "apps", "tools", "open-source", "fun", *map(str, range(2021, 2027))]

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
    "cursor": ("#8a6a2a", "#b89d65"),
}

# Seconds: before the first prompt, a prompt idling before it types, per key, before enter, after output.
START, IDLE, KEY, ENTER, NEXT = 0.4, 0.2, 0.035, 0.15, 0.08


@dataclass
class Args:
    animate: bool = False
    """Prompts appear one after another and type their commands; viewers who ask for reduced motion get the still version."""
    out: Path = HERE / "term"
    """Directory the SVGs are written to; emptied first."""


def style(animate: bool) -> str:
    def inks(i):
        return " ".join(f".{k} {{ fill: {v[i]} }}" for k, v in INK.items())

    css = (f"text {{ font-family: {FONT}; font-size: {SIZE}px; white-space: pre }} .b {{ font-weight: 700 }} "
           f"{inks(0)} @media (prefers-color-scheme: dark) {{ {inks(1)} }}")
    if animate:
        css += (f" @keyframes show {{ from {{ opacity: 0 }} to {{ opacity: 1 }} }}"
                f" @keyframes key {{ from {{ font-size: 0 }} to {{ font-size: {SIZE}px }} }}"
                " @keyframes on { from, to { opacity: 1 } }"
                " @keyframes blink { 50% { opacity: 0 } }"
                " .appear { animation: show 1ms linear both } .k { animation: key 1ms linear both }"
                " .cur { opacity: 0 } .idle .cur { opacity: 1 }"
                " @media (prefers-reduced-motion: reduce) { * { animation: none !important } }")
    else:
        css += " .cur { opacity: 0 } .idle .cur { opacity: 1 }"
    return css


def svg(lines: list[str], rows: int, width_chars: int, animate: bool, cls: str = "") -> str:
    w, h = round(width_chars * CHAR + 8), round(rows * LINE)
    body = "\n".join(lines)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" class="{cls}">'
            f"<style>{style(animate)}</style>\n{body}\n</svg>\n")


def at(seconds: float) -> str:
    return f' style="animation-delay: {seconds:.2f}s"'


def prompt_line(y: float) -> str:
    parts = [("normal", "╭─["), ("host", "github"), ("normal", "] "), ("conj", "as "), ("user", "max"),
             ("conj", " in "), ("path b", "~")]
    return f'<text x="0" y="{y}">' + "".join(f'<tspan class="{c}">{escape(t)}</tspan>' for c, t in parts) + "</text>"


def command_line(y: float, command: str, directory: str | None, t0: float | None) -> tuple[str, float]:
    """The second prompt line, with the command typed from t0 on when animated; returns the line and when it ends."""
    head = '<tspan class="normal">╰─➤ </tspan>'
    split = len(command) - len(directory) if directory else len(command)
    keys, t = [], (t0 or 0) + IDLE
    for i, ch in enumerate(command):
        cls = "dir b" if i >= split else "text"
        keys.append(f'<tspan class="{cls} k"{at(t) if t0 is not None else ""}>{escape(ch)}</tspan>')
        t += KEY
    cursor_anim = f' style="animation: on {t - t0:.2f}s linear {t0:.2f}s"' if t0 is not None else ""
    cursor = f'<tspan class="cursor cur"{cursor_anim}>▋</tspan>'
    keys_markup = "".join(keys) if t0 is not None else "".join(
        f'<tspan class="{c}">{escape(s)}</tspan>' for c, s in (("text", command[:split]), ("dir b", command[split:])) if s)
    return f'<text x="0" y="{y}">{head}{keys_markup}{cursor}</text>', t


def main(args: Args):
    shutil.rmtree(args.out, ignore_errors=True)
    args.out.mkdir(parents=True)
    t = START
    for stem, command, directory, output in SESSION:
        t0 = t if args.animate else None
        first = prompt_line(BASELINE)
        second, typed = command_line(LINE + BASELINE, command, directory, t0)
        enter = typed + ENTER
        out = [f'<text x="0" y="{(2 + i) * LINE + BASELINE}" class="text"><tspan class="appear"'
               f'{at(enter) if args.animate else ""}>{escape(line)}</tspan></text>' for i, line in enumerate(output)]
        group = f'<g class="appear"{at(t0)}>' if args.animate else "<g>"
        lines = [group, first, second, *out, "</g>"]
        width = max(len("╭─[github] as max in ~"), 4 + len(command) + 1, *map(len, output))
        (args.out / f"prompt-{stem}.svg").write_text(svg(lines, 2 + len(output), width, args.animate))
        t = enter + NEXT
    # The prompt the session ends on, waiting, its cursor blinking.
    blink = f' style="animation: blink 1.1s step-end {t:.2f}s infinite"' if args.animate else ""
    idle = [f'<g class="appear"{at(t)}>' if args.animate else "<g>", prompt_line(BASELINE),
            f'<text x="0" y="{LINE + BASELINE}"><tspan class="normal">╰─➤ </tspan><tspan class="cursor cur"{blink}>▋</tspan></text>',
            "</g>"]
    (args.out / "prompt-idle.svg").write_text(svg(idle, 2, 24, args.animate, cls="idle"))
    for d in DIRS:
        line = f'<text x="0" y="{BASELINE}" class="dir b">{escape(d)}/</text>'
        (args.out / f"dir-{d}.svg").write_text(svg([line], 1, len(d) + 1, False))
    print(args.out)
    if args.animate:
        print(f"the card's command is entered at {t - NEXT:.2f}s: pass that to card.py --appear-after")


if __name__ == "__main__":
    main(tyro.cli(Args, description=__doc__))
