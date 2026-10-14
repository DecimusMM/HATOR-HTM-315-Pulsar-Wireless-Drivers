#!/usr/bin/env python3
"""
pulsar_gui.py - tkinter front end for pulsar.py (HATOR Pulsar Wireless, 258a:002e / 002f)

Needs only the Arch package `tk` (python's tkinter). All device access goes through pulsar.py.

Design: `Session` holds all logic (no GUI imports) so it can be unit-tested without a display;
`App` is a thin tkinter layer. Every write is a read-modify-write of the 146-byte profile block, preceded
by a backup under ~/.config/pulsar-linux/, exactly like the CLI. Only fields you actually changed are touched.
"""
import os, struct, sys, time

import pulsar as P
import pulsar_macros as M

MAX_UI_STAGES = 7          # Cfg.ini lists 7 DPI stages for this model (the block has room for 8)

# Matrix slot labels. 1..6 are hardware/vendor-artwork confirmed; 7..10 are Cfg.ini slots with unknown physical meaning.
SLOT_LABELS = {1: "Left button", 2: "Right button", 3: "Wheel click", 4: "Side button (back)",
               5: "Side button (forward)", 6: "Button under wheel",
               7: "DPI button (bottom of the mouse)", 8: "Report-rate button(bottom of the mouse)"}
# Per-effect LED behaviour as measured on the real mouse (see hardware report).
#   LED_SPEED   : usable raw speed range (min, max). Effects not listed have no speed control.
#   LED_COLOURS : effects whose colour(s) the firmware actually honours. Brightness does nothing on any effect -> not exposed.
LED_SPEED = {1: (1, 4), 3: (1, 12), 4: (1, 4), 5: (1, 4), 7: (0, 3), 8: (1, 4), 9: (0, 5)}
LED_COLOURS = {3, 7}
LED_SPEED_HINT = {1: "1 = slowest ... 4 = fastest", 3: "1-12 (13-15 would reset to default)", 4: "1 = slowest ... 4 = fastest",
                  5: "1 = slowest ... 4 = fastest", 7: "0-3", 8: "1-4", 9: "0-5 (higher values just give max speed)"}
UI_SLOTS = range(1, 9)     # slots 9/10 have no physical button (user-confirmed); they stay at the disabled default in the matrix


def button_text(v, macros=None):
    """Editable text for a matrix dword that parse_button() accepts again.
    A key dword that matches a macro's trigger key is shown as `macro:NAME`."""
    d = P.describe_button(v)
    if macros and d.lower().startswith("key:"):
        for name, m in macros.items():
            if d.lower() == "key:" + m["trigger"]:
                return "macro:" + name
    if d.startswith("special") or d == "?" or d.startswith("key:") and "usage" in d:
        return "raw:" + struct.pack("<I", v).hex()
    return d


def button_choices(macros=None):
    return (["left", "right", "middle", "back", "forward", "scroll-up", "scroll-down", "double-click", "triple-click",
             "dpi-up", "dpi-loop", "disable"] +
            ["macro:" + n for n in sorted(macros or {})] +
            ["key:ctrl+c", "key:ctrl+v", "key:ctrl+shift+esc", "raw:41000000"])


def parse_button_ui(text, macros):
    """parse_button() plus `macro:NAME` (sent to the mouse as the macro's trigger key; pulsar_macros.py daemon runs it)."""
    t = text.strip()
    if t.lower().startswith("macro:"):
        name = t[6:]
        if name not in macros:
            raise ValueError(f"unknown macro '{name}'")
        try:
            return P.parse_button("key:" + macros[name]["trigger"])
        except Exception as e:
            raise ValueError(f"pulsar.py cannot encode the trigger key '{macros[name]['trigger']}' ({e})")
    return P.parse_button(t)


class Session:
    """One profile of one device: load, edit settings dict, apply."""

    def __init__(self, dev, profile=None):
        self.dev = dev
        self.profile = profile
        self.rep = self.block = self.orig = None

    # ---- read
    def load(self):
        dev = self.dev
        if self.profile is None:
            self.profile = P.PROFILE_FOR_PID.get(dev.pid) or dev.active_profile() or 1
        self.rep = dev.read_profile_raw(self.profile)
        self.block = P.block_of(self.rep)
        if not P.markers_ok(self.block):
            raise RuntimeError("profile block lacks the expected markers (b[0]=0x64, b[0x90]=0xA5): refusing to touch this device")
        self.matrix, self.matrix_known = P.load_matrix_state(self.profile)
        self.orig = self.extract(self.block)
        return self.orig

    def extract(self, b):
        d = P.decode(b)
        return {"rate": d["rate_hz"], "n_stages": d["n_stages"], "cur_stage": d["cur_stage"],
                "dpi": list(d["dpi_x"]), "colors": list(d["colors"]), "led": self.led_of(b)}

    @staticmethod
    def led_of(b):
        d = P.decode_led(b)
        eid = d["effect"]
        out = {"effect": eid}
        if eid not in P.LED_EFFECTS:
            return out
        if eid in LED_SPEED and "speed" in d:
            out["speed"] = d["speed"]
        if "direction_bit" in d:
            out["direction"] = int(bool(d["direction_bit"]))
        if "colors" in d:
            cols = list(d["colors"])
            ctl = P.LED_EFFECTS[eid]
            if ctl[6] is None:          # no count byte in the block: infer the count from trailing zero colours
                while len(cols) > 1 and cols[-1] == "000000":
                    cols.pop()
            out["colors"] = cols
        return out

    def led_defaults(self, eid):
        """What the block currently holds for effect `eid` (so switching effect starts from sane values)."""
        tmp = bytearray(self.block)
        tmp[0x3D] = eid
        return self.led_of(tmp)

    # ---- build / apply
    def build(self, want, base_block=None):
        """Patch only fields changed in the UI onto the freshest profile block available."""
        new = bytearray(self.block if base_block is None else base_block)
        old = self.orig
        if want["rate"] != old["rate"]:
            P.patch_rate(new, want["rate"])
        if want["n_stages"] != old["n_stages"]:
            P.patch_nstages(new, want["n_stages"])
        if want["cur_stage"] != old["cur_stage"] or want["n_stages"] != old["n_stages"]:
            P.patch_stage(new, min(want["cur_stage"], want["n_stages"]))
        for i in range(P.N_STAGES):
            if want["dpi"][i] != old["dpi"][i]:
                P.patch_dpi(new, i + 1, want["dpi"][i])
            if want["colors"][i].upper() != old["colors"][i].upper():
                P.patch_color(new, i + 1, want["colors"][i])
        w = want["led"]
        if any(old["led"].get(k) != v for k, v in w.items()):
            P.patch_led(new, w["effect"], w.get("speed"), None, w.get("colors"), w.get("direction"))
        return new

    def diff(self, new, base_block=None):
        base = self.block if base_block is None else base_block
        return [(i, base[i], new[i]) for i in range(P.BLOCK_LEN) if base[i] != new[i]]

    def apply(self, want, matrix=None):
        """Apply staged profile settings and (optionally) a complete 20-slot button matrix.

        All local validation is performed before the first hardware write. Profile changes are
        backed up by pulsar.py; button maps receive a separate local backup because the mouse
        does not provide a verified way to read that matrix back.
        """
        lines = []
        # Re-read just before patching so edits made by the mouse or another tool since
        # this window opened are preserved in fields the user did not change here.
        fresh_rep = self.dev.read_profile_raw(self.profile)
        fresh_block = P.block_of(fresh_rep)
        if not P.markers_ok(fresh_block):
            raise RuntimeError("profile markers changed since load; refusing to write an unfamiliar block")
        new = self.build(want, base_block=fresh_block)
        d = self.diff(new, base_block=fresh_block)
        matrix = list(matrix) if matrix is not None else None
        if matrix is not None:
            if len(matrix) != P.N_BTN:
                raise ValueError(f"button matrix must contain {P.N_BTN} slots")
            if P.BTN_NAMED["left"] not in matrix:
                raise ValueError("at least one slot must stay 'left' (the vendor app enforces the same rule)")

        # Save the profile before touching the device. If the write/read-back fails, do not
        # silently continue with a second, independent matrix write.
        if d:
            lines.append("profile block: " + ", ".join(f"[{i:#04x}] {x:02x}->{y:02x}" for i, x, y in d))
            lines.append("backup: " + P.save_backup(self.profile, fresh_rep))
            self.dev.write_profile(self.profile, new)
            chk = P.block_of(self.dev.read_profile_raw(self.profile))
            if chk != new:
                lines.append("read-back: differs from requested bytes; button changes were skipped for safety")
                self.load()
                return "\n".join(lines)
            lines.append("read-back: matches")
        else:
            lines.append("profile block: no changes")

        if matrix is not None and matrix != list(self.matrix):
            P.save_matrix_backup(self.profile, list(self.matrix), known=self.matrix_known)
            self.dev.write_buttons(self.profile, matrix)
            P.save_matrix_state(self.profile, matrix)
            lines.append("button matrix written (mouse read-back is not verified; local copy updated)")
        self.load()
        return "\n".join(lines)

    def power(self):
        r = self.dev.cmd([0x90, 0, 0, 0, 0])
        if r[2] in (0x10, 0x11):
            return {"usb": r[2] == 0x10, "percent": r[3]}
        return None


