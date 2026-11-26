"""Standalone audio playback, synchronized lyrics, and MIDI guide control."""

from bisect import bisect_left, bisect_right
import math
from pathlib import Path
import time

import numpy as np
from PySide6.QtCore import QObject, QSettings, QTimer, QUrl, Qt, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import QFileDialog, QMessageBox

from .artwork_background import extract_embedded_cover
from .furigana import detect_lyrics_collection_language, set_auto_language_context, set_reading_mode
from .loader import PLAYER_LYRIC_LINE_LIMIT, get_lyrics_generation_mode, load_lyrics
from .lrcx_parser import get_lrcx_offset
from .midi import MidiImportError, load_midi
from .pitch_detector import PitchDetector
from .settings import KARAOKE_DEFAULTS, apply_karaoke_settings


LYRIC_PAIR_LOOKAHEAD_RATIO = 0.40
AUDIO_FILTER = "Audio files (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus *.wma);;All files (*)"
LYRICS_FILTER = "Timed lyrics (*.lrcx *.lrc);;All files (*)"


class KaraokePlayer(QObject):
    def __init__(self, screen, preferences=None):
        super().__init__(screen)
        self.screen = screen
        self.preferences = QSettings() if preferences is None else preferences
        self.audio_output = QAudioOutput(self)
        self.media_player = QMediaPlayer(self)
        self.media_player.setAudioOutput(self.audio_output)
        self.audio_path = None
        self.lyric_lines = []
        self.lyric_times = []
        self.visible_indices = []
        self.lyrics_offset_seconds = 0.0
        self._shown_lines = (None, None)
        self._position_anchor_ms = 0
        self._position_anchor_monotonic = None

        self.current_midi = None
        self.selected_midi_track = None
        self.midi_assignments = self.preferences.value("midi_assignments", {}) or {}
        self.midi_offsets = self.preferences.value("midi_offsets", {}) or {}
        if not isinstance(self.midi_assignments, dict):
            self.midi_assignments = {}
        if not isinstance(self.midi_offsets, dict):
            self.midi_offsets = {}
        self.square_wave_enabled = self.preferences.value("midi_player_enabled", True, type=bool)
        self.play_other_instruments = self.preferences.value("play_other_instruments", False, type=bool)
        self._synth_stream = None
        self._synth_timer = None
        self._synth_status_last_update = 0.0
        self._synth_audio_state = None
        self._synth_callback_status = ""
        self._synth_callback_error = ""
        self._synth_cursor_seconds = None
        self._synth_callback_was_playing = False
        self._pitch_detector = None

        screen.midi_audio.setChecked(self.square_wave_enabled)
        screen.other_instruments.setChecked(self.play_other_instruments)
        screen.other_instruments.setEnabled(self.square_wave_enabled)
        screen.automatic_readings.setChecked(self.preferences.value("automatic_readings", False, type=bool))
        style = screen.sweep_style.findData(self.preferences.value("sweep_style", "classic"))
        screen.sweep_style.setCurrentIndex(max(0, style))
        self._apply_sweep_style()

        screen.open_audio_button.clicked.connect(self.choose_audio)
        screen.open_lyrics_button.clicked.connect(self.choose_lyrics)
        screen.play_button.clicked.connect(self.toggle_playback)
        screen.progress.sliderMoved.connect(self.media_player.setPosition)
        screen.progress.actionTriggered.connect(self._seek_action)
        screen.midi_button.clicked.connect(self.choose_midi_file)
        screen.track_combo.currentIndexChanged.connect(self.on_midi_track_selected)
        screen.track_combo.setEnabled(False)
        screen.offset_spin.valueChanged.connect(self.on_alignment_offset_changed)
        screen.transpose_spin.valueChanged.connect(self.on_transpose_changed)
        screen.mic_button.clicked.connect(self._toggle_microphone)
        screen.midi_audio.toggled.connect(self._set_midi_audio)
        screen.other_instruments.toggled.connect(self._set_other_instruments)
        screen.automatic_readings.toggled.connect(self._set_automatic_readings)
        screen.sweep_style.currentIndexChanged.connect(self._save_sweep_style)
        screen.play_button.setEnabled(False)
        screen.open_lyrics_button.setEnabled(False)
        screen.progress.setEnabled(False)
        self.media_player.positionChanged.connect(self._anchor_position)
        self.media_player.playbackStateChanged.connect(self._playback_state_changed)
        self.media_player.mediaStatusChanged.connect(self._media_status_changed)
        self.media_player.durationChanged.connect(self._duration_changed)
        self.media_player.seekableChanged.connect(screen.progress.setEnabled)
        self.media_player.errorOccurred.connect(self._media_error)
        screen.finished.connect(self.shutdown)

        self.frame_timer = QTimer(self)
        self.frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.frame_timer.setInterval(round(1000 / KARAOKE_DEFAULTS["sweep_fps"]))
        self.frame_timer.timeout.connect(self.refresh)
        self.frame_timer.start()
        self.refresh()

    def choose_audio(self):
        directory = str(self.audio_path.parent) if self.audio_path else ""
        path, _ = QFileDialog.getOpenFileName(self.screen, "Open audio", directory, AUDIO_FILTER)
        if path:
            try:
                self.open_audio(path)
            except OSError as error:
                QMessageBox.warning(self.screen, "Audio unavailable", str(error))

    def open_audio(self, path):
        path = Path(path).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        self.media_player.stop()
        self._stop_square_wave_stream()
        self.audio_path = path
        self.screen.track_label.setText(path.stem)
        artwork = extract_embedded_cover(path)
        if artwork:
            self.screen.background.set_artwork_bytes(artwork)
        else:
            self.screen.background.clear_artwork()
        self.screen.pitch_lane.set_position(0)
        self.screen.transpose_spin.setValue(0)
        self._set_lyrics([], 0.0)
        self.screen.lyrics_status.setText("No timed lyrics loaded")
        for suffix in (".lrcx", ".lrc"):
            sidecar = path.with_suffix(suffix)
            if sidecar.is_file():
                self._load_lyrics_or_warn(sidecar, dialog=False)
                break
        self.load_midi_for_track(path)
        self.screen.open_lyrics_button.setEnabled(True)
        self.screen.play_button.setEnabled(True)
        self.media_player.setSource(QUrl.fromLocalFile(str(path)))
        self._anchor_position(0)
        self.refresh()

    def choose_lyrics(self):
        directory = str(self.audio_path.parent) if self.audio_path else ""
        path, _ = QFileDialog.getOpenFileName(self.screen, "Open timed lyrics", directory, LYRICS_FILTER)
        if path:
            self._load_lyrics_or_warn(path)

    def _load_lyrics_or_warn(self, path, *, dialog=True):
        try:
            self.open_lyrics(path)
        except (OSError, ValueError) as error:
            if dialog:
                QMessageBox.warning(self.screen, "Lyrics unavailable", str(error))
            else:
                self.screen.lyrics_status.setText(f"Lyrics unavailable: {error}")

    def open_lyrics(self, path):
        path = Path(path)
        if path.suffix.lower() not in {".lrc", ".lrcx"}:
            raise ValueError("Choose an LRC or LRCX timed-lyrics file.")
        lines = load_lyrics(path)
        if len(lines) > PLAYER_LYRIC_LINE_LIMIT:
            raise ValueError(f"The timed-lyrics file exceeds {PLAYER_LYRIC_LINE_LIMIT:,} rows.")
        if not any(line.text.strip() for line in lines):
            raise ValueError("No timed lyrics were found.")
        set_reading_mode(get_lyrics_generation_mode(path) or "auto")
        self._set_lyrics(lines, get_lrcx_offset(path) / 1000.0)
        self.screen.lyrics_status.setText(path.name)
        self.refresh()

    def _set_lyrics(self, lines, offset):
        self.lyric_lines = lines
        self.lyric_times = [line.start for line in lines]
        self.visible_indices = [index for index, line in enumerate(lines) if line.text.strip()]
        self.lyrics_offset_seconds = offset
        set_auto_language_context(detect_lyrics_collection_language(line.text for line in lines))
        self._invalidate_lyric_rows()

    def toggle_playback(self):
        if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media_player.pause()
        elif self.audio_path:
            self.media_player.play()

    def _seek_action(self, _action):
        if not self.screen.progress.isSliderDown():
            self.media_player.setPosition(self.screen.progress.sliderPosition())

    def _duration_changed(self, duration):
        self.screen.progress.setRange(0, max(0, duration))
        self.screen._update_time_label()

    def _playback_state_changed(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.screen.play_button.setText("Pause" if playing else "Play")
        self._anchor_position(self.media_player.position())

    def _media_status_changed(self, _status):
        self._anchor_position(self.media_player.position())

    def _media_error(self, _error, message):
        self.screen.lyrics_status.setText(message or "This audio file could not be played.")

    def _anchor_position(self, position_ms):
        self._position_anchor_ms = position_ms
        playing = self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        stalled = self.media_player.mediaStatus() == QMediaPlayer.MediaStatus.StalledMedia
        self._position_anchor_monotonic = time.monotonic() if playing and not stalled else None

    def _estimated_position_seconds(self):
        position_ms = self._position_anchor_ms
        if self._position_anchor_monotonic is not None:
            position_ms += 1000.0 * (time.monotonic() - self._position_anchor_monotonic)
        duration = self.media_player.duration()
        return min(position_ms, duration) / 1000.0 if duration > 0 else position_ms / 1000.0

    def refresh(self):
        if not self.screen.progress.isSliderDown():
            self.screen.progress.setValue(self.media_player.position())
        position = self._estimated_position_seconds()
        self.screen.pitch_lane.set_position(position * 1000.0)
        lyric_position = position - self.lyrics_offset_seconds
        self._refresh_lyric_rows(lyric_position)
        for row in (self.screen.current_lyric, self.screen.next_lyric):
            row.set_karaoke_position(lyric_position)

    def _refresh_lyric_rows(self, position):
        current = bisect_right(self.lyric_times, position) - 1
        if current >= 0:
            current = bisect_left(self.lyric_times, self.lyric_times[current])
        indices = self._lyric_pair(current, position)
        rows = (self.screen.current_lyric, self.screen.next_lyric)
        for row, index, previous in zip(rows, indices, self._shown_lines):
            if index != previous:
                self._set_lyric_row(row, index)
                if index is not None:
                    row.start_line_entrance()
            row.set_current_line(index is not None and index == current)
        self.screen.next_lyric.setVisible(indices[1] is not None)
        self._shown_lines = indices

    def _lyric_pair(self, current, position):
        visible = self.visible_indices
        if current < 0 or not visible:
            return None, None
        slot = min(bisect_left(visible, current), len(visible) - 1)
        page = slot - slot % 2
        if page > 0:
            start = self.lyric_times[visible[page]]
            previous_bottom = self.lyric_times[visible[page - 1]]
            if position < start - (start - previous_bottom) * LYRIC_PAIR_LOOKAHEAD_RATIO:
                page -= 2
        top = visible[page]
        bottom = visible[page + 1] if page + 1 < len(visible) else None
        next_page = page + 2
        if bottom is not None and next_page < len(visible):
            upcoming = visible[next_page]
            start = self.lyric_times[upcoming]
            lookahead = (start - self.lyric_times[bottom]) * LYRIC_PAIR_LOOKAHEAD_RATIO
            if start - lookahead <= position < start:
                top = upcoming
        return top, bottom

    def _set_lyric_row(self, row, index):
        if index is None:
            if row.lyric:
                row.set_lyric("", generate=False)
            return
        line = self.lyric_lines[index]
        options = {
            "style_spans": line.style_spans,
            "is_blockquote": line.is_blockquote,
            "translation_lines": line.translation_lines,
            "generate": self.screen.automatic_readings.isChecked(),
        }
        if line.segments:
            row.set_segments(line.text, line.segments, line.end, **options)
        else:
            row.set_lyric(line.text, **options)

    def _set_midi_audio(self, enabled):
        self.preferences.setValue("midi_player_enabled", enabled)
        self.screen.other_instruments.setEnabled(enabled)
        self._square_wave_toggled(enabled)

    def _set_other_instruments(self, enabled):
        self.play_other_instruments = enabled
        self.preferences.setValue("play_other_instruments", enabled)
        self._update_square_wave_state()

    def _set_automatic_readings(self, enabled):
        self.preferences.setValue("automatic_readings", enabled)
        self._invalidate_lyric_rows()
        self.refresh()

    def _invalidate_lyric_rows(self):
        # A different lyric document can have the same visible row indices.
        self._shown_lines = (-1, -1)

    def _save_sweep_style(self):
        self.preferences.setValue("sweep_style", self.screen.sweep_style.currentData())
        self._apply_sweep_style()

    def _apply_sweep_style(self):
        values = dict(KARAOKE_DEFAULTS, sweep_style=self.screen.sweep_style.currentData())
        for row in (self.screen.current_lyric, self.screen.next_lyric):
            apply_karaoke_settings(row, values)

    def shutdown(self):
        self.frame_timer.stop()
        self.media_player.stop()
        self._stop_square_wave_stream()
        self._stop_microphone()
        for detector in self.findChildren(PitchDetector):
            detector.stop()
            detector.wait()

    def load_midi_for_track(self, audio_path):
        """Use an assigned MIDI file, or a same-name sidecar when present."""
        audio_path = Path(audio_path)
        self._apply_saved_alignment_offset(audio_path)
        candidates = (
            self.midi_assignments.get(str(audio_path)),
            audio_path.with_suffix(".mid"),
            audio_path.with_suffix(".midi"),
        )
        for path in candidates:
            if path and Path(path).is_file() and self._load_midi_path(path, show_errors=False):
                return
        self.current_midi = None
        self._populate_midi_tracks()

    def _apply_saved_alignment_offset(self, audio_path):
        if self.screen is None:
            return
        try:
            offset = float(self.midi_offsets.get(str(audio_path), 0.0))
        except (TypeError, ValueError):
            offset = 0.0
        self.screen.offset_spin.blockSignals(True)
        self.screen.offset_spin.setValue(offset)
        self.screen.offset_spin.blockSignals(False)
        self.screen.pitch_lane.set_offset(offset)

    def choose_midi_file(self):
        audio_path = self.audio_path
        directory = str(Path(audio_path).parent) if audio_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self.screen,
            "Import Karaoke MIDI",
            directory,
            "MIDI Files (*.mid *.midi);;All Files (*)",
        )
        if not path:
            return
        if self._load_midi_path(path, show_errors=True) and audio_path:
            self.midi_assignments[str(audio_path)] = path
            self.preferences.setValue("midi_assignments", self.midi_assignments)

    def _load_midi_path(self, path, *, show_errors):
        try:
            midi = load_midi(path)
        except MidiImportError as error:
            if show_errors:
                QMessageBox.warning(self.screen, "MIDI Import", str(error))
            return False
        self.current_midi = midi
        self._populate_midi_tracks()
        return True

    def _populate_midi_tracks(self):
        screen = self.screen
        if screen is None:
            return
        combo = screen.track_combo
        combo.blockSignals(True)
        self.selected_midi_track = None
        combo.clear()
        midi = self.current_midi
        if midi is not None:
            for index, track in enumerate(midi["tracks"]):
                if track["notes"]:
                    combo.addItem(track["name"], index)
        if combo.count():
            combo.setCurrentIndex(0)
        combo.setEnabled(combo.count() > 0)
        screen.midi_button.setText(
            "Change MIDI…" if midi is not None else "Import MIDI…"
        )
        combo.blockSignals(False)
        if combo.count():
            self.on_midi_track_selected(combo.currentIndex())
        else:
            screen.pitch_lane.set_notes([])
            self._stop_square_wave_stream()
            self._set_square_wave_status("", force=True)

    def on_midi_track_selected(self, combo_index):
        if self.screen is None or self.current_midi is None or combo_index < 0:
            return
        track_index = self.screen.track_combo.itemData(combo_index)
        track = self.current_midi["tracks"][track_index]
        self.selected_midi_track = track
        self.screen.pitch_lane.set_notes(self._transposed_notes(track["notes"]))
        if self.square_wave_enabled and self._synth_stream is None:
            self._start_square_wave_stream()
        elif not self.square_wave_enabled:
            self._set_square_wave_status("MIDI guide audio off", force=True)
        self._update_square_wave_state()

    def _transpose_semitones(self):
        return int(self.screen.transpose_spin.value()) if self.screen is not None else 0

    def _transposed_notes(self, notes):
        semitones = self._transpose_semitones()
        return [dict(note, pitch=max(0, min(127, int(note["pitch"]) + semitones)))
                for note in notes]

    def on_transpose_changed(self, _semitones):
        if self.screen is not None and self.selected_midi_track is not None:
            self.screen.pitch_lane.set_notes(
                self._transposed_notes(self.selected_midi_track["notes"])
            )

    def on_alignment_offset_changed(self, offset_seconds):
        if self.screen is not None:
            self.screen.pitch_lane.set_offset(offset_seconds)
        audio_path = self.audio_path
        if audio_path:
            self.midi_offsets[str(audio_path)] = float(offset_seconds)
            self.preferences.setValue("midi_offsets", self.midi_offsets)

    def _square_wave_toggled(self, enabled):
        self.square_wave_enabled = bool(enabled)
        if not enabled:
            self._stop_square_wave_stream()
            if self.selected_midi_track is not None:
                self._set_square_wave_status("MIDI guide audio off", force=True)
        else:
            self._start_square_wave_stream()

    def _start_square_wave_stream(self):
        self._stop_square_wave_stream()
        if not self.square_wave_enabled or self.selected_midi_track is None:
            return
        try:
            import sounddevice as sd

            target_name = self.audio_output.device().description().casefold()
            output_device = None
            for index, device in enumerate(sd.query_devices()):
                if (device.get("max_output_channels", 0) >= 2
                        and target_name in str(device.get("name", "")).casefold()):
                    output_device = index
                    break
            self._synth_audio_state = None
            self._synth_callback_status = ""
            self._synth_callback_error = ""
            self._synth_cursor_seconds = None
            self._synth_callback_was_playing = False
            self._synth_stream = sd.OutputStream(
                device=output_device,
                samplerate=48000,
                channels=2,
                dtype="float32",
                callback=self._square_wave_audio_callback,
                blocksize=0,
            )
            self._synth_stream.start()
            self._synth_timer = QTimer(self.screen)
            self._synth_timer.setInterval(15)
            self._synth_timer.timeout.connect(self._update_square_wave_state)
            self._synth_timer.start()
            self._set_square_wave_status("MIDI guide audio on", force=True)
        except Exception as error:
            self._stop_square_wave_stream()
            self._set_square_wave_status(f"MIDI Player failed: {error}", force=True)

    def _stop_square_wave_stream(self):
        if self._synth_timer is not None:
            self._synth_timer.stop()
            self._synth_timer.deleteLater()
            self._synth_timer = None
        self._synth_audio_state = None
        stream = self._synth_stream
        self._synth_stream = None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        self._synth_cursor_seconds = None
        self._synth_callback_was_playing = False

    def _update_square_wave_state(self):
        if self._synth_stream is None or self.screen is None:
            return
        is_playing = (
            self.selected_midi_track is not None
            and self.square_wave_enabled
            and self.media_player.playbackState()
            == QMediaPlayer.PlaybackState.PlayingState
        )
        position = self._estimated_position_seconds() if is_playing else 0.0
        playback_tracks = [self.selected_midi_track]
        if self.play_other_instruments and self.current_midi is not None:
            playback_tracks.extend(
                track for track in self.current_midi["tracks"]
                if track is not self.selected_midi_track
            )
        playback_notes = (
            note
            for track in playback_tracks
            for note in (track or {}).get("notes", [])
        )
        all_notes = tuple(
            (
                float(note["start"]),
                float(note["end"]),
                max(0, min(127, int(note["pitch"]) + self._transpose_semitones())),
                max(0.0, min(1.0, float(note.get("velocity", 90)) / 127.0)),
            )
            for note in playback_notes
        )
        offset = self.screen.offset_spin.value()
        notes = tuple(
            note for note in all_notes
            if note[1] + offset > position - 0.1
            and note[0] + offset < position + 0.5
        )
        rate = 1.0
        anchor = time.monotonic()
        # Atomic pointer replacement: the PortAudio callback reads only this
        # immutable snapshot and never reaches into Qt or widgets.
        self._synth_audio_state = (is_playing, position, anchor, rate, offset, notes)

        if self._synth_callback_error or self._synth_callback_status:
            self._set_square_wave_status(self._synth_callback_error or self._synth_callback_status)

    def _square_wave_audio_callback(self, outdata, frames, time_info, status):
        outdata.fill(0.0)
        if status:
            self._synth_callback_status = str(status)
        state = self._synth_audio_state
        if state is None:
            return
        playing, position, anchor, rate, offset, notes = state
        if not playing or not notes:
            self._synth_cursor_seconds = None
            self._synth_callback_was_playing = False
            return
        try:
            sample_rate = float(self._synth_stream.samplerate)
            requested_start = position + max(0.0, time.monotonic() - anchor) * rate
            if (self._synth_cursor_seconds is None
                    or not self._synth_callback_was_playing
                    or abs(requested_start - self._synth_cursor_seconds) > 0.2):
                # Initial playback, resume, or a deliberate seek.
                start = requested_start
            else:
                # Ignore small Qt clock re-anchors so they cannot jump the
                # oscillator phase and create an audible click.
                start = self._synth_cursor_seconds
            self._synth_callback_was_playing = True
            times = start + np.arange(frames, dtype=np.float64) * (rate / sample_rate)
            mono = np.zeros(frames, dtype=np.float32)
            fade_seconds = 0.005
            for note_start_raw, note_end_raw, pitch, velocity in notes:
                note_start = note_start_raw + offset
                note_end = note_end_raw + offset
                active = (times >= note_start) & (times < note_end)
                if not np.any(active):
                    continue
                frequency = 440.0 * (2.0 ** ((pitch - 69) / 12.0))
                phase = 2.0 * np.pi * frequency * (times[active] - note_start)
                harmonics = [
                    harmonic for harmonic in (1, 3, 5)
                    if harmonic * frequency < sample_rate * 0.5
                ]
                if not harmonics:
                    continue
                weights = [1.0 / harmonic for harmonic in harmonics]
                weight_sum = sum(weights)
                tone = sum(
                    np.sin(phase * harmonic) * weight
                    for harmonic, weight in zip(harmonics, weights)
                ) / weight_sum
                elapsed = times[active] - note_start
                remaining = note_end - times[active]
                envelope = np.clip(
                    np.minimum(elapsed / fade_seconds, remaining / fade_seconds),
                    0.0,
                    1.0,
                )
                mono[active] += tone.astype(np.float32) * envelope.astype(np.float32) * (0.18 * velocity)
            outdata[:] = np.clip(mono[:, None], -1.0, 1.0)
            self._synth_cursor_seconds = start + frames * rate / sample_rate
        except Exception as error:
            self._synth_callback_error = f"callback error: {error}"
            outdata.fill(0.0)

    def _set_square_wave_status(self, message, *, force=False):
        now = time.monotonic()
        if self.screen is not None and (force or now - self._synth_status_last_update >= 0.25):
            self.screen.square_wave_status.setText(str(message))
            self._synth_status_last_update = now

    def _toggle_microphone(self):
        if self._pitch_detector is not None:
            self._stop_microphone()
            return
        if self.screen is None:
            return
        detector = PitchDetector(self)
        self._pitch_detector = detector
        detector.pitch_detected.connect(self._on_microphone_pitch)
        detector.status_changed.connect(self._on_microphone_status)
        detector.finished.connect(self._microphone_finished)
        detector.finished.connect(detector.deleteLater)
        self.screen.mic_button.setChecked(True)
        self.screen.mic_button.setText("Stop mic")
        self.screen.mic_pitch_status.setText("Starting mic...")
        self.screen.mic_pitch_status.setToolTip("")
        detector.start()

    def _reset_microphone_ui(self):
        if self.screen is not None:
            self.screen.mic_button.setChecked(False)
            self.screen.mic_button.setText("Start mic")
            self.screen.pitch_lane.set_live_pitch(None, 0.0)

    def _stop_microphone(self):
        detector = self._pitch_detector
        self._pitch_detector = None
        if detector is not None:
            detector.stop()
            detector.wait(1000)
        self._reset_microphone_ui()
        if self.screen is not None:
            self.screen.mic_pitch_status.setText("Mic off")
            self.screen.mic_pitch_status.setToolTip("")

    @Slot(str)
    def _on_microphone_status(self, message):
        if self.screen is None or self.sender() is not self._pitch_detector:
            return
        self.screen.mic_pitch_status.setToolTip(message)
        self.screen.mic_pitch_status.setText(
            "Listening..." if message == "Listening..." else "Mic unavailable"
        )

    @Slot()
    def _microphone_finished(self):
        if self.sender() is not self._pitch_detector:
            return
        self._pitch_detector = None
        self._reset_microphone_ui()
        if self.screen is not None and self.screen.mic_pitch_status.text() != "Mic unavailable":
            self.screen.mic_pitch_status.setText("Mic off")

    @Slot(float, float)
    def _on_microphone_pitch(self, frequency, confidence):
        if self.screen is None or self.sender() is not self._pitch_detector:
            return
        lane = self.screen.pitch_lane
        if (not math.isfinite(frequency) or not math.isfinite(confidence)
                or frequency <= 0 or confidence < PitchDetector.MIN_CONFIDENCE):
            self.screen.mic_pitch_status.setText("Listening...")
            lane.set_live_pitch(None, 0.0)
            return
        pitch = 69.0 + 12.0 * math.log2(frequency / 440.0)
        note = round(pitch)
        cents = round((pitch - note) * 100)
        names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
        self.screen.mic_pitch_status.setText(f"{names[note % 12]}{note // 12 - 1}  {cents:+d} ct")
        if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            lane.set_live_pitch(pitch, confidence, self._estimated_position_seconds())
        else:
            lane.set_live_pitch(None, 0.0)

