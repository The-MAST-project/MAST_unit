from threading import Timer
from enum import Flag


class RepeatTimer(Timer):
    def run(self):
        while not self.finished.wait(self.interval):
            self.function(*self.args,**self.kwargs)


class Activities:

    activities: Flag

    def start_activity(self, activity: Flag, logger):
        self.activities |= activity
        logger.info(f'activity {activity.name} - started')

    def end_activity(self, activity: Flag, logger):
        self.activities &= ~activity
        logger.info(f'activity {activity.name} - ended')

    def is_active(self, activity: Flag) -> bool:
        return not (self.activities & activity) == 0


class AscomDriverInfo:
    name: str
    description: str
    version: str

    def __init__(self, driver):
        self.name = driver.Name
        self.description = driver.Description
        self.version = driver.DriverVersion
