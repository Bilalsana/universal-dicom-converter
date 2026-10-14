import sys
import subprocess
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_VERSION = "2.0"
OUTPUT_DIR_NAME = "All_Converted_Videos"
LOG_NAME = "conversion_log.txt"

# Files that are never DICOM; skipping them avoids slow force-reads of big videos/images.
NON_DICOM_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".pdf", ".txt", ".csv", ".xml", ".html", ".htm", ".json", ".zip", ".exe", ".dll", ".inf",
    ".ini", ".log", ".py", ".app", ".dmg",
}

# --- 1. AUTO-INSTALLER & IMPORTS ---
def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])

required_packages = [
    "pydicom",
    "numpy",
    "opencv-python",
    "pylibjpeg",
    "pylibjpeg-libjpeg",
    "pylibjpeg-openjpeg"
]

# Try to import. If fail, install and retry.
# Never pip-install from the bundled .app: sys.executable is the app itself there.
try:
    import pydicom
    import numpy as np
    import cv2
except ImportError:
    if getattr(sys, "frozen", False):
        raise
    print("Dependencies missing. Installing now...")
    for package in required_packages:
        try:
            install(package)
        except Exception as e:
            print(f"Failed to install {package}: {e}")

    import pydicom
    import numpy as np
    import cv2

