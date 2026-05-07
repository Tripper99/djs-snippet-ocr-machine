"""DJs Docling Snippet OCR Machine — clipboard image OCR tool."""

from __future__ import annotations

import hashlib
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

try:
    from PIL import Image, ImageGrab
except ImportError:
    Image = None  # type: ignore[assignment]
    ImageGrab = None  # type: ignore[assignment]

try:
    from ocrmac.ocrmac import OCR as MacOCR
except ImportError:
    MacOCR = None  # type: ignore[assignment]

APP_TITLE = "DJs Docling Snippet OCR Machine"
VERSION = "0.5.0"

LANGUAGES: dict[str, str] = {
    "Svenska": "sv-SE",
    "Engelska": "en-US",
    "Tyska": "de-DE",
    "Danska": "da-DK",
    "Norska": "no-NO",
    "Franska": "fr-FR",
}

SEPARATOR = "\n\n---\n\n"


# ─────────────────────────────────────────────────────────────────────────────
# Background threads
# ─────────────────────────────────────────────────────────────────────────────


class ClipboardMonitor(threading.Thread):
    """Polls the clipboard every 500 ms and sends new images to ocr_queue."""

    def __init__(self, ocr_queue: queue.Queue, stop_event: threading.Event) -> None:
        super().__init__(daemon=True)
        self._queue = ocr_queue
        self._stop = stop_event
        self._last_hash: str | None = None

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                img = ImageGrab.grabclipboard()
                if isinstance(img, Image.Image):
                    h = hashlib.md5(img.tobytes()).hexdigest()
                    if h != self._last_hash:
                        self._last_hash = h
                        self._queue.put(img.copy())
            except Exception:
                pass
            self._stop.wait(0.5)


class OcrWorker(threading.Thread):
    """Reads images from ocr_queue, runs OCR, posts results to result_queue."""

    def __init__(
        self,
        ocr_queue: queue.Queue,
        result_queue: queue.Queue,
        stop_event: threading.Event,
        lang_getter,
    ) -> None:
        super().__init__(daemon=True)
        self._ocr_q = ocr_queue
        self._result_q = result_queue
        self._stop = stop_event
        self._get_lang = lang_getter
        self._counter = 0

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                img = self._ocr_q.get(timeout=0.5)
            except queue.Empty:
                continue

            self._counter += 1
            snippet_id = self._counter
            lang = self._get_lang()
            timestamp = datetime.now().strftime("%H:%M:%S")

            self._result_q.put(("pending", snippet_id, timestamp))

            try:
                annotations = MacOCR(img, language_preference=[lang]).recognize()
                text = _annotations_to_text(annotations)
            except Exception as exc:
                text = f"(OCR-fel: {exc})"

            self._result_q.put(("result", snippet_id, text))


