import logging
import threading
from enum import Enum, Flag
import datetime

import utils
from utils import RepeatTimer, return_with_status, Activities, init_log, TimeStamped
from mastapi import Mastapi
from powered_device import PoweredDevice
import os
import sys
import platform

cur_dir = os.path.abspath(os.path.dirname(__file__))                            # Specifies the current directory.
ximc_dir = os.path.join(cur_dir, "Standa", "ximc-2.13.6", "ximc")               # dependencies for examples.
sys.path.append(os.path.join(ximc_dir, "crossplatform", "wrappers", "python"))  # add pyximc.py wrapper to python path

if platform.system() == "Windows":
    # Determining the directory with dependencies for windows depending on the bit depth.
    arch_dir = "win64" if "64" in platform.architecture()[0] else "win32"  #
    lib_dir = os.path.join(ximc_dir, arch_dir)
    if sys.version_info >= (3, 8):
        os.add_dll_directory(lib_dir)
    else:
        os.environ["Path"] = lib_dir + ";" + os.environ["Path"]  # add dll path into an environment variable

    from pyximc import (Result,  EnumerateFlags, device_information_t, string_at, byref, MvcmdStatus, cast, POINTER,
                        c_int, status_t, edges_settings_t)
    from pyximc import lib as ximclib


class StageActivities(Flag):
    Idle = 0
    StartingUp = (1 << 0)
    ShuttingDown = (1 << 1)
    Moving = (1 << 2)


class StageDirection(Enum):
    Up = 0
    Down = 1


class StageStatus(TimeStamped):
    is_powered: bool
    is_connected: bool
    is_operational: bool
    position: int
    state_verbal: str
    activities: StageActivities
    activities_verbal: str
    api_methods: list
    reasons: list[str]


class PresetPosition(Enum):
    Image = 1,
    Spectra = 2,
    Min = 3,
    Middle = 4,
    Max = 5,


stage_position_str2int_dict: dict = {
    'Min': PresetPosition.Min,
    'Max': PresetPosition.Max,
    'Middle': PresetPosition.Middle,
    'Image': PresetPosition.Image,
    'Spectra': PresetPosition.Spectra,
}

stage_direction_str2int_dict: dict = {
    'Up': StageDirection.Up,
    'Down': StageDirection.Down,
}


