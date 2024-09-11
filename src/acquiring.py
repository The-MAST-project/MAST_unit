import time
import logging
from common.utils import function_name, Coord
from common.paths import PathMaker
from common.mast_logging import init_log
from common.activities import UnitActivities
from common.utils import UnitRoi
from stage import StagePresetPosition
from camera import CameraSettings, CameraBinning, ExposurePurpose
from astropy.coordinates import Angle
import astropy.units as u
from solving import SolvingTolerance
from threading import Thread

logger = logging.Logger('mast.unit.acquirer')
init_log(logger)


class Acquirer:

    def __init__(self, unit: 'Unit'):
        self.unit: 'Unit' = unit

    def do_acquire(self, target_ra_j2000_hours: float, target_dec_j2000_degs: float):
        op = function_name()

        self.unit.errors = []
        self.unit.reference_image = None
        acquisition_conf = self.unit.unit_conf['acquisition']
        self.unit.start_activity(UnitActivities.Acquiring)
        self.unit.start_activity(UnitActivities.Positioning)

        logger.info(f"acquisition: phase #1, stage at Sky position")
        #
        # move the stage and mount into position
        #
        self.unit.stage.move_to_preset(StagePresetPosition.Sky)

        self.unit.mount.start_tracking()
        self.unit.mount.goto_ra_dec_j2000(target_ra_j2000_hours, target_dec_j2000_degs)
        while self.unit.stage.is_moving or self.unit.mount.is_slewing:
            time.sleep(1)
        logger.info(f"sleeping 10 seconds to let the mount stop ...")
        time.sleep(10)
        self.unit.end_activity(UnitActivities.Positioning)

        #
        # set the camera for phase1 of acquisition mode (stage at Sky position)
        #
        acquisition_folder = PathMaker().make_acquisition_folder(
            tags={'target': f"{target_ra_j2000_hours},{target_dec_j2000_degs}"})
        phase1_settings = CameraSettings(
            seconds=acquisition_conf['exposure'],
            purpose=ExposurePurpose.Acquisition,
            base_folder=acquisition_folder,
            gain=acquisition_conf['gain'],
            binning=CameraBinning(acquisition_conf['binning']['x'], acquisition_conf['binning']['y']),
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

        if not self.unit.solver.solve_and_correct(exposure_settings=phase1_settings,
                                                  target=target,
                                                  solving_tolerance=SolvingTolerance(ra_tolerance, dec_tolerance),
                                                  caller='phase#1',
                                                  parent_activity=UnitActivities.Acquiring,
                                                  max_tries=tries):
            logger.info(f"{op}: phase #1 (stage at Sky) failed")
            self.unit.end_activity(UnitActivities.Acquiring)
            return

        #
        # we managed to get within tolerances
        #
        logger.info(f"acquisition: phase #2, stage at Spec position")

        self.unit.stage.move_to_preset(StagePresetPosition.Spec)
        while self.unit.stage.is_moving:
            time.sleep(.2)

        guiding_conf = self.unit.unit_conf['guiding']
        binning: CameraBinning = CameraBinning(guiding_conf['binning'], guiding_conf['binning'])
        phase2_settings = CameraSettings(
            seconds=guiding_conf['exposure'],
            purpose=ExposurePurpose.Acquisition,
            binning=binning,
            roi=UnitRoi.from_dict(guiding_conf['roi']).to_camera_roi(binning=binning),
            gain=guiding_conf['gain'] if 'gain' in guiding_conf else None,
            base_folder=acquisition_folder,
            save=True
        )
        success = self.unit.solver.solve_and_correct(exposure_settings=phase2_settings,
                                                     target=target,
                                                     solving_tolerance=SolvingTolerance(ra_tolerance, dec_tolerance),
                                                     caller="phase#2",
                                                     parent_activity=UnitActivities.Acquiring,
                                                     max_tries=tries)
        logger.info("phase #2 (stage at Spec) " + 'succeeded' if success else 'failed')
        if success:
            self.unit.reference_image = self.unit.camera.image
        self.unit.end_activity(UnitActivities.Acquiring)

        # self.unit.do_guide_by_solving_with_shm(target=target, folder=acquisition_folder)

    def acquire(self, ra_j2000_hours: float, dec_j2000_degs: float):
        Thread(name='acquisition', target=self.do_acquire, args=[ra_j2000_hours, dec_j2000_degs]).start()
