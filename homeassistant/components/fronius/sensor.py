"""Support for Fronius devices."""
from __future__ import annotations

import copy
from datetime import timedelta
import logging
from typing import Any

from pyfronius import Fronius, FroniusError
import voluptuous as vol

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    STATE_CLASS_MEASUREMENT,
    STATE_CLASS_TOTAL_INCREASING,
    SensorEntity,
)
from homeassistant.const import (
    CONF_DEVICE,
    CONF_MONITORED_CONDITIONS,
    CONF_RESOURCE,
    CONF_SCAN_INTERVAL,
    CONF_SENSOR_TYPE,
    DEVICE_CLASS_BATTERY,
    DEVICE_CLASS_CURRENT,
    DEVICE_CLASS_ENERGY,
    DEVICE_CLASS_POWER,
    DEVICE_CLASS_POWER_FACTOR,
    DEVICE_CLASS_TEMPERATURE,
    DEVICE_CLASS_TIMESTAMP,
    DEVICE_CLASS_VOLTAGE,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

_LOGGER = logging.getLogger(__name__)

CONF_SCOPE = "scope"

TYPE_INVERTER = "inverter"
TYPE_STORAGE = "storage"
TYPE_METER = "meter"
TYPE_POWER_FLOW = "power_flow"
TYPE_LOGGER_INFO = "logger_info"
SCOPE_DEVICE = "device"
SCOPE_SYSTEM = "system"

DEFAULT_SCOPE = SCOPE_DEVICE
DEFAULT_DEVICE = 0
DEFAULT_INVERTER = 1
DEFAULT_SCAN_INTERVAL = timedelta(seconds=60)

SENSOR_TYPES = [
    TYPE_INVERTER,
    TYPE_STORAGE,
    TYPE_METER,
    TYPE_POWER_FLOW,
    TYPE_LOGGER_INFO,
]
SCOPE_TYPES = [SCOPE_DEVICE, SCOPE_SYSTEM]

PREFIX_DEVICE_CLASS_MAPPING = [
    ("state_of_charge", DEVICE_CLASS_BATTERY),
    ("temperature", DEVICE_CLASS_TEMPERATURE),
    ("power_factor", DEVICE_CLASS_POWER_FACTOR),
    ("power", DEVICE_CLASS_POWER),
    ("energy", DEVICE_CLASS_ENERGY),
    ("current", DEVICE_CLASS_CURRENT),
    ("timestamp", DEVICE_CLASS_TIMESTAMP),
    ("voltage", DEVICE_CLASS_VOLTAGE),
]

PREFIX_STATE_CLASS_MAPPING = [
    ("state_of_charge", STATE_CLASS_MEASUREMENT),
    ("temperature", STATE_CLASS_MEASUREMENT),
    ("power_factor", STATE_CLASS_MEASUREMENT),
    ("power", STATE_CLASS_MEASUREMENT),
    ("energy", STATE_CLASS_TOTAL_INCREASING),
    ("current", STATE_CLASS_MEASUREMENT),
    ("timestamp", STATE_CLASS_MEASUREMENT),
    ("voltage", STATE_CLASS_MEASUREMENT),
]


def _device_id_validator(config):
    """Ensure that inverters have default id 1 and other devices 0."""
    config = copy.deepcopy(config)
    for cond in config[CONF_MONITORED_CONDITIONS]:
        if CONF_DEVICE not in cond:
            if cond[CONF_SENSOR_TYPE] == TYPE_INVERTER:
                cond[CONF_DEVICE] = DEFAULT_INVERTER
            else:
                cond[CONF_DEVICE] = DEFAULT_DEVICE
    return config


PLATFORM_SCHEMA = vol.Schema(
    vol.All(
        PLATFORM_SCHEMA.extend(
            {
                vol.Required(CONF_RESOURCE): cv.url,
                vol.Required(CONF_MONITORED_CONDITIONS): vol.All(
                    cv.ensure_list,
                    [
                        {
                            vol.Required(CONF_SENSOR_TYPE): vol.In(SENSOR_TYPES),
                            vol.Optional(CONF_SCOPE, default=DEFAULT_SCOPE): vol.In(
                                SCOPE_TYPES
                            ),
                            vol.Optional(CONF_DEVICE): cv.positive_int,
                        }
                    ],
                ),
            }
        ),
        _device_id_validator,
    )
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up of Fronius platform."""
    session = async_get_clientsession(hass)
    fronius = Fronius(session, config[CONF_RESOURCE])

    scan_interval = config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    adapters = []
    # Creates all adapters for monitored conditions
    for condition in config[CONF_MONITORED_CONDITIONS]:

        device = condition[CONF_DEVICE]
        sensor_type = condition[CONF_SENSOR_TYPE]
        scope = condition[CONF_SCOPE]
        name = f"Fronius {condition[CONF_SENSOR_TYPE].replace('_', ' ').capitalize()} {device if scope == SCOPE_DEVICE else SCOPE_SYSTEM} {config[CONF_RESOURCE]}"
        if sensor_type == TYPE_INVERTER:
            if scope == SCOPE_SYSTEM:
                adapter_cls = FroniusInverterSystem
            else:
                adapter_cls = FroniusInverterDevice
        elif sensor_type == TYPE_METER:
            if scope == SCOPE_SYSTEM:
                adapter_cls = FroniusMeterSystem
            else:
                adapter_cls = FroniusMeterDevice
        elif sensor_type == TYPE_POWER_FLOW:
            adapter_cls = FroniusPowerFlow
        elif sensor_type == TYPE_LOGGER_INFO:
            adapter_cls = FroniusLoggerInfo
        else:
            adapter_cls = FroniusStorage

        adapters.append(adapter_cls(fronius, name, device, async_add_entities))

    # Creates a lamdba that fetches an update when called
    def adapter_data_fetcher(data_adapter):
        async def fetch_data(*_):
            await data_adapter.async_update()

        return fetch_data

    # Set up the fetching in a fixed interval for each adapter
    for adapter in adapters:
        fetch = adapter_data_fetcher(adapter)
        # fetch data once at set-up
        await fetch()
        async_track_time_interval(hass, fetch, scan_interval)


class FroniusAdapter:
    """The Fronius sensor fetching component."""

    def __init__(
        self, bridge: Fronius, name: str, device: int, add_entities: AddEntitiesCallback
    ) -> None:
        """Initialize the sensor."""
        self.bridge = bridge
        self._name = name
        self._device = device
        self._fetched: dict[str, Any] = {}
        self._available = True

        self.sensors: set[str] = set()
        self._registered_sensors: set[SensorEntity] = set()
        self._add_entities = add_entities

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def data(self):
        """Return the state attributes."""
        return self._fetched

    @property
    def available(self):
        """Whether the fronius device is active."""
        return self._available

    async def async_update(self):
        """Retrieve and update latest state."""
        try:
            values = await self._update()
        except FroniusError as err:
            # fronius devices are often powered by self-produced solar energy
            # and henced turned off at night.
            # Therefore we will not print multiple errors when connection fails
            if self._available:
                self._available = False
                _LOGGER.error("Failed to update: %s", err)
            return

        self._available = True  # reset connection failure

        attributes = self._fetched
        # Copy data of current fronius device
        for key, entry in values.items():
            # If the data is directly a sensor
            if "value" in entry:
                attributes[key] = entry
        self._fetched = attributes

        # Add discovered value fields as sensors
        # because some fields are only sent temporarily
        new_sensors = []
        for key in attributes:
            if key not in self.sensors:
                self.sensors.add(key)
                _LOGGER.info("Discovered %s, adding as sensor", key)
                new_sensors.append(FroniusTemplateSensor(self, key))
        self._add_entities(new_sensors, True)

        # Schedule an update for all included sensors
        for sensor in self._registered_sensors:
            sensor.async_schedule_update_ha_state(True)

    async def _update(self) -> dict:
        """Return values of interest."""

    @callback
    def register(self, sensor):
        """Register child sensor for update subscriptions."""
        self._registered_sensors.add(sensor)
        return lambda: self._registered_sensors.remove(sensor)


class FroniusInverterSystem(FroniusAdapter):
    """Adapter for the fronius inverter with system scope."""

    async def _update(self):
        """Get the values for the current state."""
        return await self.bridge.current_system_inverter_data()


class FroniusInverterDevice(FroniusAdapter):
    """Adapter for the fronius inverter with device scope."""

    async def _update(self):
        """Get the values for the current state."""
        return await self.bridge.current_inverter_data(self._device)


class FroniusStorage(FroniusAdapter):
    """Adapter for the fronius battery storage."""

    async def _update(self):
        """Get the values for the current state."""
        return await self.bridge.current_storage_data(self._device)


class FroniusMeterSystem(FroniusAdapter):
    """Adapter for the fronius meter with system scope."""

    async def _update(self):
        """Get the values for the current state."""
        return await self.bridge.current_system_meter_data()


class FroniusMeterDevice(FroniusAdapter):
    """Adapter for the fronius meter with device scope."""

    async def _update(self):
        """Get the values for the current state."""
        return await self.bridge.current_meter_data(self._device)


class FroniusPowerFlow(FroniusAdapter):
    """Adapter for the fronius power flow."""

    async def _update(self):
        """Get the values for the current state."""
        return await self.bridge.current_power_flow()


class FroniusLoggerInfo(FroniusAdapter):
    """Adapter for the fronius power flow."""

    async def _update(self):
        """Get the values for the current state."""
        return await self.bridge.current_logger_info()


class FroniusTemplateSensor(SensorEntity):
    """Sensor for the single values (e.g. pv power, ac power)."""

    def __init__(self, parent: FroniusAdapter, key: str) -> None:
        """Initialize a singular value sensor."""
        self._key = key
        self._attr_name = f"{key.replace('_', ' ').capitalize()} {parent.name}"
        self._parent = parent
        for prefix, device_class in PREFIX_DEVICE_CLASS_MAPPING:
            if self._key.startswith(prefix):
                self._attr_device_class = device_class
                break
        for prefix, state_class in PREFIX_STATE_CLASS_MAPPING:
            if self._key.startswith(prefix):
                self._attr_state_class = state_class
                break

    @property
    def should_poll(self):
        """Device should not be polled, returns False."""
        return False

    @property
    def available(self):
        """Whether the fronius device is active."""
        return self._parent.available

    async def async_update(self):
        """Update the internal state."""
        state = self._parent.data.get(self._key)
        self._attr_native_value = state.get("value")
        if isinstance(self._attr_native_value, float):
            self._attr_native_value = round(self._attr_native_value, 2)
        self._attr_native_unit_of_measurement = state.get("unit")

    async def async_added_to_hass(self):
        """Register at parent component for updates."""
        self.async_on_remove(self._parent.register(self))

    def __hash__(self):
        """Hash sensor by hashing its name."""
        return hash(self.name)