class Stage(Mastapi, Activities, PoweredDevice):
    logger: logging.Logger
    ticks_at_start: int
    ticks_at_target: int
    motion_start_time: datetime
    timer: RepeatTimer
    device_uri: str = None
    _position: int | None = None
    is_moving: bool = False
    target: int | None = None
    stage_lock: threading.Lock
    activities: StageActivities = StageActivities.Idle
    presets: dict
    _positioning_precision = 100

    simulated: False
    sim_connected: False
    sim_delta_per_tick = 3000

    @classmethod
    @property
    def positioning_precision(cls):
        return cls._positioning_precision

    def __init__(self):
        self.logger = logging.getLogger('mast.unit.stage')
        init_log(self.logger)

        PoweredDevice.__init__(self, 'Stage', self)

        self.device = None
        self.activities = StageActivities.Idle

        # This is device search and enumeration with probing. It gives more information about devices.
        probe_flags = EnumerateFlags.ENUMERATE_PROBE
        enum_hints = b"addr="
        dev_enum = ximclib.enumerate_devices(probe_flags, enum_hints)

        dev_count = ximclib.get_device_count(dev_enum)
        if dev_count > 0:
            self.device_uri = ximclib.get_device_name(dev_enum, 0)
            dev = ximclib.open_device(self.device_uri)
            if dev != -1:
                x_device_information = device_information_t()
                result = ximclib.get_device_information(dev, byref(x_device_information))
                x_edges_settings = edges_settings_t()
                result1 = ximclib.get_edges_settings(dev, byref(x_edges_settings))
                if result == Result.Ok and result1 == Result.Ok:
                    comport = str(self.device_uri)
                    comport = comport[comport.find('COM'):]
                    self.min_travel = x_edges_settings.LeftBorder + 100
                    self.max_travel = x_edges_settings.RightBorder - 100

                    self.device_info = "Port: {}, Manufacturer={}, Product={}, Version={}.{}.{}, Range={}..{}".format(
                        comport,
                        repr(string_at(x_device_information.Manufacturer).decode()),
                        repr(string_at(x_device_information.ProductDescription).decode()),
                        repr(x_device_information.Major),
                        repr(x_device_information.Minor),
                        repr(x_device_information.Release),
                        self.min_travel,
                        self.max_travel,
                    )
                ximclib.close_device(byref(cast(dev, POINTER(c_int))))
                self.stage_lock = threading.Lock()
                self.simulated = False
        else:
            self.simulated = True
            self.max_travel = 300000 - 100
            self.min_travel = 100
            self.sim_connected = False
            self.sim_delta_per_tick = 3000
            self.device_info = "Simulated, Range={}..{}".format(
                    self.min_travel,
                    self.max_travel,
                )

        image_position = utils.config.get("stage", "ImagePosition")
        spectra_position = utils.config.get("stage", "SpectraPosition")

        self.presets = {
            PresetPosition.Min: self.min_travel,
            PresetPosition.Max: self.max_travel,
            PresetPosition.Middle: int((self.max_travel - self.min_travel) / 2),
            PresetPosition.Image: image_position,
            PresetPosition.Spectra: spectra_position,
        }

        if self.simulated:
            self._position = self.presets[PresetPosition.Spectra]

        self.timer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = 'stage-timer-thread'
        self.timer.start()

        self.logger.info(f'initialized ({self.device_info})')

    @property
    def connected(self) -> bool:
        if self.simulated:
            return self.sim_connected
        else:
            return self.device is not None

    @connected.setter
    def connected(self, value):
        if self.simulated:
            self.sim_connected = value
            return

        if not self.is_powered:
            return

        if value:
            dev = ximclib.open_device(self.device_uri)
            if dev != -1:
                self.device = dev
            else:
                self.device = None
        else:
            ximclib.close_device(byref(cast(self.device, POINTER(c_int))))
            self.device = None

        self.logger.info(f'connected = {value} => {self.connected}')

    @return_with_status
    def connect(self):
        """
        Connects to the **MAST** stage controller

        :mastapi:
        """

        if self.is_powered:
            self.connected = True

    @return_with_status
    def disconnect(self):
        """
        Disconnects from the **MAST** stage controller

        :mastapi:
        """

        if self.is_powered:
            self.connected = False

    @return_with_status
    def startup(self):
        """
        Startup routine for the **MAST** stage.  Makes it ``operational``:
        * If not powered, powers it ON
        * If not connected, connects to the controller
        * If the stage is not at operational position, it is moved

        :mastapi:
        """

        if not self.is_powered:
            self.power_on()
        if not self.connected:
            self.connect()
        if not Stage.close_enough(self.position, self.presets[PresetPosition.Spectra]):
            self.start_activity(StageActivities.StartingUp, self.logger)
            self.move(PresetPosition.Spectra)

    @return_with_status
    def shutdown(self):
        """
        Shutdown routine for the **MAST** stage.  Makes it ``idle``

        :mastapi:
        """
        self.disconnect()
        self.power_off()

    @property
    def position(self) -> int | None:

        return self._position

    @position.setter
    def position(self, value):
        if not self.connected:
            raise Exception('Not connected')

        if Stage.close_enough(self.position, value):
            self.logger.info(f'Not changing position ({self.position} is close enough to {value}')
            return

        self.target = value
        if self.simulated:
            self.start_activity(StageActivities.Moving, self.logger)
            return
        else:
            with self.stage_lock:
                result = ximclib.command_move(self.device, value)
            if result == Result.Ok:
                self.start_activity(StageActivities.Moving, self.logger)
            else:
                raise Exception(f'Could not start move to {value}')

    def status(self) -> StageStatus:
        """
        Returns the status of the MAST stage
        :mastapi:
        """
        st = StageStatus()
        st.is_connected = False
        st.activities = self.activities
        st.activities_verbal = self.activities.name
        st.is_powered = self.is_powered
        st.is_connected = self.connected

        st.reasons = list()
        if not self.is_powered:
            st.reasons.append('not-powered')
        if not self.connected:
            st.reasons.append('not-connected')
        if self.is_active(StageActivities.Moving):
            st.reasons.append('moving')
        if self.connected and not Stage.close_enough(self.position, self.presets[PresetPosition.Spectra]):
            st.reasons.append('not close enough to the Spectra position ' 
                              f'(current: {self.position}, Spectra: {self.presets[PresetPosition.Spectra]})')
        st.is_operational = len(st.reasons) == 0

        st.position = self.position if self.connected else None
        st.stamp()
        return st

    @staticmethod
    def close_enough(position, target):
        return abs(position - target) <= Stage.positioning_precision

    def ontimer(self):
        if not self.connected:
            return

        if self.simulated:
            if self.is_active(StageActivities.Moving):
                if Stage.close_enough(self.position, self.target):
                    self.is_moving = False
                else:
                    delta = min(abs(self.target - self.position), self.sim_delta_per_tick)
                    if self.target < self.position:
                        delta = -delta
                    self._position += delta
                    self.logger.debug(f'ontimer: position: {self.position}')
        else:
            hw_status = status_t()
            with self.stage_lock:
                result = ximclib.get_status(self.device, byref(hw_status))
            if result == Result.Ok:
                self._position = hw_status.CurPosition
                self.is_moving = hw_status.MvCmdSts & MvcmdStatus.MVCMD_RUNNING

        if not self.is_moving:
            if self.is_active(StageActivities.Moving) and Stage.close_enough(self.position, self.target):
                self.end_activity(StageActivities.Moving, self.logger)

            if (self.is_active(StageActivities.StartingUp) and
                    Stage.close_enough(self.position, self.presets[PresetPosition.Spectra])):
                self.end_activity(StageActivities.StartingUp, self.logger)

    @return_with_status
    def move(self, where: PresetPosition | str):
        """
        Starts moving the stage to one of the preset positions

        Parameters
        ----------
        where
            Name of a preset position

        :mastapi:
        """
        if not self.connected:
            return

        if isinstance(where, str):
            if where not in [pre.name for pre in PresetPosition]:
                self.logger.warning(f"No such preset position '{where}'")
                return
        preset_position = self.presets[stage_position_str2int_dict[where]]
        if Stage.close_enough(self.position, preset_position):
            self.logger.info(f'Not moving (current position:{self.position}) close enough to {preset_position})')
            return

        self.target = preset_position
        self.start_activity(StageActivities.Moving, self.logger)

        self.ticks_at_start = self.position
        self.motion_start_time = datetime.datetime.now()
        self.logger.info(f'move: at {self.position} started moving to {self.target}')

        if not self.simulated:
            try:
                with self.stage_lock:
                    response = ximclib.command_move(self.device, self.target, 0)
                if response != Result.Ok:
                    self.logger.error(f'Failed to start stage move (command_move({self.device}, {self.target}, 0)')
                    self.target = None
                    return
            except Exception as ex:
                self.logger.exception(f'Failed to start stage move (command_move({self.device}, {self.target}, 0)', ex)
                self.target = None

    @return_with_status
    def move_microns(self, direction: StageDirection | str, amount: int | str):
        """
        Starts moving the stage in the specified direction by the specified number of microns

        Parameters
        ----------
        direction
            The direction to move (**Up**: away from the motor, **Down**: towards the motor)
        amount
            How many microns to move
        :mastapi:
        """
        if self.simulated:
            self.logger.info('Simulated: not moving in microns')
            return

        if isinstance(direction, str):
            direction = StageDirection(stage_direction_str2int_dict[direction])
        if isinstance(amount, str):
            amount = abs(int(amount))

        amount *= 1 if direction == StageDirection.Up else -1
        try:
            self.target = self.position + amount
            self.start_activity(StageActivities.Moving, self.logger)
            with self.stage_lock:
                response = ximclib.command_movr(self.device, amount, 0)
            if response != Result.Ok:
                self.logger.error(f'Failed to start stage move (command_movr({self.device}, {amount})')
                return
        except Exception as ex:
            self.logger.exception(f'Failed to start stage move relative (command_movr({self.device}, {amount})', ex)

    def abort(self):
        """
        Aborts any in-progress stage activities

        :mastapi:
        Returns
        -------

        """
        for activity in (StageActivities.StartingUp, StageActivities.Moving, StageActivities.ShuttingDown):
            if self.is_active(activity):
                self.end_activity(activity, self.logger)

        if not self.simulated:
            ximclib.command_stop(self.device)
