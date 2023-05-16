import time
import win32com.client
from typing import TypeAlias
import logging

CameraType: TypeAlias = "Camera"


logger = logging.getLogger('mast.unit.camera')


class CameraStatus:

    def __init__(self, c: CameraType):
        self.is_exposing = c.exposing
        self.temperature = c.cam.CCDTemperature
        self.set_point = c.set_point


class Camera:

    _connected: bool = False
    _is_exposing: bool = False
    _set_point = 0

    def __init__(self, driver: str, set_point: float = None):
        try:
            self.cam = win32com.client.Dispatch(driver)
        except Exception as ex:
            print(ex)

        if set_point is not None:
            self._set_point = set_point
        logger.info('initialized')

    @property
    def set_point(self):
        return self._set_point

    @set_point.setter
    def set_point(self, value):
        self._set_point = value

    @property
    def connected(self) -> bool:
        if self.cam is not None:
            return self.cam.connected
        else:
            return False

    @connected.setter
    def connected(self, value: bool):
        if self.cam is not None:
            self.cam.connected = value

    def start_exposure(self, seconds: int, shutter: bool, readout_mode: int):
        if not self.connected:
            raise "Not Connected"

        if not self.cam.HasShutter:
            shutter = False

        # binning, gain?

        self.cam.StartExposure(seconds, shutter)
        self._is_exposing = True
        while not self.cam.ImageReady:
            time.sleep(1)
        self._is_exposing = False

    def stop_exposure(self):
        if self.cam.CanAbortExposure:
            try:
                self.cam.AbortExposure()
            except Exception as e:
                pass
        self._is_exposing = False

    def is_exposing(self):
        return self._is_exposing

    @property
    def exposing(self) -> bool:
        return self._is_exposing

    def status(self) -> CameraStatus:
        if not self.connected:
            self.connected = True
        return CameraStatus(self)

    def startup(self):
        """
        Performs startup procedure for the camera
        :return:
        """
        if not self.connected:
            logger.info(f'startup: connecting')
            self.connected = True

        self.set_point = -25
        # Set the Set Point
        if self.cam.CanSetCCDTemperature:
            logger.info(f'startup: setting set_point to {self.set_point:.1f}')
            self.cam.SetCCDTemperature = self.set_point

        # Turn on cooler
        if not self.cam.CoolerOn:
            logger.info(f'startup: cooler ON')
            self.cam.CoolerOn = True

        # Wait for camera temperature to reach the set point
        logger.info(f'startup: Temp: {self.cam.CCDTemperature:.1f}, set_point: {self.set_point:.1f}')
        while self.cam.CCDTemperature >= self.set_point:
            time.sleep(2)
            logger.info(f'startup: Temp: {self.cam.CCDTemperature:.1f}')

    def shutdown(self):
        """
        Performs startup procedure for the camera
        :return:
        """

        if not self.cam.Connected:
            logger.info(f'shutdown: connecting')
            self.cam.Connected = True

        # Turn on cooler
        if self.cam.CoolerOn:
            logger.info(f'shutdown: cooler OFF')
            self.cam.CoolerOn = False

        # Wait for camera temperature to reach the set point
        temp = self.cam.CCDTemperature
        while temp < 10:
            logger.info(f'shutdown: Temperature: {temp:.1f}')
            time.sleep(2)
            temp = self.cam.CCDTemperature

        if self.cam.CanSetCCDTemperature:
            logger.info(f'shutdown: setting set_point to {self.set_point:.1f}')
            self.cam.SetCCDTemperature(self.set_point)

        logger.info(f'shutdown: cooler OFF (Temp: {self.cam.CCDTemperature:.1f})')
        self.cam.CoolerOn = False
