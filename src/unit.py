import datetime
import io
import os
from itertools import chain
import logging
import socket

import numpy as np

import camera
from PlaneWave import pwi4_client
import time
from typing import List, Any, NamedTuple
from camera import Camera, CameraActivities, Binning, CameraRoi, ExposurePurpose, ExposureSettings
from covers import Covers, CoverActivities
from stage import Stage, StageActivities, StagePresetPosition
from mount import Mount, MountActivities
from focuser import Focuser, FocuserActivities
from dlipower.dlipower.dlipower import SwitchedPowerDevice, PowerSwitchFactory
from astropy.coordinates import Angle
import astropy.units as u
from common.utils import RepeatTimer
from enum import IntFlag, auto
from threading import Thread
from multiprocessing.shared_memory import SharedMemory
from common.utils import Component, DailyFileHandler, BASE_UNIT_PATH
from common.utils import time_stamp, CanonicalResponse, CanonicalResponse_Ok, PathMaker, function_name, Filer
from common.config import Config
import subprocess
from enum import Enum
import json
from fastapi.routing import APIRouter
import concurrent.futures
from PIL import Image
import ipaddress
from starlette.websockets import WebSocket, WebSocketDisconnect
from skimage.registration import phase_cross_correlation
from PlaneWave.ps3cli_client import PS3CLIClient

PLATE_SOLVING_SHM_NAME = 'PlateSolving_Image'

logger = logging.getLogger('mast.unit')


class Coord(NamedTuple):
    ra: Angle
    dec: Angle


class SolvingTolerance:
    ra: Angle
    dec: Angle

    def __init__(self, ra: Angle, dec: Angle):
        self.ra = ra
        self.dec = dec


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


class UnitRoi:
    """
    In unit terms a region-of-interest is centered on a pixel and has width and height
    """
    fiber_x: int
    fiber_y: int
    width: int
    height: int

    def __init__(self, fiber_x: int, fiber_y: int, width: int, height: int):
        self.fiber_x = fiber_x
        self.fiber_y = fiber_y
        self.width = width
        self.height = height

    def to_camera_roi(self, binning: Binning = Binning(1, 1)) -> CameraRoi:
        """
        In ASCOM camera terms it has a starting pixel (x, y), width and height
        Returns The corresponding camera region-of-interest
        -------

        """
        return CameraRoi(
            (self.fiber_x - int(self.width / 2)) * binning.x,
            (self.fiber_y - int(self.height / 2)) * binning.y,
            self.width * binning.x,
            self.height * binning.y
        )

    @staticmethod
    def from_dict(d):
        return UnitRoi(d['fiber_x'], d['fiber_y'], d['width'], d['height'])

    def __repr__(self) -> str:
        return f"x={self.fiber_x},y={self.fiber_y},w={self.width},h={self.height}"


class AutofocusResult:
    success: bool
    best_position: float | None
    tolerance: float | None
    time_stamp: str


class PlateSolverCode(IntFlag):
    Success = 0,
    InvalidArguments = 1,
    CatalogNotFound = 2,
    NoStarMatch = 3,
    NoImageLoad = 4,
    GeneralFailure = 99


class PlateSolverResult:
    succeeded: bool = False
    ra_j2000_hours: float | None = None
    dec_j2000_degrees: float | None = None
    arcsec_per_pixel: float | None = None
    rot_angle_degs: float | None = None
    errors: List[str] = []

    def __init__(self, d):
        self.succeeded = d['succeeded']
        if 'ra_j2000_hours' in d:
            self.ra_j2000_hours = d['ra_j2000_hours']
        if 'dec_j2000_degrees' in d:
            self.dec_j2000_degrees = d['dec_j2000_degrees']
        if 'rot_angle_degs' in d:
            self.rot_angle_degs = d['rot_angle_degs']
        if 'arcsec_per_pixel' in d:
            self.arcsec_per_pixel = d['arcsec_per_pixel']
        if 'errors' in d:
            self.errors = d['errors']

    @staticmethod
    def from_file(file: str) -> 'PlateSolverResult':
        ret = {'succeeded': True}
        try:
            with open(file, 'r') as f:
                for line in f.readlines():
                    k, v = line.split('=')
                    ret[k] = v
            ret['succeeded'] = all(['ra_j2000_hours' in ret, 'dec_j2000_degrees' in ret,
                                    'arcsec_per_pixel' in ret, 'rot_angle_degs' in ret])
            return PlateSolverResult(ret)

        except Exception as e:
            logger.error(f"{e}")
            return PlateSolverResult(ret)


class PS3Solution:
    num_matched_stars: int
    match_rms_error_arcsec: float
    match_rms_error_pixels: int
    center_ra_j2000_rads: float
    center_dec_j2000_rads: float
    matched_arcsec_per_pixel: float
    rotation_angle_degs: float

    def __init__(self, d: dict):
        if d is None:
            self.num_matched_stars = 0
            self.match_rms_error_arcsec = 0
            self.match_rms_error_pixels = 0
            self.center_ra_j2000_rads = 0
            self.center_dec_j2000_rads = 0
            self.matched_arcsec_per_pixel = 0
            self.rotation_angle_degs = 0
        else:
            self.num_matched_stars = d['num_matched_stars']
            self.match_rms_error_arcsec = d['match_rms_error_arcsec']
            self.match_rms_error_pixels = d['match_rms_error_pixels']
            self.center_ra_j2000_rads = d['center_ra_j2000_rads']
            self.center_dec_j2000_rads = d['center_dec_j2000_rads']
            self.matched_arcsec_per_pixel = d['matched_arcsec_per_pixel']
            self.rotation_angle_degs = d['rotation_angle_degs']


class PS3Result:
    state: str              # 'ready', 'loading', 'extracting', 'matching', 'found_match', 'no_match', 'error'
    error_message: str
    last_log_message: str
    num_extracted_stars: int
    running_time_seconds: float
    solution: PS3Solution

    def __init__(self, d: dict):
        self.state: str = d['state']
        self.error_message: str | None = d['error_message'] if 'error_message' in d else None
        self.last_log_message: str | None = d['last_log_message'] if 'last_log_message' in d else None
        self.num_extracted_stars: int = d['num_extracted_stars'] if 'num_extracted_stars' in d else 0
        self.running_time_seconds: float = d['running_time_seconds'] if 'running_time_seconds' in d else 0
        self.solution: PS3Solution | None = PS3Solution(d['solution']) if 'solution' in d else None


class UnitActivities(IntFlag):
    Idle = 0
    Autofocusing = auto()
    Guiding = auto()
    StartingUp = auto()
    ShuttingDown = auto()
    Acquiring = auto()
    Positioning = auto()    # getting in position (e.g. for acquisition)
    Solving = auto()
    Correcting = auto()


