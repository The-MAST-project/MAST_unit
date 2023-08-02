import logging
from enum import Enum, Flag
import datetime
from utils import RepeatTimer, return_with_status, Activities, init_log, TimeStamped
from typing import TypeAlias
from mastapi import Mastapi
from powered_device import PoweredDevice
import os
import sys
import platform

StageStateType: TypeAlias = "StageState"

cur_dir = os.path.abspath(os.path.dirname(__file__))  # Specifies the current directory.
ximc_dir = os.path.join(cur_dir, "Standa", "ximc-2.13.6", "ximc")  # Formation of the directory name with all dependencies. The dependencies for the examples are located in the ximc directory.
ximc_package_dir = os.path.join(ximc_dir, "crossplatform", "wrappers", "python")  # Formation of the directory name with python dependencies.
sys.path.append(ximc_package_dir)  # add pyximc.py wrapper to python path

if platform.system() == "Windows":
    # Determining the directory with dependencies for windows depending on the bit depth.
    arch_dir = "win64" if "64" in platform.architecture()[0] else "win32"  #
    lib_dir = os.path.join(ximc_dir, arch_dir)
    if sys.version_info >= (3, 8):
        os.add_dll_directory(lib_dir)
    else:
        os.environ["Path"] = lib_dir + ";" + os.environ["Path"]  # add dll path into an environment variable

from pyximc import Result,  EnumerateFlags, device_information_t, string_at, byref, MvcmdStatus, cast, POINTER, c_int, status_t, edges_settings_t

from pyximc import lib as ximclib


class StageActivities(Flag):
    Idle = 0
    StartingUp = (1 << 0)
    ShuttingDown = (1 << 1)
    MovingToScience = (1 << 2)
    MovingToSky = (1 << 3)
    MovingToMin = (1 << 4)
    MovingToMid = (1 << 5)
    MovingToMax = (1 << 6)
    MovingToTarget = (1 << 7)


class StageDirection(Enum):
    Up = 0
    Down = 1


class StageStatus(TimeStamped):
    is_powered: bool
    is_connected: bool
    is_operational: bool
    position: int
    state: StageStateType
    state_verbal: str
    activities: StageActivities
    activities_verbal: str
    api_methods: list
    reasons: list[str]


class StageState(Enum):
    Unknown = 0
    Idle = 1
    AtScience = 2
    AtSky = 3
    AtMin = 4
    AtMid = 5
    AtMax = 6
    MovingToScience = 7
    MovingToSky = 8
    MovingToMin = 9
    MovingToMid = 10
    MovingToMax = 11
    MovingToTarget = 12
    Error = 13


stage_state_str2int_dict: dict = {
    'Unknown': StageState.Unknown,
    'Idle': StageState.Idle,
    'AtScience': StageState.AtScience,
    'AtSky': StageState.AtSky,
    'AtMin': StageState.AtMin,
    'AtMid': StageState.AtMid,
    'AtMax': StageState.AtMax,
    'MovingToScience': StageState.MovingToScience,
    'MovingToSky': StageState.MovingToSky,
    'MovingToMin': StageState.MovingToMin,
    'MovingToMid': StageState.MovingToMid,
    'MovingToMax': StageState.MovingToMax,
    'MovingToTarget': StageState.MovingToTarget,
    'Error': StageState.Error,
}

stage_direction_str2int_dict: dict = {
    'Up': StageDirection.Up,
    'Down': StageDirection.Down,
}


