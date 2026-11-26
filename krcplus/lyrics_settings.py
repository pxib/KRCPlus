from dataclasses import dataclass


LYRIC_LAYER_ORDER = ("lyrics", "romaji", "translation", "furigana")


@dataclass(frozen=True)
class LyricShadow:
    color: str = "#000000"
    opacity: int = 63
    offset_x: int = 2
    offset_y: int = 2
    blur: int = 0

    @property
    def spread(self):
        return 3 * self.blur


DEFAULT_LYRIC_SHADOW = LyricShadow()


def text_effect_padding(stroke_width, enabled, shadow):
    if enabled and shadow.opacity:
        return (
            max(stroke_width, abs(shadow.offset_x) + shadow.spread),
            max(0, shadow.spread - shadow.offset_y),
            max(0, shadow.spread + shadow.offset_y),
        )
    return stroke_width, 0, 0


def normalize_layer_order(value):
    order = []
    for layer in value if isinstance(value, (list, tuple)) else ():
        if layer in LYRIC_LAYER_ORDER and layer not in order:
            order.append(layer)
    return order + [layer for layer in LYRIC_LAYER_ORDER if layer not in order]
