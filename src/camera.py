import datetime
import socket
import time
from logging import Logger

import win32com.client
from typing import List
import logging
import astropy.units as u
from enum import IntFlag, auto
from threading import Thread

from common.utils import RepeatTimer, time_stamp, BASE_UNIT_PATH
from common.ascom import ascom_run, AscomDispatcher
from common.utils import path_maker, image_to_fits, Component, CanonicalResponse
from common.config import Config
from dlipower.dlipower.dlipower import SwitchedPowerDevice

from fastapi.routing import APIRouter

logger = logging.getLogger('mast.unit.' + __name__)


class CameraState(IntFlag):
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
    CoolingDown = auto()
    WarmingUp = auto()
    Exposing = auto()
    ShuttingDown = auto()
    StartingUp = auto()
    ReadingOut = auto()


class CameraExposure:
    def __init__(self):
        self.file: str | None = None
        self.seconds: float | None = None
        self.date: datetime.datetime | None = None


class Camera(Component, SwitchedPowerDevice, AscomDispatcher):
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Camera, cls).__new__(cls)
        return cls._instance

    @property
    def logger(self) -> Logger:
        return logger

    @property
    def ascom(self) -> win32com.client.Dispatch:
        return self._ascom

    def __init__(self):
        self.defaults = {
            'temp_check_interval': 15,
        }

        self.conf = Config().toml['camera']
        Component.__init__(self)
        SwitchedPowerDevice.__init__(self, self.conf)

        if not self.is_on():
            self.power_on()

        try:
            self._ascom = win32com.client.Dispatch(self.conf['ascom_driver'])
        except Exception as ex:
            logger.exception(ex)
            raise ex

        self.latest_exposure: None | CameraExposure = None
        self.latest_temperature_check: datetime.datetime | None = None
        self.temp_check_interval = self.conf['temp_check_interval'] \
            if 'temp_check_interval' in self.conf else self.defaults['temp_check_interval']

        self._is_exposing: bool = False
        self.operational_set_point: float = -25
        self.warm_set_point: float = 5  # temperature at which the camera is considered warm
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
        self.errors: List[str] = []
        self.expected_mid_exposure: datetime.datetime | None = None
        self.ccd_temp_at_mid_exposure: float | None = None
        
        self._was_shut_down: bool = False

        self.timer: RepeatTimer = RepeatTimer(1, function=self.ontimer)
        self.timer.name = 'camera-timer-thread'
        self.timer.start()

        self._detected = False

        logger.info('initialized')

    @property
    def connected(self) -> bool:
        if not self.is_on() or not self._ascom:
            return False
        response = ascom_run(self, 'Connected')
        return response.value if response.succeeded else False

    @connected.setter
    def connected(self, value: bool):
        if not self.is_on() or not self._ascom:
            return

        response = ascom_run(self, f'Connected = {value}')
        if response.succeeded:
            if value:
                response = ascom_run(self, 'PixelSizeX')
                if response.succeeded:
                    self.PixelSizeX = response.value
                    self._detected = True

                response = ascom_run(self, 'PixelSizeY')
                if response.succeeded:
                    self.PixelSizeY = response.value

                response = ascom_run(self, 'NumX')
                if response.succeeded:
                    self.NumX = response.value

                response = ascom_run(self, 'NumY')
                if response.succeeded:
                    self.NumY = response.value

                self.RadX = (self.PixelSizeX * self.NumX * u.arcsec).to(u.rad).value
                self.RadY = (self.PixelSizeY * self.NumY * u.arcsec).to(u.rad).value
        else:
            logger.info(f"failed connected = {value} (failure='{response.failure}')")
        self._detected = value

    def connect(self):
        """
        Connects to the **MAST** camera

        :mastapi:
        Returns
        -------

        """
        self.connected = True
        return CanonicalResponse.ok

    def disconnect(self):
        """
        Disconnects from the **MAST* camera

        :mastapi:
        """
        self.connected = False
        return CanonicalResponse.ok

    def start_exposure(self, seconds: int | str):
        """
        Starts a **MAST** camera exposure

        Parameters
        ----------
        seconds
            Exposure length in seconds

        :mastapi:
        """
        if isinstance(seconds, str):
            seconds = int(seconds)
        self.errors = []
        if not self._ascom:
            self.errors.append(f"no ASCOM handle")
            return
        if not self.connected:
            self.errors.append(f"not connected")
            return

        response = ascom_run(self, f'StartExposure({seconds}, True)')
        if response.succeeded:
            self.start_activity(CameraActivities.Exposing)
            self.expected_mid_exposure = datetime.datetime.now() + datetime.timedelta(seconds=seconds/2)

            if self.latest_exposure is None:
                self.latest_exposure = CameraExposure()

            self.start_activity(CameraActivities.Exposing)
            self.image = None
            self.latest_exposure = CameraExposure()
            self.latest_exposure.seconds = seconds
            logger.info(f'exposure started (seconds={seconds})')
        else:
            if response.is_exception:
                self.errors.append(response.exception)
            if response.is_error:
                self.errors.append(response.errors)
        return CanonicalResponse(errors=self.errors) if self.errors else CanonicalResponse.ok

    def abort_exposure(self):
        """
        Aborts the current **MAST** camera exposure. No image readout.

        :mastapi:
        """
        self.errors = []
        if not self.connected:
            self.errors.append("not connected")
            return
        if not self.is_active(CameraActivities.Exposing):
            self.errors.append("not exposing")

        response = ascom_run(self, 'CanAbortExposure')
        if response.succeeded and response.value:
            response = ascom_run(self, 'AbortExposure()')
            if response.failed:
                self.errors.append(f"failed to abort (failure='{response.failure}')")
        self.end_activity(CameraActivities.Exposing)
        return CanonicalResponse(errors=self.errors) if self.errors else CanonicalResponse.ok

    def stop_exposure(self):
        """
        Stops the current **MAST** camera exposure.  An image readout is initiated

        :mastapi:
        """
        self.errors = []
        if not self.connected:
            self.errors.append("not connected")
            return
        if not self.is_active(CameraActivities.Exposing):
            self.errors.append("not exposing")

        response = ascom_run(self, 'StopExposure()')  # the timer will read the image
        if response.failed:
            self.errors.append(f"could not StopExposure(), (failure='{response.failure}')")
        return CanonicalResponse(errors=self.errors) if self.errors else CanonicalResponse.ok

    def status(self):
        """
        Gets the **MAST** camera status

        :mastapi:
        Returns
        -------

        """
        ret = self.power_status() | self.ascom_status() | self.component_status()
        ret |= {
            'errors': self.errors,
        }
        if self.connected:
            ret['set_point'] = self.operational_set_point
            ret['temperature'] = self._ascom.CCDTemperature
            ret['cooler_power'] = self._ascom.CoolerPower
            if self.latest_exposure:
                ret['latest_exposure'] = {}
                ret['latest_exposure']['file'] = self.latest_exposure.file
                ret['latest_exposure']['seconds'] = self.latest_exposure.seconds
                ret['latest_exposure']['date'] = self.latest_exposure.date
        time_stamp(ret)

        return ret

    def startup(self):
        """
        Starts the **MAST** camera up (cooling down , if needed)

        :mastapi:

        """
        # self.start_activity(CameraActivities.StartingUp)
        self.errors = []
        self.power_on()
        self.connect()
        if self.connected:
            response = ascom_run(self, 'CoolerOn = True')
            if response.failed:
                self.errors.append(f"could not set CoolerOn to True (failure='{response.failure}'")
        return CanonicalResponse(errors=self.errors) if self.errors else CanonicalResponse.ok

    def cooldown(self):
        if not self.connected:
            return

        response = ascom_run(self, 'CanSetCCDTemperature')
        if response.succeeded and response.value:
            self.start_activity(CameraActivities.CoolingDown)
            response = ascom_run(self, 'CanSetCCDTemperature')
            if response.succeeded:
                logger.info(f'cool-down: setting set-point to {self.operational_set_point:.1f}')
                response = ascom_run(self, f'SetCCDTemperature = {self.operational_set_point}')
                if response.failed:
                    logger.error(f"failed to set set-point (failure='{response.failure}')")

            response = ascom_run(self, 'CoolerOn')
            if response.succeeded and not response.value:
                response = ascom_run(self, 'CoolerOn = True')
                if response.failed:
                    logger.error(f"failed to set CoolerOn = True (failure='{response.failure}')")
                else:
                    logger.info(f'cool-down: turned cooler ON')
        return CanonicalResponse.ok

    def shutdown(self):
        """
        Shuts the **MAST** camera down (warms up, if needed)

        :mastapi:
        """
        # if self.connected:
        #     self.start_activity(CameraActivities.ShuttingDown)
        #     if abs(ascom_run(self, 'CCDTemperature') - self.warm_set_point) > 0.5:
        #         self.warmup()
        # else:
        #     self.power_off()
        self.errors = []
        if self.connected:
            response = ascom_run(self, 'CoolerOn = False')
            if response.failed:
                self.errors.append(f"could not set CoolerOn to False (failure='{response.failure}'")
            else:
                time.sleep(2)
        self.power_off()
        self._was_shut_down = True
        return CanonicalResponse(errors=self.errors) if self.errors else CanonicalResponse.ok

    def warmup(self):
        """
        Warms the **MAST** camera up, to prevent temperature shock
        """
        if not self.connected:
            return

        response = ascom_run(self, 'CanSetCCDTemperature')
        if response.succeeded and response.value:
            self.start_activity(CameraActivities.WarmingUp)
            response = ascom_run(self, 'CCDTemperature')
            temp = None
            if response.succeeded:
                temp = response.value

            response = ascom_run(self, f'SetCCDTemperature({self.warm_set_point})')
            if response.succeeded:
                message = 'warm-up started:'
                if temp:
                    message = message + f" current temp: {temp:.1f},"
                logger.info(
                    f'{message} setting set-point to {self.warm_set_point:.1f}')
            else:
                logger.error(f"could not set warm point (failure='{response.failure}')")

    def abort(self):
        """
        :mastapi:
        Returns
        -------

        """
        self.abort_exposure()
        return CanonicalResponse.ok

    def ontimer(self):
        """
        Called by timer, checks if any ongoing activities have changed state
        """
        if not self.connected:
            return

        now = datetime.datetime.now()
        if self.last_state is None:
            response = ascom_run(self, 'CameraState', no_entry_log=True)
            if response.succeeded:
                self.last_state = response.value
                logger.info(f'state changed from None to {CameraState(self.last_state)}')
        else:
            response = ascom_run(self, 'CameraState', no_entry_log=True)
            if response.succeeded:
                state = response.value
                if not state == self.last_state:
                    percent = ''
                    if (state == CameraState.Exposing or state == CameraState.Waiting or
                            state == CameraState.Reading or state == CameraState.Download):
                        response = ascom_run(self, 'PercentCompleted')
                        percent = f"{response.value} %" if response.succeeded else ''
                    logger.info(f'state changed from {CameraState(self.last_state)} to ' +
                                      f'{CameraState(state)} {percent}')
                    self.last_state = state

        if self.is_active(CameraActivities.Exposing):
            if now >= self.expected_mid_exposure:
                response = ascom_run(self, 'CCDTemperature')
                if response.succeeded:
                    self.ccd_temp_at_mid_exposure = response.value
                    self.expected_mid_exposure = None

            response = ascom_run(self, 'ImageReady')
            if response.succeeded and response.value:
                response = ascom_run(self, 'ImageArray', no_entry_log=True)
                if response.succeeded:
                    # we have an image array
                    self.image = response.value
                    if self.latest_exposure is None:
                        self.latest_exposure = CameraExposure()
                    if not self.latest_exposure.file:
                        self.latest_exposure.file = path_maker.make_exposure_file_name(camera='guiding')
                    self.latest_exposure.date = datetime.datetime.now()
                    header = {
                        'SIMPLE': 'True',
                        'DATE': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        'NAXIS1': self.NumY,
                        'NAXIS2': self.NumX,
                        'EXPOSURE': self.latest_exposure.seconds,
                        'INSTRUME': socket.gethostname(),
                    }
                    if self.ccd_temp_at_mid_exposure:
                        header['CCDTEMP'] = self.ccd_temp_at_mid_exposure
                        self.ccd_temp_at_mid_exposure = None

                    Thread(name='fits-saver-thread',
                           target=image_to_fits,
                           args=[
                            self.image,
                            self.latest_exposure.file,
                            header,
                            logger
                           ]).start()
                response = ascom_run(self, 'LastExposureDuration')
                if response.succeeded:
                    logger.info(f"image acquired (seconds={response.value})")
                self.end_activity(CameraActivities.Exposing)

        if (self.latest_temperature_check and
                (now - self.latest_temperature_check) >= datetime.timedelta(seconds=self.temp_check_interval)):
            response = ascom_run(self, 'CCDTemperature')
            if response.succeeded:
                ccd_temp = response.value
                response = ascom_run(self, 'CoolerPower')
                if response.succeeded:
                    cooler_power = response.value
                    logger.debug(f"{ccd_temp=}, {cooler_power=}")
        self.latest_temperature_check = now

        # if self.is_active(CameraActivities.CoolingDown):
        #     ccd_temp = ascom_run(self, 'CCDTemperature')
        #     # ambient_temp = ascom_run(self, 'HeatSinkTemperature')
        #     # logger.debug(f"{ambient_temp=}, {ccd_temp=}")
        #     if ccd_temp <= self.operational_set_point:
        #         self.end_activity(CameraActivities.CoolingDown)
        #         self.end_activity(CameraActivities.StartingUp)
        #         logger.info(f'cool-down: done ' +
        #           f'(temperature={ccd_temp:.1f}, set-point={self.operational_set_point})')

        # if self.is_active(CameraActivities.WarmingUp):
        #     ccd_temp = ascom_run(self, 'CCDTemperature')
        #     if ccd_temp >= self.warm_set_point:
        #         ascom_run(self, 'CoolerOn = False')
        #         logger.info('turned cooler OFF')
        #         self.end_activity(CameraActivities.WarmingUp)
        #         self.end_activity(CameraActivities.ShuttingDown)
        #         logger.info(f'warm-up done (temperature={ccd_temp:.1f}, set-point={self.warm_set_point})')
        #         self.power_off()

    @property
    def operational(self) -> bool:
        response = ascom_run(self, 'CoolerOn')

        return all([self.switch.detected, self.is_on(), self.detected, self._ascom,
                    self._ascom.connected, response.succeeded, response.value])

    @property
    def why_not_operational(self) -> List[str]:
        label = f'{self.name}'
        response = ascom_run(self, 'CoolerOn')

        ret = []
        if not self.switch.detected:
            ret.append(f"{label}: power switch '{self.switch.name}' (at '{self.switch.ipaddress}') not detected")
        elif not self.is_on():
            ret.append(f"{label}: not powered")
        elif not self.detected:
            ret.append(f"{label}: not detected")
        elif not self._ascom:
            ret.append(f"{label}: (ASCOM) - no handle")
        elif not self._ascom.connected:
            ret.append(f"{label}: (ASCOM) - not connected")
        elif not (response.succeeded and response.value):
            ret.append(f"{label}: (ASCOM) - cooler not ON")

        return ret

    @property
    def name(self) -> str:
        return 'camera'

    @property
    def detected(self) -> bool:
        return self._detected

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down


base_path = BASE_UNIT_PATH + "/camera"
tag = 'Camera'

camera = Camera()

router = APIRouter()
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=camera.startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=camera.shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=camera.abort)
router.add_api_route(base_path + '/status', tags=[tag], endpoint=camera.status)
router.add_api_route(base_path + '/connect', tags=[tag], endpoint=camera.connect)
router.add_api_route(base_path + '/disconnect', tags=[tag], endpoint=camera.disconnect)
router.add_api_route(base_path + '/start_exposure', tags=[tag], endpoint=camera.start_exposure)
router.add_api_route(base_path + '/stop_exposure', tags=[tag], endpoint=camera.stop_exposure)
router.add_api_route(base_path + '/abort_exposure', tags=[tag], endpoint=camera.abort_exposure)
