# KRCPlus

KRCPlus is a standalone desktop karaoke player extracted from LRCPlus's Karaoke Screen. It plays local audio and displays two rows of synchronized lyrics, with word-by-word highlighting and authored ruby from LRCX files. A MIDI file can add a melody guide, and a microphone can display your live pitch over that guide.

## Run from source

Use Python 3.10 or newer. In the repository folder, create a virtual environment and install the dependencies.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m krcplus
```

On macOS or Linux, use `.venv/bin/python` for the last two commands. The MIDI guide and microphone use PortAudio through `sounddevice`.

## Play a song

1. Click **Open audio** or press **Ctrl+O**. KRCPlus looks beside the audio for a same-name `.lrcx` or `.lrc` file and a `.mid` or `.midi` file. Use **Open lyrics** or **Import MIDI** to choose a different file.
2. Press **Space** to play or pause and drag the seek bar to jump. Press **F11** to toggle fullscreen or **Esc** to leave it.
3. Select a melody track to display it. **Sync** shifts MIDI timing, and **Key** transposes the MIDI guide. Use **Play MIDI guide** for audible notes and **Start mic** to see your live pitch.

**Generate readings** is optional. It sends Japanese lyric text to [Yomi](https://yomi.onrender.com/) and generates Mandarin readings locally with pypinyin. Authored readings in LRCX display by default.

You can also open a song from the command line with `python -m krcplus "song.wav" --lyrics "song.lrcx"`.

## Development

Install pytest, then run `python -m pytest` from the repository root. The tests exercise playback loading and seeking, the lyric stage, MIDI import, and pitch detection without requiring an audio input device.

The karaoke screen and lyric renderer originated in [LRCPlus](https://github.com/pxib/lrcplus). See [LICENSE.txt](LICENSE.txt).
