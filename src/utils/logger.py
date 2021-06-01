import os
import traceback
import logging
from functools import wraps

def rank_zero_only(fn):
    
    @wraps(fn)
    def wrapped_fn(*args, **kwargs):
        if rank_zero_only.rank == 0:
            s =  traceback.extract_stack()
            filename, lineno, name, line = s[-2]
            args = list(args)
            args[0] = f'{filename}:{lineno} {args[0]}'
            args = tuple(args)
            return fn(*args, **kwargs)

    return wrapped_fn


# TODO: this should be part of the cluster environment
def _get_rank() -> int:
    rank_keys = ('RANK', 'SLURM_PROCID', 'LOCAL_RANK')
    for key in rank_keys:
        rank = os.environ.get(key)
        if rank is not None:
            return int(rank)
    return 0


# add the attribute to the function but don't overwrite in case Trainer has already set it
rank_zero_only.rank = getattr(rank_zero_only, 'rank', _get_rank())


def get_logger(name=__name__, level=logging.INFO) -> logging.Logger:
    """Initializes multi-GPU-friendly python logger."""

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    if logger.hasHandlers():
        logger.handlers.clear()
    formatter = '[%(asctime)s] [%(levelname)s] %(message)s'
    formatter = logging.Formatter(formatter)
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)

    # this ensures all logging levels get marked with the rank zero decorator
    # otherwise logs would get multiplied for each GPU process in multi-GPU setup
    for level in ("debug", "info", "warning", "error", "exception", "fatal", "critical"):
        setattr(logger, level, rank_zero_only(getattr(logger, level)))
        setattr(sh, level, rank_zero_only(getattr(logger, level)))

    logger.addHandler(sh)
    sh.close()
    return logger