def _annotations_to_text(annotations: list) -> str:
    """Convert Vision OCR annotations to plain text with correct reading order.

    Vision returns individual word tokens for complex/degraded images. We must
    group tokens whose vertical centers are close together (same physical line),
    then sort each group left-to-right by x before joining.
    """
    if not annotations:
        return "(ingen text hittad)"

    # Extract (text, center_x, center_y, height) for each token.
    # bbox = [x, y, width, height]; (x,y) is bottom-left in Vision coords
    # where y=0 is the bottom of the image and y increases upward.
    items: list[tuple[str, float, float, float]] = []
    for ann in annotations:
        text, _conf, bbox = ann[0], ann[1], ann[2]
        bx, by, bw, bh = bbox[0], bbox[1], bbox[2], bbox[3]
        items.append((text, bx + bw / 2, by + bh / 2, bh))

    # Estimate typical line height from median token height.
    median_h = sorted(i[3] for i in items)[len(items) // 2]
    # Tokens whose vertical centers are within 60% of a line height apart
    # are considered to be on the same physical line.
    threshold = median_h * 0.6

    # Sort top-to-bottom (high cy = near top of image in Vision coords).
    items.sort(key=lambda t: -t[2])

    # Greedy line clustering: assign each token to the existing line whose
    # mean center_y is closest, or start a new line if none is close enough.
    lines: list[list[tuple[str, float, float, float]]] = []
    for item in items:
        best_idx: int | None = None
        best_dist = float("inf")
        for i, line in enumerate(lines):
            line_cy = sum(t[2] for t in line) / len(line)
            dist = abs(item[2] - line_cy)
            if dist <= threshold and dist < best_dist:
                best_dist = dist
                best_idx = i
        if best_idx is not None:
            lines[best_idx].append(item)
        else:
            lines.append([item])

    # Sort lines top-to-bottom, then tokens within each line left-to-right.
    lines.sort(key=lambda ln: -sum(t[2] for t in ln) / len(ln))
    result = []
    for line in lines:
        line.sort(key=lambda t: t[1])
        result.append(" ".join(t[0] for t in line))

    return "\n".join(result)


# ─────────────────────────────────────────────────────────────────────────────
# Snippet card widget
# ─────────────────────────────────────────────────────────────────────────────


class SnippetCard(ttk.Frame):
    """A collapsible card showing one OCR result."""

    def __init__(
        self,
        parent,
        snippet_id: int,
        timestamp: str,
        copy_callback,
    ) -> None:
        super().__init__(parent, relief="groove", borderwidth=1)
        self._snippet_id = snippet_id
        self._timestamp = timestamp
        self._copy_cb = copy_callback
        self._text = ""
        self._expanded = True
        self._selected = tk.BooleanVar(value=False)
        self._build()

    def _build(self) -> None:
        # ── Header row ────────────────────────────────────────────────
        header = ttk.Frame(self)
        header.pack(fill="x")

        ttk.Checkbutton(header, variable=self._selected).pack(side="left", padx=(4, 0))

        self._arrow = ttk.Label(header, text="▼", cursor="hand2", width=2)
        self._arrow.pack(side="left")
        self._arrow.bind("<Button-1>", self._toggle)

        self._title_lbl = ttk.Label(
            header,
            text=f"Avsnitt {self._snippet_id} — {self._timestamp}   ⏳",
            cursor="hand2",
            font=("Helvetica", 11, "bold"),
        )
        self._title_lbl.pack(side="left", fill="x", expand=True, padx=6, pady=4)
        self._title_lbl.bind("<Button-1>", self._toggle)

        self._copy_btn = ttk.Button(
            header, text="Kopiera", width=8, command=self._copy, state="disabled"
        )
        self._copy_btn.pack(side="right", padx=4, pady=3)

        # ── Body ──────────────────────────────────────────────────────
        self._body = ttk.Frame(self)
        self._body.pack(fill="x")

        self._text_widget = ScrolledText(
            self._body,
            wrap="word",
            height=3,
            state="disabled",
            font=("Helvetica", 11),
            relief="flat",
            borderwidth=0,
            background="#f8f8f8",
        )
        self._text_widget.pack(fill="x", padx=8, pady=(0, 6))

    def _toggle(self, _event=None) -> None:
        if self._expanded:
            self._body.pack_forget()
            self._arrow.configure(text="►")
        else:
            self._body.pack(fill="x")
            self._arrow.configure(text="▼")
        self._expanded = not self._expanded
        # Trigger the canvas to recalculate scroll region
        self.event_generate("<<Resized>>", propagate=True)

    def set_text(self, text: str) -> None:
        self._text = text
        self._text_widget.configure(state="normal")
        self._text_widget.delete("1.0", "end")
        self._text_widget.insert("1.0", text)
        self._text_widget.configure(state="disabled")
        # Fit height to content (2–12 lines)
        line_count = text.count("\n") + 1
        self._text_widget.configure(height=min(max(line_count, 2), 12))
        self._title_lbl.configure(
            text=f"Avsnitt {self._snippet_id} — {self._timestamp}"
        )
        self._copy_btn.configure(state="normal")

    def get_text(self) -> str:
        return self._text

    def is_selected(self) -> bool:
        return self._selected.get()

    def _copy(self) -> None:
        self._copy_cb(self._text)


# ─────────────────────────────────────────────────────────────────────────────
# Main application
# ─────────────────────────────────────────────────────────────────────────────


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE}  v{VERSION}")
        self.minsize(560, 560)
        self.resizable(True, True)

        self._ocr_queue: queue.Queue = queue.Queue()
        self._result_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._lang_var = tk.StringVar(value="Svenska")
        self._cards: list[SnippetCard] = []
        self._pending: dict[int, SnippetCard] = {}

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._check_deps)

    # ─────────────────────────────────────────────────────────────────
    # UI construction
    # ─────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Control bar ───────────────────────────────────────────────
        ctrl = ttk.Frame(self, padding=(8, 6))
        ctrl.pack(fill="x")

        self._status_lbl = ttk.Label(
            ctrl, text="● Övervakar urklipp...", foreground="#2a7a2a"
        )
        self._status_lbl.pack(side="left")

        ttk.Button(ctrl, text="Rensa alla", command=self._clear_all).pack(
            side="right"
        )

        lang_row = ttk.Frame(ctrl)
        lang_row.pack(side="right", padx=(0, 10))
        ttk.Label(lang_row, text="Språk:").pack(side="left")
        ttk.Combobox(
            lang_row,
            textvariable=self._lang_var,
            values=list(LANGUAGES.keys()),
            width=10,
            state="readonly",
        ).pack(side="left", padx=(4, 0))

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        ttk.Label(self, text="Textavsnitt:", padding=(8, 4)).pack(anchor="w")

        # ── Scrollable card area ───────────────────────────────────────
        scroll_container = ttk.Frame(self)
        scroll_container.pack(fill="both", expand=True, padx=6, pady=(0, 4))

        self._canvas = tk.Canvas(scroll_container, highlightthickness=0)
        vscroll = ttk.Scrollbar(
            scroll_container, orient="vertical", command=self._canvas.yview
        )
        self._canvas.configure(yscrollcommand=vscroll.set)

        vscroll.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._cards_frame = ttk.Frame(self._canvas)
        self._cards_frame.columnconfigure(0, weight=1)
        self._window_id = self._canvas.create_window(
            (0, 0), window=self._cards_frame, anchor="nw"
        )

        self._cards_frame.bind("<Configure>", self._on_frame_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)
        self._cards_frame.bind("<<Resized>>", self._on_frame_configure)

        # ── Bottom buttons ─────────────────────────────────────────────
        ttk.Separator(self, orient="horizontal").pack(fill="x")
        btn_row = ttk.Frame(self, padding=(8, 6))
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Kopiera alla", command=self._copy_all).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(btn_row, text="Kopiera markerade", command=self._copy_selected).pack(
            side="left"
        )

    # ─────────────────────────────────────────────────────────────────
    # Canvas / scroll helpers
    # ─────────────────────────────────────────────────────────────────

    def _on_frame_configure(self, _event=None) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self._canvas.itemconfig(self._window_id, width=event.width)

    def _on_mousewheel(self, event) -> None:
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ─────────────────────────────────────────────────────────────────
    # Thread management
    # ─────────────────────────────────────────────────────────────────

    def _start_threads(self) -> None:
        ClipboardMonitor(self._ocr_queue, self._stop_event).start()
        OcrWorker(
            self._ocr_queue,
            self._result_queue,
            self._stop_event,
            self._get_lang_code,
        ).start()
        self._poll_results()

    def _get_lang_code(self) -> str:
        return LANGUAGES.get(self._lang_var.get(), "sv-SE")

    # ─────────────────────────────────────────────────────────────────
    # Result polling
    # ─────────────────────────────────────────────────────────────────

    def _poll_results(self) -> None:
        try:
            while True:
                msg = self._result_queue.get_nowait()
                kind = msg[0]
                if kind == "pending":
                    _, snippet_id, timestamp = msg
                    card = SnippetCard(
                        self._cards_frame, snippet_id, timestamp, self._copy_to_clipboard
                    )
                    row = len(self._cards)
                    card.grid(row=row, column=0, sticky="ew", padx=2, pady=2)
                    self._cards.append(card)
                    self._pending[snippet_id] = card
                    # Scroll to new card
                    self.after(80, lambda: self._canvas.yview_moveto(1.0))
                elif kind == "result":
                    _, snippet_id, text = msg
                    if snippet_id in self._pending:
                        self._pending.pop(snippet_id).set_text(text)
        except queue.Empty:
            pass
        self.after(100, self._poll_results)

    # ─────────────────────────────────────────────────────────────────
    # Actions
    # ─────────────────────────────────────────────────────────────────

    def _copy_to_clipboard(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)

    def _copy_all(self) -> None:
        texts = [c.get_text() for c in self._cards if c.get_text()]
        if not texts:
            messagebox.showinfo("Info", "Inga textavsnitt att kopiera.", parent=self)
            return
        self._copy_to_clipboard(SEPARATOR.join(texts))

    def _copy_selected(self) -> None:
        texts = [c.get_text() for c in self._cards if c.is_selected() and c.get_text()]
        if not texts:
            messagebox.showinfo("Info", "Inga avsnitt markerade.", parent=self)
            return
        self._copy_to_clipboard(SEPARATOR.join(texts))

    def _clear_all(self) -> None:
        for card in self._cards:
            card.destroy()
        self._cards.clear()
        self._pending.clear()

    # ─────────────────────────────────────────────────────────────────
    # Startup checks
    # ─────────────────────────────────────────────────────────────────

    def _check_deps(self) -> None:
        if ImageGrab is None or Image is None:
            messagebox.showerror(
                "Pillow saknas",
                "Pillow är inte installerat.\n\nKör: pip install pillow",
                parent=self,
            )
            sys.exit(1)
        if MacOCR is None:
            messagebox.showerror(
                "ocrmac saknas",
                "ocrmac är inte installerat.\n\nKör: pip install ocrmac",
                parent=self,
            )
            sys.exit(1)
        self._start_threads()

    def _on_close(self) -> None:
        self._stop_event.set()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
