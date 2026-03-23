from enum import StrEnum

from loguru import logger as log

from .DiscordCore import DiscordCore
from src.backend.PluginManager.EventAssigner import EventAssigner
from src.backend.PluginManager.InputBases import Input

from ..autopan import AutopanMode
from ..discordrpc.commands import (
    VOICE_STATE_CREATE,
    VOICE_STATE_DELETE,
    VOICE_CHANNEL_SELECT,
    GET_CHANNEL,
)


class Icons(StrEnum):
    AUTOPAN_ON = "unmute"
    AUTOPAN_OFF = "mute"


class AutoPan(DiscordCore):
    """Action for automatic user panning distribution.

    Key behavior:
    - Short Press: Cycle through autopan modes (Off -> Auto -> Wide -> Off)
    - Hold: Recenter all users to mono (left: 1.0, right: 1.0)

    In Auto mode, users are distributed across the stereo field using a
    spring model that leaves margins at the extremes. When users join or
    leave, the distribution is recalculated and reapplied.

    In Wide mode, users are distributed across the full stereo field without
    margins, with the first user at extreme left and the last at extreme right.
    Useful for recording scenarios where stereo separation is desired.

    Display:
    - Top label: Channel name (or "Not in voice")
    - Center label: Current mode ("Auto", "Wide", or "Autopan off")
    - Bottom label: User count
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = False

        self._current_channel_id: str = None
        self._current_channel_name: str = ""
        self._in_voice_channel: bool = False

        self.icon_keys = [Icons.AUTOPAN_ON, Icons.AUTOPAN_OFF]
        self.current_icon = self.get_icon(Icons.AUTOPAN_OFF)
        self.icon_name = Icons.AUTOPAN_OFF

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
                id="toggle-autopan",
                ui_label="toggle-autopan",
                default_event=Input.Key.Events.SHORT_UP,
                callback=self._on_toggle,
            )
        )

        self.event_manager.add_event_assigner(
            EventAssigner(
                id="recenter-users",
                ui_label="recenter-users",
                default_event=Input.Key.Events.HOLD_START,
                callback=self._on_recenter,
            )
        )

    def _on_toggle(self, _):
        try:
            current_mode = self.backend.get_autopan_mode()
            if current_mode == AutopanMode.OFF:
                new_mode = AutopanMode.DEFAULT
            elif current_mode == AutopanMode.DEFAULT:
                new_mode = AutopanMode.WIDE
            else:
                new_mode = AutopanMode.OFF

            self.backend.set_autopan_mode(new_mode)

            if new_mode != AutopanMode.OFF:
                self.backend.apply_autopan()

            self._update_display()
        except Exception as ex:
            log.error(f"Failed to toggle autopan: {ex}")
            self.show_error(3)

    def _on_recenter(self, _):
        try:
            self.backend.recenter_all_users()
            self._update_display()
        except Exception as ex:
            log.error(f"Failed to recenter users: {ex}")
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
                self.backend.clear_voice_channel_users()
            else:
                new_channel_id = data.get("channel_id")

                if self._current_channel_id and self._current_channel_id != new_channel_id:
                    self.backend.unsubscribe_voice_states(self._current_channel_id)

                self._in_voice_channel = True
                self._current_channel_id = new_channel_id
                self._current_channel_name = data.get("name", "Voice")

                self.plugin_base.add_callback(VOICE_STATE_CREATE, self._on_voice_state_create)
                self.plugin_base.add_callback(VOICE_STATE_DELETE, self._on_voice_state_delete)

                self.backend.subscribe_voice_states(self._current_channel_id)
                self.backend.get_channel(self._current_channel_id)

            self._update_display()
        except Exception as ex:
            log.error(f"AutoPan: Error in _on_voice_channel_select: {ex}")

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

            self.backend.update_voice_channel_user(
                user_id,
                user_data.get("username", "Unknown"),
                vs.get("nick"),
                vs.get("volume", 100),
                vs.get("mute", False),
                left,
                right
            )

        if self.backend.is_autopan_enabled():
            self.backend.apply_autopan()

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

        self.backend.update_voice_channel_user(
            user_id,
            user_data.get("username", "Unknown"),
            data.get("nick"),
            data.get("volume", 100),
            data.get("mute", False),
            left,
            right
        )

        if self.backend.is_autopan_enabled():
            self.backend.apply_autopan()

        self._update_display()

    def _on_voice_state_delete(self, data: dict):
        if not data:
            return

        user_data = data.get("user", {})
        user_id = user_data.get("id")
        if not user_id:
            return

        self.backend.remove_voice_channel_user(user_id)

        if self.backend.is_autopan_enabled():
            self.backend.apply_autopan()

        self._update_display()

    def _update_display(self):
        mode = self.backend.get_autopan_mode()
        user_count = len(self.backend.get_voice_channel_users())

        if mode == AutopanMode.DEFAULT:
            self.set_center_label("Auto")
        elif mode == AutopanMode.WIDE:
            self.set_center_label("Wide")
        else:
            self.set_center_label("Autopan off")

        icon = Icons.AUTOPAN_ON if mode != AutopanMode.OFF else Icons.AUTOPAN_OFF

        if not self._in_voice_channel:
            self.set_top_label("Not in voice")
            self.set_bottom_label("")
        else:
            channel_display = self._current_channel_name[:12] if len(self._current_channel_name) > 12 else self._current_channel_name
            self.set_top_label(channel_display)
            if user_count == 0:
                self.set_bottom_label("No users")
            else:
                self.set_bottom_label(f"{user_count} user{'s' if user_count != 1 else ''}")

        self.icon_name = Icons(icon)
        self.current_icon = self.get_icon(self.icon_name)
        self.display_icon()
