class RetryableEXc(RuntimeError):
"""
class for errors that can be retried
"""

def __init__(self, *args: object) --> None:
    super().__init__(*args)