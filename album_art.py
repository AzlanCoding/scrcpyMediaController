"""
album_art.py — Offline album art fetching for scrcpy-media-controller.

Public API
----------
art_cache_key(title, artist, album) -> str
	Stable string key identifying a track for cache lookups.

request_art(key, title, artist, album, package, on_ready) -> str
	Returns a cached file:// URI immediately if one exists, otherwise
	starts a background fetch and calls on_ready(key, uri) when done.
	Returns '' when the result is not yet known.
"""

import hashlib
import os
import re
import shlex
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from threading import Thread

_CACHE_DIR = Path.home() / ".cache" / "scrcpyMediaController"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_BLACKLISTED_PACKAGES: set[str] = {
	"com.spotify.music",
	"de.danoeh.antennapod",
	"de.danoeh.antennapod.debug",
}

_cache: dict[str, str] = {}
_album_cache: dict[str, str] = {}
_cache_lock = threading.Lock()
_in_flight: set[str] = set()


def _cache_path(identifier: str, ext: str) -> Path:
	safe_ext = ext if ext.startswith(".") else f".{ext}"
	digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
	return _CACHE_DIR / f"{digest}{safe_ext}"


def _is_supported_image(path: Path) -> bool:
	header = path.read_bytes()[:12]
	is_jpeg = header[:3] == b"\xff\xd8\xff"
	is_png = header[:4] == b"\x89PNG"
	is_webp = header[:4] == b"RIFF" and header[8:12] == b"WEBP"
	return is_jpeg or is_png or is_webp


def _uri_exists(uri: str) -> bool:
	return uri.startswith("file://") and Path(uri[7:]).exists()


def _album_identity(package: str, artist: list[str], album: str) -> str | None:
	if not album:
		return None
	return f"{package}\x00{album}\x00{''.join(sorted(artist))}"


def _query_album_id(title: str) -> str | None:
	sql_safe = title.replace("'", "''").replace("\n", " ").replace("\r", " ")
	like_safe = sql_safe.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
	clauses = (
		f"title='{sql_safe}'",
		f"title LIKE '%{like_safe}%' ESCAPE '\\\\'",
	)

	for clause in clauses:
		shell_clause = shlex.quote(clause)
		query = subprocess.run(
			[
				"adb",
				"shell",
				f"content query --uri content://media/external/audio/media --projection album_id --where {shell_clause}",
			],
			capture_output=True,
			text=True,
			timeout=5,
		)
		match = re.search(r"album_id=(\d+)", query.stdout or "")
		if match:
			return match.group(1)
	return None


def _read_album_art(album_id: str) -> str:
	album_uri = f"content://media/external/audio/albumart/{album_id}"
	dest = _cache_path(album_uri, ".jpg")
	if not dest.exists():
		pull = subprocess.run(
			["adb", "exec-out", "content", "read", "--uri", album_uri],
			capture_output=True,
			timeout=8,
		)
		if pull.returncode != 0 or not pull.stdout:
			return ""
		fd, tmp_path = tempfile.mkstemp(dir=_CACHE_DIR, suffix=".tmp")
		try:
			with os.fdopen(fd, "wb") as tmp_file:
				tmp_file.write(pull.stdout)
			os.replace(tmp_path, dest)
		except Exception:
			if os.path.exists(tmp_path):
				os.unlink(tmp_path)
			raise

	if not _is_supported_image(dest):
		dest.unlink(missing_ok=True)
		return ""
	return f"file://{dest}"


def _fetch(title: str, package: str) -> str:
	if package in _BLACKLISTED_PACKAGES:
		return ""
	try:
		album_id = _query_album_id(title)
		return _read_album_art(album_id) if album_id else ""
	except Exception:
		return ""


def art_cache_key(title: str, artist: list[str], album: str) -> str:
	return f"{title}\x00{album}\x00{''.join(sorted(artist))}"


def request_art(
	key: str,
	title: str,
	artist: list[str],
	album: str,
	package: str,
	on_ready: Callable[[str, str], None],
) -> str:
	if package in _BLACKLISTED_PACKAGES:
		with _cache_lock:
			_cache[key] = ""
		return ""

	album_key = _album_identity(package, artist, album)
	with _cache_lock:
		if key in _cache:
			return _cache[key]
		if album_key:
			album_uri = _album_cache.get(album_key)
			if album_uri and _uri_exists(album_uri):
				_cache[key] = album_uri
				return album_uri
			_album_cache.pop(album_key, None)
		if key in _in_flight:
			return ""
		_in_flight.add(key)

	def _worker() -> None:
		uri = _fetch(title, package)
		with _cache_lock:
			_cache[key] = uri
			if uri and album_key:
				_album_cache[album_key] = uri
			_in_flight.discard(key)
		on_ready(key, uri)

	Thread(target=_worker, daemon=True).start()
	return ""
