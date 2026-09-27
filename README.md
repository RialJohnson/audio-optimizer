# Audio Optimizer

Drop folders or zip files onto the window. They are converted one at a time into `originalName_optimized`. Inside that folder, files are placed in `year - album name` using the first song that has both an album name and a release year. If no song has those tags, the inner folder keeps the original folder name. Originals are not changed.

- Choose **CD Quality FLAC (44/16)** (the default) or **High Quality MP3 (320kbs)**. FLAC sources become 44.1 kHz / 16-bit FLAC, or 320 kbps MP3 when that option is selected. An MP3 source is always written as MP3 and is never converted to FLAC. Its bitrate stays at the highest standard rate that does not exceed the source, up to 320 kbps.
- Files in subfolders and zip archives are included. When **Normalize file names** is checked (the default), each audio file is saved as `01. Track Title` plus `.flac` or `.mp3`, using the track number and title. A `.lrc` file with the same name is renamed to match. A file keeps its original name when either tag is missing.
- When **Normalize artist tags** is checked (the default), multi-artist tags such as `Artist1;Artist2`, `Artist1\Artist2`, or `Artist1, Artist2` are rewritten as `Artist1; Artist2`.
- When **Optimize cover photo** is checked (the default), `cover.jpg` is resized to fit within 600×600 and saved as a baseline (non-progressive) JPEG. Uncheck it to copy covers unchanged.
- Every other file is copied.

The conversion uses ffmpeg. It must be on your PATH, or the `ffmpeg` program must sit in the same folder as Audio Optimizer. On Windows that file is named `ffmpeg.exe`.

## Build

Use 64-bit Python 3.9, 3.10, 3.11, or 3.12. Python 3.13 is not supported by the Pillow version in `requirements.txt`.

Close Audio Optimizer before building again. If it is still open, the build cannot replace the program.

### Windows

Install [Python](https://www.python.org/downloads/windows/) and [ffmpeg](https://ffmpeg.org/download.html). ffmpeg must be on your PATH when you run the program, or `ffmpeg.exe` must sit next to `AudioOptimizer.exe`.

From this folder:

```powershell
py -3.9 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe build.py
```

The program is written to `dist\AudioOptimizer.exe`.

### Linux

Install Python, Tk, and ffmpeg. On Debian or Ubuntu:

```bash
sudo apt install python3 python3-venv python3-tk python3-pip ffmpeg
```

From this folder:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python build.py
```

The program is written to `dist/AudioOptimizer`. Run it with `./dist/AudioOptimizer`.

## Run from source

Windows:

```powershell
.\.venv\Scripts\python.exe audio_optimizer.py
```

Linux:

```bash
.venv/bin/python audio_optimizer.py
```
