"""Shared dark visual theme for the OKK exporter."""

import tkinter as tk
from tkinter import ttk


BG = "#18191b"
SURFACE = "#202124"
PANEL = "#242629"
PANEL_ALT = "#2c2e32"
INPUT = "#191a1c"
BORDER = "#3b3e43"
TEXT = "#e1e3e6"
MUTED = "#969ba3"
PURPLE = "#4c87ac"
PURPLE_BRIGHT = "#69a8ce"
PINK = "#58a6d6"
GREEN = "#76b99a"
BLUE = "#86adca"
RED = "#ed746d"
GLASS_HIGHLIGHT = "#484b51"
GLASS_EDGE = "#42454a"
GLASS_SHADOW = "#18191b"
UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Cascadia Mono"

__all__ = [
    "BG", "SURFACE", "PANEL", "PANEL_ALT", "INPUT", "BORDER", "TEXT",
    "MUTED", "PURPLE", "PURPLE_BRIGHT", "PINK", "GREEN", "BLUE", "RED",
    "GLASS_HIGHLIGHT", "GLASS_EDGE", "GLASS_SHADOW", "UI_FONT", "MONO_FONT",
    "configure_ttk_style",
]



def configure_ttk_style(root: tk.Misc) -> ttk.Style:
    """Apply the complete OKK widget theme and return its ttk style object."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.option_add("*Font", (UI_FONT, 10))
    root.option_add("*TCombobox*Listbox.background", INPUT)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", PURPLE)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.font", (UI_FONT, 10))

    disabled_bg = "#18212c"
    disabled_fg = "#657386"
    button_hover = "#2a384b"
    button_pressed = "#33445a"

    style.configure("TFrame", background=BG)
    style.configure("Surface.TFrame", background=SURFACE)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("PanelAlt.TFrame", background=PANEL_ALT)

    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Surface.TLabel", background=SURFACE, foreground=TEXT)
    style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
    style.configure("PanelAlt.TLabel", background=PANEL_ALT, foreground=TEXT)
    style.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
    style.configure(
        "Title.TLabel", background=BG, foreground=TEXT,
        font=(UI_FONT, 24, "bold"),
    )
    style.configure(
        "Section.TLabel", background=PANEL, foreground=TEXT,
        font=(UI_FONT, 15, "bold"),
    )

    style.configure(
        "TButton", background=PANEL_ALT, foreground=TEXT,
        font=(UI_FONT, 10), padding=(18, 11), borderwidth=1,
        bordercolor=BORDER, darkcolor=BORDER, lightcolor=BORDER,
        focuscolor=PURPLE_BRIGHT, focusthickness=1, relief="flat",
    )
    style.map(
        "TButton",
        background=[("disabled", disabled_bg), ("pressed", button_pressed),
                    ("active", button_hover)],
        foreground=[("disabled", disabled_fg), ("active", "#ffffff")],
        bordercolor=[("disabled", "#273242"), ("focus", PURPLE_BRIGHT),
                     ("active", "#4a5b72")],
        lightcolor=[("focus", PURPLE_BRIGHT)],
        darkcolor=[("focus", PURPLE_BRIGHT)],
    )

    style.configure(
        "Compact.TButton", background=PANEL_ALT, foreground=TEXT,
        font=(UI_FONT, 9, "bold"), padding=(10, 7), borderwidth=1,
        bordercolor=BORDER, darkcolor=BORDER, lightcolor=BORDER,
        focuscolor=PURPLE_BRIGHT, focusthickness=1, relief="flat",
    )
    style.map(
        "Compact.TButton",
        background=[("disabled", disabled_bg), ("pressed", button_pressed),
                    ("active", button_hover)],
        foreground=[("disabled", disabled_fg), ("active", "#ffffff")],
        bordercolor=[("disabled", "#273242"), ("focus", PURPLE_BRIGHT),
                     ("active", "#4a5b72")],
    )

    style.configure(
        "Dock.TButton", background=PANEL_ALT, foreground=MUTED,
        font=(UI_FONT, 8, "bold"), padding=(8, 3), borderwidth=0,
        focuscolor=PURPLE_BRIGHT, focusthickness=0, relief="flat",
    )
    style.map(
        "Dock.TButton",
        background=[("pressed", "#3b4a60"), ("active", "#334255")],
        foreground=[("active", "#ffffff")],
    )

    style.configure(
        "PanelToggle.TButton", background=PANEL, foreground=MUTED,
        font=(UI_FONT, 9, "bold"), padding=(12, 5), borderwidth=1,
        bordercolor=BORDER, darkcolor=BORDER, lightcolor=BORDER,
        focuscolor=PURPLE_BRIGHT, focusthickness=1, relief="flat",
    )
    style.map(
        "PanelToggle.TButton",
        background=[("pressed", button_pressed), ("active", button_hover)],
        foreground=[("active", TEXT)],
        bordercolor=[("focus", PURPLE_BRIGHT), ("active", "#4a5b72")],
    )
    style.configure(
        "PanelToggleActive.TButton", background=PANEL_ALT, foreground=PURPLE_BRIGHT,
        font=(UI_FONT, 9, "bold"), padding=(12, 5), borderwidth=1,
        bordercolor="#6d5aa8", darkcolor="#6d5aa8", lightcolor="#6d5aa8",
        focuscolor=PURPLE_BRIGHT, focusthickness=1, relief="flat",
    )
    style.map(
        "PanelToggleActive.TButton",
        background=[("pressed", "#302a49"), ("active", "#24304a")],
        foreground=[("active", "#d8ccff")],
        bordercolor=[("focus", PURPLE_BRIGHT), ("active", PURPLE)],
    )

    style.configure(
        "Accent.TButton", background=PURPLE, foreground="#ffffff",
        font=(UI_FONT, 12, "bold"), padding=(26, 12), borderwidth=1,
        bordercolor=PURPLE, darkcolor=PURPLE, lightcolor=PURPLE,
        focuscolor=PURPLE_BRIGHT, focusthickness=1, relief="flat",
    )
    style.map(
        "Accent.TButton",
        background=[("disabled", "#3b3550"), ("pressed", "#7445dc"),
                    ("active", PURPLE_BRIGHT)],
        foreground=[("disabled", "#9b96ad"), ("active", "#11101a")],
        bordercolor=[("disabled", "#3b3550"), ("focus", "#d6c9ff"),
                     ("active", PURPLE_BRIGHT)],
        lightcolor=[("focus", "#d6c9ff")],
        darkcolor=[("focus", "#d6c9ff")],
    )

    style.configure(
        "Secondary.TButton", background=PANEL, foreground=PURPLE_BRIGHT,
        font=(UI_FONT, 10, "bold"), padding=(18, 10), borderwidth=1,
        bordercolor="#594b80", darkcolor="#594b80", lightcolor="#594b80",
        focuscolor=PURPLE_BRIGHT, focusthickness=1, relief="flat",
    )
    style.map(
        "Secondary.TButton",
        background=[("disabled", disabled_bg), ("pressed", "#302a49"),
                    ("active", "#29253e")],
        foreground=[("disabled", disabled_fg), ("active", "#d8ccff")],
        bordercolor=[("disabled", "#273242"), ("focus", PURPLE_BRIGHT),
                     ("active", "#77649f")],
    )

    style.configure(
        "Danger.TButton", background="#38212a", foreground="#ffc4cc",
        font=(UI_FONT, 10), padding=(20, 11), borderwidth=1,
        bordercolor="#693543", darkcolor="#693543", lightcolor="#693543",
        focuscolor=RED, focusthickness=1, relief="flat",
    )
    style.map(
        "Danger.TButton",
        background=[("disabled", disabled_bg), ("pressed", "#582b38"),
                    ("active", "#482731")],
        foreground=[("disabled", disabled_fg), ("active", "#ffe0e5")],
        bordercolor=[("disabled", "#273242"), ("focus", RED),
                     ("active", "#914556")],
    )

    style.configure(
        "TCombobox", fieldbackground=INPUT, background=PANEL_ALT,
        foreground=TEXT, arrowcolor=MUTED, bordercolor=BORDER,
        lightcolor=BORDER, darkcolor=BORDER, focuscolor=PURPLE_BRIGHT,
        font=(UI_FONT, 10), padding=(11, 9), borderwidth=1,
        arrowsize=14, relief="flat",
    )
    style.map(
        "TCombobox",
        fieldbackground=[("disabled", disabled_bg), ("readonly", INPUT),
                         ("focus", INPUT)],
        background=[("disabled", disabled_bg), ("pressed", button_pressed),
                    ("active", button_hover)],
        foreground=[("disabled", disabled_fg), ("readonly", TEXT)],
        arrowcolor=[("disabled", disabled_fg), ("active", PURPLE_BRIGHT),
                    ("focus", PURPLE_BRIGHT)],
        bordercolor=[("disabled", "#273242"), ("focus", PURPLE_BRIGHT),
                     ("active", "#52647c")],
        lightcolor=[("focus", PURPLE_BRIGHT)],
        darkcolor=[("focus", PURPLE_BRIGHT)],
        selectbackground=[("readonly", INPUT)],
        selectforeground=[("readonly", TEXT)],
    )

    style.configure(
        "Horizontal.TProgressbar", troughcolor=INPUT, background=PURPLE,
        bordercolor=INPUT, lightcolor=PURPLE, darkcolor=PURPLE,
        borderwidth=0, thickness=8,
    )
    style.configure(
        "Metric.Horizontal.TProgressbar", troughcolor="#202c3b",
        background=PURPLE_BRIGHT, bordercolor="#202c3b",
        lightcolor=PURPLE_BRIGHT, darkcolor=PURPLE_BRIGHT,
        borderwidth=0, thickness=6,
    )

    style.configure(
        "TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0),
    )
    style.configure(
        "TNotebook.Tab", background=SURFACE, foreground=MUTED,
        font=(UI_FONT, 11, "bold"), padding=(24, 13), borderwidth=0,
        focuscolor=PURPLE_BRIGHT,
    )
    style.map(
        "TNotebook.Tab",
        background=[("disabled", disabled_bg), ("selected", PANEL_ALT),
                    ("active", PANEL)],
        foreground=[("disabled", disabled_fg), ("selected", PURPLE_BRIGHT),
                    ("active", TEXT)],
        bordercolor=[("focus", PURPLE_BRIGHT)],
        expand=[("selected", (0, 0, 0, 1))],
    )

    for orientation in ("Vertical", "Horizontal"):
        name = f"{orientation}.TScrollbar"
        style.configure(
            name, background=PANEL_ALT, troughcolor=INPUT,
            bordercolor=INPUT, darkcolor=INPUT, lightcolor=INPUT,
            arrowcolor=MUTED, borderwidth=0, relief="flat", arrowsize=13,
        )
        style.map(
            name,
            background=[("disabled", disabled_bg), ("pressed", "#3b4b60"),
                        ("active", "#304055")],
            arrowcolor=[("disabled", disabled_fg), ("active", TEXT)],
            troughcolor=[("disabled", BG)],
        )

    style.configure(
        "TPanedwindow", background=BG, borderwidth=0, sashwidth=6,
        sashrelief="flat", sashpad=2,
    )
    style.configure("Sash", background=BORDER, borderwidth=0, relief="flat")
    style.map("Sash", background=[("active", PURPLE), ("pressed", PURPLE_BRIGHT)])

    style.configure(
        "Pool.Treeview", background=INPUT, fieldbackground=INPUT, foreground=TEXT,
        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
        rowheight=26, font=(UI_FONT, 9), relief="flat", borderwidth=0,
    )
    style.map(
        "Pool.Treeview",
        background=[("selected", PANEL_ALT)],
        foreground=[("selected", "#ffffff")],
    )
    style.configure(
        "Pool.Treeview.Heading", background=PANEL_ALT, foreground=MUTED,
        font=(UI_FONT, 9, "bold"), borderwidth=0, relief="flat", padding=(6, 6),
    )
    style.map(
        "Pool.Treeview.Heading",
        background=[("active", PANEL), ("pressed", PANEL)],
        foreground=[("active", TEXT)],
    )

    return style
