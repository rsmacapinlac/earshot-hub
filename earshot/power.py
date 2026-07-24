"""Safe shutdown (FR-4, rpi/specs/state-machine.md#fr-4-safe-shutdown).

A button hold while idle powers the device off. The primary path is ``reboot(2)``
with ``LINUX_REBOOT_CMD_POWER_OFF``, which needs ``CAP_SYS_BOOT`` (granted to the
service by the systemd unit); if that is unavailable it falls back to
``systemctl poweroff --no-wall``.

The real power-off is wired **only** on the ``pi`` HAL backend
(:func:`select_shutdown_fn`), so running the app against the stub on a workstation
can never power off the developer's machine.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess

log = logging.getLogger("earshot.power")

# glibc reboot(int howto); LINUX_REBOOT_CMD_POWER_OFF halts and removes power.
_LINUX_REBOOT_CMD_POWER_OFF = 0x4321FEDC


def poweroff() -> None:
    """Power the device off. Best-effort: on success it does not return."""
    os.sync()
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.reboot(_LINUX_REBOOT_CMD_POWER_OFF)
        # reboot() only returns on failure (e.g. missing CAP_SYS_BOOT).
        log.warning("reboot(2) POWER_OFF returned (errno %d); falling back", ctypes.get_errno())
    except Exception:  # pragma: no cover - defensive; fall back regardless
        log.exception("reboot(2) POWER_OFF raised; falling back")
    subprocess.run(["systemctl", "poweroff", "--no-wall"], check=False)


def select_shutdown_fn(config):
    """The shutdown callable for this run, or ``None`` to keep the no-op.

    Real power-off is enabled only when the resolved HAL backend is ``pi`` — never
    on the stub, so a dev machine is never at risk."""
    from earshot.hal.bundle import backend_name

    try:
        backend = backend_name(config)
    except ValueError:
        return None
    return poweroff if backend == "pi" else None