# --- 2. CONVERSION LOGIC ---
def _first_number(value):
    """DICOM numeric tags can be multi-valued (e.g. several windows); use the first."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (list, tuple, pydicom.multival.MultiValue)):
            value = value[0]
        return float(value)
    except (TypeError, ValueError, IndexError):
        return None

def get_best_fps(ds, default_fps=25):
    frame_time = _first_number(ds.get("FrameTime"))
    if frame_time and frame_time > 0:
        fps = 1000.0 / frame_time
    else:
        fps = _first_number(ds.get("CineRate")) or _first_number(ds.get("RecommendedDisplayFrameRate"))
        if not fps and ds.get("FrameTimeVector"):
            # First entry is the offset of frame 1 (usually 0), the rest are frame intervals.
            intervals = [float(t) for t in list(ds.FrameTimeVector)[1:] if float(t) > 0]
            if intervals:
                fps = 1000.0 / (sum(intervals) / len(intervals))
    if not fps or fps <= 0:
        return default_fps
    return min(max(fps, 1.0), 240.0)

def is_probably_dicom(path):
    """Cheap filter by name; anything left gets a forced read, since old exports often have no DICM preamble."""
    if os.path.splitext(path)[1].lower() in NON_DICOM_EXTENSIONS:
        return False
    if os.path.basename(path).upper() == "DICOMDIR":
        return False
    return True

def output_name_for(input_root, file_path):
    """Build a stable, unique name from the path, e.g. S0000001/SE000018/O0000001 -> S0000001_SE000018_O0000001.mp4.
    Using the whole relative path avoids collisions between studies that reuse series folder names."""
    rel = os.path.relpath(file_path, input_root)
    rel = os.path.splitext(rel)[0]
    parts = [p for p in rel.split(os.sep) if p and p != "."]
    return "_".join(parts) + ".mp4"

def make_gray_mapper(ds, pixel_data):
    """Return a function that maps one raw grayscale frame to uint8, using the DICOM display window when present."""
    slope = _first_number(ds.get("RescaleSlope")) or 1.0
    intercept = _first_number(ds.get("RescaleIntercept")) or 0.0
    center = _first_number(ds.get("WindowCenter"))
    width = _first_number(ds.get("WindowWidth"))
    invert = str(ds.get("PhotometricInterpretation", "")).strip().upper() == "MONOCHROME1"

    if center is not None and width is not None and width >= 1:
        # DICOM linear windowing (PS3.3 C.11.2.1.2), in rescaled units.
        low = center - 0.5 - (width - 1) / 2.0
        scale = 255.0 / max(width - 1, 1)

        def mapper(frame):
            values = frame.astype(np.float32) * slope + intercept
            out = np.clip((values - low) * scale, 0, 255).astype(np.uint8)
            return 255 - out if invert else out
        return mapper

    g_min = float(pixel_data.min())
    g_max = float(pixel_data.max())
    if pixel_data.dtype == np.uint8:
        def mapper(frame):
            return 255 - frame if invert else frame
        return mapper

    # No window: stretch the whole clip's range (same for every frame, so no flicker).
    span = g_max - g_min
    factor = 255.0 / span if span > 0 else 0.0

    def mapper(frame):
        out = np.clip((frame.astype(np.float32) - g_min) * factor, 0, 255).astype(np.uint8)
        return 255 - out if invert else out
    return mapper

def color_to_uint8(frame):
    if frame.dtype == np.uint8:
        return frame
    top = float(frame.max()) or 1.0
    return np.clip(frame.astype(np.float32) * (255.0 / top), 0, 255).astype(np.uint8)

def open_writer(path, fps, width, height):
    """Prefer H.264 (avc1) for compatibility; fall back to MPEG-4 if the encoder is unavailable."""
    for codec in ("avc1", "mp4v"):
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*codec), fps, (width, height), isColor=True)
        if writer.isOpened():
            return writer
        writer.release()
    raise RuntimeError("could not open a video encoder")

def convert_file(ds, output_path):
    """Write one multi-frame DICOM to MP4. Returns the number of frames written."""
    pixel_data = ds.pixel_array  # decoded once; YBR is converted to RGB by pydicom
    photometric = str(ds.get("PhotometricInterpretation", "")).strip().upper()
    samples = int(ds.get("SamplesPerPixel", 1) or 1)

    if photometric == "PALETTE COLOR":
        from pydicom.pixels import apply_color_lut
        pixel_data = apply_color_lut(pixel_data, ds)
        samples = 3

    is_color = samples > 1
    expected_ndim = 4 if is_color else 3
    if pixel_data.ndim != expected_ndim:
        raise ValueError(f"unexpected pixel array shape {pixel_data.shape}")

    frames, height, width = pixel_data.shape[:3]
    fps = get_best_fps(ds)
    gray_mapper = None if is_color else make_gray_mapper(ds, pixel_data)

    # H.264 needs even dimensions; pad by one pixel on the right/bottom when needed.
    pad_bottom, pad_right = height % 2, width % 2
    out_w, out_h = width + pad_right, height + pad_bottom

    temp_path = output_path[:-4] + ".partial.mp4"
    video = open_writer(temp_path, fps, out_w, out_h)
    try:
        for i in range(frames):
            frame = pixel_data[i]
            if is_color:
                frame_out = cv2.cvtColor(color_to_uint8(frame), cv2.COLOR_RGB2BGR)
            else:
                frame_out = cv2.cvtColor(gray_mapper(frame), cv2.COLOR_GRAY2BGR)
            if pad_bottom or pad_right:
                frame_out = cv2.copyMakeBorder(frame_out, 0, pad_bottom, 0, pad_right, cv2.BORDER_CONSTANT, value=0)
            video.write(frame_out)
    except Exception:
        video.release()
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
    video.release()

    if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
        raise RuntimeError("encoder produced an empty file")
    os.replace(temp_path, output_path)
    return frames

def convert_dicom_thread(input_root, update_status_callback, update_progress_callback, finish_callback,
                         output_folder=None, event_callback=None, cancel_event=None):
    """Runs in a background thread. Always calls finish_callback, even on unexpected errors.
    event_callback(kind, text) reports each file: kind is 'found', 'converted', 'skipped' or 'failed'."""
    summary = {"converted": 0, "skipped_existing": 0, "not_video": 0, "failed": [], "output_folder": output_folder,
               "error": None, "cancelled": False, "total": 0}
    event = event_callback or (lambda kind, text: None)
    try:
        output_folder = output_folder or os.path.join(input_root, OUTPUT_DIR_NAME)
        summary["output_folder"] = output_folder
        os.makedirs(output_folder, exist_ok=True)

        update_status_callback("Scanning subfolders...")
        file_list = []
        for root, dirs, files in os.walk(input_root):
            # Never descend into our own output folder (or hidden folders).
            dirs[:] = sorted(d for d in dirs if d != OUTPUT_DIR_NAME and not d.startswith("."))
            for file in sorted(files):
                if file.startswith("."):
                    continue
                path = os.path.join(root, file)
                if is_probably_dicom(path):
                    file_list.append(path)

        total_files = len(file_list) or 1
        summary["total"] = len(file_list)
        event("found", str(len(file_list)))

        for index, file_path in enumerate(file_list):
            if cancel_event is not None and cancel_event.is_set():
                summary["cancelled"] = True
                break
            rel = os.path.relpath(file_path, input_root)
            ds = None
            try:
                ds = pydicom.dcmread(file_path, force=True)
                frames = int(_first_number(ds.get("NumberOfFrames")) or 1)
                if "PixelData" not in ds or frames < 2:
                    summary["not_video"] += 1  # still images, reports, presentation states...
                else:
                    new_name = output_name_for(input_root, file_path)
                    output_path = os.path.join(output_folder, new_name)
                    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                        summary["skipped_existing"] += 1
                        update_status_callback(f"Already converted: {new_name}")
                        event("skipped", new_name)
                    else:
                        update_status_callback(f"Converting {index + 1}/{len(file_list)}: {rel}")
                        frames = convert_file(ds, output_path)
                        summary["converted"] += 1
                        update_status_callback(f"Converted: {new_name}")
                        event("converted", f"{new_name}  ·  {frames} frames @ {round(get_best_fps(ds), 1):g} fps")
            except Exception as e:
                # Files that aren't DICOM at all (no tags read) are just ignored.
                if ds is None or len(ds) == 0:
                    summary["not_video"] += 1
                else:
                    reason = f"{type(e).__name__}: {e}"
                    summary["failed"].append((rel, reason))
                    event("failed", f"{rel}  ·  {reason}")

            update_progress_callback(((index + 1) / total_files) * 100)

        if summary["failed"]:
            with open(os.path.join(output_folder, LOG_NAME), "w") as log:
                log.write(f"Universal DICOM Converter {APP_VERSION} - files that could not be converted\n\n")
                for rel, reason in summary["failed"]:
                    log.write(f"{rel}\n    {reason}\n")
    except Exception as e:
        summary["error"] = f"{type(e).__name__}: {e}"
    finally:
        finish_callback(summary)

# --- 3. GUI APPLICATION ---
IS_MAC = sys.platform == "darwin"

# macOS semantic colors follow light/dark mode automatically; plain hex is the fallback elsewhere.
COLORS = {
    "text": "systemTextColor" if IS_MAC else "#1f2328",
    "secondary": "systemSecondaryLabelColor" if IS_MAC else "#656d76",
    "window": "systemWindowBackgroundColor" if IS_MAC else "#f6f8fa",
    "field": "systemTextBackgroundColor" if IS_MAC else "#ffffff",
    "separator": "systemSeparatorColor" if IS_MAC else "#d0d7de",
    "accent": "systemControlAccentColor" if IS_MAC else "#0969da",
    "green": "#2ea043",
    "orange": "#d29922",
    "red": "#e5534b",
}
FONT = "SF Pro Text" if IS_MAC else "Helvetica"
MONO = "Menlo" if IS_MAC else "Courier"

def shorten_path(path, limit=56):
    """Middle-truncate long paths so the start and the folder name both stay visible."""
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    if len(path) <= limit:
        return path
    keep = (limit - 1) // 2
    return path[:keep] + "…" + path[-keep:]

class DicomApp:
    def __init__(self, root):
        self.root = root
        self.input_folder = None
        self.output_folder = None  # None = default subfolder inside the input folder
        self.last_output = None
        self.cancel_event = None
        self.total = 0
        self.counts = {"converted": 0, "skipped": 0, "failed": 0}

        root.title("Universal DICOM Converter")
        root.geometry("680x640")
        root.minsize(600, 560)
        root.configure(bg=COLORS["window"])

        style = ttk.Style()
        style.configure("Card.TLabelframe", padding=(14, 8, 14, 12))
        style.configure("Card.TLabelframe.Label", font=(FONT, 12, "bold"))

        body = tk.Frame(root, bg=COLORS["window"])
        body.pack(fill="both", expand=True, padx=24, pady=(18, 14))

        # Header
        tk.Label(body, text="Universal DICOM Converter", font=(FONT, 22, "bold"),
                 fg=COLORS["text"], bg=COLORS["window"]).pack(anchor="w")
        tk.Label(body, text="Turns cath (angio) and echo cine loops into MP4 videos",
                 font=(FONT, 13), fg=COLORS["secondary"], bg=COLORS["window"]).pack(anchor="w", pady=(2, 14))

        # Folders card
        folders = ttk.LabelFrame(body, text="Folders", style="Card.TLabelframe")
        folders.pack(fill="x")
        folders.columnconfigure(1, weight=1)

        ttk.Label(folders, text="Source", foreground=COLORS["secondary"]).grid(row=0, column=0, sticky="w", padx=(0, 12), pady=4)
        self.input_label = ttk.Label(folders, text="No folder selected", foreground=COLORS["secondary"])
        self.input_label.grid(row=0, column=1, sticky="w")
        self.btn_select = ttk.Button(folders, text="Choose…", command=self.select_folder, width=10)
        self.btn_select.grid(row=0, column=2, sticky="e", padx=(8, 0))

        ttk.Label(folders, text="Save to", foreground=COLORS["secondary"]).grid(row=1, column=0, sticky="w", padx=(0, 12), pady=4)
        self.output_label = ttk.Label(folders, text=f"{OUTPUT_DIR_NAME} inside the source folder", foreground=COLORS["secondary"])
        self.output_label.grid(row=1, column=1, sticky="w")
        self.btn_output = ttk.Button(folders, text="Change…", command=self.select_output, width=10)
        self.btn_output.grid(row=1, column=2, sticky="e", padx=(8, 0))

        # Actions
        actions = tk.Frame(body, bg=COLORS["window"])
        actions.pack(fill="x", pady=(14, 8))
        self.btn_convert = ttk.Button(actions, text="Convert", command=self.start_conversion, default="active", state="disabled", width=12)
        self.btn_convert.pack(side="left")
        self.btn_cancel = ttk.Button(actions, text="Cancel", command=self.cancel_conversion, state="disabled", width=10)
        self.btn_cancel.pack(side="left", padx=(8, 0))
        self.btn_open = ttk.Button(actions, text="Open Output Folder", command=self.open_output, state="disabled")
        self.btn_open.pack(side="right")

        # Progress
        self.progress = ttk.Progressbar(body, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x", pady=(6, 4))
        status_row = tk.Frame(body, bg=COLORS["window"])
        status_row.pack(fill="x")
        self.status_label = tk.Label(status_row, text="Choose a folder with DICOM files to begin.", anchor="w",
                                     font=(FONT, 12), fg=COLORS["secondary"], bg=COLORS["window"])
        self.status_label.pack(side="left", fill="x", expand=True)
        self.count_label = tk.Label(status_row, text="", font=(FONT, 12), fg=COLORS["secondary"], bg=COLORS["window"])
        self.count_label.pack(side="right")

        # Stats
        stats = tk.Frame(body, bg=COLORS["window"])
        stats.pack(fill="x", pady=(14, 10))
        self.stat_values = {}
        for column, (key, caption, color) in enumerate((
            ("converted", "Converted", COLORS["green"]),
            ("skipped", "Already done", COLORS["secondary"]),
            ("failed", "Failed", COLORS["red"]),
        )):
            stats.columnconfigure(column, weight=1, uniform="stat")
            tile = tk.Frame(stats, bg=COLORS["field"], highlightthickness=1, highlightbackground=COLORS["separator"])
            tile.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 6, 0 if column == 2 else 6))
            value = tk.Label(tile, text="0", font=(FONT, 24, "bold"), fg=color, bg=COLORS["field"])
            value.pack(pady=(8, 0))
            tk.Label(tile, text=caption, font=(FONT, 11), fg=COLORS["secondary"], bg=COLORS["field"]).pack(pady=(0, 8))
            self.stat_values[key] = value

        # Activity log
        tk.Label(body, text="Activity", font=(FONT, 12, "bold"), fg=COLORS["text"], bg=COLORS["window"]).pack(anchor="w", pady=(4, 4))
        log_frame = tk.Frame(body, bg=COLORS["field"], highlightthickness=1, highlightbackground=COLORS["separator"])
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=8, wrap="none", font=(MONO, 11), relief="flat", borderwidth=0,
                           bg=COLORS["field"], fg=COLORS["text"], highlightthickness=0, padx=10, pady=8, state="disabled")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for tag, color in (("converted", COLORS["green"]), ("failed", COLORS["red"]),
                           ("skipped", COLORS["secondary"]), ("info", COLORS["secondary"])):
            self.log.tag_configure(tag, foreground=color)
        self.append_log("Converted files and any problems will be listed here.")

        # Footer
        tk.Label(body, text=f"Version {APP_VERSION}", font=(FONT, 10), fg=COLORS["secondary"],
                 bg=COLORS["window"]).pack(anchor="e", pady=(8, 0))

        root.bind("<Return>", lambda e: self.start_conversion())
        root.bind("<Escape>", lambda e: self.cancel_conversion())

    # --- folder selection ---
    def select_folder(self):
        folder = filedialog.askdirectory(title="Choose the folder with DICOM files")
        if not folder:
            return
        self.input_folder = folder
        self.input_label.config(text=shorten_path(folder), foreground=COLORS["text"])
        if self.output_folder is None:
            self.output_label.config(text=shorten_path(os.path.join(folder, OUTPUT_DIR_NAME)))
        self.btn_convert.config(state="normal")
        self.set_status("Ready. Press Convert to start.", COLORS["secondary"])

    def select_output(self):
        folder = filedialog.askdirectory(title="Choose where to save the videos")
        if not folder:
            return
        self.output_folder = folder
        self.output_label.config(text=shorten_path(folder), foreground=COLORS["text"])

    def open_output(self):
        if self.last_output and os.path.isdir(self.last_output):
            subprocess.call(["open", self.last_output])

    # --- helpers ---
    def set_status(self, text, color=None):
        self.status_label.config(text=text, fg=color or COLORS["secondary"])

    def append_log(self, text, tag="info"):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.config(state="disabled")

    def set_running(self, running):
        idle = "disabled" if running else "normal"
        self.btn_select.config(state=idle)
        self.btn_output.config(state=idle)
        self.btn_convert.config(state="disabled" if running or not self.input_folder else "normal")
        self.btn_cancel.config(state="normal" if running else "disabled")

    # --- conversion ---
    def start_conversion(self, folder=None):
        if folder:
            self.input_folder = folder
            self.input_label.config(text=shorten_path(folder), foreground=COLORS["text"])
        if not self.input_folder or str(self.btn_cancel["state"]) == "normal":
            return

        self.counts = {"converted": 0, "skipped": 0, "failed": 0}
        for key in self.stat_values:
            self.stat_values[key].config(text="0")
        self.total = 0
        self.progress["value"] = 0
        self.count_label.config(text="")
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self.append_log(f"Source: {shorten_path(self.input_folder, 80)}")
        self.btn_open.config(state="disabled")
        self.set_status("Scanning files…", COLORS["accent"])
        self.set_running(True)

        self.cancel_event = threading.Event()
        # Daemon thread: closing the window mid-conversion actually quits the app.
        t = threading.Thread(target=convert_dicom_thread, args=(
            self.input_folder,
            self.update_status,
            self.update_progress,
            self.on_finish,
        ), kwargs={
            "output_folder": self.output_folder,
            "event_callback": self.on_event,
            "cancel_event": self.cancel_event,
        }, daemon=True)
        t.start()

    def cancel_conversion(self):
        if self.cancel_event is not None and str(self.btn_cancel["state"]) == "normal":
            self.cancel_event.set()
            self.btn_cancel.config(state="disabled")
            self.set_status("Stopping after the current file…", COLORS["orange"])

    # Callbacks below run on the worker thread; root.after hands the work to the Tk thread.
    def update_status(self, text):
        self.root.after(0, lambda: self.set_status(text, COLORS["accent"]))

    def update_progress(self, val):
        def _apply():
            self.progress.config(value=val)
            if self.total:
                done = min(self.total, round(val * self.total / 100))
                self.count_label.config(text=f"{done} of {self.total} files")
        self.root.after(0, _apply)

    def on_event(self, kind, text):
        def _apply():
            if kind == "found":
                self.total = int(text)
                self.append_log(f"Found {self.total} files to check")
                return
            self.counts[kind] += 1
            self.stat_values[kind].config(text=str(self.counts[kind]))
            symbol = {"converted": "✓", "skipped": "–", "failed": "✗"}[kind]
            self.append_log(f"{symbol} {text}", kind)
        self.root.after(0, _apply)

    def on_finish(self, summary):
        def _finish():
            self.set_running(False)
            self.cancel_event = None
            out_folder = summary["output_folder"]
            self.last_output = out_folder
            if out_folder and os.path.isdir(out_folder):
                self.btn_open.config(state="normal")

            if summary["error"]:
                self.set_status("Conversion stopped by an error.", COLORS["red"])
                self.append_log(f"✗ {summary['error']}", "failed")
                messagebox.showerror("Error", f"Conversion stopped:\n\n{summary['error']}")
                return

            failed = len(summary["failed"])
            converted = summary["converted"]
            if summary["cancelled"]:
                self.set_status(f"Cancelled. {converted} videos were saved before stopping.", COLORS["orange"])
                self.append_log("Cancelled")
            elif failed:
                self.set_status(f"Done with problems: {converted} converted, {failed} failed (see {LOG_NAME}).", COLORS["orange"])
            elif converted == 0 and summary["skipped_existing"] == 0:
                self.set_status("No DICOM videos found in this folder.", COLORS["orange"])
            else:
                self.set_status(f"Done! {converted} new videos saved.", COLORS["green"])
                self.progress.config(value=100)
            self.append_log(f"Saved in: {shorten_path(out_folder, 80)}")

            if converted and not summary["cancelled"]:
                subprocess.call(["open", out_folder])

        self.root.after(0, _finish)

if __name__ == "__main__":
    root = tk.Tk()
    app = DicomApp(root)
    root.mainloop()
