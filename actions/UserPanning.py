from loguru import logger as log

from .DiscordCore import DiscordCore
from src.backend.PluginManager.EventAssigner import EventAssigner
from src.backend.PluginManager.InputBases import Input

from ..discordrpc.commands import (
    VOICE_STATE_CREATE,
    VOICE_STATE_DELETE,
    VOICE_STATE_UPDATE,
    VOICE_CHANNEL_SELECT,
    GET_CHANNEL,
)


class UserPanning(DiscordCore):
    """Action for controlling per-user panning via dial.

    Dial behavior:
    - Rotate CW: Increase balance toward right
    - Rotate CCW: Decrease balance toward left
    - Press: Cycle to next user in voice channel
    - Hold: Reset current user to mono (left: 1.0, right: 1.0)

    Display:
    - Top label: Current voice channel name (or "Not in voice")
    - Center label: Username/nick
    - Bottom label: Panning position (e.g., "-45°", "+30°", "Mono")
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = False

        self._users: list = []
        self._current_user_index: int = 0
        self._current_channel_id: str = None
        self._current_channel_name: str = ""
        self._in_voice_channel: bool = False

        self.PAN_STEP = 0.05

    def on_ready(self):
        super().on_ready()

        self.plugin_base.connect_to_event(
            event_id=f"{self.plugin_base.get_plugin_id()}::{VOICE_CHANNEL_SELECT}",
            callback=self._on_voice_channel_select,
        )
        self.plugin_base.connect_to_event(
            event_id=f"{self.plugin_base.get_plugin_id()}::{GET_CHANNEL}",
            callback=self._on_get_channel,
        )

        self._update_display()
        self.backend.request_current_voice_channel()

    def create_event_assigners(self):
        self.event_manager.add_event_assigner(
            EventAssigner(
                id="pan-right",
                ui_label="pan-right",
                default_event=Input.Dial.Events.TURN_CW,
                callback=self._on_pan_right,
            )
        )
        self.event_manager.add_event_assigner(
            EventAssigner(
                id="pan-left",
                ui_label="pan-left",
                default_event=Input.Dial.Events.TURN_CCW,
                callback=self._on_pan_left,
            )
        )

        self.event_manager.add_event_assigner(
            EventAssigner(
                id="cycle-user",
                ui_label="cycle-user",
                default_event=Input.Dial.Events.DOWN,
                callback=self._on_cycle_user,
            )
        )

        self.event_manager.add_event_assigner(
            EventAssigner(
                id="reset-pan",
                ui_label="reset-pan",
                default_event=Input.Dial.Events.HOLD_START,
                callback=self._on_reset_pan,
            )
        )

        self.event_manager.add_event_assigner(
            EventAssigner(
                id="cycle-user-key",
                ui_label="cycle-user-key",
                default_event=Input.Key.Events.DOWN,
                callback=self._on_cycle_user,
            )
        )

    def _on_pan_right(self, _):
        self._adjust_pan(self.PAN_STEP)

    def _on_pan_left(self, _):
        self._adjust_pan(-self.PAN_STEP)

    def _on_cycle_user(self, _):
        if not self._users:
            return
        self._current_user_index = (self._current_user_index + 1) % len(self._users)
        self._update_display()

    def _on_reset_pan(self, _):
        if not self._users or self._current_user_index >= len(self._users):
            return

        user = self._users[self._current_user_index]
        try:
            if self.backend.set_user_panning(user["id"], 1.0, 1.0):
                user["panning"] = {"left": 1.0, "right": 1.0}
                self._update_display()
        except Exception as ex:
            log.error(f"Failed to reset user panning: {ex}")
            self.show_error(3)

    def _adjust_pan(self, delta: float):
        if not self._users or self._current_user_index >= len(self._users):
            return

        user = self._users[self._current_user_index]
        panning = user.get("panning", {"left": 1.0, "right": 1.0})

        left = panning.get("left", 1.0)
        right = panning.get("right", 1.0)

        balance = self.backend.panning_to_balance(left, right)
        new_balance = max(0.0, min(1.0, balance + delta))
        new_left, new_right = self.backend.balance_to_panning(new_balance)

        try:
            if self.backend.set_user_panning(user["id"], new_left, new_right):
                user["panning"] = {"left": new_left, "right": new_right}
                self._update_display()
        except Exception as ex:
            log.error(f"Failed to set user panning: {ex}")
            self.show_error(3)

    def _on_voice_channel_select(self, *args, **kwargs):
        data = args[1]
        try:
            if data is None or data.get("channel_id") is None:
                if self._current_channel_id:
                    self.backend.unsubscribe_voice_states(self._current_channel_id)
                self._in_voice_channel = False
                self._current_channel_id = None
                self._current_channel_name = ""
                self._users.clear()
                self._current_user_index = 0
                self.backend.clear_voice_channel_users()
            else:
                new_channel_id = data.get("channel_id")

                if self._current_channel_id and self._current_channel_id != new_channel_id:
                    self.backend.unsubscribe_voice_states(self._current_channel_id)
                    self._users.clear()
                    self._current_user_index = 0

                self._in_voice_channel = True
                self._current_channel_id = new_channel_id
                self._current_channel_name = data.get("name", "Voice")

                self.plugin_base.add_callback(VOICE_STATE_CREATE, self._on_voice_state_create)
                self.plugin_base.add_callback(VOICE_STATE_DELETE, self._on_voice_state_delete)
                self.plugin_base.add_callback(VOICE_STATE_UPDATE, self._on_voice_state_update)

                self.backend.subscribe_voice_states(self._current_channel_id)
                self.backend.get_channel(self._current_channel_id)

            self._update_display()
        except Exception as ex:
            log.error(f"UserPanning[{id(self)}]: Error in _on_voice_channel_select: {ex}")

    def _on_get_channel(self, *args, **kwargs):
        data = args[1]
        if not data:
            return

        channel_id = data.get("id")
        if channel_id != self._current_channel_id:
            return

        if data.get("name"):
            self._current_channel_name = data.get("name")

        voice_states = data.get("voice_states", [])
        current_user_id = self.backend.current_user_id

        for vs in voice_states:
            user_data = vs.get("user", {})
            user_id = user_data.get("id")

            if not user_id or user_id == current_user_id:
                continue

            pan_data = vs.get("pan", {})
            left = pan_data.get("left", 1.0)
            right = pan_data.get("right", 1.0)

            user_info = {
                "id": user_id,
                "username": user_data.get("username", "Unknown"),
                "nick": vs.get("nick"),
                "panning": {"left": left, "right": right},
            }

            if not any(u["id"] == user_id for u in self._users):
                self._users.append(user_info)

            self.backend.update_voice_channel_user(
                user_id,
                user_info["username"],
                user_info["nick"],
                vs.get("volume", 100),
                vs.get("mute", False),
                left,
                right
            )

        self._update_display()

    def _on_voice_state_create(self, data: dict):
        if not data:
            return

        user_data = data.get("user", {})
        user_id = user_data.get("id")
        if not user_id or user_id == self.backend.current_user_id:
            return

        pan_data = data.get("pan", {})
        left = pan_data.get("left", 1.0)
        right = pan_data.get("right", 1.0)

        user_info = {
            "id": user_id,
            "username": user_data.get("username", "Unknown"),
            "nick": data.get("nick"),
            "panning": {"left": left, "right": right},
        }

        if not any(u["id"] == user_id for u in self._users):
            self._users.append(user_info)

        self.backend.update_voice_channel_user(
            user_id,
            user_info["username"],
            user_info["nick"],
            data.get("volume", 100),
            data.get("mute", False),
            left,
            right
        )

        self._update_display()

    def _on_voice_state_delete(self, data: dict):
        if not data:
            return

        user_data = data.get("user", {})
        user_id = user_data.get("id")
        if not user_id:
            return

        self._users = [u for u in self._users if u["id"] != user_id]

        if self._current_user_index >= len(self._users):
            self._current_user_index = max(0, len(self._users) - 1)

        self.backend.remove_voice_channel_user(user_id)

        self._update_display()

    def _on_voice_state_update(self, data: dict):
        if not data:
            return

        user_data = data.get("user", {})
        user_id = user_data.get("id")
        if not user_id:
            return

        for user in self._users:
            if user["id"] == user_id:
                if "pan" in data:
                    pan_data = data.get("pan", {})
                    user["panning"] = {
                        "left": pan_data.get("left", 1.0),
                        "right": pan_data.get("right", 1.0),
                    }
                if "nick" in data:
                    user["nick"] = data.get("nick")
                break

        self._update_display()

    def _update_display(self):
        if not self._in_voice_channel or not self._users:
            self.set_top_label("Not in voice" if not self._in_voice_channel else self._current_channel_name[:12])
            self.set_center_label("")
            self.set_bottom_label("No users" if self._in_voice_channel else "")
            return

        channel_display = self._current_channel_name[:12] if len(self._current_channel_name) > 12 else self._current_channel_name
        self.set_top_label(channel_display)

        if self._current_user_index < len(self._users):
            user = self._users[self._current_user_index]
            display_name = user.get("nick") or user.get("username", "Unknown")
            display_name = display_name[:10] if len(display_name) > 10 else display_name

            panning = user.get("panning", {"left": 1.0, "right": 1.0})
            left = panning.get("left", 1.0)
            right = panning.get("right", 1.0)

            if left >= 1.0 and right >= 1.0:
                pan_display = "Mono"
            else:
                balance = self.backend.panning_to_balance(left, right)
                degrees = int(round((balance - 0.5) * 180))
                pan_display = f"{degrees:+d}°" if degrees != 0 else "0°"

            self.set_center_label(display_name)
            self.set_bottom_label(pan_display)
        else:
            self.set_center_label("")
            self.set_bottom_label("No selection")
