import math
import os.path

from common.utils import function_name, Coord
from common.mast_logging import init_log
from common.filer import Filer
from common.extended_basemodel import ExtendedBaseModel
from acquisition import Acquisition
import logging
import time
from typing import List, Literal, Optional
from PlaneWave.ps3cli_client import PS3CLIClient
from camera import CameraSettings
from common.activities import UnitActivities
from common.corrections import Corrections, Correction
from enum import IntFlag
from astropy.coordinates import Angle
import astropy.units as u
import datetime
from multiprocessing.shared_memory import SharedMemory
import numpy as np
import json

PLATE_SOLVING_SHM_NAME = 'PlateSolving_Image'

logger = logging.Logger('mast.unit.' + __name__)
init_log(logger)


class PlateSolverExitCode(IntFlag):
    Success = 0,
    InvalidArguments = 1,
    CatalogNotFound = 2,
    NoStarMatch = 3,
    NoImageLoad = 4,
    GeneralFailure = 99


class PlateSolverResult(ExtendedBaseModel):
    succeeded: bool = False
    ra_j2000_hours: Optional[float] = None
    dec_j2000_degrees: Optional[float] = None
    arcsec_per_pixel: Optional[float] = None
    rot_angle_degs: Optional[float] = None
    errors: Optional[List[str]] = []

    # def __init__(self, d):
    #     self.succeeded = d['succeeded']
    #     if 'ra_j2000_hours' in d:
    #         self.ra_j2000_hours = d['ra_j2000_hours']
    #     if 'dec_j2000_degrees' in d:
    #         self.dec_j2000_degrees = d['dec_j2000_degrees']
    #     if 'rot_angle_degs' in d:
    #         self.rot_angle_degs = d['rot_angle_degs']
    #     if 'arcsec_per_pixel' in d:
    #         self.arcsec_per_pixel = d['arcsec_per_pixel']
    #     if 'errors' in d:
    #         self.errors = d['errors']

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
            return PlateSolverResult(**ret)

        except Exception as e:
            logger.error(f"{e}")
            return PlateSolverResult(**ret)


class PS3SolvingSolution(ExtendedBaseModel):
    num_matched_stars: int
    match_rms_error_arcsec: float
    match_rms_error_pixels: float
    center_ra_j2000_rads: float
    center_dec_j2000_rads: float
    matched_arcsec_per_pixel: float
    rotation_angle_degs: float


