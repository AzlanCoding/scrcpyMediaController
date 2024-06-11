import os
import subprocess
from threading import Timer, Thread, Event
from mpris_server.adapters import PlayState, PlayerAdapter
from mpris_server.events import EventAdapter
from mpris_server.server import Server
from player import CustomPlayer

#Modify these variables to customise the player
artUrl = "file://"+os.path.join(os.path.dirname(__file__), 'icon.png')
playerName = "scrcpy"
updateFreq = 1

class app:
  def __init__(self):
    self.title = "No Media"
    self.album = "Unknown"
    self.artist = []
    self.currentPosition = 0
    self.playbackState = PlayState.PLAYING
    self.media_adapter = None
    self.oldDevice = False

  def update(self) -> None:
    media_session = subprocess.run(["adb","shell","dumpsys","media_session"], capture_output=True, text=True).stdout
    
    try:
      desc = media_session.split("description=")[1].split("\n")[0]
      assert desc != "null"
      descList = desc.split(", ")
      assert descList != ["null","null","null"] #Media is Buffering

      self.title = descList[0]
      self.artist = descList[1:-1]#Song may have multiple artists
      self.album = descList[-1]

      playback_state = media_session.split("state=PlaybackState {")[1].split("}")[0].split(", ")
      playbackStatus = playback_state[0]

      if len(playbackStatus) <= 8:
        #Support for older versions of Android
        self.oldDevice = True
        if playbackStatus == "state=0" or playbackStatus == "state=1" or \
           playbackStatus == "state=7" or playbackStatus == "state=8":
          self.playackState = PlayState.STOPPED
        elif playbackStatus == "state=2" or playbackStatus == "state=6":
          self.playbackState = PlayState.PAUSED
        elif playbackStatus == "state=3" or playbackStatus == "state=4" or \
             playbackStatus == "state=5" or playbackStatus == "state=9" or \
             playbackStatus == "state=10" or playbackStatus == "state=11":
          self.playbackState = PlayState.PLAYING
        else:
          print("Error: unknown playback status\n" + str(playback_state))
      else:
        self.oldDevice = False
        if playbackStatus == "state=NONE(0)" or playbackStatus == "state=STOPPED(1)" or \
           playbackStatus == "state=ERROR(7)" or playbackStatus == "state=CONNECTING(8)":
          self.playackState = PlayState.STOPPED
        elif playbackStatus == "state=PAUSED(2)" or playbackStatus == "state=BUFFERING(6)":
          self.playbackState = PlayState.PAUSED
        elif playbackStatus == "state=PLAYING(3)" or playbackStatus == "state=FAST_FORWARDING(4)" or \
             playbackStatus == "state=REWINDING(5)" or playbackStatus == "state=SKIPPING_TO_PREVIOUS(9)" or \
             playbackStatus == "state=SKIPPING_TO_NEXT(10)" or playbackStatus == "state=SKIPPING_TO_QUEUE_ITEM(11)":
          self.playbackState = PlayState.PLAYING
        else:
          print("Error: unknown playback status\n" + str(playback_state))

    except (IndexError, AssertionError):
      self.title = "No Media"
      self.artist = []
      self.album = "Unknown"
      self.playbackState = PlayState.PLAYING
    
    if self.media_adapter:
      EventAdapter.emit_changes(self.media_adapter.player,["Metadata","PlaybackStatus"])

  def sendKeyCode(self, keyCode: str) -> subprocess.CompletedProcess:
    return subprocess.run(["adb", "shell", "input", "keyevent", keyCode])

  def dispatchMediaKey(self, key: str) -> subprocess.CompletedProcess:
    if self.oldDevice == True:
      return subprocess.run(["adb", "shell", "media", "dispatch", key])
    else:
      return subprocess.run(["adb", "shell", "cmd", "media_session", "dispatch", key])

App = app()
App.update()



class UpdateThread(Thread):
    def __init__(self, event):
        Thread.__init__(self)
        self.exit = event

    def run(self):
        while not self.exit.wait(updateFreq):
            App.update()

exitEvent = Event()
thread = UpdateThread(exitEvent)
thread.start()


class MediaAdapter(PlayerAdapter):
  def next(self) -> None:
    #adb shell input keyevent KEYCODE_MEDIA_NEXT
    #adb shell cmd media_session dispatch next
    print("next")
    #App.sendKeyCode("KEYCODE_MEDIA_NEXT")
    App.dispatchMediaKey("next")
    App.update()

  def previous(self) -> None:
    #adb shell input keyevent KEYCODE_MEDIA_PREVIOUS
    #adb shell cmd media_session dispatch previous
    print("prev")
    #App.sendKeyCode("KEYCODE_MEDIA_PREVIOUS")
    App.dispatchMediaKey("previous")
    App.update()

  def pause(self) -> None:
    #adb shell input keyevent KEYCODE_MEDIA_PAUSE
    #adb shell cmd media_session dispatch pause
    print('pause')
    #App.sendKeyCode("KEYCODE_MEDIA_PAUSE")
    App.dispatchMediaKey("pause")
    App.update()

  def resume(self) -> None:
    #adb shell input keyevent KEYCODE_MEDIA_PLAY
    #adb shell cmd media_session dispatch play
    print("resume")
    #App.sendKeyCode("KEYCODE_MEDIA_PLAY")
    App.dispatchMediaKey("play")
    App.update()

  def stop(self) -> None:
    #adb shell input keyevent KEYCODE_MEDIA_STOP
    #adb shell cmd media_session dispatch stop
    print("stop")
    #App.sendKeyCode("KEYCODE_MEDIA_STOP")
    App.dispatchMediaKey("stop")
    App.update()

  def play(self) -> None:
    #adb shell input keyevent KEYCODE_MEDIA_PLAY
    #adb shell cmd media_session dispatch play
    print("play")
    #App.sendKeyCode("KEYCODE_MEDIA_PLAY")
    App.dispatchMediaKey("play")
    App.update()

  def get_playstate(self) -> PlayState:
    return App.playbackState

  def get_art_url(self, track):
    return artUrl

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
    return App.title

  def get_desktop_entry(self) -> str:
    return "scrcpy"

  def get_mime_types(self) -> list[str]:
    return ["audio/mpeg", "application/ogg", "video/mpeg"]

  def get_uri_schemes(self) -> list[str]:
    return ["file"]

  def metadata(self) -> dict:
    metadata = {
      "mpris:artUrl": artUrl,
      "mpris:trackid": '/org/mpris/MediaPlayer2/scrcpy',
      "xesam:title": App.title,
      "xesam:artist": App.artist,
      "xesam:album": App.album
    }

    return metadata
    
media_adapter = MediaAdapter()
mpris = Server(name=playerName, adapter=media_adapter)
mpris.player = CustomPlayer(name=playerName, adapter=media_adapter)
mpris.interfaces = mpris.root, mpris.player
App.media_adapter = mpris

try:
  mpris.loop()
except KeyboardInterrupt:
  pass
except RuntimeError:
  print(playerName + " Media Controller already running!")
finally:
  print("quitting...")
  exitEvent.set()
  thread.join()