class Stage(Mastapi, Activities, PoweredDevice):

    POINTING_PRECISION = 10     # If within this distance of target and not moving, we arrived

    logger: logging.Logger
    state: StageState
    default_initial_state: StageState
    ticks_at_start: int
    ticks_at_target: int
    motion_start_time: datetime
    timer: RepeatTimer
    device_uri: str
    _position: int | None = None
    is_moving: bool = False
    preset_positions: dict
    target: int | None = None

    def __init__(self):
        self.logger = logging.getLogger('mast.unit.stage')
        init_log(self.logger)

        PoweredDevice.__init__(self, 'Stage', self)

        self.device = None
        self.state = StageState.Unknown

        self.timer = RepeatTimer(1, function=self.ontimer)
        self.timer.name = 'stage-timer-thread'
        self.timer.start()
        self.activities = StageActivities.Idle

        # This is device search and enumeration with probing. It gives more information about devices.
        probe_flags = EnumerateFlags.ENUMERATE_PROBE
        enum_hints = b"addr="
        dev_enum = ximclib.enumerate_devices(probe_flags, enum_hints)

        dev_count = ximclib.get_device_count(dev_enum)
        device_info = "No device"
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

                    self.preset_positions = {
                        StageState.AtScience:   (100000, StageActivities.MovingToScience, StageState.MovingToScience),   # TBD read from Settings
                        StageState.AtSky:       (10000, StageActivities.MovingToSky, StageState.MovingToSky),       # TBD read from Settings
                        StageState.AtMin:       (self.min_travel, StageActivities.MovingToMin, StageState.MovingToMin),
                        StageState.AtMax:       (self.max_travel, StageActivities.MovingToMax, StageState.MovingToMax),
                        StageState.AtMid:       ((self.max_travel - self.min_travel) / 2, StageActivities.MovingToMid, StageState.MovingToMid),
                    }

                    device_info = "Port: {}, Manufacturer={}, Product={}, Version={}.{}.{}, Range={}..{}".format(
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

        self.logger.info(f'initialized ({device_info})')

    @property
    def connected(self) -> bool:
        return self.device is not None

    @connected.setter
    def connected(self, value):
        if not self.is_powered:
            return

        if value:
            dev = ximclib.open_device(self.device_uri)
            if dev != -1:
                self.device = dev
                self.state = StageState.Unknown
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
        if self.state is not StageState.AtScience:
            self.start_activity(StageActivities.StartingUp, self.logger)
            self.move(StageState.AtScience)

    @return_with_status
    def shutdown(self):
        """
        Shutdown routine for the **MAST** stage.  Makes it ``idle``

        :mastapi:
        """
        if not self.is_powered:
            return

        if not self.state == StageState.AtSky:
            self.start_activity(StageActivities.ShuttingDown, self.logger)
            self.move(StageState.AtSky)
        self.disconnect()
        self.power_off()

    @property
    def position(self) -> int | None:
        return self._position

    @position.setter
    def position(self, value):
        if self.connected:
            result = ximclib.command_move(self.device, value)
            if result != Result.Ok:
                raise Exception(f'Could not start move to {value}')

    def status(self) -> StageStatus:
        """
        Returns the status of the MAST stage
        :mastapi:
        """
        st = StageStatus()
        st.reasons = list()
        st.is_operational = False
        st.is_connected = False
        st.is_powered = self.is_powered
        if st.is_powered:
            st.is_connected = self.connected
            if st.is_connected:
                st.state = self.state
                st.is_operational = st.state == StageState.AtScience
                if not st.is_operational:
                    st.reasons.append(f'state is {st.state} instead of {StageState.AtScience}')
                st.state_verbal = st.state.name
                st.position = self.position
                st.activities = self.activities
                st.activities_verbal = st.activities.name
            else:
                st.reasons.append('not-connected')
        else:
            st.reasons.append('not-powered')
            st.reasons.append('not-connected')
        st.timestamp()
        return st

    @staticmethod
    def close_enough(position, target, epsilon):
        return abs(position - target) <= epsilon

    def ontimer(self):
        if not self.connected:
            return

        status = status_t()
        result = ximclib.get_status(self.device, byref(status))
        if result == Result.Ok:
            self._position = status.CurPosition
            self.is_moving = status.MvCmdSts & MvcmdStatus.MVCMD_RUNNING

            if not self.is_moving:
                if self.is_active(StageActivities.MovingToTarget) and Stage.close_enough(self.position, self.target, self.POINTING_PRECISION):
                    self.end_activity(StageActivities.MovingToTarget, self.logger)
                    self.state = StageState.Idle
                    return

                for state, tup in self.preset_positions.items():
                    target = tup[0]
                    activity = tup[1]
                    if not self.is_active(activity):
                        continue
                    if Stage.close_enough(self.position, target, self.POINTING_PRECISION):
                        self.state = state
                        self.end_activity(activity, self.logger)
                        break
                self.state = StageState.Idle

    @return_with_status
    def move(self, where: StageState | str):
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
            where = StageState(stage_state_str2int_dict[where])
        if self.state == where:
            self.logger.info(f'move: already {where}')
            return

        preset = self.preset_positions[where]
        target_position = preset[0]
        new_activity = preset[1]
        new_state = preset[2]
        self.state = new_state
        self.start_activity(new_activity, self.logger)

        self.ticks_at_start = self.position
        self.motion_start_time = datetime.datetime.now()
        self.logger.info(f'move: at {self.position} started moving to {target_position} (state={self.state})')
        try:
            response = ximclib.command_move(self.device, target_position, 0)
            if response != Result.Ok:
                self.logger.error(f'Failed to start stage move (command_move({self.device}, {target_position}, 0)')
                return
        except Exception as ex:
            self.logger.exception(f'Failed to start stage move (command_move({self.device}, {target_position}, 0)', ex)

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
        if isinstance(direction, str):
            direction = StageDirection(stage_direction_str2int_dict[direction])
        if isinstance(amount, str):
            amount = int(amount)

        amount *= 1 if direction == StageDirection.Up else -1
        try:
            response = ximclib.command_movr(self.device, amount, 0)
            if response != Result.Ok:
                self.logger.error(f'Failed to start stage move (command_movr({self.device}, {amount})')
                return
            self.target = self.position + amount
            self.state = StageState.MovingToTarget
            self.start_activity(StageActivities.MovingToTarget, self.logger)
        except Exception as ex:
            self.logger.exception(f'Failed to start stage move relative (command_movr({self.device}, {amount})', ex)
