"""Thème visuel : fond olive très sombre, grands onglets rectangulaires (actif en blanc), accent brun,
en-têtes de tableau gris. Palette propre à ce projet, dans l'esprit de l'interface d'ArrayIntelligence."""
import tkinter.font as tkfont
from tkinter import ttk

# --- palette ---------------------------------------------------
BG = "#2c312b"  # fond de fenêtre / marges
PANEL = "#373936"  # panneaux et zone 3D
PANEL_2 = "#474c46"  # sous-onglets, barres
FIELD = "#5b5f5a"  # champs de saisie
BORDER = "#747973"  # filets des boutons
EDGE = "#202420"  # contour très sombre des panneaux
TAB = "#646965"  # onglet inactif
TAB_HI = "#747973"
BTN = "#4e534d"  # bouton standard
BTN_HI = "#5d625c"
ACCENT = "#886e4b"  # brun AI
ACCENT_HI = "#9b7f58"
HEADER = "#838281"  # en-tête de tableau
TEXT = "#e4e6e2"
MUTED = "#9ca19b"
BROWN = "#a58a64"  # titres de section
WHITE = "#ffffff"
ERROR = "#d9694f"

VIEWPORT = "#373936"
VIEWPORT_RGB = (0x37, 0x39, 0x36)
AXIS_X = (255, 70, 70)
AXIS_Y = (80, 220, 80)
AXIS_Z = (90, 140, 255)
HEIGHT_RAMP = ((92, 120, 112), (150, 172, 138), (226, 208, 164))  # bas -> haut du modèle sans texture
CROP = (255, 170, 40)
CROP_DIM = (150, 105, 30)
CLOSE = (90, 230, 90)

_PREFERRED = ("Myriad Pro", "Roboto", "Open Sans", "Helvetica Neue", "Segoe UI", "Arial")


def family(root):
    have = set(tkfont.families(root))
    return next((f for f in _PREFERRED if f in have), "TkDefaultFont")


def apply(root):
    """Applique le thème à toute l'application (ttk 'clam', seul thème qui accepte les couleurs partout)."""
    fam = family(root)
    base = (fam, 12)
    root.configure(bg=BG)
    st = ttk.Style(root)
    st.theme_use("clam")
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
        tkfont.nametofont(name).configure(family=fam, size=12)  # (sans règle « *Font » : elle écraserait les styles)
    # listes déroulantes des combobox
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", WHITE)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", WHITE)
    root.option_add("*TCombobox*Listbox.font", base)

    st.configure(".", background=PANEL, foreground=TEXT, fieldbackground=FIELD, bordercolor=EDGE,
                 lightcolor=PANEL, darkcolor=PANEL, troughcolor=BG, focuscolor=PANEL, font=base,
                 insertcolor=WHITE, selectbackground=ACCENT, selectforeground=WHITE)
    st.configure("TFrame", background=PANEL)
    st.configure("Window.TFrame", background=BG)
    st.configure("Bar.TFrame", background=PANEL_2)
    st.configure("Edge.TFrame", background=EDGE)
    st.configure("View.TFrame", background=VIEWPORT)

    st.configure("TLabel", background=PANEL, foreground=TEXT)
    st.configure("Muted.TLabel", foreground=MUTED, font=(fam, 11))
    st.configure("Title.TLabel", foreground=WHITE, font=(fam, 15, "bold"))
    st.configure("Section.TLabel", foreground=BROWN, font=(fam, 12, "bold"))
    st.configure("Header.TLabel", background=HEADER, foreground=WHITE, anchor="center", padding=(6, 3),
                 font=(fam, 11, "bold"))
    st.configure("Cell.TLabel", background=FIELD, foreground=WHITE, anchor="center", padding=(6, 3))
    st.configure("Console.TLabel", background=VIEWPORT, foreground=MUTED, font=(fam, 11))
    st.configure("Logo.TLabel", background=TAB, foreground=WHITE, font=(fam, 13, "italic"), anchor="center")
    st.configure("Warn.TLabel", foreground=ERROR)

    st.configure("TButton", background=BTN, foreground=TEXT, bordercolor=BORDER, relief="flat", padding=(10, 5),
                 anchor="center", focusthickness=0)
    st.map("TButton", background=[("disabled", PANEL_2), ("pressed", ACCENT), ("active", BTN_HI)],
           foreground=[("disabled", "#767a74")], bordercolor=[("active", "#9aa09a")])
    st.configure("Accent.TButton", background=ACCENT, foreground=WHITE, bordercolor=ACCENT, padding=(10, 8),
                 font=(fam, 13, "bold"))
    st.map("Accent.TButton", background=[("disabled", "#5a5042"), ("pressed", "#6f5a3d"), ("active", ACCENT_HI)],
           foreground=[("disabled", "#a79c8c")])
    st.configure("Tab.TButton", background=TAB, foreground=WHITE, bordercolor=EDGE, relief="flat",
                 padding=(0, 11), font=(fam, 14))
    st.map("Tab.TButton", background=[("active", TAB_HI)])
    st.configure("TabOn.TButton", background=WHITE, foreground=ACCENT, bordercolor=EDGE, relief="flat",
                 padding=(0, 11), font=(fam, 14, "bold"))
    st.map("TabOn.TButton", background=[("active", WHITE)], foreground=[("active", ACCENT)])
    st.configure("Tool.TButton", background=BTN, foreground=TEXT, bordercolor=BORDER, padding=(9, 3),
                 font=(fam, 11))
    st.map("Tool.TButton", background=[("pressed", ACCENT), ("active", BTN_HI)])
    st.configure("Toolbutton", background=PANEL_2, foreground=TEXT, bordercolor=BORDER, padding=(9, 3),
                 relief="flat", font=(fam, 11))
    st.map("Toolbutton", background=[("selected", ACCENT), ("active", BTN_HI)],
           foreground=[("selected", WHITE)])

    st.configure("TCheckbutton", background=PANEL, foreground=TEXT, indicatorbackground=FIELD,
                 indicatorforeground=WHITE, indicatormargin=(0, 0, 6, 0), upperbordercolor=BORDER,
                 lowerbordercolor=BORDER)
    st.map("TCheckbutton", indicatorbackground=[("selected", ACCENT), ("pressed", ACCENT_HI)],
           background=[("active", PANEL)])

    for name in ("TEntry", "TSpinbox", "TCombobox"):
        st.configure(name, fieldbackground=FIELD, background=BTN, foreground=WHITE, bordercolor=EDGE,
                     insertcolor=WHITE, arrowcolor=TEXT, padding=(5, 3), selectbackground=ACCENT,
                     selectforeground=WHITE)
    st.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", WHITE)],
           background=[("active", BTN_HI)], arrowcolor=[("active", WHITE)])
    st.map("TSpinbox", background=[("active", BTN_HI)], arrowcolor=[("active", WHITE)])
    st.map("TEntry", bordercolor=[("focus", ACCENT)])

    st.configure("TScrollbar", background=BTN, troughcolor=BG, bordercolor=BG, arrowcolor=TEXT, relief="flat",
                 gripcount=0)
    st.map("TScrollbar", background=[("active", BTN_HI)])
    st.configure("TSeparator", background=EDGE)
    st.configure("Horizontal.TProgressbar", troughcolor=BG, background=ACCENT, bordercolor=BG,
                 lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)
    return fam
