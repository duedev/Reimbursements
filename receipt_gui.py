#!/usr/bin/env python3
"""
receipt_gui.py
Modern GUI for the Receipt Processor, built with customtkinter.
Run:  python receipt_gui.py
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
from PIL import Image, ImageTk

from process_receipts import process_receipts_batch

# ── Default appearance ─────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

ACCENT   = "#3B82F6"   # steel blue (matches spreadsheet header color)
SECTION_FG = "#94A3B8" # muted label color


class ReceiptProcessorApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Receipt Processor")
        self.minsize(960, 700)
        self.resizable(True, True)

        # Internal state
        self._processing   = False
        self._output_file: Path | None = None
        self._log_queue: queue.Queue = queue.Queue()
        self._thumbnail_img = None  # keep ref to prevent GC

        self._build_ui()
        self._poll_log_queue()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)
        self.grid_rowconfigure(3, weight=1)

        # ── Top bar ──────────────────────────────────────────────────────────
        top = ctk.CTkFrame(self, corner_radius=0, height=56)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            top, text="Receipt Processor",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, padx=20, pady=12, sticky="w")

        self.theme_switch = ctk.CTkSwitch(
            top, text="Light Mode", command=self._toggle_theme,
            font=ctk.CTkFont(size=13),
        )
        self.theme_switch.grid(row=0, column=2, padx=20, pady=12, sticky="e")

        # ── Content row ──────────────────────────────────────────────────────
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=0, pady=0)
        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, weight=0)

        self._build_left_panel(content)
        self._build_right_panel(content)

        # ── Progress bar ──────────────────────────────────────────────────────
        prog_frame = ctk.CTkFrame(self, corner_radius=0, height=44)
        prog_frame.grid(row=2, column=0, columnspan=2, sticky="ew")
        prog_frame.grid_columnconfigure(0, weight=1)

        self.progress_bar = ctk.CTkProgressBar(prog_frame, height=14, corner_radius=6)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=0, column=0, padx=16, pady=15, sticky="ew")

        self.progress_label = ctk.CTkLabel(
            prog_frame, text="0 / 0", font=ctk.CTkFont(size=12), width=60,
        )
        self.progress_label.grid(row=0, column=1, padx=(0, 16), pady=15)

        # ── Log area ──────────────────────────────────────────────────────────
        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=3, column=0, columnspan=2, sticky="nsew", padx=12, pady=(0, 8))
        log_frame.grid_rowconfigure(1, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_frame, text="Processing Log",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=SECTION_FG,
        ).grid(row=0, column=0, padx=12, pady=(10, 4), sticky="w")

        self.log_textbox = ctk.CTkTextbox(
            log_frame, height=160, state="disabled",
            wrap="word", font=ctk.CTkFont(family="Courier", size=12),
        )
        self.log_textbox.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")

        # ── Bottom bar ────────────────────────────────────────────────────────
        bottom = ctk.CTkFrame(self, corner_radius=0, height=50)
        bottom.grid(row=4, column=0, columnspan=2, sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)

        self.open_btn = ctk.CTkButton(
            bottom, text="Open Output File", state="disabled",
            command=self._open_output, width=200, height=36,
            font=ctk.CTkFont(size=13),
        )
        self.open_btn.grid(row=0, column=1, padx=16, pady=7)

    def _build_left_panel(self, parent):
        left = ctk.CTkFrame(parent)
        left.grid(row=0, column=0, padx=(12, 6), pady=12, sticky="nsew")
        left.grid_columnconfigure(1, weight=1)

        row = 0
        ctk.CTkLabel(
            left, text="Configuration",
            font=ctk.CTkFont(size=14, weight="bold"), text_color=SECTION_FG,
        ).grid(row=row, column=0, columnspan=3, padx=14, pady=(14, 8), sticky="w")
        row += 1

        # File / folder pickers
        self.template_var = ctk.StringVar(value="Reimbursement_sheet_1.xlsx")
        row = self._file_row(left, row, "Template:", self.template_var, is_dir=False)

        self.receipts_var = ctk.StringVar(value="receipts")
        row = self._file_row(left, row, "Receipts Folder:", self.receipts_var, is_dir=True)

        self.output_var = ctk.StringVar(value="")
        row = self._file_row(left, row, "Output Folder:", self.output_var, is_dir=True,
                             placeholder="(same as template)")

        # Separator
        ctk.CTkFrame(left, height=1, fg_color="#4B5563").grid(
            row=row, column=0, columnspan=3, padx=14, pady=8, sticky="ew"
        )
        row += 1

        # Text fields
        self.employee_var    = ctk.StringVar(value="Duane Hamilton")
        self.job_name_var    = ctk.StringVar(value="")
        self.job_number_var  = ctk.StringVar(value="")

        row = self._text_row(left, row, "Employee:", self.employee_var, "Employee Name")
        row = self._text_row(left, row, "Job Name:", self.job_name_var, "Job Name (default if blank on receipt)")
        row = self._text_row(left, row, "Job No.:", self.job_number_var, "Job Number (default if blank on receipt)")

        # Separator
        ctk.CTkFrame(left, height=1, fg_color="#4B5563").grid(
            row=row, column=0, columnspan=3, padx=14, pady=8, sticky="ew"
        )
        row += 1

        # Process button
        self.process_btn = ctk.CTkButton(
            left, text="Process Receipts",
            height=52, font=ctk.CTkFont(size=16, weight="bold"),
            command=self._start_processing,
        )
        self.process_btn.grid(
            row=row, column=0, columnspan=3, padx=14, pady=(4, 16), sticky="ew"
        )

    def _build_right_panel(self, parent):
        right = ctk.CTkFrame(parent, width=320)
        right.grid(row=0, column=1, padx=(6, 12), pady=12, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_propagate(False)

        ctk.CTkLabel(
            right, text="Receipt Preview",
            font=ctk.CTkFont(size=14, weight="bold"), text_color=SECTION_FG,
        ).grid(row=0, column=0, padx=14, pady=(14, 8), sticky="w")

        # Thumbnail container
        self.thumb_frame = ctk.CTkFrame(right, width=290, height=230, corner_radius=8)
        self.thumb_frame.grid(row=1, column=0, padx=14, pady=(0, 8), sticky="n")
        self.thumb_frame.grid_propagate(False)

        self.thumbnail_label = ctk.CTkLabel(
            self.thumb_frame, text="No image", width=290, height=230,
            font=ctk.CTkFont(size=12), text_color=SECTION_FG,
        )
        self.thumbnail_label.place(x=0, y=0, relwidth=1, relheight=1)

        self.current_file_label = ctk.CTkLabel(
            right, text="", font=ctk.CTkFont(size=11),
            wraplength=290, justify="center",
        )
        self.current_file_label.grid(row=2, column=0, padx=14, pady=(0, 8), sticky="ew")

        # Extracted data preview
        ctk.CTkLabel(
            right, text="Last Extracted",
            font=ctk.CTkFont(size=13, weight="bold"), text_color=SECTION_FG,
        ).grid(row=3, column=0, padx=14, pady=(6, 4), sticky="w")

        self.extracted_box = ctk.CTkTextbox(
            right, height=160, state="disabled",
            wrap="word", font=ctk.CTkFont(family="Courier", size=11),
        )
        self.extracted_box.grid(row=4, column=0, padx=12, pady=(0, 12), sticky="nsew")
        right.grid_rowconfigure(4, weight=1)

    # ── Row helpers ────────────────────────────────────────────────────────────

    def _file_row(self, parent, row: int, label: str, var: ctk.StringVar,
                  is_dir: bool, placeholder: str = "") -> int:
        ctk.CTkLabel(parent, text=label, anchor="w",
                     font=ctk.CTkFont(size=12)).grid(
            row=row, column=0, padx=(14, 6), pady=5, sticky="w"
        )
        entry = ctk.CTkEntry(parent, textvariable=var, width=260,
                             placeholder_text=placeholder,
                             font=ctk.CTkFont(size=12))
        entry.grid(row=row, column=1, padx=4, pady=5, sticky="ew")
        btn = ctk.CTkButton(
            parent, text="📂", width=36, height=28,
            command=lambda v=var, d=is_dir: self._browse(v, d),
            font=ctk.CTkFont(size=14),
        )
        btn.grid(row=row, column=2, padx=(4, 14), pady=5)
        return row + 1

    def _text_row(self, parent, row: int, label: str, var: ctk.StringVar,
                  placeholder: str = "") -> int:
        ctk.CTkLabel(parent, text=label, anchor="w",
                     font=ctk.CTkFont(size=12)).grid(
            row=row, column=0, padx=(14, 6), pady=5, sticky="w"
        )
        entry = ctk.CTkEntry(parent, textvariable=var, width=260,
                             placeholder_text=placeholder,
                             font=ctk.CTkFont(size=12))
        entry.grid(row=row, column=1, columnspan=2, padx=(4, 14), pady=5, sticky="ew")
        return row + 1

    # ── Browse dialogs ─────────────────────────────────────────────────────────

    def _browse(self, var: ctk.StringVar, is_dir: bool):
        if is_dir:
            path = filedialog.askdirectory(title="Select Folder")
        else:
            path = filedialog.askopenfilename(
                title="Select Spreadsheet",
                filetypes=[("Excel files", "*.xlsx *.xls"), ("All files", "*.*")],
            )
        if path:
            var.set(path)

    # ── Theme toggle ───────────────────────────────────────────────────────────

    def _toggle_theme(self):
        current = ctk.get_appearance_mode()
        new_mode = "Light" if current.lower() == "dark" else "Dark"
        ctk.set_appearance_mode(new_mode)
        self.theme_switch.configure(text=f"{'Dark' if new_mode == 'Light' else 'Light'} Mode")

    # ── Processing ─────────────────────────────────────────────────────────────

    def _start_processing(self):
        if self._processing:
            return
        self._processing = True
        self.process_btn.configure(state="disabled", text="Processing…")
        self.open_btn.configure(state="disabled")
        self._output_file = None
        self.progress_bar.set(0)
        self.progress_label.configure(text="0 / 0")
        self._clear_log()
        self._update_extracted("")

        thread = threading.Thread(target=self._run_processing, daemon=True)
        thread.start()

    def _run_processing(self):
        """Runs in background thread. Uses queue for all UI communication."""
        template_str = self.template_var.get().strip()
        receipts_str = self.receipts_var.get().strip()
        output_str   = self.output_var.get().strip()

        template  = Path(template_str) if template_str else Path("Reimbursement_sheet_1.xlsx")
        receipts  = Path(receipts_str) if receipts_str else Path("receipts")
        out_dir   = Path(output_str) if output_str else template.parent

        def progress_cb(cur, tot, fname):
            self._log_queue.put(("progress", cur, tot, fname))

        def log_cb(msg):
            self._log_queue.put(("log", msg))

        try:
            result = process_receipts_batch(
                template_path=template,
                receipts_folder=receipts,
                output_dir=out_dir,
                employee_name=self.employee_var.get().strip() or "Employee",
                job_name_default=self.job_name_var.get().strip(),
                job_number_default=self.job_number_var.get().strip(),
                progress_callback=progress_cb,
                log_callback=log_cb,
            )
            self._log_queue.put(("done", result))
        except Exception as exc:
            self._log_queue.put(("error", str(exc)))

    # ── Queue polling ──────────────────────────────────────────────────────────

    def _poll_log_queue(self):
        try:
            while True:
                msg = self._log_queue.get_nowait()
                kind = msg[0]

                if kind == "progress":
                    _, cur, tot, fname = msg
                    self.progress_bar.set(cur / tot if tot else 0)
                    self.progress_label.configure(text=f"{cur} / {tot}")
                    self.current_file_label.configure(text=fname)
                    self._update_thumbnail(fname)

                elif kind == "log":
                    text = msg[1]
                    self._append_log(text)
                    # If it looks like an extraction result line, show in preview
                    if text.strip().startswith("[") and "→" in text:
                        self._update_extracted(text.strip())

                elif kind == "done":
                    result = msg[1]
                    self._output_file = result.get("output_path")
                    self._processing  = False
                    self.process_btn.configure(state="normal", text="Process Receipts")
                    if self._output_file and self._output_file.exists():
                        self.open_btn.configure(state="normal")
                    self.progress_bar.set(1)

                elif kind == "error":
                    self._append_log(f"\nERROR: {msg[1]}\n")
                    self._processing = False
                    self.process_btn.configure(state="normal", text="Process Receipts")

        except queue.Empty:
            pass

        self.after(50, self._poll_log_queue)

    # ── Log helpers ────────────────────────────────────────────────────────────

    def _append_log(self, text: str):
        self.log_textbox.configure(state="normal")
        self.log_textbox.insert("end", text + "\n")
        self.log_textbox.see("end")
        self.log_textbox.configure(state="disabled")

    def _clear_log(self):
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")

    def _update_extracted(self, text: str):
        self.extracted_box.configure(state="normal")
        self.extracted_box.delete("1.0", "end")
        if text:
            self.extracted_box.insert("1.0", text)
        self.extracted_box.configure(state="disabled")

    # ── Thumbnail ──────────────────────────────────────────────────────────────

    def _update_thumbnail(self, filename: str):
        receipts_path = Path(self.receipts_var.get().strip() or "receipts")
        img_path = receipts_path / filename
        if not img_path.exists():
            return
        try:
            img = Image.open(img_path)
            img.thumbnail((286, 226), Image.LANCZOS)
            ctk_img = ctk.CTkImage(light_image=img, dark_image=img,
                                   size=(img.width, img.height))
            self._thumbnail_img = ctk_img  # prevent GC
            self.thumbnail_label.configure(image=ctk_img, text="")
        except Exception:
            pass

    # ── Open output ────────────────────────────────────────────────────────────

    def _open_output(self):
        if not (self._output_file and self._output_file.exists()):
            return
        path = str(self._output_file)
        if sys.platform == "darwin":
            subprocess.run(["open", path])
        elif sys.platform == "win32":
            subprocess.run(["start", "", path], shell=True)
        else:
            subprocess.run(["xdg-open", path])


def main():
    app = ReceiptProcessorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
