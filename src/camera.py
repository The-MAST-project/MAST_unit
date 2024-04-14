import datetime
import socket

import win32com.client
from typing import List
import logging
import astropy.units as u
from enum import IntFlag, Enum
from threading import Thread

from common.utils import RepeatTimer, return_with_status, init_log
from common.ascom import AscomDriverInfo, ascom_run
from common.utils import path_maker, image_to_fits, Component
from common.config import Config
from dlipower.dlipower.dlipower import SwitchedPowerDevice
from mastapi import Mastapi


class CameraState(Enum):
    """
    Camera states as per https://ascom-standards.org/Help/Developer/html/T_ASCOM_DeviceInterface_CameraStates.htm
    """
    Idle = 0
    Waiting = 1
    Exposing = 2
    Reading = 3
    Download = 4
    Error = 5


class CameraActivities(IntFlag):
    Idle = 0
    CoolingDown = (1 << 0)
    WarmingUp = (1 << 1)
    Exposing = (1 << 2)
    ShuttingDown = (1 << 3)
    StartingUp = (1 << 4)
    ReadingOut = (1 << 5)


class CameraExposure:
    file: str | None
    seconds: float
    date: datetime

    def __init__(self):
        self.file = None
        self.seconds = 0
        self.date = None


class Camera(Mastapi, Component, SwitchedPowerDevice):

    def __init__(self):
        self.conf = Config().toml['camera']
        Component.__init__(self)
        self.logger: logging.Logger = logging.getLogger('mast.unit.camera')
        init_log(self.logger)
        try:
            self.ascom = win32com.client.Dispatch(self.conf['AscomDriver'])
        except Exception as ex:
            self.logger.exception(ex)
            raise ex

        SwitchedPowerDevice.__init__(self, self.conf)

        self.timer: RepeatTimer = RepeatTimer(1, function=self.ontimer)
        self.timer.name = 'camera-timer-thread'
        self.timer.start()
        self.logger.info('initialized')
        self.latest_exposure: None | CameraExposure = None

        self._connected: bool = False
        self._is_exposing: bool = False
        self.operational_set_point = -25
        self.warm_set_point = 5  # temperature at which the camera is considered warm
        self._image_width: int | None = None
        self._image_height: int | None = None
        self.PixelSizeX: int | None = None
        self.PixelSizeY: int | None = None
        self.NumX: int | None = None
        self.NumY: int | None = None
        self.RadX: float | None = None
        self.RadY: float | None = None
        self.image = None
        self.last_state: CameraState = CameraState.Idle

    @property
    def connected(self) -> bool:
        return self.ascom and ascom_run(self, 'Connected', no_entry_log=True)

    @connected.setter
    def connected(self, value: bool):
        if not self.is_on():
            return

        if self.ascom is not None:
            ascom_run(self, f'Connected = {value}')
        if value:
            self.PixelSizeX = ascom_run(self, 'PixelSizeX')
            self.PixelSizeY = ascom_run(self, 'PixelSizeY')
            self.NumX = ascom_run(self, 'NumX')
            self.NumY = ascom_run(self, 'NumY')
            self.RadX = (self.PixelSizeX * self.NumX * u.arcsec).to(u.rad).value
            self.RadY = (self.PixelSizeY * self.NumY * u.arcsec).to(u.rad).value
        self.logger.info(f'connected = {value}')

    @return_with_status
    def connect(self):
        """
        Connects to the **MAST** camera

        :mastapi:
        Returns
        -------

        """
        if self.is_on():
            self.connected = True

    @return_with_status
    def disconnect(self):
        """
        Disconnects from the **MAST* camera

        :mastapi:
        """
        if self.is_on():
            self.connected = False

    @return_with_status
    def start_exposure(self, seconds: int):
        """
        Starts a **MAST** camera exposure

        Parameters
        ----------
        seconds
            Exposure length in seconds

        :mastapi:
        """
        if self.latest_exposure is None:
            self.latest_exposure = CameraExposure()

        if self.connected:
            self.start_activity(CameraActivities.Exposing)
            self.image = None

            # readout mode, binning, gain?

            ascom_run(self, f'StartExposure({seconds}, True)')
            self.latest_exposure = CameraExposure()
            self.latest_exposure.seconds = seconds
            self.logger.info(f'exposure started (seconds={seconds})')

    @return_with_status
    def abort_exposure(self):
        """
        Aborts the current **MAST** camera exposure. No image readout.

        :mastapi:
        """
        if not self.connected:
            return

        if ascom_run(self, 'CanAbortExposure'):
            try:
                ascom_run(self, 'AbortExposure()')
            except Exception as ex:
                self.logger .exception(f'failed to stop exposure', ex)
        else:
            camera_name = ascom_run(self, 'Name')
            self.logger.info(f'ASCOM camera "{camera_name}" cannot stop exposure')
        self.end_activity(CameraActivities.Exposing)

    @return_with_status
    def stop_exposure(self):
        """
        Stops the current **MAST** camera exposure.  An image readout is initiated

        :mastapi:
        """
        if not self.connected:
            return

        if self.is_active(CameraActivities.Exposing):
            ascom_run(self, 'StopExposure()')  # the timer will read the image

    def status(self) -> dict:
        """
        Gets the **MAST** camera status

        :mastapi:
        Returns
        -------

        """
        ret = {
            'powered':  self.is_on(),
            'ascom':    AscomDriverInfo(self.ascom),
            'operational': self.operational,
            'why_not_operational': self.why_not_operational,
            'connected': self.connected,
            'activities': self.activities,
            'activities_verbal': self.activities.__repr__(),
        }
        if self.connected:
            ret['set_point'] = self.operational_set_point
            ret['temperature'] = self.ascom.CCDTemperature
            if self.latest_exposure:
                ret['latest_exposure'] = {}
                ret['latest_exposure']['file'] = self.latest_exposure.file
                ret['latest_exposure']['seconds'] = self.latest_exposure.seconds
                ret['latest_exposure']['date'] = self.latest_exposure.date
        ret['time_stamp'] = str(datetime.datetime.now())

        return ret

    @return_with_status
    def startup(self):
        """
        Starts the **MAST** camera up (cooling down , if needed)

        :mastapi:

        """
        self.start_activity(CameraActivities.StartingUp)
        if not self.is_on():
            self.power_on()
        if not self.connected:
            self.connect()
        if self.connected:
            ascom_run(self, 'CoolerOn = True')
            if abs(ascom_run(self, 'CCDTemperature') - self.operational_set_point) > 0.5:
                self.cooldown()

    @return_with_status
    def cooldown(self):
        if not ascom_run(self, 'Connected'):
            return

        self.start_activity(CameraActivities.CoolingDown)
        # Turn on cooler
        if not ascom_run(self, 'CoolerOn'):
            self.logger.info(f'cool-down: cooler ON')
            ascom_run(self, 'CoolerOn = True')

        if ascom_run(self, 'CanSetCCDTemperature'):
            self.logger.info(f'cool-down: setting set-point to {self.operational_set_point:.1f}')
            ascom_run(self, f'SetCCDTemperature = {self.operational_set_point}')

    @return_with_status
    def shutdown(self):
        """
        Shuts the **MAST** camera down (warms up, if needed)

        :mastapi:
        """
        if self.connected:
            self.start_activity(CameraActivities.ShuttingDown)
            if abs(ascom_run(self, 'CCDTemperature') - self.warm_set_point) > 0.5:
                self.warmup()

    @return_with_status
    def warmup(self):
        """
        Warms the **MAST** camera up, to prevent temperature shock
        """
        if not self.connected:
            return

        if ascom_run(self, 'CanSetCCDTemperature'):
            self.start_activity(CameraActivities.WarmingUp)
            temp = ascom_run(self, 'CCDTemperature')

            self.logger.info(
                f'warm-up started: current temp: {temp:.1f}, setting set-point to {self.warm_set_point:.1f}')
            ascom_run(self, f'SetCCDTemperature({self.warm_set_point})')

    def abort(self):
        """
        :mastapi:
        Returns
        -------

        """
        if self.is_active(CameraActivities.Exposing):
            ascom_run(self, 'AbortExposure()')
            self.end_activity(CameraActivities.Exposing)

    def ontimer(self):
        """
        Called by timer, checks if any ongoing activities have changed state
        """
        if not self.connected:
            return

        if self.last_state is None:
            self.last_state = ascom_run(self, 'CameraState', no_entry_log=True)
            self.logger.info(f'state changed from None to {CameraState(self.last_state)}')
        else:
            state = ascom_run(self, 'CameraState', no_entry_log=True)
            if not state == self.last_state:
                percent = ''
                if state == CameraState.Exposing or state == CameraState.Waiting or state == CameraState.Reading or \
                        state == CameraState.Download:
                    percent = f"{ascom_run(self, 'PercentCompleted')} %"
                self.logger.info(f'state changed from {CameraState(self.last_state)} to {CameraState(state)} {percent}')
                self.last_state = state

        if self.is_active(CameraActivities.Exposing) and ascom_run(self, 'ImageReady'):
            self.image = ascom_run(self, 'ImageArray', no_entry_log=True)
            if self.latest_exposure is None:
                self.latest_exposure = CameraExposure()
            if not self.latest_exposure.file:
                self.latest_exposure.file = path_maker.make_exposure_file_name()
            self.latest_exposure.date = datetime.datetime.now()
            header = {
                'SIMPLE': 'True',
                'DATE': datetime.datetime.utcnow().isoformat(),
                'NAXIS1': self.NumY,
                'NAXIS2': self.NumX,
                'EXPOSURE': self.latest_exposure.seconds,
                'INSTRUME': socket.gethostname(),
            }
            Thread(name='fits-saver-thread',
                   target=image_to_fits,
                   args=[
                    self.image,
                    self.latest_exposure.file,
                    header,
                    self.logger
                   ]).start()
            self.logger.info(f"image acquired (seconds={ascom_run(self, 'LastExposureDuration')})")
            self.end_activity(CameraActivities.Exposing)

        if self.is_active(CameraActivities.CoolingDown):
            temp = ascom_run(self, 'CCDTemperature')
            if temp <= self.operational_set_point:
                self.end_activity(CameraActivities.CoolingDown)
                self.end_activity(CameraActivities.StartingUp)
                self.logger.info(f'cool-down: done (temperature={temp:.1f}, set-point={self.operational_set_point})')

        if self.is_active(CameraActivities.WarmingUp):
            temp = ascom_run(self, 'CCDTemperature')
            if temp >= self.warm_set_point:
                ascom_run(self, 'CoolerOn = False')
                self.logger.info('turned cooler OFF')
                self.end_activity(CameraActivities.WarmingUp)
                self.end_activity(CameraActivities.ShuttingDown)
                self.logger.info(f'warm-up done (temperature={temp:.1f}, set-point={self.warm_set_point})')
                self.power_off()

    @property
    def operational(self) -> bool:
        if not all([self.switch.detected, self.is_on(), self.ascom, self.ascom.connected]):
            return False
        temp = ascom_run(self, 'CCDTemperature')
        if abs(temp - self.operational_set_point) > 0.5:
            return False
        return True

    @property
    def why_not_operational(self) -> List[str]:
        label = 'camera'
        ret = []
        if not self.switch.detected:
            ret.append(f"{label}: power switch '{self.switch.name}' (at '{self.switch.ipaddress}') not detected")
        elif not self.is_on():
            ret.append(f"{label}: not powered ON")
        elif not self.ascom:
            ret.append(f"{label}: no ASCOM attribute")
        elif not self.ascom.connected:
            ret.append(f"not ASCOM connected")
        else:
            temp = ascom_run(self, 'CCDTemperature')
            if abs(temp - self.operational_set_point) > 0.5:
                ret.append(f"{label}: temperature ({temp}) not within .5 degrees from {self.operational_set_point}")

        return ret

    @property
    def name(self) -> str:
        return 'camera'
