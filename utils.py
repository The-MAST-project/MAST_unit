import datetime
import os
import platform
from threading import Timer
from enum import Flag
from starlette.responses import Response
import json
from typing import Any
import logging
import io
from functools import wraps


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


class PrettyJSONResponse(Response):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=4,
            separators=(", ", ": "),
        ).encode("utf-8")


class DailyFileHandler(logging.FileHandler):

    filename: str = ''
    path: str

    def make_file_name(self):
        top = ''
        if platform.platform() == 'Linux':
            top = '/var/log/mast'
        elif platform.platform().startswith('Windows'):
            top = os.path.join(os.path.expandvars('%LOCALAPPDATA%'), 'mast')
        return os.path.join(top, f'{datetime.datetime.now():%Y-%m-%d}', self.path)

    def emit(self, record: logging.LogRecord):
        filename = self.make_file_name()
        if not filename == self.filename:
            if self.stream is not None:
                # we have an open file handle, clean it up
                self.stream.flush()
                self.stream.close()
                self.stream = None  # See Issue #21742: _open () might fail.

            self.baseFilename = filename
            os.makedirs(os.path.dirname(self.baseFilename), exist_ok=True)
            self.stream = self._open()
        logging.StreamHandler.emit(self, record=record)

    def __init__(self, path: str, mode='a', encoding=None, delay=False, errors=None ):
        self.path = path
        # self.filename = self.make_file_name()
        if "b" not in mode:
            encoding = io.text_encoding(encoding)
        logging.FileHandler.__init__(self, filename='', delay=True, mode=mode, encoding=encoding, errors=errors)


def init_log(logger: logging.Logger):
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    handler = DailyFileHandler(path='app.log', mode='a')
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    logger.info('initialized')


def mastapi(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        func.__dict__['mastapi'] = True
        return func(*args, **kwargs)
    return wrapper


def ismastapi(func):
    return 'mastapi' in func.__dict__.keys()


class HelpResponse:
    method: str
    help: str

    def __init__(self, method: str, doc: str):
        self.method = method
        self.help = doc