def parse_dpi(text):
    v = int(str(text).strip())
    P.reg_from_dpi(v)          # validates range/step
    return v


def parse_rgb(text):
    t = text.strip().lstrip("#")
    if len(t) != 6:
        raise ValueError(f"bad colour '{text}', want RRGGBB")
    int(t, 16)
    return t.upper()


# ============================================================================ tkinter layer

def run_gui(argv=None):
    """Launch the desktop UI. No external Python packages are needed for the core GUI."""
    import re
    import subprocess
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk, messagebox, colorchooser

    C = {
        "bg": "#15171c",         # page
        "side": "#101216",       # sidebar
        "card": "#1c1f26",       # cards
        "field": "#13151a",      # inputs, troughs
        "surface": "#272b34",    # buttons
        "surface_hi": "#323743",
        "border": "#2b303a",
        "fg": "#e8eaef",
        "muted": "#8e96a5",
        "accent": "#8fa3ff",
        "accent_hi": "#a9b8ff",
        "on_accent": "#0f1226",
        "warn": "#f2b84b",
        "ok": "#62d3a0",
    }
    PAGES = [("dpi", "DPI & polling"), ("led", "Lighting"), ("btn", "Buttons"), ("mac", "Macros")]

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Pulsar Control")
            self.geometry("1040x780")
            self.minsize(900, 720)
            self.configure(bg=C["bg"])
            self._pick_fonts()
            self._configure_style()
            self.s = None
            self.dirty = False
            self.loading = False
            self.macro_proc = None
            self.macro_log = None
            self._closing = False

            self.status = tk.StringVar(value="Changes are staged until you apply them.")
            self.device_status = tk.StringVar(value="Connecting to mouse…")
            self.listener_status = tk.StringVar(value="Listener stopped")
            self._build_shell()
            self.build_dpi()
            self.build_led()
            self.build_btn()
            self.build_macros()
            self._watch_settings()
            self.show_page("dpi")
            self.bind("<Control-s>", lambda _e: self.apply())
            self.protocol("WM_DELETE_WINDOW", self.close)

            try:
                dev = P.Pulsar.open(None)
                self.s = Session(dev)
                self.reload(force=True)
                # Host-side macros are part of the desktop app experience: start the
                # listener automatically after the mouse/profile have loaded. Keep the
                # Start/Restart control available for permission fixes and recovery.
                self.after(350, self.start_listener)
            except (Exception, SystemExit) as e:
                messagebox.showerror(
                    "Mouse not available",
                    f"Pulsar Control could not open a compatible mouse.\n\n{e}\n\n"
                    "Check the USB receiver/cable and install the udev rule. You do not normally need to run the GUI as root.",
                    parent=self,
                )
                self.destroy()
                return

        # ------------------------------------------------------------ look & feel
        def _pick_fonts(self):
            fams = set(tkfont.families(self))
            base = tkfont.nametofont("TkDefaultFont").actual("family")
            for cand in ("Inter", "Cantarell", "Noto Sans", "Ubuntu", "Segoe UI", "DejaVu Sans"):
                if cand in fams:
                    base = cand
                    break
            mono = tkfont.nametofont("TkFixedFont").actual("family")
            for cand in ("JetBrains Mono", "Fira Code", "Cascadia Mono", "DejaVu Sans Mono"):
                if cand in fams:
                    mono = cand
                    break
            self.base_family, self.mono_family = base, mono
            tkfont.nametofont("TkDefaultFont").configure(family=base, size=10)
            tkfont.nametofont("TkTextFont").configure(family=base, size=10)

        def f(self, size=10, weight="normal"):
            return (self.base_family, size, weight)

        def mono(self, size=10, weight="normal"):
            return (self.mono_family, size, weight)

        def _configure_style(self):
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            f = self.f
            # dropdown lists of comboboxes are plain tk widgets
            self.option_add("*TCombobox*Listbox.background", C["field"])
            self.option_add("*TCombobox*Listbox.foreground", C["fg"])
            self.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
            self.option_add("*TCombobox*Listbox.selectForeground", C["on_accent"])
            self.option_add("*TCombobox*Listbox.font", f(10))

            style.configure(".", background=C["bg"], foreground=C["fg"], font=f(10),
                            bordercolor=C["border"], focuscolor=C["bg"])
            style.configure("TFrame", background=C["bg"])
            style.configure("Card.TFrame", background=C["card"])
            style.configure("Side.TFrame", background=C["side"])
            style.configure("TLabel", background=C["bg"], foreground=C["fg"])
            style.configure("Card.TLabel", background=C["card"], foreground=C["fg"])
            style.configure("Muted.TLabel", background=C["card"], foreground=C["muted"], font=f(9))
            style.configure("Page.TLabel", background=C["bg"], foreground=C["fg"], font=f(20, "bold"))
            style.configure("PageSub.TLabel", background=C["bg"], foreground=C["muted"], font=f(10))
            style.configure("CardTitle.TLabel", background=C["card"], foreground=C["fg"], font=f(12, "bold"))
            style.configure("Col.TLabel", background=C["card"], foreground=C["muted"], font=f(9, "bold"))
            style.configure("Status.TLabel", background=C["side"], foreground=C["muted"], font=f(9))

            style.configure("TButton", background=C["surface"], foreground=C["fg"], padding=(14, 8),
                            borderwidth=1, bordercolor=C["surface"], lightcolor=C["surface"], darkcolor=C["surface"],
                            focuscolor=C["surface"], relief="flat")
            style.map("TButton",
                      background=[("pressed", C["surface_hi"]), ("active", C["surface_hi"])],
                      bordercolor=[("active", C["surface_hi"])],
                      lightcolor=[("active", C["surface_hi"])], darkcolor=[("active", C["surface_hi"])],
                      foreground=[("disabled", C["muted"])])
            style.configure("Accent.TButton", background=C["accent"], foreground=C["on_accent"],
                            bordercolor=C["accent"], lightcolor=C["accent"], darkcolor=C["accent"],
                            font=f(10, "bold"), padding=(18, 9))
            style.map("Accent.TButton",
                      background=[("pressed", C["accent_hi"]), ("active", C["accent_hi"])],
                      bordercolor=[("active", C["accent_hi"])],
                      lightcolor=[("active", C["accent_hi"])], darkcolor=[("active", C["accent_hi"])])
            style.configure("Quiet.TButton", background=C["card"], foreground=C["muted"], padding=(10, 6),
                            bordercolor=C["border"], lightcolor=C["card"], darkcolor=C["card"])
            style.map("Quiet.TButton", background=[("active", C["surface"])], foreground=[("active", C["fg"])],
                      lightcolor=[("active", C["surface"])], darkcolor=[("active", C["surface"])])

            field = dict(fieldbackground=C["field"], background=C["surface"], foreground=C["fg"],
                         bordercolor=C["border"], lightcolor=C["field"], darkcolor=C["field"],
                         arrowcolor=C["muted"], insertcolor=C["fg"], padding=6)
            for name in ("TCombobox", "TSpinbox", "TEntry"):
                style.configure(name, **field)
                style.map(name, bordercolor=[("focus", C["accent"])], lightcolor=[("focus", C["accent"])],
                          darkcolor=[("focus", C["accent"])],
                          foreground=[("disabled", C["muted"])],
                          fieldbackground=[("disabled", C["card"]), ("readonly", C["field"])],
                          selectbackground=[("readonly", C["field"])], selectforeground=[("readonly", C["fg"])])

            for name in ("TRadiobutton", "TCheckbutton"):
                style.configure(name, background=C["card"], foreground=C["fg"], indicatorbackground=C["field"],
                                indicatorforeground=C["accent"], indicatorcolor=C["field"],
                                bordercolor=C["muted"], lightcolor=C["field"], darkcolor=C["field"],
                                focuscolor=C["card"], upperbordercolor=C["muted"], lowerbordercolor=C["muted"])
                style.map(name, indicatorbackground=[("disabled", C["card"])],
                          indicatorforeground=[("disabled", C["muted"])],
                          bordercolor=[("disabled", C["border"]), ("selected", C["accent"])],
                          upperbordercolor=[("disabled", C["border"]), ("selected", C["accent"])],
                          lowerbordercolor=[("disabled", C["border"]), ("selected", C["accent"])],
                          foreground=[("disabled", C["muted"])],
                          background=[("active", C["card"])])
            style.configure("Card.Horizontal.TScale", background=C["accent"], troughcolor=C["field"],
                            bordercolor=C["border"], lightcolor=C["accent"], darkcolor=C["accent"],
                            gripcount=0, sliderthickness=16)
            style.map("Card.Horizontal.TScale", background=[("disabled", C["surface"])],
                      lightcolor=[("disabled", C["surface"])], darkcolor=[("disabled", C["surface"])])
            style.configure("TScrollbar", background=C["surface"], troughcolor=C["field"], bordercolor=C["field"],
                            lightcolor=C["surface"], darkcolor=C["surface"], arrowcolor=C["muted"])

        # ------------------------------------------------------------ building blocks
        def mk_card(self, parent, title=None, hint=None, pad=18):
            """Bordered card. Returns (outer, body): grid/pack `outer`, put content in `body`."""
            outer = tk.Frame(parent, bg=C["card"], highlightbackground=C["border"],
                             highlightcolor=C["border"], highlightthickness=1)
            inner = ttk.Frame(outer, style="Card.TFrame", padding=pad)
            inner.pack(fill="both", expand=True)
            if title:
                ttk.Label(inner, text=title, style="CardTitle.TLabel").pack(anchor="w")
            if hint:
                ttk.Label(inner, text=hint, style="Muted.TLabel", wraplength=520, justify="left").pack(
                    anchor="w", pady=(3, 0))
            body = ttk.Frame(inner, style="Card.TFrame")
            body.pack(fill="both", expand=True, pady=(14 if title else 0, 0))
            return outer, body

        def mk_page(self, key, title, subtitle):
            page = ttk.Frame(self.pages)
            head = ttk.Frame(page)
            head.pack(fill="x", padx=32, pady=(28, 18))
            ttk.Label(head, text=title, style="Page.TLabel").pack(anchor="w")
            ttk.Label(head, text=subtitle, style="PageSub.TLabel").pack(anchor="w", pady=(4, 0))
            content = ttk.Frame(page)
            content.pack(fill="both", expand=True, padx=32, pady=(0, 20))
            self.page_frames[key] = page
            return content

        def _build_shell(self):
            # ---- sidebar
            side = tk.Frame(self, bg=C["side"], width=216)
            side.pack(side="left", fill="y")
            side.pack_propagate(False)
            brand = tk.Frame(side, bg=C["side"])
            brand.pack(fill="x", padx=22, pady=(28, 26))
            tk.Label(brand, text="Pulsar", bg=C["side"], fg=C["fg"], font=self.f(20, "bold")).pack(anchor="w")
            tk.Label(brand, text="Control for HATOR Pulsar", bg=C["side"], fg=C["muted"], font=self.f(9)).pack(anchor="w")
            self.nav = {}
            for key, label in PAGES:
                row = tk.Frame(side, bg=C["side"], cursor="hand2")
                row.pack(fill="x")
                bar = tk.Frame(row, width=3, bg=C["side"])
                bar.pack(side="left", fill="y")
                lab = tk.Label(row, text=label, anchor="w", padx=19, pady=11, bg=C["side"], fg=C["muted"],
                               font=self.f(11, "bold"), cursor="hand2")
                lab.pack(side="left", fill="x", expand=True)
                for w in (row, bar, lab):
                    w.bind("<Button-1>", lambda _e, k=key: self.show_page(k))
                    w.bind("<Enter>", lambda _e, k=key: self._nav_hover(k, True))
                    w.bind("<Leave>", lambda _e, k=key: self._nav_hover(k, False))
                self.nav[key] = (row, bar, lab)
            tk.Label(side, textvariable=self.device_status, bg=C["side"], fg=C["muted"], font=self.f(9),
                     wraplength=180, justify="left", anchor="w").pack(side="bottom", fill="x", padx=22, pady=22)

            # ---- main area: pages above, apply bar below
            main = tk.Frame(self, bg=C["bg"])
            main.pack(side="left", fill="both", expand=True)
            footer = tk.Frame(main, bg=C["side"])
            footer.pack(side="bottom", fill="x")
            tk.Frame(footer, height=1, bg=C["border"]).pack(fill="x")
            bar = ttk.Frame(footer, style="Side.TFrame", padding=(24, 14))
            bar.pack(fill="x")
            self.dot = tk.Label(bar, text="●", bg=C["side"], fg=C["muted"], font=self.f(10))
            self.dot.pack(side="left", padx=(0, 8))
            ttk.Button(bar, text="Apply settings", style="Accent.TButton", command=self.apply).pack(side="right")
            ttk.Button(bar, text="Reload from mouse", command=self.reload).pack(side="right", padx=(12, 10))
            ttk.Label(bar, textvariable=self.status, style="Status.TLabel").pack(side="left", fill="x", expand=True)

            self.pages = ttk.Frame(main)
            self.pages.pack(fill="both", expand=True)
            self.page_frames = {}
            self.current_page = None

        def _nav_hover(self, key, inside):
            if key == self.current_page:
                return
            row, bar, lab = self.nav[key]
            colour = C["card"] if inside else C["side"]
            for w in (row, lab):
                w.configure(bg=colour)
            lab.configure(fg=C["fg"] if inside else C["muted"])

        def show_page(self, key):
            for k, frame in self.page_frames.items():
                frame.pack_forget()
            self.page_frames[key].pack(fill="both", expand=True)
            self.current_page = key
            for k, (row, bar, lab) in self.nav.items():
                active = k == key
                row.configure(bg=C["bg"] if active else C["side"])
                lab.configure(bg=C["bg"] if active else C["side"], fg=C["fg"] if active else C["muted"])
                bar.configure(bg=C["accent"] if active else C["side"])

        def set_dirty(self, flag):
            self.dirty = flag
            self.dot.configure(fg=C["warn"] if flag else C["muted"])

        def _watch_settings(self):
            variables = [getattr(self, n) for n in ("rate", "nst", "cur", "eff", "speed", "direction", "ncol") if hasattr(self, n)]
            variables += getattr(self, "dpi_vars", []) + getattr(self, "col_vars", [])
            variables += getattr(self, "led_cols", []) + list(getattr(self, "btn_vars", {}).values())
            for var in variables:
                var.trace_add("write", lambda *_: self.mark_dirty())

        def mark_dirty(self):
            if self.loading or self._closing:
                return
            self.set_dirty(True)
            self.status.set("Unsaved changes. Apply to write them to the mouse.")

        def err(self, e):
            messagebox.showerror("Pulsar Control", str(e), parent=self)

        # ------------------------------------------------------------ colour swatches
        def colour_button(self, parent, var, width=9):
            b = tk.Button(parent, width=width, textvariable=var, relief="flat", bd=0, cursor="hand2",
                          font=self.mono(9, "bold"), padx=6, pady=5, highlightthickness=1,
                          highlightbackground=C["border"], highlightcolor=C["accent"],
                          activebackground=C["surface_hi"], disabledforeground=C["muted"],
                          command=lambda: self.pick(var, b))
            var.trace_add("write", lambda *_: self._tint(b, var))
            self._tint(b, var)
            return b

        def pick(self, var, btn):
            current = var.get().strip().lstrip("#")
            if not re.fullmatch(r"[0-9a-fA-F]{6}", current):
                current = "FFFFFF"
            color = colorchooser.askcolor(color="#" + current, parent=self)[1]
            if color:
                var.set(color.lstrip("#").upper())
                self._tint(btn, var)

        @staticmethod
        def _tint(button, var):
            try:
                value = var.get().strip().lstrip("#")
                if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
                    raise ValueError("not RGB")
                rgb = tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
                button.configure(bg="#" + value, fg=("#11151d" if sum(rgb) > 410 else "#ffffff"))
            except (tk.TclError, ValueError):
                button.configure(bg=C["surface"], fg=C["fg"])

        # ------------------------------------------------------------ DPI page
        def build_dpi(self):
            f = self.mk_page("dpi", "DPI & polling", "Set the sensor stages the DPI button cycles through, and how often the mouse reports.")
            self.t_dpi = f

            outer, g = self.mk_card(f, "General")
            outer.pack(fill="x")
            self.rate = tk.StringVar()
            self.nst = tk.IntVar(value=7)
            ttk.Label(g, text="Polling rate (Hz)", style="Card.TLabel").grid(row=0, column=0, sticky="w")
            ttk.Combobox(g, textvariable=self.rate, values=[str(r) for r in sorted(P.CODE_FROM_RATE)],
                         width=8, state="readonly").grid(row=0, column=1, padx=(12, 40), sticky="w")
            ttk.Label(g, text="Active DPI stages", style="Card.TLabel").grid(row=0, column=2, sticky="w")
            ttk.Spinbox(g, from_=1, to=MAX_UI_STAGES, textvariable=self.nst, width=5,
                        command=self.refresh_rows).grid(row=0, column=3, padx=(12, 0), sticky="w")

            outer, g = self.mk_card(f, "DPI stages", "Select the stage the mouse starts on. Each stage has its own indicator colour.")
            outer.pack(fill="x", pady=(16, 0))
            for c, text in enumerate(("Current", "Stage", "DPI", "Indicator", "")):
                ttk.Label(g, text=text, style="Col.TLabel").grid(row=0, column=c, padx=(0, 18), pady=(0, 8), sticky="w")
            self.cur = tk.IntVar(value=1)
            self.dpi_vars, self.col_vars, self.row_w, self.bars = [], [], [], []
            for i in range(MAX_UI_STAGES):
                r = 1 + i
                current = ttk.Radiobutton(g, variable=self.cur, value=i + 1)
                current.grid(row=r, column=0, padx=(10, 18), pady=4)
                ttk.Label(g, text=f"Stage {i + 1}", style="Card.TLabel").grid(row=r, column=1, padx=(0, 18), sticky="w")
                dv, cv = tk.StringVar(), tk.StringVar(value="000000")
                sp = ttk.Spinbox(g, from_=100, to=16000, increment=100, textvariable=dv, width=9)
                sp.grid(row=r, column=2, padx=(0, 18), sticky="w")
                cb = self.colour_button(g, cv, width=9)
                cb.grid(row=r, column=3, padx=(0, 18), sticky="w")
                bar = tk.Canvas(g, width=190, height=8, bg=C["card"], highlightthickness=0, bd=0)
                bar.grid(row=r, column=4, sticky="w")
                self.dpi_vars.append(dv)
                self.col_vars.append(cv)
                self.row_w.append((current, sp, cb))
                self.bars.append(bar)
                dv.trace_add("write", lambda *_, i=i: self.draw_bar(i))
                cv.trace_add("write", lambda *_, i=i: self.draw_bar(i))
            g.columnconfigure(4, weight=1)
            self.nst.trace_add("write", lambda *_: self.refresh_rows())

            ttk.Label(f, text="The mouse has room for eight stages; this app shows the seven listed in the vendor configuration.",
                      style="PageSub.TLabel").pack(anchor="w", pady=(14, 0))

        def draw_bar(self, i):
            if i >= len(self.bars):
                return
            bar = self.bars[i]
            try:
                n = int(self.nst.get())
                dpi = int(self.dpi_vars[i].get())
                colour = "#" + parse_rgb(self.col_vars[i].get())
            except (tk.TclError, ValueError):
                bar.delete("all")
                bar.create_rectangle(0, 0, 190, 8, fill=C["field"], outline="")
                return
            frac = max(0.0, min(1.0, dpi / 16000.0)) ** 0.5     # sqrt scale so low DPI stays visible
            bar.delete("all")
            bar.create_rectangle(0, 0, 190, 8, fill=C["field"], outline="")
            if i < n:
                bar.create_rectangle(0, 0, max(3, int(190 * frac)), 8, fill=colour if colour != "#000000" else C["muted"], outline="")

        def refresh_rows(self):
            try:
                n = int(self.nst.get())
            except (tk.TclError, ValueError):
                return
            for i, widgets in enumerate(self.row_w):
                state = "normal" if i < n else "disabled"
                for widget in widgets:
                    try:
                        widget.configure(state=state)
                    except tk.TclError:
                        pass
                self.draw_bar(i)

        # ------------------------------------------------------------ Lighting page
        def build_led(self):
            f = self.mk_page("led", "Lighting", "Choose an effect and tune it. Only controls the firmware honours are shown.")
            self.t_led = f
            self.eff = tk.StringVar()
            self.speed = tk.IntVar()
            self.speed_hint = tk.StringVar()
            self.speed_orig = None
            self.direction = tk.IntVar()
            self.ncol = tk.IntVar(value=1)
            self.led_max = 0

            outer, g = self.mk_card(f, "Effect")
            outer.grid(row=0, column=0, sticky="new")
            ttk.Label(g, text="Effect", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=7)
            self.effect_combo = ttk.Combobox(g, textvariable=self.eff, state="readonly", width=26,
                                             values=[f"{k}  {P.LED_UI_NAMES[k]}" for k in P.LED_EFFECTS])
            self.effect_combo.grid(row=0, column=1, sticky="w", padx=(18, 0), pady=7)
            self.effect_combo.bind("<<ComboboxSelected>>", lambda _e: self.effect_changed())
            ttk.Label(g, text="Speed", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=7)
            speed_row = ttk.Frame(g, style="Card.TFrame")
            speed_row.grid(row=1, column=1, sticky="w", padx=(18, 0), pady=7)
            self.w_speed = ttk.Scale(speed_row, from_=0, to=15, orient="horizontal", length=240,
                                     style="Card.Horizontal.TScale", command=self._speed_moved)
            self.w_speed.pack(side="left")
            ttk.Label(speed_row, textvariable=self.speed, style="Card.TLabel", width=3, anchor="e").pack(side="left", padx=(10, 0))
            self._speed_sync = False
            self.speed.trace_add("write", lambda *_: self._speed_to_scale())
            ttk.Label(g, textvariable=self.speed_hint, style="Muted.TLabel").grid(row=2, column=1, sticky="w", padx=(18, 0))
            self.w_dir = ttk.Checkbutton(g, text="Reverse streaming direction", variable=self.direction)
            self.w_dir.grid(row=3, column=1, sticky="w", padx=(18, 0), pady=(12, 4))
            ttk.Label(g, text="Number of colours", style="Card.TLabel").grid(row=4, column=0, sticky="w", pady=7)
            self.w_ncol = ttk.Spinbox(g, from_=1, to=8, textvariable=self.ncol, width=5, command=self.refresh_led_cols)
            self.w_ncol.grid(row=4, column=1, sticky="w", padx=(18, 0), pady=7)

            outer, colors = self.mk_card(f, "Effect colours", "Used by Breathing and Flicker.")
            outer.grid(row=1, column=0, sticky="new", pady=(16, 0))
            self.led_cols, self.led_btns = [], []
            for i in range(8):
                v = tk.StringVar(value="000000")
                b = self.colour_button(colors, v, width=9)
                b.grid(row=i // 4, column=i % 4, padx=(0, 10), pady=5, sticky="w")
                self.led_cols.append(v)
                self.led_btns.append(b)

            outer, notes = self.mk_card(f, "Hardware notes")
            outer.grid(row=0, column=1, rowspan=2, sticky="new", padx=(16, 0))
            ttk.Label(notes, text=("Colour changes are known to work on Breathing and Flicker only. "
                                   "Speed and direction controls are enabled per effect.\n\n"
                                   "Brightness is not shown because it had no effect in the hardware tests. "
                                   "Other effect details are reverse-engineered, not vendor-certified."),
                      style="Muted.TLabel", wraplength=230, justify="left").pack(anchor="w")
            f.columnconfigure(0, weight=3)
            f.columnconfigure(1, weight=2, minsize=260)

        def _speed_moved(self, value):
            if self._speed_sync:
                return
            self._speed_sync = True
            try:
                self.speed.set(int(round(float(value))))
            finally:
                self._speed_sync = False

        def _speed_to_scale(self):
            if self._speed_sync:
                return
            self._speed_sync = True
            try:
                self.w_speed.set(self.speed.get())
            except (tk.TclError, ValueError):
                pass
            finally:
                self._speed_sync = False

        def cur_eid(self):
            try:
                return int(self.eff.get().split()[0])
            except (ValueError, IndexError):
                raise ValueError("Select a supported lighting effect.")

        def effect_changed(self):
            try:
                eid = self.cur_eid()
                self.load_led(self.s.led_defaults(eid))
            except Exception as e:
                self.err(e)

        def load_led(self, d):
            eid = d["effect"]
            self.eff.set(f"{eid}  {P.LED_UI_NAMES.get(eid, '?')}")
            spec = P.LED_EFFECTS.get(eid)
            rng = LED_SPEED.get(eid)
            maxc = spec[5] if (spec and eid in LED_COLOURS) else 0
            self.speed_orig = d.get("speed")
            if rng:
                lo, hi = rng
                self.w_speed.configure(from_=lo, to=hi)
                self.w_speed.state(["!disabled"])
                raw = d.get("speed", lo)
                self.speed.set(min(max(raw, lo), hi))
                self.speed_hint.set(LED_SPEED_HINT.get(eid, ""))
            else:
                self.w_speed.state(["disabled"])
                self.speed.set(0)
                self.speed_hint.set("This effect has no speed control.")
            self.direction.set(d.get("direction", 0))
            self.w_dir.configure(state="normal" if eid == 1 else "disabled")
            cols = d.get("colors", [])
            self.ncol.set(max(1, len(cols)) if maxc else 1)
            self.w_ncol.configure(from_=1, to=max(1, maxc), state="normal" if maxc > 1 else "disabled")
            for i, v in enumerate(self.led_cols):
                v.set(cols[i] if i < len(cols) else "000000")
            self.led_max = maxc
            self.refresh_led_cols()

        def refresh_led_cols(self):
            try:
                n = int(self.ncol.get())
            except (tk.TclError, ValueError):
                n = 1
            for i, button in enumerate(self.led_btns):
                button.configure(state="normal" if i < min(n, self.led_max) else "disabled")

        def want_led(self):
            eid = self.cur_eid()
            spec = P.LED_EFFECTS[eid]
            data = {"effect": eid}
            rng = LED_SPEED.get(eid)
            if rng:
                value = int(self.speed.get())
                original = self.speed_orig
                # Preserve a raw device value while the slider remains untouched.
                data["speed"] = original if original is not None and value == min(max(original, rng[0]), rng[1]) else value
            if eid == 1:
                data["direction"] = int(self.direction.get())
            if eid in LED_COLOURS and spec[5]:
                count = min(int(self.ncol.get()), spec[5]) if spec[5] > 1 else 1
                data["colors"] = [parse_rgb(self.led_cols[i].get()) for i in range(count)]
            return data

        # ------------------------------------------------------------ Buttons page
        def build_btn(self):
            f = self.mk_page("btn", "Buttons", "Choose what each physical button sends to your computer.")
            self.t_btn = f

            note = tk.Frame(f, bg=C["card"], highlightbackground=C["border"], highlightthickness=1)
            note.pack(fill="x", pady=(0, 16))
            tk.Frame(note, width=4, bg=C["warn"]).pack(side="left", fill="y")
            note_lbl = ttk.Label(note, text=("The mouse can't report its button map back. Applying a changed map writes all 20 slots, including "
                                             "hidden ones. This app remembers the last map it wrote; without one, factory defaults are assumed."),
                                 style="Card.TLabel", wraplength=600, justify="left", padding=(14, 10))
            note_lbl.pack(side="left", fill="x", expand=True)
            note.bind("<Configure>", lambda e: note_lbl.configure(wraplength=max(240, e.width - 60)))

            cards = ttk.Frame(f)
            cards.pack(fill="x")
            top_outer, top = self.mk_card(cards, "Top and sides")
            bot_outer, bot = self.mk_card(cards, "Underside")
            top_outer.grid(row=0, column=0, sticky="new", padx=(0, 8))
            bot_outer.grid(row=0, column=1, sticky="new", padx=(8, 0))
            cards.columnconfigure(0, weight=3, uniform="btn")
            cards.columnconfigure(1, weight=2, uniform="btn")
            top.columnconfigure(0, weight=1, uniform="slot")
            top.columnconfigure(1, weight=1, uniform="slot")
            bot.columnconfigure(0, weight=1)
            self.btn_vars, self.btn_combos = {}, []
            macros = M.load()
            for slot in UI_SLOTS:
                if slot <= 6:
                    body, r, c = top, (slot - 1) % 3, (slot - 1) // 3
                else:
                    body, r, c = bot, slot - 7, 0
                row = ttk.Frame(body, style="Card.TFrame")
                row.grid(row=r, column=c, sticky="ew", padx=(0, 16) if (body is top and c == 0) else 0, pady=(0, 12))
                ttk.Label(row, text=f"{slot}   {SLOT_LABELS[slot]}", style="Card.TLabel", wraplength=210,
                          justify="left").pack(anchor="w", pady=(0, 4))
                var = tk.StringVar()
                self.btn_vars[slot] = var
                combo = ttk.Combobox(row, textvariable=var, values=button_choices(macros), width=16)
                combo.pack(fill="x")
                self.btn_combos.append(combo)
            ttk.Label(bot, text="At least one button must stay assigned to left-click.", style="Muted.TLabel",
                      wraplength=240, justify="left").grid(row=2, column=0, sticky="w", pady=(4, 10))
            ttk.Button(bot, text="Reset to vendor defaults", command=self.reset_buttons).grid(row=3, column=0, sticky="w")

        def reset_buttons(self):
            if not messagebox.askyesno("Reset button map", "Stage the vendor default button map?\n\nNothing is written until you click Apply settings.", parent=self):
                return
            matrix = P.default_matrix()
            macros = M.load()
            for slot in UI_SLOTS:
                self.btn_vars[slot].set(button_text(matrix[slot - 1], macros))

        # ------------------------------------------------------------ Macros page
        def build_macros(self):
            f = self.mk_page("mac", "Macros", "Macros run on Linux. The listener starts with this app and stops when it closes.")
            self.t_mac = f
            layout = ttk.Frame(f)
            layout.pack(fill="both", expand=True)

            outer, listpanel = self.mk_card(layout, "Saved macros", pad=16)
            outer.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
            self.m_list = tk.Listbox(listpanel, height=8, width=22, exportselection=False,
                                     bg=C["field"], fg=C["fg"], selectbackground=C["accent"],
                                     selectforeground=C["on_accent"], relief="flat", highlightthickness=1,
                                     highlightbackground=C["border"], highlightcolor=C["accent"],
                                     activestyle="none", font=self.f(10), bd=0)
            self.m_list.pack(fill="both", expand=True)
            self.m_list.bind("<<ListboxSelect>>", lambda _e: self.macro_select())
            ttk.Button(listpanel, text="New macro", command=self.macro_new).pack(fill="x", pady=(10, 0))

            outer, editor = self.mk_card(layout, "Edit macro", pad=16)
            outer.grid(row=0, column=1, sticky="nsew")
            self.m_name = tk.StringVar()
            self.m_repeat = tk.IntVar(value=1)
            self.m_hold = tk.IntVar(value=0)
            self.m_trigger = tk.StringVar(value="Assigned when saved")
            ttk.Label(editor, text="Name", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=5)
            ttk.Entry(editor, textvariable=self.m_name, width=28).grid(row=0, column=1, columnspan=2, sticky="ew", padx=(14, 0), pady=5)
            ttk.Label(editor, text="Trigger key", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=5)
            ttk.Label(editor, textvariable=self.m_trigger, style="Muted.TLabel").grid(row=1, column=1, columnspan=2, sticky="w", padx=(14, 0), pady=5)
            ttk.Label(editor, text="Repeat count", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=5)
            ttk.Spinbox(editor, from_=1, to=9999, textvariable=self.m_repeat, width=8).grid(row=2, column=1, sticky="w", padx=(14, 0), pady=5)
            ttk.Checkbutton(editor, text="Repeat while held", variable=self.m_hold).grid(
                row=3, column=1, columnspan=2, sticky="w", padx=(14, 0), pady=(4, 8))
            self.m_text = tk.Text(editor, width=50, height=6, wrap="none", undo=True,
                                  bg=C["field"], fg=C["fg"], insertbackground=C["fg"],
                                  selectbackground=C["surface_hi"], relief="flat", padx=12, pady=10,
                                  highlightthickness=1, highlightbackground=C["border"], highlightcolor=C["accent"],
                                  font=self.mono(10))
            self.m_text.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(2, 10))
            script_scroll = ttk.Scrollbar(editor, orient="vertical", command=self.m_text.yview)
            script_scroll.grid(row=4, column=2, sticky="ns", pady=(2, 10))
            self.m_text.configure(yscrollcommand=script_scroll.set)
            actions = ttk.Frame(editor, style="Card.TFrame")
            actions.grid(row=5, column=0, columnspan=3, sticky="ew")
            ttk.Button(actions, text="Save macro", style="Accent.TButton", command=self.macro_save).pack(side="left")
            ttk.Button(actions, text="Delete", command=self.macro_delete).pack(side="left", padx=(8, 0))
            editor.columnconfigure(1, weight=1)
            editor.rowconfigure(4, weight=1)

            outer, listener = self.mk_card(layout, "Listener", pad=14)
            outer.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(16, 0))
            lrow = ttk.Frame(listener, style="Card.TFrame")
            lrow.pack(fill="x")
            ttk.Label(lrow, textvariable=self.listener_status, style="Muted.TLabel").pack(side="left", fill="x", expand=True)
            ttk.Button(lrow, text="Start / restart", command=self.start_listener).pack(side="left", padx=(8, 0))
            ttk.Button(lrow, text="Stop", command=self.stop_listener).pack(side="left", padx=(8, 0))
            help_lbl = ttk.Label(listener, text=("Script commands: key ctrl+shift+esc · down shift · up shift · type Hello · wait 100 (ms).  "
                                                 "Assign a macro on the Buttons page as macro:NAME, then apply. "
                                                 "Needs python-evdev and input/uinput access. exec runs shell commands as you, so only use trusted macros."),
                                 style="Muted.TLabel", wraplength=600, justify="left")
            help_lbl.pack(anchor="w", pady=(10, 0))
            listener.bind("<Configure>", lambda e: help_lbl.configure(wraplength=max(240, e.width - 8)))

            layout.columnconfigure(1, weight=1)
            layout.rowconfigure(0, weight=1)
            self.macro_refresh()

        def macro_refresh(self, select=None):
            self.macros = M.load()
            names = sorted(self.macros)
            self.m_list.delete(0, "end")
            for name in names:
                self.m_list.insert("end", name)
            for combo in self.btn_combos:
                combo.configure(values=button_choices(self.macros))
            if select in self.macros:
                self.m_list.selection_set(names.index(select))
                self.m_list.activate(names.index(select))
                self.macro_select()

        def macro_select(self):
            selection = self.m_list.curselection()
            if not selection:
                return
            name = self.m_list.get(selection[0])
            spec = self.macros.get(name, {})
            self.loading = True
            try:
                self.m_name.set(name)
                self.m_trigger.set(spec.get("trigger", "Unknown"))
                self.m_repeat.set(spec.get("repeat", 1))
                self.m_hold.set(int(bool(spec.get("hold"))))
                self.m_text.delete("1.0", "end")
                self.m_text.insert("1.0", spec.get("script", ""))
            finally:
                self.loading = False

        def macro_new(self):
            self.m_list.selection_clear(0, "end")
            self.loading = True
            try:
                self.m_name.set("")
                self.m_trigger.set("Assigned when saved")
                self.m_repeat.set(1)
                self.m_hold.set(0)
                self.m_text.delete("1.0", "end")
            finally:
                self.loading = False

        def macro_save(self):
            try:
                name = self.m_name.get().strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", name):
                    raise ValueError("Use a macro name with 1–48 letters, numbers, underscores, or hyphens.")
                script = self.m_text.get("1.0", "end").strip()
                M.parse_script(script)
                macros = M.load()
                old = macros.get(name)
                trigger = old["trigger"] if old else M.free_trigger(macros)
                repeat = int(self.m_repeat.get())
                if not 1 <= repeat <= 9999:
                    raise ValueError("Repeat count must be between 1 and 9999.")
                macros[name] = {"trigger": trigger, "script": script, "repeat": repeat,
                                "hold": bool(self.m_hold.get())}
                M.save(macros)
            except Exception as e:
                return self.err(e)
            self.macro_refresh(select=name)
            self.status.set(f"Macro ‘{name}’ saved locally. Map it on the Buttons page and apply the mapping.")

        def macro_delete(self):
            selection = self.m_list.curselection()
            if not selection:
                return
            name = self.m_list.get(selection[0])
            if not messagebox.askyesno("Delete macro", f"Delete macro ‘{name}’?\n\nThe saved button map may still refer to its trigger.", parent=self):
                return
            macros = M.load()
            macros.pop(name, None)
            M.save(macros)
            self.macro_refresh()
            self.macro_new()

        def start_listener(self):
            if self.macro_proc and self.macro_proc.poll() is None:
                self.listener_status.set("Listener is running")
                return
            try:
                # Avoid spawning a listener that is guaranteed to exit when the
                # optional python-evdev dependency is missing. This is non-fatal for
                # normal DPI/lighting/button configuration.
                try:
                    import importlib.util
                    evdev_available = importlib.util.find_spec("evdev") is not None
                except (ImportError, ValueError):
                    evdev_available = False
                if not evdev_available:
                    self.listener_status.set("Macros unavailable · install python-evdev")
                    return
                log_dir = os.path.dirname(M.CFG)
                os.makedirs(log_dir, exist_ok=True)
                if self.macro_log:
                    self.macro_log.close()
                self.macro_log = open(os.path.join(log_dir, "macro-listener.log"), "a", encoding="utf-8")
                script_path = os.path.abspath(M.__file__)
                self.macro_proc = subprocess.Popen([sys.executable, script_path], stdin=subprocess.DEVNULL,
                                                   stdout=self.macro_log, stderr=subprocess.STDOUT,
                                                   start_new_session=True, close_fds=True)
                self.listener_status.set("Starting listener… log: ~/.config/pulsar-linux/macro-listener.log")
                self.after(750, self._poll_listener)
            except Exception as e:
                self.err(e)

        def _poll_listener(self):
            if self._closing:
                return
            if self.macro_proc and self.macro_proc.poll() is None:
                self.listener_status.set("Listener is running · diagnostics in macro-listener.log")
                self.after(1500, self._poll_listener)
            else:
                code = self.macro_proc.poll() if self.macro_proc else None
                self.listener_status.set(f"Listener stopped (exit {code}); check macro-listener.log")
                self._close_listener_log()

        def _close_listener_log(self):
            if self.macro_log:
                try:
                    self.macro_log.close()
                except OSError:
                    pass
                self.macro_log = None

        def stop_listener(self):
            if self.macro_proc and self.macro_proc.poll() is None:
                self.macro_proc.terminate()
                try:
                    self.macro_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.macro_proc.kill()
                    self.macro_proc.wait(timeout=2)
            self._close_listener_log()
            self.listener_status.set("Listener stopped")

        # ------------------------------------------------------------ device I/O
        def reload(self, force=False):
            if self.dirty and not force:
                if not messagebox.askyesno("Discard staged changes?", "Reloading will discard unapplied changes.", parent=self):
                    return
            if self.s is None:
                return
            try:
                self.loading = True
                original = self.s.load()
                try:
                    power = self.s.power()
                except Exception:
                    # A power/status command may be unsupported while profile access still works.
                    power = None
                self.rate.set(str(original["rate"]) if original["rate"] is not None else "")
                self.nst.set(original["n_stages"])
                self.cur.set(original["cur_stage"])
                for i in range(MAX_UI_STAGES):
                    self.dpi_vars[i].set(str(original["dpi"][i]))
                    self.col_vars[i].set(original["colors"][i])
                self.load_led(original["led"])
                macros = M.load()
                for slot in UI_SLOTS:
                    self.btn_vars[slot].set(button_text(self.s.matrix[slot - 1], macros))
                self.refresh_rows()
                model = f"258a:{self.s.dev.pid:04x} · Profile {self.s.profile}"
                self.device_status.set(model)
                power_txt = f"{'USB' if power['usb'] else 'Battery'} {power['percent']}%" if power else "Power status unavailable"
                matrix_txt = "Button map remembered locally" if self.s.matrix_known else "Button map unverified (factory defaults shown)"
                self.status.set(f"Ready · {power_txt} · {matrix_txt}")
                self.set_dirty(False)
            except Exception as e:
                self.err(e)
            finally:
                self.loading = False

        def want(self):
            original = self.s.orig
            dpi, colors = list(original["dpi"]), list(original["colors"])
            for i in range(MAX_UI_STAGES):
                dpi[i] = parse_dpi(self.dpi_vars[i].get())
                colors[i] = parse_rgb(self.col_vars[i].get())
            try:
                rate = int(self.rate.get())
            except (TypeError, ValueError):
                raise ValueError("The mouse reported an unsupported polling rate. Inspect it with `pulsar.py dump` before changing settings.")
            if rate not in P.CODE_FROM_RATE:
                raise ValueError("Polling rate must be one of 125, 250, 500, or 1000 Hz.")
            n_stages, current = int(self.nst.get()), int(self.cur.get())
            if not 1 <= n_stages <= MAX_UI_STAGES:
                raise ValueError(f"Active stages must be between 1 and {MAX_UI_STAGES}.")
            if not 1 <= current <= n_stages:
                raise ValueError("The selected current stage must be within the active DPI stages.")
            return {"rate": rate, "n_stages": n_stages, "cur_stage": current,
                    "dpi": dpi, "colors": colors, "led": self.want_led()}

        def want_matrix(self):
            matrix = list(self.s.matrix)
            macros = M.load()
            for slot in UI_SLOTS:
                matrix[slot - 1] = parse_button_ui(self.btn_vars[slot].get(), macros)
            if P.BTN_NAMED["left"] not in matrix:
                raise ValueError("At least one mouse button must remain mapped to left-click.")
            return matrix

        def apply(self):
            if self.s is None:
                return
            try:
                want, matrix = self.want(), self.want_matrix()
                matrix_changed = matrix != list(self.s.matrix)
                if matrix_changed:
                    known = "The remembered map will be backed up." if self.s.matrix_known else "The current mouse map cannot be read, so the factory-default base may not match the physical map."
                    if not messagebox.askyesno(
                        "Write button map?",
                        "This writes the complete 20-slot button matrix, including slots not shown in the UI.\n\n"
                        f"{known}\n\nContinue?",
                        parent=self,
                    ):
                        return
                result = self.s.apply(want, matrix)
            except Exception as e:
                return self.err(e)
            self.set_dirty(False)
            self.status.set(result.replace("\n", " · "))
            self.reload(force=True)
            if "differs from requested bytes" in result:
                messagebox.showwarning("Partial apply", result, parent=self)
            else:
                messagebox.showinfo("Settings applied", result, parent=self)

        def close(self):
            self._closing = True
            try:
                self.stop_listener()
            except Exception:
                pass
            if self.s is not None:
                try:
                    self.s.dev.close()
                except (AttributeError, OSError):
                    fds = {getattr(self.s.dev, "cfd", None), getattr(self.s.dev, "dfd", None)} - {None, -1}
                    for fd in fds:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
            self.destroy()

    try:
        App().mainloop()
    except SystemExit:
        return


if __name__ == "__main__":
    try:
        import tkinter  # noqa: F401
    except ImportError:
        sys.exit("tkinter missing: install the Python Tk package (Arch: sudo pacman -S tk)")
    run_gui()
