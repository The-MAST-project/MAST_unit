import logging
import threading
from enum import IntEnum, IntFlag, auto
import datetime
from typing import List

from common.utils import RepeatTimer, Component, time_stamp, CanonicalResponse, BASE_UNIT_PATH
from common.config import Config
from mastapi import Mastapi
from dlipower.dlipower.dlipower import SwitchedPowerDevice, make_power_conf
import os
import sys
import platform
from fastapi.routing import APIRouter

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

logger = logging.getLogger('mast.unit.' + __name__)


class StageActivities(IntFlag):
    Idle = 0
    StartingUp = auto()
    ShuttingDown = auto()
    Moving = auto()


class StageDirection(IntEnum):
    Up = auto()   
    Down = auto()


class PresetPosition(IntEnum):
    Image = auto(),
    Spectra = auto(),
    Min = auto(),
    Middle = auto(),
    Max = auto(),


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


class Stage(Mastapi, Component, SwitchedPowerDevice):
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Stage, cls).__new__(cls)
        return cls._instance

    _positioning_precision: int = 100

    def __init__(self):
        self.unit_conf: dict = Config().get_unit()
        self.conf = self.unit_conf['stage']
        # logger: logging.Logger = logging.getLogger('mast.unit.stage')
        # init_log(logger)

        SwitchedPowerDevice.__init__(self, power_switch_conf=self.unit_conf['power_switch'], outlet_name='Stage')
        Component.__init__(self)

        self.device = None
        self.ticks_at_start: int | None = None
        self.ticks_at_target: int | None = None
        self.motion_start_time: datetime.datetime | None = None
        self.timer: RepeatTimer | None = None
        self.device_uri: str | None = None
        self._position: int | None = None
        self.is_moving: bool = False
        self.target: int | None = None
        self.stage_lock: threading.Lock | None = None
        self.presets: dict
        self.min_travel: int | None = None
        self.max_travel: int | None = None

        self.info = {}
        self._was_shut_down = False

        if not self.is_on():
            self.power_on()

        # This is device search and enumeration with probing. It gives more information about devices.
        probe_flags = EnumerateFlags.ENUMERATE_PROBE | EnumerateFlags.ENUMERATE_ALL_COM
        enum_hints = b"addr="
        dev_enum = ximclib.enumerate_devices(probe_flags, enum_hints)
        self.presets = {}

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
                    self.min_travel = x_edges_settings.LeftBorder
                    self.max_travel = x_edges_settings.RightBorder

                    self.info['port'] = comport
                    self.info['controller'] = repr(string_at(x_device_information.Manufacturer).decode())
                    self.info['product'] = repr(string_at(x_device_information.ProductDescription).decode())
                    self.info['version'] = (f"{repr(x_device_information.Major)}.{repr(x_device_information.Minor)}" +
                                            f".{repr(x_device_information.Release)}")
                    self.info['travel'] = {
                        'min': self.min_travel,
                        'max': self.max_travel,
                    }

                    self.device_info = "Port: {}, Manufacturer={}, Product={}, Version={}, Range={}..{}".format(
                        comport,
                        self.info['controller'],
                        self.info['product'],
                        self.info['version'],
                        self.min_travel,
                        self.max_travel,
                    )
                # ximclib.close_device(byref(cast(dev, POINTER(c_int))))
                self.device = dev
                self.stage_lock = threading.Lock()

        image_position = self.conf['image_position']
        spectra_position = self.conf['spectra_position']

        if self.device is not None:
            self.presets = {
                PresetPosition.Min: self.min_travel,
                PresetPosition.Max: self.max_travel,
                PresetPosition.Middle: int((self.max_travel - self.min_travel) / 2),
                PresetPosition.Image: image_position,
                PresetPosition.Spectra: spectra_position,
            }

            # get initial values from the hardware
            hw_status = status_t()
            with self.stage_lock:
                result = ximclib.get_status(self.device, byref(hw_status))
            if result == Result.Ok:
                self._position = hw_status.CurPosition
                self.is_moving = hw_status.MvCmdSts & MvcmdStatus.MVCMD_RUNNING

            self.timer = RepeatTimer(2, function=self.ontimer)
            self.timer.name = 'stage-timer-thread'
            self.timer.start()
            logger.info(f'initialized ({self.device_info})')
        else:
            logger.error(f"no device detected")

    @property
    def connected(self) -> bool:
        return self.device is not None

    @connected.setter
    def connected(self, value):

        if not self.is_on():
            return

        if value:
            dev = ximclib.open_device(self.device_uri)
            self.device = dev if dev != -1 else None
        else:
            ximclib.close_device(byref(cast(self.device, POINTER(c_int))))
            self.device = None

        logger.info(f'connected = {value} => {self.connected}')

    def connect(self):
        """
        Connects to the **MAST** stage controller

        :mastapi:
        """

        if not self.is_on():
            self.power_on()
        self.connected = True
        return CanonicalResponse.ok

    def disconnect(self):
        """
        Disconnects from the **MAST** stage controller

        :mastapi:
        """

        if self.is_on():
            self.connected = False
        return CanonicalResponse.ok

    def startup(self):
        """
        Startup routine for the **MAST** stage.  Makes it ``operational``:
        * If not powered, powers it ON
        * If not connected, connects to the controller
        * If the stage is not at operational position, it is moved

        :mastapi:
        """

        if not self.is_on():
            self.power_on()
        if not self.connected:
            self.connect()
        self._was_shut_down = False
        if self.at_preset != 'Spectra':
            self.start_activity(StageActivities.StartingUp)
            self.move_to_preset(PresetPosition.Spectra)
        return CanonicalResponse.ok

    def shutdown(self):
        """
        Shutdown routine for the **MAST** stage.  Makes it ``idle``

        :mastapi:
        """
        self.disconnect()
        self.power_off()
        self._was_shut_down = True
        return CanonicalResponse.ok

    @property
    def at_preset(self) -> str | None:
        current_position = self.position
        if current_position is not None:
            for preset, pos in self.presets.items():
                if self.close_enough(pos):
                    return preset.name
        return None

    @property
    def position(self) -> int | None:
        return self._position

    @position.setter
    def position(self, value):
        if not self.connected:
            raise Exception('Not connected')

        if self.close_enough(value):
            logger.info(f'Not changing position ({self.position} is close enough to {value}')
            return

        self.target = value
        with self.stage_lock:
            result = ximclib.command_move(self.device, value)
        if result == Result.Ok:
            self.start_activity(StageActivities.Moving)
        else:
            raise Exception(f'Could not start move to {value}')

    def status(self) -> dict:
        """
        Returns the status of the MAST stage
        :mastapi:
        """
        ret = self.power_status() | self.component_status()
        presets = {}
        if self.detected:
            for k, v in self.presets.items():
                presets[k.name] = v
        ret |= {
            'info': self.info,
            'presets': presets,
            'position': self.position if self.connected else None,
            'at_preset': self.at_preset,
        }
        time_stamp(ret)
        return ret

    def close_enough(self, target):
        if self._position is None or target is None:
            print(f"close_enough: {self._position=}, {target=}")
        return abs(self._position - target) <= 2

    def ontimer(self):
        if not self.connected:
            return

        hw_status = status_t()
        with self.stage_lock:
            result = ximclib.get_status(self.device, byref(hw_status))
        if result == Result.Ok:
            self._position = hw_status.CurPosition
            self.is_moving = hw_status.MvCmdSts & MvcmdStatus.MVCMD_RUNNING

        if not self.is_moving:
            if self.is_active(StageActivities.Moving) and self.close_enough(self.target):
                self.end_activity(StageActivities.Moving)

            if (self.is_active(StageActivities.StartingUp) and
                    self.close_enough(self.presets[PresetPosition.Spectra])):
                self.end_activity(StageActivities.StartingUp)

    #
    def move_to_preset(self, preset: PresetPosition | str):
        """
        Starts moving the stage to one of the preset positions

        Parameters
        ----------
        preset
            Name of a preset position

        :mastapi:
        """
        if not self.detected or not self.connected:
            return

        if isinstance(preset, str):
            try:
                preset = PresetPosition.__getitem__(preset)
            except KeyError:
                logger.warning(f"No such preset position '{preset}'")
                return

        preset_position = self.presets[preset]
        if self.close_enough(preset_position):
            logger.info(f'Not moving (current position:{self.position}) close enough to {preset_position})')
            return

        self.target = preset_position
        self.start_activity(StageActivities.Moving)

        self.ticks_at_start = self.position
        self.motion_start_time = datetime.datetime.now()
        logger.info(f'move: at {self.position} started moving to {self.target}')

        try:
            with self.stage_lock:
                response = ximclib.command_move(self.device, self.target, 0)
            if response != Result.Ok:
                self.target = None
                msg = f'Failed to start stage move (command_move({self.device}, {self.target}, 0)'
                logger.error(msg)
                return CanonicalResponse(errors=msg)
        except Exception as ex:
            self.target = None
            msg = f'Failed to start stage move (command_move({self.device}, {self.target}, 0)'
            logger.exception(msg, ex)
            return CanonicalResponse(exception=ex)
        return CanonicalResponse.ok

    def move_microns(self, direction: StageDirection | str, microns: int | str):
        """
        Starts moving the stage in the specified direction by the specified number of microns

        Parameters
        ----------
        direction
            The direction to move (**Up**: away from the motor, **Down**: towards the motor)
        microns
            How many microns to move
        :mastapi:
        """

        if isinstance(direction, str):
            direction = StageDirection(stage_direction_str2int_dict[direction])
        if isinstance(microns, str):
            microns = abs(int(microns))

        microns *= 1 if direction == StageDirection.Up else -1
        try:
            self.target = self.position + microns
            self.start_activity(StageActivities.Moving)
            with self.stage_lock:
                response = ximclib.command_movr(self.device, microns, 0)
            if response != Result.Ok:
                msg = f'Failed to start stage move (command_movr({self.device}, {microns})'
                logger.error(msg)
                return CanonicalResponse(errors=msg)
        except Exception as ex:
            msg = f'Failed to start stage move relative (command_movr({self.device}, {microns})'
            logger.exception(msg, ex)
            return CanonicalResponse(exception=ex)
        return CanonicalResponse.ok

    def move_native(self, direction: StageDirection | str, amount: int | str):
        """
        Starts moving the stage in the specified direction by the specified number of native units

        Parameters
        ----------
        direction
            The direction to move (**Up**: away from the motor, **Down**: towards the motor)
        amount
            How many units to move
        :mastapi:
        """

        if isinstance(direction, str):
            direction = StageDirection(stage_direction_str2int_dict[direction])
        if isinstance(amount, str):
            amount = abs(int(amount))

        amount *= 1 if direction == StageDirection.Up else -1
        try:
            self.target = self.position + amount
            self.start_activity(StageActivities.Moving)
            with self.stage_lock:
                response = ximclib.command_movr(self.device, amount, 0)
            if response != Result.Ok:
                msg = f'Failed to start stage move (command_movr({self.device}, {amount})'
                logger.error(msg)
                return CanonicalResponse(errors=msg)
        except Exception as ex:
            msg = f'Failed to start stage move relative (command_movr({self.device}, {amount})'
            logger.exception(msg, ex)
            return CanonicalResponse(exception=ex)
        return CanonicalResponse.ok

    def abort(self):
        """
        Aborts any in-progress stage activities

        :mastapi:
        Returns
        -------

        """
        for activity in (StageActivities.StartingUp, StageActivities.Moving, StageActivities.ShuttingDown):
            if self.is_active(activity):
                self.end_activity(activity)

        ximclib.command_stop(self.device)
        return CanonicalResponse.ok

    @property
    def name(self) -> str:
        return 'stage'

    @property
    def operational(self) -> bool:
        return all([self.is_on(), self.device, self.connected, not self.was_shut_down,
                    (self.at_preset == 'Spectra' or self.at_preset == 'Image')])

    @property
    def why_not_operational(self) -> List[str]:
        label = f'{self.name}'
        ret = []
        if not self.is_on():
            ret.append(f"{label}: not powered")
        else:
            if not self.device:
                ret.append(f"{label}: not detected")
            if self.was_shut_down:
                ret.append(f"{label}: shut down")
            if not self.connected:
                ret.append(f"{label}: not connected")
            elif not (self.at_preset == 'Spectra' or self.at_preset == 'Image'):
                ret.append(f"not at 'Spectra' or 'Image' preset positions")
        return ret

    @property
    def detected(self) -> bool:
        return self.device is not None

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down


base_path = BASE_UNIT_PATH + "/stage"
tag = 'Stage'

stage = Stage()

router = APIRouter()
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=stage.startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=stage.shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=stage.abort)
router.add_api_route(base_path + '/status', tags=[tag], endpoint=stage.status)
router.add_api_route(base_path + '/connect', tags=[tag], endpoint=stage.connect)
router.add_api_route(base_path + '/disconnect', tags=[tag], endpoint=stage.disconnect)
router.add_api_route(base_path + '/move_native', tags=[tag], endpoint=stage.move_native)
router.add_api_route(base_path + '/move_microns', tags=[tag], endpoint=stage.move_microns)
router.add_api_route(base_path + '/move_to_preset', tags=[tag], endpoint=stage.move_to_preset)
