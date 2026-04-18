#!/usr/bin/env python3
"""
scrcpy MPRIS media controller.

Exposes Android media playback over MPRIS so desktop notification panels
(swaync, dunst, waybar, …) can display and control it.
Requires an ADB-connected Android device.
"""

import subprocess
import re
from pathlib import Path
from threading import Thread, Event

import click
from mpris_server.adapters import PlayState, PlayerAdapter
from mpris_server.events import EventAdapter
from mpris_server.server import Server

from album_art import art_cache_key, request_art
from player import CustomPlayer


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

class AppState:
    def __init__(self) -> None:
        self.title: str = "No Media"
        self.album: str = ""
        self.artist: list[str] = []
        self.art_url: str = ""
        self.shuffle: bool = False
        self.loop_status: str = "None"
        self.playbackState: PlayState = PlayState.PLAYING
        self.media_adapter = None
        self.oldDevice: bool = False
        self._art_key: str = ""

    @staticmethod
    def _denull(val: str) -> str:
        """Coerce the literal string 'null' (sent by some Android versions) to ''."""
        return "" if val == "null" else val

    def update(self, emit: bool = True) -> None:
        media_session = subprocess.run(
            ["adb", "shell", "dumpsys", "media_session"],
            capture_output=True, text=True,
        ).stdout

        try:
            desc = media_session.split("description=")[1].split("\n")[0]
            assert desc != "null"
            desc_list = desc.split(", ")
            assert desc_list != ["null", "null", "null"]

            self.title  = self._denull(desc_list[0]) or "Unknown"
            self.artist = [a for a in desc_list[1:-1] if a and a != "null"]
            self.album  = self._denull(desc_list[-1])

            # -- playback state -------------------------------------------
            pb     = media_session.split("state=PlaybackState {")[1].split("}")[0].split(", ")
            status = pb[0]

            if len(status) <= 8:
                self.oldDevice = True
                if status in ("state=0", "state=1", "state=7", "state=8"):
                    self.playbackState = PlayState.STOPPED
                elif status in ("state=2", "state=6"):
                    self.playbackState = PlayState.PAUSED
                elif status in ("state=3", "state=4", "state=5",
                                "state=9", "state=10", "state=11"):
                    self.playbackState = PlayState.PLAYING
                else:
                    print(f"[mediactl] unknown playback status: {pb}")
            else:
                self.oldDevice = False
                if status in ("state=NONE(0)", "state=STOPPED(1)",
                              "state=ERROR(7)", "state=CONNECTING(8)"):
                    self.playbackState = PlayState.STOPPED
                elif status in ("state=PAUSED(2)", "state=BUFFERING(6)"):
                    self.playbackState = PlayState.PAUSED
                elif status in ("state=PLAYING(3)", "state=FAST_FORWARDING(4)",
                                "state=REWINDING(5)", "state=SKIPPING_TO_PREVIOUS(9)",
                                "state=SKIPPING_TO_NEXT(10)", "state=SKIPPING_TO_QUEUE_ITEM(11)"):
                    self.playbackState = PlayState.PLAYING
                else:
                    print(f"[mediactl] unknown playback status: {pb}")

            # -- shuffle / repeat -----------------------------------------
            sm = re.search(r"shuffle[_ ]mode\s*[=:]\s*(\d+)", media_session, re.I)
            if sm:
                self.shuffle = int(sm.group(1)) >= 2

            rm = re.search(r"repeat[_ ]mode\s*[=:]\s*(\d+)", media_session, re.I)
            if rm:
                r = int(rm.group(1))
                self.loop_status = "Track" if r == 2 else ("Playlist" if r >= 3 else "None")

            # -- album art (background fetch, cached by track identity) ----
            key = art_cache_key(self.title, self.artist, self.album)
            if key != self._art_key:
                self._art_key = key
                self.art_url  = request_art(key, self.title, self._on_art_ready)

        except (IndexError, AssertionError):
            self.title         = "No Media"
            self.artist        = []
            self.album         = ""
            self.art_url       = ""
            self.playbackState = PlayState.PLAYING

        if self.media_adapter and emit:
            EventAdapter.emit_changes(
                self.media_adapter.player,
                ["Metadata", "PlaybackStatus", "Shuffle", "LoopStatus"],
            )

    def _on_art_ready(self, key: str, uri: str) -> None:
        if uri and self._art_key == key:
            self.art_url = uri
            if self.media_adapter:
                EventAdapter.emit_changes(self.media_adapter.player, ["Metadata"])

    def dispatch_media_key(self, key: str) -> subprocess.CompletedProcess:
        if self.oldDevice:
            return subprocess.run(["adb", "shell", "media", "dispatch", key])
        return subprocess.run(["adb", "shell", "cmd", "media_session", "dispatch", key])


# ---------------------------------------------------------------------------
# Update thread
# ---------------------------------------------------------------------------

class UpdateThread(Thread):
    def __init__(self, app: AppState, update_freq: float, exit_event: Event) -> None:
        super().__init__(daemon=True, name="update-thread")
        self.app         = app
        self.update_freq = update_freq
        self.exit        = exit_event

    def run(self) -> None:
        while not self.exit.wait(self.update_freq):
            self.app.update()


