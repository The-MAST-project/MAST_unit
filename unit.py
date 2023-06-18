import datetime
import logging
import psutil

import utils
from PlaneWave import pwi4_client
from PlaneWave.platesolve import platesolve
import time
from typing import TypeAlias
import camera
import covers
import stage
import mount
import focuser
from power import Power, PowerStatus, PowerState, SocketId
from astropy.io import fits
from astropy.coordinates import Angle
import astropy.units as u
import tempfile
import numpy as np
from utils import return_with_status, Activities, RepeatTimer
from enum import Flag
from threading import Thread
from semaphore_win_ctypes import Semaphore
from multiprocessing.shared_memory import SharedMemory
from utils import parse_params, ensure_process_is_running, store_params, init_log
from camera import CameraActivities
import os
import subprocess

UnitType: TypeAlias = "Unit"

logger = logging.getLogger('mast.unit')
logger.setLevel(logging.DEBUG)
init_log(logger)


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
    reasons: dict
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
            self.reasons = dict()
            if self.power and self.power.reasons:
                self.reasons['power'] = self.power.reasons
            if self.camera and self.camera.reasons:
                self.reasons['camera'] = self.camera.reasons
            if self.mount and self.mount.reasons:
                self.reasons['mount'] = self.mount.reasons
            if self.stage and self.stage.reasons:
                self.reasons['stage'] = self.stage.reasons
            if self.covers and self.covers.reasons:
                self.reasons['covers'] = self.covers.reasons
            if self.focuser and self.focuser.reasons:
                self.reasons['focuser'] = self.focuser.reasons


