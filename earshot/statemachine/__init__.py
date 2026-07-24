"""The single control loop: idle <-> record <-> finalize (rpi/specs/state-machine.md).

Both the hardware button and the web API drive the same machine. Actions are
serialised through one control thread; the API submits commands and waits for the
result, so "one action at a time" holds regardless of which surface acted.
"""

from earshot.statemachine.machine import Controller

__all__ = ["Controller"]
