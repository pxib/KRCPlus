from collections import OrderedDict

import pytest

from krcplus import furigana as readings


@pytest.fixture
def translation_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(readings, "_SEGMENT_CACHE", OrderedDict())
    monkeypatch.setattr(readings, "get_reading_mode", lambda: "test_translation")
    monkeypatch.setattr(readings, "available_reading_providers", lambda: {"test_translation"})
    monkeypatch.setattr(readings, "_AUTOMATIC_READINGS_ENABLED", True)

    def generate(provider, text):
        calls.append(text)
        return [{"text": text, "reading": "かな"}]

    monkeypatch.setattr(readings, "generate_reading_segments", generate)
    monkeypatch.setattr(readings, "generate_reading_segments_batch",
                        lambda provider, texts: [generate(provider, text) for text in texts])
    return calls


@pytest.fixture
def immediate_reading_workers(monkeypatch):
    from concurrent.futures import Future

    class Executor:
        def submit(self, function):
            future = Future()
            try:
                future.set_result(function())
            except Exception as error:
                future.set_exception(error)
            return future

    monkeypatch.setattr("krcplus.furigana_widget._FURIGANA_EXECUTOR", Executor())


@pytest.mark.parametrize("timed", [False, True])
def test_karaoke_screen_skips_translation_in_both_rows(
        qapp, player, translation_provider, immediate_reading_workers, timed):
    from krcplus.models import LyricLine, LyricSegment

    lines = []
    player.screen.automatic_readings.setChecked(True)
    for index, original in enumerate(("Original", "Upcoming")):
        start = index * 10
        lines.append(LyricLine(
            start, original + "\nTranslation", [
                LyricSegment(start, original), LyricSegment(start, "\nTranslation", instant=True),
            ], end=start + 10, has_karaoke=timed, translation_lines=[1],
        ))
    player._set_lyrics(lines, 0)
    player.refresh()
    qapp.processEvents()
    assert set(translation_provider) == {"Original", "Upcoming"}
    for widget in (player.screen.current_lyric, player.screen.next_lyric):
        assert widget.translation_lines == [1]
        translated = [part for part in widget._prepare_segments() if part.get("translation")]
        assert "".join(part["text"] for part in translated) == "Translation"
        assert all(not part.get("reading") for part in translated)