class Unit(Activities):

    MAX_UNITS = 20

    _connected: bool = False
    _is_guiding: bool = False
    _is_autofocusing = False
    id = None
    activities: UnitActivities = UnitActivities.Idle

    reasons: list = []   # list of reasons for the last failure
    mount: mount
    covers: covers
    stage: stage
    focuser: focuser
    pw: pwi4_client.PWI4

    timer: RepeatTimer
    plate_solver_process: psutil.Process | subprocess.Popen

    # Stuff for plate solving
    image_params_shm: SharedMemory = None
    image_shm: SharedMemory = None
    results_shm: SharedMemory = None
    plate_solving_semaphore: Semaphore = None
    was_tracking_before_guiding: bool
    have_semaphore: bool = False

    GUIDING_EXPOSURE_SECONDS = 5
    GUIDING_INTER_EXPOSURE_SECONDS = 30

    def __init__(self, unit_id: int):
        if unit_id < 0 or unit_id > self.MAX_UNITS:
            raise f'Unit id must be between 0 and {self.MAX_UNITS}'

        self.plate_solving_semaphore = Semaphore(name='PlateSolving')
        self.plate_solving_semaphore.create()

        try:
            self.image_params_shm = SharedMemory(name='PlateSolving_Params')
        except FileNotFoundError:
            self.image_params_shm = SharedMemory(name='PlateSolving_Params', create=True, size=4096)

        try:
            self.results_shm = SharedMemory(name='PlateSolving_Results')
        except FileNotFoundError:
            self.results_shm = SharedMemory(name='PlateSolving_Results', create=True, size=4096)

        self.id = unit_id
        try:
            self.pw = pwi4_client.PWI4()
            self.camera = camera.Camera('ASCOM.PlaneWaveVirtual.Camera')
            self.covers = covers.Covers('ASCOM.PlaneWave.CoverCalibrator')
            self.focuser = focuser.Focuser('ASCOM.PWI4.Focuser')
            self.mount = mount.Mount()
            self.stage = stage.Stage()
        except Exception as ex:
            logger.exception(msg='could not create a Unit', exc_info=ex)
            raise ex

        self.timer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = 'unit-timer'
        self.timer.start()
        logger.info('initialized')

    def do_startup(self):
        self.start_activity(UnitActivities.StartingUp, logger)
        self.mount.startup()
        self.stage.startup()
        self.camera.startup()
        self.covers.startup()
        self.focuser.startup()

    @return_with_status
    def startup(self):
        """
        Starts the **MAST** ``unit`` subsystem.  Makes it ``operational``.

        Returns
        -------

        :mastapi:
        """
        # if not self.connected:
            # self.connect()

        if self.is_active(UnitActivities.StartingUp):
            return

        Thread(name='startup', target=self.do_startup).start()

    def do_shutdown(self):
        self.start_activity(UnitActivities.ShuttingDown, logger)
        self.mount.shutdown()
        self.covers.shutdown()
        self.camera.shutdown()
        self.stage.shutdown()
        self.focuser.shutdown()

    @return_with_status
    def shutdown(self):
        """
        Shuts down the **MAST** ``unit`` subsystem.  Makes it ``idle``.

        :mastapi:
        """
        if not self.connected:
            self.connect()

        if self.is_active(UnitActivities.ShuttingDown):
            return

        Thread(name='shutdown', target=self.do_shutdown).start()

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

        if value:
            # it's only at this stage that we know the imager size
            try:
                self.image_shm = SharedMemory(name='PlateSolving_Image')
            except FileNotFoundError:
                size = self.camera.ascom.NumX * self.camera.ascom.NumY * 4
                self.image_shm = SharedMemory(name='PlateSolving_Image', create=True, size=size)

    @return_with_status
    def connect(self):
        """
        Connects the **MAST** ``unit`` subsystems to all its ancillaries.

        :mastapi:
        """
        self.connected = True

    @return_with_status
    def disconnect(self):
        """
        Disconnects the **MAST** ``unit`` subsystems from all its ancillaries.

        :mastapi:
        """
        self.connected = False

    @return_with_status
    def start_autofocus(self):
        """
        Starts the ``autofocus`` routine (implemented by _PlaneWave_)

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
        Stops the ``autofocus`` routine

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
        Returns the status of the ``autofocus`` routine
        """
        if not self.connected:
            return False

        return self.pw.status().autofocus.is_running

    def acquire_semaphore(self):
        if self.have_semaphore:
            logger.info(f"already have semaphore '{self.plate_solving_semaphore.name}'")
            return

        sem = None
        logger.info(f"trying to acquire semaphore '{self.plate_solving_semaphore.name}'")
        while not sem:
            try:
                sem = self.plate_solving_semaphore.acquire(timeout_ms=500)
            except OSError:
                logger.info("timed out waiting for semaphore '{self.plate_solving_semaphore.name}' ...")
                time.sleep(.5)
        self.have_semaphore = True
        logger.info(f'acquired semaphore {self.plate_solving_semaphore.name}')

    def release_semaphore(self):
        self.plate_solving_semaphore.release()
        self.have_semaphore = False
        logger.info(f"released semaphore {self.plate_solving_semaphore.name}")

    def end_guiding(self):
        self.release_semaphore()
        self.plate_solver_process.kill()
        logger.info(f'guiding ended')

    def do_guide(self):

        proc = None
        pid_file = 'PlateSolveSimulator/.pid'
        if os.path.exists(pid_file):
            with open(pid_file, 'r') as f:
                pid = int(f.readline().strip())
            if psutil.pid_exists(pid):
                for proc in psutil.process_iter():
                    if proc.pid == pid:
                        break
            os.unlink(pid_file)

        if proc:
            self.plate_solver_process.kill()
            logger.info(f'killed existing plate solving process (pid={self.plate_solver_process.pid})')

        self.acquire_semaphore()    # prevent newly spawned process from acquiring it
        subprocess.Popen("c:/Users/User/PycharmProjects/MAST_unit/PlateSolveSimulator/run.bat",
                         cwd='C:/Users/User/PycharmProjects/MAST_unit/PlateSolveSimulator',
                         shell=True)
        self.plate_solver_process = utils.find_process(patt='PSSimulator')

        logger.info(f'plate solver process pid={self.plate_solver_process.pid}')

        last_ra: float = -1
        last_dec: float = -1

        while self.is_active(UnitActivities.Guiding):
            logger.info(f'starting {self.GUIDING_EXPOSURE_SECONDS} seconds guiding exposure')
            self.camera.start_exposure(seconds=self.GUIDING_EXPOSURE_SECONDS)
            while self.camera.is_active(CameraActivities.Exposing):
                if not self.is_active(UnitActivities.Guiding):
                    self.end_guiding()
                    return
                time.sleep(2)

            if not self.is_active(UnitActivities.Guiding):
                self.end_guiding()
                return

            logger.info(f'guiding exposure done, getting the image from the camera')
            if not self.image_shm:
                self.image_shm = SharedMemory(name='PlateSolving_Image', create=True,
                                              size=(self.camera.NumY * self.camera.NumX * 4))
            shared_image = np.ndarray((self.camera.NumX, self.camera.NumY),
                                      dtype=np.uint32, buffer=self.image_shm.buf)
            shared_image[:] = self.camera.image[:]
            logger.info(f'copied image to shared memory')

            self.acquire_semaphore()

            if not self.is_active(UnitActivities.Guiding):
                self.end_guiding()
                return

            pw_status = self.pw.status()
            # try to fool the plate solver by skewing ra and dec ?!?
            ra = pw_status.mount.ra_j2000_hours
            dec = pw_status.mount.dec_j2000_degs

            d = {
                'ra': ra,
                'dec': dec,
                'NumX': self.camera.NumX,
                'NumY': self.camera.NumY,
            }
            store_params(self.image_params_shm, d)

            # let the solver know it has a new job
            self.release_semaphore()

            # plate solver is now solving

            if not self.is_active(UnitActivities.Guiding):
                self.end_guiding()
                return

            # wait till the solver is done
            self.acquire_semaphore()

            logger.info('parsing plate solving result')
            results = parse_params(self.results_shm)
            self.release_semaphore()

            if 'succeeded' not in results.keys() or not results['succeeded']:
                logger.warning(f"plate solving failed")
                # TODO: what do we do?  try again?  bail out?

            if last_ra == -1 or last_dec == -1:  # first time?
                last_ra = results['ra']
                last_dec = results['dec']
                continue

            delta_ra = results['ra'] - last_ra      # mind sign and mount offset direction
            delta_dec = results['dec'] - last_dec   # ditto

            # tell the mount to correct by (delta_ra, delta_dec)
            logger.info(f'TODO: telling mount to correct by ra={delta_ra}, dec={delta_dec} ...')
            self.pw.mount_goto_ra_dec_j2000(ra + delta_ra, dec + delta_dec)
            pw_status = self.pw.status()
            while pw_status.mount.is_slewing:
                logger.info(f'waiting for mount to stop slewing ...')
                time.sleep(1)

            logger.info(f'mount stopped slewing')

            # avoid sleeping for a long time, for better agility at sensing that guiding was stopped
            td = datetime.timedelta(seconds=self.GUIDING_INTER_EXPOSURE_SECONDS)
            start = datetime.datetime.now()
            while (datetime.datetime.now() - start) <= td:
                if not self.is_active(UnitActivities.Guiding):
                    self.end_guiding()
                    return
                time.sleep(1)

    @return_with_status
    def start_guiding(self):
        """
        Starts the ``autoguide`` routine

        :mastapi:
        """
        if not self.connected:
            logger.warning('cannot start guiding - not-connected')
            return

        # if self.is_active(UnitActivities.Guiding):
            # return

        pw_stat = self.pw.status()
        self.was_tracking_before_guiding = pw_stat.mount.is_tracking
        if not self.was_tracking_before_guiding:
            self.pw.mount_tracking_on()
            logger.info('started mount tracking')

        self.start_activity(UnitActivities.Guiding, logger)
        Thread(name='guiding', target=self.do_guide).start()

    @return_with_status
    def stop_guiding(self):
        """
        Stops the ``autoguide`` routine

        :mastapi:
        """
        if not self.connected:
            logger.warning('Cannot stop guiding - not-connected')
            return

        if self.is_active(UnitActivities.Guiding):
            self.end_activity(UnitActivities.Guiding, logger)

        if self.plate_solver_process:
            self.plate_solver_process.kill()
            logger.info(f'killed plate solving process pid={self.plate_solver_process.pid}')

        if not self.was_tracking_before_guiding:
            self.mount.stop_tracking()
            logger.info('stopped tracking')

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
        Turn **ON** all power sockets

        :mastapi:
        """
        Power.all_on()

    @return_with_status
    def power_all_off(self):
        """
        Turn **OFF** all power sockets

        :mastapi:
        """
        Power.all_off()

    @return_with_status
    def power_on(self, socket_id: SocketId | str):
        """
        Turn power **ON** to the specified power socket

        Parameters
        ----------
        socket_id : int | str
            The socket to power **ON**.  Either the socket number or the socket name
        :mastapi:
        """
        if isinstance(socket_id, str):
            socket_id = SocketId(socket_id)
        Power.power(socket_id, PowerState.On)

    @return_with_status
    def power_off(self, socket_id: SocketId | str):
        """
        Turn power **OFF** to the specified power socket

        Parameters
        ----------
        socket_id : int | str
            The socket to power **OFF**.  Either the socket number or the socket name
        :mastapi:
        """
        if isinstance(socket_id, str):
            socket_id = SocketId(socket_id)
        Power.power(socket_id, PowerState.Off)

    def status(self) -> UnitStatus:
        """
        Returns
        -------
        UnitStatus
        :mastapi:
        """
        return UnitStatus(self)

    @return_with_status
    def test_solving(self, exposure_seconds: int | str):
        """
        Tests the ``platesolve`` routine
        :mastapi:
        Parameter
        ---------
        exposure_seconds int
            Exposure time in seconds
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

    def end_lifespan(self):
        logger.info('unit end lifespan')
        if self.plate_solver_process:
            self.plate_solver_process.kill()

        for shm in [self.image_shm, self.image_params_shm, self.results_shm]:
            shm.close()
            shm.unlink()
        self.plate_solving_semaphore.close()

        self.camera.ascom.Connected = False
        self.mount.ascom.Connected = False
        self.focuser.ascom.Connected = False

    @staticmethod
    def start_lifespan():
        logger.debug('unit start lifespan')
        ensure_process_is_running(pattern='PWI4',
                                  cmd='C:/Program Files (x86)/PlaneWave Instruments/PlaneWave Interface 4/PWI4.exe',
                                  logger=logger)
        ensure_process_is_running(pattern='PWShutter',
                                  cmd="C:/Program Files (x86)/PlaneWave Instruments/PlaneWave Shutter Control/PWShutter.exe",
                                  logger=logger)
