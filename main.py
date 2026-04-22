#!/usr/bin/env python3
"""
scrcpy MPRIS media controller.

Exposes Android media playback over MPRIS so desktop notification panels
(swaync, dunst, waybar, …) can display and control it.
Requires an ADB-connected Android device.
"""

import os  # at the top of the file
import re
import signal
import subprocess
from threading import Event, Thread

import click
from mpris_server.adapters import PlayerAdapter, PlayState
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
		self.package: str = ""
		self.art_url: str = ""
		self.playbackState: PlayState = PlayState.PLAYING
		self.media_adapter = None
		self.oldDevice: bool = False
		self._art_key: str = ""

	@staticmethod
	def _denull(val: str) -> str:
		"""Coerce the literal string 'null' (sent by some Android versions) to ''."""
		return "" if val == "null" else val

	@staticmethod
	def _best_session(media_session: str) -> tuple[str, str, str] | None:
		"""
		Return (package, description, playback-state-token) for the best media session.
		"""
		pattern = re.compile(r"package=([A-Za-z0-9._]+)(?P<body>(?:(?!\n\s+package=).)*)", re.S)
		candidates: list[tuple[int, str, str, str]] = []
		for m in pattern.finditer(media_session):
			package = m.group(1)
			body = m.group("body")
			state_match = re.search(r"state=PlaybackState\s*\{([^}]*)\}", body)
			desc_match = re.search(r"metadata:\s*size=\d+,\s*description=([^\n]+)", body)
			if not state_match or not desc_match:
				continue
			state_token = state_match.group(1).split(", ")[0]
			desc = desc_match.group(1).strip()
			active = "active=true" in body

			score = 0
			if active:
				score += 4
			if state_token in ("state=PLAYING(3)", "state=3"):
				score += 4
			elif state_token in ("state=PAUSED(2)", "state=2", "state=BUFFERING(6)", "state=6"):
				score += 2
			if desc != "null, null, null" and desc != "null":
				score += 2
			candidates.append((score, package, desc, state_token))
		if not candidates:
			return None
		_, package, desc, state_token = max(candidates, key=lambda x: x[0])
		return package, desc, state_token

	def update(self, emit: bool = True) -> bool:
		result = subprocess.run(
			["adb", "shell", "dumpsys", "media_session"],
			capture_output=True,
			text=True,
		)
		if result.returncode != 0:
			return False
		media_session = result.stdout

		try:
			session = self._best_session(media_session)
			if session is None:
				raise ValueError("No valid media session found")
			self.package, desc, status = session
			if desc == "null":
				raise ValueError("Session description is null")
			desc_list = desc.split(", ")
			if desc_list == ["null", "null", "null"]:
				raise ValueError("Session description has no metadata")

			if len(desc_list) >= 3:
				self.title = self._denull(", ".join(desc_list[:-2])) or "Unknown"
				artist_field = self._denull(desc_list[-2])
				self.artist = [artist_field] if artist_field else []
				self.album = self._denull(desc_list[-1])
			else:
				self.title = self._denull(desc_list[0]) or "Unknown"
				self.artist = [a for a in desc_list[1:-1] if a and a != "null"]
				self.album = self._denull(desc_list[-1]) if len(desc_list) > 1 else ""

			# -- playback state -------------------------------------------
			if len(status) <= 8:
				self.oldDevice = True
				if status in ("state=0", "state=1", "state=7", "state=8"):
					self.playbackState = PlayState.STOPPED
				elif status in ("state=2", "state=6"):
					self.playbackState = PlayState.PAUSED
				elif status in ("state=3", "state=4", "state=5", "state=9", "state=10", "state=11"):
					self.playbackState = PlayState.PLAYING
				else:
					print(f"[mediactl] unknown playback status: {status}")
			else:
				self.oldDevice = False
				if status in ("state=NONE(0)", "state=STOPPED(1)", "state=ERROR(7)", "state=CONNECTING(8)"):
					self.playbackState = PlayState.STOPPED
				elif status in ("state=PAUSED(2)", "state=BUFFERING(6)"):
					self.playbackState = PlayState.PAUSED
				elif status in (
					"state=PLAYING(3)",
					"state=FAST_FORWARDING(4)",
					"state=REWINDING(5)",
					"state=SKIPPING_TO_PREVIOUS(9)",
					"state=SKIPPING_TO_NEXT(10)",
					"state=SKIPPING_TO_QUEUE_ITEM(11)",
				):
					self.playbackState = PlayState.PLAYING
				else:
					print(f"[mediactl] unknown playback status: {status}")

			# -- album art (background fetch, cached by track identity) ----
			key = art_cache_key(self.title, self.artist, self.album)
			if key != self._art_key:
				self._art_key = key
				self.art_url = request_art(
					key,
					self.title,
					self.artist,
					self.album,
					self.package,
					self._on_art_ready,
				)

		except (IndexError, ValueError):
			self.title = "No Media"
			self.artist = []
			self.album = ""
			self.package = ""
			self.art_url = ""
			self.playbackState = PlayState.PLAYING

		if self.media_adapter and emit:
			EventAdapter.emit_changes(
				self.media_adapter.player,
				["Metadata", "PlaybackStatus"],
			)
		return True

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
		self.app = app
		self.update_freq = update_freq
		self.exit = exit_event

	def run(self) -> None:
		while not self.exit.wait(self.update_freq):
			if not self.app.update():
				print("[mediactl] device disconnected — exiting")
				os.kill(os.getpid(), signal.SIGTERM)
				break


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

	def next(self) -> None:
		self._cmd("next", "next")

	def previous(self) -> None:
		self._cmd("prev", "previous")

	def pause(self) -> None:
		self._cmd("pause", "pause")

	def resume(self) -> None:
		self._cmd("resume", "play")

	def stop(self) -> None:
		self._cmd("stop", "stop")

	def play(self) -> None:
		self._cmd("play", "play")

	# -- state --------------------------------------------------------------

	def get_playstate(self) -> PlayState:
		return self.app.playbackState

	def get_art_url(self, track) -> str:
		return self.app.art_url

	# -- capabilities -------------------------------------------------------

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

	# -- info ---------------------------------------------------------------

	def get_stream_title(self) -> str:
		return self.app.title

	def get_desktop_entry(self) -> str:
		return "scrcpy"

	def get_mime_types(self) -> list[str]:
		return ["audio/mpeg", "application/ogg", "video/mpeg"]

	def get_uri_schemes(self) -> list[str]:
		return ["file"]

	def metadata(self) -> dict:
		meta: dict = {
			"mpris:trackid": "/org/mpris/MediaPlayer2/scrcpy",
			"xesam:title": self.app.title,
			"xesam:artist": self.app.artist,
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
	"-v",
	"--video",
	is_flag=True,
	default=False,
	help="Enable scrcpy video output (starts scrcpy with a window).",
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
	video: bool,
	detach: bool,
) -> None:
	"""scrcpy MPRIS media controller.

	Exposes Android media playback over MPRIS so desktop notification panels
	(swaync, dunst, waybar, ...) can display and control it.
	Requires an ADB-connected Android device.

	By default this starts `scrcpy --no-window --no-video` and tears it down on
	exit. Pass -v/--video to launch scrcpy with a video window, or --detach to
	manage scrcpy yourself.
	"""
	# -- optionally launch scrcpy -----------------------------------------
	scrcpy_proc: subprocess.Popen | None = None
	if not detach:
		try:
			scrcpy_cmd = ["scrcpy"] if video else ["scrcpy", "--no-window", "--no-video"]
			scrcpy_proc = subprocess.Popen(
				scrcpy_cmd,
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
	mpris = Server(name=player_name, adapter=media_adapter)
	mpris.player = CustomPlayer(name=player_name, adapter=media_adapter)
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
