class Mastapi:

    @staticmethod
    def api_method(func):
        func.__dict__['mastapi'] = True

    @staticmethod
    def is_api_method(func):
        return 'mastapi' in func.__dict__.keys()
