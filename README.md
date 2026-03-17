# DEVI Smart for Home Assistant

Custom Home Assistant integration for DEVI Smart / Danfoss floor-heating thermostats.

This repository is prepared for installation through HACS as a custom integration. The runtime code required by Home Assistant is bundled inside `custom_components/devi`, so the integration does not depend on installing any separate repo-root Python package.

## Features

- Config flow pairing with a one-time DEVI Smart share code
- Climate entities for DEVI Smart thermostats
- Sensors and binary sensors for thermostat state
- Advanced entities for schedule and setpoint control
- Diagnostics export for support and troubleshooting

## Installation

### HACS

1. Open HACS in Home Assistant.
2. Add this repository as a custom repository with type `Integration`.
3. Install `DEVI Smart`.
4. Restart Home Assistant.
5. Add the integration from `Settings` -> `Devices & services`.

### Manual

1. Copy `custom_components/devi` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.
3. Add `DEVI Smart` from `Settings` -> `Devices & services`.

## Setup

The integration uses the DEVI Smart sharing flow:

1. In the DEVI Smart mobile app, generate a fresh one-time pairing code.
2. In Home Assistant, add the `DEVI Smart` integration.
3. Enter the pairing code immediately.
4. If pairing fails, keep the same Home Assistant dialog open and retry with a newly generated code.

The pairing backend is flaky and pairing codes are effectively single-use. Reusing the same open flow improves reliability.

## Current Scope

Supported today:

- House discovery during config flow
- Thermostat polling and state refresh
- Target temperature changes
- Preset and HVAC mode changes
- Weekly schedule and away interval service support
- Diagnostics export

Known limitations:

- Pairing may require multiple fresh OTP retries
- The DEVI cloud API is reverse engineered and may change without notice
- Production hardening is still ongoing around retry and recovery behavior

## Based On

This Home Assistant integration is based on prior reverse-engineered DEVI Smart / SecureDeviceGrid implementations and protocol work:

- [`Sonic-Amiga/opensdg`](https://github.com/Sonic-Amiga/opensdg), the original C/Java SecureDeviceGrid reverse-engineering effort and protocol implementation
- [`igor-podpalchenko/ha-devi-mqtt`](https://github.com/igor-podpalchenko/ha-devi-mqtt), an earlier Home Assistant-oriented DEVI Smart implementation used as a behavioral reference during the Python port

## Legal Notice

This integration is derived from reverse engineering of the proprietary SecureDeviceGrid platform used by DEVI Smart / Danfoss devices.

The code in this repository is intended for independent, non-commercial interoperability work with legitimately owned devices. It is not intended for hosting replacement cloud services, attacking or interfering with the vendor infrastructure, bypassing legitimate access controls, or otherwise harming the commercial interests of the platform owners.

If you are looking to build or sell a commercial product on top of SecureDeviceGrid, you should obtain authorization from the platform owners rather than relying on this reverse-engineered implementation.

## Troubleshooting

### Pairing rejected

- Generate a brand new code in the DEVI app.
- Submit it immediately.
- Keep the same Home Assistant pairing dialog open while retrying.

### Integration installs but fails at runtime

- Confirm Home Assistant restarted after installation.
- Check Home Assistant logs for import or dependency errors.
- Confirm both `PyNaCl` and `pycryptodome` are available if you are testing outside the normal Home Assistant dependency flow.

### Need support data

Use Home Assistant diagnostics for the `DEVI Smart` config entry. Sensitive values such as private keys are redacted.

## Development

- HACS validation workflow: `.github/workflows/validate.yml`
- Hassfest workflow: `.github/workflows/hassfest.yml`
- Test workflow: `.github/workflows/tests.yml`
- Release checks: `.github/workflows/release-checks.yml`

Published versions should be released through GitHub Releases. The release tag must match the integration version in `custom_components/devi/manifest.json`.
