import queue
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractOcrOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

APP_TITLE = "DJ:s PDF till Markdown app"
VERSION = "0.2.0"


def _build_converter() -> DocumentConverter:
    opts = PdfPipelineOptions()
    opts.do_ocr = True
    opts.ocr_options = TesseractOcrOptions(lang=["swe"])
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )


class WorkerThread(threading.Thread):
    def __init__(
        self,
        pdf_paths: list,
        output_dir: Path,
        w2g: queue.Queue,
        g2w: queue.Queue,
        cancel_event: threading.Event,
        conflict_event: threading.Event,
    ):
        super().__init__(daemon=True)
        self.pdf_paths = pdf_paths
        self.output_dir = output_dir
        self.w2g = w2g
        self.g2w = g2w
        self.cancel_event = cancel_event
        self.conflict_event = conflict_event

    def run(self):
        ok_count = 0
        skip_count = 0
        err_count = 0
        skip_all = False
        overwrite_all = False
        total = len(self.pdf_paths)

        try:
            converter = _build_converter()
        except Exception as exc:
            self.w2g.put(("log", f"Fel vid initiering: {exc}"))
            self.w2g.put(("done", 0, 0, total))
            return

        for idx, pdf_path in enumerate(self.pdf_paths):
            if self.cancel_event.is_set():
                break

            self.w2g.put(("progress", idx + 1, total, pdf_path.name))
            output_path = self.output_dir / (pdf_path.stem + ".md")

            # ── Conflict check ───────────────────────────────
            if output_path.exists() and not overwrite_all:
                if skip_all:
                    self.w2g.put(("log", f"Hoppar över: {pdf_path.name}"))
                    skip_count += 1
                    continue

                self.w2g.put(("conflict", str(output_path)))
                self.conflict_event.wait()
                self.conflict_event.clear()

                action = self.g2w.get()[1]

                if action == "cancel":
                    self.cancel_event.set()
                    break
                elif action == "skip":
                    self.w2g.put(("log", f"Hoppar över: {pdf_path.name}"))
                    skip_count += 1
                    continue
                elif action == "skip_all":
                    skip_all = True
                    self.w2g.put(("log", f"Hoppar över: {pdf_path.name}"))
                    skip_count += 1
                    continue
                elif action == "overwrite_all":
                    overwrite_all = True
                # "overwrite" and "overwrite_all" fall through to conversion

            # ── Convert ──────────────────────────────────────
            try:
                result = next(
                    converter.convert_all([pdf_path], raises_on_error=False)
                )
                if result.status == ConversionStatus.SUCCESS:
                    output_path.write_text(
                        result.document.export_to_markdown(), encoding="utf-8"
                    )
                    self.w2g.put(("log", f"✓ {pdf_path.name}"))
                    ok_count += 1
                else:
                    msgs = "; ".join(e.error_message for e in result.errors)
                    self.w2g.put(("error", pdf_path.name, msgs))
                    err_count += 1
            except Exception as exc:
                self.w2g.put(("error", pdf_path.name, str(exc)))
                err_count += 1

        self.w2g.put(("done", ok_count, skip_count, err_count))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.resizable(True, True)
        self.minsize(720, 580)

        self._pdf_paths: list = []
        self._output_dir = tk.StringVar()
        self._running = False
        self._worker = None
        self._w2g: queue.Queue = queue.Queue()
        self._g2w: queue.Queue = queue.Queue()
        self._cancel_event = threading.Event()
        self._conflict_event = threading.Event()

        self._build_ui()
        self.after(200, self._check_tesseract)

    # ─────────────────────────────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        pad = {"padx": 10, "pady": 5}

        # ── PDF-filer ─────────────────────────────────────────────────
        pdf_frame = ttk.LabelFrame(self, text="PDF-filer")
        pdf_frame.pack(fill=tk.BOTH, expand=True, **pad)

        btn_row = ttk.Frame(pdf_frame)
        btn_row.pack(fill=tk.X, padx=4, pady=(4, 2))
        ttk.Button(btn_row, text="Lägg till filer…",  command=self._add_files).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="Lägg till mapp…",   command=self._add_folder).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="Ta bort markerade", command=self._remove_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="Rensa lista",        command=self._clear_list).pack(side=tk.LEFT, padx=2)

        list_frame = ttk.Frame(pdf_frame)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        self._listbox = tk.Listbox(list_frame, selectmode=tk.EXTENDED, height=8)
        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self._listbox.yview)
        self._listbox.configure(yscrollcommand=sb.set)
        self._listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Utdatamapp ────────────────────────────────────────────────
        out_frame = ttk.LabelFrame(self, text="Utdatamapp")
        out_frame.pack(fill=tk.X, **pad)
        ttk.Entry(out_frame, textvariable=self._output_dir, state="readonly").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 2), pady=4
        )
        ttk.Button(out_frame, text="Bläddra…", command=self._browse_output).pack(
            side=tk.RIGHT, padx=(0, 4), pady=4
        )

        # ── Förlopp ───────────────────────────────────────────────────
        prog_frame = ttk.LabelFrame(self, text="Förlopp")
        prog_frame.pack(fill=tk.X, **pad)
        self._progress = ttk.Progressbar(prog_frame, orient=tk.HORIZONTAL, mode="determinate")
        self._progress.pack(fill=tk.X, padx=4, pady=(4, 2))
        self._progress_label = ttk.Label(prog_frame, text="")
        self._progress_label.pack(anchor=tk.W, padx=4)
        self._file_label = ttk.Label(prog_frame, text="", foreground="gray")
        self._file_label.pack(anchor=tk.W, padx=4, pady=(0, 4))

        # ── Logg ──────────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(self, text="Logg")
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)
        self._log = scrolledtext.ScrolledText(
            log_frame, height=8, state=tk.DISABLED, wrap=tk.WORD
        )
        self._log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # ── Knappar ───────────────────────────────────────────────────
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        self._cancel_btn = ttk.Button(
            btn_frame, text="Avbryt", command=self._cancel, state=tk.DISABLED
        )
        self._cancel_btn.pack(side=tk.RIGHT, padx=(4, 0))
        self._start_btn = ttk.Button(btn_frame, text="Starta", command=self._start)
        self._start_btn.pack(side=tk.RIGHT)

    # ─────────────────────────────────────────────────────────────────
    # File / folder management
    # ─────────────────────────────────────────────────────────────────

    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="Välj PDF-filer",
            filetypes=[("PDF-filer", "*.pdf"), ("Alla filer", "*.*")],
        )
        self._add_paths([Path(p) for p in paths])

    def _add_folder(self):
        folder = filedialog.askdirectory(title="Välj mapp med PDF-filer")
        if not folder:
            return
        pdfs = sorted(Path(folder).glob("*.pdf"))
        if not pdfs:
            messagebox.showinfo(
                "Inga PDF-filer", f"Mappen innehåller inga PDF-filer:\n{folder}"
            )
        else:
            self._add_paths(pdfs)

    def _add_paths(self, paths: list):
        existing = set(self._pdf_paths)
        for p in paths:
            if p not in existing:
                self._pdf_paths.append(p)
                self._listbox.insert(tk.END, str(p))
                existing.add(p)

    def _remove_selected(self):
        for idx in reversed(self._listbox.curselection()):
            self._listbox.delete(idx)
            del self._pdf_paths[idx]

    def _clear_list(self):
        self._listbox.delete(0, tk.END)
        self._pdf_paths.clear()

    def _browse_output(self):
        folder = filedialog.askdirectory(title="Välj utdatamapp")
        if folder:
            self._output_dir.set(folder)

    # ─────────────────────────────────────────────────────────────────
    # Start / cancel
    # ─────────────────────────────────────────────────────────────────

    def _start(self):
        if not self._pdf_paths:
            messagebox.showwarning("Inga filer", "Lägg till minst en PDF-fil.")
            return
        if not self._output_dir.get():
            messagebox.showwarning("Ingen utdatamapp", "Välj en utdatamapp.")
            return

        self._running = True
        self._cancel_event.clear()
        self._conflict_event.clear()
        self._start_btn.configure(state=tk.DISABLED)
        self._cancel_btn.configure(state=tk.NORMAL)
        self._progress.configure(maximum=len(self._pdf_paths), value=0)
        self._progress_label.configure(text=f"0 av {len(self._pdf_paths)} filer")
        self._file_label.configure(text="")
        self._log_clear()

        self._worker = WorkerThread(
            pdf_paths=list(self._pdf_paths),
            output_dir=Path(self._output_dir.get()),
            w2g=self._w2g,
            g2w=self._g2w,
            cancel_event=self._cancel_event,
            conflict_event=self._conflict_event,
        )
        self._worker.start()
        self.after(100, self._poll_queue)

    def _cancel(self):
        self._cancel_event.set()
        # Unblock worker if it is waiting on a conflict decision
        self._g2w.put(("decision", "cancel"))
        self._conflict_event.set()
        self._cancel_btn.configure(state=tk.DISABLED)
        self._log_append("Avbryter…")

    # ─────────────────────────────────────────────────────────────────
    # Queue polling
    # ─────────────────────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                msg = self._w2g.get_nowait()
                kind = msg[0]

                if kind == "progress":
                    _, idx, total, filename = msg
                    self._progress.configure(value=idx)
                    self._progress_label.configure(text=f"{idx} av {total} filer")
                    self._file_label.configure(text=f"Bearbetar: {filename}")

                elif kind == "conflict":
                    # Hand off to _handle_conflict; stop polling until resolved
                    self.after_idle(lambda p=msg[1]: self._handle_conflict(p))
                    return

                elif kind == "log":
                    self._log_append(msg[1])

                elif kind == "error":
                    self._log_append(f"✗ {msg[1]}: {msg[2]}")

                elif kind == "done":
                    self._on_done(msg[1], msg[2], msg[3])
                    return

        except queue.Empty:
            pass

        if self._running:
            self.after(100, self._poll_queue)

    def _handle_conflict(self, output_path_str: str):
        action = self._show_conflict_dialog(Path(output_path_str))
        self._g2w.put(("decision", action))
        if action == "cancel":
            self._cancel_event.set()
        self._conflict_event.set()
        if self._running and not self._cancel_event.is_set():
            self.after(100, self._poll_queue)
        elif self._running:
            # cancelled — keep polling to catch the "done" message
            self.after(100, self._poll_queue)

    # ─────────────────────────────────────────────────────────────────
    # Done
    # ─────────────────────────────────────────────────────────────────

    def _on_done(self, ok: int, skip: int, err: int):
        self._running = False
        self._start_btn.configure(state=tk.NORMAL)
        self._cancel_btn.configure(state=tk.DISABLED)
        self._file_label.configure(text="")

        if self._cancel_event.is_set():
            summary = f"Avbrutet — {ok} konverterade, {skip} hoppade över, {err} fel."
        else:
            self._progress.configure(value=self._progress["maximum"])
            summary = f"Klart! {ok} konverterade, {skip} hoppade över, {err} fel."

        self._progress_label.configure(text=summary)
        self._log_append(f"\n{summary}")

    # ─────────────────────────────────────────────────────────────────
    # Conflict dialog
    # ─────────────────────────────────────────────────────────────────

    def _show_conflict_dialog(self, output_path: Path) -> str:
        result = tk.StringVar(value="cancel")
        dlg = tk.Toplevel(self)
        dlg.title("Filen finns redan")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.focus_set()

        ttk.Label(
            dlg,
            text=f"Filen finns redan:\n{output_path.name}\n\nVad vill du göra?",
            justify=tk.LEFT,
            wraplength=320,
        ).pack(padx=20, pady=(16, 10))

        def choose(value):
            result.set(value)
            dlg.destroy()

        for text, value in [
            ("Hoppa över",      "skip"),
            ("Hoppa över alla", "skip_all"),
            ("Skriv över",      "overwrite"),
            ("Skriv över alla", "overwrite_all"),
            ("Avbryt allt",     "cancel"),
        ]:
            ttk.Button(dlg, text=text, width=18, command=lambda v=value: choose(v)).pack(
                pady=2
            )

        ttk.Frame(dlg).pack(pady=6)

        self.update_idletasks()
        w, h = 300, 280
        x = self.winfo_x() + (self.winfo_width() - w) // 2
        y = self.winfo_y() + (self.winfo_height() - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        self.wait_window(dlg)
        return result.get()

    # ─────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────

    def _log_append(self, text: str):
        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, text + "\n")
        self._log.see(tk.END)
        self._log.configure(state=tk.DISABLED)

    def _log_clear(self):
        self._log.configure(state=tk.NORMAL)
        self._log.delete("1.0", tk.END)
        self._log.configure(state=tk.DISABLED)

    def _check_tesseract(self):
        if not shutil.which("tesseract"):
            messagebox.showwarning(
                "Tesseract saknas",
                "Tesseract hittades inte i PATH.\n\n"
                "Installera med:\n"
                "  brew install tesseract tesseract-lang\n\n"
                "Appen kan inte utföra OCR utan Tesseract.",
            )


if __name__ == "__main__":
    app = App()
    app.mainloop()
