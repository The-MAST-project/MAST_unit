import time
import logging
from common.utils import function_name, Coord
from common.paths import PathMaker
from common.mast_logging import init_log
from common.activities import UnitActivities
from common.utils import UnitRoi, CanonicalResponse
from common.corrections import correction_phases
from common.filer import Filer, FilerTop
from stage import StagePresetPosition
from camera import CameraSettings, CameraBinning
from astropy.coordinates import Angle
import astropy.units as u
from solving import SolvingTolerance
from threading import Thread
from acquisition import Acquisition
import os

logger = logging.getLogger('mast.unit.' + __name__)
init_log(logger)


class Acquirer:

    def __init__(self, unit: 'Unit'):
        self.unit: 'Unit' = unit
        self.folder: str | None = None
        self.latest_acquisition: Acquisition | None = None

    def do_solve_and_correct(self,
                             target_ra_j2000_hours: float,
                             target_dec_j2000_degs: float,
                             phase: str | None = None):
        """
        Performs a sequence of: exposure, plate solving and telescope correction.
        :param target_ra_j2000_hours:
        :param target_dec_j2000_degs:
        :param phase: Used as part of the acquisition folder path.  Will be either 'sky', 'spec' or 'guiding', defaults
                       to 'testing' when not called from an acquisition phase
        :return:
        """
        op = function_name()
        acquisition_conf = self.unit.unit_conf['acquisition']

        self.unit.start_activity(UnitActivities.Positioning)
        #
        # Move the stage and mount into position
        #
        preset: StagePresetPosition = StagePresetPosition.Sky if phase == 'sky' else StagePresetPosition.Spec
        self.unit.stage.move_to_preset(preset)

        self.unit.mount.goto_ra_dec_j2000(target_ra_j2000_hours, target_dec_j2000_degs)

        while self.unit.stage.is_moving or self.unit.mount.is_slewing:
            time.sleep(1)
        logger.info(f"{op}: sleeping 10 seconds to let the mount stop ...")
        time.sleep(10)
        self.unit.end_activity(UnitActivities.Positioning)

        # Prepare camera settings
        if phase is None:
            phase = 'testing'

        if phase not in correction_phases:
            msg = f"{op}: bad phase {phase}, must be one of {','.join(correction_phases)}"
            logger.error(msg)
            raise Exception(msg)

        #
        # Possible folder names:
        #  .../<date>/Acquisitions/target=<ra>,<dec>,time<datetime>/{sky|spec|guiding|testing,00000X}
        #
        self.folder = PathMaker().make_acquisition_folder(
            phase=phase,
            tags={
                'target': f"{target_ra_j2000_hours},{target_dec_j2000_degs}",
            })
        if phase == 'testing':
            self.folder += ',' + PathMaker().make_seq(self.folder)
        self.folder = os.path.join(self.folder, phase)
        os.makedirs(self.folder, exist_ok=True)

        acquisition_settings = CameraSettings(
            seconds=acquisition_conf['exposure'],
            base_folder=self.folder,
            gain=acquisition_conf['gain'],
            binning=CameraBinning(acquisition_conf['binning']['x'], acquisition_conf['binning']['y']),
            roi=UnitRoi.from_dict(acquisition_conf['roi']).to_camera_roi(),
            save=True
        )

        # Figure out tolerances
        default_tolerance: Angle = Angle(1 * u.arcsecond)
        ra_tolerance: Angle = default_tolerance
        dec_tolerance: Angle = default_tolerance
        if 'tolerance' in acquisition_conf:
            if 'ra_arcsec' in acquisition_conf['tolerance']:
                ra_tolerance = Angle(acquisition_conf['tolerance']['ra_arcsec'] * u.arcsecond)
            if 'dec_arcsec' in acquisition_conf['tolerance']:
                dec_tolerance = Angle(acquisition_conf['tolerance']['dec_arcsec'] * u.arcsecond)

        target = Coord(ra=Angle(target_ra_j2000_hours * u.hour), dec=Angle(target_dec_j2000_degs * u.deg))

        if not self.unit.solver.solve_and_correct(target=target,
                                                  camera_settings=acquisition_settings,
                                                  solving_tolerance=SolvingTolerance(ra_tolerance, dec_tolerance),
                                                  phase=phase,
                                                  parent_activity=UnitActivities.Acquiring,
                                                  max_tries=10):
            logger.info(f"{op}: solve_and_correct failed")
        logger.info(f"{op}: solve_and_correct done.")

    def do_acquire(self, acquisition: Acquisition):
        """
        Called from start_acquisition()

        :param acquisition:
        :return:
        """
        op = function_name()

        self.unit.errors = []
        self.unit.reference_image = None

        self.latest_acquisition = acquisition
        acquisition_conf = acquisition.conf
        target_ra_j2000_hours: float = acquisition.target_ra
        target_dec_j2000_degs: float = acquisition.target_dec

        self.unit.start_activity(UnitActivities.Acquiring)
        phase = 'sky'

        logger.info(f"{op}: >>>>>>>>>>>>>>>>>>>>>>>>>>")
        logger.info(f"{op}: >>> starting {phase=} <<<")
        logger.info(f"{op}: >>>>>>>>>>>>>>>>>>>>>>>>>>")
        #
        # move the stage and mount into position
        #
        self.unit.start_activity(UnitActivities.Positioning)
        self.unit.stage.move_to_preset(StagePresetPosition.Sky)

        self.unit.mount.start_tracking()
        self.unit.mount.goto_ra_dec_j2000(target_ra_j2000_hours, target_dec_j2000_degs)
        while self.unit.stage.is_moving or self.unit.mount.is_slewing:
            time.sleep(1)
        logger.info(f"{op}: sleeping 10 seconds to let the mount and stage stop moving ...")
        time.sleep(10)
        self.unit.end_activity(UnitActivities.Positioning)

        sky_settings = CameraSettings(
            seconds=acquisition_conf['exposure'],
            base_folder=os.path.join(self.latest_acquisition.folder, phase),
            gain=acquisition_conf['gain'],
            binning=CameraBinning(acquisition_conf['binning']['x'], acquisition_conf['binning']['y']),
            roi=UnitRoi.from_dict(acquisition_conf['roi']).to_camera_roi(),
            save=True
        )

        #
        # loop trying to solve and correct the mount till within tolerances
        #
        tries: int = acquisition_conf['tries'] if 'tries' in acquisition_conf else 3

        # set up the tolerances
        default_tolerance: Angle = Angle(1 * u.arcsecond)
        ra_tolerance: Angle = default_tolerance
        dec_tolerance: Angle = default_tolerance
        if 'tolerance' in acquisition_conf:
            if 'ra_arcsec' in acquisition_conf['tolerance']:
                ra_tolerance = Angle(acquisition_conf['tolerance']['ra_arcsec'] * u.arcsecond)
            if 'dec_arcsec' in acquisition_conf['tolerance']:
                dec_tolerance = Angle(acquisition_conf['tolerance']['dec_arcsec'] * u.arcsecond)

        target = Coord(ra=Angle(target_ra_j2000_hours * u.hour), dec=Angle(target_dec_j2000_degs * u.deg))

        achieved_tolerances = self.unit.solver.solve_and_correct(target=target,
                                                                 camera_settings=sky_settings,
                                                                 solving_tolerance=
                                                                 SolvingTolerance(ra_tolerance, dec_tolerance),
                                                                 parent_activity=UnitActivities.Acquiring,
                                                                 phase='sky',
                                                                 max_tries=tries)
        logger.info(f"{op}: {phase=} {achieved_tolerances=}")
        self.latest_acquisition.save_corrections(phase)

        if not achieved_tolerances:
            self.unit.end_activity(UnitActivities.Acquiring)
            self.unit.mount.stop_tracking()
            return

        phase = 'spec'
        logger.info(f"{op}: >>>>>>>>>>>>>>>>>>>>>>>>>>")
        logger.info(f"{op}: >>> starting {phase=} <<<")
        logger.info(f"{op}: >>>>>>>>>>>>>>>>>>>>>>>>>>")

        self.unit.stage.move_to_preset(StagePresetPosition.Spec)
        while self.unit.stage.is_moving:
            time.sleep(.2)
        logger.info(f"sleeping additional 5 seconds to let the stage stop moving ...")
        time.sleep(5)
        logger.info(f"stage now at {self.unit.stage.position}")

        spec_settings = self.unit.guider.make_guiding_settings(
            base_folder=os.path.join(self.latest_acquisition.folder, phase))
        achieved_tolerances = self.unit.solver.solve_and_correct(target=target,
                                                                 camera_settings=spec_settings,
                                                                 solving_tolerance=
                                                                 SolvingTolerance(ra_tolerance, dec_tolerance),
                                                                 phase=phase,
                                                                 parent_activity=UnitActivities.Acquiring,
                                                                 max_tries=tries)
        self.latest_acquisition.save_corrections(phase)
        logger.info(f"{op}: {phase=} {achieved_tolerances=}")
        if not achieved_tolerances:
            self.unit.end_activity(UnitActivities.Acquiring)
            self.unit.mount.stop_tracking()
            return

        self.unit.reference_image = self.unit.camera.image

        phase = 'guiding'
        logger.info(f"{op}: >>>>>>>>>>>>>>>>>>>>>>>>>>")
        logger.info(f"{op}: >>> starting {phase=} <<<")
        logger.info(f"{op}: >>>>>>>>>>>>>>>>>>>>>>>>>>")

        # the guider runs until UnitActivities.Guiding is stopped
        self.unit.guider.do_guide_by_solving_with_shm(
            target=target,
            folder=os.path.join(self.latest_acquisition.folder, phase)
        )

        self.unit.end_activity(UnitActivities.Acquiring)
        self.unit.mount.stop_tracking()
        self.unit.acquirer.latest_acquisition.post_process()

    def start_acquisition(self, ra_j2000_hours: float, dec_j2000_degs: float):
        """
        Starts an acquisition

        :param ra_j2000_hours: The target's RA
        :param dec_j2000_degs: The target's Dec
        :return: The folder path on the MAST-SHARE with the acquisition's products
        """
        acquisition = Acquisition(
            target_ra=ra_j2000_hours,
            target_dec=dec_j2000_degs,
            conf=self.unit.unit_conf['acquisition'],
        )
        Thread(name='acquisition', target=self.do_acquire, args=[acquisition]).start()

        return CanonicalResponse(value=Filer().change_top_to(FilerTop.Shared, acquisition.folder))

    def start_one_solve_and_correct(self, ra_j2000_hours: float, dec_j2000_degs: float):
        """
        This is for debugging via FastAPI, not for production
        """
        self.latest_acquisition = Acquisition(target_ra=ra_j2000_hours,
                                              target_dec=dec_j2000_degs,
                                              conf=self.unit.unit_conf['acquisition'])
        Thread(
            name='solve-and-correct',
            target=self.do_solve_and_correct,
            args=[ra_j2000_hours, dec_j2000_degs, 'testing']).start()
