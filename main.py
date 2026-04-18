import re
import subprocess
import tempfile
import threading
from pathlib import Path
from threading import Thread, Event

import click
import pydbus
from mpris_server.adapters import PlayState, PlayerAdapter
from mpris_server.events import EventAdapter
from mpris_server.server import Server

from player import CustomPlayer


# ---------------------------------------------------------------------------
# Album-art helpers
# ---------------------------------------------------------------------------

_TMPDIR = Path(tempfile.gettempdir())
_ART_CACHE: dict[str, str] = {}         # art_key → file:// URI  (or "" for known miss)
_ART_CACHE_LOCK = threading.Lock()
_ART_IN_FLIGHT: set[str] = set()        # art_keys whose fetch is in progress


def _art_cache_key(title: str, artist: list[str], album: str) -> str:
    return f"{title}\x00{album}\x00{''.join(sorted(artist))}"


def _fetch_art_from_device(title: str) -> str:
    """
    Pull album art from the device's MediaStore via ADB.
    Returns a file:// URI on success, '' on failure (streaming track,
    art not found, ADB error, etc.).
    """
    try:
        safe = title.replace("'", "''")
        q = subprocess.run(
            [
                "adb", "shell", "content", "query",
                "--uri", "content://media/external/audio/media",
                "--projection", "album_id",
                "--where", f"title='{safe}'",
            ],
            capture_output=True, text=True, timeout=5,
        )
        m = re.search(r"album_id=(\d+)", q.stdout)
        if not m:
            return ""
        album_id = m.group(1)

        path = _TMPDIR / f"scrcpy_art_{album_id}.jpg"
        if not path.exists():
            pull = subprocess.run(
                [
                    "adb", "exec-out", "content", "read",
                    "--uri", f"content://media/external/audio/albumart/{album_id}",
                ],
                capture_output=True, timeout=8,
            )
            # Validate JPEG magic bytes (FF D8 FF); errors come back as ASCII text
            if not pull.stdout or pull.stdout[:3] != b"\xff\xd8\xff":
                return ""
            path.write_bytes(pull.stdout)

        return f"file://{path}"
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

class AppState:
    def __init__(self) -> None:
        self.title: str = "No Media"
        self.album: str = ""
        self.artist: list[str] = []
        self.art_url: str = ""          # "" means no art; populated by background fetch
        self.shuffle: bool = False
        self.loop_status: str = "None"  # "None" | "Track" | "Playlist"
        self.playbackState: PlayState = PlayState.PLAYING
        self.media_adapter = None
        self.oldDevice: bool = False
        self._art_key: str = ""         # identifies which track the cached art is for

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
            assert desc_list != ["null", "null", "null"]  # media is buffering

            self.title  = self._denull(desc_list[0]) or "Unknown"
            self.artist = [a for a in desc_list[1:-1] if a and a != "null"]
            self.album  = self._denull(desc_list[-1])

            # -- playback state ------------------------------------------
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

            # -- shuffle / repeat (best-effort; not all apps expose these) ----
            sm = re.search(r"shuffle[_ ]mode\s*[=:]\s*(\d+)", media_session, re.I)
            if sm:
                # 0=invalid  1=none  2=all  3=group  → active when ≥ 2
                self.shuffle = int(sm.group(1)) >= 2

            rm = re.search(r"repeat[_ ]mode\s*[=:]\s*(\d+)", media_session, re.I)
            if rm:
                r = int(rm.group(1))
                self.loop_status = "Track" if r == 2 else ("Playlist" if r >= 3 else "None")

            # -- album art (background fetch, cached by album_id) -------------
            key = _art_cache_key(self.title, self.artist, self.album)
            if key != self._art_key:
                self._art_key = key
                self.art_url  = ""
                with _ART_CACHE_LOCK:
                    if key in _ART_CACHE:
                        self.art_url = _ART_CACHE[key]
                    elif key not in _ART_IN_FLIGHT:
                        _ART_IN_FLIGHT.add(key)
                        Thread(target=self._art_worker, args=(key, self.title),
                               daemon=True).start()

        except (IndexError, AssertionError):
            self.title        = "No Media"
            self.artist       = []
            self.album        = ""
            self.art_url      = ""
            self.playbackState = PlayState.PLAYING

        if self.media_adapter and emit:
            EventAdapter.emit_changes(
                self.media_adapter.player,
                ["Metadata", "PlaybackStatus", "Shuffle", "LoopStatus"],
            )

    def _art_worker(self, key: str, title: str) -> None:
        uri = _fetch_art_from_device(title)
        with _ART_CACHE_LOCK:
            _ART_CACHE[key] = uri
            _ART_IN_FLIGHT.discard(key)
        if uri and self._art_key == key:    # still the same track
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
    def __init__(
        self,
        app: AppState,
        update_freq: float,
        exit_event: Event,
        kde_watcher: "KdeConnectWatcher | None" = None,
    ) -> None:
        super().__init__(daemon=True, name="update-thread")
        self.app         = app
        self.update_freq = update_freq
        self.exit        = exit_event
        self.kde_watcher = kde_watcher

    def run(self) -> None:
        while not self.exit.wait(self.update_freq):
            # When KDE Connect is active we keep polling ADB (so state is fresh the
            # moment we take over) but suppress PropertiesChanged signals so MPRIS
            # clients naturally prefer the KDE Connect entry.
            emit = self.kde_watcher is None or not self.kde_watcher.active
            self.app.update(emit=emit)


