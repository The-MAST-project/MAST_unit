import win32com.client
from typing import TypeAlias
import logging
import astropy.units as u
from enum import Flag
from utils import AscomDriverInfo, RepeatTimer, return_with_status

CameraType: TypeAlias = "Camera"

logger = logging.getLogger('mast.unit.camera')


class CameraActivities(Flag):
    Idle = 0
    CoolingDown = (1 << 1)
    WarmingUp = (1 << 2)
    Exposing = (1 << 3)
    ShuttingDown = (1 << 4)
    StartingUp = (1 << 5)


class CameraStatus:

    activities: CameraActivities
    is_operational: bool
    temperature: float
    cooler_power: float # percent

    def __init__(self, c: CameraType):
        self.ascom = AscomDriverInfo(c.ascom)
        self.temperature = c.ascom.CCDTemperature
        self.cooler_power = c.ascom.CoolerPower
        self.is_operational = abs(self.temperature - c.operational_set_point) <= 0.5
        self.activities = c.activities
        self.activities_verbal = self.activities.name


class Camera:

    _connected: bool = False
    _is_exposing: bool = False
    operational_set_point = -25
    warm_set_point = 5  # temperature at which the camera is considered warm
    _image_width: int = None
    _image_height: int = None
    PixelSizeX: int
    PixelSizeY: int
    NumX: int
    NumY: int
    Xrad: float
    Yrad: float
    ascom = None
    activities: CameraActivities = CameraActivities.Idle
    timer: RepeatTimer
    image = None

    def __init__(self, driver: str, set_point: float = None):
        try:
            self.ascom = win32com.client.Dispatch(driver)
        except Exception as ex:
            logger.exception(ex)
            raise ex

        timer = RepeatTimer(2, function=self.ontimer)
        timer.name = 'mast.camera'
        timer.start()
        logger.info('initialized')

    @property
    def connected(self) -> bool:
        if self.ascom is not None:
            return self.ascom.connected
        else:
            return False

    @connected.setter
    def connected(self, value: bool):
        if self.ascom is not None:
            self.ascom.connected = value
        if value:
            self.PixelSizeX = self.ascom.PixelSizeX
            self.PixelSizeY = self.ascom.PixelSizeY
            self.NumX = self.ascom.NumX
            self.NumY = self.ascom.NumY
            self.Xrad = (self.PixelSizeX * self.NumX * u.arcsec).to(u.rad).value
            self.Yrad = (self.PixelSizeY * self.NumY * u.arcsec).to(u.rad).value
        logger.info(f'connected = {value}')

    @return_with_status
    def connect(self):
        self.connected = True

    @return_with_status
    def disconnect(self):
        self.connected = False

    @return_with_status
    def start_exposure(self, seconds: int, shutter: bool, readout_mode: int):
        if not self.connected:
            raise "Not Connected"

        self.activities |= CameraActivities.Exposing
        self.image = None
        if not self.ascom.HasShutter:
            shutter = False

        # readout mode, binning, gain?

        self.ascom.StartExposure(seconds, shutter)
        logger.info(f'exposure started (seconds={seconds}, shutter={shutter})')

    @return_with_status
    def stop_exposure(self):
        if self.ascom.CanAbortExposure:
            try:
                self.ascom.AbortExposure()
            except Exception as ex:
                logger .exception(f'failed to stop exposure', ex)
        else:
            logger.info(f'ASCOM camera "{self.ascom.Name}" cannot stop exposure')
        self.activities &= ~CameraActivities.Exposing

    def status(self) -> CameraStatus:
        return CameraStatus(self)

    @return_with_status
    def startup(self):
        if abs(self.ascom.CCDTemperature - self.operational_set_point) > 0.5:
            self.cooldown()

    @return_with_status
    def cooldown(self):
        if not self.ascom.Connected:
            return

        self.activities |= CameraActivities.CoolingDown
        # Turn on cooler
        if not self.ascom.CoolerOn:
            logger.info(f'cool-down: cooler ON')
            self.ascom.CoolerOn = True

        if self.ascom.CanSetCCDTemperature:
            logger.info(f'cool-down: setting set-point to {self.operational_set_point:.1f}')
            self.ascom.SetCCDTemperature = self.operational_set_point

    @return_with_status
    def shutdown(self):
        if abs(self.ascom.CCDTemperature - self.warm_set_point) > 0.5:
            self.warmup()

    @return_with_status
    def warmup(self):
        if not self.ascom.Connected:
            return

        if self.ascom.CanSetCCDTemperature:
            self.activities |= CameraActivities.WarmingUp
            temp = self.ascom.CCDTemperature

            logger.info(f'warm-up started: current temp: {temp:.1f}, setting set-point to {self.warm_set_point:.1f}')
            self.ascom.SetCCDTemperature(self.warm_set_point)

    def ontimer(self):
        """
        Called by timer, checks if any ongoing activities have changed state
        """
        if (self.activities & CameraActivities.Exposing) and self.ascom.ImageReady:
            self.activities &= ~CameraActivities.Exposing
            self.image = self.ascom.ImageArray
            logger.info(f'image acquired (open-shutter-time={self.ascom.LastExposureDuration})')

        if self.activities & CameraActivities.CoolingDown:
            temp = self.ascom.CCDTemperature
            if temp <= self.operational_set_point:
                self.activities &= ~CameraActivities.CoolingDown
                self.activities &= ~CameraActivities.StartingUp
                logger.info(f'cool-down: done (temperature={temp:.1f}, set-point={self.operational_set_point})')

        if self.activities & CameraActivities.WarmingUp:
            temp = self.ascom.CCDTemperature
            if temp >= self.warm_set_point:
                # self.ascom.CoolerOn = False
                self.activities &= ~CameraActivities.WarmingUp
                self.activities &= ~CameraActivities.ShuttingDown
                logger.info(f'warm-up done (temperature={temp:.1f}, set-point={self.warm_set_point})')
