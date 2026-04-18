#!/usr/bin/env python3
"""
scrcpy MPRIS media controller.

Exposes Android media playback over MPRIS so desktop notification panels
(swaync, dunst, waybar, …) can display and control it.
Requires an ADB-connected Android device.
"""

import re
import subprocess
import tempfile
import threading
from pathlib import Path
from threading import Thread, Event

import click
from mpris_server.adapters import PlayState, PlayerAdapter
from mpris_server.events import EventAdapter
from mpris_server.server import Server

from player import CustomPlayer


# ---------------------------------------------------------------------------
# Album-art helpers: foreground-app detection + per-app resolvers
# ---------------------------------------------------------------------------

_TMPDIR = Path(tempfile.gettempdir())
_ART_CACHE: dict[str, str] = {}         # art_key → file:// URI  (or "" for known miss)
_ART_CACHE_LOCK = threading.Lock()
_ART_IN_FLIGHT: set[str] = set()        # art_keys whose fetch is in progress

# Package → app tag mapping.  Add entries here to support more apps.
_KNOWN_PACKAGES: dict[str, str] = {
    "io.github.muntashirakon.Music":   "auxio",
    "io.github.zyrouge.symphony":      "auxio",   # similar Coil cache layout
    "com.spotify.music":               "spotify",
    "de.danoeh.antennapod":            "antennapod",
    "de.danoeh.antennapod.debug":      "antennapod",
}


