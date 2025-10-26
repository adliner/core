"""Govee light local."""

from __future__ import annotations

import logging
from typing import Any

from govee_local_api import GoveeDevice, GoveeLightFeatures

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
    filter_supported_color_modes,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import GoveeLocalApiCoordinator, GoveeLocalConfigEntry

_LOGGER = logging.getLogger(__name__)

_NONE_SCENE = "none"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: GoveeLocalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Govee light setup."""

    coordinator = config_entry.runtime_data

    def discovery_callback(device: GoveeDevice, is_new: bool) -> bool:
        if is_new:
            async_add_entities([GoveeLight(coordinator, device)])
        return True

    async_add_entities(
        GoveeLight(coordinator, device) for device in coordinator.devices
    )

    await coordinator.set_discovery_callback(discovery_callback)


class GoveeLight(CoordinatorEntity[GoveeLocalApiCoordinator], LightEntity):
    """Govee Light."""

    _attr_translation_key = "govee_light"
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_color_modes: set[ColorMode]
    _fixed_color_mode: ColorMode | None = None
    _attr_effect_list: list[str] | None = None
    _attr_effect: str | None = None
    _attr_supported_features: LightEntityFeature = LightEntityFeature(0)
    _last_color_state: (
        tuple[
            ColorMode | str | None,
            int | None,
            tuple[int, int, int] | tuple[int | None] | None,
        ]
        | None
    ) = None

    def __init__(
        self,
        coordinator: GoveeLocalApiCoordinator,
        device: GoveeDevice,
    ) -> None:
        """Govee Light constructor."""

        super().__init__(coordinator)
        self._device = device
        device.set_update_callback(self._update_callback)

        self._attr_unique_id = device.fingerprint

        capabilities = device.capabilities
        color_modes = {ColorMode.ONOFF}
        if capabilities:
            if GoveeLightFeatures.COLOR_RGB & capabilities.features:
                color_modes.add(ColorMode.RGB)
            if GoveeLightFeatures.COLOR_KELVIN_TEMPERATURE & capabilities.features:
                color_modes.add(ColorMode.COLOR_TEMP)
                self._attr_max_color_temp_kelvin = 9000
                self._attr_min_color_temp_kelvin = 2000
            if GoveeLightFeatures.BRIGHTNESS & capabilities.features:
                color_modes.add(ColorMode.BRIGHTNESS)

            if (
                GoveeLightFeatures.SCENES & capabilities.features
                and capabilities.scenes
            ):
                self._attr_supported_features = LightEntityFeature.EFFECT
                self._attr_effect_list = [_NONE_SCENE, *capabilities.scenes.keys()]

        self._attr_supported_color_modes = filter_supported_color_modes(color_modes)
        if len(self._attr_supported_color_modes) == 1:
            # If the light supports only a single color mode, set it now
            self._fixed_color_mode = next(iter(self._attr_supported_color_modes))

        self._attr_device_info = DeviceInfo(
            identifiers={
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, device.fingerprint)
            },
            name=device.sku,
            manufacturer=MANUFACTURER,
            model_id=device.sku,
            serial_number=device.fingerprint,
        )

        # Cache for last known color/brightness to preserve across off state
        self._cached_rgb_color: tuple[int, int, int] | None = None
        self._cached_color_temp: int | None = None
        self._cached_brightness: int | None = None

    # -- Attribute accessors with caching ---------------------------------
    @property
    def is_on(self) -> bool:
        """Return true if device is on (brightness above 0)."""
        return self._device.on

    @property
    def brightness(self) -> int:
        """Return the brightness of this light between 0..255."""
        # Cache brightness even if device reports 0 (off)
        current = int((self._device.brightness / 100.0) * 255.0)
        if current > 0:
            self._cached_brightness = current
        return self._cached_brightness or current
    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the color temperature in Kelvin."""
        val = self._device.temperature_color
        if val:
            self._cached_color_temp = val
        return self._cached_color_temp or val

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the rgb color."""
        val = self._device.rgb_color
        if val:
            self._cached_rgb_color = val
        return self._cached_rgb_color or val

    @property
    def color_mode(self) -> ColorMode | str | None:
        """Return the color mode."""
        if self._fixed_color_mode:
            # The light supports only a single color mode, return it
            return self._fixed_color_mode

        # The light supports both color temperature and RGB, determine which
        # mode the light is in
        if (
            self._device.temperature_color is not None
            and self._device.temperature_color > 0
        ):
            return ColorMode.COLOR_TEMP
        return ColorMode.RGB

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the device on."""
        if ATTR_BRIGHTNESS in kwargs:
            brightness: int = int((float(kwargs[ATTR_BRIGHTNESS]) / 255.0) * 100.0)
            await self.coordinator.set_brightness(self._device, brightness)

        if ATTR_RGB_COLOR in kwargs:
            self._attr_color_mode = ColorMode.RGB
            self._attr_effect = None
            self._last_color_state = None
            red, green, blue = kwargs[ATTR_RGB_COLOR]
            await self.coordinator.set_rgb_color(self._device, red, green, blue)
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_effect = None
            self._last_color_state = None
            temperature: float = kwargs[ATTR_COLOR_TEMP_KELVIN]
            await self.coordinator.set_temperature(self._device, int(temperature))
        elif ATTR_EFFECT in kwargs:
            effect = kwargs[ATTR_EFFECT]
            if effect and self._attr_effect_list and effect in self._attr_effect_list:
                if effect == _NONE_SCENE:
                    self._attr_effect = None
                    await self._restore_last_color_state()
                else:
                    self._attr_effect = effect
                    self._save_last_color_state()
                    await self.coordinator.set_scene(self._device, effect)

        if not self.is_on or not kwargs:
            await self.coordinator.turn_on(self._device)

        # If turning on without color args, restore cached color/brightness
        if not any(k in kwargs for k in (ATTR_RGB_COLOR, ATTR_COLOR_TEMP_KELVIN, ATTR_EFFECT)):
            if self._cached_rgb_color:
                await self.coordinator.set_rgb_color(self._device, *self._cached_rgb_color)
            elif self._cached_color_temp:
                await self.coordinator.set_temperature(self._device, int(self._cached_color_temp))
            if self._cached_brightness:
                brightness_pct = int((float(self._cached_brightness) / 255.0) * 100.0)
                await self.coordinator.set_brightness(self._device, brightness_pct)

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the device off."""
        await self.coordinator.turn_off(self._device)
        self.async_write_ha_state()

        # Preserve last known color/brightness on power off
        if self._device.rgb_color:
            self._cached_rgb_color = self._device.rgb_color
        if self._device.temperature_color:
            self._cached_color_temp = self._device.temperature_color
        if self._device.brightness:
            self._cached_brightness = int((self._device.brightness / 100.0) * 255.0)

        # Do NOT clear color/brightness cache when off; they’re needed for snapshot restore

    @callback
    def _update_callback(self, device: GoveeDevice) -> None:
        self.async_write_ha_state()


        # Refresh cache when new state updates arrive
        if device.rgb_color:
            self._cached_rgb_color = device.rgb_color
        if device.temperature_color:
            self._cached_color_temp = device.temperature_color
        if device.brightness:
            self._cached_brightness = int((device.brightness / 100.0) * 255.0)

    def _save_last_color_state(self) -> None:
        color_mode = self.color_mode
        self._last_color_state = (
            color_mode,
            self.brightness,
            (self.color_temp_kelvin,)
            if color_mode == ColorMode.COLOR_TEMP
            else self.rgb_color,
        )

    async def _restore_last_color_state(self) -> None:
        if self._last_color_state:
            color_mode, brightness, color = self._last_color_state
            if color:
                if color_mode == ColorMode.RGB:
                    await self.coordinator.set_rgb_color(self._device, *color)
                elif color_mode == ColorMode.COLOR_TEMP:
                    await self.coordinator.set_temperature(self._device, *color)
            if brightness:
                await self.coordinator.set_brightness(
                    self._device, int((float(brightness) / 255.0) * 100.0)
                )
            self._last_color_state = None
