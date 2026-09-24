"""Convert FLAC files to CD quality without touching the originals."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import zipfile
from collections import deque

from PIL import Image, ImageOps
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # Drag-and-drop is optional so the window still opens.
    DND_FILES = None
    TkinterDnD = None

CREATE_NO_WINDOW = 0x08000000
COVER_MAX_EDGE = 600


class Cancelled(Exception):
    pass


def destination_for(source: str) -> str:
    source = os.path.abspath(source)
    if _is_zip_file(source):
        source = os.path.splitext(source)[0]
    return source.rstrip("\\/") + "_optimized"


def _is_zip_file(path: str) -> bool:
    return os.path.isfile(path) and path.lower().endswith(".zip")


def _is_source(path: str) -> bool:
    return os.path.isdir(path) or _is_zip_file(path)


def find_ffmpeg() -> str | None:
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(os.path.dirname(sys.executable))
    candidates.append(os.path.dirname(os.path.abspath(__file__)))

    for directory in candidates:
        path = os.path.join(directory, "ffmpeg.exe")
        if os.path.isfile(path):
            return path

    return shutil.which("ffmpeg")


def convert_folder(
    source,
    ffmpeg,
    on_progress,
    cancel_event,
    optimize_covers=True,
    rename_flacs=True,
    normalize_artists=True,
):
    """Convert FLACs to 44.1 kHz / 16-bit and copy every other file.

    Subfolders are included. Zip files are extracted into the output folder
    and converted the same way. The source folder or archive is not modified.
    cover.jpg is resized only when optimize_covers is true.
    FLAC files are renamed from track number and title when rename_flacs is true.
    Artist tags are rewritten as "Artist1; Artist2" when normalize_artists is true.
    """
    source = os.path.abspath(source)
    if not _is_source(source):
        raise NotADirectoryError("'%s' is not a folder or a zip file." % source)

    destination = destination_for(source)
    if os.path.isdir(source) and _is_inside(destination, source):
        raise RuntimeError("The output folder would be inside the folder being converted.")

    os.makedirs(destination, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix="file-optimizer-")
    converted = 0
    covers = 0
    copied = 0
    failed = 0
    try:
        on_progress(0, 0, "Scanning…")
        jobs = []
        if _is_zip_file(source):
            _collect_zip(source, destination, jobs, temp_dir, cancel_event, optimize_covers=optimize_covers)
        else:
            _collect_dir(source, destination, jobs, temp_dir, cancel_event, optimize_covers=optimize_covers)
        _label_jobs(destination, jobs)
        used_destinations = {os.path.normcase(job["dest"]) for job in jobs if job.get("dest")}
        ffprobe = _ffprobe_beside(ffmpeg) if rename_flacs else None
        if rename_flacs and not ffprobe:
            on_progress(0, 0, "Note: ffprobe was not found, so FLAC names were kept.")

        total = len(jobs)
        for index, job in enumerate(jobs, start=1):
            if cancel_event.is_set():
                raise Cancelled()

            label = job["label"]
            on_progress(index - 1, total, label)
            try:
                if job["kind"] == "error":
                    raise RuntimeError(job["error"])
                if job["kind"] == "flac":
                    if rename_flacs:
                        _apply_flac_name(job, job["path"], ffprobe, used_destinations, destination)
                        label = job["label"]
                    os.makedirs(os.path.dirname(job["dest"]), exist_ok=True)
                    metadata = _artist_metadata_args(job["path"], ffmpeg) if normalize_artists else None
                    _convert_flac(ffmpeg, job["path"], job["dest"], cancel_event, metadata)
                    converted += 1
                    on_progress(index, total, "Converted %s" % label)
                elif job["kind"] == "cover":
                    _optimize_cover(job["path"], job["dest"])
                    covers += 1
                    on_progress(index, total, "Converted %s" % label)
                elif job["kind"] == "copy":
                    os.makedirs(os.path.dirname(job["dest"]), exist_ok=True)
                    shutil.copy2(job["path"], job["dest"])
                    copied += 1
                    on_progress(index, total, "Copied %s" % label)
                elif job["kind"] == "zip-cover":
                    extracted = _extract_zip_member(job["zip_path"], job["member"], temp_dir)
                    try:
                        _optimize_cover(extracted, job["dest"])
                    finally:
                        if os.path.isfile(extracted):
                            os.remove(extracted)
                    covers += 1
                    on_progress(index, total, "Converted %s" % label)
                elif job["kind"] == "zip-flac":
                    extracted = _extract_zip_member(job["zip_path"], job["member"], temp_dir)
                    try:
                        if rename_flacs:
                            _apply_flac_name(job, extracted, ffprobe, used_destinations, destination)
                            label = job["label"]
                        os.makedirs(os.path.dirname(job["dest"]), exist_ok=True)
                        metadata = _artist_metadata_args(extracted, ffmpeg) if normalize_artists else None
                        _convert_flac(ffmpeg, extracted, job["dest"], cancel_event, metadata)
                    finally:
                        if os.path.isfile(extracted):
                            os.remove(extracted)
                    converted += 1
                    on_progress(index, total, "Converted %s" % label)
                elif job["kind"] == "zip-copy":
                    os.makedirs(os.path.dirname(job["dest"]), exist_ok=True)
                    _extract_zip_member(job["zip_path"], job["member"], job["dest"])
                    copied += 1
                    on_progress(index, total, "Copied %s" % label)
                else:
                    raise RuntimeError("Unknown job %s" % job["kind"])
            except Cancelled:
                raise
            except Exception as exc:
                failed += 1
                detail = str(exc).strip() or exc.__class__.__name__
                on_progress(index, total, "Failed: %s — %s" % (label, detail))
                if os.path.isfile(job.get("dest", "")):
                    try:
                        os.remove(job["dest"])
                    except OSError:
                        pass
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    on_progress(total, total, "Done.")
    return {
        "converted": converted,
        "covers": covers,
        "copied": copied,
        "failed": failed,
        "destination": destination,
        "total": total,
    }


def _collect_dir(directory, dest_root, jobs, temp_dir, cancel_event, optimize_covers=True):
    if cancel_event.is_set():
        raise Cancelled()

    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        jobs.append({"kind": "error", "label": directory, "error": str(exc), "dest": ""})
        return

    entries.sort(key=lambda entry: entry.name.lower())
    for entry in entries:
        if cancel_event.is_set():
            raise Cancelled()
        if entry.name == "__MACOSX":
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
            is_file = entry.is_file(follow_symlinks=False)
        except OSError as exc:
            jobs.append({"kind": "error", "label": entry.name, "error": str(exc), "dest": ""})
            continue
        if is_dir:
            _collect_dir(
                entry.path,
                os.path.join(dest_root, entry.name),
                jobs,
                temp_dir,
                cancel_event,
                optimize_covers=optimize_covers,
            )
        elif is_file and entry.name.lower().endswith(".zip"):
            stem = os.path.splitext(entry.name)[0]
            _collect_zip(
                entry.path,
                os.path.join(dest_root, stem),
                jobs,
                temp_dir,
                cancel_event,
                optimize_covers=optimize_covers,
            )
        elif is_file and entry.name.lower().endswith(".flac"):
            jobs.append({
                "kind": "flac",
                "label": entry.name,
                "path": entry.path,
                "dest": os.path.join(dest_root, entry.name),
            })
        elif is_file and optimize_covers and _is_cover_jpg(entry.name):
            jobs.append({
                "kind": "cover",
                "label": entry.name,
                "path": entry.path,
                "dest": os.path.join(dest_root, entry.name),
            })
        elif is_file:
            jobs.append({
                "kind": "copy",
                "label": entry.name,
                "path": entry.path,
                "dest": os.path.join(dest_root, entry.name),
            })


def _collect_zip(zip_path, dest_root, jobs, temp_dir, cancel_event, depth=0, optimize_covers=True):
    if cancel_event.is_set():
        raise Cancelled()
    if depth > 8:
        jobs.append({
            "kind": "error",
            "label": os.path.basename(zip_path),
            "error": "Zip is nested too deeply.",
            "dest": "",
        })
        return

    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        jobs.append({
            "kind": "error",
            "label": os.path.basename(zip_path),
            "error": str(exc),
            "dest": "",
        })
        return

    with archive:
        members = list(archive.infolist())
        members.sort(key=lambda info: info.filename.lower())
        for info in members:
            if cancel_event.is_set():
                raise Cancelled()
            parts = _safe_zip_parts(info.filename)
            if parts is None:
                jobs.append({
                    "kind": "error",
                    "label": info.filename,
                    "error": "Skipped an unsafe path in the zip.",
                    "dest": "",
                })
                continue
            if not parts or "__MACOSX" in parts:
                continue
            if info.is_dir():
                continue
            dest = os.path.join(dest_root, *parts)
            name = parts[-1]
            if name.lower().endswith(".zip"):
                nested_dest = os.path.join(dest_root, *parts[:-1], os.path.splitext(name)[0])
                nested_path = _extract_zip_member(archive, info.filename, temp_dir)
                _collect_zip(
                    nested_path,
                    nested_dest,
                    jobs,
                    temp_dir,
                    cancel_event,
                    depth + 1,
                    optimize_covers=optimize_covers,
                )
            elif name.lower().endswith(".flac"):
                jobs.append({
                    "kind": "zip-flac",
                    "label": name,
                    "zip_path": zip_path,
                    "member": info.filename,
                    "dest": dest,
                })
            elif optimize_covers and _is_cover_jpg(name):
                jobs.append({
                    "kind": "zip-cover",
                    "label": name,
                    "zip_path": zip_path,
                    "member": info.filename,
                    "dest": dest,
                })
            else:
                jobs.append({
                    "kind": "zip-copy",
                    "label": name,
                    "zip_path": zip_path,
                    "member": info.filename,
                    "dest": dest,
                })


def _label_jobs(dest_root, jobs):
    for job in jobs:
        dest = job.get("dest")
        if dest and os.path.isabs(dest):
            try:
                job["label"] = os.path.relpath(dest, dest_root)
            except ValueError:
                pass


def _safe_zip_parts(name: str):
    name = name.replace("\\", "/").strip()
    if not name or name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
        return None
    parts = []
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return parts


def _extract_zip_member(archive_or_path, member, target):
    """Write one zip member to target. target may be a directory or a file path."""
    if os.path.isdir(target):
        filename = os.path.basename(member.replace("\\", "/")) or "member"
        handle, target = tempfile.mkstemp(prefix="member-", suffix="-" + filename, dir=target)
        os.close(handle)

    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)

    opened_here = False
    archive = archive_or_path
    if isinstance(archive_or_path, str):
        archive = zipfile.ZipFile(archive_or_path)
        opened_here = True
    try:
        with archive.open(member, "r") as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
    finally:
        if opened_here:
            archive.close()
    return target


def _is_inside(path, parent):
    path = os.path.abspath(path)
    parent = os.path.abspath(parent)
    try:
        return os.path.commonpath([path, parent]) == parent
    except ValueError:
        return False


def _ffprobe_beside(ffmpeg):
    folder = os.path.dirname(os.path.abspath(ffmpeg))
    candidate = os.path.join(folder, "ffprobe.exe")
    if os.path.isfile(candidate):
        return candidate
    return shutil.which("ffprobe")


def _apply_flac_name(job, source_path, ffprobe, used_destinations, output_root):
    """Point a FLAC job at NN. Title.flac when both tags are present."""
    filename = _flac_name_from_tags(source_path, ffprobe)
    if not filename:
        return
    new_dest = _unique_dest(os.path.dirname(job["dest"]), filename, used_destinations, job["dest"])
    old_key = os.path.normcase(job["dest"])
    new_key = os.path.normcase(new_dest)
    if new_key == old_key:
        return
    used_destinations.discard(old_key)
    used_destinations.add(new_key)
    job["dest"] = new_dest
    try:
        job["label"] = os.path.relpath(new_dest, output_root)
    except ValueError:
        job["label"] = os.path.basename(new_dest)


def _unique_dest(directory, filename, used_destinations, current):
    root, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    current_key = os.path.normcase(current)
    number = 2
    while True:
        key = os.path.normcase(candidate)
        if key == current_key or key not in used_destinations:
            return candidate
        candidate = os.path.join(directory, "%s (%d)%s" % (root, number, ext))
        number += 1


def _flac_name_from_tags(path, ffprobe):
    if not ffprobe:
        return None
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format_tags",
        "-of",
        "json",
        path,
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        return None
    tags = payload.get("format", {}).get("tags") or {}
    folded = {str(key).lower(): value for key, value in tags.items()}
    number = _parse_track_number(folded.get("tracknumber") or folded.get("track") or "")
    title = _clean_track_title(folded.get("title") or "")
    if number is None or not title:
        return None
    return "%02d. %s.flac" % (number, title)


def _parse_track_number(value):
    text = str(value).strip().split("/")[0].strip()
    if not text.isdigit():
        return None
    number = int(text)
    if number < 0 or number > 999:
        return None
    return number


def _clean_track_title(value):
    chars = []
    for char in str(value).replace("\x00", ""):
        if char in '<>:"/\\|?*' or ord(char) < 32:
            chars.append(" ")
        else:
            chars.append(char)
    title = " ".join("".join(chars).split()).strip(" .")
    return title


def _is_cover_jpg(name: str) -> bool:
    return os.path.basename(name).lower() == "cover.jpg"


def _optimize_cover(src, dest):
    """Fit cover.jpg inside 600x600 and save a baseline optimized JPEG."""
    with Image.open(src) as image:
        image = ImageOps.exif_transpose(image)
        image = _to_rgb(image)
        image.thumbnail((COVER_MAX_EDGE, COVER_MAX_EDGE), Image.Resampling.LANCZOS)
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        image.save(
            dest,
            format="JPEG",
            quality=85,
            optimize=True,
            progressive=False,
        )


def _to_rgb(image):
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


_ARTIST_SEPARATOR = re.compile(
    r"""
    (?:
        \s*;+\s* |
        \s*\\+\s* |
        \s*\|+\s* |
        \s+/\s+ |
        ,\s+ |
        \s+(?:featuring|feat\.?|ft\.?|vs\.?|w/|x)\s+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _normalize_artist_list(value):
    """Turn common multi-artist spellings into 'Artist1; Artist2'."""
    parts = []
    for part in _ARTIST_SEPARATOR.split(str(value)):
        part = " ".join(part.split()).strip()
        if part and part not in parts:
            parts.append(part)
    if len(parts) < 2:
        return None
    return "; ".join(parts)


def _artist_tag_kind(key):
    folded = key.replace(" ", "").replace("_", "").lower()
    if folded in ("artist", "albumartist", "artists"):
        return folded
    return None


def _read_ffmetadata(path, ffmpeg):
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        path,
        "-f",
        "ffmetadata",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    return _parse_ffmetadata(completed.stdout.decode("utf-8", errors="replace"))


def _parse_ffmetadata(text):
    logical = []
    current = ""
    for line in text.splitlines():
        current = line if not current else current + "\n" + line
        trailing = len(current) - len(current.rstrip("\\"))
        if trailing % 2 == 1:
            current = current[:-1]
            continue
        logical.append(current)
        current = ""
    if current:
        logical.append(current)

    entries = []
    for line in logical:
        if not line or line.startswith(";") or line.startswith("["):
            continue
        key, value = _split_ffmeta(line)
        if key is None:
            continue
        entries.append((key, value))
    return entries


def _split_ffmeta(line):
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "=":
            return line[:index], _unescape_ffmeta(line[index + 1 :])
    return None, None


def _unescape_ffmeta(value):
    chars = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            chars.append(value[index + 1])
            index += 2
        else:
            chars.append(value[index])
            index += 1
    return "".join(chars)


def _artist_metadata_args(path, ffmpeg):
    grouped = {}
    for key, value in _read_ffmetadata(path, ffmpeg):
        kind = _artist_tag_kind(key)
        if not kind:
            continue
        bucket = grouped.setdefault(kind, {"key": key, "values": []})
        bucket["values"].append(value)

    arguments = []
    for bucket in grouped.values():
        parts = []
        for value in bucket["values"]:
            split = _ARTIST_SEPARATOR.split(value)
            pieces = [" ".join(piece.split()).strip() for piece in split]
            pieces = [piece for piece in pieces if piece]
            if not pieces:
                continue
            for piece in pieces:
                if piece not in parts:
                    parts.append(piece)
        if len(parts) < 2:
            continue
        normalized = "; ".join(parts)
        original = "; ".join(value.strip() for value in bucket["values"])
        if normalized == original:
            continue
        arguments.extend(["-metadata", "%s=%s" % (bucket["key"], normalized)])
    return arguments


def _convert_flac(ffmpeg, infile, outfile, cancel_event, metadata_args=None):
    # Same flags as the shell script. -y avoids an overwrite prompt on a re-run.
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        infile,
        "-ar",
        "44100",
        "-sample_fmt",
        "s16",
        "-c:a",
        "flac",
        "-map_metadata",
        "0",
    ]
    if metadata_args:
        command.extend(metadata_args)
    command.append(outfile)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
    )
    while True:
        try:
            _, stderr = process.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if cancel_event.is_set():
                process.kill()
                process.communicate()
                raise Cancelled()

    if cancel_event.is_set():
        raise Cancelled()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        if not detail:
            detail = "ffmpeg exited with code %s." % process.returncode
        raise RuntimeError(detail)