class Unit(Component):

    MAX_UNITS = 20
    MAX_AUTOFOCUS_TRIES = 3

    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Unit, cls).__new__(cls)
            logger.info(f"Unit.__new__: allocated instance 0x{id(cls._instance):x}")
        return cls._instance

    def __init__(self, id_: int | str):
        if self._initialized:
            return
        logger.info(f"Unit.__init__: initiating instance 0x{id(self):x}")

        Component.__init__(self)

        self._connected: bool = False
        self._is_guiding: bool = False
        self._is_autofocusing: bool = False

        # Stuff for plate solving
        self.was_tracking_before_guiding: bool = False

        file_handler = [h for h in logger.handlers if isinstance(h, DailyFileHandler)]
        logger.info(f"logging to '{file_handler[0].path}'")

        if isinstance(id_, int) and not 1 <= id_ <= Unit.MAX_UNITS:
            raise f"Bad unit id '{id_}', must be in [1..{Unit.MAX_UNITS}]"

        self.id = id_
        self.unit_conf = Config().get_unit()

        self.min_ra_correction_arcsec: float = float(self.unit_conf['guiding']['min_ra_correction_arcsec']) \
            if 'min_ra_correction_arcsec' in self.unit_conf['guiding'] else 1
        self.min_dec_correction_arcsec: float = float(self.unit_conf['guiding']['min_dec_correction_arcsec']) \
            if 'min_dec_correction_arcsec' in self.unit_conf['guiding'] else 1

        self.autofocus_max_tolerance = self.unit_conf['autofocus']['max_tolerance']
        self.autofocus_try: int = 0

        self.hostname = socket.gethostname()
        try:
            self.power_switch = PowerSwitchFactory.get_instance(
                conf=self.unit_conf['power_switch'],
                upload_outlet_names=True)
            self.mount: Mount = Mount()
            self.camera: Camera = Camera()
            self.covers: Covers = Covers()
            self.stage: Stage = Stage()
            self.focuser: Focuser = Focuser()
            self.pw: pwi4_client.PWI4 = pwi4_client.PWI4()
        except Exception as ex:
            logger.exception(msg='could not create a Unit', exc_info=ex)
            raise ex

        self.components: List[Component] = [
            self.power_switch,
            self.mount,
            self.camera,
            self.covers,
            self.focuser,
            self.stage,
        ]

        self.timer: RepeatTimer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = 'unit-timer-thread'
        self.timer.start()

        self.reference_image = None
        self.autofocus_result: AutofocusResult | None = None

        self._was_shut_down = False

        self.connected_clients: List[WebSocket] = []
        # self.camera.register_visualizer('image-to-dashboard', self.push_image_to_dashboards)

        self.errors: List[str] = []

        self.latest_solver_result: PS3Result | None = None

        self._initialized = True
        logger.info("unit: initialized")

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

        self._was_shut_down = False
        Thread(name='unit-startup-thread', target=self.do_startup).start()
        return CanonicalResponse_Ok

    def do_shutdown(self):
        self.start_activity(UnitActivities.ShuttingDown)
        [comp.shutdown() for comp in self.components]
        self._was_shut_down = True

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
        return CanonicalResponse_Ok

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
        return CanonicalResponse_Ok

    def disconnect(self):
        """
        Disconnects the **MAST** ``unit`` subsystems from all its ancillaries.

        :mastapi:
        """
        self.connected = False
        return CanonicalResponse_Ok

    def start_autofocus(self):
        """
        Starts the ``autofocus`` routine (implemented by _PlaneWave_)

        :mastapi:
        """
        # if not self.connected:
        #     logger.error('Cannot start PlaneWave autofocus - not-connected')
        #     return

        if self.pw.status().autofocus.is_running:
            logger.info("autofocus already running")
            return

        #
        # The current autofocus API does not allow setting of the following values.
        #  We prepare them in case they change the API
        #
        # autofocus_conf = self.unit_conf['autofocus']
        # binning_for_autofocus = autofocus_conf['binning']
        # exposure_for_autofocus = autofocus_conf['exposure']
        # exposure_for_images = autofocus_conf['images']
        # exposure_for_spacing = autofocus_conf['spacing']

        self.pw.request("/autofocus/start")
        while not self.pw.status().autofocus.is_running:        # wait for it to actually start
            logger.debug('waiting for PlaneWave autofocus to start')
            time.sleep(1)
        if self.autofocus_try == 0:
            self.start_activity(UnitActivities.Autofocusing)
        logger.debug('PlaneWave autofocus has started')
        return CanonicalResponse_Ok

    def stop_autofocus(self):
        """
        Stops the ``autofocus`` routine

        :mastapi:
        """
        # if not self.connected:
        #     logger.error('Cannot stop PlaneWave autofocus - not-connected')
        #     return

        if not self.pw.status().autofocus.is_running:
            logger.info("Cannot stop PlaneWave autofocus, it is not running")
            return
        self.pw.request("/autofocus/stop")
        self.end_activity(UnitActivities.Autofocusing)
        return CanonicalResponse_Ok

    @property
    def is_autofocusing(self) -> bool:
        """
        Returns the status of the ``autofocus`` routine
        """
        if not self.connected:
            return False

        return self.pw.status().autofocus.is_running

    def end_guiding(self):
        self.end_activity(UnitActivities.Guiding)
        logger.info(f'guiding ended')

    #
    # $ ./ps3cli --help
    # Usage: ps3cli imagefile.fits pixelscale resultsfile catalogpath
    #
    #   imagefile.fits: The path to the FITS file that will be plate-solved
    #   pixelscale:     The estimated scale of the image, in arcseconds per pixel
    #   resultsfile:    The path to the file that will be written with the
    #                   platesolve results
    #   catalogpath:    The path to the PlateSolve star catalog files
    #                   (typically a directory containing UC4 and Orca subdirs)
    #
    # Exit status:
    #   0: Success; Match found and output written
    #   1: Invalid arguments
    #   2: Catalog files not found
    #   3: Star match not found
    #   4: Error loading image
    #  99: General failure

    def do_guide_by_solving_without_shm(self, base_folder: str | None = None):
        def guiding_was_stopped() -> bool:
            if not self.is_active(UnitActivities.Guiding):
                self.end_guiding()
                return True
            return False

        def stopped_while_waiting_for_next_cycle() -> bool:
            """
            Sleeps for up to self.unit_conf['guiding']['interval'] seconds, checking every second if
             the guiding was stopped (via end_activity(UnitActivities.Guiding))

            Returns
            -------
                True if the guiding was stopped
                False if the time expired.

            """
            # avoid sleeping for a long time, for better agility at sensing that guiding was stopped
            start: datetime.datetime = self.timings[UnitActivities.Guiding].start_time
            now = datetime.datetime.now()
            cadence = datetime.timedelta(seconds=float(self.unit_conf['guiding']['cadence']))
            end = start + cadence
            elapsed_seconds = (now - start).seconds
            if elapsed_seconds > cadence.seconds:
                logger.error(f"the guiding cycle took {elapsed_seconds} seconds ({cadence.seconds=}), not sleeping")
                return False

            remaining_seconds = (end - now).seconds
            logger.info(f"done solving cycle, sleeping {remaining_seconds} seconds ...")
            while datetime.datetime.now() < end:
                if guiding_was_stopped():
                    logger.info(f"guiding was stopped, stopped sleeping ")
                    return True
                time.sleep(1)
            return False

        filer = Filer()
        cmd = 'C:\\Program Files (x86)\\PlaneWave Instruments\\ps3cli\\ps3cli'

        op = function_name()
        guiding_conf = self.unit_conf['guiding']
        guiding_roi: UnitRoi = UnitRoi(
            fiber_x=guiding_conf['fiber_x'],
            fiber_y=guiding_conf['fiber_y'],
            width=self.camera.guiding_roi_width,
            height=self.camera.guiding_roi_height
        )
        binning: Binning = Binning(guiding_conf['binning'], guiding_conf['binning'])
        guiding_settings: ExposureSettings = ExposureSettings(
            purpose=ExposurePurpose.Guiding,
            seconds=guiding_conf['exposure'],
            base_folder=base_folder,
            gain=guiding_conf['gain'],
            binning=binning,
            roi=guiding_roi.to_camera_roi(binning=binning),
            save=True
        )

        # TODO: use solve_and_correct()

        while self.is_active(UnitActivities.Guiding):

            # root_path = path_maker.make_guiding_root_name()
            # image_path = f"{root_path}image.fits"
            # result_path = f"{root_path}result.txt"
            # correction_path = f"{root_path}correction.txt"

            logger.info(f'{op}: starting {self.unit_conf['guiding']['exposure']} seconds guiding exposure')
            response = self.camera.do_start_exposure(guiding_settings)
            if response.failed:
                logger.error(f"{op}: could not start guiding exposure: {response=}")
                return response

            time.sleep(1)  # wait for exposure to start
            while not self.camera.image_saved_event.wait(1):
                if guiding_was_stopped():
                    return CanonicalResponse_Ok
                time.sleep(2)
            self.camera.image_saved_event.clear()

            if guiding_was_stopped():
                return CanonicalResponse_Ok

            try:
                pixel_scale = self.unit_conf['camera']['pixel_scale_at_bin1'] * guiding_conf['binning']
            except Exception as e:
                error = f"could not calculate pixel scale, exception={e}"
                logger.error(error)
                return CanonicalResponse(errors=[error])

            image_path = guiding_settings.image_path
            result_path = os.path.join(os.path.dirname(image_path), 'result.txt')
            correction_path = os.path.join(os.path.dirname(image_path), 'correction.json')
            command = [cmd, image_path, f'{pixel_scale}', result_path, 'C:/Users/mast/Documents/Kepler']
            logger.info(f'{op}: image saved, running solver ...')

            # result = None
            try:
                result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, shell=True)
                filer.move_ram_to_shared(image_path)
            except subprocess.CalledProcessError as e:
                # solving failed.  should we maybe move a little?
                logger.error(f'{op}: solver return code: {PlateSolverCode(e.returncode).__repr__()}')
                with open(result_path, 'w') as file:
                    file.write(e.stdout.decode())
                    filer.move_ram_to_shared(result_path)

                # if it's a HARD error (not just NoStarMatch), cannot continue
                if e.returncode == PlateSolverCode.InvalidArguments or \
                        e.returncode == PlateSolverCode.CatalogNotFound or \
                        e.returncode == PlateSolverCode.NoImageLoad or \
                        e.returncode == PlateSolverCode.GeneralFailure:
                    logger.error(f"{op}: solver returned {PlateSolverCode(e.returncode).__repr__()}, guiding aborted.")
                    self.end_guiding()
                    return CanonicalResponse(errors=[f"solver failed with {PlateSolverCode(e.returncode).__repr__()}"])

                if stopped_while_waiting_for_next_cycle():
                    return CanonicalResponse_Ok

                continue  # to next guiding cycle

            # solving succeeded, parse output
            if result.returncode == PlateSolverCode.Success:
                logger.info(f"{op}: solver found a solution")
                with open(result_path, 'r') as file:
                    lines = file.readlines()
            elif result.returncode == PlateSolverCode.NoStarMatch:
                logger.error(f"{op}: solver did not find a match {result.returncode=}")
                if stopped_while_waiting_for_next_cycle():
                    return CanonicalResponse_Ok
                continue

            filer.move_ram_to_shared(result_path)

            solver_output = {}
            for line in lines:
                fields = line.rstrip().split('=')
                if len(fields) != 2:
                    continue
                keyword, value = fields
                solver_output[keyword] = float(value)
            if 'arcsec_per_pixel' in solver_output:
                logger.info(f"{op}: {solver_output['arcsec_per_pixel']=}")

            for key in ['ra_j2000_hours', 'dec_j2000_degrees', 'rot_angle_degs']:
                if key not in solver_output:
                    logger.error(f"{op}: either 'ra_j2000_hours' or 'dec_j2000_degrees' missing in {solver_output=}")
                    continue

            # rot_angle_degs = solver_output['rot_angle_degs']
            # TODO: calculate mount offsets using (camera.offset.x, camera.offset.y) and rot_angle_degs

            pw_status = self.pw.status()
            mount_ra_hours = pw_status.mount.ra_j2000_hours
            mount_dec_degs = pw_status.mount.dec_j2000_degs

            solved_ra_hours = solver_output['ra_j2000_hours']
            solved_dec_degs = solver_output['dec_j2000_degrees']
            # self.pw.mount_model_add_point(solved_ra_hours, solved_dec_degs)

            try:
                delta_ra_hours = solved_ra_hours - mount_ra_hours    # mind sign and mount offset direction
                delta_dec_degs = solved_dec_degs - mount_dec_degs    # ditto
                # delta_ra_hours = mount_ra_hours - solved_ra_hours    # mind sign and mount offset direction
                # delta_dec_degs = mount_dec_degs - solved_dec_degs    # ditto

                delta_ra_arcsec = Angle(delta_ra_hours * u.hour).arcsecond
                delta_dec_arcsec = Angle(delta_dec_degs * u.deg).arcsecond
            except Exception as e:
                return CanonicalResponse(exception=e)

            correction = {
                'ra': {
                    'mount_ra_hours': mount_ra_hours,
                    'solved_ra_hours': solved_ra_hours,
                    'delta_ra_arcsec': delta_ra_arcsec,
                    'min_ra_correction_arcsec': self.min_ra_correction_arcsec,
                    'needs_correction': False,
                }, 'dec': {
                    'mount_dec_degs': mount_dec_degs,
                    'solved_dec_degs': solved_dec_degs,
                    'delta_dec_arcsec': delta_dec_arcsec,
                    'min_dec_correction_arcsec': self.min_dec_correction_arcsec,
                    'needs_correction': False,
                }
            }

            try:
                if abs(delta_ra_arcsec) >= self.min_ra_correction_arcsec:
                    correction['ra']['correction_arcsec'] = delta_ra_arcsec
                    correction['ra']['correction_deg'] = Angle(delta_ra_arcsec * u.arcsec).degree
                    correction['ra']['correction_sexa'] = (Angle(delta_ra_arcsec * u.arcsec).
                                                           to_string(unit='degree', sep=':', precision=3))
                    correction['ra']['needs_correction'] = True

                if abs(delta_dec_arcsec) >= self.min_dec_correction_arcsec:
                    correction['dec']['correction_arcsec'] = delta_dec_arcsec
                    correction['dec']['correction_deg'] = Angle(delta_dec_arcsec * u.arcsec).degree
                    correction['dec']['correction_sexa'] = (Angle(delta_dec_arcsec * u.arcsec).
                                                            to_string(unit='degree', sep=':', precision=3))
                    correction['dec']['needs_correction'] = True
            except Exception as e:
                return CanonicalResponse(exception=e)

            with open(correction_path, 'w') as file:
                json.dump(correction, file, indent=2)
                logger.info(f"saved correction file to {correction_path}")

            if correction['ra']['needs_correction'] or correction['dec']['needs_correction']:
                logger.info(f'{op}: telling mount to offset by ra={delta_ra_arcsec:.3f}arcsec, ' +
                            f'dec={delta_dec_arcsec:.3f}arcsec')
                # self.pw.mount_offset(ra_add_arcsec=correction['ra']['correction_arcsec'],
                #                      dec_add_arcsec=correction['dec']['correction_arcsec'])
                # stat = self.pw.status()
                # while stat.mount.is_slewing:
                #     time.sleep(.1)
                #     stat = self.pw.status()
                # time.sleep(5)
                # logger.info(f"mount stopped slewing")
            else:
                logger.info(f"{op}: correction abs({correction['ra']['correction_arcsec']}) " +
                            f"< {self.min_ra_correction_arcsec=} " +
                            f"and abs({correction['dec']['correction_arcsec']=}) < {self.min_dec_correction_arcsec=} " +
                            f"too small , skipped.")

            filer.move_ram_to_shared(correction_path)

            if stopped_while_waiting_for_next_cycle():
                return CanonicalResponse_Ok
            # continue looping

    def do_guide_by_solving_with_shm(self, target: Coord):

        op = function_name()
        guiding_conf = self.unit_conf['guiding']
        guiding_roi: UnitRoi = UnitRoi(
            fiber_x=guiding_conf['fiber_x'],
            fiber_y=guiding_conf['fiber_y'],
            width=guiding_conf['width'],
            height=guiding_conf['height']
        )
        binning: Binning = Binning(guiding_conf['binning'], guiding_conf['binning'])
        guiding_settings: ExposureSettings = ExposureSettings(
            purpose=ExposurePurpose.Guiding,
            seconds=guiding_conf['exposure'],
            # base_folder=base_folder,
            gain=guiding_conf['gain'],
            binning=binning,
            roi=guiding_roi.to_camera_roi(binning=binning),
            save=True
        )
        pixel_scale = self.unit_conf['camera']['pixel_scale_at_bin1'] * binning.x

        shm = SharedMemory(name=PLATE_SOLVING_SHM_NAME, create=True, size=guiding_roi.width * guiding_roi.height * 2)
        ps3_client = PS3CLIClient()
        ps3_client.connect('127.0.0.1', 9896)

        while self.is_active(UnitActivities.Guiding):
            logger.info(f"starting {guiding_conf['exposure']} seconds guiding exposure")
            self.camera.do_start_exposure(guiding_settings)

            logger.info(f"{op}: waiting for image ready ...")
            self.camera.wait_for_image_ready()
            logger.info(f"{op}: image is ready ...")

            if not self.is_active(UnitActivities.Guiding):
                self.end_guiding()
                break

            logger.info(f"{op}: getting the image from the camera ...")
            image = self.camera.image

            shared_image = np.ndarray((guiding_roi.width, guiding_roi.height), dtype=np.uint16, buffer=shm.buf)
            shared_image[:] = image[:]
            logger.info(f"{op}: copied image to shared memory")

            if not self.is_active(UnitActivities.Guiding):
                break

            ps3_client.begin_platesolve_shm(
                shm_key=PLATE_SOLVING_SHM_NAME,
                width_pixels=guiding_roi.width,
                height_pixels=guiding_roi.height,
                arcsec_per_pixel_guess=pixel_scale,
                enable_all_sky_match=True,
                enable_local_quad_match=True,
                enable_local_triangle_match=True,
                ra_guess_j2000_rads=target.ra.radian,
                dec_guess_j2000_rads=target.dec.radian,
            )

            solving_result: PS3Result | None = None
            start = datetime.datetime.now()
            end = start + datetime.timedelta(seconds=60)

            while datetime.datetime.now() < end:
                solving_result: PS3Result = PS3Result(ps3_client.platesolve_status())
                if (solving_result.state == 'error' or
                        solving_result.state == 'found_match' or
                        solving_result.state == 'no_match'):
                    break
                else:
                    time.sleep(.1)

            if datetime.datetime.now() >= end:
                ps3_client.platesolve_cancel()
                continue

            if not self.is_active(UnitActivities.Guiding):
                break

            if solving_result.state == 'no_match':
                logger.error(f"solver did not find a match (latest_error: {solving_result.error_message}")
                continue
            if solving_result.state == 'error':
                logger.error(f"solver error: {solving_result.error_message}")
                continue

            if solving_result.state == 'found_match' and solving_result.solution:
                logger.info(f"plate solver found a match")
                solved_ra_arcsec = Angle(solving_result.solution.center_ra_j2000_rads * u.radian).arcsecond
                solved_dec_arcsec = Angle(solving_result.solution.center_dec_j2000_rads * u.radian).arcsecond

                delta_ra_arcsec = solved_ra_arcsec - target.ra.arcsecond      # mind sign and mount offset direction
                delta_dec_arcsec = solved_dec_arcsec - target.dec.arcsecond   # ditto

                logger.info(f"offsetting mount by ra={delta_ra_arcsec:.3f}arcsec, " +
                            f"dec={delta_dec_arcsec:.3f}arcsec")
                self.pw.mount_offset(ra_add_arcsec=delta_ra_arcsec, dec_add_arcsec=delta_dec_arcsec)

            logger.info(f"done solving cycle, sleeping {self.unit_conf['guiding']['interval']} seconds ...")
            # avoid sleeping for a long time, for better agility at sensing that guiding was stopped
            start_sleep = datetime.datetime.now()
            end_sleep = start_sleep + datetime.timedelta(seconds=float(self.unit_conf['guiding']['interval']))
            while datetime.datetime.now() < end_sleep:
                if not self.is_active(UnitActivities.Guiding):
                    logger.info('no UnitActivities.Guiding, bailing out ...')
                    break
                logger.info('sleeping ...')
                time.sleep(1)

        self.end_guiding()
        ps3_client.close()
        shm.unlink()

    def start_guiding_by_solving(self, ra_j2000_hours: float, dec_j2000_degs: float):
        """
        Starts the ``autoguide`` routine

        :mastapi:
        """
        # if not self.connected:
        #     logger.warning('cannot start guiding - not-connected')
        #     return

        if self.is_active(UnitActivities.Guiding):
            return CanonicalResponse(errors=['already guiding'])

        pw_stat = self.pw.status()
        self.was_tracking_before_guiding = pw_stat.mount.is_tracking
        if not self.was_tracking_before_guiding:
            self.pw.mount_tracking_on()
            logger.info('started mount tracking')

        self.start_activity(UnitActivities.Guiding)

        executor = concurrent.futures.ThreadPoolExecutor()
        executor.thread_names_prefix = 'guiding-executor'
        target: Coord = Coord(ra=Angle(ra_j2000_hours * u.hour), dec=Angle(dec_j2000_degs * u.deg))
        future = executor.submit(self.do_guide_by_solving_with_shm, target=target)
        time.sleep(2)
        if future.running():
            def stop_tracking(_):
                self.pw.mount_tracking_off()

            if not self.was_tracking_before_guiding:
                future.add_done_callback(stop_tracking)
            return CanonicalResponse_Ok
        else:
            return future.result()  # a CanonicalResponse with errors

    def stop_guiding(self):
        """
        Stops the ``autoguide`` routine

        :mastapi:
        """
        # if not self.connected:
        #     logger.warning('Cannot stop guiding - not-connected')
        #     return

        if not self.is_active(UnitActivities.Guiding):
            error = "not guiding"
            logger.error(error)
            return CanonicalResponse(errors=[error])

        if self.is_active(UnitActivities.Guiding):
            self.end_activity(UnitActivities.Guiding)

        if not self.was_tracking_before_guiding:
            self.mount.stop_tracking()
            logger.info('stopped tracking')

        return CanonicalResponse_Ok

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
        ret = self.component_status()
        ret |= {
            'id': id(self),
            'guiding': self.guiding,
            'autofocusing': self.is_autofocusing,
        }
        for comp in self.components:
            ret[comp.name] = comp.status()
        time_stamp(ret)

        if self.autofocus_result:
            ret['autofocus'] = {
                'success': self.autofocus_result.success,
                'best_position': self.autofocus_result.best_position,
                'tolerance': self.autofocus_result.tolerance,
                'time_stamp': self.autofocus_result.time_stamp
            }

        ret['powered'] = True
        ret['type'] = 'full'
        # return ret
        return serialize_ip_addresses(ret)

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

    def ontimer(self):
        """
        Used in order to end activities that were started elsewhere in the code.

        Returns
        -------

        """
        # UnitActivities.StartingUp
        if self.is_active(UnitActivities.StartingUp):
            if not (self.mount.is_active(MountActivities.StartingUp) or
                    self.camera.is_active(CameraActivities.StartingUp) or
                    self.stage.is_active(StageActivities.StartingUp) or
                    self.focuser.is_active(FocuserActivities.StartingUp) or
                    self.covers.is_active(CoverActivities.StartingUp)):
                self.end_activity(UnitActivities.StartingUp)

        # UnitActivities.ShuttingDown
        if self.is_active(UnitActivities.ShuttingDown):
            if not (self.mount.is_active(MountActivities.ShuttingDown) or
                    self.camera.is_active(CameraActivities.ShuttingDown) or
                    self.stage.is_active(StageActivities.ShuttingDown) or
                    self.focuser.is_active(FocuserActivities.ShuttingDown) or
                    self.covers.is_active(CoverActivities.ShuttingDown) or
                    self.mount.is_active(MountActivities.ShuttingDown)):
                self.end_activity(UnitActivities.ShuttingDown)
                self._was_shut_down = True

        # UnitActivities.Autofocusing
        if self.is_active(UnitActivities.Autofocusing):
            autofocus_status = self.pw.status().autofocus
            if not autofocus_status:
                logger.error('Empty PlaneWave autofocus status')
            elif not autofocus_status.is_running:   # it's done
                logger.info('PlaneWave autofocus ended, getting status.')
                self.autofocus_result = AutofocusResult()
                self.autofocus_result.success = autofocus_status.success
                if self.autofocus_result.success:
                    self.autofocus_result.best_position = autofocus_status.best_position
                    self.autofocus_result.tolerance = autofocus_status.tolerance

                    best_position = autofocus_status.best_position
                    self.unit_conf['focuser']['known_as_good_position'] = best_position
                    try:
                        Config().set_unit(self.hostname, self.unit_conf)
                        logger.info(f"autofocus: saved {best_position=} in the configuration for unit {self.hostname}.")
                        if autofocus_status.tolerance > self.autofocus_max_tolerance:
                            if self.autofocus_try < Unit.MAX_AUTOFOCUS_TRIES:
                                self.autofocus_try += 1
                                logger.info(f"autofocus: latest {autofocus_status.tolerance=} greater than" +
                                            f"{self.autofocus_max_tolerance=}, starting autofocus " +
                                            f"try #{self.autofocus_try}")
                                self.start_autofocus()
                            else:
                                logger.info(f"autofocus: failed to reach {self.autofocus_max_tolerance=} " +
                                            f"in {Unit.MAX_AUTOFOCUS_TRIES=}")
                        else:
                            self.autofocus_try = 0

                    except Exception as e:
                        logger.exception("failed to save unit_conf for ['focuser']['know_as_good_position']",
                                         exc_info=e)
                else:
                    logger.error(f"PlaneWave autofocus failed")
                    self.autofocus_result.best_position = None
                    self.autofocus_result.tolerance = None
                self.autofocus_result.time_stamp = datetime.datetime.now().isoformat()

                self.end_activity(UnitActivities.Autofocusing)
            else:
                logger.info(f'PlaneWave autofocus in progress {self.autofocus_try=}')

    def end_lifespan(self):
        logger.info('unit end lifespan')
        self.shutdown()

    def start_lifespan(self):
        logger.debug('unit start lifespan')
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
        # return all([comp.detected for comp in self.components])
        return True

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down

    async def unit_visual_ws(self, websocket: WebSocket):
        logger.info(f"accepting on {websocket=} ...")
        await websocket.accept()
        self.connected_clients.append(websocket)
        logger.info(f"added {websocket} to self.connected_clients")
        try:
            while True:
                _ = await websocket.receive_text()
        except WebSocketDisconnect:
            self.connected_clients.remove(websocket)
            logger.info(f"removed {websocket} from self.connected_clients")

    async def push_image_to_dashboards(self, image: np.ndarray):
        transposed_image = np.transpose(image.astype(np.uint16))
        image_pil = Image.fromarray(transposed_image)
        with io.BytesIO() as output:
            image_pil.save(output, format="PNG")
            png_data = output.getvalue()

        for websocket in self.connected_clients:
            try:
                logger.info(f"pushing to {websocket.url=} ...")
                await websocket.send(png_data)
                # loop = asyncio.get_event_loop()
                # loop.run_until_complete(websocket.send(png_data))
            except Exception as e:
                logger.error(f"websocket.send error: {e}")

    def plate_solve(self, settings: ExposureSettings, target: Coord) -> PS3Result:
        op = function_name()

        while self.is_active(UnitActivities.Solving):

            image_path = settings.image_path

            #
            # Start exposure
            #
            logger.info(f'{op}: starting {settings.seconds=} acquisition exposure')
            response = self.camera.do_start_exposure(settings)
            if response.failed:
                logger.error(f"{op}: could not start acquisition exposure: {response=}")
                return PS3Result({
                    'state': 'error',
                    'error_message': f'could not start exposure ({[response.errors]})'
                })

            self.camera.wait_for_image_ready()
            logger.info(f"{op}: image is ready")

            if settings.binning.x != settings.binning.y:
                raise Exception(f"cannot deal with non-equal horizontal and vertical binning " +
                                f"({settings.binning.x=}, {settings.binning.y=}")
            pixel_scale = self.unit_conf['camera']['pixel_scale_at_bin1'] * settings.binning.x

            filer = Filer()

            width = settings.roi.numX
            height = settings.roi.numY
            shm = SharedMemory(name=PLATE_SOLVING_SHM_NAME, create=True, size=width * height * 2)
            shared_image = np.ndarray((width, height), dtype=np.uint16, buffer=shm.buf)
            shared_image[:] = self.camera.image[:]
            ps3_client: PS3CLIClient = PS3CLIClient()

            ps3_client.connect('127.0.0.1', 9896)
            start = datetime.datetime.now()
            timeout_seconds: float = 10
            end = start + datetime.timedelta(seconds=timeout_seconds)
            ps3_client.begin_platesolve_shm(
                shm_key=PLATE_SOLVING_SHM_NAME,
                height_pixels=settings.roi.numY,
                width_pixels=settings.roi.numX,
                arcsec_per_pixel_guess=pixel_scale,
                enable_all_sky_match=True,
                enable_local_quad_match=True,
                enable_local_triangle_match=True,
                ra_guess_j2000_rads=target.ra.radian,
                dec_guess_j2000_rads=target.dec.radian
            )

            solver_status: PS3Result
            while True:
                solver_status = PS3Result(ps3_client.platesolve_status())

                if (solver_status.state == 'error' or
                        solver_status.state == 'no_match' or
                        solver_status.state == 'found_match'):
                    break

                if datetime.datetime.now() >= end:
                    ps3_client.platesolve_cancel()
                    solver_status = PS3Result({
                        'state': 'error',
                        'error_message': f'time out ({timeout_seconds} seconds), cancelled'
                    })
                    break
                else:
                    time.sleep(.1)

            self.camera.wait_for_image_saved()
            filer.move_ram_to_shared(image_path)

            return solver_status

    def solve_and_correct(self,
                          target: Coord,
                          exposure_settings: ExposureSettings,
                          solving_tolerance: SolvingTolerance,
                          caller: str | None = None,
                          max_tries: int = 3) -> bool:
        """
        Tries for max_tries times to:
        - Take an exposure using exposure_settings
        - Plate solve the image
        - If the solved coordinates are NOT within the solving_tolerance (first time vs. the mount coordinates,
           following times vs. the last solved coordinates), correct the mount

        Parameters
        ----------
        target: (ra, dec)
        exposure_settings: camera settings for the exposure
        solving_tolerance: how close do we need to be to stop trying
        caller: A string to be added to the log messages
        max_tries: How many times to try to get withing the solving_tolerance

        Returns
        -------
        boolean: True if succeeded within max_tries to get within solving_tolerance

        """
        op = function_name()
        if caller:
            op += f":{caller}"

        self.start_activity(UnitActivities.Solving)

        try_number: int = 0
        for try_number in range(max_tries):
            logger.info(f"{op}: calling plate_solve ({try_number=} of {max_tries=})")
            exposure_settings.tags['try'] = try_number
            self.latest_solver_result = self.plate_solve(target=target, settings=exposure_settings)

            if self.latest_solver_result.state != 'found_match':
                msg = None
                if self.latest_solver_result.error_message:
                    msg = self.latest_solver_result.error_message
                elif self.latest_solver_result.last_log_message:
                    msg = self.latest_solver_result.last_log_message
                logger.info(f"{op}: plate solver failed state={self.latest_solver_result.state}, {msg=}")
                self.end_activity(UnitActivities.Solving)
                return False

            logger.info(f"plate solver found a match, yey!!!")
            solved_ra_arcsec: float = (
                Angle(self.latest_solver_result.solution.center_ra_j2000_rads * u.radian).arcsecond)
            solved_dec_arcsec: float = (
                Angle(self.latest_solver_result.solution.center_dec_j2000_rads * u.radian).arcsecond)
            delta_ra_arcsec: float = solved_ra_arcsec - target.ra.arcsecond
            delta_dec_arcsec: float = solved_dec_arcsec - target.dec.arcsecond

            if (abs(delta_ra_arcsec) <= solving_tolerance.ra.arcsecond and
                    abs(delta_dec_arcsec) <= solving_tolerance.dec.arcsecond):
                logger.info(f"{op}: within tolerances, actual: ({delta_ra_arcsec:.3f}, {delta_dec_arcsec:.3f}) " +
                            f"tolerance: ({solving_tolerance.ra.arcsecond:.3f}, " +
                            f"{solving_tolerance.dec.arcsecond:.3f}), done.")
                self.end_activity(UnitActivities.Solving)
                break

            logger.info(f"{op}: outside tolerances, actual: ({delta_ra_arcsec:.3f}, {delta_dec_arcsec:.3f}) " +
                        f"tolerance: ({solving_tolerance.ra.arcsecond:.3f}, {solving_tolerance.dec.arcsecond:.3f})")
            logger.info(f"{op}: offsetting mount by ({delta_ra_arcsec:.3f}, {delta_dec_arcsec:.3f}) arcsec ...")

            self.start_activity(UnitActivities.Correcting)
            self.pw.mount_offset(ra_add_arcsec=delta_ra_arcsec, dec_add_arcsec=delta_dec_arcsec)
            while self.mount.is_slewing:
                time.sleep(.5)
            time.sleep(5)
            self.end_activity(UnitActivities.Correcting)
            logger.info(f"{op}: mount stopped moving")
            # give it another try ...

        if try_number == max_tries - 1:
            self.end_activity(UnitActivities.Solving)
            logger.info(f"{op}: could not reach tolerances within {max_tries=}")
            return False

        return True

    def do_acquire(self, target_ra_j2000_hours: float, target_dec_j2000_degs: float):
        op = function_name()

        self.errors = []
        self.reference_image = None
        acquisition_conf = self.unit_conf['acquisition']
        self.start_activity(UnitActivities.Acquiring)
        self.start_activity(UnitActivities.Positioning)

        logger.info(f"acquisition: phase #1, stage at Sky position")
        #
        # move the stage and mount into position
        #
        self.stage.move_to_preset(StagePresetPosition.Sky)

        self.mount.start_tracking()
        self.mount.goto_ra_dec_j2000(target_ra_j2000_hours, target_dec_j2000_degs)
        while self.stage.is_moving or self.mount.is_slewing:
            time.sleep(1)
        logger.info(f"sleeping 10 seconds to let the mount stop ...")
        time.sleep(10)
        self.end_activity(UnitActivities.Positioning)

        #
        # set the camera for phase1 of acquisition mode (stage at Sky position)
        #
        acquisition_folder = PathMaker().make_acquisition_folder(
            tags={'target': f"{target_ra_j2000_hours},{target_dec_j2000_degs}"})
        phase1_settings = ExposureSettings(
            seconds=acquisition_conf['exposure'],
            purpose=ExposurePurpose.Acquisition,
            base_folder=acquisition_folder,
            gain=acquisition_conf['gain'],
            binning=Binning(acquisition_conf['binning']['x'], acquisition_conf['binning']['y']),
            roi=UnitRoi.from_dict(acquisition_conf['roi']).to_camera_roi(),
            save=True
        )

        #
        # loop trying to solve and correct the mount till within tolerances
        #
        tries: int = acquisition_conf['tries'] if 'tries' in acquisition_conf else 3
        default_tolerance: Angle = Angle(1 * u.arcsecond)
        ra_tolerance: Angle = default_tolerance
        dec_tolerance: Angle = default_tolerance
        if 'tolerance' in acquisition_conf:
            if 'ra_arcsec' in acquisition_conf['tolerance']:
                ra_tolerance = Angle(acquisition_conf['tolerance']['ra_arcsec'] * u.arcsecond)
            if 'dec_arcsec' in acquisition_conf['tolerance']:
                dec_tolerance = Angle(acquisition_conf['tolerance']['dec_arcsec'] * u.arcsecond)

        target = Coord(ra=Angle(target_ra_j2000_hours * u.hour), dec=Angle(target_dec_j2000_degs * u.deg))

        if not self.solve_and_correct(exposure_settings=phase1_settings,
                                      target=target,
                                      solving_tolerance=SolvingTolerance(ra_tolerance, dec_tolerance),
                                      caller='phase#1',
                                      max_tries=tries):
            logger.info(f"{op}: phase #1 (stage at Sky) failed")
            self.end_activity(UnitActivities.Acquiring)
            return

        #
        # we managed to get within tolerances
        #
        logger.info(f"acquisition: phase #2, stage at Spec position")

        self.stage.move_to_preset(StagePresetPosition.Spec)
        while self.stage.is_moving:
            time.sleep(.2)

        guiding_conf = self.unit_conf['guiding']
        binning: Binning = Binning(guiding_conf['binning'], guiding_conf['binning'])
        phase2_settings = ExposureSettings(
            seconds=guiding_conf['exposure'],
            purpose=ExposurePurpose.Acquisition,
            binning=binning,
            roi=UnitRoi.from_dict(guiding_conf['roi']).to_camera_roi(binning=binning),
            gain=guiding_conf['gain'] if 'gain' in guiding_conf else None,
            base_folder=acquisition_folder,
            save=True
        )
        success = self.solve_and_correct(exposure_settings=phase2_settings,
                                         target=target,
                                         solving_tolerance=SolvingTolerance(ra_tolerance, dec_tolerance),
                                         caller="phase#2",
                                         max_tries=tries)
        logger.info("phase #2 (stage at Spec) " + 'succeeded' if success else 'failed')
        if success:
            self.reference_image = self.camera.image
        self.end_activity(UnitActivities.Acquiring)

        # self.do_start_guiding_by_cross_correlation()

    def acquire(self, ra_j2000_hours: float, dec_j2000_degs: float):
        Thread(name='acquisition', target=self.do_acquire, args=[ra_j2000_hours, dec_j2000_degs]).start()

    def endpoint_start_guiding_by_cross_correlation(self):
        Thread(name='shift-analysis-guider', target=self.do_start_guiding_by_cross_correlation).start()

    def do_start_guiding_by_cross_correlation(self, base_folder: str | None = None):
        """
        Uses the last acquisition image as reference and phase_cross_correlation to detect pixel level shifts and
         correct them.
        """
        op = function_name()

        self.start_activity(UnitActivities.Guiding)
        #
        # prepare exposure settings for guiding
        #
        guiding_conf = self.unit_conf['guiding']

        guiding_roi: UnitRoi = UnitRoi(
            fiber_x=guiding_conf['fiber_x'],
            fiber_y=guiding_conf['fiber_y'],
            width=guiding_conf['width'],
            height=guiding_conf['height']
        )

        binning: Binning = Binning(guiding_conf['binning'], guiding_conf['binning'])
        guiding_settings: ExposureSettings = ExposureSettings(
            purpose=ExposurePurpose.Exposure,
            seconds=guiding_conf['exposure'],
            base_folder=base_folder,
            gain=guiding_conf['gain'],
            binning=binning,
            roi=guiding_roi.to_camera_roi(binning=binning),
            save=True
        )

        cadence: int = guiding_conf['cadence_seconds']
        min_ra_correction_arcsec: float = guiding_conf['min_ra_correction_arcsec']
        min_dec_correction_arcsec: float = guiding_conf['min_dec_correction_arcsec']

        min_ra_correction = Angle(min_ra_correction_arcsec * u.arcsecond)
        min_dec_correction = Angle(min_dec_correction_arcsec * u.arcsecond)
        pixel_scale_arcsec = self.unit_conf['camera']['pixel_scale_at_bin1'] * guiding_conf['binning']

        if self.reference_image is not None:
            logger.info(f"{op}: using existing reference image")
            reference_image = self.reference_image
        else:
            logger.info(f"{op}: taking a reference image {guiding_roi=}")
            self.camera.do_start_exposure(guiding_settings)
            logger.info(f"{op}: waiting for image ...")
            self.camera.wait_for_image_ready()
            logger.info(f"{op}: reference image is ready")
            reference_image = self.camera.image
            logger.info(f"{op}: got reference image from camera")

        while self.is_active(UnitActivities.Guiding):   # may be deactivated by stop_guiding()
            start = datetime.datetime.now()
            end = start + datetime.timedelta(seconds=cadence)

            response = self.camera.do_start_exposure(guiding_settings)
            if not response.succeeded:
                logger.error(f"{op}: failed to start_exposure ({response.errors=}")
                time.sleep(cadence)
                continue

            logger.info(f"{op}: waiting for the image ...")
            self.camera.wait_for_image_ready()
            logger.info(f"{op}: image is ready")
            image = self.camera.image
            logger.info(f"{op}: read the image from the camera")

            rotation_angle: Angle | None = Angle(self.latest_solver_result.solution.rotation_angle_degs * u.deg) \
                if self.latest_solver_result.state == 'found_match' else None
            if rotation_angle is None:
                raise Exception(f"{op}: missing rotation angle, cannot use cross correlation results")

            shifted_pixels, error, phase_diff = phase_cross_correlation(reference_image, image, upsample_factor=1000)
            #
            # TODO: formula for converting shifted_pixels into (ra,dec), using the rotation angle
            #
            delta_ra: Angle = Angle(shifted_pixels[1] * pixel_scale_arcsec * u.arcsecond)
            delta_dec: Angle = Angle(shifted_pixels[0] * pixel_scale_arcsec * u.arcsecond)

            delta_ra = delta_ra if abs(delta_ra.arcsecond) >= min_ra_correction else 0
            delta_dec = delta_dec if abs(delta_dec.arcsecond) >= min_dec_correction else 0

            if delta_ra or delta_dec:
                logger.info(f"{op}: correcting mount by ({delta_ra=}, {delta_dec=} ...")
                self.start_activity(UnitActivities.Correcting)
                self.pw.mount_offset(ra_add_arcsec=delta_ra.arcsecond, dec_add_arcsec=delta_dec.arcsecond)
                while self.mount.is_slewing:
                    time.sleep(.5)
                self.end_activity(UnitActivities.Correcting)

            now = datetime.datetime.now()
            if now < end:
                time.sleep((end - now).seconds)

    def expose_roi(self,
                   seconds: float | str = 3,
                   fiber_x: int | str = 6000,
                   fiber_y: int | str = 2500,
                   width: int | str = 500,
                   height: int | str = 300,
                   binning: int | str = 1,
                   gain: int | str = 170) -> CanonicalResponse:

        Thread(name='expose-roi-thread', target=self.do_expose_roi,
               args=[seconds, fiber_x, fiber_y, width, height, binning, gain]).start()
        return CanonicalResponse_Ok

    def do_expose_roi(self,
                      seconds: float | str = 3,
                      fiber_x: int | str = 6000,
                      fiber_y: int | str = 2500,
                      width: int | str = 1500,
                      height: int | str = 1300,
                      binning: int | str = 1,
                      gain: int | str = 170) -> CanonicalResponse:

        seconds = float(seconds) if isinstance(seconds, str) else seconds
        fiber_x = int(fiber_x) if isinstance(fiber_x, str) else fiber_x
        fiber_y = int(fiber_y) if isinstance(fiber_y, str) else fiber_y
        width = int(width) if isinstance(width, str) else width
        height = int(height) if isinstance(height, str) else height
        _binning = int(binning) if isinstance(binning, str) else binning
        gain = int(gain) if isinstance(gain, str) else gain

        if _binning not in [1, 2, 4]:
            return CanonicalResponse(errors=[f"bad {_binning=}, should be 1, 2 or 4"])

        unit_roi = UnitRoi(fiber_x, fiber_y, width, height)
        binning: Binning = Binning(_binning, _binning)
        context = camera.ExposureSettings(
            seconds=seconds,
            purpose=ExposurePurpose.Exposure,
            gain=gain,
            binning=binning,
            roi=unit_roi.to_camera_roi(binning=binning),
            tags={'expose-roi': None},
            save=True)
        self.camera.do_start_exposure(context)
        self.camera.wait_for_image_saved()
        Filer().move_ram_to_shared(self.camera.latest_settings.image_path)
        return CanonicalResponse_Ok

    def test_stage_repeatability(self,
                                 start_position: int | str = 50000,
                                 end_position: int | str = 300000,
                                 step: int | str = 25000,
                                 exposure_seconds: int | str = 5,
                                 binning: int | str = 1,
                                 gain: int | str = 170) -> CanonicalResponse:
        Thread(name='test-stage-repeatability', target=self.do_test_stage_repeatability,
               args=[start_position, end_position, step, exposure_seconds, binning, gain]).start()
        return CanonicalResponse_Ok

    def do_test_stage_repeatability(self,
                                    start_position: int | str = 50000,
                                    end_position: int | str = 300000,
                                    step: int | str = 25000,
                                    exposure_seconds: int | str = 5,
                                    binning: int | str = 1,
                                    gain: int | str = 170) -> CanonicalResponse:
        op = function_name()

        if isinstance(start_position, str):
            start_position = int(start_position)
        if isinstance(end_position, str):
            end_position = int(end_position)
        if isinstance(step, str):
            step = int(step)
        if isinstance(exposure_seconds, str):
            exposure_seconds = int(exposure_seconds)
        if isinstance(binning, str):
            binning = int(binning)
        if isinstance(gain, str):
            gain = int(gain)

        reference_position = start_position

        for position in range(start_position + step, end_position, step):
            logger.info(f"{op}: moving stage to {reference_position=}")
            self.stage.move_absolute(reference_position)
            while self.stage.is_active(StageActivities.Moving):
                time.sleep(.5)

            # expose at reference
            exposure_settings = camera.ExposureSettings(
                seconds=exposure_seconds,
                gain=gain,
                binning=Binning(binning, binning),
                roi=None,
                tags={
                    "stage-repeatability": None,
                    "reference-for": position,
                }, save=True)

            self.camera.do_start_exposure(exposure_settings)
            self.camera.wait_for_image_saved()
            logger.info(f"{op}: reference image was saved")
            Filer().move_ram_to_shared(exposure_settings.image_path)

            # expose at shifted position
            logger.info(f"{op}: moving stage to shifted {position=}")
            self.stage.move_absolute(position)
            while self.stage.is_active(StageActivities.Moving):
                time.sleep(.5)

            exposure_settings = camera.ExposureSettings(
                seconds=exposure_seconds,
                gain=gain,
                binning=Binning(binning, binning),
                roi=None,
                tags={
                    "stage-repeatability": None,
                    "position": position,
                },
                save=True)
            self.camera.do_start_exposure(exposure_settings)
            self.camera.wait_for_image_saved()
            logger.info(f"{op}: image at {position=} was saved")
            Filer().move_ram_to_shared(exposure_settings.image_path)

        logger.info(f"{op}: done.")
        return CanonicalResponse_Ok