class PS3SolvingResult(ExtendedBaseModel):
    state: Literal['ready', 'loading', 'extracting', 'matching', 'found_match', 'no_match', 'error', 'unknown']
    error_message: Optional[str] = None
    last_log_message: Optional[str] = None
    num_extracted_stars: Optional[int] = None
    running_time_seconds: Optional[float] = None
    solution: Optional[PS3SolvingSolution] = None


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

            settings.make_file_name()

            #
            # Start exposure
            #
            logger.info(f'{op}: starting {settings.seconds=} acquisition exposure')
            response = self.unit.camera.do_start_exposure(settings)
            if response.failed:
                logger.error(f"{op}: could not start acquisition exposure: {response=}")
                return PS3SolvingResult(**{
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
            shared_image[:] = self.unit.camera.image[:]
            ps3_client: PS3CLIClient = PS3CLIClient()

            ps3_client.connect('127.0.0.1', 8998)
            start = datetime.datetime.now()
            timeout_seconds: float = 50
            end = start + datetime.timedelta(seconds=timeout_seconds)
            logger.info(f"{op}: calling ps3_client.begin_platesolve_shm ...")
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
                solver_status = PS3SolvingResult(**ps3_client.platesolve_status())

                if (solver_status.state == 'error' or
                        solver_status.state == 'no_match' or
                        solver_status.state == 'found_match'):
                    break

                if datetime.datetime.now() >= end:
                    ps3_client.platesolve_cancel()
                    solver_status = PS3SolvingResult(**{
                        'state': 'error',
                        'error_message': f'time out ({timeout_seconds} seconds), cancelled'
                    })
                    break
                else:
                    time.sleep(.1)

            self.unit.camera.wait_for_image_saved()
            filer.move_ram_to_shared(settings.image_path)

            return solver_status

    def solve_and_correct(self,
                          target: Coord,
                          camera_settings: CameraSettings,
                          solving_tolerance: SolvingTolerance,
                          parent_activity: UnitActivities | None = None,
                          phase: str | None = None,
                          max_tries: int = 3) -> bool:
        """
        Tries for max_tries times to:
        - Take an exposure using camera_settings
        - Plate solve the image
        - If the solved coordinates are NOT within the solving_tolerance from the target, correct the mount


        :param target: (ra, dec) tuple
        :param camera_settings: Camera settings for the exposure
        :param solving_tolerance: How close do we need to be to stop trying
        :param parent_activity: If the parent_activity (e.g. UnitActivities.Acquiring, UnitActivities.Guiding) is stopped, this function stops as well
        :param max_tries: How many times to try to get withing the solving_tolerance
        :param phase: One of ['sky', 'spec', 'guiding']

        :rtype: bool
        :return: True if succeeded to achieve tolerances within max_tries, False otherwise

        """
        op = function_name()
        if phase:
            op += f":{phase}"

        self.unit.start_activity(UnitActivities.Solving)

        if not self.unit.acquirer.latest_acquisition:
            # when not part of an acquisition sequence
            self.unit.acquirer.latest_acquisition = Acquisition(
                target.ra.arcsecond,
                target.dec.arcsecond,
                {
                    'tolerance': {
                        'ra_arcsec': solving_tolerance.ra.arcsecond,
                        'dec_arcsec': solving_tolerance.dec.arcsecond,
                    }
                }
            )
            self.unit.acquirer.latest_acquisition.corrections = {}

        if phase not in self.unit.acquirer.latest_acquisition.corrections:
            # in case there were no corrections yet for this phase
            self.unit.acquirer.latest_acquisition.corrections[phase] = Corrections(
                phase=phase,
                target_ra=target.ra.hour,
                target_dec=target.dec.deg,
                tolerance_ra=solving_tolerance.ra.arcsecond,
                tolerance_dec=solving_tolerance.dec.arcsecond,
            )
        latest_corrections = self.unit.acquirer.latest_acquisition.corrections[phase]

        for try_number in range(max_tries):
            if parent_activity and not self.unit.is_active(parent_activity):  # have we been cancelled?
                return False

            logger.info(f"{op}: calling plate_solve ({try_number=} of {max_tries=})")

            # run the plate solver
            result = None
            try:
                result = self.plate_solve(target=target, settings=camera_settings)
            except TimeoutError:
                logger.error(f"plate solving timed out, continuing ...")
                continue

            self.latest_result = result
            if result is None:
                logger.error(f"{op}: plate_solve returned None")
                continue

            # save the solver result for debugging
            result_file_name = camera_settings.image_path.replace('.fits', '-solver_result.json')
            os.makedirs(os.path.dirname(result_file_name), exist_ok=True)
            with open(result_file_name, 'w') as fp:
                fp.write(json.dumps(result.dict(), indent=2))
            time.sleep(2)
            Filer().move_ram_to_shared(result_file_name)

            #
            # From "PlateSolve3 server documentation"
            #
            # state: Indicates the state of the solver as one of the following values:
            # 	ready: Solver has not yet attempted a solve since starting, but is ready to accept a request
            # 	loading: Solver is loading the image
            # 	extracting: Solver is locating stars within the image
            # 	matching: Solver is attempting to match detected stars to the star catalog
            # 	found_match: Solver successfully performed a match and is finished processing
            # 	no_match: Solver failed to find a match and is finished processing
            # 	error: An error occurred and processing was stopped
            #
            # error_message: If "state" == "error", this contains a string describing the error. Otherwise, this is null
            #
            # last_log_message: A string containing a report of the most recently-performed step in the solver
            #

            if result.state in ['no_match', 'error', 'unknown']:
                msg = None
                if result.error_message:
                    msg = f"error_message: '{result.error_message}'"
                elif result.last_log_message:
                    msg = f"last_log_message: '{result.last_log_message}'"
                self.unit.errors.append(f"{op}: {try_number=}, {result.state=}, {msg=}")
                logger.info(f"{op}: plate solver failed state={result.state}, {msg=}")
                self.unit.end_activity(UnitActivities.Solving)
                continue  # next try

            elif result.state == 'found_match':
                logger.info(f"{op}: >>>>> plate solver found a match, YEY, YEPEEE, HURRAY !!! <<<")
                solved_ra_arcsec: float = Angle(result.solution.center_ra_j2000_rads * u.radian).arcsecond
                solved_dec_arcsec: float = Angle(result.solution.center_dec_j2000_rads * u.radian).arcsecond

                delta_dec_arcsec: float = target.dec.arcsecond - solved_dec_arcsec
                ang_rad: float = Angle(((target.dec.arcsecond + solved_dec_arcsec) / 2) * u.arcsecond).radian
                delta_ra_arcsec: float = (target.ra.arcsecond - solved_ra_arcsec) * math.cos(ang_rad)

                coord_solved = Coord(ra=Angle(result.solution.center_ra_j2000_rads * u.radian),
                                     dec=Angle(result.solution.center_dec_j2000_rads * u.radian))
                coord_delta = Coord(ra=Angle(delta_ra_arcsec * u.arcsecond), dec=Angle(delta_dec_arcsec * u.arcsecond))
                coord_tolerance = Coord(ra=solving_tolerance.ra, dec=solving_tolerance.dec)
                logger.info(f"{op}: target: {target}, solved: {coord_solved}, delta: {coord_delta}, " +
                            f"tolerance: {coord_tolerance}")

                if (abs(delta_ra_arcsec) <= solving_tolerance.ra.arcsecond and
                        abs(delta_dec_arcsec) <= solving_tolerance.dec.arcsecond):
                    #
                    # Within tolerance, no correction is needed
                    #
                    logger.info(f"{op}: within tolerances, deltas: ({delta_ra_arcsec:.9f}, {delta_dec_arcsec:.9f}) " +
                                f"tolerance: ({solving_tolerance.ra.arcsecond:.9f}, " +
                                f"{solving_tolerance.dec.arcsecond:.9f})")

                    latest_corrections.last_delta = Correction(
                        time=datetime.datetime.now(datetime.UTC),
                        ra_arcsec=delta_ra_arcsec,
                        dec_arcsec=delta_dec_arcsec
                    )

                    file_name = os.path.join(camera_settings.folder, 'corrections.json')
                    with open(file_name, 'w') as f:
                        json.dump(latest_corrections.to_dict(), f, indent=2)
                    Filer().move_ram_to_shared(file_name)

                    self.unit.end_activity(UnitActivities.Solving)
                    return True

                else:
                    #
                    # Outside of tolerance, need to correct
                    #
                    logger.info(f"{op}: outside of tolerances, deltas: ({delta_ra_arcsec:.9f}, " +
                                f"{delta_dec_arcsec:.9f}) " +
                                f"tolerance: ({solving_tolerance.ra.arcsecond:.9f}, " +
                                f"{solving_tolerance.dec.arcsecond:.9f})")
                    logger.info(f"{op}: OFFSETTING MOUNT BY ({delta_ra_arcsec:.9f}, {delta_dec_arcsec:.9f}) arcsec ...")

                    latest_corrections.sequence.append(Correction(
                        time=datetime.datetime.now(datetime.UTC),
                        ra_arcsec=delta_ra_arcsec,
                        dec_arcsec=delta_dec_arcsec,
                    ))

                    self.unit.start_activity(UnitActivities.Correcting)
                    self.unit.pw.mount_offset(ra_add_arcsec=delta_ra_arcsec, dec_add_arcsec=delta_dec_arcsec)
                    while self.unit.mount.is_slewing:
                        time.sleep(.5)
                    logger.info(f"sleeping 5 additional seconds to let the mount stop moving ...")
                    time.sleep(5)
                    self.unit.end_activity(UnitActivities.Correcting)
                    self.unit.errors.append(f"{op}: {try_number=}, " +
                                            f"corrected by {delta_ra_arcsec=:.6f}, {delta_dec_arcsec=:.6f}")
            else:
                logger.error(f"{op}: unknown/unexpected solver state '{result.state=}', continuing ...")
                continue    # next try

        #
        # By now the tries have been exhausted, and we're still not within tolerance
        #

        logger.info(f"{op}: could not reach tolerances within {max_tries=}")
        return False