class App:
    def __init__(self):
        if TkinterDnD is not None:
            self.root = TkinterDnD.Tk()
        else:
            self.root = tk.Tk()

        self.root.title("File Optimizer")
        self.root.geometry("680x660")
        self.root.minsize(560, 560)
        self.root.configure(bg="white")

        self._events = queue.Queue()
        self._pending = deque()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._cancel = threading.Event()
        self._current = None
        self._worker = None
        self._ffmpeg = None
        self._destination = None
        self._drag_over = False

        self._build()
        self.root.after(100, self._poll)

        initial = [arg for arg in sys.argv[1:] if _is_source(arg)]
        if initial:
            self.root.after(200, lambda: self.enqueue(initial))

    def _build(self):
        header = tk.Frame(self.root, bg="white")
        header.pack(fill="x", padx=20, pady=(16, 4))
        tk.Label(
            header,
            text="FLAC to CD quality",
            font=("Segoe UI", 16, "bold"),
            bg="white",
            fg="#0f172a",
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            header,
            text=(
                "Drop folders or zips. FLACs become 44.1 kHz / 16-bit. cover.jpg can be resized to 600×600.\n"
                "They convert one at a time. Results go in a new folder ending in _optimized."
            ),
            font=("Segoe UI", 9),
            bg="white",
            fg="#475569",
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(4, 0))

        self.drop = tk.Frame(
            self.root,
            bg="#f8fafc",
            highlightthickness=2,
            highlightbackground="#cbd5e1",
            height=120,
            cursor="hand2",
        )
        self.drop.pack(fill="x", padx=20, pady=(12, 4))
        self.drop.pack_propagate(False)
        self.drop_label = tk.Label(
            self.drop,
            text="Drop folders or zips here\nThey convert one at a time",
            font=("Segoe UI", 12, "bold"),
            bg="#f8fafc",
            fg="#0f172a",
            cursor="hand2",
        )
        self.drop_label.place(relx=0.5, rely=0.5, anchor="center")
        self.drop.bind("<Button-1>", lambda _event: self.choose_folder())
        self.drop_label.bind("<Button-1>", lambda _event: self.choose_folder())

        if DND_FILES is not None:
            for widget in (self.root, self.drop, self.drop_label):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<DragEnter>>", self._drag_enter)
                widget.dnd_bind("<<DragLeave>>", self._drag_leave)
                widget.dnd_bind("<<Drop>>", self._on_drop)

        queue_host = tk.Frame(self.root, bg="white")
        queue_host.pack(fill="x", padx=20, pady=(8, 0))
        tk.Label(
            queue_host,
            text="Queue",
            font=("Segoe UI", 9, "bold"),
            bg="white",
            fg="#334155",
            anchor="w",
        ).pack(fill="x")
        queue_row = tk.Frame(queue_host, bg="white")
        queue_row.pack(fill="x", pady=(2, 0))
        self.queue_list = tk.Listbox(
            queue_row,
            height=4,
            font=("Segoe UI", 9),
            bg="#f8fafc",
            fg="#0f172a",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#e2e8f0",
            activestyle="none",
        )
        queue_scroll = ttk.Scrollbar(queue_row, command=self.queue_list.yview)
        self.queue_list.configure(yscrollcommand=queue_scroll.set)
        queue_scroll.pack(side="right", fill="y")
        self.queue_list.pack(side="left", fill="x", expand=True)

        buttons = tk.Frame(self.root, bg="white")
        buttons.pack(fill="x", padx=20, pady=(8, 4))
        self.choose_button = tk.Button(buttons, text="Choose folder", command=self.choose_folder)
        self.choose_button.pack(side="left")
        self.zip_button = tk.Button(buttons, text="Choose zip", command=self.choose_zip)
        self.zip_button.pack(side="left", padx=(8, 0))
        self.cancel_button = tk.Button(buttons, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.open_button = tk.Button(
            buttons, text="Open output folder", command=self.open_destination, state="disabled"
        )
        self.open_button.pack(side="left", padx=(8, 0))

        options = tk.Frame(self.root, bg="white")
        options.pack(fill="x", padx=20, pady=(0, 4))
        self.cover_var = tk.BooleanVar(value=True)
        self._optimize_covers = True
        self.cover_var.trace_add("write", self._on_cover_toggle)
        self.rename_var = tk.BooleanVar(value=True)
        self._rename_flacs = True
        self.rename_var.trace_add("write", self._on_rename_toggle)
        self.artist_var = tk.BooleanVar(value=True)
        self._normalize_artists = True
        self.artist_var.trace_add("write", self._on_artist_toggle)
        for text, variable in (
            ("Optimize cover photo", self.cover_var),
            ("Normalize file names", self.rename_var),
            ("Normalize artist tags", self.artist_var),
        ):
            tk.Checkbutton(
                options,
                text=text,
                variable=variable,
                bg="white",
                activebackground="white",
                font=("Segoe UI", 9),
            ).pack(side="left", padx=(0, 12))

        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill="x", padx=20, pady=(8, 0))

        self.status = tk.Label(
            self.root,
            text="Waiting for a folder or a zip." if DND_FILES is not None else "Choose a folder or a zip to begin.",
            font=("Segoe UI", 9),
            bg="white",
            fg="#334155",
            anchor="w",
        )
        self.status.pack(fill="x", padx=20, pady=(8, 0))

        log_host = tk.Frame(self.root, bg="white")
        log_host.pack(fill="both", expand=True, padx=20, pady=(4, 16))
        self.log = tk.Text(
            log_host,
            height=10,
            wrap="word",
            font=("Consolas", 9),
            bg="#f8fafc",
            fg="#0f172a",
            relief="flat",
            state="disabled",
        )
        scroll = ttk.Scrollbar(log_host, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    def mainloop(self):
        self.root.mainloop()

    def choose_folder(self):
        folder = filedialog.askdirectory(title="Choose a folder to convert")
        if folder:
            self.enqueue([folder])

    def choose_zip(self):
        path = filedialog.askopenfilename(
            title="Choose a zip to convert",
            filetypes=[("Zip archives", "*.zip"), ("All files", "*.*")],
        )
        if path:
            self.enqueue([path])

    def enqueue(self, paths):
        sources = []
        for path in paths:
            path = os.path.abspath(os.path.normpath(path))
            if _is_source(path) and path not in sources:
                sources.append(path)
        if not sources:
            self._set_status("Drop a folder or a zip, or a file inside the folder you want to convert.")
            return

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            messagebox.showwarning(
                "File Optimizer",
                "ffmpeg.exe was not found. Install ffmpeg and add it to PATH, "
                "or place ffmpeg.exe in the same folder as this program.",
            )
            return
        self._ffmpeg = ffmpeg

        added = []
        with self._wake:
            known = {os.path.normcase(item) for item in self._pending}
            if self._current:
                known.add(os.path.normcase(self._current))
            for source in sources:
                key = os.path.normcase(source)
                if key in known:
                    continue
                self._pending.append(source)
                known.add(key)
                added.append(source)
            if added and self._current is None:
                self._cancel.clear()
            self._wake.notify()

        if not added:
            self._set_status("Already in the queue.")
            self._sync_controls()
            return

        self._ensure_worker()
        self._sync_controls()
        if self._current is None:
            self._set_status("Queued %s." % ", ".join(os.path.basename(path) for path in added))

    def _ensure_worker(self):
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run_queue, daemon=True)
            self._worker.start()

    def _run_queue(self):
        while True:
            with self._wake:
                if not self._pending:
                    self._current = None
                    self._events.put(("state", None))
                while not self._pending:
                    self._wake.wait()
                source = self._pending.popleft()
                self._current = source
                self._cancel.clear()

            self._events.put(("begin", source))
            try:
                result = convert_folder(
                    source,
                    self._ffmpeg,
                    lambda done, total, message: self._events.put(("progress", done, total, message)),
                    self._cancel,
                    optimize_covers=self._optimize_covers,
                    rename_flacs=self._rename_flacs,
                    normalize_artists=self._normalize_artists,
                )
                self._events.put(("result", result))
            except Cancelled:
                self._events.put(("cancelled", source))
            except Exception as exc:
                self._events.put(("error", str(exc)))

    def _on_cover_toggle(self, *_args):
        self._optimize_covers = bool(self.cover_var.get())

    def _on_rename_toggle(self, *_args):
        self._rename_flacs = bool(self.rename_var.get())

    def _on_artist_toggle(self, *_args):
        self._normalize_artists = bool(self.artist_var.get())

    def cancel(self):
        with self._wake:
            self._pending.clear()
            self._cancel.set()
            self._wake.notify_all()
        self._set_status("Cancelling…")
        self._sync_controls()

    def open_destination(self):
        if self._destination and os.path.isdir(self._destination):
            os.startfile(self._destination)

    def _on_drop(self, event):
        self._set_drag_over(False)
        raw_paths = self.root.tk.splitlist(event.data)
        folders = []
        for path in raw_paths:
            path = os.path.normpath(path)
            if os.path.isdir(path) or _is_zip_file(path):
                folder = path
            elif os.path.isfile(path):
                folder = os.path.dirname(path)
            else:
                continue
            if folder and folder not in folders:
                folders.append(folder)
        self.enqueue(folders)

    def _drag_enter(self, _event):
        self._set_drag_over(True)

    def _drag_leave(self, _event):
        self._set_drag_over(False)

    def _set_drag_over(self, active):
        if active == self._drag_over:
            return
        self._drag_over = active
        bg = "#eff6ff" if active else "#f8fafc"
        border = "#2563eb" if active else "#cbd5e1"
        self.drop.configure(bg=bg, highlightbackground=border)
        self.drop_label.configure(bg=bg)

    def _poll(self):
        try:
            while True:
                item = self._events.get_nowait()
                kind = item[0]
                if kind == "progress":
                    _, done, total, message = item
                    if message == "Scanning…":
                        self._set_status("Scanning…" + self._waiting_suffix())
                        continue
                    maximum = total if total else 1
                    self.progress.configure(maximum=maximum, value=min(done, maximum))
                    if message != "Done.":
                        if message.startswith(("Converted ", "Copied ", "Failed:", "Note:")):
                            self._append_log(message)
                        shown = done if message.startswith(("Converted ", "Copied ", "Failed:")) else done + 1
                        self._set_status(
                            "(%s/%s) %s%s" % (min(shown, maximum), maximum, message, self._waiting_suffix())
                        )
                elif kind == "result":
                    result = item[1]
                    self._destination = result["destination"]
                    self.open_button.configure(state="normal")
                    if result["total"] == 0:
                        summary = "No files found."
                    else:
                        summary = "Done. %s FLAC converted, %s covers optimized, %s copied, %s failed." % (
                            result["converted"],
                            result["covers"],
                            result["copied"],
                            result["failed"],
                        )
                    self._set_status(summary + self._waiting_suffix())
                    self._append_log(summary)
                    self._append_log(result["destination"])
                    self._sync_controls()
                elif kind == "begin":
                    self.progress.configure(value=0, maximum=1)
                    self._append_log(item[1])
                    self._set_status("Starting %s%s" % (os.path.basename(item[1]), self._waiting_suffix()))
                    self._sync_controls()
                elif kind == "cancelled":
                    self._set_status("Cancelled.")
                    self._append_log("Cancelled.")
                    self._sync_controls()
                elif kind == "error":
                    self._set_status(item[1])
                    self._append_log(item[1])
                    self._sync_controls()
                elif kind == "state":
                    self._sync_controls()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _snapshot(self):
        with self._lock:
            return self._current, list(self._pending)

    def _waiting_suffix(self):
        _, pending = self._snapshot()
        if not pending:
            return ""
        noun = "folder" if len(pending) == 1 else "folders"
        return " — %s %s waiting" % (len(pending), noun)

    def _sync_controls(self):
        current, pending = self._snapshot()
        self.cancel_button.configure(state="normal" if current or pending else "disabled")
        self.queue_list.delete(0, "end")
        if current:
            self.queue_list.insert("end", "Converting   %s" % os.path.basename(current))
        for path in pending:
            self.queue_list.insert("end", "Waiting      %s" % os.path.basename(path))

    def _set_status(self, message):
        self.status.configure(text=message)

    def _append_log(self, line):
        self.log.configure(state="normal")
        if self.log.index("end-1c") != "1.0":
            self.log.insert("end", "\n")
        self.log.insert("end", line)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
