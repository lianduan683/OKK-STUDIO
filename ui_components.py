import tkinter as tk

from ui_theme import BORDER, GREEN, INPUT, MUTED, PANEL, SURFACE, TEXT, UI_FONT

__all__ = ["Card", "SectionHeader", "StatusPill", "EmptyState"]


class Card(tk.Frame):
    def __init__(self, parent, bg=PANEL, padding=18, border=True):
        super().__init__(
            parent,
            bg=bg,
            padx=padding,
            pady=padding,
            bd=0,
            relief="flat",
            highlightthickness=1 if border else 0,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
        )


def SectionHeader(parent, title, subtitle="", accent=None, bg=None):
    background = PANEL if bg is None else bg
    frame = tk.Frame(parent, bg=background, bd=0, highlightthickness=0)

    if accent is not None:
        accent_label = tk.Label(
            frame,
            text=accent,
            bg=background,
            fg=MUTED,
            font=(UI_FONT, 9),
            bd=0,
            highlightthickness=0,
        )
        accent_label.pack(side="right", anchor="n")

    tk.Label(
        frame,
        text=title,
        bg=background,
        fg=TEXT,
        font=(UI_FONT, 14, "bold"),
        anchor="w",
        bd=0,
        highlightthickness=0,
    ).pack(side="top", fill="x", anchor="w")

    if subtitle:
        tk.Label(
            frame,
            text=subtitle,
            bg=background,
            fg=MUTED,
            font=(UI_FONT, 10),
            anchor="w",
            justify="left",
            bd=0,
            highlightthickness=0,
        ).pack(side="top", fill="x", anchor="w", pady=4)

    return frame


def StatusPill(parent, text, color=GREEN, bg=SURFACE):
    return tk.Label(
        parent,
        text=text,
        bg=bg,
        fg=color,
        font=(UI_FONT, 9, "bold"),
        padx=12,
        pady=6,
        bd=0,
        relief="flat",
        highlightthickness=0,
    )


def EmptyState(parent, title, detail, bg=INPUT):
    frame = tk.Frame(
        parent,
        bg=bg,
        padx=24,
        pady=24,
        bd=0,
        relief="flat",
        highlightthickness=0,
    )
    tk.Label(
        frame,
        text=title,
        bg=bg,
        fg=TEXT,
        font=(UI_FONT, 12, "bold"),
        anchor="center",
        justify="center",
        bd=0,
        highlightthickness=0,
    ).pack(fill="x")
    tk.Label(
        frame,
        text=detail,
        bg=bg,
        fg=MUTED,
        font=(UI_FONT, 10),
        anchor="center",
        justify="center",
        wraplength=420,
        bd=0,
        highlightthickness=0,
    ).pack(fill="x", pady=8)
    return frame
