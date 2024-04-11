"""The Coordinator for Adaptive Cover."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from homeassistant.components.cover import DOMAIN as COVER_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_SET_COVER_POSITION,
    SERVICE_SET_COVER_TILT_POSITION,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.template import state_attr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .calculation import (
    AdaptiveHorizontalCover,
    AdaptiveTiltCover,
    AdaptiveVerticalCover,
    ClimateCoverData,
    ClimateCoverState,
    NormalCoverState,
)
from .const import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CONF_AWNING_ANGLE,
    CONF_AZIMUTH,
    CONF_CLIMATE_MODE,
    CONF_DEFAULT_HEIGHT,
    CONF_DELTA_POSITION,
    CONF_DELTA_TIME,
    CONF_DISTANCE,
    CONF_ENTITIES,
    CONF_FOV_LEFT,
    CONF_FOV_RIGHT,
    CONF_HEIGHT_WIN,
    CONF_INVERSE_STATE,
    CONF_LENGTH_AWNING,
    CONF_MAX_POSITION,
    CONF_OUTSIDETEMP_ENTITY,
    CONF_PRESENCE_ENTITY,
    CONF_START_ENTITY,
    CONF_START_TIME,
    CONF_SUNSET_OFFSET,
    CONF_SUNSET_POS,
    CONF_TEMP_ENTITY,
    CONF_TEMP_HIGH,
    CONF_TEMP_LOW,
    CONF_TILT_DEPTH,
    CONF_TILT_DISTANCE,
    CONF_TILT_MODE,
    CONF_WEATHER_ENTITY,
    CONF_WEATHER_STATE,
    DOMAIN,
    LOGGER,
)
from .helpers import (
    get_datetime_from_state,
    get_last_updated,
    get_safe_state,
    get_time,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class StateChangedData:
    """StateChangedData class."""

    entity_id: str
    old_state: State | None
    new_state: State | None


@dataclass
class AdaptiveCoverData:
    """AdaptiveCoverData class."""

    climate_mode_toggle: bool
    states: dict
    attributes: dict


class AdaptiveDataUpdateCoordinator(DataUpdateCoordinator[AdaptiveCoverData]):
    """Adaptive cover data update coordinator."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant) -> None:  # noqa: D107
        super().__init__(hass, LOGGER, name=DOMAIN)

        self._cover_type = self.config_entry.data.get("sensor_type")
        self._climate_mode = self.config_entry.options.get(CONF_CLIMATE_MODE, False)
        self._switch_mode = True if self._climate_mode else False
        self._inverse_state = self.config_entry.options.get(CONF_INVERSE_STATE, False)
        self._temp_toggle = False
        self._control_toggle = True
        self.manual_duration = self.config_entry.options.get(
            CONF_MANUAL_OVERRIDE_DURATION, {"minutes": 15}
        )
        self.state_change = False
        self.state_change_data: StateChangedData | None = None
        self.manager = AdaptiveCoverManager(self.manual_duration)

    async def async_config_entry_first_refresh(self):
        """Call the first update from config_entry."""
        await super().async_config_entry_first_refresh()
        # Call your custom method here
        for cover in self.entities:
            await self.async_set_position(cover)

    async def async_check_entity_state_change(
        self, entity: str, old_state: State | None, new_state: State | None
    ) -> None:
        """Fetch and process state change event."""
        self.state_change_data = StateChangedData(entity, old_state, new_state)
        self.state_change = True
        await self.async_refresh()

    async def _async_update_data(self) -> AdaptiveCoverData:
        self.entities = self.config_entry.options.get(CONF_ENTITIES, [])
        self.min_change = self.config_entry.options.get(CONF_DELTA_POSITION, 1)
        self.time_threshold = self.config_entry.options.get(CONF_DELTA_TIME, 2)
        self.start_time = self.config_entry.options.get(CONF_START_TIME)
        self.start_time_entity = self.config_entry.options.get(CONF_START_ENTITY)
        self.manual_reset = self.config_entry.options.get(
            CONF_MANUAL_OVERRIDE_RESET, False
        )
        self.manual_duration = self.config_entry.options.get(
            CONF_MANUAL_OVERRIDE_DURATION, {"minutes": 15}
        )

        cover_data = self.get_blind_data()

        self.manager.add_covers(self.entities)

        control_method = "intermediate"
        self.climate_state = None

        if self._climate_mode:
            climate_data_var = [
                self.hass,
                self.config_entry.options.get(CONF_TEMP_ENTITY),
                self.config_entry.options.get(CONF_TEMP_LOW),
                self.config_entry.options.get(CONF_TEMP_HIGH),
                self.config_entry.options.get(CONF_PRESENCE_ENTITY),
                self.config_entry.options.get(CONF_WEATHER_ENTITY),
                self.config_entry.options.get(CONF_WEATHER_STATE),
                self.config_entry.options.get(CONF_OUTSIDETEMP_ENTITY),
                self._temp_toggle,
                self._cover_type,
            ]
            climate = ClimateCoverData(*climate_data_var)
            self.climate_state = round(
                ClimateCoverState(cover_data, climate).get_state()
            )
            climate_data = ClimateCoverState(cover_data, climate).climate_data
            if climate_data.is_summer and self.switch_mode:
                control_method = "summer"
            if climate_data.is_winter and self.switch_mode:
                control_method = "winter"

        self.default_state = round(NormalCoverState(cover_data).get_state())

        if self.state_change:
            self.manager.state_change(
                self.state_change_data, self.state, self._cover_type, self.manual_reset
            )
            self.state_change = False  # reset state change
        await self.manager.reset_if_needed()

        if self.control_toggle:
            for cover in self.entities:
                if not self.manager.is_cover_manual(cover):
                    await self.async_handle_call_service(cover)

        return AdaptiveCoverData(
            climate_mode_toggle=self.switch_mode,
            states={
                "state": self.state,
                "start": NormalCoverState(cover_data).cover.solar_times()[0],
                "end": NormalCoverState(cover_data).cover.solar_times()[1],
                "control": control_method,
                "binary": NormalCoverState(cover_data).cover.valid,
            },
            attributes={
                "default": self.config_entry.options.get(CONF_DEFAULT_HEIGHT),
                "sunset_default": self.config_entry.options.get(CONF_SUNSET_POS),
                "sunset_offset": self.config_entry.options.get(CONF_SUNSET_OFFSET),
                "azimuth_window": self.config_entry.options.get(CONF_AZIMUTH),
                "field_of_view": [
                    self.config_entry.options.get(CONF_FOV_LEFT),
                    self.config_entry.options.get(CONF_FOV_RIGHT),
                ],
                # "manual control": self.find_manual_control,
                "manual_control": self.manager.manual_control,
                "manual_control_time": self.manager.manual_control_time,
                "track_state_change": self.state_change,
            },
        )

    async def async_handle_call_service(self, entity):
        """Handle call service."""
        if (
            self.check_position(entity)
            and self.check_time_delta(entity)
            and self.after_start_time
        ):
            await self.async_set_position(entity)

    async def async_set_position(self, entity):
        """Call service to set cover position."""
        service = SERVICE_SET_COVER_POSITION
        service_data = {}
        service_data[ATTR_ENTITY_ID] = entity

        if self._cover_type == "cover_tilt":
            service = SERVICE_SET_COVER_TILT_POSITION
            service_data[ATTR_TILT_POSITION] = self.state
        else:
            service_data[ATTR_POSITION] = self.state

        await self.hass.services.async_call(COVER_DOMAIN, service, service_data)
        _LOGGER.debug("Run %s with data %s", service, service_data)

    def get_blind_data(self):
        """Assign correct class for type of blind."""
        if self._cover_type == "cover_blind":
            cover_data = AdaptiveVerticalCover(
                self.hass, *self.pos_sun, *self.common_data, *self.vertical_data
            )
        if self._cover_type == "cover_awning":
            cover_data = AdaptiveHorizontalCover(
                self.hass,
                *self.pos_sun,
                *self.common_data,
                *self.vertical_data,
                *self.horizontal_data,
            )
        if self._cover_type == "cover_tilt":
            cover_data = AdaptiveTiltCover(
                self.hass, *self.pos_sun, *self.common_data, *self.tilt_data
            )
        return cover_data

    @property
    def after_start_time(self):
        """Check if time is after start time."""
        if self.start_time_entity is not None:
            time = get_datetime_from_state(get_safe_state(self.start_time_entity))
            now = dt.datetime.now(dt.UTC)
            if now.date() == time.date():
                return now >= time
        if self.start_time is not None:
            time = get_time(self.start_time).time()
            now = dt.datetime.now().time()
            return now >= time
        return True

    def check_position(self, entity):
        """Check cover positions to reduce calls."""
        if self._cover_type == "cover_tilt":
            position = state_attr(self.hass, entity, "current_tilt_position")
        else:
            position = state_attr(self.hass, entity, "current_position")
        if position is not None:
            return abs(position - self.state) >= self.min_change
        return True

    def check_time_delta(self, entity):
        """Check if time delta is passed."""
        now = dt.datetime.now(dt.UTC)
        last_updated = get_last_updated(entity, self.hass)
        if last_updated is not None:
            return now - last_updated >= dt.timedelta(minutes=self.time_threshold)
        return True

    @property
    def pos_sun(self):
        """Fetch information for sun position."""
        return [
            state_attr(self.hass, "sun.sun", "azimuth"),
            state_attr(self.hass, "sun.sun", "elevation"),
        ]

    @property
    def common_data(self):
        """Update shared parameters."""
        return [
            self.config_entry.options.get(CONF_SUNSET_POS),
            self.config_entry.options.get(CONF_SUNSET_OFFSET),
            self.hass.config.time_zone,
            self.config_entry.options.get(CONF_FOV_LEFT),
            self.config_entry.options.get(CONF_FOV_RIGHT),
            self.config_entry.options.get(CONF_AZIMUTH),
            self.config_entry.options.get(CONF_DEFAULT_HEIGHT),
            self.config_entry.options.get(CONF_MAX_POSITION, 100),
        ]

    @property
    def vertical_data(self):
        """Update data for vertical blinds."""
        return [
            self.config_entry.options.get(CONF_DISTANCE),
            self.config_entry.options.get(CONF_HEIGHT_WIN),
        ]

    @property
    def horizontal_data(self):
        """Update data for horizontal blinds."""
        return [
            self.config_entry.options.get(CONF_LENGTH_AWNING),
            self.config_entry.options.get(CONF_AWNING_ANGLE),
        ]

    @property
    def tilt_data(self):
        """Update data for tilted blinds."""
        return [
            self.config_entry.options.get(CONF_TILT_DISTANCE),
            self.config_entry.options.get(CONF_TILT_DEPTH),
            self.config_entry.options.get(CONF_TILT_MODE),
        ]

    @property
    def state(self):
        """Handle the output of the state based on mode."""
        state = self.default_state
        if self._switch_mode:
            state = self.climate_state
        if self._inverse_state:
            state = 100 - state
        return state

    @property
    def switch_mode(self):
        """Let switch toggle climate mode."""
        return self._switch_mode

    @switch_mode.setter
    def switch_mode(self, value):
        self._switch_mode = value

    @property
    def temp_toggle(self):
        """Let switch toggle between inside or outside temperature."""
        return self._temp_toggle

    @temp_toggle.setter
    def temp_toggle(self, value):
        self._temp_toggle = value

    @property
    def control_toggle(self):
        """Toggle automation."""
        return self._control_toggle

    @control_toggle.setter
    def control_toggle(self, value):
        self._control_toggle = value


