import logging
from PlaneWave import pwi4_client
from PlaneWave.platesolve import platesolve
import time
from typing import TypeAlias
import camera
import covers
import stage
import mount
import focuser
from power import Power, PowerStatus, PowerState
from astropy.io import fits
from astropy.coordinates import Angle
import astropy.units as u
import tempfile
import os
import numpy as np
from utils import return_with_status, Activities, RepeatTimer
from enum import Flag

UnitType: TypeAlias = "Unit"

logger = logging.getLogger('mast.unit')

MAX_UNITS = 20


class UnitActivities(Flag):
    Idle = 0
    Autofocusing = (1 << 0)
    Guiding = (1 << 1)
    StartingUp = (1 << 2)
    ShuttingDown = (1 << 3)


class UnitStatus:

    power: PowerStatus
    camera: camera.CameraStatus
    stage: stage.StageStatus
    mount: mount.MountStatus
    covers: covers.CoversStatus
    focuser: focuser.FocuserStatus
    not_operational_because: dict
    activities: UnitActivities

    def __init__(self, unit: UnitType):
        self.power = Power.status()
        self.camera = unit.camera.status() if unit.camera is not None else None
        self.stage = unit.stage.status() if unit.stage is not None else None
        self.covers = unit.covers.status() if unit.covers is not None else None
        self.mount = unit.mount.status() if unit.mount is not None else None
        self.focuser = unit.focuser.status() if unit.focuser is not None else None

        self.is_operational = \
            (self.mount is not None and self.mount.is_operational) and \
            (self.camera is not None and self.camera.is_operational) and \
            (self.covers is not None and self.covers.is_operational) and \
            (self.focuser is not None and self.focuser.is_operational) and \
            (self.stage is not None and self.stage.is_operational)

        self.is_guiding = unit.guiding
        self.is_autofocusing = unit.is_autofocusing
        self.is_connected = unit.connected
        self.is_busy = self.is_autofocusing or self.is_guiding

        if not self.is_operational:
            self.not_operational_because = dict()
            if self.power and self.power.not_operational_because:
                self.not_operational_because['power'] = self.power.not_operational_because
            if self.camera and self.camera.not_operational_because:
                self.not_operational_because['camera'] = self.camera.not_operational_because
            if self.mount and self.mount.not_operational_because:
                self.not_operational_because['mount'] = self.mount.not_operational_because
            if self.stage and self.stage.not_operational_because:
                self.not_operational_because['stage'] = self.stage.not_operational_because
            if self.covers and self.covers.not_operational_because:
                self.not_operational_because['covers'] = self.covers.not_operational_because
            if self.focuser and self.focuser.not_operational_because:
                self.not_operational_because['focuser'] = self.focuser.not_operational_because


