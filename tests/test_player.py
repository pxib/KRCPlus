import struct
import time
import wave

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QGraphicsBlurEffect, QGraphicsDropShadowEffect

from krcplus.models import LyricLine, LyricSegment


@pytest.fixture
def track(tmp_path):
    audio = tmp_path / "Song.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        handle.writeframes(bytes(8000 * 2 * 24))
    notes = b"\x00\x90\x45\x50\x60\x80\x45\x00\x00\xff\x2f\x00"
    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, 96)
    audio.with_suffix(".mid").write_bytes(header + b"MTrk" + struct.pack(">I", len(notes)) + notes)
    audio.with_suffix(".lrc").write_text("[00:00]Fallback\n", encoding="utf-8")
    audio.with_suffix(".lrcx").write_text(
        "[offset:250]\n[00:00]<00:00>{私|わたし}<00:01>の歌<00:05>\n"
        "[00:06]<00:06>Second<00:10>\n[00:11]Third\n", encoding="utf-8"
    )
    return audio


def test_audio_opens_with_sidecars_and_seeks(player, track):
    player.open_audio(track)
    screen = player.screen
    deadline = time.monotonic() + 3.0
    while not player.media_player.isSeekable() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert player.media_player.duration() == 24000
    assert player.media_player.isSeekable()
    assert screen.lyrics_status.text() == "Song.lrcx"
    assert screen.track_label.text() == "Song"
    assert screen.track_combo.count() == 1
    assert player.selected_midi_track["notes"][0]["pitch"] == 69
    screen.transpose_spin.setValue(2)
    assert player._transposed_notes(player.selected_midi_track["notes"])[0]["pitch"] == 71
    screen.offset_spin.setValue(0.5)
    assert player.preferences.value("midi_offsets")[str(track)] == 0.5
    screen.progress.sliderMoved.emit(2000)
    player.refresh()
    assert player.media_player.position() == 2000
    assert screen.progress.value() == 2000
    assert screen.current_lyric.lyric == "私の歌"
    assert screen.current_lyric.karaoke_position == pytest.approx(1.75)
    assert screen.pitch_lane.position_seconds == pytest.approx(2.0)
    assert any(part.get("reading") == "わたし" for part in screen.current_lyric._prepare_segments())

    player.open_audio(track)
    assert screen.offset_spin.value() == 0.5
    assert screen.transpose_spin.value() == 0


def test_bad_lyric_or_midi_import_keeps_the_current_one(player, track, tmp_path):
    player.open_audio(track)
    old_lines = player.lyric_lines
    old_track = player.selected_midi_track
    invalid = tmp_path / "empty.lrc"
    invalid.write_text("Untimed text", encoding="utf-8")
    with pytest.raises(ValueError, match="No timed lyrics"):
        player.open_lyrics(invalid)
    assert not player._load_midi_path(invalid, show_errors=False)
    assert player.lyric_lines is old_lines
    assert player.selected_midi_track is old_track


@pytest.fixture
def timed(player):
    lines = [
        LyricLine(start, f"Line {index}",
                  [LyricSegment(start, f"Line {index}", end=start + 10)],
                  end=start + 10, has_karaoke=True)
        for index, start in enumerate((0, 10, 20, 30))
    ]
    player._set_lyrics(lines, 0)
    return player


@pytest.mark.parametrize("offset", [4, -4, 0])
@pytest.mark.parametrize("lyric_time,indices", [
    (15.9, (0, 1)), (16.0, (2, 1)), (19.9, (2, 1)), (20.0, (2, 3)),
])
def test_pair_lookahead_uses_lyric_time_and_pitch_uses_media_time(timed, offset, lyric_time, indices):
    timed.lyrics_offset_seconds = offset
    timed._anchor_position(round((lyric_time + offset) * 1000))
    timed.refresh()
    assert timed._shown_lines == indices
    assert timed.screen.current_lyric.karaoke_position == pytest.approx(lyric_time)
    assert timed.screen.pitch_lane.position_seconds == pytest.approx(lyric_time + offset)


def test_lyric_focus_and_sweep_style(timed):
    screen = timed.screen
    timed.refresh()
    assert screen.current_lyric.is_current_line
    assert not screen.next_lyric.is_current_line
    for row in (screen.current_lyric, screen.next_lyric):
        assert isinstance(row.graphicsEffect(), QGraphicsBlurEffect)
        assert isinstance(row.parentWidget().graphicsEffect(), QGraphicsDropShadowEffect)
    timed._anchor_position(10000)
    timed.refresh()
    assert not screen.current_lyric.is_current_line
    assert screen.next_lyric.is_current_line
    screen.sweep_style.setCurrentIndex(screen.sweep_style.findData("soft"))
    assert screen.current_lyric.karaoke_sweep_style == "soft"
    assert screen.next_lyric.karaoke_sweep_style == "soft"


def test_seek_before_first_lyric_clears_both_rows(timed):
    timed.refresh()
    assert timed.screen.current_lyric.lyric == "Line 0"
    timed.lyrics_offset_seconds = 0.25
    timed.refresh()
    assert timed.screen.current_lyric.lyric == ""
    assert timed.screen.next_lyric.lyric == ""
    assert timed.screen.current_lyric.karaoke_position == -0.25