# ---------------------------------------------------------------------------
# KDE Connect watcher
# ---------------------------------------------------------------------------

class KdeConnectWatcher:
    """
    Polls the D-Bus session bus every `poll_interval` seconds for a KDE Connect
    MPRIS player (org.mpris.MediaPlayer2.kdeconnect.*).

    While one is found and responsive:
      - Our PropertiesChanged signals are suppressed (UpdateThread.emit=False).
      - Commands sent to our MPRIS entry are logged but not forwarded to ADB,
        so the two players don't accidentally double-dispatch.

    The moment KDE Connect disappears or stops responding we resume emitting
    events and executing commands normally, without any restart required.

    Disable entirely with --no-kde.
    """

    _PREFIX = "org.mpris.MediaPlayer2.kdeconnect"

    def __init__(self, poll_interval: float = 10.0) -> None:
        self.poll_interval = poll_interval
        self._active: bool  = False
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def _find_name(self) -> str | None:
        try:
            bus     = pydbus.SessionBus()
            dbus_obj = bus.get("org.freedesktop.DBus", "/org/freedesktop/DBus")
            for name in dbus_obj.ListNames():
                if name.startswith(self._PREFIX):
                    return name
        except Exception:
            pass
        return None

    def _responsive(self, name: str) -> bool:
        """
        Try to read PlaybackStatus from the KDE Connect player.
        Times out after 2 s — KDE Connect on flaky Wi-Fi can block indefinitely.
        """
        ok = [False]

        def probe() -> None:
            try:
                bus    = pydbus.SessionBus()
                player = bus.get(name, "/org/mpris/MediaPlayer2")
                _      = player.PlaybackStatus
                ok[0]  = True
            except Exception:
                pass

        t = Thread(target=probe, daemon=True)
        t.start()
        t.join(2.0)
        return ok[0]

    def check(self) -> None:
        name      = self._find_name()
        was_active = self._active
        active    = bool(name and self._responsive(name))

        with self._lock:
            self._active = active

        if active and not was_active:
            print(
                f"[mediactl] KDE Connect MPRIS active ({name}), yielding — "
                f"re-checking every {self.poll_interval:.0f} s"
            )
        elif not active and was_active:
            print("[mediactl] KDE Connect MPRIS gone / unresponsive — taking over")

    def run(self, exit_event: Event) -> None:
        self.check()    # immediate check on startup
        while not exit_event.wait(self.poll_interval):
            self.check()


# ---------------------------------------------------------------------------
# MPRIS adapter
# ---------------------------------------------------------------------------