# ---------------------------------------------------------------------------
# MPRIS adapter
# ---------------------------------------------------------------------------

class MediaAdapter(PlayerAdapter):
    def __init__(self, app: AppState) -> None:
        super().__init__()
        self.app = app

    def _cmd(self, label: str, key: str) -> None:
        print(f"[mediactl] {label}")
        self.app.dispatch_media_key(key)
        self.app.update()

    # -- commands -----------------------------------------------------------

    def next(self)     -> None: self._cmd("next",   "next")
    def previous(self) -> None: self._cmd("prev",   "previous")
    def pause(self)    -> None: self._cmd("pause",  "pause")
    def resume(self)   -> None: self._cmd("resume", "play")
    def stop(self)     -> None: self._cmd("stop",   "stop")
    def play(self)     -> None: self._cmd("play",   "play")

    # -- state --------------------------------------------------------------

    def get_playstate(self)   -> PlayState: return self.app.playbackState
    def get_shuffle(self)     -> bool:      return self.app.shuffle
    def get_loop_status(self) -> str:       return self.app.loop_status
    def get_art_url(self, track) -> str:    return self.app.art_url

    # -- capabilities -------------------------------------------------------

    def can_go_next(self)     -> bool: return True
    def can_go_previous(self) -> bool: return True
    def can_play(self)        -> bool: return True
    def can_pause(self)       -> bool: return True
    def can_seek(self)        -> bool: return False
    def can_control(self)     -> bool: return True
    def can_quit(self)        -> bool: return False
    def can_raise(self)       -> bool: return False
    def has_tracklist(self)   -> bool: return False
    def can_fullscreen(self)  -> bool: return False
    def get_fullscreen(self)  -> None: return None

    # -- info ---------------------------------------------------------------

    def get_stream_title(self)  -> str:       return self.app.title
    def get_desktop_entry(self) -> str:       return "scrcpy"
    def get_mime_types(self)    -> list[str]: return ["audio/mpeg", "application/ogg", "video/mpeg"]
    def get_uri_schemes(self)   -> list[str]: return ["file"]

    def metadata(self) -> dict:
        meta: dict = {
            "mpris:trackid": "/org/mpris/MediaPlayer2/scrcpy",
            "xesam:title":   self.app.title,
            "xesam:artist":  self.app.artist,
        }
        if self.app.album:
            meta["xesam:album"] = self.app.album
        if self.app.art_url:
            meta["mpris:artUrl"] = self.app.art_url
        return meta


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--player-name",
    default="scrcpy",
    show_default=True,
    help="MPRIS player name exposed on D-Bus.",
)
@click.option(
    "--update-freq",
    default=1.0,
    show_default=True,
    type=float,
    help="How often (in seconds) to poll ADB for media state.",
)
@click.option(
    "--detach",
    is_flag=True,
    default=False,
    help="Do not start or manage scrcpy. Lets you handle the scrcpy lifecycle yourself.",
)
def cli(
    player_name: str,
    update_freq: float,
    detach: bool,
) -> None:
    """scrcpy MPRIS media controller.

    Exposes Android media playback over MPRIS so desktop notification panels
    (swaync, dunst, waybar, ...) can display and control it.
    Requires an ADB-connected Android device.

    By default this also starts `scrcpy --no-window --no-video` and tears it
    down on exit. Pass --detach to manage scrcpy yourself.
    """
    # -- optionally launch scrcpy -----------------------------------------
    scrcpy_proc: subprocess.Popen | None = None
    if not detach:
        try:
            scrcpy_proc = subprocess.Popen(
                ["scrcpy", "--no-window", "--no-video"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            def _drain(proc: subprocess.Popen) -> None:
                for raw in proc.stdout:
                    print(f"[scrcpy] {raw.decode(errors='replace').rstrip()}")

            Thread(target=_drain, args=(scrcpy_proc,), daemon=True).start()
            print("[mediactl] scrcpy started")
        except FileNotFoundError:
            print("[mediactl] scrcpy not found in PATH — continuing without it")

    # -- MPRIS setup ------------------------------------------------------
    app = AppState()
    app.update(emit=False)

    exit_event = Event()
    update_thread = UpdateThread(app, update_freq, exit_event)
    update_thread.start()

    media_adapter = MediaAdapter(app)
    mpris         = Server(name=player_name, adapter=media_adapter)
    mpris.player  = CustomPlayer(name=player_name, adapter=media_adapter)
    mpris.interfaces = mpris.root, mpris.player
    app.media_adapter = mpris

    try:
        mpris.loop()
    except KeyboardInterrupt:
        pass
    except RuntimeError:
        print(f"[mediactl] {player_name} media controller is already running!")
    finally:
        print("[mediactl] quitting...")
        exit_event.set()
        update_thread.join()
        if scrcpy_proc is not None:
            scrcpy_proc.terminate()
            try:
                scrcpy_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                scrcpy_proc.kill()


if __name__ == "__main__":
    cli()
