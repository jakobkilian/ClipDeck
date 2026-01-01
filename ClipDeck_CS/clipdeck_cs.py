from __future__ import annotations
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor"))
import logging
import threading
import time

import Live
from ableton.v2.base import const, inject
from ableton.v2.control_surface import ControlSurface
from ableton.v2.control_surface.components import SessionRingComponent, SessionComponent

from ClipDeck.skin_default import default_skin
from pythonosc import dispatcher, osc_server, udp_client

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("ClipDeck")

# Default display order based on folder name (used for initial OSC port assignment)
# ClipDeck will send the actual config via OSC
_folder_name = os.path.basename(os.path.dirname(__file__))
_DEFAULT_ORDERS = {
    "ClipDeck_Left": 0,
    "ClipDeck_Right": 1,
    "ClipDeck_1": 0,
    "ClipDeck_2": 1,
    "ClipDeck_3": 2,
    "ClipDeck_4": 3,
}
DEFAULT_DISPLAY_ORDER = _DEFAULT_ORDERS.get(_folder_name, 0)


# =============================================================================
# MAIN CONTROL SURFACE
# Bridges Ableton Live session with StreamDeck via OSC
# Config is received from clipdeck.py via OSC /config message
# =============================================================================

class ClipDeck(ControlSurface):
    
    # OSC port calculation: base + (display_order * 10)
    OSC_BASE_SEND_PORT = 9000
    OSC_BASE_RECV_PORT = 9001
    
    def __init__(self, c_instance: ControlSurface) -> None:
        self._initialized = False
        self._song_loaded = False
        self._running = True
        self._config_received = False
        self._debug_mode = False
        self._debug_client = None  # Will be set when debug mode is enabled
        
        # Use default display order for initial OSC setup
        # Actual config comes from clipdeck.py via OSC
        self._display_order = DEFAULT_DISPLAY_ORDER
        self._h_offset = 0  # Will be set by /config message
        self._initial_h_offset = 0  # Original offset from config for reset functionality
        
        super().__init__(c_instance)
        self.c_instance = c_instance
        self._wait_for_live()

        self._app = Live.Application.get_application()
        self._document = self._app.get_document()
        self._document.add_visible_tracks_listener(self._on_document_changed)

        # Session ring: 8 tracks x 4 scenes
        self._num_tracks = 8
        self._num_scenes = 4
        self._v_offset = 0
        self._last_applied_h_offset = -1  # Track what was last applied to session ring
        self._last_applied_v_offset = -1

        # OSC ports based on display order
        self._osc_send_port = self.OSC_BASE_SEND_PORT + (self._display_order * 10)
        self._osc_recv_port = self.OSC_BASE_RECV_PORT + (self._display_order * 10)

        self._structural_mismatch = False
        self._osc_client = udp_client.SimpleUDPClient("127.0.0.1", self._osc_send_port)

        self._osc_dispatcher = dispatcher.Dispatcher()
        self._osc_dispatcher.map("/pyonline", self._osc_pyonline_handler)
        self._osc_dispatcher.map("/scroll", self._osc_scroll_handler)
        self._osc_dispatcher.map("/trigger_clip", self._osc_trigger_clip_handler)
        self._osc_dispatcher.map("/config", self._osc_config_handler)
        self._osc_dispatcher.map("/refresh", self._osc_refresh_handler)
        self._osc_server = osc_server.ThreadingOSCUDPServer(
            ("127.0.0.1", self._osc_recv_port), self._osc_dispatcher
        )
        threading.Thread(target=self._osc_server.serve_forever, daemon=True).start()

        with self.component_guard():
            with inject(skin=const(default_skin)).everywhere():
                self._session_ring = SessionRingComponent(
                    num_tracks=self._num_tracks,
                    num_scenes=self._num_scenes,
                )
                self._session = SessionComponent(session_ring=self._session_ring)

            self._session_ring.add_offset_listener(self._on_offset_changed)

        # Request config from clipdeck.py
        threading.Thread(target=self._request_config, daemon=True).start()

        threading.Thread(target=self._poll_clips, daemon=True).start()
        threading.Thread(target=self._send_ablonline, daemon=True).start()

        self._add_song_listener()
        self._initialized = True
        self._song_loaded = True
        logger.info(f"[CLIPDECK INFO] CS initialized (display_order={self._display_order})")

    def _debug_log(self, message):
        """Log debug messages when debug mode is enabled."""
        if self._debug_mode:
            logger.info(f"[CLIPDECK DEBUG] {message}")

    # =========================================================================
    # CONFIG HANDLING
    # =========================================================================

    def _request_config(self):
        """Periodically request config from clipdeck.py until received."""
        while self._running and not self._config_received:
            self._osc_send("/config_request", self._display_order)
            time.sleep(1)

    def _osc_config_handler(self, addr, *args):
        """Handle config message from clipdeck.py: /config <display_order> <h_offset> <debug_mode>"""
        try:
            if len(args) >= 2:
                display_order = int(args[0])
                h_offset = int(args[1])
                debug_mode = bool(int(args[2])) if len(args) >= 3 else False
                
                # Only apply if this config is for us
                if display_order == self._display_order:
                    # Only apply h_offset on first config receive, not on subsequent updates
                    # This allows user to scroll around without being reset by periodic config
                    first_config = not self._config_received
                    
                    if first_config:
                        self._h_offset = h_offset
                        self._initial_h_offset = h_offset  # Store for reset functionality
                    
                    self._config_received = True
                    
                    # Set up debug mode
                    self._debug_mode = debug_mode
                    if debug_mode:
                        # Create debug client on port + 1000
                        debug_port = self._osc_send_port + 1000
                        self._debug_client = udp_client.SimpleUDPClient("127.0.0.1", debug_port)
                    
                    # Apply the offset to session ring only on first config
                    if first_config and (self._h_offset != self._last_applied_h_offset or self._v_offset != self._last_applied_v_offset):
                        self._session_ring.set_offsets(self._h_offset, self._v_offset)
                        self._last_applied_h_offset = self._h_offset
                        self._last_applied_v_offset = self._v_offset
                        self._prepare_and_send_clip_info()
                    
                    # Respond with ablonline to confirm we're alive
                    self._osc_send("/ablonline", self._display_order)
                    if first_config:
                        self._debug_log("Config received: "
                                        f"display_order={display_order}, h_offset={h_offset}, debug_mode={debug_mode}")

        except:
            pass

    # =========================================================================
    # INITIALIZATION & LIFECYCLE
    # =========================================================================

    def _wait_for_live(self):
        max_retries = 100
        retry_count = 0
        while not self._can_proceed():
            retry_count += 1
            if retry_count >= max_retries:
                raise RuntimeError("Failed to initialize: Live not ready")
            time.sleep(0.1)

    def _can_proceed(self) -> bool:
        try:
            _ = self.song
            _ = Live.Application.get_application()
            return True
        except:
            return False

    def _on_document_changed(self):
        try:
            self._send_message({"type": "document_closing"})
            time.sleep(0.1)
            self._song_loaded = False
            self._initialized = False
            
            def delayed_reinit():
                time.sleep(1)
                self._wait_for_live()
                if getattr(self.song, 'is_loaded', True):
                    self._reinitialize()
            threading.Thread(target=delayed_reinit, daemon=True).start()
        except:
            pass

    def _reinitialize(self):
        try:
            self._wait_for_live()
            if not getattr(self.song, 'is_loaded', True):
                return
            self._add_song_listener()
            
            def delayed_refresh():
                time.sleep(0.5)
                self._prepare_and_send_clip_info()
            threading.Thread(target=delayed_refresh, daemon=True).start()
            
            self._initialized = True
            self._song_loaded = True
        except:
            pass

    # =========================================================================
    # SONG & PLAYBACK LISTENERS
    # =========================================================================

    def _add_song_listener(self):
        try:
            self.song.add_scenes_listener(self._on_song_changed)
            if not hasattr(self, '_playing_listener_added') or not self._playing_listener_added:
                if hasattr(self.song, 'add_is_playing_listener'):
                    self.song.add_is_playing_listener(self._on_playing_changed)
                    self._playing_listener_added = True
        except:
            pass

    def _remove_song_listener(self):
        try:
            self.song.remove_scenes_listener(self._on_song_changed)
            if hasattr(self, '_playing_listener_added') and self._playing_listener_added:
                if hasattr(self.song, 'remove_is_playing_listener'):
                    self.song.remove_is_playing_listener(self._on_playing_changed)
                    self._playing_listener_added = False
        except:
            pass

    def _on_playing_changed(self):
        pass

    def _on_song_changed(self):
        try:
            self._prepare_and_send_clip_info()
        except:
            pass

    # =========================================================================
    # STRUCTURAL VALIDATION
    # =========================================================================

    def _check_structural_validity(self):
        """Check if h_offset is completely unreachable (no tracks at all at that offset)."""
        try:
            available_tracks = len(self.song.tracks)
            
            # Only flag mismatch if the offset is completely unreachable
            # i.e., there are NO tracks at h_offset
            if self._h_offset >= available_tracks:
                if not self._structural_mismatch:
                    self._structural_mismatch = True
                    self._send_message({"type": "structural_mismatch", "show": True})
                return False
            else:
                if self._structural_mismatch:
                    self._structural_mismatch = False
                    self._send_message({"type": "structural_mismatch", "show": False})
                return True
        except:
            return False

    def _handle_scroll(self, direction):
        step = 1
        if direction.endswith('-fast'):
            direction = direction.replace('-fast', '')
            step = 4
        
        old_h = self._h_offset
        old_v = self._v_offset
        
        if direction == "up":
            self._v_offset = max(0, self._v_offset - step)
        elif direction == "down":
            max_offset = max(0, len(self.song.scenes) - self._num_scenes)
            self._v_offset = min(max_offset, self._v_offset + step)
        elif direction == "left":
            self._h_offset = max(0, self._h_offset - step)
        elif direction == "right":
            max_h_offset = max(0, len(self.song.tracks) - self._num_tracks)
            self._h_offset = min(max_h_offset, self._h_offset + step)
        elif direction == "reset":
            # Reset only horizontal offset to initial value, keep vertical as-is
            self._h_offset = self._initial_h_offset
        
        # Only update session ring if offset actually changed
        if self._h_offset != old_h or self._v_offset != old_v:
            self._session_ring.set_offsets(self._h_offset, self._v_offset)
            self._last_applied_h_offset = self._h_offset
            self._last_applied_v_offset = self._v_offset

    # =========================================================================
    # OSC COMMUNICATION
    # =========================================================================

    def _osc_send(self, address, *args):
        if not hasattr(self, '_osc_client') or self._osc_client is None:
            return
        self._osc_client.send_message(address, args)
        # Also send to debug port if debug mode is enabled
        if self._debug_mode and self._debug_client:
            self._debug_client.send_message(address, args)

    def _osc_pyonline_handler(self, addr, *args):
        self._debug_log(f"OSC recv: {addr} {args}")
        self._osc_send("/ablonline", self._display_order)

    def _osc_scroll_handler(self, addr, *args):
        self._debug_log(f"OSC recv: {addr} {args}")
        try:
            direction = args[0] if args else None
            if direction is not None:
                self._handle_scroll(direction)
        except:
            pass

    def _osc_trigger_clip_handler(self, addr, track_offset, scene_offset):
        self._debug_log(f"OSC recv: {addr} track={track_offset} scene={scene_offset}")
        self._handle_trigger_clip(track_offset, scene_offset)

    def _osc_refresh_handler(self, addr, *args):
        """Handle refresh request from ClipDeck - forces full clip info update."""
        logger.info(f"[CLIPDECK INFO] Refresh request received: {args}")
        display_order = int(args[0]) if args else self._display_order
        if display_order == self._display_order:
            logger.info(f"[CLIPDECK INFO] Processing refresh for order {display_order}, config_received={self._config_received}")
            self._prepare_and_send_clip_info()
            self._osc_send("/ablonline", self._display_order)

    # =========================================================================
    # CLIP INFO GATHERING & SENDING
    # =========================================================================

    def _send_message(self, message):
        if not hasattr(self, '_osc_client') or self._osc_client is None:
            return
        if isinstance(message, dict):
            msg_type = message.get("type")
            if msg_type == "ablonline":
                self._osc_send("/ablonline", self._display_order)
            elif msg_type == "structural_mismatch":
                self._osc_send("/structural_mismatch", self._display_order, int(message.get("show", False)))
            elif msg_type == "document_closing":
                self._osc_send("/document_closing", self._display_order)
            elif msg_type == "track_stopped":
                self._osc_send("/track_stopped", self._display_order, message.get("track_index", 0), int(message.get("was_playing", True)))
        elif isinstance(message, list):
            self._osc_send("/clip_info", self._display_order, *message)

    def _handle_trigger_clip(self, track_offset: int, scene_offset: int) -> None:
        try:
            track_index = self._h_offset + track_offset
            scene_index = self._v_offset + scene_offset

            if 0 <= track_index < len(self.song.tracks) and 0 <= scene_index < len(self.song.scenes):
                track = self.song.tracks[track_index]
                clip_slot = track.clip_slots[scene_index]
                
                if clip_slot.has_clip:
                    clip_slot.fire()
                else:
                    track_is_playing = any(
                        slot.has_clip and (slot.clip.is_playing or slot.clip.is_triggered)
                        for slot in track.clip_slots
                    )
                    track.stop_all_clips()
                    self._send_message({
                        "type": "track_stopped",
                        "track_index": track_offset,
                        "was_playing": track_is_playing
                    })
                
                if self._check_structural_validity():
                    self._prepare_and_send_clip_info()
        except:
            pass

    def _on_offset_changed(self, *args) -> None:
        try:
            self._check_structural_validity()
            self._prepare_and_send_clip_info()
        except:
            pass

    def _prepare_and_send_clip_info(self) -> None:
        try:
            # Don't send until config is received
            if not self._config_received:
                return

            # Only skip completely if h_offset is unreachable
            if self._h_offset >= len(self.song.tracks):
                return

            available_tracks = len(self.song.tracks)
            available_scenes = len(self.song.scenes)

            clip_info = []

            track_is_playing = {}
            for track_index in range(self._num_tracks):
                actual_track_idx = self._h_offset + track_index
                if actual_track_idx < available_tracks:
                    track = self.song.tracks[actual_track_idx]
                    track_is_playing[track_index] = any(
                        slot.has_clip and (slot.clip.is_playing or slot.clip.is_triggered)
                        for slot in track.clip_slots
                    )
                else:
                    track_is_playing[track_index] = False

            for scene_index in range(self._num_scenes):
                actual_scene_idx = self._v_offset + scene_index
                for track_index in range(self._num_tracks):
                    actual_track_idx = self._h_offset + track_index
                    
                    # Check if this track/scene exists
                    if actual_track_idx >= available_tracks or actual_scene_idx >= available_scenes:
                        # Non-existent slot: special marker -4
                        clip_info.append("X|3276800|-4")
                        continue
                    
                    clip_slot = self.song.tracks[actual_track_idx].clip_slots[actual_scene_idx]
                    if clip_slot.has_clip:
                        clip = clip_slot.clip
                        if clip.is_playing:
                            raw_progress = clip.playing_position / clip.length
                            progress_val = max(0, min(int(raw_progress * 16), 15)) + 1
                            clip_info.append(f"{clip.name}|{clip.color}|{progress_val}")
                        elif clip.is_triggered:
                            clip_info.append(f"{clip.name}|{clip.color}|-1")
                        else:
                            if track_is_playing[track_index]:
                                clip_info.append(f"{clip.name}|{clip.color}|-2")
                            else:
                                clip_info.append(f"{clip.name}|{clip.color}|-3")
                    else:
                        if not track_is_playing[track_index]:
                            clip_info.append(" |0|-3")
                        else:
                            clip_info.append(" |0|-2")

            self._send_message(clip_info)
        except:
            pass

    # =========================================================================
    # BACKGROUND THREADS
    # =========================================================================

    def _poll_clips(self) -> None:
        while self._running:
            try:
                if not self._running:
                    break
                if not self._initialized or not self._song_loaded:
                    time.sleep(0.1)
                    continue

                try:
                    song_ref = self.song
                    if not getattr(song_ref, 'is_loaded', True):
                        time.sleep(0.1)
                        continue
                except:
                    time.sleep(0.1)
                    continue

                last_16th = -1
                paused_timer = 0.0

                while self._running and self._initialized and self._song_loaded:
                    try:
                        if not getattr(song_ref, 'is_loaded', True):
                            break
                        if not song_ref.is_playing:
                            time.sleep(0.02)
                            paused_timer += 0.02
                            if paused_timer >= 0.4:
                                paused_timer = 0
                                self._prepare_and_send_clip_info()
                        else:
                            paused_timer = 0
                            current_16th = int(song_ref.current_song_time * 4)
                            if current_16th != last_16th:
                                last_16th = current_16th
                                self._prepare_and_send_clip_info()
                            time.sleep(0.02)
                    except:
                        break
            except RuntimeError:
                if not self._running:
                    break
                self._song_loaded = False
                time.sleep(1)
            except:
                if not self._running:
                    break
                time.sleep(1)

    def _send_ablonline(self) -> None:
        while self._running:
            try:
                if not self._running:
                    break
                try:
                    is_playing = getattr(self.song, 'is_playing', True)
                except:
                    if not self._running:
                        break
                    time.sleep(1)
                    continue
                if not is_playing:
                    time.sleep(1)
                    continue
                self._send_message({"type": "ablonline"})
                if self._structural_mismatch:
                    self._send_message({"type": "structural_mismatch", "show": True})
                time.sleep(1)
            except:
                if not self._running:
                    break
                time.sleep(1)

    # =========================================================================
    # DISCONNECT & CLEANUP
    # =========================================================================

    def disconnect(self) -> None:
        try:
            self._running = False
            time.sleep(0.1)
            self._initialized = False
            self._song_loaded = False
            
            if hasattr(self, '_document'):
                self._document.remove_visible_tracks_listener(self._on_document_changed)
            try:
                if hasattr(self, '_session_ring'):
                    self._session_ring.remove_offset_listener(self._on_offset_changed)
            except:
                pass
            try:
                self._remove_song_listener()
            except:
                pass
            try:
                if hasattr(self, '_session'):
                    self._session.disconnect()
                if hasattr(self, '_session_ring'):
                    self._session_ring.disconnect()
            except:
                pass
            try:
                if hasattr(self, '_osc_server') and self._osc_server:
                    self._osc_server.shutdown()
                    self._osc_server.server_close()
            except:
                pass
            super().disconnect()
        except:
            pass
