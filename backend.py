import json
import math

from streamcontroller_plugin_tools import BackendBase

from loguru import logger as log

from discordrpc import AsyncDiscord, commands
from autopan import AutopanMode


class Backend(BackendBase):
    def __init__(self):
        super().__init__()
        self.client_id: str = None
        self.client_secret: str = None
        self.access_token: str = None
        self.refresh_token: str = None
        self.discord_client: AsyncDiscord = None
        self._is_authed: bool = False
        self._current_voice_channel: str = None
        self._is_reconnecting: bool = False
        self._voice_channel_users: dict = {}  # {user_id: {username, nick, volume, muted, panning}}
        self._current_user_id: str = None  # Current user's ID (for filtering)
        self._autopan_mode: int = AutopanMode.OFF

    def discord_callback(self, code, event):
        if code == 0:
            return
        try:
            event = json.loads(event)
        except Exception as ex:
            log.error(f"failed to parse discord event: {ex}")
            return
        resp_code = (
            event.get("data").get("code", 0) if event.get("data") is not None else 0
        )
        if resp_code in [4006, 4009]:
            if not self.refresh_token:
                self.setup_client()
                return
            try:
                token_resp = self.discord_client.refresh(self.refresh_token)
            except Exception as ex:
                log.error(f"failed to refresh token {ex}")
                self._update_tokens("", "")
                self.setup_client()
                return
            access_token = token_resp.get("access_token")
            refresh_token = token_resp.get("refresh_token")
            self._update_tokens(access_token, refresh_token)
            self.discord_client.authenticate(self.access_token)
            return
        match event.get("cmd"):
            case commands.AUTHORIZE:
                auth_code = event.get("data").get("code")
                token_resp = self.discord_client.get_access_token(auth_code)
                self.access_token = token_resp.get("access_token")
                self.refresh_token = token_resp.get("refresh_token")
                self.discord_client.authenticate(self.access_token)
                self.frontend.save_access_token(self.access_token)
                self.frontend.save_refresh_token(self.refresh_token)
            case commands.AUTHENTICATE:
                self.frontend.on_auth_callback(True)
                self._is_authed = True
                # Capture current user ID for filtering in UserVolume
                data = event.get("data", {})
                user = data.get("user", {})
                self._register_callbacks()
                self._current_user_id = user.get("id")
                self._get_current_voice_channel()
            case commands.DISPATCH:
                evt = event.get("evt")
                self.frontend.trigger_event(evt, event.get("data"))
            case commands.GET_SELECTED_VOICE_CHANNEL:
                self._current_voice_channel = (
                    event.get("data").get("channel_id") if event.get("data") else None
                )
                self.frontend.trigger_event(commands.VOICE_CHANNEL_SELECT, event.get("data"))
            case commands.GET_CHANNEL:
                self.frontend.trigger_event(commands.GET_CHANNEL, event.get("data"))

    def _update_tokens(self, access_token: str = "", refresh_token: str = ""):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.frontend.save_access_token(access_token)
        self.frontend.save_refresh_token(refresh_token)

    def setup_client(self):
        if self._is_reconnecting:
            log.debug("Already reconnecting, skipping duplicate attempt")
            return
        try:
            self._is_reconnecting = True
            self.discord_client = AsyncDiscord(self.client_id, self.client_secret)
            self.discord_client.connect(self.discord_callback)
            if not self.access_token:
                self.discord_client.authorize()
            else:
                self.discord_client.authenticate(self.access_token)
        except Exception as ex:
            self.frontend.on_auth_callback(False, str(ex))
            log.error("failed to setup discord client: {0}", ex)
            if self.discord_client:
                self.discord_client.disconnect()
            self.discord_client = None
        finally:
            self._is_reconnecting = False

    def update_client_credentials(
        self,
        client_id: str,
        client_secret: str,
        access_token: str = "",
        refresh_token: str = "",
    ):
        if None in (client_id, client_secret) or "" in (client_id, client_secret):
            self.frontend.on_auth_callback(
                False, "actions.base.credentials.missing_client_info"
            )
            return
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.setup_client()

    def is_authed(self) -> bool:
        return self._is_authed

    def _register_callbacks(self):
        self.discord_client.subscribe(commands.VOICE_SETTINGS_UPDATE)
        self.discord_client.subscribe(commands.VOICE_CHANNEL_SELECT)
        self.discord_client.subscribe(commands.GET_CHANNEL)

    def _ensure_connected(self) -> bool:
        """Ensure client is connected, trigger reconnection if needed."""
        if self.discord_client is None or not self.discord_client.is_connected():
            if not self._is_reconnecting:
                self.setup_client()
            return False
        return True

    def set_mute(self, muted: bool):
        if not self._ensure_connected():
            log.warning("Discord client not connected, cannot set mute")
            return
        self.discord_client.set_voice_settings({"mute": muted})

    def set_deafen(self, muted: bool):
        if not self._ensure_connected():
            log.warning("Discord client not connected, cannot set deafen")
            return
        self.discord_client.set_voice_settings({"deaf": muted})

    def change_voice_channel(self, channel_id: str = None) -> bool:
        if not self._ensure_connected():
            log.warning("Discord client not connected, cannot change voice channel")
            return False
        self.discord_client.select_voice_channel(channel_id, True)
        return True

    def change_text_channel(self, channel_id: str) -> bool:
        if not self._ensure_connected():
            log.warning("Discord client not connected, cannot change text channel")
            return False
        self.discord_client.select_text_channel(channel_id)
        return True

    def set_push_to_talk(self, ptt: str) -> bool:
        if not self._ensure_connected():
            log.warning("Discord client not connected, cannot set push to talk")
            return False
        self.discord_client.set_voice_settings({"mode": {"type": ptt}})
        return True

    @property
    def current_voice_channel(self):
        return self._current_voice_channel

    @property
    def current_user_id(self):
        return self._current_user_id

    def _get_current_voice_channel(self):
        if not self._ensure_connected():
            log.warning(
                "Discord client not connected, cannot get current voice channel"
            )
            return
        self.discord_client.get_selected_voice_channel()

    def request_current_voice_channel(self):
        """Public method to request current voice channel state (dispatches to callbacks)."""
        self._get_current_voice_channel()

    # User volume control methods

    def set_user_volume(self, user_id: str, volume: int) -> bool:
        """Set volume for a specific user (0-200, 100 = normal)."""
        if not self._ensure_connected():
            log.warning("Discord client not connected, cannot set user volume")
            return False
        self.discord_client.set_user_voice_settings(user_id, volume=volume)
        if user_id in self._voice_channel_users:
            self._voice_channel_users[user_id]["volume"] = volume
        return True

    def set_user_mute(self, user_id: str, muted: bool) -> bool:
        """Mute/unmute a specific user locally."""
        if not self._ensure_connected():
            log.warning("Discord client not connected, cannot set user mute")
            return False
        self.discord_client.set_user_voice_settings(user_id, mute=muted)
        if user_id in self._voice_channel_users:
            self._voice_channel_users[user_id]["muted"] = muted
        return True

    def set_user_panning(self, user_id: str, left: float, right: float) -> bool:
        """Set panning for a specific user (0.0-1.0 for each channel)."""
        if not self._ensure_connected():
            log.warning("Discord client not connected, cannot set user panning")
            return False
        left = max(0.0, min(1.0, left))
        right = max(0.0, min(1.0, right))
        self.discord_client.set_user_voice_settings(user_id, left=left, right=right)
        if user_id in self._voice_channel_users:
            self._voice_channel_users[user_id]["panning"] = {"left": left, "right": right}
        return True

    def update_voice_channel_user(self, user_id: str, username: str, nick: str = None,
                                   volume: int = 100, muted: bool = False,
                                   left: float = 1.0, right: float = 1.0):
        """Track a user in the current voice channel."""
        left = max(0.0, min(1.0, left))
        right = max(0.0, min(1.0, right))
        self._voice_channel_users[user_id] = {
            "username": username,
            "nick": nick,
            "volume": volume,
            "muted": muted,
            "panning": {"left": left, "right": right}
        }

    def remove_voice_channel_user(self, user_id: str):
        """Remove a user from tracking when they leave."""
        self._voice_channel_users.pop(user_id, None)

    def clear_voice_channel_users(self):
        """Clear all tracked users (when leaving voice channel)."""
        self._voice_channel_users.clear()

    def get_voice_channel_users(self) -> dict:
        """Get a copy of the current voice channel users."""
        return self._voice_channel_users.copy()

    # Autopan methods

    def get_autopan_mode(self) -> int:
        """Get the current autopan mode."""
        return self._autopan_mode

    def set_autopan_mode(self, mode: int):
        """Set the autopan mode."""
        self._autopan_mode = mode

    def is_autopan_enabled(self) -> bool:
        """Check if autopan is currently enabled (any mode other than OFF)."""
        return self._autopan_mode != AutopanMode.OFF

    def apply_autopan(self):
        """Apply autopan distribution based on current mode."""
        if self._autopan_mode == AutopanMode.OFF:
            return
        elif self._autopan_mode == AutopanMode.DEFAULT:
            self._apply_autopan_default()
        elif self._autopan_mode == AutopanMode.WIDE:
            self._apply_autopan_wide()

    def _apply_autopan_default(self):
        """Distribute all users across the stereo field using spring model.

        Uses N+2 positions with "dummy users" at extremes, so real users
        occupy positions 1 through N. Formula: balance[i] = (i + 1) / (N + 1).
        This leaves margins at extremes that scale with user count.
        """
        if not self._ensure_connected():
            log.warning("Discord client not connected, cannot apply autopan")
            return

        user_ids = list(self._voice_channel_users.keys())
        n = len(user_ids)

        if n == 0:
            return

        for i, user_id in enumerate(user_ids):
            balance = (i + 1) / (n + 1)
            left, right = self.balance_to_panning(balance)
            self.set_user_panning(user_id, left, right)

    def _apply_autopan_wide(self):
        """Distribute users across the full stereo field without margins.

        Uses the full range from extreme left to extreme right, so the first
        user is fully left and the last is fully right. Formula for N > 1:
        balance[i] = i / (N - 1). For a single user, centered at 0.5.
        Useful for recording scenarios where stereo separation is desired.
        """
        if not self._ensure_connected():
            log.warning("Discord client not connected, cannot apply autopan")
            return

        user_ids = list(self._voice_channel_users.keys())
        n = len(user_ids)

        if n == 0:
            return

        for i, user_id in enumerate(user_ids):
            if n == 1:
                balance = 0.5
            else:
                balance = i / (n - 1)
            left, right = self.balance_to_panning(balance)
            self.set_user_panning(user_id, left, right)

    def recenter_all_users(self):
        """Reset all users to mono (left: 1.0, right: 1.0)."""
        if not self._ensure_connected():
            log.warning("Discord client not connected, cannot recenter users")
            return

        for user_id in self._voice_channel_users:
            self.set_user_panning(user_id, 1.0, 1.0)

    def get_channel(self, channel_id: str) -> bool:
        """Fetch channel information including voice states."""
        if not self._ensure_connected():
            log.warning("Discord client not connected, cannot get channel")
            return False
        self.discord_client.get_channel(channel_id)
        return True

    def subscribe_voice_states(self, channel_id: str) -> bool:
        """Subscribe to voice state events for a specific channel."""
        if not self._ensure_connected():
            log.warning("Discord client not connected, cannot subscribe to voice states")
            return False
        args = {"channel_id": channel_id}
        self.discord_client.subscribe(commands.VOICE_STATE_CREATE, args)
        self.discord_client.subscribe(commands.VOICE_STATE_DELETE, args)
        self.discord_client.subscribe(commands.VOICE_STATE_UPDATE, args)
        return True

    def unsubscribe_voice_states(self, channel_id: str) -> bool:
        """Unsubscribe from voice state events for a specific channel."""
        if not self._ensure_connected():
            return False
        args = {"channel_id": channel_id}
        self.discord_client.unsubscribe(commands.VOICE_STATE_CREATE, args)
        self.discord_client.unsubscribe(commands.VOICE_STATE_DELETE, args)
        self.discord_client.unsubscribe(commands.VOICE_STATE_UPDATE, args)
        return True

    def close(self):
        if self.discord_client:
            try:
                self.discord_client.disconnect()
            except Exception as ex:
                log.error(f"Error disconnecting Discord client: {ex}")
            self.discord_client = None
        self._is_authed = False

    @staticmethod
    def balance_to_panning(balance: float) -> tuple[float, float]:
        """Convert balance (0.0-1.0) to constant-power panning."""
        angle = balance * (math.pi / 2)
        return math.cos(angle), math.sin(angle)

    @staticmethod
    def panning_to_balance(left: float, right: float) -> float:
        """Convert panning back to balance (0.0-1.0).

        Note: For Discord's default mono (1.0, 1.0), this returns 0.5,
        which is acceptable for display purposes but round-trips to
        (0.707, 0.707), not the original mono state.
        """
        return math.atan2(right, left) / (math.pi / 2)

backend = Backend()