class AdaptiveCoverManager:
    """Track position changes."""

    def __init__(self, reset_duration: dict[str:int]) -> None:
        """Initialize the AdaptiveCoverManager."""
        self.covers: set[str] = set()

        self.manual_control: dict[str, bool] = {}
        self.manual_control_time: dict[str, dt.datetime] = {}
        self.reset_duration = dt.timedelta(**reset_duration)

    def add_covers(self, entity):
        """Update set with entities."""
        self.covers.update(entity)

    def state_change(self, states_data, our_state, blind_type, allow_reset):
        event = states_data
        if event is None:
            return
        entity_id = event.entity_id
        if entity_id not in self.covers:
            return

        new_state = event.new_state

        if blind_type == "cover_tilt":
            new_position = new_state.attributes.get("current_tilt_position")
        else:
            new_position = new_state.attributes.get("current_position")

        if new_position != our_state:
            _LOGGER.debug(
                "Set manual control for %s, for at least %s seconds, reset_allowed: %s",
                entity_id,
                self.reset_duration.total_seconds(),
                allow_reset,
            )
            self.mark_manual_control(entity_id)
            self.set_last_updated(entity_id, new_state, allow_reset)

    def set_last_updated(self, entity_id, new_state, allow_reset):
        if entity_id not in self.manual_control_time or allow_reset:
            last_updated = new_state.last_updated
            self.manual_control_time[entity_id] = last_updated
            _LOGGER.debug(
                "Updating last updated to %s for %s. Allow reset:%s",
                last_updated,
                entity_id,
                allow_reset,
            )
        if not allow_reset:
            _LOGGER.debug(
                "Already time specified for %s, reset is not allowed by user setting:%s",
                entity_id,
                allow_reset,
            )

    def mark_manual_control(self, cover: str) -> None:
        self.manual_control[cover] = True

    async def reset_if_needed(self):
        """Reset manual control state of the covers."""
        current_time = dt.datetime.now(dt.UTC)
        # Iterate over a copy of the dictionary
        manual_control_time_copy = dict(self.manual_control_time)
        for entity_id, last_updated in manual_control_time_copy.items():
            if current_time - last_updated > self.reset_duration:
                _LOGGER.debug(
                    "Resetting manual override for %s, because duration has elasped",
                    entity_id,
                )
                self.reset(entity_id)

    def reset(self, entity_id):
        self.manual_control[entity_id] = False
        self.manual_control_time.pop(entity_id, None)

    def is_cover_manual(self, entity_id):
        return self.manual_control.get(entity_id, False)
