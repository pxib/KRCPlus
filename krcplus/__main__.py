import argparse
from pathlib import Path
import sys

from PySide6.QtWidgets import QApplication

from .gui_garbage_collector import install_gui_garbage_collector
from .player import KaraokePlayer
from .screen import KaraokeScreen


def main():
    parser = argparse.ArgumentParser(description="Play local audio with synchronized karaoke lyrics.")
    parser.add_argument("audio", nargs="?", type=Path, help="Audio file to open")
    parser.add_argument("--lyrics", type=Path, help="LRC or LRCX file to use with the audio")
    args = parser.parse_args()
    if args.lyrics and not args.audio:
        parser.error("--lyrics requires an audio file")
    for path in (args.audio, args.lyrics):
        if path is not None and not path.is_file():
            parser.error(f"file does not exist: {path}")

    app = QApplication(sys.argv[:1])
    app.setApplicationName("KRCPlus")
    app.setOrganizationName("pxib")
    install_gui_garbage_collector(app)
    screen = KaraokeScreen()
    player = KaraokePlayer(screen)
    if args.audio:
        player.open_audio(args.audio)
    if args.lyrics:
        player._load_lyrics_or_warn(args.lyrics, dialog=False)
    app.aboutToQuit.connect(player.shutdown)
    screen.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
