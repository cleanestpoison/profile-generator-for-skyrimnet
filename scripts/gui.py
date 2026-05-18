"""
Tkinter GUI wrapper around the two CLI stages.

Pick an .esp, pick an output dir, hit Extract to produce per-NPC dossiers,
then hit Synthesize to turn every dossier in that dir into a .prompt bio.
Both buttons shell out to the existing scripts as subprocesses and stream
their stdout/stderr into the log pane line-by-line.

Run:
    python scripts/gui.py
"""

from __future__ import annotations
import os
import sys
import queue
import signal
import threading
import subprocess
from pathlib import Path
from tkinter import (
    Tk, StringVar, BooleanVar, IntVar, END, DISABLED, NORMAL, WORD,
    filedialog, messagebox,
)
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

REPO_ROOT = Path(__file__).resolve().parents[1]
EXTRACT_SCRIPT = REPO_ROOT / "scripts" / "extract_npc_dialogue.py"
SYNTH_SCRIPT   = REPO_ROOT / "scripts" / "synthesize_bio.py"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "output"

MODELS = ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]


def safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in (name or "")).strip("_").lower() or "unnamed"


class DialogueAnalyserGUI:
    def __init__(self, root: Tk) -> None:
        self.root = root
        root.title("SkyrimNet Dialogue Analyser")
        root.geometry("960x640")
        root.minsize(720, 480)

        self.esp_path     = StringVar()
        self.out_dir      = StringVar()
        self.model        = StringVar(value=MODELS[0])
        self.overwrite    = BooleanVar(value=False)
        self.concurrency  = IntVar(value=5)
        self.limit        = IntVar(value=0)  # 0 = no limit

        self.proc: subprocess.Popen | None = None
        self.log_queue: queue.Queue[tuple[str, str] | None] = queue.Queue()

        self._build_ui()
        self._poll_log_queue()

    # ---- UI layout ---------------------------------------------------------

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        frm = ttk.Frame(self.root)
        frm.pack(fill="x", **pad)

        # ESP picker
        ttk.Label(frm, text="Plugin (.esp):").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.esp_path).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(frm, text="Browse…", command=self._pick_esp).grid(row=0, column=2)

        # Output dir picker
        ttk.Label(frm, text="Output dir:").grid(row=1, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.out_dir).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(frm, text="Browse…", command=self._pick_outdir).grid(row=1, column=2)

        frm.columnconfigure(1, weight=1)

        # Options
        opts = ttk.LabelFrame(self.root, text="Synthesis options")
        opts.pack(fill="x", **pad)

        ttk.Label(opts, text="Model:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Combobox(opts, textvariable=self.model, values=MODELS, width=28,
                     state="readonly").grid(row=0, column=1, sticky="w")

        ttk.Checkbutton(opts, text="Overwrite existing .prompt files",
                        variable=self.overwrite).grid(row=0, column=2, sticky="w", padx=16)

        ttk.Label(opts, text="Concurrency:").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Spinbox(opts, from_=1, to=20, width=6, textvariable=self.concurrency)\
            .grid(row=1, column=1, sticky="w")

        ttk.Label(opts, text="Limit (0 = all):").grid(row=1, column=2, sticky="e", padx=4)
        ttk.Spinbox(opts, from_=0, to=999, width=6, textvariable=self.limit)\
            .grid(row=1, column=3, sticky="w")

        # Action buttons
        actions = ttk.Frame(self.root)
        actions.pack(fill="x", **pad)
        self.btn_extract = ttk.Button(actions, text="1. Extract dossiers",
                                      command=self._run_extract)
        self.btn_extract.pack(side="left", padx=4)
        self.btn_synth = ttk.Button(actions, text="2. Synthesize bios",
                                    command=self._run_synth)
        self.btn_synth.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(actions, text="Stop",
                                   command=self._stop, state=DISABLED)
        self.btn_stop.pack(side="left", padx=4)
        self.btn_open = ttk.Button(actions, text="Open output folder",
                                   command=self._open_output)
        self.btn_open.pack(side="right", padx=4)

        # Log pane
        self.log = ScrolledText(self.root, wrap=WORD, height=20, state=DISABLED,
                                font=("Consolas", 10))
        self.log.pack(fill="both", expand=True, **pad)
        self.log.tag_config("err",  foreground="#cc3333")
        self.log.tag_config("info", foreground="#225599")
        self.log.tag_config("ok",   foreground="#338833")

        # Status bar
        self.status = StringVar(value="Ready.")
        ttk.Label(self.root, textvariable=self.status, anchor="w",
                  relief="sunken").pack(fill="x", side="bottom")

    # ---- pickers -----------------------------------------------------------

    def _pick_esp(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Skyrim plugin",
            filetypes=[("Skyrim plugins", "*.esp *.esm *.esl"), ("All files", "*.*")],
        )
        if not path:
            return
        self.esp_path.set(path)
        # Auto-fill output dir if blank
        if not self.out_dir.get():
            base = safe_filename(Path(path).stem)
            self.out_dir.set(str(DEFAULT_OUTPUT_ROOT / base))

    def _pick_outdir(self) -> None:
        path = filedialog.askdirectory(title="Select output directory")
        if path:
            self.out_dir.set(path)

    def _open_output(self) -> None:
        path = self.out_dir.get().strip()
        if not path or not Path(path).exists():
            messagebox.showinfo("Open folder", "Output directory doesn't exist yet.")
            return
        # Windows-friendly open
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except AttributeError:
            subprocess.Popen(["xdg-open", path])

    # ---- run / stop --------------------------------------------------------

    def _run_extract(self) -> None:
        esp = self.esp_path.get().strip()
        if not esp or not Path(esp).is_file():
            messagebox.showerror("Missing plugin", "Pick a valid .esp file first.")
            return
        out = self.out_dir.get().strip()
        if not out:
            out = str(DEFAULT_OUTPUT_ROOT / safe_filename(Path(esp).stem))
            self.out_dir.set(out)
        Path(out).mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-u", str(EXTRACT_SCRIPT), esp, out]
        self._spawn(cmd, label="extract")

    def _run_synth(self) -> None:
        out = self.out_dir.get().strip()
        if not out or not Path(out).is_dir():
            messagebox.showerror(
                "Missing dossiers",
                "Output directory doesn't exist. Run Extract first, or pick the "
                "directory containing the dossier .md files.",
            )
            return
        # Must have at least one .md dossier that isn't _unattributed
        dossiers = [p for p in Path(out).iterdir()
                    if p.suffix == ".md" and not p.name.startswith("_")]
        if not dossiers:
            messagebox.showerror(
                "No dossiers found",
                f"No dossier .md files in {out}. Run Extract first.",
            )
            return
        cmd = [sys.executable, "-u", str(SYNTH_SCRIPT), out,
               "--model", self.model.get(),
               "--concurrency", str(max(1, self.concurrency.get()))]
        if self.overwrite.get():
            cmd.append("--overwrite")
        if self.limit.get() > 0:
            cmd += ["--limit", str(self.limit.get())]
        self._spawn(cmd, label="synthesize")

    def _spawn(self, cmd: list[str], label: str) -> None:
        if self.proc is not None and self.proc.poll() is None:
            messagebox.showwarning("Already running",
                                   "A job is already running. Stop it first.")
            return
        self._set_running(True)
        self._append(f"\n$ {' '.join(cmd)}\n", "info")
        self.status.set(f"Running {label}…")

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(REPO_ROOT),
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                              if os.name == "nt" else 0,
            )
        except Exception as e:
            self._append(f"Failed to start: {e}\n", "err")
            self._set_running(False)
            return

        threading.Thread(target=self._reader_thread,
                         args=(self.proc, label), daemon=True).start()

    def _reader_thread(self, proc: subprocess.Popen, label: str) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self.log_queue.put(("line", line))
        rc = proc.wait()
        tag = "ok" if rc == 0 else "err"
        self.log_queue.put((tag, f"\n[{label} exited with code {rc}]\n"))
        self.log_queue.put(None)  # sentinel: re-enable buttons

    def _stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self._append("\n[stop requested]\n", "info")
        try:
            if os.name == "nt":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.terminate()
        except Exception as e:
            self._append(f"stop failed: {e}\n", "err")

    # ---- log pump (Tk-thread) ---------------------------------------------

    def _poll_log_queue(self) -> None:
        try:
            while True:
                item = self.log_queue.get_nowait()
                if item is None:
                    self._set_running(False)
                    self.status.set("Ready.")
                    continue
                tag, text = item
                self._append(text, tag if tag in ("err", "ok", "info") else None)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_log_queue)

    def _append(self, text: str, tag: str | None = None) -> None:
        self.log.configure(state=NORMAL)
        if tag:
            self.log.insert(END, text, tag)
        else:
            self.log.insert(END, text)
        self.log.see(END)
        self.log.configure(state=DISABLED)

    def _set_running(self, running: bool) -> None:
        state_run  = DISABLED if running else NORMAL
        state_stop = NORMAL   if running else DISABLED
        self.btn_extract.configure(state=state_run)
        self.btn_synth.configure(state=state_run)
        self.btn_stop.configure(state=state_stop)


def main() -> None:
    root = Tk()
    DialogueAnalyserGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
