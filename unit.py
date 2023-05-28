import logging
from PlaneWave import pwi4_client
from PlaneWave.platesolve import platesolve
import time
from typing import TypeAlias
import camera
import covers
import stage
import mount
import power
from astropy.io import fits
from astropy.coordinates import Angle
import astropy.units as u
import tempfile
import os
import numpy as np
from utils import return_with_status

UnitType: TypeAlias = "Unit"

logger = logging.getLogger('mast.unit')

MAX_UNITS = 20


class UnitStatus:

    power: power.PowerStatus
    camera: camera.CameraStatus
    stage: stage.StageStatus
    mount: mount.MountStatus
    cover: covers.CoversStatus

    def __init__(self, u: UnitType):
        if u.power is not None:
            self.power = u.power.status()
        if u.power.is_on('Camera') and u.camera is not None:
            self.camera = u.camera.status()
        if u.power.is_on('Stage') and u.stage is not None:
            self.stage = u.stage.status()
        if u.power.is_on('Cover') and u.covers is not None:
            self.cover = u.covers.status()
        if u.power.is_on('Mount') and u.mount is not None:
            self.mount = u.mount.status()

        self.is_operational = \
            (self.power is not None and self.power.is_operational) and \
            (self.mount is not None and self.mount.is_operational) and \
            (self.camera is not None and self.camera.is_operational) and \
            (self.cover is not None and self.cover.is_operational) and \
            (self.stage is not None and self.stage.is_operational)

        self.is_guiding = u.guiding
        self.is_autofocusing = u.is_autofocusing
        self.is_connected = u.connected
        self.is_busy = self.is_autofocusing or self.is_guiding


class Unit:

    _connected: bool = False
    _is_guiding: bool = False
    _is_autofocusing = False
    id = None

    reasons: list = []   # list of reasons for the last query
    mount: mount
    covers: covers
    stage: stage
    power: power
    pw: pwi4_client.PWI4

    def __init__(self, unit_id: int):
        if unit_id < 0 or unit_id > MAX_UNITS:
            raise f'Unit id must be between 0 and {MAX_UNITS}'

        self.id = unit_id
        try:
            self.pw = pwi4_client.PWI4()
            self.camera = camera.Camera('ASCOM.PlaneWaveVirtual.Camera')
            self.covers = covers.Covers('ASCOM.PlaneWave.CoverCalibrator')
            self.mount = mount.Mount()
            self.stage = stage.Stage()
            self.power = power.Power(self.id)
            logger.info('initialized')
        except Exception as ex:
            logger.exception(ex)

    @return_with_status
    def startup(self):
        """
        Starts the **MAST** _unit_ subsystems.  Makes it _operational_
        :mastapi:
        :return:
        """
        if not self.connected:
            return

        self.power.startup()

        self.mount.connected = True
        self.camera.connected = True
        self.stage.connected = True
        self.covers.connected = True

        self.mount.startup()
        self.stage.startup()
        self.camera.startup()
        self.covers.startup()
        # return self.pw.status()

    @return_with_status
    def shutdown(self):
        """
        Shuts the **MAST** _unit_ subsystems down.  Makes it _idle_
        :mastapi:
        :return:
        """
        if not self.connected:
            raise 'Not connected'

        self.mount.shutdown()
        self.covers.shutdown()
        self.camera.shutdown()
        self.stage.shutdown()

        self.mount.connected = False
        self.camera.connected = False
        self.stage.connected = False
        self.covers.connected = False

        self.power.shutdown()

    @property
    def connected(self):
        self.reasons = []
        pw_status = self.pw.status()
        if not pw_status.mount.is_connected:
            self.reasons.append('Mount not connected')
        if not self.camera.connected:
            self.reasons.append('Camera not connected')
        if not self.stage.connected:
            self.reasons.append('Stage not connected')
        if not self.covers.connected:
            self.reasons.append('Covers not connected')

        return True if len(self.reasons) == 0 else False

    @connected.setter
    def connected(self, value):
        """
        Should connect/disconnect anything that needs connecting/disconnecting
        :param value:
        :return:
        """

        self.power.connected = True
        self.mount.connected = True
        self.camera.connected = True
        self.covers.connected = True
        self.stage.connected = True
        if not value == self._connected:
            self._connected = value

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
        :return:
        """
        self.connected = False

    @return_with_status
    def start_autofocus(self):
        """
        Starts the _autofocus_ routine (implemented by PlaneWave)
        :mastapi:
        :return:
        """
        if self.pw.status().autofocus.is_running:
            logger.info("autofocus already running")
            return
        self.pw.request("/autofocus/start")
        logger.info('autofocus started')

    @return_with_status
    def stop_autofocus(self):
        """
        Stops the _autofocus_ routine
        :mastapi:
        :return:
        """
        if not self.pw.status().autofocus.is_running:
            logger.info("autofocus not running")
            return
        self.pw.request("/autofocus/stop")
        logger.info('autofocus stopped')

    @property
    def is_autofocusing(self) -> bool:
        """
        Returns the status of the _autofocus_ routine
        :return: ``True`` if _autofocus_ is active, ``False`` otherwise
        """
        return self.pw.status().autofocus.is_running

    @return_with_status
    def start_guiding(self):
        """
        Starts the _autoguide_ routine
        :mastapi:
        :return:
        """
        self._is_guiding = True

    @return_with_status
    def stop_guiding(self):
        """
        Stops the _autoguide_ routine
        :mastapi:
        :return:
        .. seealso:: start_guiding, is_guiding
        """
        if self._is_guiding:
            self._is_guiding = False

    def is_guiding(self) -> bool:
        return self._is_guiding

    @property
    def guiding(self) -> bool:
        return self._is_guiding

    def status(self) -> UnitStatus:
        """
        :return The status of the ``unit`` subsystem:
        :rtype UnitStatus:
        :mastapi:
        """
        return UnitStatus(self)

    @return_with_status
    def test_solving(self, exposure_seconds: int):
        """
        Tests the plate solving routine
        :mastapi:
        :param exposure_seconds:
        :return:
        """
        if not self.camera.connected:
            raise Exception('Camera not connected')

        pw_stat = self.pw.request_with_status('/status')
        if not pw_stat.mount.is_connected:
            raise Exception('Mount not connected')
        if not pw_stat.mount.is_tracking:
            raise Exception('Mount is not tracking')

        ra = pw_stat.mount.ra_j2000_hours
        dec = pw_stat.dec_j2000_degs

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
