# Audio Optimizer

Drop folders or zip files onto the window. They are converted one at a time into a new folder named `originalName_optimized`. Originals are not changed.

- Choose **CD Quality FLAC (44/16)** (the default) or **High Quality MP3 (320kbs)**. FLAC sources become 44.1 kHz / 16-bit FLAC, or 320 kbps MP3 when that option is selected. An MP3 source is always written as MP3 and is never converted to FLAC. Its bitrate stays at the highest standard rate that does not exceed the source, up to 320 kbps.
- Files in subfolders and zip archives are included. When **Normalize file names** is checked (the default), each audio file is saved as `01. Track Title` plus `.flac` or `.mp3`, using the track number and title. A `.lrc` file with the same name is renamed to match. A file keeps its original name when either tag is missing.
- When **Normalize artist tags** is checked (the default), multi-artist tags such as `Artist1;Artist2`, `Artist1\Artist2`, or `Artist1, Artist2` are rewritten as `Artist1; Artist2`.
- When **Optimize cover photo** is checked (the default), `cover.jpg` is resized to fit within 600×600 and saved as a baseline (non-progressive) JPEG. Uncheck it to copy covers unchanged.
- Every other file is copied.

The conversion uses ffmpeg. It must be on your PATH, or `ffmpeg.exe` must sit in the same folder as the program.

## Build the .exe

Use 64-bit Python 3.9, 3.10, 3.11, or 3.12 on Windows. Python 3.13 is not supported by the Pillow version in `requirements.txt`.

From this folder:

```powershell
py -3.9 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --windowed --name AudioOptimizer --collect-all tkinterdnd2 audio_optimizer.py
```

The program is written to `dist\AudioOptimizer.exe`.

Close Audio Optimizer before building again. If it is still open, PyInstaller cannot replace the exe.

## Run from source

```powershell
.\.venv\Scripts\python.exe audio_optimizer.py
```
