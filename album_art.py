"""
album_art.py — Album art fetching for scrcpy-media-controller.

Public API
----------
art_cache_key(title, artist, album) -> str
	Stable string key identifying a track for cache lookups.

request_art(key, title, on_ready) -> str
	Returns a cached file:// URI immediately if one exists, otherwise
	starts a background fetch and calls on_ready(key, uri) when done.
	Returns '' when the result is not yet known.
"""

import re
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from threading import Thread

# ---------------------------------------------------------------------------
# Cache state (module-level, shared across all callers)
# ---------------------------------------------------------------------------

_TMPDIR = Path(tempfile.gettempdir())
_cache: dict[str, str] = {}  # key → file:// URI or "" for known miss
_cache_lock = threading.Lock()
_in_flight: set[str] = set()  # keys whose fetch is currently running

# ---------------------------------------------------------------------------
# Package → app tag mapping
# ---------------------------------------------------------------------------

_KNOWN_PACKAGES: dict[str, str] = {
	"io.github.muntashirakon.Music": "auxio",
	"io.github.zyrouge.symphony": "auxio",  # similar Coil cache layout
	"com.spotify.music": "spotify",
	"de.danoeh.antennapod": "antennapod",
	"de.danoeh.antennapod.debug": "antennapod",
}

# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------


def _adb_shell(cmd: str, timeout: int = 5) -> str:
	result = subprocess.run(
		["adb", "shell", cmd],
		capture_output=True,
		text=True,
		timeout=timeout,
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
	try:
		out = _adb_shell("dumpsys window | grep mCurrentFocus")
		m = re.search(r"\{[^}]+ (\S+)/", out)
		if m:
			return m.group(1)
	except Exception:
		pass
	return None


# ---------------------------------------------------------------------------
# Per-app resolvers: return a remote path on the device, or None
# ---------------------------------------------------------------------------


def _art_auxio(package: str) -> str | None:
	"""
	Auxio/Symphony uses Coil's image_manager_disk_cache.
	Readable via run-as on debug builds; falls back to the MediaStore
	notification approach for release builds.
	"""
	cache_dir = f"/data/data/{package}/cache/image_manager_disk_cache"
	try:
		result = subprocess.run(
			["adb", "shell", f"run-as {package} ls -t {cache_dir}"],
			capture_output=True,
			text=True,
			timeout=5,
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
	Grabs the most recently modified image file.
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
				"adb",
				"shell",
				f"run-as {package} find {cache_dir} -type f -printf '%T@ %p\\n' | sort -rn | head -n 1 | awk '{{print $2}}'",
			],
			capture_output=True,
			text=True,
			timeout=8,
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
	through MediaStore. Works on Android 9 and below; _data is redacted
	on Android 10+ (scoped storage).
	"""
	try:
		dump = _adb_shell(f"dumpsys notification | grep -A 20 '{package}'")
		m = re.search(r"album_id[=: ]+([0-9]+)", dump)
		if m:
			album_id = m.group(1)
			out = _adb_shell(f"content query --uri content://media/external/audio/albumart/{album_id} --projection _data")
			dm = re.search(r"_data=([^\s,]+)", out)
			if dm:
				return dm.group(1)
	except Exception:
		pass
	return None


_RESOLVERS: dict[str, Callable[[str], str | None]] = {
	"auxio": _art_auxio,
	"spotify": _art_spotify,
	"antennapod": _art_antennapod,
}

# ---------------------------------------------------------------------------
# File transfer
# ---------------------------------------------------------------------------


def _pull_remote_art(remote_path: str, dest: Path, package: str) -> bool:
	"""Copy a file from the device to *dest*. Uses run-as for private paths."""
	if remote_path.startswith("/data/data/"):
		result = subprocess.run(
			["adb", "shell", f"run-as {package} cat '{remote_path}'"],
			capture_output=True,
			timeout=8,
		)
		if result.returncode == 0 and result.stdout:
			dest.write_bytes(result.stdout)
			return True
		return False
	result = subprocess.run(
		["adb", "pull", remote_path, str(dest)],
		capture_output=True,
		text=True,
		timeout=10,
	)
	return result.returncode == 0


# ---------------------------------------------------------------------------
# Fetch logic
# ---------------------------------------------------------------------------


def _fetch(title: str) -> str:
	"""
	Pull album art from the device. Returns a file:// URI on success, '' on failure.

	Strategy:
	  1. Detect the foreground app and use its dedicated resolver.
	  2. Fall back to a MediaStore content-provider query by track title
		 (works for any local-music app on Android 9 and below).
	"""
	# strategy 1: app-specific resolver
	try:
		pkg = _foreground_package()
		if pkg:
			app_tag = _KNOWN_PACKAGES.get(pkg)
			resolver = _RESOLVERS.get(app_tag) if app_tag else None
			if resolver:
				remote = resolver(pkg)
				if remote:
					ext = Path(remote).suffix or ".jpg"
					dest = _TMPDIR / f"scrcpy_art_{pkg}{ext}"
					if _pull_remote_art(remote, dest, pkg):
						header = dest.read_bytes()[:4]
						if (
							header[:3] == b"\xff\xd8\xff"  # JPEG
							or header[:4] == b"\x89PNG"  # PNG
							or header[:4] == b"RIFF"  # WEBP
						):
							return f"file://{dest}"
	except Exception:
		pass

	# strategy 2: MediaStore query by title
	try:
		safe = title.replace("'", "''")
		q = subprocess.run(
			[
				"adb",
				"shell",
				"content",
				"query",
				"--uri",
				"content://media/external/audio/media",
				"--projection",
				"album_id",
				"--where",
				f"title='{safe}'",
			],
			capture_output=True,
			text=True,
			timeout=5,
		)
		m = re.search(r"album_id=(\d+)", q.stdout)
		if not m:
			return ""
		album_id = m.group(1)

		dest = _TMPDIR / f"scrcpy_art_{album_id}.jpg"
		if not dest.exists():
			pull = subprocess.run(
				[
					"adb",
					"exec-out",
					"content",
					"read",
					"--uri",
					f"content://media/external/audio/albumart/{album_id}",
				],
				capture_output=True,
				timeout=8,
			)
			if not pull.stdout or pull.stdout[:3] != b"\xff\xd8\xff":
				return ""
			dest.write_bytes(pull.stdout)

		return f"file://{dest}"
	except Exception:
		return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def art_cache_key(title: str, artist: list[str], album: str) -> str:
	"""Stable string key identifying a track for cache lookups."""
	return f"{title}\x00{album}\x00{''.join(sorted(artist))}"


def request_art(
	key: str,
	title: str,
	on_ready: Callable[[str, str], None],
) -> str:
	"""
	Return a cached file:// URI immediately if one is available.
	Otherwise start a background fetch and call on_ready(key, uri) when done.
	Returns '' when the result is not yet known.
	"""
	with _cache_lock:
		if key in _cache:
			return _cache[key]
		if key in _in_flight:
			return ""
		_in_flight.add(key)

	def _worker() -> None:
		uri = _fetch(title)
		with _cache_lock:
			_cache[key] = uri
			_in_flight.discard(key)
		on_ready(key, uri)

	Thread(target=_worker, daemon=True).start()
	return ""
