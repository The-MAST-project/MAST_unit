import logging

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import sys
import os
from common.mast_logging import init_log
from common.utils import function_name, Filer
from typing import List, NamedTuple

logger = logging.Logger('mast.unit.' + __name__)
init_log(logger)


# Function to determine if the environment is interactive
def is_interactive():
    return sys.stdin.isatty()


class Point(NamedTuple):
    x: int
    y: float
    label: str


def plot_autofocus_analysis(result: 'PS3FocusAnalysisResult', folder: str | None = None, pixel_scale: float = 0.2612):
    op = function_name()

    if not result:
        logger.error(f"{op}: result is None")
        return

    if not result.has_solution:
        logger.error(f"{op}: result doesn't have a solution")
        return

    points: List[Point] = []
    positions: List[int] = []
    star_diameters: List[float] = []

    for sample in result.focus_samples:
        if not sample.is_valid:
            continue
        points.append(
            Point(x=int(sample.focus_position), y=sample.star_rms_diameter_pixels, label=f"{sample.num_stars} stars"))
        star_diameters.append(sample.star_rms_diameter_pixels)
        positions.append(int(sample.focus_position))

    # Define the focuser positions (X values)
    x = np.linspace(start=min(positions) - 100, stop=max(positions) + 100,
                    num=abs(max(positions) - min(positions)) + 200)

    # Calculate the diameters for each X value using the given equation
    star_diameter = np.sqrt(result.vcurve_a * x ** 2 + result.vcurve_b * x + result.vcurve_c)

    # Calculate the X-coordinate of the minimum using the formula X_min = -B / (2 * A)
    x_min = -result.vcurve_b / (2 * result.vcurve_a)

    # Calculate the diameter at the minimum X position
    diameter_min = np.sqrt(result.vcurve_a * x_min ** 2 + result.vcurve_b * x_min + result.vcurve_c)

    # Plot the V-curve
    plt.figure(figsize=(8, 6))
    # plt.plot(x, star_diameter, label=r'$\sqrt{A \cdot X^2 + B \cdot X + C}$', color='blue')
    plt.plot(x, star_diameter, label='Star diameters (RMS, pixels)', color='blue')

    # Add a red tick at the minimum X position on the X-axis
    plt.axvline(x_min, ymin=0, ymax=diameter_min, color='red', linestyle=':', label=f'Best focus: {int(x_min)} microns')
    plt.scatter(x_min, diameter_min, color='red', zorder=5)

    # Add a black tick on the Y-axis at the minimum diameter
    min_diam_asec = diameter_min * pixel_scale
    plt.axhline(diameter_min, color='black', linestyle=':',
                label=f'Min. diam.: {diameter_min:.2f} px, {min_diam_asec:.2f} asec')

    # Add green lines for tolerance
    x_left = x_min - result.tolerance
    x_right = x_min + result.tolerance
    y_max = np.max(star_diameter)
    y_min = np.min(star_diameter)
    y_left = np.sqrt(result.vcurve_a * x_left**2 + result.vcurve_b * x_left + result.vcurve_c) / y_max
    y_right = np.sqrt(result.vcurve_a * x_right**2 + result.vcurve_b * x_right + result.vcurve_c) / y_max
    plt.axvline(x_left, ymin=0, ymax=y_left, color='green', linestyle=':', label='2.5% diam. increase')
    plt.axvline(x_right, ymin=0, ymax=y_right, color='green', linestyle=':')

    # Add circles and labels at the specified points
    for point in points:
        plt.scatter(point.x, point.y, color='green', edgecolor='black', s=100, zorder=5, marker='o')
        # Add label in small red font at NE corner
        plt.text(point.x + 5, point.y + 0.1, str(point.label), color='red', fontsize=10)

    # Add labels and title
    plt.title('Autofocus V-Curve')
    plt.xlabel('Focuser Position')
    plt.ylabel('Star Diameter (px)')

    tolerance_label = Patch(color='none', label=f"Tolerance: {result.tolerance} microns")
    # Show grid and legend
    plt.grid(True)
    plt.legend(handles=plt.gca().get_legend_handles_labels()[0] + [tolerance_label], loc='upper right', framealpha=1)

    # if is_interactive():
    #     plt.show()

    if folder:
        file: str = os.path.join(folder, 'vcurve.png')
        logger.info(f"{op}: saved plot in {file}")
        plt.savefig(file, format='png')
        Filer().move_ram_to_shared(file)

    plt.show()


class FocusSample:
    def __init__(self, is_valid: bool, focus_position: int, num_stars: int, star_rms_diameter_pixels: float):
        self.is_valid = is_valid
        self.focus_position = focus_position
        self.num_stars = num_stars
        self.star_rms_diameter_pixels = star_rms_diameter_pixels


class DummyResult:

    def __init__(self):
        self.has_solution: bool = True
        self.best_focus_position: int = 27444
        self.tolerance: float = 20
        self.vcurve_a: float = 0.003969732449914025
        self.vcurve_b: float = -218.95312265253094
        self.vcurve_c: float = 3019252.867956934
        self.focus_samples = [
            FocusSample(is_valid=True, focus_position=27444, num_stars=12, star_rms_diameter_pixels=14.059341433692959),
            FocusSample(is_valid=True, focus_position=27494, num_stars=13, star_rms_diameter_pixels=12.504629812575239),
            FocusSample(is_valid=True, focus_position=27544, num_stars=14, star_rms_diameter_pixels=11.868161311662165),
            FocusSample(is_valid=True, focus_position=27594, num_stars=18, star_rms_diameter_pixels=10.85730885089308),
        ]


class DummyStatus:

    def __init__(self):
        self.is_running = False
        self.has_solution: bool = True
        self.analysis_result = DummyResult()


if __name__ == '__main__':

    plot_autofocus_analysis(DummyResult(), 'C:\\Temp')