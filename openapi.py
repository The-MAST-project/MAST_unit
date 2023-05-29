import inspect
from stage import StageState
from fastapi.openapi.utils import get_openapi
from mastapi import Mastapi


def make_parameters(method_name, method) -> dict:
    parameters_dict = dict()
    annotations = inspect.get_annotations(method)
    for param_name in annotations.keys():
        param_dict = dict()
        param_dict['name'] = param_name
        param_dict['in'] = 'query'
        param_dict['description'] = 'TBD'
        param_dict['required'] = True
        param_type = annotations[param_name]
        if param_name == 'return':
            continue
        schema = {}
        if param_type == StageState:
            schema = {
                'type': 'string',
                'enum': [str(StageState.In), str(StageState.Out)]
                }
        elif param_type == int:
            schema = {
                'type': 'integer',
                'format': 'int32'
            }
        elif param_type == float:
            schema = {
                'type': 'number',
                'format': 'float'
            }
        elif param_type == str:
            schema = {
                'type': 'string',
            }
        else:
            print(f'make_parameters: method: {method_name}, parameter type {param_type} for param: {param_name}')

        param_dict['schema'] = schema

        parameters_dict[param_name] = param_dict
    return parameters_dict


def make_openapi_schema(app, subsystems):

    openapi_schema = get_openapi(
        title="Welcome to the mistery show!",
        version="1.0",
        description="This page allows you to explore the MAST Api",
        routes=app.routes,
    )

    openapi_schema['servers'] = [{
        'url': 'http://127.0.0.1:8000/mast/api/v1'
    }]

    openapi_schema['paths'] = dict()
    for sub in subsystems.keys():
        subsystem_name = subsystems[sub]['name']
        subsystem_path = '/' + subsystems[sub]['name'].replace('unit.', '')
        subsystem_obj = subsystems[sub]['obj']
        tuples = inspect.getmembers(subsystem_obj, inspect.ismethod)
        for tup in tuples:
            method_name = tup[0]
            method = tup[1]
            path = subsystem_path + '/' + method_name
            if (subsystem_name == 'planewave' and method_name == 'status' or
                    method_name.startswith('mount_') or
                    method_name.startswith('focuser_') or
                    method_name.startswith('virtualcamera_')) or \
                    Mastapi.is_api_method(method):
                description = method.__doc__.replace(':mastapi:\n', '') if method.__doc__ is not None else None
                parameters = make_parameters(method_name, method)
            else:
                continue

            openapi_schema['paths'][path] = {
                'get': {
                    'tags': [sub],
                    'description': description,
                    'parameters': parameters,
                    'responses': {
                        '200': {
                            'description': 'OK'
                        }
                    }
                }
            }

    app.openapi_schema = openapi_schema
    return app.openapi_schema