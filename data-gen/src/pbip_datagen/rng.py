"""All randomness flows through a seeded generator so every dataset is reproducible."""

import numpy as np


def make_rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)
