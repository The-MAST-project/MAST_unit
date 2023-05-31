import inspect
from fastapi.openapi.utils import get_openapi
from mastapi import Mastapi
from docstring_parser import parse
from utils import Subsystem
from typing import Union


class TypeToSchema:
    t: type
    schema: dict

    def __init__(self, t: type, schema: dict):
        self.t = t
        self.schema = schema


def make_parameters(method_name, method) -> list:

    types_to_schemas = [
        TypeToSchema(int, {'type': 'integer', 'format': 'int32'}),
        TypeToSchema(float, {'type': 'number', 'format': 'float'}),
        TypeToSchema(str, {'type': 'string'}),
        TypeToSchema(Union[int, str], {'type': 'number', 'format': 'int32'}),
        TypeToSchema(Union[float, str], {'type': 'number', 'format': 'float'}),
    ]

    parameters_list = list()
    annotations = inspect.get_annotations(method)
    docstring = parse(method.__doc__.replace(':mastapi:\n', '')) if method.__doc__ else None

    for param_id, param_name in enumerate(annotations.keys()):
        if param_name == 'return':
            continue
        param_dict = {
            'name': param_name,
            'in': 'query',
            'required': True,
        }
        if docstring and docstring.params and param_id < len(docstring.params):
            param_dict['description'] = docstring.params[param_id].description
        param_type = annotations[param_name]

        found = [x for x in types_to_schemas if x.t == param_type]
        if len(found) == 0:
            print(f'make_parameters: method: {method_name}, parameter type {param_type} for param: {param_name}')
        param_dict['schema'] = found[0].schema if not len(found) == 0 else None
        parameters_list.append(param_dict)

    return parameters_list


def make_openapi_schema(app, subsystems: list[Subsystem]):

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
    for sub in subsystems:
        # subsystem_path = '/' + sub.obj_name.replace('unit.', '')
        tuples = inspect.getmembers(sub.obj, inspect.ismethod)
        for tup in tuples:
            method_name = tup[0]
            method = tup[1]
            path = f'/{sub.path}/{method_name}'
            if (sub.path == 'planewave' and method_name == 'status' or
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
                    'tags': [sub.path],
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