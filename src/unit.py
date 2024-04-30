import datetime
from itertools import chain
import logging
import socket
import psutil
import guiding
from PlaneWave import pwi4_client
import time
from typing import List
from camera import Camera, CameraActivities
from covers import Covers, CoverActivities
from stage import Stage, StageActivities
from mount import Mount, MountActivities
from focuser import Focuser, FocuserActivities
from dlipower.dlipower.dlipower import SwitchedPowerDevice
from astropy.io import fits
from astropy.coordinates import Angle
import astropy.units as u
import numpy as np
from common.utils import RepeatTimer
from enum import IntFlag, auto
from threading import Thread
from multiprocessing.shared_memory import SharedMemory
from common.utils import ensure_process_is_running, init_log, Component, path_maker
from common.utils import find_process, time_stamp, CanonicalResponse
import os
import subprocess
from enum import Enum
import json

GUIDING_SHM_NAME = 'PlateSolving_Image'


class GuideDirections(Enum):
    guideNorth = 0
    guideSouth = 1
    guideEast = 2
    guideWest = 3


class SolverResponse:
    solved: bool
    reason: str
    ra: float
    dec: float


class AutofocusResult:
    success: bool
    best_position: float
    tolerance: float
    time_stamp: str


class UnitActivities(IntFlag):
    Idle = 0
    Autofocusing = auto()
    Guiding = auto()
    StartingUp = auto()
    ShuttingDown = auto()


