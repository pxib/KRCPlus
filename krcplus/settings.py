from pathlib import Path

from PySide6.QtCore import QStandardPaths


KARAOKE_SWEEP_STYLES = {
    "classic": "Classic",
    "soft": "Soft",
    "glow": "Glow",
    "shimmer": "Shimmer",
    "rainbow": "Rainbow",
    "pulse": "Pulse",
    "spotlight": "Spotlight",
}

KARAOKE_DEFAULTS = {
    "sweep_fps": 60,
    "sweep_style": "classic",
    "sweep_easing": "linear",
    "softness": 8,
    "glow_intensity": 70,
    "glow_radius": 1,
    "shimmer_width": 10,
    "sweep_delay_ms": 100,
    "sweep_delay_percent": 10,
    "line_entrance": True,
    "word_lift": True,
}


def get_app_data_dir():
    return Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))


def apply_karaoke_settings(widget, values):
    keys = {"sweep_style": "style", "sweep_easing": "easing"}
    widget.set_karaoke_properties(**{
        keys.get(key, key): value for key, value in values.items() if key != "sweep_fps"
    })
