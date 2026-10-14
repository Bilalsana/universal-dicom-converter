# Universal DICOM Converter

A small macOS app that turns DICOM cine loops — cath lab angiography (XA) and echocardiography (US) — into MP4 videos you can play anywhere.

Pick a folder (for example a CD/DICOMDIR export), press **Convert**, and every multi-frame DICOM inside it, including subfolders, is saved as an H.264 MP4 in one output folder.

## Features

- Searches all subfolders; still images, reports and presentation states are skipped.
- Uses the frame rate stored in the file (`FrameTime`, `CineRate`, `RecommendedDisplayFrameRate` or `FrameTimeVector`).
- Grayscale uses the file's display window (Window Center/Width) and rescale values, with `MONOCHROME1` inverted correctly. Color echo clips (RGB, YBR, palette color) keep their colors.
- Compressed transfer syntaxes (JPEG, JPEG 2000) via `pylibjpeg`.
- Output names come from the folder path (`S0000001_SE000018_O0000001.mp4`), so series from different studies never collide.
- Re-running a folder skips videos that already exist.
- Files that fail are listed with the reason in `conversion_log.txt` in the output folder.
- Live progress, counts, an activity log and Cancel. The window follows macOS light and dark mode.

## Run from source

Requires Python 3.10+ with Tk.

```bash
pip install -r requirements.txt
python universal_converter.py
```

## Build the macOS app

```bash
pip install pyinstaller
pyinstaller --noconfirm UniversalConverter.spec
```

The app is created at `dist/Universal Converter.app`. Copy it to `/Applications`. The build is not notarized, so on first launch you may need to right-click it and choose **Open**.

## Privacy

Everything runs locally; no files leave your computer. Output file names come from folder names, and the videos contain only pixel data, but the source DICOM files contain patient information. Handle the input and output folders according to your institution's rules.

## Disclaimer

For viewing, teaching and sharing convenience only. Not a medical device and not for diagnostic use.
