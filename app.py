import uvicorn
from fastapi import FastAPI, Request
from PlaneWave import pwi4_client
from unit import Unit
from utils import init_log, PrettyJSONResponse, HelpResponse, quote, Subsystem, ensure_process_is_running
import inspect
from mastapi import Mastapi
from openapi import make_openapi_schema
import logging
from contextlib import asynccontextmanager
import psutil
import os

from sys import exit

unit_id = 17
logger = logging.Logger('mast')
logger.setLevel(logging.DEBUG)
init_log(logger)
unit = None
pw = None
try:
    pw = pwi4_client.PWI4()
    pw.status()
    unit = Unit(unit_id)
except pwi4_client.PWException as ex:
    logger.error('No PWI4', exc_info=ex)
    exit(1)
except Exception as ex:
    logger.error('Could not create a Unit object', exc_info=ex)

if not unit:
    logger.error('No unit')
    exit(1)


@asynccontextmanager
async def lifespan(fast_app: FastAPI):
    unit.start_lifespan()
    yield
    unit.end_lifespan()


app = FastAPI(
    docs_url='/docs',
    redocs_url=None,
    lifespan=lifespan,
    openapi_url='/mast/api/v1/openapi.json')

root = '/mast/api/v1/'


subsystems = [
    Subsystem(path='unit', obj=unit, obj_name='unit'),
    Subsystem(path='mount', obj=unit.mount, obj_name='unit.mount'),
    Subsystem(path='focuser', obj=unit.focuser, obj_name='unit.focuser'),
    Subsystem(path='camera', obj=unit.camera, obj_name='unit.camera'),
    Subsystem(path='stage', obj=unit.stage, obj_name='unit.stage'),
    Subsystem(path='covers', obj=unit.covers, obj_name='unit.covers'),
    Subsystem(path='planewave', obj=pw, obj_name='pw')
]

make_openapi_schema(app=app, subsystems=subsystems)


def app_quit():
    logger.info('Quiting at API request')
    parent_pid = os.getpid()
    parent = psutil.Process(parent_pid)
    for child in parent.children(recursive=True):  # or parent.children() for recursive=False
        child.kill()
    parent.kill()


@app.get(root + '{subsystem}/{method}', response_class=PrettyJSONResponse)
def do_item(subsystem: str, method: str, request: Request):

    sub = [s for s in subsystems if s.path == subsystem]
    if len(sub) == 0:
        return f'Invalid MAST subsystem \"{subsystem}\", valid ones: {", ".join([x.path for x in subsystems])}'

    sub = sub[0]
    all_method_tuples = inspect.getmembers(sub.obj, inspect.ismethod)
    api_method_tuples = [t for t in all_method_tuples if Mastapi.is_api_method(t[1]) or (sub.path == 'planewave' and not t[0].startswith('_'))]
    api_method_names = [t[0] for t in api_method_tuples]
    api_method_objects = [t[1] for t in api_method_tuples]

    if method == 'quit':
        app_quit()

    if method == 'help':
        responses = list()
        for i, obj in enumerate(api_method_objects):
            responses.append(HelpResponse(api_method_names[i],
                                          api_method_objects[i].__doc__.replace(':mastapi:\n', '').lstrip('\n').strip()))
        return responses

    if method not in api_method_names:
        return f'Invalid method "{method}" for subsystem {subsystem}, valid ones: {", ".join(api_method_names)}'

    cmd = f'{sub.obj_name}.{method}('
    for k, v in request.query_params.items():
        cmd += f"{k}={quote(v)}, "
    cmd = cmd.removesuffix(', ') + ')'

    return eval(cmd)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
