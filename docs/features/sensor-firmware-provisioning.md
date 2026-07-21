# Sensor firmware provisioning

**Status:** Draft — to be refined
**Tracking issue:** [#3](https://github.com/michael-jahn-dev/lunos/issues/3)

## Summary

Lunos provides the ambient-light sensor's firmware itself (ESPHome-based), rather
than relying on the user to flash a Lunar-compatible build separately. The system
tray app can then install/flash/provision that firmware onto the ESP32 device —
turning a bare sensor board into a Lunos-ready sensor from within Lunos.

## Motivation

Today the sensor firmware is a separate, user-supplied prerequisite. Shipping and
installing it from Lunos makes the sensor a first-class, self-contained part of the
project and removes an external setup step.

## Rough scope

- **Firmware source** — a Lunos-maintained ESPHome firmware definition for the
  ESP32 + ambient-light sensor.
- **Flashing/provisioning flow** — driven from the tray app: connect the device,
  flash the firmware, and configure it (Wi-Fi, hostname/`lunarsensor.local`).
- **Integration** — after provisioning, the sensor exposes the SSE endpoint and
  `sensor_event_id` channel the daemon already expects.

## Depends on

- [System tray app](system-tray-app.md) ([#2](https://github.com/michael-jahn-dev/lunos/issues/2)) — the provisioning flow lives in the app.

## Open questions

- How is the firmware built and shipped (prebuilt binary vs. building ESPHome on
  the fly)?
- Flashing mechanism — USB/serial, web-serial, OTA?
- How much does Lunos define vs. reuse from the existing Lunar/ESPHome firmware?
- Where do device-specific settings (Wi-Fi credentials, event id) get entered and
  stored?
