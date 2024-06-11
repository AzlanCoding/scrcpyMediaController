from __future__ import annotations
#NOTE: THIS IS A COPY OF mpris-server.interfaces.player but without `Position`, `Shuffle`, `loop`, `OpenUri`, `Seek`, `SetPosition`, `Rate` and `Volume`

import logging
from fractions import Fraction
from typing import ClassVar, Final

from pydbus.generic import signal

from mpris_server.interfaces.interface import MprisInterface, log_trace
from mpris_server.base import BEGINNING, DbusObj, DbusTypes, Interface, MAX_RATE, MAX_VOLUME, MIN_RATE, MUTE_VOLUME, \
  PAUSE_RATE, PlayState, Position, Rate, Track, Volume
from mpris_server.enums import Access, Arg, Direction, LoopStatus, Method, Property, Signal
from mpris_server.mpris.metadata import Metadata, MetadataEntries, create_metadata_from_track, get_dbus_metadata, update_metadata


log = logging.getLogger(__name__)

ERR_NOT_ENOUGH_METADATA: Final[str] = \
  "Couldn't find enough metadata, please implement metadata() or get_stream_title() and get_current_track() methods.`"


class CustomPlayer(MprisInterface):
  INTERFACE: ClassVar[Interface] = Interface.Player

  __doc__: Final[str] = f"""
  <node>
    <interface name="{INTERFACE}">
      <method name="{Method.Next}"/>
      <method name="{Method.Pause}"/>
      <method name="{Method.PlayPause}"/>
      <method name="{Method.Play}"/>
      <method name="{Method.Previous}"/>
      <method name="{Method.Stop}"/>

      <property name="{Property.CanControl}" type="{DbusTypes.BOOLEAN}" access="{Access.READ}"/>
      <property name="{Property.CanGoNext}" type="{DbusTypes.BOOLEAN}" access="{Access.READ}"/>
      <property name="{Property.CanGoPrevious}" type="{DbusTypes.BOOLEAN}" access="{Access.READ}"/>
      <property name="{Property.CanPause}" type="{DbusTypes.BOOLEAN}" access="{Access.READ}"/>
      <property name="{Property.CanPlay}" type="{DbusTypes.BOOLEAN}" access="{Access.READ}"/>
      <property name="{Property.CanSeek}" type="{DbusTypes.BOOLEAN}" access="{Access.READ}"/>
      <property name="{Property.Metadata}" type="{DbusTypes.METADATA}" access="{Access.READ}"/>
      <property name="{Property.PlaybackStatus}" type="{DbusTypes.STRING}" access="{Access.READ}"/>

      <signal name="{Signal.Seeked}">
        <arg name="{Arg.Position}" type="{DbusTypes.INT64}"/>
      </signal>
    </interface>
  </node>
  """

  Seeked: Final[signal] = signal()

  def _get_metadata(self) -> Metadata | None:
    if metadata := self.adapter.metadata():
      return get_dbus_metadata(metadata)

    return None

  def _get_basic_metadata(self, track: Track) -> Metadata:
    metadata: Metadata = Metadata()

    if name := self.adapter.get_stream_title():
      update_metadata(metadata, MetadataEntries.TITLE, name)

    if art_url := self._get_art_url(track):
      update_metadata(metadata, MetadataEntries.ART_URL, art_url)

    return metadata

  def _get_art_url(self, track: DbusObj | Track | None) -> str:
    return self.adapter.get_art_url(track)

  @property
  @log_trace
  def CanControl(self) -> bool:
    return self.adapter.can_control()

  @property
  @log_trace
  def CanGoNext(self) -> bool:
    # if not self.CanControl:
    # return False

    return self.adapter.can_go_next()

  @property
  @log_trace
  def CanGoPrevious(self) -> bool:
    # if not self.CanControl:
    # return False

    return self.adapter.can_go_previous()

  @property
  @log_trace
  def CanPause(self) -> bool:
    return self.adapter.can_pause()
    # if not self.CanControl:
    # return False

    # return True

  @property
  @log_trace
  def CanPlay(self) -> bool:
    # if not self.CanControl:
    # return False

    return self.adapter.can_play()

  @property
  @log_trace
  def CanSeek(self) -> bool:
    return self.adapter.can_seek()
    # if not self.CanControl:
    # return False

    # return True

  @property
  @log_trace
  def Metadata(self) -> Metadata:
    # prefer adapter's metadata to building our own
    if metadata := self._get_metadata():
      return metadata

    # build metadata if no metadata supplied by adapter
    log.debug(f"Building {self.INTERFACE}.{Property.Metadata}")

    track = self.adapter.get_current_track()
    metadata: Metadata = self._get_basic_metadata(track)

    if not track:
      log.warning(ERR_NOT_ENOUGH_METADATA)
      return metadata

    return create_metadata_from_track(track, metadata)

  @property
  @log_trace
  def PlaybackStatus(self) -> PlayState:
    state = self.adapter.get_playstate()
    return state.value.title()

  @log_trace
  def Next(self):
    if not self.CanGoNext:
      log.debug(f"{self.INTERFACE}.{Method.Next} not allowed")
      return

    self.adapter.next()

  @log_trace
  def Previous(self):
    if not self.CanGoPrevious:
      log.debug(f"{self.INTERFACE}.{Method.Previous} not allowed")
      return

    self.adapter.previous()

  @log_trace
  def Pause(self):
    if not self.CanPause:
      log.debug(f"{self.INTERFACE}.{Method.Pause} not allowed")
      return

    self.adapter.pause()

  @log_trace
  def Play(self):
    if not self.CanPlay:
      log.debug(f"{self.INTERFACE}.{Method.Play} not allowed")
      return

    match self.adapter.get_playstate():
      case PlayState.PAUSED:
        self.adapter.resume()

      case _:
        self.adapter.play()

  @log_trace
  def PlayPause(self):
    if not self.CanPause:
      log.debug(f"{self.INTERFACE}.{Method.PlayPause} not allowed")
      return

    match self.adapter.get_playstate():
      case PlayState.PLAYING:
        self.adapter.pause()

      case PlayState.PAUSED:
        self.adapter.resume()

      case PlayState.STOPPED:
        self.adapter.play()

  @log_trace
  def Stop(self):
    if not self.CanControl:
      log.debug(f"{self.INTERFACE}.{Method.Stop} not allowed")
      return

    self.adapter.stop()
