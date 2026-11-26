import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    from krcplus.gui_garbage_collector import install_gui_garbage_collector

    app = QApplication.instance() or QApplication([])
    install_gui_garbage_collector(app)
    return app


@pytest.fixture
def player(qapp, tmp_path):
    from PySide6.QtCore import QCoreApplication, QEvent, QSettings
    from krcplus.player import KaraokePlayer
    from krcplus.screen import KaraokeScreen

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("midi_player_enabled", False)
    screen = KaraokeScreen()
    player = KaraokePlayer(screen, settings)
    screen.show()
    qapp.processEvents()
    yield player
    screen.close()
    screen.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