class Unit(Component):

    GUIDING_EXPOSURE_SECONDS = 5
    GUIDING_INTER_EXPOSURE_SECONDS = 30
    MAX_UNITS = 20

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Unit, cls).__new__(cls)
        return cls._instance

    def __init__(self, unit_id: int):
        Component.__init__(self)

        self._connected: bool = False
        self._is_guiding: bool = False
        self._is_autofocusing: bool = False
        self.shm: SharedMemory | None = None

        self.plate_solver_process: psutil.Process | subprocess.Popen | None = None

        # Stuff for plate solving
        self.was_tracking_before_guiding: bool = False
        self.sock_to_solver: socket = None

        self.logger: logging.Logger = logging.getLogger('mast.unit')
        init_log(self.logger)

        if not 1 <= unit_id <= Unit.MAX_UNITS:
            raise f"Bad unit id '{unit_id}', must be in [1..{Unit.MAX_UNITS}]"

        self.id = unit_id
        try:
            self.mount: Mount = Mount()
            self.camera: Camera = Camera()
            self.covers: Covers = Covers()
            self.stage: Stage = Stage()
            self.focuser: Focuser = Focuser()
            self.pw: pwi4_client.PWI4 = pwi4_client.PWI4()
        except Exception as ex:
            self.logger.exception(msg='could not create a Unit', exc_info=ex)
            raise ex

        self.components: List[Component] = [
            self.mount,
            self.camera,
            self.covers,
            self.focuser,
            self.stage,
        ]

        self.timer: RepeatTimer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = 'unit-timer-thread'
        self.timer.start()

        self.shm = None
        self.autofocus_result: AutofocusResult | None = None
        log_filename = os.path.join(path_maker.make_daily_folder_name(), 'log.txt')
        self.logger.info('initialized')
        self.logger.info(f'logging to {log_filename}')

    def do_startup(self):
        self.start_activity(UnitActivities.StartingUp)
        [comp.startup() for comp in self.components]

    def startup(self):
        """
        Starts the **MAST** ``unit`` subsystem.  Makes it ``operational``.

        Returns
        -------

        :mastapi:
        """
        if self.is_active(UnitActivities.StartingUp):
            return

        Thread(name='startup-thread', target=self.do_startup).start()
        return CanonicalResponse.ok

    def do_shutdown(self):
        self.start_activity(UnitActivities.ShuttingDown)
        [comp.shutdown() for comp in self.components]

    def shutdown(self):
        """
        Shuts down the **MAST** ``unit`` subsystem.  Makes it ``idle``.

        :mastapi:
        """
        if not self.connected:
            self.connect()

        if self.is_active(UnitActivities.ShuttingDown):
            return

        Thread(name='shutdown-thread', target=self.do_shutdown).start()
        return CanonicalResponse.ok

    @property
    def connected(self):
        return all([comp.connected for comp in self.components])

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

    def connect(self):
        """
        Connects the **MAST** ``unit`` subsystems to all its ancillaries.

        :mastapi:
        """
        self.connected = True
        return CanonicalResponse.ok

    def disconnect(self):
        """
        Disconnects the **MAST** ``unit`` subsystems from all its ancillaries.

        :mastapi:
        """
        self.connected = False
        return CanonicalResponse.ok

    def start_autofocus(self):
        """
        Starts the ``autofocus`` routine (implemented by _PlaneWave_)

        :mastapi:
        """
        if not self.connected:
            self.logger.error('Cannot start PlaneWave autofocus - not-connected')
            return

        if self.pw.status().autofocus.is_running:
            self.logger.info("autofocus already running")
            return

        self.pw.request("/autofocus/start")
        while not self.pw.status().autofocus.is_running:        # wait for it to actually start
            self.logger.debug('waiting for PlaneWave autofocus to start')
            time.sleep(1)
        self.start_activity(UnitActivities.Autofocusing)
        self.logger.debug('PlaneWave autofocus has started')
        return CanonicalResponse.ok

    def stop_autofocus(self):
        """
        Stops the ``autofocus`` routine

        :mastapi:
        """
        if not self.connected:
            self.logger.error('Cannot stop PlaneWave autofocus - not-connected')
            return

        if not self.pw.status().autofocus.is_running:
            self.logger.info("Cannot stop PlaneWave autofocus, it is not running")
            return
        self.pw.request("/autofocus/stop")
        self.end_activity(UnitActivities.Autofocusing)
        return CanonicalResponse.ok

    @property
    def is_autofocusing(self) -> bool:
        """
        Returns the status of the ``autofocus`` routine
        """
        if not self.connected:
            return False

        return self.pw.status().autofocus.is_running

    def end_guiding(self):
        try:
            self.plate_solver_process.kill()
        except psutil.NoSuchProcess:
            pass

        self.sock_to_solver.shutdown(socket.SHUT_RDWR)
        self.logger.info(f'guiding ended')

    def do_guide(self):

        if not self.shm:
            try:
                self.shm = SharedMemory(name=GUIDING_SHM_NAME)
            except FileNotFoundError:
                self.shm = SharedMemory(name=GUIDING_SHM_NAME, create=True, size=self.camera.NumX*self.camera.NumY*4)

        proc = find_process(patt='PSSimulator')
        if proc:
            proc.kill()
            self.logger.info(f'killed existing plate solving simulator process (pid={proc.pid})')

        sim_dir = os.path.realpath(os.path.join('src', 'PlateSolveSimulator'))
        sim_cmd = os.path.join(sim_dir, 'run.bat')
        self.logger.info(f'starting plate-solver process with: {sim_cmd}')
        subprocess.Popen(sim_cmd, cwd=sim_dir, shell=True)
        start = time.time()
        while time.time() - start <= 20:
            self.plate_solver_process = find_process(patt='PSSimulator')
            if self.plate_solver_process:
                break
            else:
                time.sleep(1)

        if self.plate_solver_process:
            self.logger.info(f'plate solver simulator process pid={self.plate_solver_process.pid}')
        else:
            self.logger.error(f'No solver process after 20 seconds')
            return

        # self.logger.info(f"creating server socket ...")
        server = socket.create_server(guiding.guider_address_port, family=socket.AF_INET)
        self.logger.info(f"listening on {guiding.guider_address_port}")
        server.listen()
        # self.logger.info(f"accepting on server socket")
        self.sock_to_solver, address = server.accept()
        # self.logger.info("accepted on server socket")

        # self.logger.info("receiving on server socket")
        s = self.sock_to_solver.recv(1024)
        # self.logger.info(f"received '{s}' on server socket")
        hello = json.loads(s.decode(encoding='utf-8'))
        if not hello['ready']:
            pass  # TBD

        self.logger.info(f'plate solver simulator is ready')

        while self.is_active(UnitActivities.Guiding):
            self.logger.info(f'starting {self.GUIDING_EXPOSURE_SECONDS} seconds guiding exposure')
            self.camera.start_exposure(seconds=self.GUIDING_EXPOSURE_SECONDS)
            while self.camera.is_active(CameraActivities.Exposing):
                if not self.is_active(UnitActivities.Guiding):
                    self.end_guiding()
                    return
                time.sleep(2)

            if not self.is_active(UnitActivities.Guiding):
                self.end_guiding()
                return

            image = self.camera.image

            self.logger.info(f'guiding exposure done, getting the image from the camera')
            shared_image = np.ndarray((self.camera.NumX, self.camera.NumY), dtype=int, buffer=self.shm.buf)
            shared_image[:] = image[:]
            self.logger.info(f'copied image to shared memory')

            if not self.is_active(UnitActivities.Guiding):
                self.end_guiding()
                return

            pw_status = self.pw.status()
            # try to fool the plate solver by skewing ra and dec ?!?
            ra = pw_status.mount.ra_j2000_hours
            dec = pw_status.mount.dec_j2000_degs

            request = {
                'ra': ra + (20 / (24 * 60 * 60)),
                'dec': dec + (15 / (360 * 60 * 60)),
                'width': self.camera.NumX,
                'height': self.camera.NumY,
            }
            self.sock_to_solver.send(json.dumps(request).encode('utf-8'))

            # plate solver is now solving

            if not self.is_active(UnitActivities.Guiding):
                self.end_guiding()
                return

            # block till the solver is done

            b = self.sock_to_solver.recv(1024)
            response = json.loads(b)

            if not response['success']:
                self.logger.warning(f"solver could not solve, reason '{response.reasons}")
                continue

            # self.logger.info('parsing plate solving result')

            if response['success']:
                self.logger.info(f"plate solving succeeded")
                solved_ra = response['ra']
                solved_dec = response['dec']
                pw_status = self.pw.status()
                mount_ra = pw_status.mount.ra_j2000_hours
                mount_dec = pw_status.mount.dec_j2000_degs

                delta_ra = solved_ra - mount_ra      # mind sign and mount offset direction
                delta_dec = solved_dec - mount_dec   # ditto

                delta_ra_arcsec = delta_ra / (60 * 60)
                delta_dec_arcsec = delta_dec / (60 * 60)

                self.logger.info(f'telling mount to offset by ra={delta_ra_arcsec:.10f}arcsec, '
                                 f'dec={delta_dec_arcsec:.10f}arcsec')
                self.pw.mount_offset(ra_add_arcsec=delta_ra_arcsec, dec_add_arcsec=delta_dec_arcsec)
            else:
                pass  # TBD

            self.logger.info(f"done solving cycle, sleeping {self.GUIDING_INTER_EXPOSURE_SECONDS} seconds ...")
            # avoid sleeping for a long time, for better agility at sensing that guiding was stopped
            td = datetime.timedelta(seconds=self.GUIDING_INTER_EXPOSURE_SECONDS)
            start = datetime.datetime.now()
            while (datetime.datetime.now() - start) <= td:
                if not self.is_active(UnitActivities.Guiding):
                    self.end_guiding()
                    return
                time.sleep(1)

    def start_guiding(self):
        """
        Starts the ``autoguide`` routine

        :mastapi:
        """
        if not self.connected:
            self.logger.warning('cannot start guiding - not-connected')
            return

        # if self.is_active(UnitActivities.Guiding):
            # return

        pw_stat = self.pw.status()
        self.was_tracking_before_guiding = pw_stat.mount.is_tracking
        if not self.was_tracking_before_guiding:
            self.pw.mount_tracking_on()
            self.logger.info('started mount tracking')

        self.start_activity(UnitActivities.Guiding)
        Thread(name='guiding-thread', target=self.do_guide).start()

    def stop_guiding(self):
        """
        Stops the ``autoguide`` routine

        :mastapi:
        """
        if not self.connected:
            self.logger.warning('Cannot stop guiding - not-connected')
            return

        if self.is_active(UnitActivities.Guiding):
            self.end_activity(UnitActivities.Guiding)

        if self.plate_solver_process:
            try:
                self.plate_solver_process.kill()
                self.logger.info(f'killed plate solving process pid={self.plate_solver_process.pid}')
            except psutil.NoSuchProcess:
                pass

        if not self.was_tracking_before_guiding:
            self.mount.stop_tracking()
            self.logger.info('stopped tracking')

    def is_guiding(self) -> bool:
        if not self.connected:
            return False

        return self.is_active(UnitActivities.Guiding)

    @property
    def guiding(self) -> bool:
        return self.is_active(UnitActivities.Guiding)

    def power_all_on(self):
        """
        Turn **ON** all power sockets

        :mastapi:
        """
        for c in self.components:
            if isinstance(c, SwitchedPowerDevice):
                c.power_on()

    def power_all_off(self):
        """
        Turn **OFF** all power sockets

        :mastapi:
        """
        for c in self.components:
            if isinstance(c, SwitchedPowerDevice):
                c.power_off()

    def status(self) -> dict:
        """
        Returns
        -------
        UnitStatus
        :mastapi:
        """
        ret = {
            'detected': self.detected,
            'connected': self.connected,
            'operational': self.operational,
            'why_not_operational': self.why_not_operational,
            'activities': self.activities,
            'activities_verbal': self.activities.__repr__(),
            'guiding': self.guiding,
            'autofocusing': self.is_autofocusing,
            'busy': self.is_autofocusing or self.is_guiding,
            'mount': self.mount.status(),
            'camera': self.camera.status(),
            'stage': self.stage.status(),
            'covers': self.covers.status(),
            'focuser': self.focuser.status(),
        }
        time_stamp(ret)

        if self.autofocus_result:
            ret['autofocus'] = {}
            ret['autofocus']['success'] = self.autofocus_result.success
            ret['autofocus']['best_position'] = self.autofocus_result.best_position
            ret['autofocus']['tolerance'] = self.autofocus_result.tolerance
            ret['autofocus']['time_stamp'] = self.autofocus_result.time_stamp

        return ret

    @staticmethod
    def quit():
        """
        Quits the application

        :mastapi:
        Returns
        -------

        """
        from app import app_quit
        app_quit()

    def abort(self):
        """
        Aborts any in-progress mount activity

        :mastapi:
        Returns
        -------

        """
        if self.is_active(UnitActivities.Guiding):
            self.stop_guiding()

        if self.is_active(UnitActivities.Autofocusing):
            self.stop_autofocus()

        if self.is_active(UnitActivities.StartingUp):
            self.mount.abort()
            self.camera.abort()
            self.focuser.abort()
            self.stage.abort()
            self.covers.abort()

    def test_solving(self, exposure_seconds: int | str):
        """
        Tests the ``platesolve`` routine
        :mastapi:
        Parameter
        ---------
        exposure_seconds int
            Exposure time in seconds
        """
        self.camera.startup()

        pw_stat = self.pw.request_with_status('/status')
        if self.mount.connected:
            self.mount.connect()

        stop_tracking = False
        if not pw_stat.mount.is_tracking:
            stop_tracking = True
            self.logger.info(f"starting tracking")
            self.mount.start_tracking()

        ra = pw_stat.mount.ra_j2000_hours
        dec = pw_stat.mount.dec_j2000_degs

        if isinstance(exposure_seconds, str):
            exposure_seconds = int(exposure_seconds)
        try:
            self.camera.start_exposure(exposure_seconds)
            time.sleep(.5)
        except Exception as ex:
            self.logger.exception('could not start exposure:', ex)

        while not self.camera.ascom.ImageReady:
            self.logger.info(f"Waiting for ImageReady ...")
            time.sleep(1)

        self.logger.info(f"loading image")
        image = self.camera.ascom.ImageArray

        if stop_tracking:
            self.logger.info(f"stopped tracking")
            self.mount.stop_tracking()

        header = fits.Header()
        header['NAXIS'] = 2
        header['NAXIS1'] = self.camera.ascom.NumY
        header['NAXIS2'] = self.camera.ascom.NumX
        header['RA'] = f"{Angle(ra * u.deg).value}"
        header['DEC'] = f"{Angle(dec * u.deg).value}"
        hdu = fits.PrimaryHDU(data=image, header=header)
        hdu_list = fits.HDUList([hdu])

        # fits_file = temp_file.TemporaryFile(prefix='platesolve-', suffix='.fits')
        fits_file = 'c:/Temp/xxx.fits'
        try:
            hdu_list.writeto(fits_file, overwrite=True)
            self.logger.info(f"wrote file '{fits_file}'")
        except Exception as ex:
            self.logger.error(f"failed to write to '{fits_file}'", exc_info=ex)

        # result = platesolve(fits_file, self.camera.PixelSizeX)
        # os.remove(fits_file)

        # return result

    def ontimer(self):

        if self.is_active(UnitActivities.StartingUp):
            if not (self.mount.is_active(MountActivities.StartingUp) or
                    self.camera.is_active(CameraActivities.StartingUp) or
                    self.stage.is_active(StageActivities.StartingUp) or
                    self.focuser.is_active(FocuserActivities.StartingUp) or
                    self.covers.is_active(CoverActivities.StartingUp)):
                self.end_activity(UnitActivities.StartingUp)
                
        if self.is_active(UnitActivities.ShuttingDown):
            if not (self.mount.is_active(MountActivities.ShuttingDown) or
                    self.camera.is_active(CameraActivities.ShuttingDown) or
                    self.stage.is_active(StageActivities.ShuttingDown) or
                    self.focuser.is_active(FocuserActivities.ShuttingDown) or
                    self.covers.is_active(CoverActivities.ShuttingDown) or
                    self.mount.is_active(MountActivities.ShuttingDown)):
                self.end_activity(UnitActivities.ShuttingDown)

        if self.is_active(UnitActivities.Autofocusing):
            autofocus_status = self.pw.status().autofocus
            if not autofocus_status:
                self.logger.error('Empty PlaneWave autofocus status')
            elif not autofocus_status.is_running:   # it's done
                self.logger.info('PlaneWave autofocus ended, getting status.')
                self.autofocus_result.success = autofocus_status.success
                self.autofocus_result.best_position = autofocus_status.best_position
                self.autofocus_result.tolerance = autofocus_status.tolerance
                self.autofocus_result.time_stamp = datetime.datetime.now().isoformat()

                self.end_activity(UnitActivities.Autofocusing)
            else:
                self.logger.info('PlaneWave autofocus in progress')

    def end_lifespan(self):
        self.logger.info('unit end lifespan')
        if 'plate_solver_process' in self.__dict__.keys() and self.plate_solver_process:
            try:
                self.plate_solver_process.kill()
            except psutil.NoSuchProcess:
                pass

        if self.shm:
            self.shm.close()
            self.shm.unlink()
            self.shm = None

        self.shutdown()

    def start_lifespan(self):
        self.logger.debug('unit start lifespan')
        self.startup()

    @property
    def operational(self) -> bool:
        return all([c.operational for c in self.components])

    @property
    def why_not_operational(self) -> List[str]:
        return list(chain.from_iterable(c.why_not_operational for c in self.components))

    @property
    def name(self) -> str:
        return 'unit'

    @property
    def detected(self) -> bool:
        return all([comp.detected for comp in self.components])