class MediaAdapter(PlayerAdapter):
    def __init__(
        self,
        app: AppState,
        fallback_art: str = "",
        kde_watcher: "KdeConnectWatcher | None" = None,
    ) -> None:
        super().__init__()
        self.app          = app
        self.fallback_art = fallback_art
        self.kde_watcher  = kde_watcher

    def _kde_active(self) -> bool:
        return self.kde_watcher is not None and self.kde_watcher.active

    def _cmd(self, label: str, key: str) -> None:
        """Dispatch a media command; defer silently when KDE Connect is active."""
        if self._kde_active():
            print(f"[mediactl] KDE Connect active — deferring '{label}'")
            return
        print(label)
        self.app.dispatch_media_key(key)
        self.app.update()

    # -- commands -------------------------------------------------------

    def next(self)     -> None: self._cmd("next",   "next")
    def previous(self) -> None: self._cmd("prev",   "previous")
    def pause(self)    -> None: self._cmd("pause",  "pause")
    def resume(self)   -> None: self._cmd("resume", "play")
    def stop(self)     -> None: self._cmd("stop",   "stop")
    def play(self)     -> None: self._cmd("play",   "play")

    # -- state ----------------------------------------------------------

    def get_playstate(self) -> PlayState: return self.app.playbackState
    def get_shuffle(self)   -> bool:      return self.app.shuffle
    def get_loop_status(self) -> str:     return self.app.loop_status

    def get_art_url(self, track) -> str:
        return self.app.art_url or self.fallback_art

    # -- capabilities ---------------------------------------------------

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

    # -- info -----------------------------------------------------------

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
        art = self.app.art_url or self.fallback_art
        if art:
            meta["mpris:artUrl"] = art
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
    "--art-url",
    default="",
    help=(
        "Fallback album art URI used when device art cannot be fetched "
        "(e.g. for streaming tracks). Defaults to none."
    ),
)
@click.option(
    "--no-kde",
    is_flag=True,
    default=False,
    help="Disable KDE Connect detection; always act as primary MPRIS controller.",
)
@click.option(
    "--kde-interval",
    default=10.0,
    show_default=True,
    type=float,
    help="Seconds between KDE Connect responsiveness checks. Ignored with --no-kde.",
)
def cli(
    player_name: str,
    update_freq: float,
    art_url: str,
    no_kde: bool,
    kde_interval: float,
) -> None:
    """scrcpy MPRIS media controller.

    Exposes Android media playback over MPRIS so desktop notification panels
    (swaync, dunst, waybar, …) can display and control it.
    Requires an ADB-connected Android device.

    By default the script monitors the session bus for a KDE Connect MPRIS player.
    While one is active it yields control (suppresses its own MPRIS events and
    defers commands). When KDE Connect disappears or stops responding it takes
    over automatically. Use --no-kde to always act as primary controller.
    """
    app = AppState()
    app.update(emit=False)

    exit_event = Event()

    kde_watcher: KdeConnectWatcher | None = None
    if not no_kde:
        kde_watcher = KdeConnectWatcher(poll_interval=kde_interval)
        Thread(
            target=kde_watcher.run,
            args=(exit_event,),
            daemon=True,
            name="kde-watcher",
        ).start()

    update_thread = UpdateThread(app, update_freq, exit_event, kde_watcher)
    update_thread.start()

    media_adapter = MediaAdapter(app, fallback_art=art_url, kde_watcher=kde_watcher)
    mpris         = Server(name=player_name, adapter=media_adapter)
    mpris.player  = CustomPlayer(name=player_name, adapter=media_adapter)
    mpris.interfaces = mpris.root, mpris.player
    app.media_adapter = mpris

    try:
        mpris.loop()
    except KeyboardInterrupt:
        pass
    except RuntimeError:
        print(player_name + " Media Controller already running!")
    finally:
        print("quitting...")
        exit_event.set()
        update_thread.join()


if __name__ == "__main__":
    cli()
