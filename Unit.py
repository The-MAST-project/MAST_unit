import logging

from PlaneWave import pwi4_client
import time
from typing import TypeAlias
from Camera import Camera
import json

UnitType: TypeAlias = "Unit"

logger = logging.getLogger('mast.unit')


class UnitStatus:

    def __init__(self, u: UnitType):
        self.is_guiding = u.guiding
        self.is_autofocusing = u.is_autofocusing
        self.is_connected = u.connected
        if u.camera is not None and u.camera.connected:
            self.camera = u.camera.status()

        self.is_busy = self.is_autofocusing or self.is_guiding


class Unit:

    _connected: bool = False
    _is_guiding: bool = False
    _is_autofocusing = False
    _id = None

    def __init__(self, unit_id: int):
        self._id = unit_id
        self.pwi4 = pwi4_client.PWI4()
        # self.camera = Camera('ASCOM.PlaneWaveVirtual.Camera')
        self.camera = Camera('ASCOM.Simulator.Camera')
        logger.info('initialized')

    def startup(self):
        """
        Makes the MAST unit operational.  Called before the actual workload begins.
        """
        # self.connect_to_mount()
        # self.enable_motors()
        # self.find_home()
        # self.fans_on()
        # self.covers_open()
        self.camera.startup()
        # return self.pwi4.status()

    def shutdown(self):
        """
        Makes the unit idle
        :return:
        """
        # self.fans_off()
        # self.covers_open()
        self.camera.shutdown()

    @property
    def connected(self):
        return self._connected

    @connected.setter
    def connected(self, value):
        """
        Should connect/disconnect anything that needs connecting/disconnecting
        :param value:
        :return:
        """
        if not value == self._connected:
            self._connected = value

    def fans_on(self):
        return self.pwi4.request("/fans/on")

    def fans_off(self):
        return self.pwi4.request("/fans/off")

    def covers_open(self):
        """
        Opens the telescope covers
        TBD: maybe get open/close status, to optimize
        :return:
        """
        pass

    def covers_close(self):
        """
        Opens the telescope covers
        TBD: maybe get open/close status, to optimize
        :return:
        """
        pass

    def connect_to_mount(self):
        if not self.pwi4.status().mount.is_connected:
            print("mast: Connecting to mount...", end='', flush=True)
            self.pwi4.mount_connect()
            while not self.pwi4.status().mount.is_connected:
                time.sleep(1)
            print("Done")
        else:
            print("mast: Mount is connected")

    def enable_motors(self):
        status = self.pwi4.status()
        if not status.mount.axis0.is_enabled:
            print("mast: Enabling HA motors...", end='', flush=True)
            self.pwi4.mount_enable(0)
            while not self.pwi4.status().mount.axis0.is_enabled:
                time.sleep(1)
            print("done")
        else:
            print("mast: HA motor is enabled")

        if not status.mount.axis1.is_enabled:
            print("Enabling DEC motors...", end='', flush=True)
            self.pwi4.mount_enable(1)
            while not self.pwi4.status().mount.axis1.is_enabled:
                time.sleep(1)
            print("done")
        else:
            print("mast: DEC motor is enabled")

    def find_home(self):
        print("mast: Finding home...", end='', flush=True)
        self.pwi4.mount_find_home()
        last_axis0_position_degrees = -99999
        last_axis1_position_degrees = -99999
        while True:
            status = self.pwi4.status()
            delta_axis0_position_degrees = status.mount.axis0.position_degs - last_axis0_position_degrees
            delta_axis1_position_degrees = status.mount.axis1.position_degs - last_axis1_position_degrees

            if abs(delta_axis0_position_degrees) < 0.001 and abs(delta_axis1_position_degrees) < 0.001:
                break

            last_axis0_position_degrees = status.mount.axis0.position_degs
            last_axis1_position_degrees = status.mount.axis1.position_degs

            time.sleep(1)
        print("Done")

    def start_autofocus(self):
        if self.pwi4.status().autofocus.is_running:
            print("mast: Autofocus is running")
            return

        self._is_autofocusing = True
        print("mast: Starting autofocus", flush=True, end='')
        self.pwi4.request("/autofocus/start")
        time.sleep(2)   # let it start
        while self.pwi4.status().autofocus.is_running:
            time.sleep(5)
            print(".", flush=True, end='')
        print("done. ", flush=True, end='')
        st = self.pwi4.status()
        if st.autofocus.success:
            print(f"best_position={st.autofocus.best_position}, tolerance={st.autofocus.tolerance}")
        else:
            print("FAILED")

        self._is_autofocusing = False
        return st

    def stop_autofocusing(self):
        self.pwi4.request("/autofocus/stop")
        self._is_autofocusing = False

    @property
    def is_autofocusing(self):
        return self._is_autofocusing

    def start_guiding(self):
        self._is_guiding = True

    def stop_guiding(self):
        if self._is_guiding:
            self._is_guiding = False

    def is_guiding(self):
        return self._is_guiding

    @property
    def guiding(self) -> bool:
        return self._is_guiding

    def status(self):
        return UnitStatus(self)
