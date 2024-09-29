import logging
from common.paths import PathMaker
from common.mast_logging import init_log
from common.corrections import Corrections
from plotting import plot_corrections
import os
import json
from common.filer import Filer
from typing import Dict

logger = logging.getLogger('mast.unit.' + __name__)
init_log(logger)


class Acquisition:
    def __init__(self, target_ra: float, target_dec: float, conf: Dict):
        self.target_ra: float = target_ra
        self.target_dec: float = target_dec
        self.conf = conf
        self.ra_tolerance = conf['tolerance']['ra_arcsec']
        self.dec_tolerance = conf['tolerance']['dec_arcsec']
        self.corrections: Dict[str, Corrections] = {}
        self.folder = PathMaker().make_acquisition_folder(
            tags={
                'target': f"{target_ra},{target_dec}",
            })

    def save_corrections(self, phase: str):
        if phase in self.corrections:
            path = os.path.join(self.folder, phase, 'corrections.json')
            with open(path, 'w') as fp:
                json.dump((self.corrections[phase]).to_dict(), fp, indent=2)
                Filer().move_ram_to_shared(path)

    def post_process(self):
        plot_corrections(self.folder.replace(Filer().ram.root, Filer().shared.root))