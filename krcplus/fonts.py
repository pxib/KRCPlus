"""System font selection for lyrics and readings."""

from functools import lru_cache
import unicodedata

from PySide6.QtGui import QFont, QFontDatabase, QRawFont


LYRIC_FONT_FAMILIES = ("Noto Sans JP", "Yu Gothic UI", "Hiragino Sans")

# Prefer modern outline fonts over Windows' much larger legacy fallback path.
CONTENT_FALLBACK_FAMILIES = (
    "Microsoft YaHei UI", "Malgun Gothic", "Noto Sans CJK SC",
    "Noto Sans CJK KR", "PingFang SC", "Apple SD Gothic Neo",
)


@lru_cache(maxsize=1)
def _library_font_family():
    return next((family for family in LYRIC_FONT_FAMILIES
                 if QFontDatabase.hasFamily(family)), None)


def library_font(base_font):
    """Keep the view's size and style, preferring an installed Japanese font."""
    font = QFont(base_font)
    family = _library_font_family()
    if family:
        font.setFamily(family)
    return font


def content_font(base_font, text):
    """Use Noto's glyphs when covered, allowing fallback for other text.

    Windows can otherwise load large system fonts for Simplified Chinese even
    when Noto has those glyphs. Check coverage before disabling font merging
    so text containing unsupported characters still renders normally.
    """
    font = library_font(base_font)
    if _library_font_family():
        return covering_font(font, text)
    font.setStyleStrategy(font.styleStrategy() & ~QFont.StyleStrategy.NoFontMerging)
    return font


def covering_font(base_font, text):
    """Keep a chosen face when it covers the text, otherwise try modern fonts."""
    font = QFont(base_font)
    strategy = font.styleStrategy() & ~QFont.StyleStrategy.NoFontMerging
    font.setStyleStrategy(strategy)
    raw_font = QRawFont.fromFont(font)
    # Use UCS-4 integers; PySide's string overload can misreport coverage.
    characters = {ord(char) for char in unicodedata.normalize("NFC", text)
                  if not char.isspace()}
    if (raw_font.isValid()
            and all(raw_font.supportsCharacter(char) for char in characters)):
        font.setStyleStrategy(strategy | QFont.StyleStrategy.NoFontMerging)
        return font
    for fallback in CONTENT_FALLBACK_FAMILIES:
        if not QFontDatabase.hasFamily(fallback):
            continue
        candidate = QFont(font)
        candidate.setFamily(fallback)
        raw_font = QRawFont.fromFont(candidate)
        if (raw_font.isValid()
                and all(raw_font.supportsCharacter(char) for char in characters)):
            candidate.setStyleStrategy(strategy | QFont.StyleStrategy.NoFontMerging)
            return candidate
    return font