def _adb_shell(cmd: str, timeout: int = 5) -> str:
    result = subprocess.run(
        ["adb", "shell", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def _foreground_package() -> str | None:
    """Return the package name of the topmost foreground activity (Android 10+)."""
    try:
        out = _adb_shell("dumpsys activity top | grep ACTIVITY | tail -n 1")
        m = re.search(r"ACTIVITY\s+(\S+)/", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    # Fallback: window manager focus info
    try:
        out = _adb_shell("dumpsys window | grep mCurrentFocus")
        m = re.search(r"\{[^}]+ (\S+)/", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


# -- Per-app resolvers: each returns a remote path on the device, or None ----

def _art_auxio(package: str) -> str | None:
    """
    Auxio (and Symphony) uses Coil's image_manager_disk_cache.
    Readable via run-as on debug builds; falls back to MediaStore notification
    approach for release builds.
    """
    cache_dir = f"/data/data/{package}/cache/image_manager_disk_cache"
    try:
        result = subprocess.run(
            ["adb", "shell", f"run-as {package} ls -t {cache_dir}"],
            capture_output=True, text=True, timeout=5,
        )
        files = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
        if files:
            return f"{cache_dir}/{files[0]}"
    except Exception:
        pass
    return _mediastore_art_via_notification(package)


def _art_spotify(package: str) -> str | None:
    """
    Spotify caches art as .jpg/.webp in external storage — no root needed.
    Grab the most recently modified image file.
    """
    bases = [
        f"/sdcard/Android/data/{package}/cache/",
        f"/storage/emulated/0/Android/data/{package}/cache/",
    ]
    for base in bases:
        try:
            newest = _adb_shell(
                f"find {base} -type f \\( -name '*.jpg' -o -name '*.webp' \\) "
                f"-printf '%T@ %p\\n' 2>/dev/null | sort -rn | head -n 1 | awk '{{print $2}}'",
                timeout=8,
            )
            if newest:
                return newest
        except Exception:
            continue
    return None


def _art_antennapod(package: str) -> str | None:
    """
    AntennaPod uses a Glide disk cache in its private data dir.
    Tries run-as first, then falls back to external storage images.
    """
    cache_dir = f"/data/data/{package}/cache/image_manager_disk_cache"
    try:
        result = subprocess.run(
            [
                "adb", "shell",
                f"run-as {package} "
                f"find {cache_dir} -type f -printf '%T@ %p\\n' "
                f"| sort -rn | head -n 1 | awk '{{print $2}}'",
            ],
            capture_output=True, text=True, timeout=8,
        )
        path = result.stdout.strip()
        if path:
            return path
    except Exception:
        pass
    try:
        img = _adb_shell(
            f"find /sdcard/Android/data/{package}/ -type f "
            f"\\( -name '*.jpg' -o -name '*.png' \\) "
            f"-printf '%T@ %p\\n' 2>/dev/null | sort -rn | head -n 1 | awk '{{print $2}}'",
            timeout=8,
        )
        if img:
            return img
    except Exception:
        pass
    return None


def _mediastore_art_via_notification(package: str) -> str | None:
    """
    Extract album_id from the active media notification, then resolve it
    through MediaStore.  Works on Android 9 and below; _data is redacted
    on Android 10+ (scoped storage).
    """
    try:
        dump = _adb_shell(f"dumpsys notification | grep -A 20 '{package}'")
        m = re.search(r"album_id[=: ]+([0-9]+)", dump)
        if m:
            album_id = m.group(1)
            out = _adb_shell(
                f"content query --uri content://media/external/audio/albumart/{album_id} "
                f"--projection _data"
            )
            dm = re.search(r"_data=([^\s,]+)", out)
            if dm:
                return dm.group(1)
    except Exception:
        pass
    return None


_ART_RESOLVERS: dict[str, object] = {
    "auxio":      _art_auxio,
    "spotify":    _art_spotify,
    "antennapod": _art_antennapod,
}


def _pull_remote_art(remote_path: str, dest: Path, package: str) -> bool:
    """Copy a file from the device to *dest*. Uses run-as for private paths."""
    if remote_path.startswith("/data/data/"):
        result = subprocess.run(
            ["adb", "shell", f"run-as {package} cat '{remote_path}'"],
            capture_output=True, timeout=8,
        )
        if result.returncode == 0 and result.stdout:
            dest.write_bytes(result.stdout)
            return True
        return False
    result = subprocess.run(
        ["adb", "pull", remote_path, str(dest)],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0


def _art_cache_key(title: str, artist: list[str], album: str) -> str:
    return f"{title}\x00{album}\x00{''.join(sorted(artist))}"


def _fetch_art_from_device(title: str) -> str:
    """
    Attempt to pull album art from the device.
    Strategy (in order):
      1. Detect foreground app and use its dedicated resolver.
      2. Fall back to MediaStore content provider query by title
         (works for any local-music app on Android 9 and below).
    Returns a file:// URI on success, '' on failure.
    """
    # --- strategy 1: app-specific resolver ----------------------------------
    try:
        pkg = _foreground_package()
        if pkg:
            app_tag = _KNOWN_PACKAGES.get(pkg)
            resolver = _ART_RESOLVERS.get(app_tag) if app_tag else None
            if resolver:
                remote = resolver(pkg)
                if remote:
                    ext = Path(remote).suffix or ".jpg"
                    dest = _TMPDIR / f"scrcpy_art_{pkg}{ext}"
                    if _pull_remote_art(remote, dest, pkg):
                        # Basic sanity-check: at least looks like an image
                        header = dest.read_bytes()[:4]
                        if (
                            header[:3] == b"\xff\xd8\xff"   # JPEG
                            or header[:4] == b"\x89PNG"      # PNG
                            or header[:4] == b"RIFF"         # WEBP (RIFF....)
                        ):
                            return f"file://{dest}"
    except Exception:
        pass

    # --- strategy 2: MediaStore query by title ------------------------------
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

        dest = _TMPDIR / f"scrcpy_art_{album_id}.jpg"
        if not dest.exists():
            pull = subprocess.run(
                [
                    "adb", "exec-out", "content", "read",
                    "--uri", f"content://media/external/audio/albumart/{album_id}",
                ],
                capture_output=True, timeout=8,
            )
            if not pull.stdout or pull.stdout[:3] != b"\xff\xd8\xff":
                return ""
            dest.write_bytes(pull.stdout)

        return f"file://{dest}"
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
            key = _art_cache_key(self.title, self.artist, self.album)
            if key != self._art_key:
                self._art_key = key
                self.art_url  = ""
                with _ART_CACHE_LOCK:
                    if key in _ART_CACHE:
                        self.art_url = _ART_CACHE[key]
                    elif key not in _ART_IN_FLIGHT:
                        _ART_IN_FLIGHT.add(key)
                        Thread(
                            target=self._art_worker,
                            args=(key, self.title),
                            daemon=True,
                        ).start()

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

    def _art_worker(self, key: str, title: str) -> None:
        uri = _fetch_art_from_device(title)
        with _ART_CACHE_LOCK:
            _ART_CACHE[key] = uri
            _ART_IN_FLIGHT.discard(key)
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
    def __init__(self, app: AppState, fallback_art: str = "") -> None:
        super().__init__()
        self.app          = app
        self.fallback_art = fallback_art

    def _cmd(self, label: str, key: str) -> None:
        print(label)
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

    def get_art_url(self, track) -> str:
        return self.app.art_url or self.fallback_art

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
        "(e.g. for streaming tracks). Defaults to the bundled icon."
    ),
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
    art_url: str,
    detach: bool,
) -> None:
    """scrcpy MPRIS media controller.

    Exposes Android media playback over MPRIS so desktop notification panels
    (swaync, dunst, waybar, ...) can display and control it.
    Requires an ADB-connected Android device.

    By default this also starts `scrcpy --no-window --no-video` and tears it
    down on exit. Pass --detach to manage scrcpy yourself.
    """
    # -- resolve fallback art icon ----------------------------------------
    if not art_url:
        icon = Path(__file__).parent / "icon.png"
        if icon.exists():
            art_url = f"file://{icon}"

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

    media_adapter = MediaAdapter(app, fallback_art=art_url)
    mpris         = Server(name=player_name, adapter=media_adapter)
    mpris.player  = CustomPlayer(name=player_name, adapter=media_adapter)
    mpris.interfaces = mpris.root, mpris.player
    app.media_adapter = mpris

    try:
        mpris.loop()
    except KeyboardInterrupt:
        pass
    except RuntimeError:
        print(player_name + " media controller is already running!")
    finally:
        print("quitting...")
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
