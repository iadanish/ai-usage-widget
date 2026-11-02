"""AI usage widget for Windows: plan usage for Claude Code, Codex, GitHub Copilot and the GLM Coding Plan.

Dock mode (default): a panel on the right screen edge; drag it up/down, "—" minimizes it to a slim handle.
Floating mode: a small pill you can drag anywhere; click it to open the card.
Ctrl+Alt+U hides/shows it from anywhere (Windows); right-click (or Control-click) for the menu.

  pythonw usage_widget.pyw            # last mode used (macOS/Linux: python3 usage_widget.pyw)
  pythonw usage_widget.pyw --dock     # right-edge dock
  pythonw usage_widget.pyw --float    # floating pill
  pythonw usage_widget.pyw --demo     # made-up accounts (no network, nothing read) for screenshots
  pythonw usage_widget.pyw --mask     # mask emails, e.g. for screen sharing

Windows, macOS (and Linux). Pure Python standard library; no packages to install.
"""
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usage_core  # noqa: E402
import usage_service  # noqa: E402  (hosted in-process, see Widget.__init__)

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = "--demo" in sys.argv
MASK = "--mask" in sys.argv or bool(usage_core.config().get("mask_emails"))
API = f"http://127.0.0.1:{usage_service.PORT}/api/usage"
STATE = os.path.join(usage_core.APP_DIR, "widget-demo.json" if DEMO else "widget.json")
POLL_MS = 120_000

BG, PANEL, LINE, TEXT, MUTE = "#1b1d22", "#23262d", "#30343c", "#e8e9ec", "#8b909a"
OK, WARN, BAD, IDLE, TRACK = "#4fbf7f", "#e9a23b", "#e5534b", "#555a64", "#3a3e47"
CLAUDE, CODEX, COPILOT, GLM = "#d97757", "#7d8cff", "#8957e5", "#2f9e8f"
PROV_COL = {"claude": CLAUDE, "codex": CODEX, "copilot": COPILOT, "glm": GLM}
PROV_GLYPH = {"claude": "✳", "codex": "⬡", "copilot": "◈", "glm": "Z"}
LOGIN = {"logged in": OK, "expired": WARN, "re-logged": WARN, "not logged in": IDLE, "never run": IDLE}  # login dot colours
IS_WIN, IS_MAC = os.name == "nt", sys.platform == "darwin"
F = "Segoe UI" if IS_WIN else "Helvetica Neue" if IS_MAC else "DejaVu Sans"

user32 = ctypes.windll.user32 if IS_WIN else None
if IS_WIN:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def colour(p, live=True):
    if p is None or not live:
        return IDLE
    return BAD if p >= 90 else WARN if p >= 70 else OK


def when(iso):
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%a %H:%M")
    except Exception:
        return ""


def passed(iso):
    try:
        return datetime.fromisoformat(iso).timestamp() <= time.time()
    except Exception:
        return False


def resets_at(iso):
    """'18:38' if today, else 'Thu 03:00'."""
    try:
        t = datetime.fromisoformat(iso).astimezone()
    except Exception:
        return "?"
    return t.strftime("%H:%M") if t.date() == datetime.now().date() else t.strftime("%a %H:%M")


def expiry(iso):
    """Banked-reset expiry in local time: 'Nov 7 04:58'."""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except Exception:
        return "?"
    return f"{t.strftime('%b')} {t.day} {t.strftime('%H:%M')}"


def borderless(root):
    """No title bar. macOS: Tk's 'plain' window style, which (unlike overrideredirect) still gets clicks."""
    if IS_MAC:
        try:
            root.tk.call("::tk::unsupported::MacWindowStyle", "style", root._w, "plain", "none")
            return
        except tk.TclError:
            pass
    root.overrideredirect(True)


def mask(email):
    """alice@example.com -> a•••@e•••.com (for screen sharing)."""
    if not MASK or "@" not in (email or ""):
        return email
    user, _, dom = email.partition("@")
    host, dot, tld = dom.rpartition(".")
    return f"{user[:1]}•••@{(host or dom)[:1]}•••{dot}{tld if host else ''}"


def load_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(**kw):
    s = load_state()
    s.update(kw)
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as fh:
            json.dump(s, fh)
    except Exception:
        pass