class Unit(Activities):

    _connected: bool = False
    _is_guiding: bool = False
    _is_autofocusing = False
    id = None
    activities: UnitActivities = UnitActivities.Idle

    not_operational_because: list = []   # list of not_operational_because for the last query
    mount: mount
    covers: covers
    stage: stage
    focuser: focuser
    pw: pwi4_client.PWI4

    timer: RepeatTimer

    def __init__(self, unit_id: int):
        if unit_id < 0 or unit_id > MAX_UNITS:
            raise f'Unit id must be between 0 and {MAX_UNITS}'

        self.id = unit_id
        try:
            self.pw = pwi4_client.PWI4()
            self.camera = camera.Camera('ASCOM.PlaneWaveVirtual.Camera')
            self.covers = covers.Covers('ASCOM.PlaneWave.CoverCalibrator')
            self.focuser = focuser.Focuser('ASCOM.PWI4.Focuser')
            self.mount = mount.Mount()
            self.stage = stage.Stage()
        except Exception as ex:
            logger.exception(ex)

        self.timer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = 'unit-timer'
        self.timer.start()
        logger.info('initialized')

    @return_with_status
    def startup(self):
        """
        Starts the **MAST** _unit_ subsystems.  Makes it _operational_
        :mastapi:
        """
        if not self.connected:
            self.connect()

        self.start_activity(UnitActivities.StartingUp, logger)
        self.mount.startup()
        self.stage.startup()
        self.camera.startup()
        self.covers.startup()
        self.focuser.startup()

    @return_with_status
    def shutdown(self):
        """
        Shuts the **MAST** _unit_ subsystems down.  Makes it _idle_
        :mastapi:
        """
        if not self.connected:
            self.connect()

        self.start_activity(UnitActivities.ShuttingDown, logger)
        self.mount.shutdown()
        self.covers.shutdown()
        self.camera.shutdown()
        self.stage.shutdown()
        self.focuser.shutdown()

    @property
    def connected(self):
        pw_status = self.pw.status()
        return pw_status.mount.is_connected and self.camera.connected and self.stage.connected and \
            self.covers.connected and self.focuser.connected

    @connected.setter
    def connected(self, value):
        """
        Should connect/disconnect anything that needs connecting/disconnecting
        """

        self.mount.connected = value
        self.camera.connected = value
        self.covers.connected = value
        self.stage.connected = value
        self.focuser.connected = value

    @return_with_status
    def connect(self):
        """
        Connects the **MAST** _unit_ subsystems to all its ancillaries.
        :mastapi:
        :return:
        """
        self.connected = True

    @return_with_status
    def disconnect(self):
        """
        Disconnects the **MAST** _unit_ subsystems from all its ancillaries.
        :mastapi:
        """
        self.connected = False

    @return_with_status
    def start_autofocus(self):
        """
        Starts the _autofocus_ routine (implemented by PlaneWave)
        :mastapi:
        """
        if not self.connected:
            logger.error('Cannot start autofocusing - not-connected')
            return

        if self.pw.status().autofocus.is_running:
            logger.info("autofocus already running")
            return

        self.start_activity(UnitActivities.Autofocusing, logger)
        self.pw.request("/autofocus/start")

    @return_with_status
    def stop_autofocus(self):
        """
        Stops the _autofocus_ routine
        :mastapi:
        """
        if not self.connected:
            logger.error('Cannot stop autofocusing - not-connected')
            return

        if not self.pw.status().autofocus.is_running:
            logger.info("Cannot stop autofocusing, it is not running")
            return
        self.pw.request("/autofocus/stop")
        self.end_activity(UnitActivities.Autofocusing, logger)

    @property
    def is_autofocusing(self) -> bool:
        """
        Returns the status of the _autofocus_ routine
        """
        if not self.connected:
            return False

        return self.pw.status().autofocus.is_running

    @return_with_status
    def start_guiding(self):
        """
        Starts the _autoguide_ routine
        :mastapi:
        """
        if not self.connected:
            logger.warning('Cannot start guiding - not-connected')
            return

        self.start_activity(UnitActivities.Guiding, logger)

    @return_with_status
    def stop_guiding(self):
        """
        Stops the _autoguide_ routine
        :mastapi:
        """
        if not self.connected:
            logger.warning('Cannot stop guiding - not-connected')
            return

        if self.is_active(UnitActivities.Guiding):
            self.end_activity(UnitActivities.Guiding, logger)

    def is_guiding(self) -> bool:
        if not self.connected:
            return False

        return self.is_active(UnitActivities.Guiding)

    @property
    def guiding(self) -> bool:
        return self.is_active(UnitActivities.Guiding)

    @return_with_status
    def power_all_on(self):
        """
        Turn ON all power sockets
        :mastapi:
        """
        Power.all_on()

    @return_with_status
    def power_all_off(self):
        """
        Turn OFF all power sockets
        :mastapi:
        """
        Power.all_off()

    @return_with_status
    def power_on(self, socket_id: int | str):
        """
        Turn power ON to the specified power socket
        :mastapi:
        """
        if isinstance(socket_id, str) and socket_id.isnumeric():
            socket_id = int(socket_id)
        Power.power(socket_id, PowerState.On)

    @return_with_status
    def power_off(self, socket_id: int | str):
        """
        Turn power OFF to the specified power socket
        :mastapi:
        """
        if isinstance(socket_id, str) and socket_id.isnumeric():
            socket_id = int(socket_id)
        Power.power(socket_id, PowerState.Off)

    def status(self) -> UnitStatus:
        """
        :return The status of the ``unit`` subsystem:
        :rtype UnitStatus:
        :mastapi:
        """
        return UnitStatus(self)

    @return_with_status
    def test_solving(self, exposure_seconds: int | str):
        """
        Tests the plate solving routine
        :mastapi:
        Parameter: int exposure_seconds: Exposure **time** in seconds
        """
        if not self.camera.connected:
            return

        pw_stat = self.pw.request_with_status('/status')
        if self.mount.connected:
            raise Exception('Mount not connected')
        if not pw_stat.mount.is_tracking:
            raise Exception('Mount is not tracking')

        ra = pw_stat.mount.ra_j2000_hours
        dec = pw_stat.dec_j2000_degs

        if isinstance(exposure_seconds, str):
            exposure_seconds = int(exposure_seconds)
        try:
            self.camera.start_exposure(exposure_seconds, True, readout_mode=0)
            time.sleep(.5)
        except Exception as ex:
            logger.exception('plate solve failed:', ex)

        while not self.camera.ascom.ImageReady:
            time.sleep(1)
        image = self.camera.ascom.ImageArray

        header = fits.Header()
        header['NAXIS'] = 2
        header['NAXIS1'] = image.shape[1]
        header['NAXIS2'] = image.shape[0]
        header['RA'] = Angle(ra * u.deg).value.tostring(decimal=False)
        header['DEC'] = Angle(dec * u.deg).value.tostring(decimal=False)
        hdu = fits.PrimaryHDU(data=image.astype(np.float32), header=header)

        fits_file = tempfile.TemporaryFile(mode='w', prefix='platesolve-', suffix='.fits')
        hdu.writeto(fits_file)

        result = platesolve(fits_file, self.camera.PixelSizeX)
        os.remove(fits_file)

        return result

    def ontimer(self):

        if self.is_active(UnitActivities.StartingUp):
            if not (self.mount.is_active(mount.MountActivity.StartingUp) or
                    self.camera.is_active(camera.CameraActivities.StartingUp) or
                    self.stage.is_active(stage.StageActivities.StaringUp) or
                    self.focuser.is_active(focuser.FocuserActivities.StartingUp) or
                    self.covers.is_active(covers.CoverActivities.StartingUp) or
                    self.mount.is_active(mount.MountActivity.StartingUp)):
                self.end_activity(UnitActivities.StartingUp, logger)
                
        if self.is_active(UnitActivities.ShuttingDown):
            if not (self.mount.is_active(mount.MountActivity.ShuttingDown) or
                    self.camera.is_active(camera.CameraActivities.ShuttingDown) or
                    self.stage.is_active(stage.StageActivities.StaringUp) or
                    self.focuser.is_active(focuser.FocuserActivities.ShuttingDown) or
                    self.covers.is_active(covers.CoverActivities.ShuttingDown) or
                    self.mount.is_active(mount.MountActivity.ShuttingDown)):
                self.end_activity(UnitActivities.ShuttingDown, logger)
                