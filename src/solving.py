from threading import Thread
from common.utils import function_name, init_log, PathMaker, Filer, CanonicalResponse_Ok, Coord
import logging
import time
from typing import List
from PlaneWave.ps3cli_client import PS3CLIClient, PS3AutofocusResult
from camera import CameraSettings
from unit import UnitActivities, UnitRoi
from enum import IntFlag
from astropy.coordinates import Angle
import astropy.units as u
import datetime
from multiprocessing.shared_memory import SharedMemory
import numpy as np

PLATE_SOLVING_SHM_NAME = 'PlateSolving_Image'

logger = logging.Logger('mast.unit.solving')
init_log(logger)


class PlateSolverExitCode(IntFlag):
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


class PS3SolvingSolution:
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


class PS3SolvingResult:
    state: str              # 'ready', 'loading', 'extracting', 'matching', 'found_match', 'no_match', 'error'
    error_message: str
    last_log_message: str
    num_extracted_stars: int
    running_time_seconds: float
    solution: PS3SolvingSolution

    def __init__(self, d: dict):
        self.state: str = d['state']
        self.error_message: str | None = d['error_message'] if 'error_message' in d else None
        self.last_log_message: str | None = d['last_log_message'] if 'last_log_message' in d else None
        self.num_extracted_stars: int = d['num_extracted_stars'] if 'num_extracted_stars' in d else 0
        self.running_time_seconds: float = d['running_time_seconds'] if 'running_time_seconds' in d else 0
        self.solution: PS3SolvingSolution | None = PS3SolvingSolution(d['solution']) if 'solution' in d else None


class SolvingTolerance:
    ra: Angle
    dec: Angle

    def __init__(self, ra: Angle, dec: Angle):
        self.ra = ra
        self.dec = dec


