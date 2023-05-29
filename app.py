import uvicorn
from fastapi import FastAPI, Request
from PlaneWave import pwi4_client
from unit import Unit
from utils import init_log, PrettyJSONResponse, HelpResponse, ResultWithStatus
import inspect
from mastapi import Mastapi
from openapi import make_openapi_schema

import logging

unit_id = 17
logger = logging.getLogger('mast')
init_log(logger)

app = FastAPI(docs_url='/docs', redocs_url=None, openapi_url='/mast/api/v1/openapi.json')
pw = pwi4_client.PWI4()
unit = Unit(unit_id)
root = '/mast/api/v1/'


subsystems = {
    'unit': {'obj': unit, 'name': 'unit'},
    'mount': {'obj': unit.mount, 'name': 'unit.mount'},
    # 'power': {'obj': unit.power, 'name': 'unit.power'},
    'camera': {'obj': unit.camera, 'name': 'unit.camera'},
    'stage': {'obj': unit.stage, 'name': 'unit.stage'},
    'covers': {'obj': unit.covers, 'name': 'unit.covers'},
    'planewave': {'obj': pw, 'name': 'planewave'}
}


make_openapi_schema(app=app, subsystems=subsystems)


@app.get(root + '{subsystem}/{method}', response_class=PrettyJSONResponse)
def do_item(subsystem: str, method: str, request: Request):

    if subsystem in subsystems.keys():
        subsystem_object = subsystems[subsystem]['obj']
    else:
        return f'Invalid MAST subsystem \"{subsystem}\", valid ones: {", ".join(subsystems.keys())}'

    api_methods = list()
    api_method_names = list()
    tuples = inspect.getmembers(subsystem_object, inspect.ismethod)
    for tup in tuples:
        if Mastapi.is_api_method(tup[1]):
            api_method_names.append(tup[0])
            api_methods.append(tup[1])

    if method == 'help':
        responses = list()
        for i in range(len(api_methods)):
            responses.append(HelpResponse(api_method_names[i], api_methods[i].__doc__))
        return responses

    if method not in api_method_names:
        return f'Invalid method "{method}" for subsystem {subsystem}, valid ones: {", ".join(api_method_names)}'

    subsystem_name = subsystems[subsystem]['name']
    cmd = f'{subsystem_name}.{method}('
    for k, v in request.query_params.items():
        cmd += f"{k}={v}, "
    cmd = cmd.removesuffix(', ') + ')'
    # try:
    return eval(cmd)
    # except Exception as ex:
    #     ret = ResultWithStatus()
    #     ret.status = None
    #     ret.error = ex
    #     ret.result = None
    #     return ret


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
