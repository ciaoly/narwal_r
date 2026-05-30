"""Narwal Flow Robot Vacuum integration for Home Assistant."""

from __future__ import annotations

import logging
from typing import TypeAlias

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import CONF_MODEL, CONF_PRODUCT_KEY, DOMAIN, FAN_SPEED_MAP, PLATFORMS
from .coordinator import NarwalCoordinator
from .narwal_client import FanLevel, MopHumidity, NarwalConnectionError

_LOGGER = logging.getLogger(__name__)

NarwalConfigEntry: TypeAlias = ConfigEntry[NarwalCoordinator]

SERVICE_CLEAN_AREA = "clean_area"
ATTR_CLEANING_AREA_ID = "cleaning_area_id"
ATTR_CLEANING_MODE = "cleaning_mode"
ATTR_FAN_SPEED = "fan_speed"
ATTR_MOP_HUMIDITY = "mop_humidity"

CLEANING_MODE_MAP: dict[str, int] = {
    "sweep": 1,
    "mop": 2,
    "sweep_and_mop": 3,
    "sweep_then_mop": 4,
}

MOP_HUMIDITY_MAP: dict[str, MopHumidity] = {
    "dry": MopHumidity.DRY,
    "normal": MopHumidity.NORMAL,
    "wet": MopHumidity.WET,
}

SERVICE_CLEAN_AREA_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required(ATTR_CLEANING_AREA_ID): vol.All(cv.ensure_list, [str]),
        vol.Optional(ATTR_CLEANING_MODE): vol.In(list(CLEANING_MODE_MAP)),
        vol.Optional(ATTR_FAN_SPEED): vol.In(
            list(FAN_SPEED_MAP) + ["standard"]
        ),
        vol.Optional(ATTR_MOP_HUMIDITY): vol.In(list(MOP_HUMIDITY_MAP)),
    }
)


def _fan_speed_from_service(value: str | None) -> FanLevel | None:
    """Convert service fan speed names to Narwal fan level values."""
    if value is None:
        return None
    if value == "standard":
        value = "normal"
    return FAN_SPEED_MAP[value]


async def _async_handle_clean_area(hass: HomeAssistant, call) -> None:
    """Handle narwal_r.clean_area with per-task cleaning settings."""
    registry = er.async_get(hass)
    coordinators: dict[str, NarwalCoordinator] = hass.data.get(DOMAIN, {})

    area_ids: list[str] = call.data[ATTR_CLEANING_AREA_ID]
    cleaning_mode = CLEANING_MODE_MAP.get(call.data.get(ATTR_CLEANING_MODE))
    fan_level = _fan_speed_from_service(call.data.get(ATTR_FAN_SPEED))
    mop_humidity = MOP_HUMIDITY_MAP.get(call.data.get(ATTR_MOP_HUMIDITY))

    for entity_id in call.data[ATTR_ENTITY_ID]:
        registry_entry = registry.async_get(entity_id)
        if registry_entry is None:
            raise ServiceValidationError(
                f"Entity {entity_id} is not registered"
            )
        if registry_entry.domain != "vacuum":
            raise ServiceValidationError(
                f"Entity {entity_id} is not a vacuum entity"
            )

        coordinator = coordinators.get(registry_entry.config_entry_id or "")
        if coordinator is None:
            raise ServiceValidationError(
                f"Entity {entity_id} does not belong to a loaded Narwal entry"
            )

        state = coordinator.client.state
        if cleaning_mode is not None:
            state.cleaning_mode = cleaning_mode
        if fan_level is not None:
            state.fan_level = int(fan_level)
        if mop_humidity is not None:
            state.mop_humidity = int(mop_humidity)
        coordinator.async_set_updated_data(state)

    await hass.services.async_call(
        "vacuum",
        "clean_area",
        {
            ATTR_ENTITY_ID: call.data[ATTR_ENTITY_ID],
            ATTR_CLEANING_AREA_ID: area_ids,
        },
        blocking=True,
        context=call.context,
    )


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entries to version 2 (add product_key)."""
    if config_entry.version < 2:
        _LOGGER.info(
            "Migrating Narwal config entry from version %d to 2",
            config_entry.version,
        )
        new_data = {**config_entry.data}
        if CONF_PRODUCT_KEY not in new_data:
            new_data[CONF_PRODUCT_KEY] = "QoEsI5qYXO"
        if CONF_MODEL not in new_data:
            new_data[CONF_MODEL] = "Narwal Flow"
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, version=2,
        )
        _LOGGER.info("Migration complete: product_key=%s", new_data[CONF_PRODUCT_KEY])
    return True


async def async_setup_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Set up Narwal from a config entry."""
    coordinator = NarwalCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except NarwalConnectionError as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to Narwal vacuum at {entry.data['host']}: {err}"
        ) from err

    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    if not hass.services.has_service(DOMAIN, SERVICE_CLEAN_AREA):
        async def async_handle_clean_area(call) -> None:
            await _async_handle_clean_area(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAN_AREA,
            async_handle_clean_area,
            schema=SERVICE_CLEAN_AREA_SCHEMA,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: NarwalConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        domain_data: dict[str, NarwalCoordinator] = hass.data.get(DOMAIN, {})
        domain_data.pop(entry.entry_id, None)
        if not domain_data:
            hass.services.async_remove(DOMAIN, SERVICE_CLEAN_AREA)
            hass.data.pop(DOMAIN, None)
        await entry.runtime_data.async_shutdown()

    return unload_ok
