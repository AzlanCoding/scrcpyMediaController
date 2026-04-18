import os
import subprocess
from threading import Thread, Event

import click
from mpris_server.adapters import PlayState, PlayerAdapter
from mpris_server.events import EventAdapter
from mpris_server.server import Server

from player import CustomPlayer

_DEFAULT_ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")


class AppState:
    def __init__(self) -> None:
        self.title = "No Media"
        self.album = "Unknown"
        self.artist: list[str] = []
        self.playbackState = PlayState.PLAYING
        self.media_adapter = None
        self.oldDevice = False

    def update(self) -> None:
        media_session = subprocess.run(
            ["adb", "shell", "dumpsys", "media_session"],
            capture_output=True,
            text=True,
        ).stdout

        try:
            desc = media_session.split("description=")[1].split("\n")[0]
            assert desc != "null"
            desc_list = desc.split(", ")
            assert desc_list != ["null", "null", "null"]  # media is buffering

            self.title = desc_list[0]
            self.artist = desc_list[1:-1]  # song may have multiple artists
            self.album = desc_list[-1]

            playback_state = (
                media_session.split("state=PlaybackState {")[1].split("}")[0].split(", ")
            )
            status = playback_state[0]

            if len(status) <= 8:
                # older Android versions use numeric state codes
                self.oldDevice = True
                if status in ("state=0", "state=1", "state=7", "state=8"):
                    self.playbackState = PlayState.STOPPED
                elif status in ("state=2", "state=6"):
                    self.playbackState = PlayState.PAUSED
                elif status in (
                    "state=3", "state=4", "state=5",
                    "state=9", "state=10", "state=11",
                ):
                    self.playbackState = PlayState.PLAYING
                else:
                    print("Error: unknown playback status\n" + str(playback_state))
            else:
                self.oldDevice = False
                if status in (
                    "state=NONE(0)", "state=STOPPED(1)",
                    "state=ERROR(7)", "state=CONNECTING(8)",
                ):
                    self.playbackState = PlayState.STOPPED
                elif status in ("state=PAUSED(2)", "state=BUFFERING(6)"):
                    self.playbackState = PlayState.PAUSED
                elif status in (
                    "state=PLAYING(3)", "state=FAST_FORWARDING(4)", "state=REWINDING(5)",
                    "state=SKIPPING_TO_PREVIOUS(9)", "state=SKIPPING_TO_NEXT(10)",
                    "state=SKIPPING_TO_QUEUE_ITEM(11)",
                ):
                    self.playbackState = PlayState.PLAYING
                else:
                    print("Error: unknown playback status\n" + str(playback_state))

        except (IndexError, AssertionError):
            self.title = "No Media"
            self.artist = []
            self.album = "Unknown"
            self.playbackState = PlayState.PLAYING

        if self.media_adapter:
            EventAdapter.emit_changes(self.media_adapter.player, ["Metadata", "PlaybackStatus"])

    def send_key_code(self, key_code: str) -> subprocess.CompletedProcess:
        return subprocess.run(["adb", "shell", "input", "keyevent", key_code])

    def dispatch_media_key(self, key: str) -> subprocess.CompletedProcess:
        if self.oldDevice:
            return subprocess.run(["adb", "shell", "media", "dispatch", key])
        return subprocess.run(["adb", "shell", "cmd", "media_session", "dispatch", key])


class UpdateThread(Thread):
    def __init__(self, app: AppState, update_freq: float, exit_event: Event) -> None:
        super().__init__(daemon=True)
        self.app = app
        self.update_freq = update_freq
        self.exit = exit_event

    def run(self) -> None:
        while not self.exit.wait(self.update_freq):
            self.app.update()


class MediaAdapter(PlayerAdapter):
    def __init__(self, app: AppState, art_url: str) -> None:
        super().__init__()
        self.app = app
        self.art_url = art_url

    def next(self) -> None:
        print("next")
        self.app.dispatch_media_key("next")
        self.app.update()

    def previous(self) -> None:
        print("prev")
        self.app.dispatch_media_key("previous")
        self.app.update()

    def pause(self) -> None:
        print("pause")
        self.app.dispatch_media_key("pause")
        self.app.update()

    def resume(self) -> None:
        print("resume")
        self.app.dispatch_media_key("play")
        self.app.update()

    def stop(self) -> None:
        print("stop")
        self.app.dispatch_media_key("stop")
        self.app.update()

    def play(self) -> None:
        print("play")
        self.app.dispatch_media_key("play")
        self.app.update()

    def get_playstate(self) -> PlayState:
        return self.app.playbackState

    def get_art_url(self, track) -> str:
        return self.art_url

    def can_go_next(self) -> bool:
        return True

    def can_go_previous(self) -> bool:
        return True

    def can_play(self) -> bool:
        return True

    def can_pause(self) -> bool:
        return True

    def can_seek(self) -> bool:
        return False

    def can_control(self) -> bool:
        return True

    def can_quit(self) -> bool:
        return False

    def can_raise(self) -> bool:
        return False

    def has_tracklist(self) -> bool:
        return False

    def can_fullscreen(self) -> bool:
        return False

    def get_fullscreen(self) -> None:
        return None

    def get_stream_title(self) -> str:
        return self.app.title

    def get_desktop_entry(self) -> str:
        return "scrcpy"

    def get_mime_types(self) -> list[str]:
        return ["audio/mpeg", "application/ogg", "video/mpeg"]

    def get_uri_schemes(self) -> list[str]:
        return ["file"]

    def metadata(self) -> dict:
        return {
            "mpris:artUrl": self.art_url,
            "mpris:trackid": "/org/mpris/MediaPlayer2/scrcpy",
            "xesam:title": self.app.title,
            "xesam:artist": self.app.artist,
            "xesam:album": self.app.album,
        }


@click.command()
@click.option(
    "--art-url",
    default=f"file://{_DEFAULT_ICON}",
    show_default=True,
    help="URI to the album art / player icon.",
)
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
def cli(art_url: str, player_name: str, update_freq: float) -> None:
    """scrcpy MPRIS media controller.

    Exposes Android media playback over MPRIS so desktop notification
    panels (swaync, dunst, waybar, etc.) can display and control it.
    Requires an ADB-connected Android device.
    """
    app = AppState()
    app.update()

    exit_event = Event()
    thread = UpdateThread(app, update_freq, exit_event)
    thread.start()

    media_adapter = MediaAdapter(app, art_url)
    mpris = Server(name=player_name, adapter=media_adapter)
    mpris.player = CustomPlayer(name=player_name, adapter=media_adapter)
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
        thread.join()


if __name__ == "__main__":
    cli()