def ensure_service():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{usage_service.PORT}/health", timeout=2)
    except Exception:
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        subprocess.Popen([pyw if os.path.exists(pyw) else sys.executable, os.path.join(HERE, "usage_service.py")],
                         cwd=HERE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def hotkey_thread(q):
    """Ctrl+Alt+U anywhere -> toggle (Windows). RegisterHotKey with no window posts WM_HOTKEY to this thread."""
    if not IS_WIN:
        return
    import ctypes.wintypes as wt
    MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x0312
    if not user32.RegisterHotKey(None, 1, MOD_ALT | MOD_CONTROL | MOD_NOREPEAT, ord("U")):
        return
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY:
            q.put("toggle")


class Widget:
    def __init__(self):
        self.root = tk.Tk()
        borderless(self.root)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        self.data, self.error, self.open, self.tab = None, None, "--open" in sys.argv, load_state().get("tab", "all")
        self.mode = "float" if "--float" in sys.argv else "dock" if "--dock" in sys.argv else load_state().get("mode", "dock")
        self.collapsed = bool(load_state().get("collapsed", False))
        self.q = queue.Queue()
        self.drag = None
        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.menu = tk.Menu(self.root, tearoff=0, bg=PANEL, fg=TEXT, activebackground=LINE, activeforeground=TEXT, bd=0)
        self.menu.add_command(label="Refresh", command=lambda: self.refresh(force=True))
        self.menu.add_command(label="Reset position", command=self.reset_pos)
        if IS_WIN:  # without the global hotkey there'd be no way to bring a hidden widget back
            self.menu.add_command(label="Hide (Ctrl+Alt+U to show)", command=self.hide)
        self.menu.add_separator()
        self.menu.add_command(label="Quit widget", command=self.root.destroy)
        self.render()
        self.root.update_idletasks()
        self.round_corners()
        self.place(load_state().get("pos"))
        threading.Thread(target=hotkey_thread, args=(self.q,), daemon=True).start()
        self.root.after(400, self.pump)
        # Host the usage service inside this process (one process instead of two). If another
        # usage_service already owns the port, read from it over HTTP instead.
        self.embedded = False if DEMO else usage_service.start()
        self.refresh()

    # ---------- window plumbing ----------
    def bind_menu(self, w):
        popup = lambda e: self.menu.tk_popup(e.x_root, e.y_root)
        w.bind("<Button-3>", popup)
        if IS_MAC:  # two-finger click / Control-click
            w.bind("<Button-2>", popup)
            w.bind("<Control-Button-1>", popup)

    def hwnd(self):
        return user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()

    def round_corners(self):
        if not IS_WIN:
            return
        try:  # Windows 11 rounded corners (DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2)
            v = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self.hwnd(), 33, ctypes.byref(v), ctypes.sizeof(v))
        except Exception:
            pass

    def virtual_screen(self):
        if not IS_WIN:
            return 0, 0, self.root.winfo_vrootwidth() or self.root.winfo_screenwidth(), self.root.winfo_vrootheight() or self.root.winfo_screenheight()
        g = user32.GetSystemMetrics
        return g(76), g(77), g(78), g(79)  # x, y, w, h across all monitors

    def default_pos(self):
        h, w = self.root.winfo_reqheight(), self.root.winfo_reqwidth()
        return self.root.winfo_screenwidth() - w - 12, max(0, (self.root.winfo_screenheight() - h) // 2)

    def place(self, pos=None):
        self.root.update_idletasks()
        vx, vy, vw, vh = self.virtual_screen()
        if pos and vx <= pos[0] <= vx + vw - 40 and vy <= pos[1] <= vy + vh - 30:
            x, y = pos
        else:
            x, y = self.default_pos()
        self.root.geometry(f"+{x}+{y}")

    def reset_pos(self):
        save_state(pos=None, dock_y=None)
        if self.mode == "dock":
            self.dock_place()
        else:
            self.place(None)

    def dock_place(self, y=None):
        """Right edge; y is the remembered drag position (clamped to the screen), default vertically centred."""
        self.root.update_idletasks()
        sh, h = self.root.winfo_screenheight(), self.root.winfo_reqheight()
        if y is None:
            y = load_state().get("dock_y")
        y = (sh - h) // 2 if y is None else y
        y = max(0, min(int(y), sh - h))
        self.root.geometry(f"+{self.root.winfo_screenwidth() - self.root.winfo_reqwidth()}+{y}")

    def bind_vdrag(self, w, on_click=None):
        """Dock drag: up/down only, stays on the right edge. A click without movement runs on_click."""
        if str(w.cget("cursor")) == "hand2":  # buttons keep their own click handler
            return

        def press(e):
            self.drag = (e.y_root, self.root.winfo_y(), False)

        def motion(e):
            if not self.drag:
                return
            sy, wy, moved = self.drag
            dy = e.y_root - sy
            if moved or abs(dy) > 4:
                self.drag = (sy, wy, True)
                self.dock_place(wy + dy)

        def release(e):
            moved = self.drag and self.drag[2]
            self.drag = None
            if moved:
                save_state(dock_y=self.root.winfo_y())
            elif on_click:
                on_click()

        w.bind("<ButtonPress-1>", press)
        w.bind("<B1-Motion>", motion)
        w.bind("<ButtonRelease-1>", release)
        self.bind_menu(w)
        for c in w.winfo_children():
            self.bind_vdrag(c, on_click)

    def bind_drag(self, w, on_click=None):
        if str(w.cget("cursor")) == "hand2":  # buttons/tabs keep their own click handler
            return

        def press(e):
            self.drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y(), False)

        def motion(e):
            if not self.drag:
                return
            sx, sy, wx, wy, moved = self.drag
            dx, dy = e.x_root - sx, e.y_root - sy
            if moved or abs(dx) + abs(dy) > 4:
                self.drag = (sx, sy, wx, wy, True)
                self.root.geometry(f"+{wx + dx}+{wy + dy}")

        def release(e):
            moved = self.drag and self.drag[4]
            self.drag = None
            if moved:
                save_state(pos=[self.root.winfo_x(), self.root.winfo_y()])
            elif on_click:
                on_click()

        w.bind("<ButtonPress-1>", press)
        w.bind("<B1-Motion>", motion)
        w.bind("<ButtonRelease-1>", release)
        self.bind_menu(w)
        for c in w.winfo_children():
            self.bind_drag(c, on_click)

    def pump(self):
        try:
            while True:
                if self.q.get_nowait() == "toggle":
                    if self.root.state() == "withdrawn":
                        self.root.deiconify()
                        self.root.attributes("-topmost", True)
                    else:
                        self.root.withdraw()
        except queue.Empty:
            pass
        self.root.after(400, self.pump)

    # ---------- data ----------
    def refresh(self, force=False):
        if getattr(self, "_next", None):
            self.root.after_cancel(self._next)
            self._next = None

        def work():
            try:
                if DEMO:
                    d, err = usage_core.demo(), None
                elif self.embedded:
                    d = None if force else usage_service.latest()
                    for _ in range(40):  # first start: wait for the service's own collection instead of doubling it
                        if d or force:
                            break
                        time.sleep(0.25)
                        d = usage_service.latest()
                    d, err = d or usage_service.refresh_now(), None
                else:
                    with urllib.request.urlopen(API, timeout=10) as r:
                        d, err = json.load(r), None
            except Exception as e:
                d, err = None, "service starting…" if "503" in str(e) else "service offline"
                ensure_service()
            self.root.after(0, lambda: self.update(d, err))
        threading.Thread(target=work, daemon=True).start()

    def update(self, d, err):
        if d:
            self.data, self.error = d, None
        else:
            self.error = err
        self.render()
        self._next = self.root.after(POLL_MS if d else 10_000, self.refresh)

    def items(self):
        """Uniform rows: (provider, name, percent, live, detail, email, login status, is_default)."""
        out = []
        d = self.data or {}
        for r in (d.get("claude") or {}).get("rows", []):
            s = r.get("session")
            name = r["name"]
            if r.get("note") or not s:
                out.append(("claude", name, None, False, "idle" if "expired" in (r.get("note") or "") else (r.get("note") or "")[:22], r.get("email", ""), r.get("login", "?"), r.get("default", False)))
            else:
                parts = ["5h free" if not s["percent"] else f"5h ↻ {resets_at(s.get('resets'))}"]
                for w in r.get("weekly") or []:  # some plans have a model-scoped weekly limit, others an overall one
                    lbl = "wk" if w.get("label") in (None, "all") else f"{w['label']} wk"
                    parts.append(f"{lbl} {w['percent']}% ↻ {resets_at(w.get('resets'))}")
                if r.get("stale_since"):  # last check failed; these are the last good figures
                    parts.append(f"as of {resets_at(r['stale_since'])}")
                sub = " · ".join(parts)
                out.append(("claude", name, s["percent"], True, sub, r.get("email", ""), r.get("login", "?"), r.get("default", False)))
        for r in (d.get("codex") or {}).get("rows", []):
            s, wk, st = r.get("session"), r.get("weekly"), r.get("state")
            if not s:
                sub = "re-logged, run once" if st == "identity-changed" else (st or "")
                out.append(("codex", r["seat"], None, False, sub, r.get("account") or "", r.get("login", "?"), False))
                continue
            pct, live = s["percent"], st == "fresh"
            if passed(s.get("resets")):  # 5h window has reset since the figure was recorded: it's free again
                pct, live, parts = 0, True, ["5h available"]
            elif not pct:  # unused window: its "reset" is just now + 5h, so say it's free instead
                parts = ["5h free"]
            else:
                parts = [f"5h ↻ {resets_at(s.get('resets'))}"]
            if wk:
                parts.append("wk available" if passed(wk.get("resets")) else f"wk {wk['percent']}% ↻ {resets_at(wk.get('resets'))}")
            if r.get("banked_list"):
                parts.append(f"↺ {len(r['banked_list'])} banked, exp " + ", ".join(expiry(c["expires"]) for c in r["banked_list"]))
            elif r.get("banked") is not None:
                parts.append(f"↺ {r['banked']} banked")
            out.append(("codex", r["seat"], pct, live, " · ".join(parts), r.get("account") or "", r.get("login", "?"), False))
        for r in (d.get("copilot") or {}).get("rows", []):
            if r.get("note"):
                out.append(("copilot", r["login"], None, False, r["note"], r.get("plan") or "", r.get("status", "?"), False))
                continue
            prem = r.get("premium")
            parts = ["premium: unlimited" if r.get("unlimited") else
                     f"premium {prem['percent']}% · {prem.get('remaining')} of {prem.get('entitlement')} left" if prem else "premium: –"]
            if r.get("resets"):
                parts.append(f"resets {expiry(r['resets']).rsplit(' ', 1)[0]}")
            out.append(("copilot", r["login"], None if r.get("unlimited") else (prem or {}).get("percent"), True,
                        " · ".join(parts), f"Copilot {r.get('plan') or ''}".strip(), r.get("status", "?"), False))
        for r in (d.get("glm") or {}).get("rows", []):
            if r.get("note"):
                out.append(("glm", "glm", None, False, r["note"], r.get("name", ""), r.get("status", "?"), False))
                continue
            lims = r.get("limits") or []
            parts, top = [], None
            for lim in lims:
                # window = number x unit (z.ai units: 3 = hours, 6 = weeks)
                n, unit = lim.get("number"), lim.get("unit")
                lbl = f"{n}h" if unit == 3 else ("wk" if n == 1 else f"{n}wk") if unit == 6 else                     {"TIME_LIMIT": "tools", "TOKENS_LIMIT": "tokens"}.get(lim.get("type"), (lim.get("type") or "").lower()[:8])
                pc = lim.get("percent")
                top = pc if pc is not None and (top is None or pc > top) else top
                txt = f"{lbl} {pc}%" if pc is not None else lbl
                if lim.get("total"):
                    txt += f" ({lim.get('used') or 0}/{lim['total']})"
                if lim.get("resets"):
                    txt += f" ↻ {resets_at(lim['resets'])}" if not passed(lim["resets"]) else " available"
                parts.append(txt)
            out.append(("glm", "glm", top, True, " · ".join(parts) or "no limits reported",
                        f"GLM Coding Plan {r.get('plan') or ''}".strip(), r.get("status", "?"), False))
        return out

    def providers(self):
        """Providers that have at least one account on this machine (Claude and Codex always listed)."""
        its = self.items()
        return ["claude", "codex"] + [p_ for p_ in ("copilot", "glm") if any(i[0] == p_ for i in its)]

    @staticmethod
    def order(i):
        """Codex/Copilot/GLM in name order; Claude live first, highest usage first."""
        return (0, i[1]) if i[0] != "claude" else (not i[3], -(i[2] or -1))

    def worst(self, prov):
        ps = [p for v, _, p, live, *_ in self.items() if v == prov and live and p is not None]
        return max(ps) if ps else None

    # ---------- drawing ----------
    def icon(self, parent, prov, size=18):
        c = tk.Canvas(parent, width=size, height=size, bg=parent["bg"], highlightthickness=0)
        r, col = 5, PROV_COL[prov]
        c.create_polygon(r, 0, size - r, 0, size, 0, size, r, size, size - r, size, size, size - r, size, r, size,
                         0, size, 0, size - r, 0, r, 0, 0, fill=col, smooth=True)
        c.create_text(size / 2, size / 2 + 0.5, text=PROV_GLYPH[prov], fill="white", font=(F, int(size * 0.45), "bold"))
        return c

    def pct_label(self, parent, p, live=True, size=10):
        return tk.Label(parent, text="–" if p is None else f"{p}%", fg=colour(p, live) if p is not None else MUTE,
                        bg=parent["bg"], font=(F, size, "bold"))

    def render(self):
        for w in self.body.winfo_children():
            w.destroy()
        if self.mode == "dock":
            self.dock()
            self.root.update_idletasks()
            self.dock_place()
            return
        self.pill()
        if self.open:
            self.card()
        self.bind_drag(self.body, on_click=None)
        self.root.update_idletasks()

    def set_mode(self, mode):
        self.mode = mode
        save_state(mode=mode)
        self.render()
        if mode == "float":
            self.place(load_state().get("pos"))

    def hide(self):
        self.root.withdraw()

    def set_collapsed(self, v):
        self.collapsed = v
        save_state(collapsed=v)
        self.render()

    def btn(self, parent, txt, cmd, tip=None, size=10):
        b = tk.Label(parent, text=txt, fg=MUTE, bg=parent["bg"], font=(F, size), cursor="hand2", padx=3)
        b.bind("<ButtonRelease-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(fg=TEXT))
        b.bind("<Leave>", lambda e: b.configure(fg=MUTE))
        self.bind_menu(b)
        return b

    def dock(self):
        """Right-edge panel (default). Collapses to a slim handle; the ⇱ icon switches to the floating pill."""
        its = self.items()
        if self.collapsed:
            h = tk.Frame(self.body, bg=BG, width=16, padx=4, pady=10, cursor="sb_v_double_arrow")
            h.pack()
            for prov in self.providers():
                w = self.worst(prov)
                for col in (PROV_COL[prov], colour(w)):
                    d = tk.Canvas(h, width=8, height=8, bg=BG, highlightthickness=0, cursor="sb_v_double_arrow")
                    d.create_oval(1, 1, 7, 7, fill=col, outline="")
                    d.pack(pady=2)
            self.bind_vdrag(h, on_click=lambda: self.set_collapsed(False))
            return
        p = tk.Frame(self.body, bg=BG, padx=12, pady=10)
        p.pack(fill="both")
        hd = tk.Frame(p, bg=BG)
        hd.pack(fill="x")
        tk.Label(hd, text="AI usage", fg=TEXT, bg=BG, font=(F, 10, "bold")).pack(side="left")
        tk.Label(hd, text=("demo data · " if DEMO else "") + (self.error or self.ago()), fg=MUTE, bg=BG, font=(F, 8)).pack(side="left", padx=8)
        self.btn(hd, "—", lambda: self.set_collapsed(True)).pack(side="right")  # minimize to the edge handle
        self.btn(hd, "⇱", lambda: self.set_mode("float")).pack(side="right")
        self.btn(hd, "⟳", lambda: self.refresh(force=True)).pack(side="right")
        for prov, label in ((p_, "GLM" if p_ == "glm" else p_.capitalize()) for p_ in self.providers()):
            rows = sorted([i for i in its if i[0] == prov], key=self.order)
            near = sum(1 for i in rows if i[3] and i[2] is not None and i[2] >= 70)
            sec = tk.Frame(p, bg=BG)
            sec.pack(fill="x", pady=(10, 2))
            self.icon(sec, prov, 16).pack(side="left")
            tk.Label(sec, text=label.upper(), fg=MUTE, bg=BG, font=(F, 8, "bold")).pack(side="left", padx=6)
            tk.Label(sec, text=f"{len(rows)}" + (f" · {near} near limit" if near else ""), fg=MUTE, bg=BG, font=(F, 8)).pack(side="right")
            g = tk.Frame(p, bg=BG)
            g.pack(fill="x")
            for col, px in ((0, 175), (1, 190), (3, 48), (4, 470)):  # same widths in both sections so columns line up
                g.grid_columnconfigure(col, minsize=px)
            for i, (prv, name, pc, live, sub, email, login, dflt) in enumerate(rows):
                fg = TEXT if live else MUTE
                nm = tk.Frame(g, bg=BG)
                nm.grid(row=i, column=0, sticky="w")
                d = tk.Canvas(nm, width=8, height=8, bg=BG, highlightthickness=0)
                d.create_oval(1, 1, 7, 7, fill=LOGIN.get(login, IDLE), outline="")
                d.pack(side="left", padx=(0, 5))
                tk.Label(nm, text=name[:20], fg=fg, bg=BG, font=(F, 9, "bold" if live else "normal")).pack(side="left")
                if dflt:
                    tk.Label(nm, text="default", fg=BG, bg=MUTE, font=(F, 7, "bold"), padx=3).pack(side="left", padx=(5, 0))
                tk.Label(g, text=mask(email)[:30], fg=MUTE, bg=BG, font=(F, 8), anchor="w").grid(row=i, column=1, sticky="w", padx=(8, 0))
                bar = tk.Canvas(g, width=110, height=6, bg=BG, highlightthickness=0)
                bar.create_rectangle(0, 0, 110, 6, fill=TRACK, outline="")
                if pc:
                    bar.create_rectangle(0, 0, int(110 * min(pc, 100) / 100), 6, fill=colour(pc, live), outline="")
                bar.grid(row=i, column=2, padx=8, pady=3)
                unl = prv == "copilot" and pc is None and live  # unlimited plan
                tk.Label(g, text=("∞" if unl else "–") if pc is None else f"{pc}%", fg=OK if unl else colour(pc, live) if pc is not None else MUTE, bg=BG,
                         font=(F, 9, "bold"), anchor="e", width=5).grid(row=i, column=3, sticky="e")
                tk.Label(g, text=sub[:110], fg=MUTE, bg=BG, font=(F, 8), anchor="w").grid(row=i, column=4, sticky="w", padx=(8, 0))
        p.configure(cursor="sb_v_double_arrow")
        self.bind_vdrag(p)

    def ago(self):
        if not self.data:
            return ""
        mins = int((time.time() - datetime.fromisoformat(self.data["checked"]).timestamp()) // 60)
        return "just now" if mins < 1 else f"{mins} min ago"

    def pill(self):
        p = tk.Frame(self.body, bg=BG, padx=12, pady=7)
        p.pack(anchor="w")
        if self.error and not self.data:
            tk.Label(p, text=self.error, fg=MUTE, bg=BG, font=(F, 10)).pack(side="left")
        for i, prov in enumerate(self.providers()):
            if i:
                tk.Frame(p, bg=LINE, width=1, height=14).pack(side="left", padx=8)
            self.icon(p, prov).pack(side="left", padx=(0, 6))
            if prov == "copilot" and self.worst(prov) is None and any(i[0] == "copilot" and i[3] for i in self.items()):
                tk.Label(p, text="∞", fg=OK, bg=BG, font=(F, 11, "bold")).pack(side="left")
            else:
                self.pct_label(p, self.worst(prov)).pack(side="left")
        near = sum(1 for _, _, pc, live, *_ in self.items() if live and pc is not None and pc >= 70)
        if near:
            tk.Label(p, text=f"· {near} near limit", fg=MUTE, bg=BG, font=(F, 9)).pack(side="left", padx=(8, 0))
        p.after_idle(lambda: self.bind_drag(p, on_click=self.toggle_card))

    def toggle_card(self):
        self.open = not self.open
        self.render()

    def card(self):
        c = tk.Frame(self.body, bg=BG, padx=12, pady=4)
        c.pack(fill="both")
        tk.Frame(c, bg=LINE, height=1).pack(fill="x", pady=(0, 8))
        hd = tk.Frame(c, bg=BG)
        hd.pack(fill="x")
        tk.Label(hd, text="AI usage", fg=TEXT, bg=BG, font=(F, 10, "bold")).pack(side="left")
        ago = ""
        if self.data:
            mins = int((time.time() - datetime.fromisoformat(self.data["checked"]).timestamp()) // 60)
            ago = "just now" if mins < 1 else f"{mins} min ago"
        tk.Label(hd, text=self.error or ago, fg=MUTE, bg=BG, font=(F, 8)).pack(side="left", padx=8)
        self.btn(hd, "—", self.toggle_card).pack(side="right")
        self.btn(hd, "⇤", lambda: self.set_mode("dock")).pack(side="right")
        self.btn(hd, "⟳", lambda: self.refresh(force=True)).pack(side="right")
        tabs = tk.Frame(c, bg=BG)
        tabs.pack(fill="x", pady=8)
        its = self.items()
        for key, label in (("claude", f"Claude · {sum(1 for i in its if i[0] == 'claude')}"),
                           ("codex", f"Codex · {sum(1 for i in its if i[0] == 'codex')}"),
                           *[(p_, f"{'GLM' if p_ == 'glm' else p_.capitalize()} · {sum(1 for i in its if i[0] == p_)}") for p_ in self.providers()[2:]],
                           ("all", "All")):
            on = self.tab == key
            t = tk.Label(tabs, text=label, fg=TEXT if on else MUTE, bg=PANEL if on else BG, font=(F, 9), padx=10, pady=3, cursor="hand2")
            t.pack(side="left", padx=(0, 4))
            t.bind("<ButtonRelease-1>", lambda e, k=key: self.set_tab(k))
        shown = [i for i in its if self.tab in ("all", i[0])]
        shown.sort(key=lambda i: (i[0] != "claude", self.order(i)))  # Claude first, then Codex
        grid = tk.Frame(c, bg=BG)
        grid.pack(pady=(0, 10))
        for n, (prov, name, pc, live, sub, email, login, dflt) in enumerate(shown):
            self.tile(grid, prov, name, pc, live, sub, email, login, dflt).grid(row=n // 4, column=n % 4, padx=4, pady=4)

    def set_tab(self, k):
        self.tab = k
        save_state(tab=k)
        self.render()

    def tile(self, parent, prov, name, pc, live, sub, email="", login="?", dflt=False):
        t = tk.Frame(parent, bg=PANEL, width=240, height=212)
        t.grid_propagate(False)
        t.pack_propagate(False)
        ring = tk.Canvas(t, width=68, height=68, bg=PANEL, highlightthickness=0)
        ring.pack(pady=(8, 2))
        ring.create_oval(5, 5, 63, 63, outline=TRACK, width=7)
        if pc:
            ring.create_arc(5, 5, 63, 63, start=90, extent=-3.6 * min(pc, 100) + (0.01 if pc >= 100 else 0),
                            style="arc", outline=colour(pc, live), width=7)
        ring.create_text(34, 34, text=("∞" if prov == "copilot" and live else "–") if pc is None else str(pc), fill=TEXT if live else MUTE, font=(F, 14, "bold"))
        row = tk.Frame(t, bg=PANEL)
        row.pack()
        dot = tk.Canvas(row, width=8, height=8, bg=PANEL, highlightthickness=0)
        dot.create_oval(1, 1, 7, 7, fill=LOGIN.get(login, IDLE), outline="")
        dot.pack(side="left", padx=(0, 4))
        tk.Label(row, text=name[:20] + (" ★" if dflt else ""), fg=TEXT if live else MUTE, bg=PANEL, font=(F, 10, "bold")).pack(side="left")
        tk.Label(t, text=mask(email)[:28], fg=TEXT if live else MUTE, bg=PANEL, font=(F, 8)).pack()
        tk.Label(t, text=sub.replace(" · ", chr(10), 1)[:140], fg=MUTE, bg=PANEL, font=(F, 8), wraplength=224, justify="center").pack(padx=6)
        return t

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Widget().run()
