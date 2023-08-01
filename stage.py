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

from pyximc import Result,  EnumerateFlags, device_information_t, string_at, byref, MvcmdStatus, cast, POINTER, c_int, status_t

from pyximc import lib as ximclib


class StageActivities(Flag):
    Idle = 0
    StartingUp = (1 << 0)
    ShuttingDown = (1 << 1)
    MovingToScience = (1 << 2)
    MovingToSki = (1 << 3)


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
    MovingToScience = 4
    MovingToSky = 5
    Error = 6


stage_state_str2int_dict: dict = {
    'Unknown': StageState.Unknown,
    'Idle': StageState.Idle,
    'AtScience': StageState.AtScience,
    'AtSky': StageState.AtSky,
    'MovingToScience': StageState.MovingToScience,
    'MovingToSky': StageState.MovingToSky,
    'Error': StageState.Error,
}


class Stage(Mastapi, Activities, PoweredDevice):

    POINTING_PRECISION = 10     # If within this distance of target and not moving, we arrived
    POSITION_SCIENCE = 200000    # stage position when AtScience
    POSITION_SKY = 10000        # stage position when AtSky

    logger: logging.Logger
    state: StageState
    default_initial_state: StageState
    ticks_at_start: int
    ticks_at_target: int
    motion_start_time: datetime
    timer: RepeatTimer
    device_name: bytes
    _position: int | None = None
    is_moving: bool = False

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

        result = ximclib.set_bindy_key(os.path.join(ximc_dir, "win32", "keyfile.sqlite").encode("utf-8"))
        if result != Result.Ok:
            ximclib.set_bindy_key("keyfile.sqlite".encode("utf-8")) # Search for the key file in the current directory.

        # This is device search and enumeration with probing. It gives more information about devices.
        probe_flags = EnumerateFlags.ENUMERATE_PROBE + EnumerateFlags.ENUMERATE_NETWORK
        enum_hints = b"addr="
        dev_enum = ximclib.enumerate_devices(probe_flags, enum_hints)

        dev_count = ximclib.get_device_count(dev_enum)
        device_info = "No device"
        if dev_count > 0:
            self.device_name = ximclib.get_device_name(dev_enum, 0)
            dev = ximclib.open_device(self.device_name)
            if dev != -1:
                x_device_information = device_information_t()
                result = ximclib.get_device_information(dev, byref(x_device_information))
                if result == Result.Ok:
                    device_info = "Port: {}, Manufacturer={}, Product={}, Version={}.{}.{}".format(
                        self.device_name[self.device_name.find(b'COM'):].decode(),
                        repr(string_at(x_device_information.Manufacturer).decode()),
                        repr(string_at(x_device_information.ProductDescription).decode()),
                        repr(x_device_information.Major),
                        repr(x_device_information.Minor),
                        repr(x_device_information.Release)
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
            dev = ximclib.open_device(self.device_name)
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
                if Stage.close_enough(self.position, self.POSITION_SCIENCE, self.POINTING_PRECISION):
                    self.state = StageState.AtScience
                elif Stage.close_enough(self.position, self.POSITION_SKY, self.POINTING_PRECISION):
                    self.state = StageState.AtSky
                else:
                    self.state = StageState.Idle

        duration = None
        if self.is_active(StageActivities.StartingUp) and self.state == StageState.AtScience:
            self.end_activity(StageActivities.StartingUp, self.logger)
            duration = datetime.datetime.now() - self.motion_start_time

        if self.is_active(StageActivities.ShuttingDown) and self.state == StageState.AtSky:
            self.end_activity(StageActivities.ShuttingDown, self.logger)
            duration = datetime.datetime.now() - self.motion_start_time

        if self.is_active(StageActivities.MovingToSki) and self.state == StageState.AtSky:
            self.end_activity(StageActivities.MovingToSki, self.logger)
            duration = datetime.datetime.now() - self.motion_start_time

        if self.is_active(StageActivities.MovingToScience) and self.state == StageState.AtScience:
            self.end_activity(StageActivities.MovingToScience, self.logger)
            duration = datetime.datetime.now() - self.motion_start_time

        if duration:
            self.logger.info(f'motion duration: {duration}')

    @return_with_status
    def move(self, where: StageState | str):
        """
        Starts moving the stage to one of two pre-defined positions
        :mastapi:
        :param where: Where to move the stage to (either StageState.AtScience or StageState.AtSky)
        """
        if not self.connected:
            return

        if isinstance(where, str):
            where = StageState(stage_state_str2int_dict[where])
        if self.state == where:
            self.logger.info(f'move: already {where}')
            return

        if where == StageState.AtScience:
            self.state = StageState.MovingToScience
            self.start_activity(StageActivities.MovingToScience, self.logger)
            target = self.POSITION_SCIENCE
        else:
            self.state = StageState.MovingToSky
            self.start_activity(StageActivities.MovingToSki, self.logger)
            target = self.POSITION_SKY

        self.ticks_at_start = self.position
        self.motion_start_time = datetime.datetime.now()
        self.logger.info(f'move: at {self.position} started moving to {target} (state={self.state})')
        ximclib.command_move(self.device, target, 0)