class Solver:

    def __init__(self, unit: 'Unit'):
        self.unit: 'Unit' = unit
        self.latest_result: PS3SolvingResult | None = None

    def plate_solve(self, settings: CameraSettings, target: Coord) -> PS3SolvingResult:
        op = function_name()

        while self.unit.is_active(UnitActivities.Solving):

            image_path = settings.image_path

            #
            # Start exposure
            #
            logger.info(f'{op}: starting {settings.seconds=} acquisition exposure')
            response = self.unit.camera.do_start_exposure(settings)
            if response.failed:
                logger.error(f"{op}: could not start acquisition exposure: {response=}")
                return PS3SolvingResult({
                    'state': 'error',
                    'error_message': f'could not start exposure ({[response.errors]})'
                })

            self.unit.camera.wait_for_image_ready()
            logger.info(f"{op}: image is ready")

            if settings.binning.x != settings.binning.y:
                raise Exception(f"cannot deal with non-equal horizontal and vertical binning " +
                                f"({settings.binning.x=}, {settings.binning.y=}")
            pixel_scale = self.unit.unit_conf['camera']['pixel_scale_at_bin1'] * settings.binning.x

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

            solver_status: PS3SolvingResult
            while True:
                solver_status = PS3SolvingResult(ps3_client.platesolve_status())

                if (solver_status.state == 'error' or
                        solver_status.state == 'no_match' or
                        solver_status.state == 'found_match'):
                    break

                if datetime.datetime.now() >= end:
                    ps3_client.platesolve_cancel()
                    solver_status = PS3SolvingResult({
                        'state': 'error',
                        'error_message': f'time out ({timeout_seconds} seconds), cancelled'
                    })
                    break
                else:
                    time.sleep(.1)

            self.unit.camera.wait_for_image_saved()
            filer.move_ram_to_shared(image_path)

            return solver_status

    def solve_and_correct(self,
                          target: Coord,
                          exposure_settings: CameraSettings,
                          solving_tolerance: SolvingTolerance,
                          caller: str | None = None,
                          parent_activity: UnitActivities | None = None,
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
        parent_activity: This function may be called from within a parent activity (Guiding, Acquiring, etc.).  If the
           parent_activity is stopped, we stop as well
        max_tries: How many times to try to get withing the solving_tolerance

        Returns
        -------
        boolean: True if succeeded within max_tries to get within solving_tolerance

        """
        op = function_name()
        if caller:
            op += f":{caller}"

        self.unit.start_activity(UnitActivities.Solving)

        try_number: int = 0
        for try_number in range(max_tries):
            if not self.unit.is_active(parent_activity):  # have we been cancelled?
                return False

            logger.info(f"{op}: calling plate_solve ({try_number=} of {max_tries=})")
            exposure_settings.tags['try'] = try_number

            result = self.plate_solve(target=target, settings=exposure_settings)
            self.latest_result = result

            if result.state != 'found_match':
                msg = None
                if result.error_message:
                    msg = result.error_message
                elif result.last_log_message:
                    msg = result.last_log_message
                logger.info(f"{op}: plate solver failed state={result.state}, {msg=}")
                self.unit.end_activity(UnitActivities.Solving)
                return False

            logger.info(f"plate solver found a match, yey!!!")
            solved_ra_arcsec: float = Angle(result.solution.center_ra_j2000_rads * u.radian).arcsecond
            solved_dec_arcsec: float = Angle(result.solution.center_dec_j2000_rads * u.radian).arcsecond

            delta_ra_arcsec: float = solved_ra_arcsec - target.ra.arcsecond
            delta_dec_arcsec: float = solved_dec_arcsec - target.dec.arcsecond

            coord_solved = Coord(ra=Angle(result.solution.center_ra_j2000_rads * u.radian),
                                 dec=Angle(result.solution.center_dec_j2000_rads * u.radian))
            coord_delta = Coord(ra=Angle(delta_ra_arcsec * u.arcsecond), dec=Angle(delta_dec_arcsec * u.arcsecond))
            coord_tolerance = Coord(ra=solving_tolerance.ra, dec=solving_tolerance.dec)
            logger.info(f"target: {target}, solved: {coord_solved}, delta: {coord_delta}, tolerance: {coord_tolerance}")

            if (abs(delta_ra_arcsec) <= solving_tolerance.ra.arcsecond and
                    abs(delta_dec_arcsec) <= solving_tolerance.dec.arcsecond):
                logger.info(f"{op}: within tolerances, actual: ({delta_ra_arcsec:.3f}, {delta_dec_arcsec:.3f}) " +
                            f"tolerance: ({solving_tolerance.ra.arcsecond:.3f}, " +
                            f"{solving_tolerance.dec.arcsecond:.3f}), done.")
                self.unit.end_activity(UnitActivities.Solving)
                break

            if not self.unit.is_active(parent_activity):  # have we been canceled?
                return False

            logger.info(f"{op}: outside tolerances, actual: ({delta_ra_arcsec:.3f}, {delta_dec_arcsec:.3f}) " +
                        f"tolerance: ({solving_tolerance.ra.arcsecond:.3f}, {solving_tolerance.dec.arcsecond:.3f})")
            logger.info(f"{op}: offsetting mount by ({delta_ra_arcsec:.3f}, {delta_dec_arcsec:.3f}) arcsec ...")

            self.unit.start_activity(UnitActivities.Correcting)
            self.unit.pw.mount_offset(ra_add_arcsec=delta_ra_arcsec, dec_add_arcsec=delta_dec_arcsec)
            while self.unit.mount.is_slewing:
                time.sleep(.5)
            time.sleep(5)
            self.unit.end_activity(UnitActivities.Correcting)
            logger.info(f"{op}: mount stopped moving")
            # give it another try ...

        if try_number == max_tries - 1:
            self.unit.end_activity(UnitActivities.Solving)
            logger.info(f"{op}: could not reach tolerances within {max_tries=}")
            return False

        return True