def serialize_ip_addresses(data: Any) -> Any:
    if isinstance(data, dict):
        return {key: serialize_ip_addresses(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [serialize_ip_addresses(item) for item in data]
    elif isinstance(data, ipaddress.IPv4Address):
        return str(data)
    else:
        return data


unit_id: int | str | None = None
hostname = socket.gethostname()
if hostname.startswith('mast'):
    try:
        unit_id = int(hostname[4:])
    except ValueError:
        unit_id = hostname[4:]
else:
    logger.error(f"Cannot figure out the MAST unit_id ({hostname=})")

base_path = BASE_UNIT_PATH
tag = 'Unit'

unit: Unit | None = None
if not unit:
    unit = Unit(id_=unit_id)


def unit_route(sub_path: str):
    return base_path + sub_path


router = APIRouter()
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=unit.startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=unit.shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=unit.abort)
router.add_api_route(base_path + '/status', tags=[tag], endpoint=unit.status)
router.add_api_route(base_path + '/connect', tags=[tag], endpoint=unit.connect)
router.add_api_route(base_path + '/disconnect', tags=[tag], endpoint=unit.disconnect)
router.add_api_route(base_path + '/start_autofocus', tags=[tag], endpoint=unit.start_autofocus)
router.add_api_route(base_path + '/stop_autofocus', tags=[tag], endpoint=unit.stop_autofocus)
router.add_api_route(base_path + '/start_guiding_by_solving', tags=[tag], endpoint=unit.start_guiding_by_solving)
router.add_api_route(base_path + '/start_guiding_by_phase_correlation', tags=[tag],
                     endpoint=unit.endpoint_start_guiding_by_cross_correlation)
router.add_api_route(base_path + '/stop_guiding', tags=[tag], endpoint=unit.stop_guiding)
router.add_api_route(base_path + '/acquire', tags=[tag], endpoint=unit.acquire)
router.add_api_route(base_path + '/expose_roi', tags=[tag], endpoint=unit.expose_roi)
router.add_api_route(base_path + '/test_stage_repeatability', tags=[tag], endpoint=unit.test_stage_repeatability)
